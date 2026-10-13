import json
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpMockSequence
from httplib2 import Response

from backend.app.domain import AccountData, Problem, RecordData, Revision, now, today
from backend.app.queries import query_records
from backend.app.storage import DriveBackend, Repository, digest


def backend():
    result = DriveBackend.__new__(DriveBackend)
    result.root_id = "test-root"
    result.folders = {"account": "a", "contact": "c", "record": "r", "indexes": "i", "backups": "b"}
    result.service = MagicMock()
    result.ignore_checkpoints = False
    return result


def rev(entity=None, parents=None):
    data = AccountData(name="Synthetic account").model_dump()
    return Revision(
        id=uuid4().hex,
        entityId=entity or uuid4().hex,
        entityType="account",
        parents=parents or [],
        createdAt=now(),
        actor="test",
        action="create",
        fingerprint=digest(data),
        data=data,
    ).model_dump(mode="json")


def test_drive_list_paginates():
    drive = backend()
    call = drive.service.files.return_value.list.return_value.execute
    call.side_effect = [
        {"files": [{"id": "first"}], "nextPageToken": "next"},
        {"files": [{"id": "second"}]},
    ]
    assert list(drive._list("test")) == [{"id": "first"}, {"id": "second"}]
    assert drive.service.files.return_value.list.call_args_list[1].kwargs["pageToken"] == "next"


@pytest.mark.parametrize("content_type", ["application/json", "application/octet-stream"])
def test_drive_download_decodes_json_once(content_type):
    drive = backend()
    revision = rev()
    drive.service = build(
        "drive",
        "v3",
        http=HttpMockSequence(
            [({"status": "200", "content-type": content_type}, json.dumps(revision).encode())]
        ),
        static_discovery=True,
        cache_discovery=False,
    )
    assert drive.get(revision["id"]) == revision


@pytest.mark.parametrize("status", [403, 404])
def test_drive_download_handles_missing_and_denied_files(status):
    drive = backend()
    drive.service = build(
        "drive",
        "v3",
        http=HttpMockSequence(
            [({"status": str(status), "content-type": "application/json"}, b'{"error":{"message":"Denied"}}')]
        ),
        static_discovery=True,
        cache_discovery=False,
    )
    if status == 404:
        assert drive.get("missing-file") is None
    else:
        with pytest.raises(Problem) as error:
            drive.get("denied-file")
        assert error.value.code == "drive_read"


def test_changes_pagination_removals_and_filtering():
    drive = backend()
    one, two = rev(), rev()
    drive.service.changes.return_value.list.return_value.execute.side_effect = [
        {
            "nextPageToken": "p2",
            "changes": [
                {"fileId": one["id"], "file": {"parents": ["a"]}},
                {"fileId": "unrelated", "file": {"parents": ["other-folder"]}},
            ],
        },
        {
            "newStartPageToken": "p3",
            "changes": [
                {"fileId": two["id"], "file": {"parents": ["a"]}},
                {"fileId": "deleted", "removed": True},
            ],
        },
    ]
    drive.get = MagicMock(side_effect=[one, two])
    revisions, token, removed = drive.changes("p1")
    assert revisions == [one, two] and token == "p3"
    assert removed == ["unrelated", "deleted"]
    assert drive.get.call_count == 2


def test_ambiguous_create_retry_uses_same_file():
    drive = backend()
    revision = rev()
    drive.service.files.return_value.create.return_value.execute.side_effect = HttpError(
        Response({"status": "409"}),
        b'{"error":{"message":"exists"}}',
    )
    drive.get = MagicMock(return_value=revision)
    drive.create(revision)
    assert drive.service.files.return_value.create.call_args.kwargs["body"]["id"] == revision["id"]
    drive.get.return_value = {**revision, "actor": "another"}
    with pytest.raises(Problem, match="different content"):
        drive.create(revision)


