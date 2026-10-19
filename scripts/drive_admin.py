"""Back up, migrate, rebuild, or restore the app-owned Drive workspace."""

import argparse
import os
import time

from backend.app.domain import Problem, Revision, now
from backend.app.storage import (
    FOLDERS,
    DriveBackend,
    Repository,
    digest,
    document,
    document_revisions,
)


def collect(backend):
    folders = backend._folders(refresh=True, allow_legacy=True)
    versions, files, canonical = {}, [], {}
    for kind in FOLDERS:
        for metadata in backend._list(f"'{folders[kind]}' in parents and trashed = false"):
            raw = backend.get(metadata["id"])
            if not isinstance(raw, dict):
                raise Problem("An app data file is missing or invalid.")
            if raw.get("schemaVersion") == 2:
                revisions = document_revisions(raw)
                entity = raw["entityId"]
                if entity in canonical:
                    raise Problem("Multiple canonical files exist for one record.")
                canonical[entity] = metadata["id"]
            else:
                Revision.model_validate(raw)
                if raw["id"] != metadata["id"]:
                    raise Problem("A legacy version has an unexpected file ID.")
                revisions, entity = [raw], raw["entityId"]
            if any(r["entityType"] != kind or r["entityId"] != entity for r in revisions):
                raise Problem("A data file is in the wrong folder or mixes entities.")
            for revision in revisions:
                prior = versions.get(revision["id"])
                if prior is not None and prior != revision:
                    raise Problem("The same history ID contains different data.")
                versions[revision["id"]] = revision
            files.append(
                {"id": metadata["id"], "entityId": entity, "versionIds": sorted(r["id"] for r in revisions)}
            )
    groups = {}
    for revision in versions.values():
        groups.setdefault(revision["entityId"], []).append(revision)
    for entity, revisions in groups.items():
        document(revisions)
        if entity not in canonical:
            latest = max(revisions, key=lambda r: (r["createdAt"], r["id"]))
            canonical[entity] = next(f["id"] for f in files if latest["id"] in f["versionIds"])
    return {
        "format": 2,
        "sourceRoot": backend.root_id,
        "settings": backend.settings.copy(),
        "revisions": sorted(versions.values(), key=lambda r: r["id"]),
        "files": sorted(files, key=lambda f: f["id"]),
        "canonicalFiles": canonical,
    }


def checked_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("format") not in (1, 2):
        raise Problem("The backup format is invalid.")
    content = {key: value for key, value in manifest.items() if key != "checksum"}
    expected = digest(manifest["revisions"]) if manifest["format"] == 1 else digest(content)
    if manifest.get("checksum") != expected:
        raise Problem("The backup checksum does not match.")
    groups = {}
    for revision in manifest["revisions"]:
        groups.setdefault(revision["entityId"], []).append(revision)
    for revisions in groups.values():
        document(revisions)
    return manifest


def save_backup(backend, snapshot, file_id=None):
    manifest = {**snapshot, "checksum": digest(snapshot)}
    file_id = file_id or backend.allocate()
    backend._upload(
        manifest, backend._folders(allow_legacy=True)["backups"], f"backup_{file_id}.json", file_id
    )
    if backend.get(file_id) != manifest:
        raise Problem("Backup verification failed; no source files were changed.")
    return file_id, manifest


def backup(backend, repo=None):
    file_id, manifest = save_backup(backend, collect(backend))
    print(f"Backup file ID: {file_id}. Verified {len(manifest['revisions'])} saved versions.")
    return file_id


