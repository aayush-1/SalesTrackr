import re
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from backend.app.domain import Problem
from backend.app.manufacturing_backups import ManufacturingDriveBackupStore
from backend.app.manufacturing_drive import FOLDER_MIME, child_folder, relocate


@pytest.fixture
def drive():
    backend = Mock()
    rows = {
        "manufacturing-root": {
            "id": "manufacturing-root", "name": "ProductTrackr", "parents": [], "mimeType": FOLDER_MIME,
            "appProperties": {"existing": "keep"}, "properties": {"public": "keep"}, "trashed": False,
        },
    }
    versions, created = {"manufacturing-root": 1}, []
    backend.etag.side_effect = lambda key: f'"{versions[key]}"'
    backend.allocate.side_effect = lambda: f"reserved-{len(created) + 1}"
    backend._execute.side_effect = lambda request: request.execute()
    files = backend._api.return_value.files.return_value
    conditional = backend._conditional_api.return_value.files.return_value

    def get(fileId, **kwargs):
        def execute():
            if fileId not in rows:
                raise Problem("Missing", 503, "drive_missing")
            return deepcopy(rows[fileId])
        return Mock(execute=execute)

    def create(body, **kwargs):
        def execute():
            if body["id"] not in rows:
                rows[body["id"]] = {**deepcopy(body), "trashed": False}
                versions[body["id"]] = 1
                created.append(body["id"])
            return {"id": body["id"]}
        return Mock(execute=execute)

    def update(fileId, body, addParents=None, removeParents=None, **kwargs):
        request = Mock(headers={})

        def execute():
            if request.headers and request.headers["If-Match"] != backend.etag(fileId):
                raise Problem("Changed", 409, "stale_edit")
            if "properties" in body:
                for visibility, key in (("PRIVATE", "appProperties"), ("PUBLIC", "properties")):
                    rows[fileId][key] = {
                        prop["key"]: prop["value"] for prop in body["properties"]
                        if prop["visibility"] == visibility
                    }
            if "name" in body:
                rows[fileId]["name"] = body["name"]
            if addParents:
                assert rows[fileId]["parents"] == [removeParents]
                rows[fileId]["parents"] = [addParents]
            versions[fileId] += 1
            return {"id": fileId}
        request.execute.side_effect = execute
        return request

    def listing(query, **kwargs):
        parent = re.search(r"'([^']+)' in parents", query)[1]
        app = re.search(r"key='app' and value='([^']+)'", query)[1]
        mime = "application/zip" if "application/zip" in query else FOLDER_MIME
        return [
            deepcopy(row) for row in rows.values()
            if row["parents"] == [parent] and row.get("appProperties", {}).get("app") == app
            and row["mimeType"] == mime and not row["trashed"]
        ]

    files.get.side_effect, files.create.side_effect, files.update.side_effect = get, create, update
    conditional.update.side_effect = update
    backend._list.side_effect = listing
    backend._api.return_value.permissions.return_value.list.return_value.execute.return_value = {
        "permissions": [{"type": "user"}],
    }
    return backend, rows, files, conditional


def test_child_folders_are_reserved_once_and_preserve_existing_root_properties(drive):
    backend, rows, files, conditional = drive
    products = child_folder(backend, "manufacturing-root", "products")
    assert child_folder(backend, "manufacturing-root", "products") == products
    backups = child_folder(backend, "manufacturing-root", "backups")
    assert products != backups
    assert rows[products]["name"] == "Products" and rows[backups]["name"] == "Backups"
    assert rows[products]["parents"] == rows[backups]["parents"] == ["manufacturing-root"]
    assert rows["manufacturing-root"]["properties"] == {"public": "keep"}
    assert rows["manufacturing-root"]["appProperties"] == {
        "existing": "keep", "producttrackr_products": products, "producttrackr_backups": backups,
    }
    assert files.create.call_count == conditional.update.call_count == 2


def test_lost_child_creation_response_reuses_the_reserved_id(drive):
    backend, rows, files, _ = drive
    original = files.create.side_effect

    def lost(**kwargs):
        request = original(**kwargs)

        def execute():
            request.execute()
            raise Problem("Response lost", 503, "drive_network")
        return Mock(execute=execute)

    files.create.side_effect = lost
    folder = child_folder(backend, "manufacturing-root", "products")
    assert child_folder(backend, "manufacturing-root", "products") == folder
    assert files.create.call_count == 1
    rows[folder]["parents"] = ["elsewhere"]
    with pytest.raises(Problem, match="moved"):
        child_folder(backend, "manufacturing-root", "products")
    assert files.create.call_count == 1


def test_private_children_are_required_without_changing_sharing(drive):
    backend, _, _, _ = drive
    folder = child_folder(backend, "manufacturing-root", "backups")
    permissions = backend._api.return_value.permissions.return_value
    permissions.list.return_value.execute.return_value = {"permissions": [{"type": "anyone"}]}
    with pytest.raises(Problem, match="private"):
        child_folder(backend, "manufacturing-root", "backups")
    permissions.create.assert_not_called()
    permissions.update.assert_not_called()
    assert folder


def test_inventory_keeps_root_name_and_moves_only_managed_backup_zips(drive):
    backend, rows, _, _ = drive
    store = object.__new__(ManufacturingDriveBackupStore)
    store.backend = backend
    store.folder_id, store.folder_url, store.settings = "manufacturing-root", None, Mock()
    rows["managed"] = {
        "id": "managed", "name": "ProductTrackr-manual.zip", "parents": ["manufacturing-root"],
        "mimeType": "application/zip", "appProperties": {"app": store.app}, "trashed": False,
        "size": "20", "md5Checksum": "checksum", "createdTime": "2026-10-28T00:00:00Z",
    }
    rows["unrelated"] = {**rows["managed"], "id": "unrelated", "appProperties": {}}
    backend.etag.side_effect = lambda key: '"stable"'
    # Migration does not change content or the file ID, only its parent.
    backend._conditional_api.return_value.files.return_value.update.side_effect = (
        lambda fileId, addParents, removeParents, **kwargs: Mock(
            headers={}, execute=lambda: rows[fileId].update({"parents": [addParents]}),
        )
    )
    # Reserve the already-provisioned subfolder so this test isolates backup relocation.
    rows["backups-folder"] = {
        "id": "backups-folder", "name": "Backups", "parents": ["manufacturing-root"], "mimeType": FOLDER_MIME,
        "appProperties": {"app": "producttrackr-backups"}, "trashed": False,
    }
    rows["manufacturing-root"]["appProperties"]["producttrackr_backups"] = "backups-folder"
    with patch("backend.app.manufacturing_backups.DriveBackupStore") as shared:
        shared.return_value._folder.return_value = "backups-folder"
        assert [row["id"] for row in store.inventory()] == ["managed"]
    shared.assert_called_once_with(store.settings)
    assert rows["manufacturing-root"]["name"] == "ProductTrackr"
    assert rows["managed"]["parents"] == ["backups-folder"]
    assert rows["unrelated"]["parents"] == ["manufacturing-root"]
    assert store.folder_url.endswith("/backups-folder")


def test_relocation_rejects_foreign_items_before_writing(drive):
    backend, rows, _, conditional = drive
    rows["manufacturing-root"]["appProperties"]["app"] = "unrelated"
    with pytest.raises(Problem, match="moved or replaced"):
        relocate(
            backend, "manufacturing-root", "old", "new", FOLDER_MIME, {"app": "producttrackr-product-files"},
        )
    conditional.update.assert_not_called()