def test_checkpoint_replay_and_canonical_history():
    drive = backend()
    first = rev()
    second = rev(first["entityId"], [first["id"]])
    source = Repository(MagicMock())
    source._apply(first)
    source.cursor = "saved-cursor"
    state = source.state()
    drive.initial = MagicMock(return_value=(state, [], "saved-cursor"))
    drive.changes = MagicMock(return_value=([second], "new-cursor", []))
    restored = Repository(drive)
    restored.sync()
    assert restored.current(first["entityId"])["id"] == second["id"]
    drive.changes.assert_called_once_with("saved-cursor")
    assert restored.cursor == "new-cursor"
    assert len(restored.history(first["entityId"])) == 2


def test_corrupt_checkpoint_falls_back_to_full_scan():
    drive = backend()
    good = rev()
    drive._list = MagicMock(
        side_effect=[
            [{"id": "bad-checkpoint", "createdTime": "2026-01-01"}],
            [{"id": good["id"]}],
            [],
            [],
        ]
    )
    drive.get = MagicMock(side_effect=[json.JSONDecodeError("bad json", "", 0), good])
    drive.service.changes.return_value.getStartPageToken.return_value.execute.return_value = {
        "startPageToken": "before-scan",
    }
    state, revisions, cursor = drive.initial()
    assert state is None and revisions == [good] and cursor == "before-scan"


def test_removed_revision_remains_an_error_until_restored():
    drive = backend()
    first = rev()
    drive.initial = MagicMock(return_value=(None, [first], "initial"))
    drive.changes = MagicMock(
        side_effect=[
            ([], "first", []),
            ([], "second", [first["id"]]),
            ([], "third", []),
            ([first], "fourth", []),
        ]
    )
    repo = Repository(drive)
    repo.sync()
    with pytest.raises(Problem, match="removed"):
        repo.sync(force=True)
    with pytest.raises(Problem, match="removed"):
        repo.sync(force=True)
    repo.sync(force=True)
    assert repo.current(first["entityId"])["id"] == first["id"]


def test_refresh_failure_is_actionable():
    drive = backend()
    request = MagicMock()
    request.execute.side_effect = RefreshError("invalid_grant")
    with pytest.raises(Problem) as error:
        drive._execute(request)
    assert error.value.code == "drive_reconnect"


def test_saved_revision_survives_index_failure():
    drive = backend()
    first = rev()
    drive.create = MagicMock()
    drive.initial = MagicMock(return_value=(None, [], "initial"))
    drive.changes = MagicMock(side_effect=Problem("Unavailable", 503))
    repo = Repository(drive)
    result = repo.commit(first)
    assert result["warning"].startswith("Saved, but")
    assert repo.current(first["entityId"])["id"] == first["id"]


def test_warm_queries_do_not_read_120000_historical_files():
    drive = backend()
    drive.initial = MagicMock(return_value=(None, [], "initial"))
    drive.changes = MagicMock(return_value=([], "current", []))
    repo = Repository(drive)
    repo.sync()
    record = RecordData(
        accountId="synthetic-account",
        title="Synthetic roller and belt enquiry",
        productNotes="Several replacement parts with quantities and specifications.",
        stageDates={"new_lead": today()},
    ).model_dump(mode="json")
    # Ingest realistic history shapes without network requests or retained full old snapshots.
    for entity_index in range(12000):
        entity = f"synthetic-{entity_index:08d}"
        parent = None
        for revision_index in range(10):
            raw = Revision(
                id=uuid4().hex,
                entityId=entity,
                entityType="record",
                parents=[parent] if parent else [],
                createdAt=now(),
                actor="test",
                action="edit",
                fingerprint=digest(record),
                data=record,
            ).model_dump(mode="json")
            repo._apply(raw)
            parent = raw["id"]
    repo.pending_checkpoint = 0
    drive.changes.reset_mock()
    drive.service.reset_mock()
    for _ in range(5):
        result = query_records(
            repo.list("record"),
            {"synthetic-account": {"name": "Synthetic account", "address": "Test city"}},
            "leads",
        )
        assert result["total"] == 12000 and len(result["items"]) == 50
    assert len(repo.meta) == 120000
    assert len(repo.head_data) == 12000
    drive.service.files.assert_not_called()
    drive.changes.assert_not_called()
