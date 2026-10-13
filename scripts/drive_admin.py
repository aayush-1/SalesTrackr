"""Create a Drive backup, rebuild a checkpoint, or restore to a new empty root."""

import argparse
import os
from uuid import uuid4

from backend.app.domain import Problem
from backend.app.storage import DriveBackend, Repository, digest


def backup(backend, repo):
    repo.sync(force=True)
    revisions = []
    for metadata in repo.meta.values():
        raw = backend.get(metadata["id"])
        if raw is None or digest(raw) != metadata["hash"]:
            raise Problem("Backup stopped: missing or modified source revision.")
        revisions.append(raw)
    manifest = {"format": 1, "sourceRoot": backend.root_id, "revisions": revisions}
    manifest["checksum"] = digest(revisions)
    saved = backend._upload(manifest, backend._folders()["backups"], f"backup_{uuid4().hex}.json")
    print(f"Backup file ID: {saved['id']}. Contains {len(revisions)} immutable versions.")


def restore(backend, backup_id, target_root, token):
    manifest = backend.get(backup_id)
    if (
        not manifest
        or manifest.get("format") != 1
        or manifest.get("checksum") != digest(manifest["revisions"])
    ):
        raise Problem("The backup is invalid or its checksum does not match.")
    target = DriveBackend(token, target_root)
    repo = Repository(target)
    repo.sync(force=True)
    if repo.meta:
        raise Problem(
            "Restore requires an empty app-created target root. Existing data is never overwritten."
        )
    mapping = {revision["id"]: target.allocate() for revision in manifest["revisions"]}
    for old in manifest["revisions"]:
        revision = {**old, "id": mapping[old["id"]], "parents": [mapping[p] for p in old["parents"]]}
        target.create(revision)
    repo = Repository(target)
    repo.sync(force=True)
    target.checkpoint(repo.state())
    print(f"Restored {len(repo.meta)} versions. Verify this root before changing production configuration.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "rebuild", "restore"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--backup-id")
    parser.add_argument("--target-root")
    args = parser.parse_args()
    token = os.environ.get("GOOGLE_TOKEN_JSON")
    if not token:
        parser.error("Set GOOGLE_TOKEN_JSON securely in the operator environment.")
    backend = DriveBackend(token, args.root)
    repo = Repository(backend)
    if args.action == "backup":
        backup(backend, repo)
    elif args.action == "restore":
        if not args.backup_id or not args.target_root or args.target_root == args.root:
            parser.error("Restore requires --backup-id and a different --target-root.")
        restore(backend, args.backup_id, args.target_root, token)
    else:
        backend.ignore_checkpoints = True
        repo.sync(force=True)
        backend.checkpoint(repo.state())
        print(f"Checkpoint refreshed for {len(repo.meta)} versions.")


if __name__ == "__main__":
    main()
