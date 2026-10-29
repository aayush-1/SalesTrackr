"""Offline backup verification/recovery and the authenticated scheduled-job entry point."""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from google.cloud import firestore

from backend.app.backups import BackupManager, checked_revisions, read_archive, scheduled_operation_id
from backend.app.config import Settings
from backend.app.domain import Problem, now
from backend.app.firestore_storage import FirestoreRepository
from backend.app.queries import VIEWS
from backend.app.record_cleanup import RecordCleanup
from backend.app.shared_backups import SharedBackupManager
from backend.app.storage import DriveBackend, digest


def drive_inventory(source):
    folders = source._folders(refresh=True, allow_legacy=True)
    return {
        kind: sorted(source._list(
            f"'{folders[kind]}' in parents and trashed = false",
            fields="id,name,parents,md5Checksum,version",
        ), key=lambda value: value["id"])
        for kind in ("account", "contact", "record", "backups")
    }


def migrate_drive(source, repo, archive_path):
    """Fence old writers and copy all history; never rewrite or trash Drive data/backup files."""
    from scripts.drive_admin import checked_manifest, collect, update_settings

    source._folders(refresh=True, allow_legacy=True)
    marker = source.settings.get("firestoreCutover")
    destination = repo.workspace._document_path
    if marker:
        if marker.get("target") != destination or not source.settings.get("maintenance"):
            raise Problem("The existing Drive cutover fence does not match this target.")
    else:
        if source.settings.get("maintenance") or source.settings.get("migration"):
            raise Problem("Another Drive maintenance operation is active.")
        marker = {
            "target": destination, "startedAt": now(), "drained": False,
            "sourceSettings": source.settings.copy(),
        }
        update_settings(source, {**source.settings, "maintenance": True, "firestoreCutover": marker})
    if not marker["drained"]:
        started_at = datetime.fromisoformat(marker["startedAt"])
        elapsed = (datetime.fromisoformat(now()) - started_at).total_seconds()
        time.sleep(max(0, 300 - elapsed))
        marker = {**marker, "drained": True}
        update_settings(source, {**source.settings, "firestoreCutover": marker})

    before = drive_inventory(source)
    snapshot = collect(source)
    snapshot.update(
        createdAt=marker["startedAt"], settings=marker["sourceSettings"], sourceInventory=before,
    )
    snapshot["checksum"] = digest(snapshot)
    archive_path = Path(archive_path)
    if archive_path.exists():
        if checked_manifest(json.loads(archive_path.read_text(encoding="utf-8"))) != snapshot:
            raise Problem("The retained migration snapshot does not match the fenced Drive source.")
    else:
        with archive_path.open("x", encoding="utf-8") as target:
            json.dump(snapshot, target, ensure_ascii=False, indent=2)
    revisions = snapshot["revisions"]
    expected = {raw["id"]: digest(raw) for raw in revisions}
    binding = {
        "driveRoot": source.root_id, "dataHash": digest(expected), "sourceInventoryHash": digest(before),
    }
    repo.sync()
    if repo.control.get("driveMigration") not in (None, binding):
        raise Problem("The Firestore target is bound to a different migration snapshot.")
    if repo.meta or repo.control["sequence"]:
        if repo.control.get("driveMigration") != binding:
            raise Problem("The Firestore target is not an empty migration workspace.")
    else:
        repo.workspace.update({"driveMigration": binding, "weeklyEnabled": False})
        repo.sync()
        restore_empty(repo, revisions)
    if (
        {raw["id"]: digest(raw) for raw in repo.snapshot()[0]} != expected
        or {raw["id"]: digest(raw) for raw in collect(source)["revisions"]} != expected
        or drive_inventory(source) != before
    ):
        raise Problem("Migration verification failed. All source files and the snapshot were retained.")
    return {
        "counts": checked_revisions(revisions), "dataHash": digest(expected),
        "generation": repo.generation, "sourceFilesUnchanged": True, "sourceBackupsUnchanged": True,
    }


