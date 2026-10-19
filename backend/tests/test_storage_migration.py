import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from backend.app.domain import AccountData, Problem, Revision, now
from backend.app.storage import (
    FOLDERS,
    DriveBackend,
    LocalBackend,
    Repository,
    digest,
    document,
    document_revisions,
)
from scripts.drive_admin import collect, migrate, restore, save_backup


def revision(entity=None, parents=None, name="Synthetic account"):
    data = AccountData(name=name).model_dump(mode="json")
    return Revision(
        id=uuid4().hex,
        entityId=entity or uuid4().hex,
        entityType="account",
        parents=parents or [],
        createdAt=now(),
        actor="synthetic-operator",
        action="test",
        fingerprint=digest({"data": data, "parents": parents}),
        data=data,
    ).model_dump(mode="json")


class Workspace(DriveBackend):
    """In-memory Drive transport; exercise the real index, document, and migration code."""

    def __init__(self, schema=1):
        token = json.dumps(
            {"refresh_token": "synthetic", "client_id": "synthetic", "client_secret": "synthetic"}
        )
        super().__init__(token, "synthetic-root", operator=True)
        self.entries, self.events = {}, []
        self.sequence = 0
        self.settings_id = "synthetic-settings"
        folders = {key: f"folder-{key}" for key in (*FOLDERS, "indexes", "backups")}
        settings = {"schemaVersion": schema, "rootId": self.root_id, "folders": folders}
        if schema == 2:
            settings["checkpointId"] = "synthetic-checkpoint"
        self._upload(settings, self.root_id, "settings.json", self.settings_id)
        self.service = MagicMock()
        self.service.files.return_value.get.side_effect = lambda **kw: self.request(
            lambda: self.metadata(kw["fileId"])
        )
        self.service.changes.return_value.getStartPageToken.side_effect = lambda: self.request(
            lambda: {"startPageToken": str(len(self.events))}
        )
        self.service.changes.return_value.list.side_effect = lambda **kw: self.request(
            lambda: {
                "changes": deepcopy(self.events[int(kw["pageToken"]) :]),
                "newStartPageToken": str(len(self.events)),
            }
        )

    @staticmethod
    def request(callback):
        request = MagicMock()
        request.execute.side_effect = lambda **_: callback()
        return request

    def metadata(self, file_id):
        entry = self.entries.get(file_id)
        return {
            "id": file_id,
            "parents": [entry["parent"]] if entry else [self.root_id],
            "mimeType": "application/json" if entry else "application/vnd.google-apps.folder",
            "trashed": entry["trashed"] if entry else False,
        }

    def allocate(self):
        self.sequence += 1
        return f"synthetic-file-{self.sequence:08d}"

    def _list(self, query, fields="id,name,parents,appProperties"):
        parent = query.split("'")[1]
        for file_id, entry in list(self.entries.items()):
            if entry["parent"] != parent or entry["trashed"]:
                continue
            if "name = 'settings.json'" in query and entry["name"] != "settings.json":
                continue
            yield {
                "id": file_id,
                "name": entry["name"],
                "createdTime": entry["createdTime"],
                "parents": [parent],
            }

    def get(self, file_id):
        entry = self.entries.get(file_id)
        return deepcopy(entry["content"]) if entry and not entry["trashed"] else None

    def versioned_get(self, file_id):
        content = self.get(file_id)
        if content is None:
            raise Problem("Missing synthetic file.", 503, "data_removed")
        return content, str(self.entries[file_id]["version"])

    def etag(self, file_id):
        return str(self.entries[file_id]["version"])

    def _changed(self, file_id):
        entry = self.entries[file_id]
        entry["version"] += 1
        self.events.append(
            {"fileId": file_id, "file": {"parents": [entry["parent"]], "trashed": entry["trashed"]}}
        )

    def _upload(self, data, parent, filename, file_id=None, properties=None):
        file_id = file_id or self.allocate()
        if file_id in self.entries:
            if self.get(file_id) == data:
                return {"id": file_id}
            raise Problem("Identifier already used.", 409, "write_id_reused")
        self.entries[file_id] = {
            "content": deepcopy(data),
            "parent": parent,
            "name": filename,
            "createdTime": now(),
            "trashed": False,
            "version": 0,
        }
        self._changed(file_id)
        return {"id": file_id}

    def replace(self, file_id, data, etag, *, name=None, properties=None):
        if str(self.entries[file_id]["version"]) != etag:
            raise Problem("Changed synthetic file.", 409, "stale_edit")
        self.entries[file_id]["content"] = deepcopy(data)
        if name:
            self.entries[file_id]["name"] = name
        self._changed(file_id)
        return {"id": file_id}

    def trash(self, file_id, *, etag=None):
        if etag is not None and etag != str(self.entries[file_id]["version"]):
            raise Problem("Changed synthetic file.", 409, "stale_edit")
        self.entries[file_id]["trashed"] = True
        self._changed(file_id)
        return {"id": file_id}

    def add_legacy(self, raw):
        folders = self.get(self.settings_id)["folders"]
        self._upload(raw, folders[raw["entityType"]], f"{raw['entityId']}__{raw['id']}.json", raw["id"])

    def live_ids(self, kind):
        parent = self.get(self.settings_id)["folders"][kind]
        return {f["id"] for f in self._list(f"'{parent}' in parents and trashed = false")}


