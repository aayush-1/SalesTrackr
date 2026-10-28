import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.domain import Problem
from backend.app.main import create_app
from backend.app.manufacturing_storage import ManufacturingRepository
from backend.tests.test_manufacturing import app as app
from backend.tests.test_manufacturing import command, create
from backend.tests.test_manufacturing_backups import backup, headers, inspect, restore


@pytest.fixture
def configured_app(app):
    _, original = app
    settings = replace(
        original, token_json="configured-test-credential",
        manufacturing_backup_folder_id="manufacturing-root",
    )
    with TestClient(create_app(settings)) as client:
        yield client, settings


@pytest.fixture
def drive(configured_app):
    store = Mock()
    store._root_folder.return_value = "manufacturing-root"
    store.products_folder.return_value = "manufacturing-products"
    backend = store.backend
    backend._execute.side_effect = lambda request: request.execute()
    backend.allocate.side_effect = lambda: f"folder-{uuid4().hex}"
    backend.etag.return_value = '"folder-etag"'
    files = backend._api.return_value.files.return_value
    rows = {}

    def get(fileId, **kwargs):
        def execute():
            if fileId not in rows:
                raise Problem("Missing", 503, "drive_missing")
            return rows[fileId].copy()
        return Mock(execute=execute)

    def create_folder(body, **kwargs):
        def execute():
            rows.setdefault(body["id"], {**body, "trashed": False})
            return {"id": body["id"]}
        return Mock(execute=execute)

    def listing(query, **kwargs):
        product = re.search(r"key='product' and value='([^']+)'", query)[1]
        return [
            {"id": row["id"]} for row in rows.values()
            if row["appProperties"]["product"] == product
        ]

    files.get.side_effect, files.create.side_effect = get, create_folder
    def move(fileId, body, addParents=None, removeParents=None, **kwargs):
        def execute():
            if addParents:
                assert rows[fileId]["parents"] == [removeParents]
                rows[fileId]["parents"] = [addParents]
            if "title" in body:
                rows[fileId]["name"] = body["title"]
            return {"id": fileId}
        return Mock(execute=execute, headers={})

    backend._conditional_api.return_value.files.return_value.update.side_effect = move
    backend._list.side_effect = listing
    with patch("backend.app.manufacturing_files.ManufacturingDriveBackupStore", return_value=store):
        yield store, rows, files


def folder(client, product, expected=200, request_headers=None):
    response = client.post(
        f"/api/manufacturing/products/{product['id']}/drive-folder",
        json={},
        headers=request_headers if request_headers is not None else headers(client),
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_unconfigured_files_do_not_connect_to_drive_and_stale_requests_are_fenced(app):
    client, _ = app
    product = create(client, "products", name="Unconnected product")
    with patch("backend.app.manufacturing_files.ManufacturingDriveBackupStore") as store:
        assert folder(client, product)["configured"] is False
        folder(client, product, 403, {})
        folder(client, product, 409, {**headers(client), "X-ProductTrackr-Generation": uuid4().hex})
        store.assert_not_called()


def test_folders_are_per_product_and_stay_linked_after_renaming_and_lost_local_mapping(configured_app, drive):
    client, settings = configured_app
    store, rows, files = drive
    product = create(client, "products", name="Gear", reference="G-042")
    other = create(client, "products", name="Gear", reference="G-042")
    first = folder(client, product)
    assert first["configured"] is True
    assert len(rows) == 2  # Product creation already provisioned both folders.
    assert next(iter(rows.values()))["name"] == "G-042 - Gear"
    assert next(iter(rows.values()))["parents"] == ["manufacturing-products"]
    assert folder(client, product) == first
    assert folder(client, other)["url"] != first["url"]
    response, _ = command(
        client, f"/products/{product['id']}", {"name": "Renamed", "reference": "G-043"},
        product["version"], product["id"], "PUT",
    )
    assert response.status_code == 200
    assert rows[first["url"].rsplit("/", 1)[-1]]["name"] == "G-043 - Renamed"
    assert folder(client, product) == first
    repo = ManufacturingRepository(settings)
    repo.workspace.collection("productFolders").document(product["id"]).delete()
    assert folder(client, product) == first
    assert files.create.call_count == len(rows) == 2
    assert store.backend.allocate.call_count == 2
    assert repo.snapshot()[0]["products"][product["id"]]["reference"] == "G-043"
    saved = repo.read("products", product["id"])
    response, _ = command(
        client, f"/products/{product['id']}", {"name": "Name only", "reference": ""},
        saved["version"], product["id"], "PUT",
    )
    assert response.status_code == 200
    assert rows[first["url"].rsplit("/", 1)[-1]]["name"] == "Name only"
    assert files.create.call_count == 2


def test_concurrent_open_and_lost_drive_response_reuse_reserved_folder_id(configured_app, drive):
    client, _ = configured_app
    store, rows, files = drive
    original = files.create.side_effect

    def lost_response(**kwargs):
        request = original(**kwargs)

        def execute():
            request.execute()
            raise Problem("Response lost", 503, "drive_network")
        return Mock(execute=execute)

    files.create.side_effect = lost_response
    product = create(client, "products", name="Concurrent folder")
    identity = headers(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: folder(client, product, request_headers=identity), range(2)))
    assert results[0] == results[1]
    assert len(rows) == 1
    assert folder(client, product) == results[0]


