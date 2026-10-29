import hashlib
import io
import json
import os
import zipfile
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.backups import archive as sales_archive
from backend.app.config import Settings
from backend.app.domain import Problem
from backend.app.firestore_storage import FirestoreRepository
from backend.app.main import create_app
from backend.app.shared_backups import (
    ManufacturingRecoveryView,
    SharedBackupManager,
    SharedDriveStore,
    read_archive,
)
from backend.app.storage import digest, encode
from backend.tests.test_firestore import account, cleanup_body, cleanup_record, headers
from backend.tests.test_manufacturing import command, create
from scripts import firestore_admin


@pytest.fixture
def shared(tmp_path):
    if not os.getenv("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("Requires the local Firestore emulator.")
    suffix = uuid4().hex
    settings = Settings(
        "demo", tmp_path, "shared-salestrackr-root", "", "", "", False,
        storage_backend="firestore", firestore_project="demo-shared-trackr-tests",
        firestore_workspace=f"sales-{suffix}", manufacturing_enabled=True,
        manufacturing_project="demo-shared-trackr-tests", manufacturing_workspace=f"products-{suffix}",
    )
    sales = FirestoreRepository(settings)
    with TestClient(create_app(settings, sales)) as client:
        yield client, sales, settings


def backup(client, write_id=None):
    response = client.post("/api/backups", headers={"X-SalesTrackr-Request": "1"},
                           json={"writeId": write_id or uuid4().hex})
    assert response.status_code == 200, response.text
    return response.json()["backup"]


def test_one_backup_contains_both_workspaces_and_does_not_connect_to_drive(shared):
    client, sales, _ = shared
    customer = account(sales, "Shared backup account")
    product = create(client, "products", name="Shared backup product")
    recovery = client.app.state.recovery
    mapping = recovery.repo.products.workspace.collection("productFolders").document(product["id"])
    mapping.set({"id": "shared-product-folder", "parentId": "shared-products-folder"})
    with patch("backend.app.manufacturing_backups.DriveBackend") as drive:
        write_id = uuid4().hex
        row = backup(client, write_id)
        assert backup(client, write_id)["id"] == row["id"]
        drive.assert_not_called()
    content = client.get(f"/api/backups/{row['id']}/download").content
    data, manifest = read_archive(content)
    assert data["sales"][0]["id"] == customer["id"]
    assert data["manufacturing"]["products"][product["id"]]["name"] == "Shared backup product"
    assert data["manufacturing"]["productFolders"]["folders"][product["id"]]["url"].endswith(
        "/shared-product-folder",
    )
    assert manifest["counts"]["account"] == manifest["counts"]["products"] == 1
    with zipfile.ZipFile(io.BytesIO(content)) as zipped:
        assert set(zipped.namelist()) == {"manifest.json", "salestrackr.zip", "producttrackr.zip"}
    status = client.get("/api/settings/backup-recovery").json()
    assert status["includesManufacturing"] and status["manufacturingEnabled"]
    assert status["backups"][0]["id"] == row["id"]
    preference = client.post(
        "/api/settings/backup-recovery", headers=headers(sales), json={"weeklyEnabled": False},
    ).json()
    assert preference["manufacturingEnabled"] and preference["includesManufacturing"]
    view = ManufacturingRecoveryView(recovery)
    sliced, _ = view.get(row["id"])
    assert view.read_archive(sliced)[0] == data["manufacturing"]
    assert client.post("/api/manufacturing/backups", headers={"X-SalesTrackr-Request": "1"},
                       json={"writeId": uuid4().hex}).status_code == 409


def test_restore_switches_both_workspaces_together_and_retries_once(shared):
    client, sales, _ = shared
    customer = account(sales, "At recovery point")
    product = create(client, "products", name="At recovery point")
    recovery = client.app.state.recovery
    row = backup(client)
    content, _ = recovery.get(row["id"])
    account(sales, "After backup")
    extra = create(client, "products", name="After backup")
    preview = recovery.inspect(content, "local-demo")
    old_generation = sales.generation
    old_meta = client.get("/api/manufacturing/meta").json()
    stage = recovery.repo.products.stage

    def while_staging(data):
        response, _ = command(client, "/products", {"name": "Blocked while recovering"})
        assert response.status_code == 503
        assert sales.workspace.get().to_dict()["generation"] == old_generation
        assert recovery.repo.products.workspace.get().to_dict()["generation"] == old_meta["generation"]
        return stage(data)

    write_id = uuid4().hex
    with patch.object(recovery.repo.products, "stage", while_staging):
        result = recovery.restore(content, "local-demo", preview["token"], "RESTORE", write_id)
    assert recovery.restore(content, "local-demo", "", "RESTORE", write_id) == result
    assert result["generation"] != old_generation
    assert result["manufacturingGeneration"] != old_meta["generation"]
    assert client.get("/api/accounts").json()["items"][0]["entityId"] == customer["entityId"]
    assert client.get("/api/accounts").json()["total"] == 1
    assert client.get(f"/api/manufacturing/products/{product['id']}").status_code == 200
    assert client.get(f"/api/manufacturing/products/{extra['id']}").status_code == 404
    assert len(recovery.rows()) == 2
    stale = {
        "X-SalesTrackr-Request": "1", "X-ProductTrackr-Workspace": old_meta["workspaceId"],
        "X-ProductTrackr-Generation": old_meta["generation"],
    }
    assert command(client, "/products", {"name": "Stale form"}, headers=stale)[0].status_code == 409
    response = client.post("/api/writes/prepare", json={"entityType": "account"}, headers={
        "X-SalesTrackr-Request": "1", "X-SalesTrackr-Generation": old_generation,
    })
    assert response.status_code == 409


@pytest.mark.parametrize("failure", ["safety-backup", "product-stage"])
def test_failure_never_activates_only_one_workspace(shared, failure):
    client, sales, _ = shared
    account(sales)
    create(client, "products", name="Must remain live")
    recovery = client.app.state.recovery
    row = backup(client)
    content, _ = recovery.get(row["id"])
    preview = recovery.inspect(content, "local-demo")
    before = [repo.workspace.get().to_dict() for repo in (sales, recovery.repo.products)]
    target, method = (
        (recovery.store, "put") if failure == "safety-backup" else (recovery.repo.products, "stage")
    )
    with patch.object(target, method, side_effect=Problem("Injected recovery failure", 503)):
        with pytest.raises(Problem, match="Injected"):
            recovery.restore(content, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    for repo, original in zip((sales, recovery.repo.products), before):
        control = repo.workspace.get().to_dict()
        assert control["generation"] == original["generation"]
        assert control["activeDataset"] == original["activeDataset"]
        assert not control.get("maintenance")
        assert not control.get("backupLease")


def test_manufacturing_changes_invalidate_shared_preview_and_legacy_archive_cannot_wipe_it(shared):
    client, sales, _ = shared
    recovery = client.app.state.recovery
    row = backup(client)
    content, _ = recovery.get(row["id"])
    preview = recovery.inspect(content, "local-demo")
    create(client, "products", name="Added after shared preview")
    with pytest.raises(Problem, match="changed"):
        recovery.restore(content, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    legacy, _ = sales_archive([], source="manual", operation_id=uuid4().hex,
                              actor="local-demo", control=sales.control)
    with pytest.raises(Problem, match="combined"):
        recovery.inspect(legacy, "local-demo")
    assert client.get("/api/manufacturing/products").json()["total"] == 1


def test_shared_recovery_requires_same_database_and_authority_over_both_workspaces(shared):
    _, sales, settings = shared
    with pytest.raises(RuntimeError, match="same Firestore"):
        SharedBackupManager(sales, replace(settings, manufacturing_project="demo-another-project"))
    manager = object.__new__(SharedBackupManager)
    manager.settings = replace(
        settings, mode="production", recovery_admins=("both@example.test", "sales@example.test"),
        manufacturing_admins=("both@example.test", "products@example.test"),
    )
    assert manager.admin("both@example.test")
    assert not manager.admin("sales@example.test")
    assert not manager.admin("products@example.test")
    assert not manager.admin("ordinary@example.test")


def test_products_use_the_sales_drive_root_and_credential(shared):
    _, _, settings = shared
    settings = replace(
        settings, token_json="shared-credential",
    )
    with patch("backend.app.manufacturing_backups.DriveBackend") as drive:
        from backend.app.manufacturing_backups import ManufacturingDriveBackupStore
        ManufacturingDriveBackupStore(settings)
    drive.assert_called_once_with("shared-credential", settings.root_id, operator=True)


def test_same_in_progress_operation_remains_retryable_without_creating_a_second_request(shared):
    client, _, _ = shared
    manager = client.app.state.recovery
    write_id = uuid4().hex
    operation, _, owner = manager._lease(write_id, "local-demo", "manual")
    try:
        with pytest.raises(Problem) as pending:
            manager.backup(write_id, "local-demo")
        assert pending.value.status == 503
    finally:
        manager._finish(operation, owner)
    row = backup(client)
    content, _ = manager.get(row["id"])
    preview = manager.inspect(content, "local-demo")
    restore_id = uuid4().hex
    stage = manager.repo.products.stage

    def retry_while_staging(data):
        with pytest.raises(Problem) as pending:
            manager.restore(content, "local-demo", preview["token"], "RESTORE", restore_id)
        assert pending.value.status == 503
        return stage(data)

    with patch.object(manager.repo.products, "stage", retry_while_staging):
        assert manager.restore(content, "local-demo", preview["token"], "RESTORE", restore_id)["restored"]


def test_sales_cleanup_requires_a_verified_combined_backup_and_preserves_product_data(shared):
    client, sales, _ = shared
    owner = account(sales)
    record = cleanup_record(sales, owner["entityId"], "discarded")
    product = create(client, "products", name="Not deleted by sales cleanup")
    manager = client.app.state.recovery
    body = cleanup_body(record)
    with patch.object(manager.store, "put", side_effect=Problem("Safety backup failed", 503)):
        failed = client.post("/api/settings/record-cleanup", json=body, headers=headers(sales))
    assert failed.status_code == 503
    assert sales.current(record["entityId"])["id"] == record["id"]
    response = client.post("/api/settings/record-cleanup", json=body, headers=headers(sales))
    assert response.status_code == 200, response.text
    assert response.json()["deletedCount"] == 1
    assert client.get(f"/api/manufacturing/products/{product['id']}").status_code == 200
    rows = manager.rows()
    saved, _ = manager.read_archive(manager.get(rows[0]["id"])[0])
    assert any(row["id"] == record["id"] for row in saved["sales"])
    assert product["id"] in saved["manufacturing"]["products"]


def test_nested_corruption_is_rejected_even_with_recomputed_outer_checksums(shared):
    client, _, _ = shared
    content = client.app.state.recovery.get(backup(client)["id"])[0]
    with zipfile.ZipFile(io.BytesIO(content)) as source:
        files = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(files["manifest.json"])
    files["producttrackr.zip"] = b"corrupted nested workspace"
    manifest["files"]["producttrackr.zip"] = hashlib.sha256(files["producttrackr.zip"]).hexdigest()
    manifest["checksum"] = digest({key: value for key, value in manifest.items() if key != "checksum"})
    files["manifest.json"] = encode(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        for name, value in files.items():
            target.writestr(name, value)
    with pytest.raises(Problem, match="complete combined"):
        read_archive(output.getvalue())


def test_shared_drive_inventory_and_deletion_never_include_legacy_archives():
    store = object.__new__(SharedDriveStore)
    store.backend = Mock()
    store.backend._list.return_value = []
    with patch.object(store, "_folder", return_value="sales-backups"):
        assert store.inventory() == []
    assert "value='trackr-workspace-backup'" in store.backend._list.call_args.args[0]
    with patch("backend.app.backups.DriveBackupStore._metadata", return_value={
        "id": "legacy-backup", "appProperties": {"app": "salestrackr-firestore-backup"},
    }):
        with pytest.raises(Problem, match="not a combined"):
            store.trash("legacy-backup")
    store.backend.trash.assert_not_called()


def test_scheduled_backup_entrypoint_uses_the_same_combined_recovery(shared, capsys):
    client, sales, settings = shared
    account(sales)
    create(client, "products", name="Included by the scheduled entrypoint")
    with (
        patch.object(Settings, "load", return_value=settings),
        patch.object(firestore_admin, "FirestoreRepository", return_value=sales),
        patch("sys.argv", ["firestore_admin", "backup", "--scheduled"]),
    ):
        firestore_admin.main()
    assert "Verified backup" in capsys.readouterr().out
    row = client.app.state.recovery.rows()[0]
    assert row["source"] == "scheduled"
    assert row["counts"]["products"] == row["counts"]["account"] == 1