def legacy_workspace():
    drive = Workspace()
    first = revision()
    second = revision(first["entityId"], [first["id"]], "Updated synthetic account")
    other = revision()
    for raw in (first, second, other):
        drive.add_legacy(raw)
    return drive, [first, second, other]


def test_migration_dry_run_changes_nothing():
    drive, versions = legacy_workspace()
    before = deepcopy(drive.entries)
    plan = migrate(drive)
    assert len(plan["canonicalFiles"]) == 2
    assert len(plan["revisions"]) == len(versions)
    assert drive.entries == before
    with pytest.raises(Problem, match="writers-stopped"):
        migrate(drive, apply=True, drain_seconds=0)
    assert drive.entries == before


def test_migration_reuses_existing_files_preserves_history_and_is_idempotent():
    drive, versions = legacy_workspace()
    original_ids = drive.live_ids("account")
    manifest = migrate(drive, apply=True, writers_stopped=True, drain_seconds=0)
    assert drive.live_ids("account") < original_ids
    assert len(drive.live_ids("account")) == 2
    assert len(drive.live_ids("indexes")) == len(drive.live_ids("backups")) == 1
    expected = {r["id"]: r for r in versions}
    assert {r["id"]: r for r in collect(drive)["revisions"]} == expected
    settings = drive.get(drive.settings_id)
    assert settings["schemaVersion"] == 2
    assert "maintenance" not in settings
    assert drive.get(settings["migrationBackupId"]) == manifest
    repo = Repository(drive)
    repo.sync()
    first = versions[0]
    assert repo.current(first["entityId"]) == versions[1]
    assert sorted(repo.read_history(first["entityId"]), key=lambda r: r["id"]) == sorted(
        versions[:2], key=lambda r: r["id"]
    )
    before = deepcopy(drive.entries)
    migrate(drive, apply=True, writers_stopped=True, drain_seconds=0)
    assert drive.entries == before


@pytest.mark.parametrize("phase", ["replace", "trash", "checkpoint"])
def test_interrupted_migration_resumes_without_duplicate_backups(phase):
    drive, versions = legacy_workspace()
    operation = "_upload" if phase == "checkpoint" else phase
    original = getattr(drive, operation)
    interrupted = False

    def fail_once(*args, **kwargs):
        nonlocal interrupted
        result = original(*args, **kwargs)
        content = args[0] if operation == "_upload" else args[1] if operation == "replace" else None
        matches = phase == "trash" or (
            isinstance(content, dict)
            and (
                content.get("schemaVersion") == 2 and "current" in content
                if phase == "replace"
                else content.get("format") == 2 and "state" in content
            )
        )
        if matches and not interrupted:
            interrupted = True
            raise OSError("Synthetic interrupted migration")
        return result

    setattr(drive, operation, fail_once)
    with pytest.raises(OSError, match="interrupted"):
        migrate(drive, apply=True, writers_stopped=True, drain_seconds=0)
    assert drive.get(drive.settings_id)["maintenance"]
    setattr(drive, operation, original)
    migrate(drive, apply=True, writers_stopped=True, drain_seconds=0)
    assert len(drive.live_ids("backups")) == 1
    assert len(drive.live_ids("indexes")) == 1
    assert len(drive.live_ids("account")) == 2
    assert {r["id"]: r for r in collect(drive)["revisions"]} == {r["id"]: r for r in versions}