def test_restore_keeps_files_and_moved_folders_fail_without_creating_replacements(configured_app, drive):
    client, settings = configured_app
    _, rows, files = drive
    product = create(client, "products", name="Restore folder")
    first = folder(client, product)
    saved = backup(client)
    create(client, "products", name="After backup")
    restore(client, saved, inspect(client, saved))
    reopened = folder(client, product)
    assert reopened["url"] == first["url"]
    assert reopened["generation"] != first["generation"]
    assert len(rows) == 2
    mapping = ManufacturingRepository(settings).workspace.collection("productFolders").document(
        product["id"],
    ).get().to_dict()
    rows[mapping["id"]]["parents"] = ["somewhere-else"]
    result = folder(client, product, 409)
    assert result["error"]["code"] == "product_folder_changed"
    ManufacturingRepository(settings).workspace.collection("productFolders").document(product["id"]).delete()
    assert folder(client, product, 409)["error"]["code"] == "product_folder_changed"
    assert files.create.call_count == 2


def test_existing_product_folder_moves_under_products_without_changing_its_id(configured_app, drive):
    client, settings = configured_app
    _, rows, files = drive
    product = create(client, "products", name="Existing drawing folder", reference="OLD-001")
    first = folder(client, product)
    repo = ManufacturingRepository(settings)
    reference = repo.workspace.collection("productFolders").document(product["id"])
    mapping = reference.get().to_dict()
    rows[mapping["id"]]["parents"] = ["manufacturing-root"]
    reference.update({"parentId": "manufacturing-root"})
    assert folder(client, product) == first
    assert rows[mapping["id"]]["parents"] == ["manufacturing-products"]
    assert reference.get().to_dict()["parentId"] == "manufacturing-products"
    assert files.create.call_count == 1


def test_failed_drive_provisioning_keeps_the_saved_product_and_can_retry(configured_app, drive):
    client, _ = configured_app
    store, rows, _ = drive
    store.products_folder.side_effect = Problem("Drive is unavailable", 503, "drive_network")
    response, body = command(client, "/products", {"name": "Saved despite folder outage"})
    assert response.status_code == 200 and response.json()["id"] == body["id"]
    assert response.json()["folderWarning"] == "Drive is unavailable"
    assert client.get(f"/api/manufacturing/products/{body['id']}").status_code == 200
    assert not rows
    store.products_folder.side_effect = None
    assert folder(client, {"id": body["id"]})["configured"] is True
    assert len(rows) == 1