def verify_live(repo, source, store):
    from scripts.drive_admin import collect

    revisions, control = repo.snapshot()
    binding = control.get("driveMigration") or {}
    if control.get("pendingRecordCleanup"):
        raise Problem(
            "Record cleanup is in progress. Retry verification afterwards.", 409, "cleanup_in_progress",
        )
    retired = binding.get("sourceRetirement")
    if retired:
        receipt = repo.workspace.collection("sourceRetirements").document(retired).get().to_dict()
        if (
            not receipt or receipt.get("format") != 1 or receipt.get("completed") is not True
            or receipt.get("driveRoot") != binding.get("driveRoot")
            or receipt.get("sourceInventoryHash") != binding.get("sourceInventoryHash")
            or not isinstance(receipt.get("versionHashes"), dict)
            or not isinstance(receipt.get("versionEntities"), dict)
            or not isinstance(receipt.get("legacyFolders"), dict)
        ):
            raise Problem("The Drive source-retirement receipt is missing or invalid.")
        expected = receipt["versionHashes"]
        entities = receipt["versionEntities"]
        folders = receipt["legacyFolders"]
        source_inventory_hash = receipt["sourceInventoryHash"]
    else:
        original = collect(source)["revisions"]
        expected = {raw["id"]: digest(raw) for raw in original}
        entities = {raw["id"]: {
            "entityId": raw["entityId"], "entityType": raw["entityType"],
        } for raw in original}
        source_inventory_hash = digest(drive_inventory(source))
        folders = source.settings["folders"]
    actual = {raw["id"]: digest(raw) for raw in revisions}
    removed = {
        item.id for item in repo.workspace.collection("recordDeletions").stream()
        if item.to_dict().get("completed") is True and item.to_dict().get("entityType") == "record"
    }
    def intentionally_deleted(key):
        entity = entities.get(key) or {}
        return entity.get("entityType") == "record" and entity.get("entityId") in removed

    if (
        binding.get("driveRoot") != source.root_id or binding.get("dataHash") != digest(expected)
        or binding.get("sourceInventoryHash") != source_inventory_hash
        or any(
            actual.get(key) != value and not (key not in actual and intentionally_deleted(key))
            for key, value in expected.items()
        )
    ):
        raise Problem("Migration/source integrity check failed. Preserve all data for investigation.")
    for view in VIEWS:
        repo.query_records(view, "", None, None, None, None, None, "all", 0, 1)
    accounts = repo.query_accounts(limit=1)["items"]
    if accounts:
        account = accounts[0]
        repo.query_accounts(account["data"]["name"], limit=1)
        repo.matching_accounts(account["data"]["name"])
        repo.query_contacts(account["entityId"])
        repo.history_page(account["entityId"], "account", 0, 1)
        repo.query_records("all", "", account["entityId"], None, None, None, None, "all", 0, 1)
    if not folders or store.folder_id in folders.values() or store.folder_id == source.root_id:
        raise Problem("Firestore backups must not use any existing Drive data/backup folder.")
    store._folder()
    return {
        "counts": checked_revisions(revisions), "dataHash": digest(actual),
        "sourceFilesUnchanged": not bool(retired), "sourceBackupsUnchanged": not bool(retired),
        "sourceRetired": bool(retired), "intentionallyDeletedRecords": len(removed),
        "dedicatedBackupFolder": True, "indexedViewsChecked": len(VIEWS),
    }