def restore(backend, backup_id, target_root, token):
    manifest = checked_manifest(backend.get(backup_id))
    target = DriveBackend(token, target_root, operator=True)
    if collect(target)["revisions"]:
        raise Problem("Restore requires an empty app-created target root.")
    if target.settings["schemaVersion"] != 2:
        raise Problem("Restore requires a version-2 target root created by the current bootstrap script.")
    folders = target._folders()
    groups = {}
    for revision in manifest["revisions"]:
        groups.setdefault(revision["entityId"], []).append(revision)
    for entity, revisions in groups.items():
        target._upload(
            document(revisions),
            folders[revisions[0]["entityType"]],
            f"{entity}.json",
            target.allocate(),
            {"app": "salestrackr", "entityType": revisions[0]["entityType"], "entityId": entity},
        )
    repo = Repository(target)
    repo.sync(force=True)
    expected = {r["id"]: digest(r) for r in manifest["revisions"]}
    if {key: value["hash"] for key, value in repo.meta.items()} != expected:
        raise Problem("Restore verification failed. Preserve the partial target for diagnosis.")
    target.checkpoint(repo.state())
    print(f"Restored {len(groups)} records and {len(repo.meta)} versions into one file per record.")


def update_settings(backend, value):
    current, etag = backend.versioned_get(backend.settings_id)
    if current != backend.settings:
        raise Problem("Workspace settings changed. Stop and inspect before continuing.")
    backend.replace(backend.settings_id, value, etag)
    backend.settings = value
    backend.folders = None


def verify_sources(snapshot, manifest):
    expected = {r["id"]: digest(r) for r in manifest["revisions"]}
    actual = {r["id"]: digest(r) for r in snapshot["revisions"]}
    source_ids = {f["id"] for f in manifest["files"]}
    if actual != expected or any(f["id"] not in source_ids for f in snapshot["files"]):
        raise Problem(
            "Source data changed or is incomplete. Migration stopped; maintenance and backup retained."
        )


