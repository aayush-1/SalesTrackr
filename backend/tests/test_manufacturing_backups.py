import hashlib
import io
import json
import zipfile
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest

from backend.app.backups import BACKUP_RETENTION, LocalBackupStore
from backend.app.backups import archive as sales_archive
from backend.app.domain import Problem
from backend.app.manufacturing_backups import (
    FILES,
    PRODUCT_FOLDERS_FILE,
    ManufacturingBackupManager,
    ManufacturingDriveBackupStore,
    archive,
    checked_data,
    read_archive,
)
from backend.app.manufacturing_storage import ManufacturingRepository
from backend.app.storage import digest
from backend.tests.test_firestore import mock_drive_store
from backend.tests.test_manufacturing import app as app
from backend.tests.test_manufacturing import command, create
from backend.tests.test_manufacturing_records import apply, job
from backend.tests.test_manufacturing_records import process as process


def headers(client):
    meta = client.get("/api/manufacturing/meta").json()
    return {
        "X-SalesTrackr-Request": "1",
        "X-ProductTrackr-Workspace": meta["workspaceId"],
        "X-ProductTrackr-Generation": meta["generation"],
    }


def post(client, path, body, expected=200, request_headers=None):
    response = client.post(
        "/api/manufacturing" + path,
        json=body,
        headers=request_headers or headers(client),
    )
    assert response.status_code == expected, response.text
    return response.json()


def backup(client, write_id=None):
    return post(client, "/backups", {"writeId": write_id or uuid4().hex})["backup"]


def inspect(client, row):
    return post(client, "/backups/inspect", {"backupId": row["id"]})


def restore(client, row, preview, write_id=None, expected=200, request_headers=None):
    return post(
        client,
        "/backups/restore",
        {
            "backupId": row["id"],
            "previewToken": preview["token"],
            "writeId": write_id or uuid4().hex,
            "confirmation": "RESTORE",
        },
        expected,
        request_headers,
    )


def test_archive_includes_unlinked_masters_all_links_jobs_history_counters_and_receipts(process):
    client, settings, product, company, contact, *_ = process
    create(client, "products", name="Standalone unused Product")
    create(client, "operations", name="Standalone unused operation")
    create(client, "partners", name="Standalone unused company")
    create(client, "contacts", name="Unlinked contact at A", partnerId=company["id"])
    current, _ = job(client, product)
    current = apply(client, current, "assign", partnerId=company["id"], contactId=contact["id"])
    current = apply(client, current, "complete")
    current = apply(client, current, "cancel", reason="Retain cancelled job too")
    job(client, product)
    repo = ManufacturingRepository(settings)
    expected, control = repo.snapshot()
    operation_id = uuid4().hex
    row = backup(client, operation_id)
    assert backup(client, operation_id)["id"] == row["id"]
    downloaded = client.get(f"/api/manufacturing/backups/{row['id']}/download")
    assert downloaded.status_code == 200
    assert "ProductTrackr-" in downloaded.headers["content-disposition"]
    content, manifest = read_archive(downloaded.content)
    assert content == expected
    assert manifest["counts"] == row["counts"] == checked_data(expected)
    assert manifest["sequence"] == control["sequence"]
    assert manifest["counts"]["products"] == 2
    assert manifest["counts"]["contacts"] == 3
    assert manifest["counts"]["manufacturingRecords"] == 2
    assert manifest["counts"]["manufacturingRecordEvents"] == 5
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as zipped:
        assert set(zipped.namelist()) == {
            "manifest.json", "workspace-settings.json", PRODUCT_FOLDERS_FILE, *FILES.values(),
        }
        assert b"\n  " in zipped.read("products.json")
    status = client.get("/api/manufacturing/settings/backup-recovery").json()
    assert status["provider"] == "local" and status["retention"] == 10
    assert len(status["backups"]) == 1
    assert client.get("/api/settings/backup-recovery").json()["backups"] == []