def restore_empty(repo, revisions):
    """Never overwrite an existing active dataset, including during an interrupted import."""
    repo.sync()
    before = repo.control
    if repo.meta or before.get("maintenance") or before["sequence"]:
        raise Problem("Recovery requires a new, empty target workspace. Existing live data was not changed.")
    target, statistics = repo.stage(revisions)

    @firestore.transactional
    def activate(transaction):
        control = repo.workspace.get(transaction=transaction).to_dict()
        if (
            control["generation"] != before["generation"] or control["sequence"] != 0
            or control.get("maintenance")
        ):
            raise Problem("The target changed during staging. No data was activated.")
        value = {
            **control, **statistics, "activeDataset": target.id, "generation": target.id,
            "sequence": 1,
        }
        transaction.set(repo.workspace, value)
        transaction.set(target, {"state": "active", "activatedAt": now()}, merge=True)
        return value

    repo.control = activate(repo.client.transaction())
    repo.sync()
    actual = {raw["id"]: digest(raw) for raw in repo.snapshot()[0]}
    expected = {raw["id"]: digest(raw) for raw in revisions}
    if actual != expected:
        raise Problem("Post-activation verification failed. Preserve both datasets for investigation.")
    return target.id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="Validate a downloaded ZIP/legacy JSON without cloud access.")
    validate.add_argument("archive", type=Path)
    unpack = sub.add_parser("unpack", help="Write ordinary JSON containing every original saved version.")
    unpack.add_argument("archive", type=Path)
    unpack.add_argument("--output", type=Path, required=True)
    restore = sub.add_parser("restore-empty", help="Stage and verify recovery into a new empty workspace.")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--project", required=True)
    restore.add_argument("--database", default="(default)")
    restore.add_argument("--workspace", default="salestrackr")
    restore.add_argument("--apply", action="store_true")
    restore.add_argument("--emulator", action="store_true", help="Use only a localhost demo- target.")
    backup = sub.add_parser("backup", help="Run with the app's configured ADC and Drive credential.")
    backup.add_argument("--scheduled", action="store_true")
    backup.add_argument("--operation-id")
    sub.add_parser("verify-live", help="Read-only runtime checks of migration, indexes and backup isolation.")
    args = parser.parse_args()
    if args.command in ("backup", "verify-live"):
        settings = Settings.load()
        if settings.storage_backend != "firestore":
            raise Problem("The live storage backend is not Firestore.")
        repo = FirestoreRepository(settings)
        manager = (
            SharedBackupManager(repo, settings)
            if settings.manufacturing_enabled else BackupManager(repo, settings)
        )
        if args.command == "verify-live":
            source = DriveBackend(settings.token_json, settings.root_id, operator=True)
            result = verify_live(repo, source, manager.store)
            actor = settings.recovery_admins[0] if settings.recovery_admins else "recovery-operator"
            status = manager.status(actor)
            cleanup = RecordCleanup(repo, manager).listing(actor)
            result.update(
                backupRetention=status["retention"], availableBackups=len(status["backups"]),
                manualBackupReady=status["canBackup"], recoveryAdminReady=status["canRestore"],
                backupStatusHealthy=not status["lastError"], recordCleanupReady=cleanup["canDelete"],
                eligibleCleanupRecords=cleanup["total"], backupReadbackVerified=False,
            )
            if status["backups"]:
                _, manifest = manager.read_archive(manager.get(status["backups"][0]["id"])[0])
                result.update(backupReadbackVerified=True, latestBackupCounts=manifest["counts"])
            print(json.dumps(result, sort_keys=True))
            return
        if args.scheduled and not repo.control.get("weeklyEnabled", True):
            print("Weekly backups are disabled; no backup was created.")
            return
        operation_id = args.operation_id or (scheduled_operation_id() if args.scheduled else repo.allocate())
        row = manager.backup(
            operation_id, "scheduled-job" if args.scheduled else "recovery-operator",
            source="scheduled" if args.scheduled else "manual",
        )
        print(f"Verified backup {row['id']}; {row['counts']['revisions']} saved versions.")
        return
    revisions, manifest = read_archive(args.archive.read_bytes())
    print(f"Verified backup: {json.dumps(manifest['counts'], sort_keys=True)}")
    print(f"Saved-version digest: {manifest['dataHash']}")
    if args.command == "unpack":
        output = {
            "format": 2, "createdAt": manifest["createdAt"],
            "sourceBackupHash": manifest["dataHash"], "revisions": revisions,
        }
        output["checksum"] = digest(output)
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(output, target, ensure_ascii=False, indent=2)
        print("Wrote plain JSON. The original backup file was not modified.")
    elif args.command == "restore-empty":
        if not args.apply:
            print("Plan only. Use --apply with an empty target to stage, verify, and activate this archive.")
            return
        settings = Settings(
            "demo" if args.emulator else "production", Path(".local-data"), "", "", "", "", False,
            storage_backend="firestore", firestore_project=args.project,
            firestore_database=args.database, firestore_workspace=args.workspace,
        )
        repo = FirestoreRepository(settings, initialize=True)
        namespace = restore_empty(repo, revisions)
        print(f"Activated verified target dataset {namespace}.")
        print("No original app or Drive credential was needed.")


if __name__ == "__main__":
    main()