def migrate(backend, *, apply=False, writers_stopped=False, drain_seconds=300):
    folders = backend._folders(refresh=True, allow_legacy=True)
    settings = backend.settings
    if settings["schemaVersion"] == 2 and not settings.get("migration"):
        snapshot = collect(backend)
        if len(snapshot["files"]) != len(snapshot["canonicalFiles"]):
            raise Problem("The version-2 workspace contains redundant data files.")
        print(
            f"Already migrated: {len(snapshot['files'])} entity files, "
            f"{len(snapshot['revisions'])} embedded versions."
        )
        return snapshot
    if not apply:
        snapshot = collect(backend)
        print(
            f"Migration plan: {len(snapshot['files'])} data files -> "
            f"{len(snapshot['canonicalFiles'])} entity files; "
            f"preserve all {len(snapshot['revisions'])} versions. No files changed."
        )
        return snapshot
    if not writers_stopped:
        raise Problem("Deploy the migration-aware release first, then confirm --writers-stopped.")
    if drain_seconds < 0:
        raise Problem("Drain seconds cannot be negative.")
    migration = settings.get("migration")
    if migration is None:
        migration = {
            "backupId": backend.allocate(),
            "startedAt": now(),
            "sourceSettings": settings,
            "drained": False,
        }
        update_settings(backend, {**settings, "maintenance": True, "migration": migration})
    if not migration["drained"]:
        print(f"Writes paused. Waiting {drain_seconds} seconds for old requests to drain.", flush=True)
        time.sleep(drain_seconds)
        migration = {**migration, "drained": True}
        update_settings(backend, {**backend.settings, "migration": migration})
    manifest = backend.get(migration["backupId"])
    if manifest is None:
        snapshot = collect(backend)
        snapshot["settings"] = migration["sourceSettings"]
        _, manifest = save_backup(backend, snapshot, migration["backupId"])
    manifest = checked_manifest(manifest)
    if manifest.get("sourceRoot") != backend.root_id or manifest.get("format") != 2:
        raise Problem("The migration backup belongs to a different workspace or format.")
    verify_sources(collect(backend), manifest)
    print(
        f"Verified backup {migration['backupId']}: {len(manifest['revisions'])} versions.", flush=True
    )
    groups = {}
    for revision in manifest["revisions"]:
        groups.setdefault(revision["entityId"], []).append(revision)
    for entity, revisions in groups.items():
        file_id = manifest["canonicalFiles"][entity]
        content, etag = backend.versioned_get(file_id)
        desired = document(revisions)
        if content != desired:
            backend.replace(
                file_id,
                desired,
                etag,
                name=f"{entity}.json",
                properties={
                    "app": "salestrackr", "entityType": revisions[0]["entityType"], "entityId": entity
                },
            )
        if backend.get(file_id) != desired:
            raise Problem("Canonical file verification failed. Backup and source files retained.")
    verify_sources(collect(backend), manifest)
    canonical_ids = set(manifest["canonicalFiles"].values())
    for metadata in collect(backend)["files"]:
        if metadata["id"] in canonical_ids:
            continue
        content, etag = backend.versioned_get(metadata["id"])
        revisions = document_revisions(content) if content.get("schemaVersion") == 2 else [content]
        expected = {r["id"]: r for r in manifest["revisions"]}
        if any(expected.get(r["id"]) != r for r in revisions):
            raise Problem("A redundant file changed; it was not moved to Trash.")
        backend.trash(metadata["id"], etag=etag)
    remaining = collect(backend)
    verify_sources(remaining, manifest)
    if {f["id"] for f in remaining["files"]} != canonical_ids:
        raise Problem("Canonical file count verification failed.")
    checkpoints = list(
        backend._list(
            f"'{folders['indexes']}' in parents and trashed = false", fields="id,createdTime"
        )
    )
    checkpoint_id = backend.settings.get("checkpointId") or (
        max(checkpoints, key=lambda f: f["createdTime"])["id"] if checkpoints else backend.allocate()
    )
    update_settings(
        backend,
        {**backend.settings, "schemaVersion": 2, "checkpointId": checkpoint_id, "maintenance": True},
    )
    backend.ignore_checkpoints = True
    repo = Repository(backend)
    repo.sync(force=True)
    expected = {r["id"]: digest(r) for r in manifest["revisions"]}
    if {key: value["hash"] for key, value in repo.meta.items()} != expected:
        raise Problem("The rebuilt index does not match the verified backup.")
    backend.checkpoint(repo.state())
    backend.ignore_checkpoints = False
    recovered = Repository(backend)
    recovered.sync(force=True)
    if recovered.state() != repo.state():
        # The cursor can advance for the checkpoint's own write.
        before, after = repo.state(), recovered.state()
        before.pop("cursor")
        after.pop("cursor")
        if before != after:
            raise Problem("Checkpoint restart verification failed.")
    for checkpoint in checkpoints:
        if checkpoint["id"] != checkpoint_id:
            backend.trash(checkpoint["id"])
    final_settings = {
        key: value for key, value in backend.settings.items() if key not in ("migration", "maintenance")
    }
    final_settings["migrationBackupId"] = migration["backupId"]
    update_settings(backend, final_settings)
    print(
        f"Migration complete: {len(groups)} entity files, {len(expected)} preserved versions, "
        "one shared checkpoint. Redundant files moved to Trash; backup retained.",
        flush=True,
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "rebuild", "restore", "migrate"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--backup-id")
    parser.add_argument("--target-root")
    parser.add_argument("--apply", action="store_true", help="Apply a planned one-file migration")
    parser.add_argument(
        "--writers-stopped", action="store_true", help="Confirm the new release serves all traffic"
    )
    parser.add_argument("--drain-seconds", type=int, default=300)
    args = parser.parse_args()
    token = os.environ.get("GOOGLE_TOKEN_JSON")
    if not token:
        parser.error("Set GOOGLE_TOKEN_JSON securely in the operator environment.")
    backend = DriveBackend(token, args.root, operator=True)
    if args.action == "migrate":
        migrate(
            backend, apply=args.apply, writers_stopped=args.writers_stopped, drain_seconds=args.drain_seconds
        )
    elif args.action == "backup":
        backup(backend)
    elif args.action == "restore":
        if not args.backup_id or not args.target_root or args.target_root == args.root:
            parser.error("Restore requires --backup-id and a different --target-root.")
        restore(backend, args.backup_id, args.target_root, token)
    else:
        backend.ignore_checkpoints = True
        repo = Repository(backend)
        repo.sync(force=True)
        backend.checkpoint(repo.state())
        print(f"Shared checkpoint refreshed for {len(repo.meta)} embedded versions.")


if __name__ == "__main__":
    main()