def test_legacy_backups_without_product_folder_links_still_restore(process):
    client, settings, product, *_ = process
    repo = ManufacturingRepository(settings)
    original, control = repo.snapshot()
    original.pop("productFolders")
    content, manifest = archive(
        original, source="manual", operation_id=uuid4().hex, actor="local-demo", control=control,
    )
    assert manifest["version"] == 1
    assert read_archive(content)[0] == original
    create(client, "products", name="Not present in the legacy backup")
    manager = ManufacturingBackupManager(repo, settings)
    preview = manager.inspect(content, "local-demo")
    result = manager.restore(content, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    assert result["restored"] is True
    assert client.get("/api/manufacturing/products").json()["items"][0]["id"] == product["id"]
    restored, _ = ManufacturingRepository(settings).snapshot()
    assert restored.pop("productFolders")["folders"] == {}
    assert restored == original


@pytest.mark.parametrize("damage", ["url", "unknown-product", "duplicate-folder", "missing-root"])
def test_invalid_product_folder_links_are_rejected(process, damage):
    client, settings, product, *_ = process
    other = create(client, "products", name="Other product")
    data, _ = ManufacturingRepository(settings).snapshot()
    mapping = {
        "id": "original-product-folder", "parentId": "manufacturing-products",
        "url": "https://drive.google.com/drive/folders/original-product-folder",
    }
    data["productFolders"]["rootId"] = "manufacturing-root"
    folders = data["productFolders"]["folders"]
    folders[product["id"]] = mapping
    if damage == "url":
        mapping["url"] = "https://example.invalid/untrusted"
    elif damage == "unknown-product":
        folders[uuid4().hex] = folders.pop(product["id"])
    elif damage == "duplicate-folder":
        folders[other["id"]] = mapping.copy()
    else:
        data["productFolders"]["rootId"] = ""
    with pytest.raises(Problem, match="consistent"):
        checked_data(data)


def test_product_folder_file_is_checksum_verified_before_restore(process):
    client, _, *_ = process
    saved = backup(client)
    original = client.get(f"/api/manufacturing/backups/{saved['id']}/download").content
    damaged = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(damaged, "w") as target:
        for item in source.infolist():
            value = source.read(item.filename)
            if item.filename == PRODUCT_FOLDERS_FILE:
                value += b" "
            target.writestr(item.filename, value)
    response = client.post(
        "/api/manufacturing/backups/inspect", content=damaged.getvalue(),
        headers={**headers(client), "Content-Type": "application/zip"},
    )
    assert response.status_code == 422


def test_full_restore_is_atomic_fences_old_forms_preserves_sales_and_retries_once(process):
    client, settings, product, company, *_ = process
    current, _ = job(client, product)
    current = apply(client, current, "assign", partnerId=company["id"])
    repo = ManufacturingRepository(settings)
    original, _ = repo.snapshot()
    sales_before = client.get("/api/accounts").json()["items"]
    row = backup(client)
    create(client, "products", name="Change after backup")
    current = apply(client, current, "complete")
    preview = inspect(client, row)
    old_headers = headers(client)
    write_id = uuid4().hex
    result = restore(client, row, preview, write_id)
    assert result["restored"] is True
    assert result["generation"] != old_headers["X-ProductTrackr-Generation"]
    restored_repo = ManufacturingRepository(settings)
    restored, _ = restored_repo.snapshot()
    assert restored == original
    assert restored_repo.control["version"] == restored_repo.control["sequence"]
    assert client.get("/api/accounts").json()["items"] == sales_before
    assert command(client, "/products", {"name": "Stale form"}, headers=old_headers)[0].status_code == 409
    assert restore(client, row, preview, write_id, request_headers=old_headers) == result
    assert len(client.get("/api/manufacturing/settings/backup-recovery").json()["backups"]) == 2
    assert (
        restored_repo.workspace.collection("datasets").document("legacy").get().to_dict()["state"]
        == "deleted"
    )
    assert not list(restored_repo.workspace.collection("products").stream())
    assert not list(restored_repo.workspace.collection("manufacturingRecordEvents").stream())
    receipt_id, receipt = next(iter(original["writes"].items()))
    assert (
        command(
            client,
            "/products",
            {},
            body={
                "writeId": receipt_id,
                "id": receipt["result"]["id"],
                "baseVersion": None,
                "data": {},
            },
        )[0].status_code
        == 409
    )
    next_job, _ = job(client, product)
    assert next_job["number"] == "MFG-00002"
    assert next_job["status"] == "ready"
    assert (
        client.get(f"/api/manufacturing/records/{current['id']}").json()["record"]["status"] == "in_progress"
    )


def test_preview_staleness_and_confirmation_do_not_change_live_data(process):
    client, settings, product, *_ = process
    job(client, product)
    row = backup(client)
    preview = inspect(client, row)
    create(client, "partners", name="Created after preview")
    restore(client, row, preview, expected=409)
    assert client.get("/api/manufacturing/partners?search=Created after preview").json()["total"] == 1
    repo = ManufacturingRepository(settings)
    assert repo.control["activeDataset"] == "legacy"
    assert len(client.get("/api/manufacturing/settings/backup-recovery").json()["backups"]) == 1
    post(
        client,
        "/backups/restore",
        {
            "backupId": row["id"],
            "writeId": uuid4().hex,
            "previewToken": preview["token"],
            "confirmation": "",
        },
        422,
    )
    assert not repo.workspace.get().to_dict().get("maintenance")


def test_failed_pre_restore_backup_and_failed_staging_never_replace_live_data(process):
    client, settings, product, *_ = process
    job(client, product)
    manager = ManufacturingBackupManager(ManufacturingRepository(settings), settings)
    row = manager.backup(uuid4().hex, "local-demo")
    content, _ = manager.get(row["id"])
    create(client, "products", name="Must survive failures")
    preview = manager.inspect(content, "local-demo")
    generation = manager.repo.control["generation"]
    with patch.object(manager.store, "put", side_effect=Problem("Upload failed", 503)):
        with pytest.raises(Problem, match="Upload failed"):
            manager.restore(content, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    assert manager.repo.workspace.get().to_dict()["generation"] == generation
    assert not manager.repo.workspace.get().to_dict().get("maintenance")
    with patch.object(manager.repo, "stage", side_effect=Problem("Stage failed", 503)):
        with pytest.raises(Problem, match="Stage failed"):
            manager.restore(content, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    assert manager.repo.workspace.get().to_dict()["generation"] == generation
    assert client.get("/api/manufacturing/products?search=Must survive").json()["total"] == 1
    assert not manager.repo.workspace.get().to_dict().get("maintenance")


def test_restore_blocks_writes_until_activation_and_rechecks_staged_contents(process):
    client, settings, product, *_ = process
    job(client, product)
    repo = ManufacturingRepository(settings)
    manager = ManufacturingBackupManager(repo, settings)
    row = manager.backup(uuid4().hex, "local-demo")
    content, _ = manager.get(row["id"])
    preview = manager.inspect(content, "local-demo")
    original_export = repo.export_at
    attempted = []

    def export(target, preferences):
        if target.path != repo.workspace.path:
            attempted.append(command(client, "/products", {"name": "Blocked during restore"})[0].status_code)
            value = original_export(target, preferences)
            value["products"].pop(product["id"])
            return value
        return original_export(target, preferences)

    with patch.object(repo, "export_at", side_effect=export):
        with pytest.raises(Problem):
            manager.restore(content, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    assert attempted == [503]
    assert repo.workspace.get().to_dict()["activeDataset"] == "legacy"
    assert client.get("/api/manufacturing/products?search=Blocked during restore").json()["total"] == 0


def test_ten_newest_backups_across_sources_and_no_pruning_after_failed_verification(process):
    client, settings, product, *_ = process
    job(client, product)
    manager = ManufacturingBackupManager(ManufacturingRepository(settings), settings)
    saved = [
        manager.backup(uuid4().hex, "local-demo", source=("manual", "scheduled", "pre-restore")[i % 3])
        for i in range(12)
    ]
    assert len(manager.rows()) == BACKUP_RETENTION == len(manager.store.inventory())
    assert {row["id"] for row in manager.rows()} == {row["id"] for row in saved[2:]}
    before = {row["id"] for row in manager.rows()}
    with patch.object(manager.store, "read", side_effect=[None, b"wrong readback"]):
        with pytest.raises(Problem, match="readback"):
            manager.backup(uuid4().hex, "local-demo")
    assert before.issubset({row["id"] for row in manager.rows()})
    assert len(list((manager.store.directory / "trash").iterdir())) == 2


@pytest.mark.parametrize(
    "damage",
    [
        "missing-contact-company",
        "missing-product-step",
        "wrong-primary",
        "missing-history",
        "counter-too-low",
        "invalid-step-status",
        "wrong-product-owner",
    ],
)
def test_relationship_and_history_validation_rejects_even_rechecksummed_incomplete_data(process, damage):
    client, settings, product, company, *_ = process
    current, _ = job(client, product)
    data, control = ManufacturingRepository(settings).snapshot()
    broken = deepcopy(data)
    first_step = next(iter(broken["steps"].values()))
    if damage == "missing-contact-company":
        broken["partners"].pop(company["id"])
    elif damage == "missing-product-step":
        broken["steps"].pop(first_step["id"])
    elif damage == "wrong-primary":
        first_step["primaryAssignmentId"] = uuid4().hex
    elif damage == "missing-history":
        broken["manufacturingRecordEvents"].clear()
    elif damage == "counter-too-low":
        broken["sequences"]["manufacturing-records"]["value"] = 0
    elif damage == "invalid-step-status":
        broken["manufacturingRecords"][current["id"]]["operations"][1]["status"] = "completed"
    elif damage == "wrong-product-owner":
        first_step["productId"] = uuid4().hex
    with pytest.raises(Problem, match="consistent"):
        checked_data(broken)
    valid, manifest = archive(
        data, source="manual", operation_id=uuid4().hex, actor="local-demo", control=control
    )
    with zipfile.ZipFile(io.BytesIO(valid)) as original:
        files = {name: original.read(name) for name in original.namelist() if name != "manifest.json"}
    for kind, filename in FILES.items():
        files[filename] = json.dumps(broken[kind]).encode()
    manifest["files"] = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
    manifest["counts"] = {kind: len(broken[kind]) for kind in FILES}
    manifest["dataHash"] = digest(broken)
    manifest["checksum"] = digest({key: value for key, value in manifest.items() if key != "checksum"})
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("manifest.json", json.dumps(manifest))
        for name, value in files.items():
            zipped.writestr(name, value)
    with pytest.raises(Problem):
        read_archive(result.getvalue())


def test_sales_archives_and_unexpected_collections_are_never_silently_accepted(process):
    client, settings, product, *_ = process
    job(client, product)
    content, _ = sales_archive(
        [],
        source="manual",
        operation_id=uuid4().hex,
        actor="local-demo",
        control={
            "generation": uuid4().hex,
            "sequence": 0,
        },
    )
    response = client.post(
        "/api/manufacturing/backups/inspect",
        content=content,
        headers={
            **headers(client),
            "Content-Type": "application/zip",
        },
    )
    assert response.status_code == 422
    repo = ManufacturingRepository(settings)
    repo.workspace.collection("unknownBusinessData").document("unknown-record").set(
        {"value": "must not be omitted"}
    )
    with pytest.raises(Problem, match="cannot be omitted"):
        repo.snapshot()
    assert repo.workspace.get().to_dict()["activeDataset"] == "legacy"


def test_drive_folder_is_private_separate_and_only_producttrackr_files_are_managed():
    base = mock_drive_store(b"archive")
    store = object.__new__(ManufacturingDriveBackupStore)
    store.backend, store.folder_id, store.folder_url = base.backend, base.folder_id, None
    store.sales_folders = {"salestrackr-root-folder", "salestrackr-backup-folder"}
    base.folder_metadata.update(parents=["independent-parent"], appProperties={})
    files = store.backend._api.return_value.files.return_value
    files.get.return_value.execute.return_value["appProperties"] = {"app": "producttrackr-backup"}
    base.folder_metadata["name"] = "ProductTrackr"
    assert store._root_folder() == base.folder_id
    files.update.assert_not_called()
    base.folder_metadata["appProperties"] = {"app": "salestrackr-firestore-backups"}
    with pytest.raises(Problem, match="separate"):
        store._root_folder()
    base.folder_metadata["appProperties"] = {}
    base.folder_metadata["parents"] = ["salestrackr-root-folder"]
    with pytest.raises(Problem, match="separate"):
        store._root_folder()
    base.folder_metadata["parents"] = ["independent-parent"]
    permissions = store.backend._api.return_value.permissions.return_value
    permissions.list.return_value.execute.return_value = {"permissions": [{"type": "anyone"}]}
    with pytest.raises(Problem, match="private"):
        store._root_folder()
    permissions.list.return_value.execute.return_value = {"permissions": [{"type": "user"}]}
    store._folder = lambda: base.folder_id
    store.backend._list.return_value = []
    assert store.inventory() == []
    assert "producttrackr-backup" in store.backend._list.call_args.args[0]
    files.get.return_value.execute.side_effect = Problem("Missing", 503, "drive_missing")
    store.put("new-producttrackr-backup", b"archive", "ProductTrackr.zip")
    assert files.create.call_args.kwargs["body"]["appProperties"] == {"app": "producttrackr-backup"}
    assert files.create.call_args.kwargs["body"]["parents"] == [store.folder_id]


def test_local_storage_never_connects_to_drive_and_drive_errors_do_not_fall_back(app):
    _, settings = app
    configured = replace(settings, manufacturing_backup_folder_id="dedicated-producttrackr-folder")
    with patch("backend.app.manufacturing_backups.DriveBackend") as drive:
        manager = ManufacturingBackupManager(ManufacturingRepository(configured), configured)
        assert isinstance(manager.store, LocalBackupStore)
        assert "dedicated-producttrackr-folder" in manager.status("local-demo")["configuredFolderUrl"]
        drive.assert_not_called()
    with pytest.raises(Problem, match="reconnect"):
        ManufacturingBackupManager(
            ManufacturingRepository(configured),
            replace(
                configured,
                manufacturing_backup_storage="drive",
            ),
        )


def test_separate_producttrackr_credential_does_not_replace_sales_authorization(app):
    _, settings = app
    scopes = ["https://www.googleapis.com/auth/drive"]
    token = json.dumps({"scopes": scopes, "refresh_token": "synthetic"})
    configured = replace(
        settings,
        token_json="sales-credential",
        manufacturing_token_json=token,
        manufacturing_backup_folder_id="dedicated-producttrackr-root",
    )
    with patch("backend.app.manufacturing_backups.DriveBackend") as drive:
        ManufacturingDriveBackupStore(configured)
        drive.assert_called_once_with(
            token, "dedicated-producttrackr-root", operator=True, scopes=scopes,
        )
    assert configured.token_json == "sales-credential"


def test_uploaded_restore_retries_with_old_identity_without_repeating_recovery(process):
    client, settings, product, *_ = process
    job(client, product)
    row = backup(client)
    content = client.get(f"/api/manufacturing/backups/{row['id']}/download").content
    create(client, "products", name="Only in the pre-restore safety backup")
    upload_headers = {**headers(client), "Content-Type": "application/zip"}
    preview = client.post("/api/manufacturing/backups/inspect", content=content, headers=upload_headers)
    assert preview.status_code == 200, preview.text
    restore_headers = {
        **upload_headers,
        "X-ProductTrackr-Restore-Token": preview.json()["token"],
        "X-ProductTrackr-Restore-Confirm": "RESTORE",
        "X-ProductTrackr-Write-Id": uuid4().hex,
    }
    first = client.post("/api/manufacturing/backups/restore", content=content, headers=restore_headers)
    assert first.status_code == 200, first.text
    again = client.post("/api/manufacturing/backups/restore", content=content, headers=restore_headers)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(client.get("/api/manufacturing/settings/backup-recovery").json()["backups"]) == 2
    manager = ManufacturingBackupManager(ManufacturingRepository(settings), settings)
    safety, _ = manager.get(first.json()["recoveryBackup"]["id"])
    saved, _ = read_archive(safety)
    assert any(row["name"] == "Only in the pre-restore safety backup" for row in saved["products"].values())
    with zipfile.ZipFile(io.BytesIO(content)) as source:
        changed = io.BytesIO()
        with zipfile.ZipFile(changed, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                value = source.read(item.filename)
                target.writestr(item.filename, value + (b" " if item.filename == "manifest.json" else b""))
    wrong = client.post(
        "/api/manufacturing/backups/restore", content=changed.getvalue(), headers=restore_headers
    )
    assert wrong.status_code == 409


@pytest.mark.parametrize("proof", ["missing-backup", "different-archive", "active-dataset"])
def test_archived_cleanup_requires_physical_matching_backup_and_never_deletes_active_data(process, proof):
    client, settings, product, *_ = process
    job(client, product)
    manager = ManufacturingBackupManager(ManufacturingRepository(settings), settings)
    selected = manager.backup(uuid4().hex, "local-demo")
    content, _ = manager.get(selected["id"])
    create(client, "products", name="Additional current Product")
    preview = manager.inspect(content, "local-demo")
    with patch.object(manager, "finish_cleanup"):
        result = manager.restore(content, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    workspace = manager.repo.workspace
    archived = workspace.collection("datasets").document("legacy")
    safety_id = result["recoveryBackup"]["id"]
    assert archived.get().to_dict()["state"] == "archived"
    assert workspace.collection("products").document(product["id"]).get().exists
    if proof == "missing-backup":
        manager.store.directory.joinpath(f"{safety_id}.zip").unlink()
    elif proof == "different-archive":
        archived.update({"backupDataHash": "f" * 64})
    else:
        export = manager.repo.export_at

        def switch_active(target, preferences):
            value = export(target, preferences)
            workspace.update({"activeDataset": "legacy"})
            return value

        with patch.object(manager.repo, "export_at", side_effect=switch_active):
            with pytest.raises(Problem, match="cleanup stopped"):
                manager.cleanup_archived()
        workspace.update({"activeDataset": result["generation"]})
    if proof != "active-dataset":
        manager.finish_cleanup()
    assert archived.get().to_dict()["state"] == "archived"
    assert workspace.collection("products").document(product["id"]).get().exists
    assert not workspace.get().to_dict().get("maintenance")
    assert manager.repo.read("products", product["id"])["name"] == product["name"]
