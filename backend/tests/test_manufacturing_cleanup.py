from unittest.mock import patch
from uuid import uuid4

import pytest

from backend.app.backups import LocalBackupStore
from backend.app.domain import Problem
from backend.app.manufacturing_backups import ManufacturingBackupManager
from backend.app.manufacturing_cleanup import ManufacturingCleanup
from backend.app.manufacturing_storage import ManufacturingRepository, deleted_record_receipt
from backend.tests.test_manufacturing import app as app
from backend.tests.test_manufacturing import command
from backend.tests.test_manufacturing_backups import backup, headers, inspect, restore
from backend.tests.test_manufacturing_records import apply, job, record
from backend.tests.test_manufacturing_records import process as process

PATH = "/api/manufacturing/settings/record-cleanup"


def finished(client, product, company):
    current, sent = job(client, product)
    while current["status"] != "completed":
        current = apply(client, current, "assign", partnerId=company["id"])
        current = apply(client, current, "complete")
    return current, sent


def request(*records):
    return {
        "writeId": uuid4().hex,
        "confirmation": "DELETE",
        "records": [{"id": row["id"], "baseVersion": row["version"]} for row in records],
    }


def remove(client, body, expected=200, identity=None):
    response = client.post(PATH, json=body, headers=identity or headers(client))
    assert response.status_code == expected, response.text
    return response.json()


def test_selected_completed_jobs_and_history_are_removed_but_shared_data_and_counter_stay(process):
    client, settings, product, company, *_ = process
    first, sent = finished(client, product, company)
    second, _ = finished(client, product, company)
    waiting, _ = job(client, product)
    repo = ManufacturingRepository(settings)
    before, _ = repo.snapshot()
    sales = client.get("/api/accounts").json()["items"]
    listing = client.get(PATH).json()
    assert {row["id"] for row in listing["items"]} == {first["id"], second["id"]}
    body = request(first)
    result = remove(client, body)
    assert result["deletedIds"] == [first["id"]] and result["deletedCount"] == 1
    assert result["activityEventsRemoved"] == first["version"] == result["writeReceiptsRemoved"]
    assert result["retryMarkersKept"] == 1
    assert remove(client, body) == result
    assert client.get(f"/api/manufacturing/records/{first['id']}").status_code == 404
    after, _ = repo.snapshot()
    for kind in ("products", "operations", "partners", "contacts", "steps", "assignments", "sequences"):
        assert after[kind] == before[kind]
    assert after["manufacturingRecords"] == {
        key: row for key, row in before["manufacturingRecords"].items() if key != first["id"]
    }
    deleted_events = {
        key for key, row in before["manufacturingRecordEvents"].items() if row["recordId"] == first["id"]
    }
    assert not deleted_events.intersection(after["manufacturingRecordEvents"])
    assert not deleted_events.intersection(after["writes"])
    marker = after["writes"][deleted_record_receipt(first["id"])]
    assert marker["result"]["deleted"] is True
    assert after["writes"].keys() == (before["writes"].keys() - deleted_events) | {
        deleted_record_receipt(first["id"])
    }
    replay, _ = command(client, "/records", {}, body=sent)
    assert replay.status_code == 410
    assert record(client, second["id"])["record"] == second
    assert record(client, waiting["id"])["record"] == waiting
    new, _ = job(client, product)
    assert new["number"] == "MFG-00004"
    assert client.get("/api/accounts").json()["items"] == sales
    manager = ManufacturingBackupManager(repo, settings)
    saved, _ = manager.read_archive(manager.get(result["recoveryBackup"]["id"])[0])
    assert saved == before
    assert not repo.workspace.get().to_dict().get("pendingRecordCleanup")


@pytest.mark.parametrize("status", ["ready", "in_progress", "cancelled"])
def test_non_completed_jobs_are_protected_without_creating_backups(process, status):
    client, settings, product, company, *_ = process
    current, _ = job(client, product)
    if status == "in_progress":
        current = apply(client, current, "assign", partnerId=company["id"])
    elif status == "cancelled":
        current = apply(client, current, "cancel", reason="Not completed")
    remove(client, request(current), 409)
    assert record(client, current["id"])["record"] == current
    assert ManufacturingBackupManager(ManufacturingRepository(settings), settings).rows() == []
    assert client.get(PATH).json()["total"] == 0


def test_confirmation_identity_stale_selection_and_permission_are_enforced(process):
    client, settings, product, company, *_ = process
    current, _ = finished(client, product, company)
    body = request(current)
    remove(client, {**body, "confirmation": ""}, 422)
    remove(client, {**body, "records": body["records"] * 2}, 422)
    remove(client, body, 409, {**headers(client), "X-BuildTrackr-Generation": uuid4().hex})
    remove(client, body, 409, {**headers(client), "X-BuildTrackr-Workspace": uuid4().hex})
    with patch.object(ManufacturingBackupManager, "admin", return_value=False):
        assert client.get(PATH).json()["canDelete"] is False
        remove(client, body, 403)
    reopened = apply(
        client,
        current,
        "undo",
        stepId=current["operations"][-1]["id"],
        reason="Reopen before stale cleanup",
    )
    remove(client, body, 409)
    assert record(client, current["id"])["record"] == reopened
    assert ManufacturingBackupManager(ManufacturingRepository(settings), settings).rows() == []