def test_migration_stops_if_unexpected_data_appears():
    drive, versions = legacy_workspace()
    replace = drive.replace
    injected = False

    def insert_unexpected(*args, **kwargs):
        nonlocal injected
        result = replace(*args, **kwargs)
        if args[1].get("schemaVersion") == 2 and "current" in args[1] and not injected:
            injected = True
            drive.add_legacy(revision())
        return result

    drive.replace = insert_unexpected
    with pytest.raises(Problem, match="Source data changed"):
        migrate(drive, apply=True, writers_stopped=True, drain_seconds=0)
    assert all(not drive.entries[r["id"]]["trashed"] for r in versions)
    assert drive.get(drive.settings_id)["maintenance"]
    assert len(drive.live_ids("backups")) == 1


def test_migration_does_not_convert_sources_if_backup_readback_fails():
    drive, versions = legacy_workspace()
    get = drive.get

    def bad_readback(file_id):
        value = get(file_id)
        if value and value.get("format") == 2 and "revisions" in value:
            return {**value, "checksum": "synthetic-corruption"}
        return value

    drive.get = bad_readback
    with pytest.raises(Problem, match="Backup verification failed"):
        migrate(drive, apply=True, writers_stopped=True, drain_seconds=0)
    assert all(get(raw["id"]) == raw for raw in versions)
    assert drive.get(drive.settings_id)["maintenance"]
    drive.get = get
    migrate(drive, apply=True, writers_stopped=True, drain_seconds=0)
    assert len(drive.live_ids("backups")) == 1


def test_migrated_legacy_conflicts_can_be_resolved_inside_one_file():
    drive = Workspace()
    root = revision()
    left = revision(root["entityId"], [root["id"]], "Left synthetic edit")
    right = revision(root["entityId"], [root["id"]], "Right synthetic edit")
    for raw in (root, left, right):
        drive.add_legacy(raw)
    migrate(drive, apply=True, writers_stopped=True, drain_seconds=0)
    repo = Repository(drive)
    repo.sync()
    assert len(repo.conflicts()) == 1 and not repo.list("account")
    resolved = revision(root["entityId"], [left["id"], right["id"]], "Reviewed synthetic edit")
    repo.commit(resolved)
    assert repo.current(root["entityId"]) == resolved
    assert not repo.conflicts()
    assert len(repo.read_history(root["entityId"])) == 4
    assert len(drive.live_ids("account")) == 1


def test_drive_racing_update_rejects_loser_without_overwriting_winner():
    drive = Workspace(schema=2)
    root = revision(entity=drive.allocate())
    drive.create(root)
    left = revision(root["entityId"], [root["id"]], "Left synthetic edit")
    right = revision(root["entityId"], [root["id"]], "Right synthetic edit")
    replace = drive.replace
    raced = False

    def race(file_id, data, etag, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            replace(file_id, document([root, right]), etag)
        return replace(file_id, data, etag, **kwargs)

    drive.replace = race
    with pytest.raises(Problem) as error:
        drive.create(left)
    assert error.value.code == "stale_edit"
    assert drive.revisions(root["entityId"]) == [root, right]
    assert len(drive.live_ids("account")) == 1


def test_ambiguous_update_retry_returns_original_operation():
    drive = Workspace(schema=2)
    root = revision(entity=drive.allocate())
    drive.create(root)
    edit = revision(root["entityId"], [root["id"]], "Confirmed synthetic edit")
    replace = drive.replace

    def ambiguous(*args, **kwargs):
        replace(*args, **kwargs)
        raise Problem("Unconfirmed synthetic response.", 503, "save_unknown")

    drive.replace = ambiguous
    with pytest.raises(Problem) as error:
        drive.create(edit)
    assert error.value.code == "save_unknown"
    drive.replace = replace
    assert drive.create({**edit, "createdAt": now()}) == edit
    assert len(drive.revisions(root["entityId"])) == 2
    assert len(drive.live_ids("account")) == 1


def test_one_hundred_saves_keep_one_entity_file_and_one_checkpoint():
    drive = Workspace(schema=2)
    repo = Repository(drive)
    repo.sync()
    root = revision(entity=drive.allocate())
    repo.commit(root)
    current = root
    for index in range(100):
        current = revision(root["entityId"], [current["id"]], f"Synthetic edit {index}")
        repo.commit(current)
    assert len(drive.live_ids("account")) == 1
    assert drive.live_ids("indexes") == {"synthetic-checkpoint"}
    assert len(repo.read_history(root["entityId"])) == 101
    recovered = Repository(drive)
    recovered.sync()
    assert recovered.current(root["entityId"]) == current
    assert len(recovered.read_history(root["entityId"])) == 101


def test_invalid_checkpoint_pointer_cannot_overwrite_a_business_record():
    drive = Workspace(schema=2)
    root = revision(entity=drive.allocate())
    drive.create(root)
    before = drive.get(root["entityId"])
    drive.settings["checkpointId"] = root["entityId"]
    with pytest.raises(Problem) as error:
        drive.checkpoint(Repository(drive).state())
    assert error.value.code == "drive_config"
    assert drive.get(root["entityId"]) == before


def test_local_overlapping_process_locks_preserve_the_winning_save(tmp_path):
    root = revision()
    LocalBackend(tmp_path).create(root)
    edits = [
        revision(root["entityId"], [root["id"]], f"Synthetic edit {index}") for index in range(2)
    ]

    def save(raw):
        try:
            return LocalBackend(tmp_path).create(raw)
        except Problem as error:
            assert error.code == "stale_edit"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, edits))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert LocalBackend(tmp_path).revisions(root["entityId"]) == [root, winners[0]]
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_legacy_local_files_are_consolidated_without_changing_versions(tmp_path):
    root = revision()
    edit = revision(root["entityId"], [root["id"]])
    for raw in (root, edit):
        (tmp_path / f"{raw['id']}.json").write_text(json.dumps(raw), encoding="utf-8")
    backend = LocalBackend(tmp_path)
    repo = Repository(backend)
    repo.sync()
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert backend.revisions(root["entityId"]) == [root, edit]


