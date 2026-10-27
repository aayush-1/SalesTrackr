import hashlib
import io
import json
import os
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.backups import BACKUP_RETENTION, BackupManager, DriveBackupStore, archive, read_archive
from backend.app.config import Settings
from backend.app.domain import STAGES, AccountData, ContactData, Problem, RecordData, Revision, now, today
from backend.app.firestore_storage import FirestoreRepository
from backend.app.main import create_app
from backend.app.queries import VIEWS, query_records
from backend.app.record_cleanup import RecordCleanup
from backend.app.storage import digest, document
from scripts.firestore_admin import migrate_drive, restore_empty, verify_live


@pytest.fixture
def settings(tmp_path):
    if not os.getenv("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("Run the explicitly configured Firestore emulator integration suite.")
    return Settings(
        "demo", tmp_path, "", "", "", "", False,
        storage_backend="firestore", firestore_project="demo-salestrackr-tests",
        firestore_workspace=uuid4().hex,
    )


@pytest.fixture
def repo(settings):
    return FirestoreRepository(settings)


@pytest.fixture
def client(settings, repo):
    with TestClient(create_app(settings, repo)) as client:
        yield client


def raw(kind="account", entity_id=None, data=None, parents=(), **values):
    return Revision(
        id=uuid4().hex, entityId=entity_id or uuid4().hex, entityType=kind,
        parents=list(parents), createdAt=now(), actor="local-demo", action="test",
        fingerprint=uuid4().hex, data=data or AccountData(name="Example").model_dump(mode="json"),
        **values,
    ).model_dump(mode="json")


def headers(repo):
    return {"X-SalesTrackr-Request": "1", "X-SalesTrackr-Generation": repo.generation}


def write(client, repo, kind, data, *, current=None, write_id=None, route=None, method="POST"):
    body = {
        "writeId": write_id or uuid4().hex,
        "entityId": current["entityId"] if current else uuid4().hex,
        "baseRevision": current["id"] if current else None, "data": data,
    }
    return client.request(method, route or f"/api/{kind}s", json=body, headers=headers(repo)), body


def account(repo, name="Example"):
    value = raw(data=AccountData(name=name, address="Test city").model_dump(mode="json"))
    return repo.commit(value)["revision"]


def test_firestore_account_save_retry_and_stale_edit(client, repo):
    response, body = write(client, repo, "account", {"name": "Example"})
    assert response.status_code == 200, response.text
    first = response.json()["revision"]
    assert response.json()["revisionIsCurrent"]
    second, _ = write(
        client, repo, "account", {"name": "Example", "notes": "Updated"}, current=first,
        route=f"/api/accounts/{first['entityId']}", method="PATCH",
    )
    assert second.status_code == 200, second.text
    retry = client.post("/api/accounts", json=body, headers=headers(repo))
    assert retry.status_code == 200 and retry.json()["revision"] == first
    assert not retry.json()["revisionIsCurrent"]
    stale, _ = write(
        client, repo, "account", {"name": "Wrong"}, current=first,
        route=f"/api/accounts/{first['entityId']}", method="PATCH",
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_edit"
    history = client.get(f"/api/entities/account/{first['entityId']}/history", params={"limit": 1})
    assert history.status_code == 200, history.text
    assert history.json()["total"] == 2 and history.json()["items"] == [second.json()["revision"]]
    assert repo.read_history(first["entityId"])


def test_firestore_generation_is_required_for_prepare_and_save(client, repo):
    response = client.post(
        "/api/writes/prepare", json={"entityType": "account"},
        headers={"X-SalesTrackr-Request": "1"},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "workspace_changed"
    response = client.post("/api/writes/prepare", json={"entityType": "account"}, headers=headers(repo))
    assert response.status_code == 200
    assert set(response.json()) == {"writeId", "entityId"}
    assert client.get("/api/me").json()["generation"] == repo.generation


def test_firestore_stock_update_on_legacy_record_preserves_history_and_indexed_queues(client, repo):
    owner = account(repo)
    data = RecordData(
        accountId=owner["entityId"],
        workflowVersion=3,
        stageDates={stage: today() for stage in STAGES[:4]},
    ).model_dump(mode="json", exclude={"stockUpdated", "stockUpdatedOn"})
    original = raw("record", data=data)
    repo.commit(original)
    before = repo.workspace.get().to_dict()
    assert client.get(f"/api/records/{original['entityId']}/detail").json()["revision"] == original
    assert repo.current(original["entityId"]) == original
    response, body = write(
        client,
        repo,
        "record",
        {"updated": True},
        current=original,
        route=f"/api/records/{original['entityId']}/stock",
    )
    assert response.status_code == 200, response.text
    saved = response.json()["revision"]
    assert saved["data"] == {**data, "stockUpdated": True, "stockUpdatedOn": today().isoformat()}
    assert repo.workspace.get().to_dict()["dashboard"] == {**before["dashboard"], "stock-pending": 0}
    retry = client.post(f"/api/records/{original['entityId']}/stock", json=body, headers=headers(repo))
    assert retry.status_code == 200 and retry.json()["revision"] == saved
    assert len(repo.read_history(original["entityId"])) == 2
    assert original in repo.read_history(original["entityId"])
    assert repo.query_records("dispatched", "", None, None, None, None, None, "all", 0, 50)["total"] == 1


def test_stock_view_backfill_resumes_concurrently_without_rewriting_history(repo, settings):
    owner = account(repo)
    for stage_index, updated, discarded in (
        (2, False, False), (3, False, False), (5, False, False),
        (2, True, False), (3, True, False), (3, False, True),
    ):
        data = RecordData(
            accountId=owner["entityId"], workflowVersion=3,
            stageDates={stage: today() for stage in STAGES[:stage_index + 1]},
            paymentReceived=stage_index == 5,
            paymentReceivedOn=today() if stage_index == 5 else None,
            stockUpdated=updated, stockUpdatedOn=today() if updated else None,
            discarded=discarded, discardNote="Synthetic discarded record" if discarded else "",
            discardedOn=today() if discarded else None,
        ).model_dump(mode="json")
        if not updated:
            data.pop("stockUpdated")
            data.pop("stockUpdatedOn")
        repo.commit(raw("record", data=data))
    other = FirestoreRepository(settings)
    before, control = repo.snapshot()
    for item in repo.namespace.collection("entities").stream():
        old = item.to_dict()
        if old["kind"] != "record":
            continue
        old["viewVersion"] = 1
        old["queryData"].pop("stockUpdated", None)
        old["views"] = [view for view in old["views"] if view != "stock-pending"]
        old["buckets"] = [key for key in old["buckets"] if not key.startswith("record|stock-pending|")]
        item.reference.set(old)
    repo.workspace.update({
        "viewIndex": {"dataset": control["activeDataset"], "version": 1},
        "dashboard": {
            view: count for view, count in control["dashboard"].items() if view != "stock-pending"
        },
    })
    original = repo._revision
    calls = 0

    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Synthetic interrupted index rebuild")
        return original(*args, **kwargs)

    with patch.object(repo, "_revision", interrupt), pytest.raises(OSError):
        repo.sync()
    assert repo.workspace.get().to_dict()["viewIndex"]["version"] == 1
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda target: target.sync(), (repo, other)))
    assert repo.snapshot()[0] == before
    assert repo.control["sequence"] == control["sequence"]
    assert repo.generation == control["generation"]
    assert repo.control["dashboard"]["stock-pending"] == 3
    result = repo.query_records("stock-pending", "", None, None, None, None, None, "all", 0, 50)
    assert result["total"] == 3
    assert any("dispatched" not in row["data"]["stageDates"] for row in result["items"])
    assert {row["data"]["stageDates"].get("closed") is not None for row in result["items"]} == {True, False}
    after = repo.workspace.get().to_dict()
    with patch.object(repo, "_revision", side_effect=AssertionError("Index already upgraded")):
        repo.sync()
    assert repo.workspace.get().to_dict() == after


def test_stock_view_backfill_is_fenced_against_a_dataset_switch(repo):
    owner = account(repo)
    data = RecordData(accountId=owner["entityId"], stageDates={"new_lead": today()}).model_dump(mode="json")
    saved = repo.commit(raw("record", data=data))["revision"]
    namespace = repo.namespace
    ref = namespace.collection("entities").document(saved["entityId"])
    old = ref.get().to_dict()
    old.pop("viewVersion")
    ref.set(old)
    repo.workspace.update({"activeDataset": uuid4().hex, "generation": uuid4().hex, "viewIndex": None})
    with pytest.raises(Problem) as failure:
        repo._upgrade_views()
    assert failure.value.code == "workspace_changed"
    assert ref.get().to_dict() == old
    assert repo.workspace.get().to_dict()["viewIndex"] is None


def test_stock_indexes_repair_an_old_revision_finishing_a_save_after_upgrade(repo):
    owner = account(repo)
    data = RecordData(
        accountId=owner["entityId"], workflowVersion=3,
        stageDates={stage: today() for stage in STAGES[:3]},
    ).model_dump(mode="json")
    saved = repo.commit(raw("record", data=data))["revision"]
    before, control = repo.snapshot()
    ref = repo.namespace.collection("entities").document(saved["entityId"])
    old = ref.get().to_dict()
    old.pop("viewVersion")
    old["queryData"].pop("stockUpdated")
    old["views"].remove("stock-pending")
    old["buckets"] = [key for key in old["buckets"] if not key.startswith("record|stock-pending|")]
    ref.set(old)
    repo.workspace.update({"sequence": control["sequence"] + 1})
    assert repo.workspace.get().to_dict()["dashboard"]["stock-pending"] == 1
    assert repo.query_records("stock-pending", "", None, None, None, None, None, "all", 0, 50)["total"] == 0
    repo.sync()
    assert repo.query_records("stock-pending", "", None, None, None, None, None, "all", 0, 50)["total"] == 1
    assert repo.control["dashboard"]["stock-pending"] == 1
    assert repo.snapshot()[0] == before


def test_save_retries_if_an_old_writer_invalidates_indexes_between_sync_and_commit(repo):
    saved = account(repo)
    changed = raw(
        entity_id=saved["entityId"], parents=[saved["id"]],
        data={**saved["data"], "name": "Updated after index repair"},
    )
    repo.workspace.update({"sequence": repo.control["sequence"] + 1})
    with pytest.raises(Problem) as failure:
        repo.commit(changed)
    assert failure.value.code == "index_upgrade_pending"
    assert not repo.namespace.collection("revisions").document(changed["id"]).get().exists
    assert repo.current(saved["entityId"]) == saved
    repo.sync()
    assert repo.commit(changed)["revision"] == changed


def test_firestore_normalized_duplicate_is_atomic_across_clients(repo, settings):
    other = FirestoreRepository(settings)
    first = raw(data={"name": "G M Offset"})
    second = raw(data={"name": "GM offset"})

    def save(target, value):
        try:
            return target.commit(value)
        except Problem as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda args: save(*args), [(repo, first), (other, second)]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "account_exists" in results
    assert len(repo.query_accounts()["items"]) == 1
    found = repo.matching_accounts("GM Ofset")
    assert found["total"] == 1 and found["items"][0]["match"] == "similar"
    assert repo.matching_accounts("GM OFFSET")["exactCount"] == 1


def test_firestore_write_identifier_cannot_cross_entities_or_actors(repo):
    first = account(repo)
    changed = {**first, "entityId": uuid4().hex}
    with pytest.raises(Problem, match="different request"):
        repo.commit(changed)
    changed = {**first, "actor": "different-author"}
    with pytest.raises(Problem, match="different request"):
        repo.commit(changed)
    assert repo.current(first["entityId"]) == first


def test_firestore_large_record_is_chunked_without_history_reads(repo):
    owner = account(repo)
    followups = [{
        "id": uuid4().hex, "note": secrets_text(index),
        "occurredOn": today().isoformat(), "recordedAt": now(),
    } for index in range(160)]
    data = RecordData(
        accountId=owner["entityId"], workflowVersion=3,
        stageDates={"new_lead": today()}, followUps=followups,
    ).model_dump(mode="json")
    first = raw("record", data=data)
    assert len(json.dumps(first)) > 1_048_576
    repo.commit(first)
    second = raw("record", first["entityId"], {**data, "productNotes": "Changed"}, [first["id"]])
    with patch.object(repo, "read_history", side_effect=AssertionError("No history reads on saves")):
        repo.commit(second)
    assert repo.current(first["entityId"]) == second
    payload = repo.workspace.collection("payloads").document(digest(second)).get().to_dict()
    assert payload["parts"] >= 2
    assert len(repo.read_history(first["entityId"])) == 2


def secrets_text(index):
    import random
    import string

    generator = random.Random(index)
    return "".join(generator.choices(string.ascii_letters + string.digits, k=10_000))


def test_firestore_indexed_filters_match_legacy_semantics(repo):
    owner = account(repo, "Northern company")
    revisions = []
    for index in range(12):
        stages = {"new_lead": today() - timedelta(days=20 - index)}
        if index % 2:
            stages["follow_up"] = today() - timedelta(days=2)
        data = RecordData(
            accountId=owner["entityId"], workflowVersion=3, stageDates=stages,
            productNotes="Special rollers" if index % 3 else "Belts",
            nextFollowUpOn=today() - timedelta(days=index % 3),
        ).model_dump(mode="json")
        revision = raw("record", data=data)
        repo.commit(revision)
        revisions.append(revision)
    accounts = {owner["entityId"]: owner["data"]}
    for view in VIEWS:
        for search in ("", "ROLLERS", "Northern", "Test city", "unmatched"):
            for due in (("all", "today", "overdue") if view == "followups" else ("all",)):
                expected = query_records(
                    revisions, accounts, view, search, None, None, None,
                    today() - timedelta(days=19), today(), due, 1, 3,
                )
                actual = repo.query_records(
                    view, search, None, None, None, today() - timedelta(days=19), today(), due, 1, 3,
                )
                assert actual == expected
    repo.sync()
    for view in VIEWS:
        expected = query_records(revisions, accounts, view)["total"]
        assert repo.control["dashboard"][view] == expected
    renamed = raw(
        entity_id=owner["entityId"], data={**owner["data"], "name": "Renamed company"}, parents=[owner["id"]],
    )
    repo.commit(renamed)
    assert repo.query_records("all", "Northern", None, None, None, None, None, "all", 0, 50)["total"] == 0
    assert repo.query_records("all", "Renamed", None, None, None, None, None, "all", 0, 50)["total"] == 12


def test_firestore_backup_roundtrip_retention_and_restore(client, repo):
    first = account(repo)
    manager = client.app.state.recovery
    original = manager.backup("original-migration", "local-demo", source="migration")
    saved = manager.backup(uuid4().hex, "local-demo")
    before = repo.snapshot()[0]
    data, _ = manager.get(saved["id"])
    decoded, manifest = read_archive(data)
    assert decoded == before and manifest["counts"]["account"] == 1
    account(repo, "Added later")
    preview = manager.inspect(data, "local-demo")
    old_generation = repo.generation
    operation_id = uuid4().hex
    result = manager.restore(data, "local-demo", preview["token"], "RESTORE", operation_id)
    assert result["restored"] and result["generation"] != old_generation
    assert repo.snapshot()[0] == before
    assert repo.current(first["entityId"]) == first
    assert manager.restore(data, "local-demo", preview["token"], "RESTORE", operation_id) == result
    assert result["recoveryBackup"]["counts"]["account"] == 2
    with pytest.raises(Problem, match="outdated"):
        repo.check_generation(old_generation)
    for _ in range(BACKUP_RETENTION):
        manager.backup(uuid4().hex, "local-demo")
    rows = manager.rows()
    assert len(rows) == 10 and manager.status("local-demo")["retention"] == 10
    assert not any(row["protected"] for row in rows)
    with pytest.raises(Problem, match="no longer retained"):
        manager.get(original["id"])


def test_backup_contains_readable_entity_json_and_exact_full_history():
    owner = raw(data=AccountData(name="Example Company", notes="First line\nSecond line").model_dump())
    person = raw("contact", data=ContactData(
        accountId=owner["entityId"], name="Example contact",
    ).model_dump())
    first = raw("record", data=RecordData(
        accountId=owner["entityId"], contactId=person["entityId"],
        stageDates={"new_lead": today()}, workflowVersion=3,
    ).model_dump(mode="json"))
    closed = raw("record", entity_id=first["entityId"], parents=[first["id"]], data={
        **first["data"], "stageDates": {stage: today().isoformat() for stage in STAGES},
        "paymentReceived": True, "paymentReceivedOn": today().isoformat(),
        "feedbackReceived": True, "feedbackReceivedOn": today().isoformat(),
        "followUps": [{"id": uuid4().hex, "note": "Requested a callback",
                       "occurredOn": today().isoformat(), "recordedAt": now()}],
    })
    versions = [owner, person, first, closed]
    data, manifest = archive(
        versions, source="manual", operation_id=uuid4().hex,
        actor="local-demo", control={"generation": uuid4().hex, "sequence": 4},
    )
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        assert set(source.namelist()) == {
            "manifest.json", "accounts.json", "contacts.json", "sales-records.json",
        }
        assert b'\n  {' in source.read("accounts.json")
        assert json.loads(source.read("accounts.json"))[0]["current"] == owner
        assert json.loads(source.read("contacts.json"))[0]["current"] == person
        sale = json.loads(source.read("sales-records.json"))[0]
        assert sale["current"] == closed and sale["history"] == [first]
        assert json.loads(source.read("manifest.json")) == manifest
    assert read_archive(data)[0] == sorted(versions, key=lambda row: row["id"])


def test_backup_retains_support_for_previous_revision_zip_format():
    first = raw()
    _, manifest = archive(
        [first], source="migration", operation_id=uuid4().hex,
        actor="local-demo", control={"generation": uuid4().hex, "sequence": 0},
    )
    manifest.pop("files")
    manifest["version"] = 1
    manifest["checksum"] = digest({key: value for key, value in manifest.items() if key != "checksum"})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        target.writestr("manifest.json", json.dumps(manifest))
        target.writestr(f"revisions/{first['id']}.json", json.dumps(first))
    assert read_archive(output.getvalue())[0] == [first]


def test_backup_rejects_changed_readable_entity_files():
    data, _ = archive(
        [raw()], source="manual", operation_id=uuid4().hex,
        actor="local-demo", control={"generation": uuid4().hex, "sequence": 0},
    )
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            contents = source.read(name)
            if name == "accounts.json":
                contents = contents.replace(b"Example", b"Changed")
            target.writestr(name, contents)
    with pytest.raises(Problem, match="not a complete"):
        read_archive(output.getvalue())


def test_backup_inventory_discovers_valid_files_and_does_not_prune_on_read(client, repo):
    account(repo)
    manager = client.app.state.recovery
    for _ in range(BACKUP_RETENTION + 1):
        data, _ = archive(
            repo.snapshot()[0], source="migration", operation_id=uuid4().hex,
            actor="local-demo", control=repo.control,
        )
        manager.store.put(uuid4().hex, data, "externally-added.zip")
    unrelated = uuid4().hex
    manager.store.put(unrelated, b'{"not":"a SalesTrackr backup"}', "unrelated.zip")
    status = manager.status("local-demo")
    assert len(status["backups"]) == 11
    assert not any(row["protected"] for row in status["backups"])
    assert unrelated not in {row["id"] for row in status["backups"]}
    assert "invalid" in status["lastError"]
    assert len(list(manager.store.directory.glob("*.zip"))) == 12
    manager.backup(uuid4().hex, "local-demo", source="scheduled")
    assert len(manager.rows()) == 10
    assert manager.store.read(unrelated) == b'{"not":"a SalesTrackr backup"}'


def test_backup_retention_orders_timezone_aware_instants_not_timestamp_text(client, repo):
    account(repo)
    manager = client.app.state.recovery
    backups = []
    for index in range(BACKUP_RETENTION + 1):
        moment = datetime(2026, 10, 25, 16, 20, tzinfo=timezone.utc) + timedelta(minutes=index)
        if index % 2 == 0:
            moment = moment.astimezone(timezone(timedelta(hours=5, minutes=30)))
        created_at = moment.isoformat()
        with patch("backend.app.backups.now", return_value=created_at):
            backups.append(manager.backup(
                uuid4().hex, "local-demo", source="migration" if not backups else "manual",
            ))
    assert [row["id"] for row in manager.rows()] == [row["id"] for row in reversed(backups[1:])]
    assert manager._catalog().document(backups[0]["id"]).get().to_dict()["pruned"]


def test_backup_inventory_excludes_missing_and_changed_files_without_retrusting(client, repo):
    account(repo)
    manager = client.app.state.recovery
    missing = manager.backup(uuid4().hex, "local-demo")
    changed = manager.backup(uuid4().hex, "local-demo")
    manager.store.trash(missing["id"])
    different, _ = archive(
        repo.snapshot()[0], source="manual", operation_id=uuid4().hex,
        actor="local-demo", control=repo.control,
    )
    manager.store._path(changed["id"]).write_bytes(different)
    assert manager.rows() == []
    assert manager.rows() == []
    assert manager._catalog().document(changed["id"]).get().to_dict()["archiveHash"] == changed["archiveHash"]
    assert manager.status("local-demo")["lastError"]
    with pytest.raises(Problem, match="changed"):
        manager.get(changed["id"])


def test_backup_inventory_uses_verified_provider_checksum_and_handles_old_catalogue(client, repo):
    account(repo)
    manager = client.app.state.recovery
    saved = manager.backup(uuid4().hex, "local-demo", source="migration")
    reference = manager._catalog().document(saved["id"])
    reference.update({"protected": True})
    inventory = manager.store.inventory()
    with patch.object(manager.store, "inventory", return_value=inventory):
        with patch.object(manager.store, "read", side_effect=AssertionError("Unnecessary download")):
            assert not manager.rows()[0]["protected"]
        reference.update({"providerChecksum": None})
        inventory[0]["checksum"] = None
        with patch.object(manager.store, "read", wraps=manager.store.read) as download:
            assert len(manager.rows()) == 1
            download.assert_called_once_with(saved["id"])
    assert not reference.get().to_dict()["protected"]


def test_backup_inventory_discovers_readable_legacy_json_with_provider_time(client, repo):
    first = account(repo)
    manager = client.app.state.recovery
    file_id = uuid4().hex
    data = json.dumps({"format": 1, "revisions": [first], "checksum": digest([first])}).encode()
    path = manager.store.directory / f"{file_id}.json"
    path.write_bytes(data)
    expected = manager.store.inventory()[0]["createdAt"]
    rows = manager.rows()
    assert len(rows) == 1 and rows[0]["createdAt"] == expected and rows[0]["name"].endswith(".json")
    response = client.get(f"/api/backups/{file_id}/download")
    assert response.status_code == 200 and response.content == data
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"].endswith('.json"')


def test_firestore_restore_preview_detects_intervening_writes(client, repo):
    account(repo)
    manager = client.app.state.recovery
    saved = manager.backup(uuid4().hex, "local-demo")
    data, _ = manager.get(saved["id"])
    preview = manager.inspect(data, "local-demo")
    account(repo, "Changed meanwhile")
    with pytest.raises(Problem, match="Live data changed"):
        manager.restore(data, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    assert repo.query_accounts()["total"] == 2
    assert not repo.workspace.get().to_dict()["maintenance"]


def test_firestore_failed_restore_keeps_live_namespace_and_data(client, repo):
    first = account(repo)
    manager = client.app.state.recovery
    saved = manager.backup(uuid4().hex, "local-demo")
    for _ in range(BACKUP_RETENTION - 1):
        manager.backup(uuid4().hex, "local-demo")
    data, _ = manager.get(saved["id"])
    preview = manager.inspect(data, "local-demo")
    original = repo.generation
    with patch.object(repo, "stage", side_effect=OSError("Simulated failure")):
        with pytest.raises(OSError):
            manager.restore(data, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    repo.sync()
    assert not repo.control["maintenance"]
    assert repo.generation == original and repo.current(first["entityId"]) == first
    assert any(row["source"] == "pre-restore" for row in manager.rows())
    assert len(manager.rows()) == 10


def test_selected_restore_retry_confirms_success_even_after_source_backup_rotates_out(client, repo):
    account(repo)
    manager = client.app.state.recovery
    saved = manager.backup(uuid4().hex, "local-demo")
    for _ in range(BACKUP_RETENTION - 1):
        manager.backup(uuid4().hex, "local-demo")
    data, _ = manager.get(saved["id"])
    preview = manager.inspect(data, "local-demo")
    body = {
        "backupId": saved["id"], "writeId": uuid4().hex,
        "previewToken": preview["token"], "confirmation": "RESTORE",
    }
    restore = manager.restore

    def lost_response(*args):
        restore(*args)
        raise Problem("Response lost after successful activation", 503, "response_lost")

    with patch.object(manager, "restore", side_effect=lost_response):
        response = client.post("/api/backups/restore", json=body, headers=headers(repo))
    assert response.status_code == 503
    assert manager._catalog().document(saved["id"]).get().to_dict()["pruned"]
    response = client.post("/api/backups/restore", json=body, headers=headers(repo))
    assert response.status_code == 200 and response.json()["restored"]
    assert len(manager.rows()) == 10
    with pytest.raises(Problem, match="different content"):
        manager.completed_restore(saved["id"], body["writeId"], "another-actor", "RESTORE")


def test_firestore_backup_verification_failure_never_prunes_old_copies(client, repo):
    account(repo)
    manager = client.app.state.recovery
    for _ in range(BACKUP_RETENTION):
        manager.backup(uuid4().hex, "local-demo")
    previous = {row["id"] for row in manager.rows()}
    with patch.object(manager.store, "put"):
        with pytest.raises(Problem, match="readback"):
            manager.backup(uuid4().hex, "local-demo")
    assert {row["id"] for row in manager.rows()} == previous
    assert all(manager.get(key) for key in previous)


def test_backup_api_raw_upload_csrf_validation_and_confirmation(client, repo):
    account(repo)
    response = client.post("/api/backups", json={"writeId": uuid4().hex}, headers=headers(repo))
    assert response.status_code == 200, response.text
    saved = response.json()["backup"]
    download = client.get(f"/api/backups/{saved['id']}/download")
    assert download.status_code == 200 and download.content.startswith(b"PK")
    upload_headers = {**headers(repo), "Content-Type": "application/zip"}
    assert client.post(
        "/api/backups/inspect", content=download.content,
        headers={"Content-Type": "application/zip"},
    ).status_code == 403
    preview = client.post("/api/backups/inspect", content=download.content, headers=upload_headers)
    assert preview.status_code == 200, preview.text
    response = client.post(
        "/api/backups/restore", content=download.content, headers={
            **upload_headers, "X-SalesTrackr-Restore-Token": preview.json()["token"],
            "X-SalesTrackr-Write-Id": uuid4().hex,
        },
    )
    assert response.status_code == 422
    response = client.post(
        "/api/backups/restore", content=download.content, headers={
            **upload_headers, "X-SalesTrackr-Restore-Token": preview.json()["token"],
            "X-SalesTrackr-Restore-Confirm": "RESTORE", "X-SalesTrackr-Write-Id": uuid4().hex,
        },
    )
    assert response.status_code == 200, response.text
    assert client.post(
        "/api/accounts", content=b"PK", headers=upload_headers,
    ).status_code == 415


def test_recovery_admin_is_fail_closed_and_local_config_cannot_reach_production(repo, settings):
    production = replace(settings, mode="production", recovery_admins=("recovery-admin@example.test",))
    manager = BackupManager(repo, production, store=BackupManager(repo, settings).store)
    with pytest.raises(Problem, match="administrator"):
        manager.require_admin("not-admin@example.test")
    assert manager.admin("Recovery-Admin@example.test")
    with patch.dict(os.environ, {
        "APP_MODE": "demo", "STORAGE_BACKEND": "firestore", "FIRESTORE_PROJECT_ID": "real-project",
        "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8085",
    }):
        with pytest.raises(RuntimeError, match="demo-"):
            Settings.load()


@pytest.mark.parametrize("bad", [b"not-json", b"{}", b"PKbroken"])
def test_invalid_backup_never_restores(bad):
    with pytest.raises(Problem) as error:
        read_archive(bad)
    assert error.value.code == "invalid_backup"


def test_backup_rejects_added_paths_and_changed_history():
    revisions = [raw()]
    data, _ = archive(
        revisions, source="manual", operation_id=uuid4().hex,
        actor="local-demo", control={"generation": uuid4().hex, "sequence": 0},
    )
    assert read_archive(data)[0] == revisions
    output = io.BytesIO(data)
    with zipfile.ZipFile(output, "a") as target:
        target.writestr("../unsafe.json", "{}")
    with pytest.raises(Problem):
        read_archive(output.getvalue())


def test_firestore_ambiguous_backup_upload_reuses_exact_file_and_snapshot(client, repo):
    account(repo)
    manager = client.app.state.recovery
    operation_id = uuid4().hex
    upload = manager.store.put

    def uncertain(file_id, data, name):
        upload(file_id, data, name)
        raise OSError("Response lost after successful upload")

    with patch.object(manager.store, "put", side_effect=uncertain):
        with pytest.raises(OSError):
            manager.backup(operation_id, "local-demo")
    account(repo, "Created after backup")
    result = manager.backup(operation_id, "local-demo")
    assert result["counts"]["account"] == 1
    assert manager.backup(operation_id, "local-demo") == result
    assert len(list(manager.store.directory.glob("*.zip"))) == 1
    assert repo.query_accounts()["total"] == 2


def test_firestore_expired_restore_unlocks_without_switching_data(repo):
    original = account(repo)
    generation = repo.generation
    repo.workspace.update({
        "maintenance": {"kind": "restore", "writeId": uuid4().hex, "owner": uuid4().hex, "expires": 0},
    })
    repo.sync()
    assert repo.generation == generation
    assert repo.current(original["entityId"]) == original
    assert repo.control["maintenance"] is None
    assert "interrupted restore" in repo.control["lastBackupError"]


def test_firestore_concurrent_restore_retry_cannot_cancel_inflight_restore(client, repo):
    account(repo)
    manager = client.app.state.recovery
    saved = manager.backup(uuid4().hex, "local-demo")
    data, _ = manager.get(saved["id"])
    preview = manager.inspect(data, "local-demo")
    operation_id, lease_owner = uuid4().hex, uuid4().hex
    from time import time

    repo.workspace.collection("restoreOperations").document(operation_id).set({
        "actor": "local-demo", "archiveHash": saved["archiveHash"],
        "generation": repo.generation, "sequence": repo.control["sequence"], "completed": False,
    })
    repo.workspace.update({
        "maintenance": {
            "kind": "restore", "writeId": operation_id, "owner": lease_owner, "expires": time() + 600,
        },
    })
    with pytest.raises(Problem) as failure:
        manager.restore(data, "local-demo", preview["token"], "RESTORE", operation_id)
    assert failure.value.status == 503
    assert failure.value.code == "restore_in_progress"
    assert repo.workspace.get().to_dict()["maintenance"]["owner"] == lease_owner


def test_firestore_legacy_migration_preserves_raw_history_and_conflicts(repo, settings):
    owner = raw(data={"name": "G M Offset", "address": "Legacy location"})
    branch_a = raw(
        entity_id=owner["entityId"], data={"name": "GM Offset", "notes": "First branch"},
        parents=[owner["id"]],
    )
    branch_b = raw(
        entity_id=owner["entityId"], data={"name": "Another old name"}, parents=[owner["id"]],
    )
    legacy_sale = raw("record", data={
        "accountId": owner["entityId"], "productNotes": "Legacy sale", "title": "Original enquiry",
        "stageDates": {"new_lead": today().isoformat(), "quotation_sent": today().isoformat()},
    })
    revisions = [owner, branch_a, branch_b, legacy_sale]
    namespace = restore_empty(repo, revisions)
    assert namespace == repo.generation
    assert {r["id"]: r for r in repo.snapshot()[0]} == {r["id"]: r for r in revisions}
    assert repo.matching_accounts("GM OFFSET")["items"][0]["conflicted"]
    assert repo.control["conflictCount"] == 1
    assert repo.control["dashboard"]["followups"] == 1
    with pytest.raises(Problem):
        restore_empty(repo, revisions)
    data, _ = archive(
        revisions, source="manual", operation_id=uuid4().hex,
        actor="local-demo", control=repo.control,
    )
    second = FirestoreRepository(replace(settings, firestore_workspace=uuid4().hex))
    restored, _ = read_archive(data)
    restore_empty(second, restored)
    assert second.current(legacy_sale["entityId"]) == legacy_sale
    assert second.conflicts() == repo.conflicts()


def test_firestore_payload_corruption_is_not_returned_as_business_data(repo):
    value = account(repo)
    payload = repo.workspace.collection("payloads").document(digest(value))
    repo._part_collection(payload, payload.get().to_dict()).document("0").update({"data": b"not-zlib"})
    with pytest.raises(Problem) as failure:
        repo.current(value["entityId"])
    assert failure.value.code == "data_altered"


def test_firestore_history_pagination_only_downloads_requested_versions(repo):
    value = account(repo)
    for index in range(6):
        value = raw(
            entity_id=value["entityId"], data={**value["data"], "notes": str(index)}, parents=[value["id"]],
        )
        repo.commit(value)
    with patch.object(repo, "_payload", wraps=repo._payload) as payload:
        page = repo.history_page(value["entityId"], "account", 2, 2)
        assert page["total"] == 7 and len(page["items"]) == 2
        assert payload.call_count == 2


def test_backup_upload_does_not_block_normal_api_saves(client, repo):
    account(repo)
    manager = client.app.state.recovery
    entered, release = threading.Event(), threading.Event()
    original = manager.store.put

    def upload(file_id, data, name):
        entered.set()
        assert release.wait(10)
        original(file_id, data, name)

    with patch.object(manager.store, "put", side_effect=upload), ThreadPoolExecutor(max_workers=2) as pool:
        backup = pool.submit(
            client.post, "/api/backups", json={"writeId": uuid4().hex}, headers=headers(repo),
        )
        assert entered.wait(10)
        try:
            save = pool.submit(write, client, repo, "account", {"name": "Created during upload"})
            response, _ = save.result(timeout=5)
            assert response.status_code == 200
        finally:
            release.set()
        response = backup.result(timeout=10)
    assert response.status_code == 200, response.text
    assert response.json()["backup"]["counts"]["account"] == 1


def mock_drive_store(data):
    store = object.__new__(DriveBackupStore)
    store.backend = Mock()
    store.folder_id = "private-backup-folder"
    store.folder_url = None
    store.backend.root_id = "salestrackr-root-folder"
    store.backend._execute.side_effect = lambda request: request.execute()
    store.backend._api.return_value.permissions.return_value.list.return_value.execute.return_value = {
        "permissions": [{"type": "user"}],
    }
    store.backend._api.return_value.files.return_value.get.return_value.execute.return_value = {
        "id": "example-backup-id", "parents": ["private-backup-folder"], "mimeType": "application/zip",
        "appProperties": {"app": "salestrackr-backup"},
        "size": str(len(data)), "md5Checksum": hashlib.md5(data, usedforsecurity=False).hexdigest(),
    }
    store.folder_metadata = {
        "id": store.folder_id, "parents": [store.backend.root_id], "trashed": False,
        "mimeType": "application/vnd.google-apps.folder",
        "appProperties": {"app": "salestrackr-firestore-backups"},
    }
    files = store.backend._api.return_value.files.return_value
    files.get.side_effect = lambda **kwargs: (
        Mock(execute=Mock(return_value=store.folder_metadata))
        if kwargs["fileId"] == store.folder_id else files.get.return_value
    )
    return store


def test_drive_backups_require_private_folder_and_preserve_upload_bytes():
    data = b"example archive bytes"
    store = mock_drive_store(data)
    permissions = store.backend._api.return_value.permissions.return_value
    permissions.list.return_value.execute.return_value = {"permissions": [{"type": "anyone"}]}
    with pytest.raises(Problem, match="private"):
        store._folder()
    permissions.list.return_value.execute.return_value = {"permissions": [{"type": "user"}]}
    files = store.backend._api.return_value.files.return_value
    files.get.return_value.execute.side_effect = Problem("Not found", 503, "drive_missing")
    store.put("example-backup-id", data, "backup.zip")
    arguments = files.create.call_args.kwargs
    assert arguments["body"]["parents"] == ["private-backup-folder"]
    assert arguments["body"]["id"] == "example-backup-id"
    assert arguments["media_body"].getbytes(0, len(data)) == data
    assert arguments["media_body"].mimetype() == "application/zip"


def test_drive_backup_download_is_checksum_bound():
    data = b"example archive bytes"
    store = mock_drive_store(data)

    def downloader(output, request, chunksize):
        def chunk(**kwargs):
            output.write(data)
            return None, True

        return Mock(next_chunk=chunk)

    with patch("backend.app.backups.MediaIoBaseDownload", side_effect=downloader):
        assert store.read("example-backup-id") == data
        metadata = store.backend._api.return_value.files.return_value.get.return_value.execute.return_value
        metadata["md5Checksum"] = "wrong-checksum"
        with pytest.raises(Problem, match="changed"):
            store.read("example-backup-id")


def test_drive_backup_inventory_uses_the_private_folder_and_provider_metadata():
    store = mock_drive_store(b"example")
    store.backend._list.return_value = [{
        "id": "example-backup-id", "name": "uploaded-backup.json", "size": "7",
        "md5Checksum": "provider-checksum", "createdTime": "2026-10-20T08:00:00Z",
    }]
    assert store.inventory() == [{
        "id": "example-backup-id", "name": "uploaded-backup.json", "sizeBytes": 7,
        "checksum": "provider-checksum", "createdAt": "2026-10-20T08:00:00Z",
    }]
    query = store.backend._list.call_args.args[0]
    assert "'private-backup-folder' in parents" in query
    assert "trashed = false" in query and "application/json" in query
    metadata = store.backend._api.return_value.files.return_value.get.return_value.execute.return_value
    metadata.update(mimeType="application/json", appProperties={})
    assert store._metadata("example-backup-id") == metadata
    metadata["parents"] = ["another-folder"]
    with pytest.raises(Problem, match="moved or replaced"):
        store._metadata("example-backup-id")


def test_firestore_backup_folder_never_falls_back_to_legacy_drive_backups():
    store = mock_drive_store(b"example")
    store.folder_id = ""
    with pytest.raises(Problem, match="separate Firestore backup folder"):
        store.inventory()
    store.backend._folders.assert_not_called()
    store.folder_id = "private-backup-folder"
    store.folder_metadata["appProperties"] = {"app": "salestrackr"}
    with pytest.raises(Problem, match="dedicated Firestore backup folder"):
        store.inventory()
    store.backend.trash.assert_not_called()
    store.folder_metadata["appProperties"] = {"app": "salestrackr-firestore-backups"}
    store.folder_metadata["parents"] = ["another-root"]
    with pytest.raises(Problem, match="dedicated Firestore backup folder"):
        store._folder()


def test_production_firestore_configuration_requires_a_separate_backup_folder():
    values = {
        "APP_MODE": "production", "STORAGE_BACKEND": "firestore", "FIRESTORE_PROJECT_ID": "example-project",
        "GOOGLE_FOLDER_ID": "source-root", "GOOGLE_TOKEN_JSON": "{}",
        "IAP_AUDIENCE": "example-audience", "PUBLIC_ORIGIN": "https://example.test",
    }
    with patch.dict(os.environ, values, clear=True):
        with pytest.raises(RuntimeError, match="separate BACKUP_DRIVE_FOLDER_ID"):
            Settings.load()
        os.environ["BACKUP_DRIVE_FOLDER_ID"] = "dedicated-backup-folder"
        assert Settings.load().backup_folder_id == "dedicated-backup-folder"


def migration_source(versions):
    source = Mock()
    source.root_id, source.settings_id = "source-root", "source-settings-id"
    folders = {kind: f"source-{kind}-folder" for kind in ("account", "contact", "record", "backups")}
    source.settings = {
        "schemaVersion": 2, "rootId": source.root_id, "folders": folders, "checkpointId": "checkpoint",
    }
    source._folders.return_value = folders
    groups = {}
    for version in versions:
        groups.setdefault(version["entityId"], []).append(version)
    documents = {entity: document(values) for entity, values in groups.items()}
    old_backup = {
        "id": "old-backup-id", "name": "old-backup.json", "md5Checksum": "unchanged", "version": "7",
    }

    def listed(query, **kwargs):
        if f"'{folders['backups']}'" in query:
            return [old_backup.copy()]
        return [{
            "id": entity, "name": f"{entity}.json", "md5Checksum": digest(value), "version": "3",
        } for entity, value in documents.items() if f"'{folders[value['entityType']]}'" in query]

    source._list.side_effect = listed
    source.get.side_effect = lambda file_id: documents[file_id]
    source.versioned_get.side_effect = lambda file_id: (source.settings.copy(), "settings-etag")

    def replace(file_id, value, etag):
        assert file_id == source.settings_id

    source.replace.side_effect = replace
    source.trash.side_effect = AssertionError("Migration must never trash Drive files")
    return source


def test_drive_to_firestore_migration_preserves_every_source_file_and_history(repo, tmp_path):
    owner = raw()
    edited = raw(entity_id=owner["entityId"], parents=[owner["id"]], data={
        **owner["data"], "notes": "Preserve all previous versions",
    })
    source = migration_source([owner, edited])
    path = tmp_path / "retained-source.json"
    with patch("scripts.firestore_admin.time.sleep") as sleep:
        result = migrate_drive(source, repo, path)
    assert 299 < sleep.call_args.args[0] <= 300
    assert result["counts"] == {"account": 1, "contact": 0, "record": 0, "revisions": 2}
    assert result["sourceFilesUnchanged"] and result["sourceBackupsUnchanged"]
    assert {raw["id"]: raw for raw in repo.snapshot()[0]} == {raw["id"]: raw for raw in [owner, edited]}
    assert repo.current(owner["entityId"]) == edited
    assert read_archive(path.read_bytes())[1]["counts"] == result["counts"]
    assert source.settings["maintenance"]
    assert not repo.workspace.get().to_dict()["weeklyEnabled"]
    assert source.replace.call_count == 2
    source.trash.assert_not_called()
    with patch("scripts.firestore_admin.time.sleep", side_effect=AssertionError("Already drained")):
        assert migrate_drive(source, repo, path) == result


def test_drive_to_firestore_failed_copy_keeps_source_fenced_and_all_files_intact(repo, tmp_path):
    source = migration_source([raw()])
    path = tmp_path / "retained-source.json"
    with patch("scripts.firestore_admin.time.sleep"), patch.object(
        repo, "stage", side_effect=OSError("Interrupted staging"),
    ):
        with pytest.raises(OSError, match="Interrupted staging"):
            migrate_drive(source, repo, path)
    assert source.settings["maintenance"] and path.exists()
    assert repo.control["sequence"] == 0
    source.trash.assert_not_called()
    with patch("scripts.firestore_admin.time.sleep", side_effect=AssertionError("Already drained")):
        assert migrate_drive(source, repo, path)["sourceFilesUnchanged"]


def test_live_verification_preserves_migrated_history_and_checks_untouched_source(repo, tmp_path):
    first = raw()
    source = migration_source([first])
    with patch("scripts.firestore_admin.time.sleep"):
        migrate_drive(source, repo, tmp_path / "source.json")
    edited = raw(entity_id=first["entityId"], parents=[first["id"]], data={
        **first["data"], "notes": "New production edit after migration",
    })
    repo.commit(edited)
    store = Mock(folder_id="new-firestore-backup-folder")
    report = verify_live(repo, source, store)
    assert report["counts"]["revisions"] == 2 and report["sourceBackupsUnchanged"]
    store._folder.assert_called_once()
    listed = source._list.side_effect

    def changed(query, **kwargs):
        rows = listed(query, **kwargs)
        if "'source-backups-folder'" in query:
            rows[0]["version"] = "8"
        return rows

    source._list.side_effect = changed
    with pytest.raises(Problem, match="integrity check failed"):
        verify_live(repo, source, store)


@pytest.mark.parametrize("invalid", [None, "incomplete", "hash", "inventory", "root"])
def test_live_verification_recognizes_only_bound_completed_source_retirement(repo, tmp_path, invalid):
    first = raw()
    source = migration_source([first])
    with patch("scripts.firestore_admin.time.sleep"):
        migrate_drive(source, repo, tmp_path / "source.json")
    binding = repo.workspace.get().to_dict()["driveMigration"]
    receipt = {
        "format": 1, "completed": True, "driveRoot": source.root_id,
        "sourceInventoryHash": binding["sourceInventoryHash"],
        "versionHashes": {first["id"]: digest(first)},
        "versionEntities": {first["id"]: {
            "entityId": first["entityId"], "entityType": first["entityType"],
        }},
        "legacyFolders": source.settings["folders"],
    }
    if invalid == "incomplete":
        receipt["completed"] = False
    elif invalid == "hash":
        receipt["versionHashes"] = {}
    elif invalid == "inventory":
        receipt["sourceInventoryHash"] = "changed"
    elif invalid == "root":
        receipt["driveRoot"] = "different-root"
    repo.workspace.collection("sourceRetirements").document("retirement").set(receipt)
    repo.workspace.update({"driveMigration.sourceRetirement": "retirement"})
    source._folders.side_effect = AssertionError("A retired source must not be accessed")
    source._list.side_effect = AssertionError("A retired source must not be accessed")
    source.get.side_effect = AssertionError("A retired source must not be accessed")
    store = Mock(folder_id="new-firestore-backup-folder")
    if invalid:
        with pytest.raises(Problem):
            verify_live(repo, source, store)
    else:
        result = verify_live(repo, source, store)
        assert result["sourceRetired"] and not result["sourceFilesUnchanged"]
        assert result["counts"]["revisions"] == 1
        with patch.object(repo, "snapshot", return_value=([], repo.workspace.get().to_dict())):
            with pytest.raises(Problem, match="integrity check failed"):
                verify_live(repo, source, store)


def cleanup_record(repo, account_id, state="complete"):
    closed = state in ("complete", "closed")
    data = RecordData(
        accountId=account_id, productNotes=f"Cleanup sample: {state}", workflowVersion=3,
        stageDates={stage: today() for stage in (STAGES if closed else STAGES[:1])},
        paymentReceived=closed, paymentReceivedOn=today() if closed else None,
        feedbackReceived=state == "complete", feedbackReceivedOn=today() if state == "complete" else None,
        discarded=state == "discarded", discardNote="No longer needed" if state == "discarded" else "",
        discardedOn=today() if state == "discarded" else None,
    ).model_dump(mode="json")
    return repo.commit(raw("record", data=data))["revision"]


def cleanup_body(*records):
    return {
        "writeId": uuid4().hex, "confirmation": "DELETE",
        "records": [{"entityId": record["entityId"], "baseRevision": record["id"]} for record in records],
    }


def test_record_cleanup_lists_only_eligible_records_and_physically_reclaims_all_generations(client, repo):
    owner = account(repo)
    contact = repo.commit(raw("contact", data=ContactData(
        accountId=owner["entityId"], name="Retained contact",
    ).model_dump(mode="json")))["revision"]
    complete = cleanup_record(repo, owner["entityId"])
    discarded = cleanup_record(repo, owner["entityId"], "discarded")
    awaiting = cleanup_record(repo, owner["entityId"], "closed")
    active = cleanup_record(repo, owner["entityId"], "active")
    edited = repo.commit(raw(
        "record", complete["entityId"], {**complete["data"], "notes": "Saved history"},
        parents=[complete["id"]],
    ))["revision"]
    all_raw, _ = repo.snapshot()
    older, _ = repo.stage(all_raw)
    original_hashes = {item["id"]: digest(item) for item in all_raw}
    selected = {complete["entityId"], discarded["entityId"]}
    removed = [item for item in all_raw if item["entityId"] in selected]
    backup = client.post(
        "/api/backups", json={"writeId": uuid4().hex}, headers=headers(repo),
    ).json()["backup"]
    archive_before = client.get(f"/api/backups/{backup['id']}/download").content
    result = client.get("/api/settings/record-cleanup").json()
    assert {row["entityId"] for row in result["items"]} == selected and result["canDelete"]
    assert all(row["baseRevision"] in (edited["id"], discarded["id"]) for row in result["items"])
    generation = repo.generation
    body = cleanup_body(edited, discarded)
    response = client.post("/api/settings/record-cleanup", json=body, headers=headers(repo))
    assert response.status_code == 200, response.text
    assert response.json()["deletedCount"] == 2 and response.json()["savedVersionsRemoved"] == 3
    assert set(response.json()["deletedIds"]) == selected
    remaining = repo.snapshot()[0]
    assert {item["id"]: digest(item) for item in remaining} == {
        key: value for key, value in original_hashes.items() if key not in {item["id"] for item in removed}
    }
    assert repo.generation == generation and repo.control["entityCounts"] == {
        "account": 1, "contact": 1, "record": 2,
    }
    for record in (owner, contact, awaiting, active):
        assert repo.current(record["entityId"]) == record
    for record in (edited, discarded):
        assert client.get(f"/api/records/{record['entityId']}/detail").status_code == 404
        assert not older.collection("entities").document(record["entityId"]).get().exists
    for version in removed:
        payload = repo.workspace.collection("payloads").document(digest(version))
        assert not payload.get().exists and not list(payload.collections())
        assert not older.collection("revisions").document(version["id"]).get().exists
    assert client.get(f"/api/backups/{backup['id']}/download").content == archive_before
    retry = client.post("/api/settings/record-cleanup", json=body, headers=headers(repo))
    assert retry.status_code == 200 and retry.json() == response.json()
    assert repo.control["entityCounts"]["record"] == 2
    assert client.get("/api/settings/record-cleanup").json()["items"] == []
    with pytest.raises(Problem, match="permanently deleted"):
        repo.commit(raw("record", complete["entityId"], complete["data"]))


@pytest.mark.parametrize("state", ["closed", "active"])
def test_record_cleanup_rejects_ineligible_selection_without_deleting_anything(client, repo, state):
    owner = account(repo)
    complete = cleanup_record(repo, owner["entityId"])
    protected = cleanup_record(repo, owner["entityId"], state)
    before = repo.snapshot()[0]
    response = client.post(
        "/api/settings/record-cleanup", json=cleanup_body(complete, protected), headers=headers(repo),
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "cleanup_ineligible"
    assert repo.snapshot()[0] == before
    assert not repo.workspace.get().to_dict().get("pendingRecordCleanup")


def test_record_cleanup_rejects_stale_selection_and_requires_confirmation_generation_and_admin(client, repo):
    owner = account(repo)
    record = cleanup_record(repo, owner["entityId"])
    body = cleanup_body(record)
    response = client.post(
        "/api/settings/record-cleanup", json={**body, "confirmation": ""}, headers=headers(repo),
    )
    assert response.status_code == 422
    response = client.post(
        "/api/settings/record-cleanup", json=body, headers={"X-SalesTrackr-Request": "1"},
    )
    assert response.status_code == 409
    with patch.object(client.app.state.recovery, "admin", return_value=False):
        assert not client.get("/api/settings/record-cleanup").json()["canDelete"]
        response = client.post("/api/settings/record-cleanup", json=body, headers=headers(repo))
        assert response.status_code == 403
    repo.commit(raw(
        "record", record["entityId"], {**record["data"], "notes": "Changed"}, parents=[record["id"]],
    ))
    response = client.post("/api/settings/record-cleanup", json=body, headers=headers(repo))
    assert response.status_code == 409 and response.json()["error"]["code"] == "stale_edit"
    assert repo.control["entityCounts"]["record"] == 1
    body["records"].append(body["records"][0])
    assert client.post("/api/settings/record-cleanup", json=body, headers=headers(repo)).status_code == 422


def test_record_cleanup_interruption_is_discoverable_snapshot_safe_and_restartable(client, repo):
    owner = account(repo)
    record = cleanup_record(repo, owner["entityId"])
    body = cleanup_body(record)
    with patch.object(RecordCleanup, "_purge_payload", side_effect=OSError("Interrupted purge")):
        with pytest.raises(OSError, match="Interrupted purge"):
            client.post("/api/settings/record-cleanup", json=body, headers=headers(repo))
    status = client.get("/api/settings/record-cleanup").json()
    assert status["pending"] == body and status["cleanupInProgress"] and status["lastError"]
    assert {row["entityId"] for row in repo.snapshot()[0]} == {owner["entityId"]}
    assert repo.control["entityCounts"]["record"] == 0
    new = client.post("/api/settings/record-cleanup", json=cleanup_body(record), headers=headers(repo))
    assert new.status_code == 409
    recovery = client.app.state.recovery
    data, _ = archive(
        [owner], source="manual", operation_id=uuid4().hex, actor="local-demo", control=repo.control,
    )
    preview = recovery.inspect(data, "local-demo")
    with pytest.raises(Problem, match="pending record cleanup"):
        recovery.restore(data, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    response = client.post("/api/settings/record-cleanup", json=body, headers=headers(repo))
    assert response.status_code == 200, response.text
    assert response.json()["deletedCount"] == 1 and response.json()["savedVersionsRemoved"] == 1
    assert not client.get("/api/settings/record-cleanup").json()["pending"]
    assert repo.current(owner["entityId"]) == owner
    assert not repo.workspace.collection("payloads").document(digest(record)).get().exists


def test_record_cleanup_cannot_interrupt_a_running_backup(client, repo):
    owner = account(repo)
    record = cleanup_record(repo, owner["entityId"])
    repo.workspace.update({"backupLease": {"expires": 10**12, "owner": "another-backup"}})
    response = client.post("/api/settings/record-cleanup", json=cleanup_body(record), headers=headers(repo))
    assert response.status_code == 409 and response.json()["error"]["code"] == "backup_in_progress"
    assert repo.current(record["entityId"]) == record


def test_migration_verification_accepts_only_completed_intentional_record_deletions(client, repo, tmp_path):
    owner = account(repo)
    record = cleanup_record(repo, owner["entityId"])
    versions, _ = repo.snapshot()
    repo.workspace.update({"driveMigration": {
        "driveRoot": "source-root", "dataHash": digest({row["id"]: digest(row) for row in versions}),
        "sourceInventoryHash": digest({"placeholder": True}),
    }})
    source = migration_source(versions)
    from scripts.firestore_admin import drive_inventory
    repo.workspace.update({"driveMigration.sourceInventoryHash": digest(drive_inventory(source))})
    store = Mock(folder_id="new-firestore-backup-folder")
    response = client.post("/api/settings/record-cleanup", json=cleanup_body(record), headers=headers(repo))
    assert response.status_code == 200, response.text
    assert verify_live(repo, source, store)["intentionallyDeletedRecords"] == 1
    marker = repo.workspace.collection("recordDeletions").document(record["entityId"])
    marker.update({"completed": False})
    with pytest.raises(Problem, match="integrity check failed"):
        verify_live(repo, source, store)


def test_cleanup_fences_every_payload_chunk_and_resumes_beyond_one_write_batch(repo, settings):
    payload = repo.workspace.collection("payloads").document("a" * 64)
    payload.set({"test": True})
    batch = repo.client.batch()
    for index in range(401):
        batch.set(payload.collection("parts").document(str(index).zfill(4)), {"data": b"x"})
    batch.commit()
    cleanup = RecordCleanup(repo, BackupManager(repo, settings))
    guard = Mock(side_effect=[None, Problem("Ownership changed", 409, "cleanup_pending")])
    with pytest.raises(Problem, match="Ownership changed"):
        cleanup._purge_payload(payload, guard)
    assert guard.call_count == 2
    assert payload.get().exists and len(list(payload.collection("parts").stream())) == 1
    guard = Mock()
    cleanup._purge_payload(payload, guard)
    assert guard.call_count == 1
    assert not payload.get().exists and not list(payload.collections())