def test_backup_failure_never_deletes_jobs_and_same_attempt_can_retry(process):
    client, settings, product, company, *_ = process
    current, _ = finished(client, product, company)
    before, _ = ManufacturingRepository(settings).snapshot()
    body = request(current)
    with patch.object(LocalBackupStore, "put", side_effect=Problem("Safety backup failed", 503)):
        remove(client, body, 503)
    assert ManufacturingRepository(settings).snapshot()[0] == before
    assert client.get(PATH).json()["pending"] is None
    result = remove(client, body)
    assert result["deletedCount"] == 1
    assert len(client.get("/api/manufacturing/settings/backup-recovery").json()["backups"]) == 1


def test_interrupted_cleanup_is_resumable_blocks_mutations_backups_and_restores(process):
    client, settings, product, company, *_ = process
    current, _ = finished(client, product, company)
    manual = backup(client)
    preview = inspect(client, manual)
    body = request(current)
    with patch.object(ManufacturingCleanup, "_remove_batch", side_effect=Problem("Interrupted", 503)):
        remove(client, body, 503)
    state = client.get(PATH).json()
    assert state["pending"]["request"] == body
    assert state["cleanupInProgress"] is True
    assert command(client, "/products", {"name": "Cannot save during pending cleanup"})[0].status_code == 503
    response = client.post(
        "/api/manufacturing/backups", json={"writeId": uuid4().hex}, headers=headers(client)
    )
    assert response.status_code == 503
    restore(client, manual, preview, expected=409)
    changed = {**body, "records": [{"id": current["id"], "baseVersion": current["version"] + 1}]}
    remove(client, changed, 409)
    result = remove(client, body)
    assert result["activityEventsRemoved"] == current["version"] == result["writeReceiptsRemoved"]
    assert remove(client, body) == result
    assert client.get(PATH).json()["pending"] is None
    assert command(client, "/products", {"name": "Saving works after cleanup"})[0].status_code == 200
    assert len(ManufacturingBackupManager(ManufacturingRepository(settings), settings).rows()) == 2


def test_multiple_delete_batches_do_not_double_count_after_lost_commit_response(process):
    client, _, product, company, *_ = process
    current, _ = finished(client, product, company)
    for _ in range(24):
        current = apply(
            client,
            current,
            "undo",
            stepId=current["operations"][-1]["id"],
            reason="Synthetic history for batched cleanup",
        )
        current = apply(client, current, "complete")
    assert current["version"] == 55
    original = ManufacturingCleanup._remove_batch

    def interrupted(self, *args):
        original(self, *args)
        raise Problem("Response lost after first committed batch", 503)

    body = request(current)
    with patch.object(ManufacturingCleanup, "_remove_batch", interrupted):
        remove(client, body, 503)
    result = remove(client, body)
    assert result["activityEventsRemoved"] == 55 == result["writeReceiptsRemoved"]
    assert result["retryMarkersKept"] == 1


def test_receipts_for_an_unrelated_entity_with_same_id_are_never_deleted(process):
    client, settings, product, company, *_ = process
    current, _ = finished(client, product, company)
    response, sent = command(client, "/operations", {"name": "Unrelated operation"}, entity_id=current["id"])
    assert response.status_code == 200
    repo = ManufacturingRepository(settings)
    receipt = repo.ref("writes", sent["writeId"]).get().to_dict()
    remove(client, request(current))
    assert repo.ref("writes", sent["writeId"]).get().to_dict() == receipt
    assert repo.read("operations", current["id"])["name"] == "Unrelated operation"


def test_backup_race_cannot_delete_a_job_reopened_after_selection(process):
    client, _, product, company, *_ = process
    current, _ = finished(client, product, company)
    original = ManufacturingBackupManager.backup

    def raced(self, *args, **kwargs):
        apply(
            client,
            current,
            "undo",
            stepId=current["operations"][-1]["id"],
            reason="Reopened while preparing cleanup",
        )
        return original(self, *args, **kwargs)

    with patch.object(ManufacturingBackupManager, "backup", raced):
        remove(client, request(current), 409)
    assert record(client, current["id"])["record"]["status"] == "in_progress"
    assert client.get(PATH).json()["pending"] is None


def test_backup_roundtrip_preserves_cleanup_markers_and_safety_backup_can_restore_jobs(process):
    client, settings, product, company, *_ = process
    current, sent = finished(client, product, company)
    result = remove(client, request(current))
    compacted = backup(client)
    restore(client, compacted, inspect(client, compacted))
    assert command(client, "/records", {}, body=sent)[0].status_code == 410
    assert ManufacturingRepository(settings).ref("writes", deleted_record_receipt(current["id"])).get().exists
    restore(client, result["recoveryBackup"], inspect(client, result["recoveryBackup"]))
    assert record(client, current["id"])["record"]["status"] == "completed"
    assert (
        not ManufacturingRepository(settings)
        .ref("writes", deleted_record_receipt(current["id"]))
        .get()
        .exists
    )
    assert job(client, product)[0]["number"] == "MFG-00002"


def test_cleanup_is_fenced_if_the_active_dataset_changes(process):
    client, settings, product, company, *_ = process
    current, _ = finished(client, product, company)
    repo = ManufacturingRepository(settings)
    saved, _ = repo.snapshot()
    target, _ = repo.stage(saved)
    original = ManufacturingCleanup._mark_record

    def switch(self, *args):
        self.repo.workspace.update(
            {
                "activeDataset": target.id,
                "generation": target.id,
                "pendingRecordCleanup": None,
                "maintenance": None,
            }
        )
        return original(self, *args)

    with patch.object(ManufacturingCleanup, "_mark_record", switch):
        remove(client, request(current), 409)
    assert target.collection("manufacturingRecords").document(current["id"]).get().to_dict() == current
    assert repo.workspace.collection("manufacturingRecords").document(current["id"]).get().exists