@pytest.mark.parametrize("corruption", ["checksum", "cycle", "duplicate", "missing_parent", "wrong_current"])
def test_invalid_embedded_history_is_rejected(corruption):
    root = revision()
    edit = revision(root["entityId"], [root["id"]])
    value = document([root, edit])
    if corruption == "checksum":
        value["current"]["data"]["name"] = "Unexpected edit"
    elif corruption == "cycle":
        value["history"][0]["parents"] = [edit["id"]]
    elif corruption == "duplicate":
        value["history"].append(root)
    elif corruption == "missing_parent":
        value["current"]["parents"] = [uuid4().hex]
    else:
        value["history"], value["current"] = [edit], root
    with pytest.raises(Problem):
        document_revisions(value)


def test_embedded_history_cannot_be_removed_even_with_recomputed_checksum():
    drive = Workspace(schema=2)
    root = revision(entity=drive.allocate())
    drive.create(root)
    edit = revision(root["entityId"], [root["id"]])
    drive.create(edit)
    repo = Repository(drive)
    repo.sync()
    content, etag = drive.versioned_get(root["entityId"])
    drive.replace(root["entityId"], document([root]), etag)
    with pytest.raises(Problem, match="history was removed"):
        repo.sync(force=True)


def test_new_runtime_refuses_legacy_layout_and_maintenance():
    drive, _ = legacy_workspace()
    drive.operator = False
    with pytest.raises(Problem) as error:
        Repository(drive).sync()
    assert error.value.code == "migration_required"
    drive.entries[drive.settings_id]["content"]["maintenance"] = True
    with pytest.raises(Problem) as error:
        Repository(drive).sync()
    assert error.value.code == "storage_maintenance"


def test_backup_restore_retains_original_history_ids(monkeypatch):
    source, versions = legacy_workspace()
    backup_id, _ = save_backup(source, collect(source))
    target = Workspace(schema=2)
    monkeypatch.setattr("scripts.drive_admin.DriveBackend", lambda *args, **kwargs: target)
    restore(source, backup_id, "synthetic-target", "synthetic-token")
    assert {r["id"]: r for r in collect(target)["revisions"]} == {r["id"]: r for r in versions}
    assert len(target.live_ids("account")) == 2
    assert len(target.live_ids("indexes")) == 1
    with pytest.raises(Problem, match="empty"):
        restore(source, backup_id, "synthetic-target", "synthetic-token")


def test_original_version_one_backup_can_be_restored(monkeypatch):
    source, versions = legacy_workspace()
    manifest = {
        "format": 1, "sourceRoot": source.root_id,
        "revisions": versions, "checksum": digest(versions),
    }
    file_id = source._upload(
        manifest, source.get(source.settings_id)["folders"]["backups"], "legacy-backup.json"
    )["id"]
    target = Workspace(schema=2)
    monkeypatch.setattr("scripts.drive_admin.DriveBackend", lambda *args, **kwargs: target)
    restore(source, file_id, "synthetic-target", "synthetic-token")
    assert {r["id"]: r for r in collect(target)["revisions"]} == {r["id"]: r for r in versions}
    assert len(target.live_ids("account")) == 2
