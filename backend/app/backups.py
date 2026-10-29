"""Verified, unencrypted application backups with staged, atomic recovery."""

import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from google.auth.exceptions import RefreshError, TransportError
from google.cloud import firestore
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from httplib2 import HttpLib2Error
from pydantic import ValidationError

from .domain import MODELS, Problem, now
from .firestore_storage import filtered
from .storage import DriveBackend, digest, document, document_revisions, encode, history_heads

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{10,160}$")
BACKUP_SOURCES = {"manual", "scheduled", "pre-restore", "migration"}
BACKUP_RETENTION = 10
BACKUP_FILES = {
    "account": "accounts.json", "contact": "contacts.json", "record": "sales-records.json",
    "model": "models.json", "sent_part": "sent-parts.json",
}


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise Problem("Invalid backup or operation identifier.")
    return value


def checked_revisions(revisions):
    if not isinstance(revisions, list):
        raise Problem("The backup has no valid saved-version list.", 422, "invalid_backup")
    groups, ids = {}, set()
    for raw in revisions:
        if not isinstance(raw, dict) or raw.get("id") in ids:
            raise Problem("The backup contains invalid or duplicate versions.", 422, "invalid_backup")
        groups.setdefault(raw.get("entityId"), []).append(raw)
        ids.add(raw.get("id"))
    counts = {kind: 0 for kind in MODELS}
    try:
        for versions in groups.values():
            history_heads(versions)
            counts[versions[0]["entityType"]] += 1
    except (Problem, ValidationError, ValueError, KeyError, TypeError):
        raise Problem(
            "The backup contains invalid data or incomplete history.", 422, "invalid_backup"
        ) from None
    return {**counts, "revisions": len(revisions)}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _json(data):
    return json.loads(
        data, object_pairs_hook=_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid JSON number")),
    )


def archive(revisions, *, source, operation_id, actor, control):
    counts = checked_revisions(revisions)
    hashes = {raw["id"]: digest(raw) for raw in revisions}
    groups = {}
    for raw in revisions:
        groups.setdefault(raw["entityId"], []).append(raw)
    records = {kind: [] for kind in MODELS}
    for entity in sorted(groups):
        versions = groups[entity]
        records[versions[0]["entityType"]].append(document(versions))
    files = {
        BACKUP_FILES[kind]: json.dumps(values, ensure_ascii=False, indent=2).encode()
        for kind, values in records.items()
    }
    manifest = {
        "format": "salestrackr-backup", "version": 2, "createdAt": now(),
        "source": source, "operationId": operation_id, "actor": actor,
        "generation": control["generation"], "sequence": control["sequence"],
        "counts": counts, "hashes": hashes, "dataHash": digest(hashes),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    manifest["checksum"] = digest(manifest)
    output = io.BytesIO()
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr("manifest.json", manifest_bytes)
        expanded = len(manifest_bytes)
        for name, data in files.items():
            expanded += len(data)
            if expanded > MAX_EXPANDED:
                raise Problem("The backup exceeds the 256 MiB expanded-data limit.", 413, "backup_size")
            target.writestr(name, data)
    return output.getvalue(), manifest


def read_archive(data):
    if len(data) > MAX_ARCHIVE:
        raise Problem("Backups must be at most 64 MiB.", 413, "backup_size")
    try:
        if not data.startswith(b"PK"):
            legacy = _json(data)
            from scripts.drive_admin import checked_manifest

            checked_manifest(legacy)
            revisions = legacy["revisions"]
            counts = checked_revisions(revisions)
            hashes = {raw["id"]: digest(raw) for raw in revisions}
            created_at = legacy.get("createdAt") or now()
            if datetime.fromisoformat(created_at).tzinfo is None:
                raise ValueError("Backup time has no timezone")
            manifest = {
                "format": "salestrackr-backup", "version": 1,
                "createdAt": created_at, "source": "migration",
                "counts": counts, "hashes": hashes, "dataHash": digest(hashes),
                "legacy": not bool(legacy.get("createdAt")),
            }
            return revisions, manifest
        with zipfile.ZipFile(io.BytesIO(data)) as source:
            files = source.infolist()
            names = [item.filename for item in files]
            if (
                len(names) != len(set(names)) or "manifest.json" not in names
                or len(files) > 100_001 or sum(item.file_size for item in files) > MAX_EXPANDED
                or any(
                    item.flag_bits & 1 or item.is_dir() or item.file_size < 0
                    or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or ((item.external_attr >> 16) & 0o170000) == 0o120000
                    for item in files
                )
            ):
                raise ValueError("Unsafe or oversized archive")
            manifest = _json(source.read("manifest.json"))
            content = {key: value for key, value in manifest.items() if key != "checksum"}
            created_at = datetime.fromisoformat(manifest.get("createdAt", ""))
            if (
                manifest.get("format") != "salestrackr-backup" or manifest.get("version") not in (1, 2)
                or created_at.tzinfo is None or manifest.get("source") not in BACKUP_SOURCES
                or manifest.get("checksum") != digest(content)
                or not isinstance(manifest.get("hashes"), dict)
                or manifest.get("dataHash") != digest(manifest["hashes"])
                or any(not IDENTIFIER.fullmatch(key) for key in manifest["hashes"])
            ):
                raise ValueError("Invalid manifest")
            revisions, entities = [], set()
            if manifest["version"] == 1:
                if set(names) != {"manifest.json", *(f"revisions/{key}.json" for key in manifest["hashes"])}:
                    raise ValueError("Unexpected archive entry")
                revisions = [_json(source.read(f"revisions/{key}.json")) for key in manifest["hashes"]]
            else:
                if (
                    set(names) != {"manifest.json", *BACKUP_FILES.values()}
                    or set(manifest.get("files", {})) != set(BACKUP_FILES.values())
                ):
                    raise ValueError("Unexpected archive entry")
                for kind, name in BACKUP_FILES.items():
                    data = source.read(name)
                    if hashlib.sha256(data).hexdigest() != manifest["files"][name]:
                        raise ValueError("File checksum mismatch")
                    values = _json(data)
                    if not isinstance(values, list):
                        raise ValueError("Invalid entity list")
                    for value in values:
                        if value.get("entityType") != kind or value.get("entityId") in entities:
                            raise ValueError("Entity is in the wrong backup file")
                        entities.add(value.get("entityId"))
                        revisions.extend(document_revisions(value))
            if (
                checked_revisions(revisions) != manifest["counts"]
                or {raw["id"]: digest(raw) for raw in revisions} != manifest["hashes"]
            ):
                raise ValueError("Count mismatch")
            return sorted(revisions, key=lambda raw: raw["id"]), manifest
    except (
        Problem, zipfile.BadZipFile, ValueError, KeyError, TypeError, AttributeError, RuntimeError,
        RecursionError,
    ) as error:
        if isinstance(error, Problem) and error.code == "backup_size":
            raise
        raise Problem(
            "This is not a complete, supported SalesTrackr backup. No live data was changed.",
            422, "invalid_backup",
        ) from None


def backup_row(file_id, data, manifest, *, name=None):
    extension = "zip" if data.startswith(b"PK") else "json"
    return {
        "id": file_id,
        "name": name or f"SalesTrackr-{manifest['source']}-{manifest['createdAt'][:10]}.{extension}",
        "createdAt": manifest["createdAt"], "source": manifest["source"],
        "protected": False, "counts": manifest["counts"],
        "sizeBytes": len(data),
    }


class LocalBackupStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.folder_url = None

    def allocate(self):
        return uuid4().hex

    def _path(self, file_id):
        identifier(file_id)
        zip_path = self.directory / f"{file_id}.zip"
        json_path = self.directory / f"{file_id}.json"
        return json_path if not zip_path.exists() and json_path.exists() else zip_path

    def inventory(self):
        rows = []
        for path in sorted(self.directory.iterdir()):
            if path.suffix not in (".zip", ".json") or not IDENTIFIER.fullmatch(path.stem):
                continue
            if not path.is_file():
                continue
            size = path.stat().st_size
            data = self.read(path.stem) if size <= MAX_ARCHIVE else None
            checksum = hashlib.md5(data, usedforsecurity=False).hexdigest() if data is not None else None
            rows.append({
                "id": path.stem, "name": path.name, "sizeBytes": size,
                "checksum": checksum,
                "createdAt": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            })
        return rows

    def read(self, file_id):
        path = self._path(file_id)
        try:
            with path.open("rb") as source:
                data = source.read(MAX_ARCHIVE + 1)
        except FileNotFoundError:
            return None
        if len(data) > MAX_ARCHIVE:
            raise Problem("The local backup exceeds the 64 MiB size limit.", 413, "backup_size")
        return data

    def put(self, file_id, data, name):
        path = self.directory / f"{identifier(file_id)}.zip"
        if path.exists():
            if path.read_bytes() != data:
                raise Problem("A backup identifier already contains different data.", 409, "backup_changed")
            return
        temporary = self.directory / f"{file_id}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise Problem(
                        "A backup identifier already contains different data.", 409, "backup_changed"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)

    def trash(self, file_id):
        path = self._path(file_id)
        if path.exists():
            trash = self.directory / "trash"
            trash.mkdir(exist_ok=True)
            path.replace(trash / path.name)


class DriveBackupStore:
    app = "salestrackr-backup"

    def __init__(self, settings):
        self.backend = DriveBackend(settings.token_json, settings.root_id, operator=True)
        self.folder_id = settings.backup_folder_id
        self.folder_url = None

    def _folder(self):
        if not self.folder_id:
            raise Problem("A separate Firestore backup folder must be configured.", 503, "backup_folder")
        folder = identifier(self.folder_id)
        metadata = self.backend._execute(self.backend._api().files().get(
            fileId=folder, fields="id,parents,mimeType,trashed,appProperties",
        ))
        if (
            metadata.get("trashed") or metadata.get("parents") != [self.backend.root_id]
            or metadata.get("mimeType") != "application/vnd.google-apps.folder"
            or metadata.get("appProperties", {}).get("app") != "salestrackr-firestore-backups"
        ):
            raise Problem(
                "Use the dedicated Firestore backup folder inside the SalesTrackr root.",
                503, "backup_folder",
            )
        token = None
        while True:
            permissions = self.backend._execute(self.backend._api().permissions().list(
                fileId=folder, fields="nextPageToken,permissions(type)", pageSize=100, pageToken=token,
            ))
            if any(permission["type"] in ("anyone", "domain") for permission in permissions["permissions"]):
                raise Problem(
                    "Use a private Drive backup folder, not public-link or whole-domain access.",
                    503, "backup_permissions",
                )
            token = permissions.get("nextPageToken")
            if not token:
                break
        self.folder_url = f"https://drive.google.com/drive/folders/{folder}"
        return folder

    def allocate(self):
        return self.backend.allocate()

    def inventory(self):
        folder = self._folder()
        return [{
            "id": item["id"], "name": item["name"], "sizeBytes": int(item.get("size", 0)),
            "checksum": item.get("md5Checksum"), "createdAt": item["createdTime"],
        } for item in self.backend._list(
            f"'{folder}' in parents and trashed = false "
            "and (mimeType = 'application/zip' or mimeType = 'application/json')",
            fields="id,name,size,md5Checksum,createdTime",
        )]

    def _metadata(self, file_id):
        try:
            value = self.backend._execute(self.backend._api().files().get(
                fileId=identifier(file_id),
                fields="id,parents,mimeType,trashed,appProperties,size,md5Checksum",
            ))
        except Problem as error:
            if error.code == "drive_missing":
                return None
            raise
        if value.get("trashed"):
            return None
        if (
            value.get("parents") != [self._folder()]
            or value.get("mimeType") not in ("application/zip", "application/json")
        ):
            raise Problem("The backup was moved or replaced in Drive.", 409, "backup_changed")
        if int(value.get("size", MAX_ARCHIVE + 1)) > MAX_ARCHIVE:
            raise Problem("The Drive backup exceeds the 64 MiB size limit.", 413, "backup_size")
        return value

    def read(self, file_id):
        metadata = self._metadata(file_id)
        if metadata is None:
            return None
        output = io.BytesIO()
        downloader = MediaIoBaseDownload(
            output, self.backend._api().files().get_media(fileId=file_id), chunksize=1024 * 1024,
        )
        try:
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)
                if output.tell() > MAX_ARCHIVE:
                    raise Problem("The Drive backup exceeds the 64 MiB size limit.", 413, "backup_size")
        except (HttpError, OSError, HttpLib2Error, RefreshError, TransportError):
            raise Problem("Cannot download the Drive backup. Please retry.", 503, "backup_drive") from None
        data = output.getvalue()
        if (
            len(data) != int(metadata["size"])
            or hashlib.md5(data, usedforsecurity=False).hexdigest() != metadata.get("md5Checksum")
        ):
            raise Problem("The Drive backup changed while downloading.", 409, "backup_changed")
        return data

    def put(self, file_id, data, name):
        existing = self.read(file_id)
        if existing is not None:
            if existing != data:
                raise Problem("This Drive backup contains different data.", 409, "backup_changed")
            return
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/zip", resumable=False)
        self.backend._execute(self.backend._api().files().create(
            body={
                "id": file_id, "name": name, "parents": [self._folder()],
                "appProperties": {"app": self.app},
            },
            media_body=media, fields="id",
        ))

    def trash(self, file_id):
        if self._metadata(file_id):
            self.backend.trash(file_id, etag=self.backend.etag(file_id))


class BackupManager:
    archive = staticmethod(archive)
    read_archive = staticmethod(read_archive)
    backup_row = staticmethod(backup_row)

    def __init__(self, repo, settings, store=None):
        self.repo, self.settings = repo, settings
        self.store = store or (
            LocalBackupStore(settings.data_dir / "backups")
            if settings.mode == "demo" else DriveBackupStore(settings)
        )
        self.signing_key = secrets.token_bytes(32)

    def admin(self, actor):
        return self.settings.mode == "demo" or actor.casefold() in self.settings.recovery_admins

    def require_admin(self, actor):
        if not self.admin(actor):
            raise Problem(
                "Backup recovery requires an authorized recovery administrator.", 403, "recovery_admin"
            )

    def _catalog(self):
        return self.repo.workspace.collection("backups")

    def rows(self):
        return self._available()[0]

    def _available(self):
        query = filtered(filtered(self._catalog(), "verified", "==", True), "pruned", "==", False)
        known = {item.id: item.to_dict() for item in query.stream()}
        inventory = self.store.inventory()
        actual = {item["id"] for item in inventory}
        warning = bool(known.keys() - actual)
        rows = []
        for item in inventory:
            row = known.get(item["id"]) or self._catalog().document(item["id"]).get().to_dict()
            if (
                row and row.get("verified") and item["checksum"]
                and row.get("providerChecksum") == item["checksum"]
            ):
                row = {**row, "protected": False, "pruned": False}
                if isinstance(self.store, DriveBackupStore):
                    row["name"] = item["name"]
                if row != known.get(item["id"]):
                    self._catalog().document(item["id"]).set(row)
                rows.append(row)
                continue
            try:
                data = self.store.read(item["id"])
                if data is None:
                    warning = True
                    continue
                checksum = hashlib.sha256(data).hexdigest()
                if row and row.get("verified") and checksum != row["archiveHash"]:
                    warning = True
                    continue
                _, manifest = self.read_archive(data)
                if manifest.get("legacy"):
                    manifest["createdAt"] = item["createdAt"]
                value = {
                    **self.backup_row(item["id"], data, manifest, name=item["name"]),
                    "archiveHash": checksum, "providerChecksum": item["checksum"],
                    "verified": True, "pruned": False,
                }
                if row and row.get("verified"):
                    value.update(createdAt=row["createdAt"], name=row["name"])
                self._catalog().document(item["id"]).set(value)
                rows.append(value)
            except Problem as error:
                if error.code not in ("invalid_backup", "backup_changed", "backup_size"):
                    raise
                warning = True
        notice = (
            "Some backup files are missing, changed, or invalid and were not listed. Check the backup folder."
            if warning else None
        )
        return sorted(
            rows, key=lambda row: (datetime.fromisoformat(row["createdAt"]), row["id"]), reverse=True,
        ), notice

    def status(self, actor):
        with self.repo.lock:
            self.repo.sync()
            control = self.repo.control.copy()
        rows, inventory_error = self._available()
        return {
            "mode": self.settings.mode, "storageBackend": "firestore",
            "location": (
                "Local Firestore emulator and simulated Drive backup folder"
                if self.settings.mode == "demo" else "Private Google Drive backup folder"
            ),
            "folderUrl": self.store.folder_url,
            "schedule": {
                "enabled": control.get("weeklyEnabled", True),
                "active": self.settings.backup_schedule_ready and self.settings.mode == "production",
                "weekday": "Sunday", "time": "02:00", "timezone": "Asia/Kolkata",
            },
            "retention": BACKUP_RETENTION, "backups": [{k: v for k, v in row.items() if k in (
                "id", "name", "createdAt", "source", "protected", "counts", "sizeBytes",
            )} for row in rows],
            "lastBackupAt": rows[0]["createdAt"] if rows else None,
            "lastError": control.get("lastBackupError") or inventory_error,
            "canBackup": True, "canRestore": self.admin(actor), "generation": control["generation"],
        }

    def preference(self, enabled, actor):
        self.require_admin(actor)
        self.repo.workspace.update({"weeklyEnabled": enabled})
        return self.status(actor)

    def get(self, file_id):
        row = self._catalog().document(identifier(file_id)).get().to_dict()
        if not row or row.get("pruned") or not row.get("verified"):
            raise Problem("Backup not found or no longer retained.", 404, "backup_not_found")
        data = self.store.read(file_id)
        if data is None:
            raise Problem("The backup is missing from its backup folder.", 409, "backup_missing")
        if hashlib.sha256(data).hexdigest() != row["archiveHash"]:
            raise Problem("The backup changed after verification.", 409, "backup_changed")
        return data, row

    def completed_restore(self, file_id, write_id, actor, confirmation):
        self.require_admin(actor)
        identifier(file_id)
        identifier(write_id)
        if confirmation != "RESTORE":
            raise Problem("Type RESTORE to confirm full-workspace recovery.", 422, "restore_confirmation")
        prior = self.repo.workspace.collection("restoreOperations").document(write_id).get().to_dict()
        if not prior or not prior.get("completed"):
            return None
        row = self._catalog().document(file_id).get().to_dict()
        if not row or prior["actor"] != actor or prior["archiveHash"] != row.get("archiveHash"):
            raise Problem("Restore identifier was used for different content.", 409, "write_id_reused")
        return prior["result"]

    def check_backup_control(self, control):
        pass

    def _lease(self, operation_id, actor, source, restore_write_id=None):
        owner = uuid4().hex
        operation = self.repo.workspace.collection("backupOperations").document(operation_id)

        @firestore.transactional
        def claim(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            prior = operation.get(transaction=transaction).to_dict()
            if prior and (prior["actor"] != actor or prior["source"] != source):
                raise Problem("Backup identifier was used for another request.", 409, "write_id_reused")
            if prior and prior.get("completed"):
                return prior, None
            self.check_backup_control(control)
            maintenance = control.get("maintenance")
            if maintenance and not (
                source == "pre-restore" and maintenance["writeId"] == restore_write_id
            ):
                raise Problem("Storage maintenance is in progress.", 503, "maintenance")
            lease = control.get("backupLease")
            if lease and lease["expires"] > time.time():
                raise Problem(
                    "Another backup is in progress. Retry shortly.",
                    503 if lease["operationId"] == operation_id else 409, "backup_in_progress",
                )
            transaction.update(self.repo.workspace, {
                "backupLease": {"owner": owner, "operationId": operation_id, "expires": time.time() + 600},
            })
            if not prior:
                prior = {"actor": actor, "source": source, "completed": False}
                transaction.create(operation, prior)
            return prior, owner

        prior, lease_owner = claim(self.repo.client.transaction())
        return operation, prior, lease_owner

    def _finish(self, operation, owner, row=None, error=None):
        @firestore.transactional
        def finish(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            if (control.get("backupLease") or {}).get("owner") != owner:
                raise Problem(
                    "Backup ownership changed; retry to reconcile the result.", 409, "backup_in_progress"
                )
            transaction.update(self.repo.workspace, {"backupLease": None, "lastBackupError": error})
            if row:
                transaction.update(operation, {"completed": True, "backupId": row["id"]})

        finish(self.repo.client.transaction())

    def _prune(self):
        for row in self.rows()[BACKUP_RETENTION:]:
            self.get(row["id"])
            self.store.trash(row["id"])
            self._catalog().document(row["id"]).update({"pruned": True})

    def backup(self, operation_id, actor, *, source="manual", prune=True, restore_write_id=None):
        identifier(operation_id)
        if source not in BACKUP_SOURCES:
            raise Problem("Unknown backup source.")
        operation, prior, owner = self._lease(operation_id, actor, source, restore_write_id)
        if owner is None:
            _, row = self.get(prior["backupId"])
            return row
        try:
            file_id = prior.get("fileId")
            if not file_id:
                file_id = self.store.allocate()
                operation.update({"fileId": file_id})
            data = self.store.read(file_id)
            if data is not None:
                _, manifest = self.read_archive(data)
                if (
                    manifest.get("operationId") != operation_id or manifest.get("actor") != actor
                    or manifest.get("source") != source
                ):
                    raise Problem("The prior backup does not match this operation.", 409, "backup_changed")
            else:
                revisions, control = self.repo.snapshot()
                data, manifest = self.archive(
                    revisions, source=source, operation_id=operation_id, actor=actor, control=control,
                )
                if len(data) > MAX_ARCHIVE:
                    raise Problem("The backup exceeds the 64 MiB archive limit.", 413, "backup_size")
                row = self.backup_row(file_id, data, manifest)
                self.store.put(file_id, data, row["name"])
            downloaded = self.store.read(file_id)
            if downloaded != data:
                raise Problem("Backup readback failed. Existing backups were retained.", 503, "backup_verify")
            self.read_archive(downloaded)
            lease = self.repo.workspace.get().to_dict().get("backupLease") or {}
            if lease.get("owner") != owner:
                raise Problem(
                    "The backup is still being reconciled. Retry the same request.", 503, "backup_busy"
                )
            row = {
                **self.backup_row(file_id, data, manifest),
                "archiveHash": hashlib.sha256(data).hexdigest(), "verified": True, "pruned": False,
                "providerChecksum": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            }
            self._catalog().document(file_id).set(row)
            if prune:
                self._prune()
            self._finish(operation, owner, row)
            return row
        except Exception:
            try:
                self._finish(operation, owner, error="Backup did not finish. Existing copies were retained.")
            except Exception:
                pass
            raise

    def _token(self, value):
        encoded = base64.urlsafe_b64encode(encode(value)).decode()
        signature = hmac.new(self.signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def _read_token(self, token, actor, data):
        try:
            encoded, signature = token.split(".")
            expected = hmac.new(self.signing_key, encoded.encode(), hashlib.sha256).hexdigest()
            value = _json(base64.urlsafe_b64decode(encoded))
            if (
                not hmac.compare_digest(signature, expected) or value["expires"] < time.time()
                or value["actor"] != actor or value["hash"] != hashlib.sha256(data).hexdigest()
            ):
                raise ValueError("Expired or changed preview")
            return value
        except (ValueError, KeyError, TypeError):
            raise Problem(
                "The recovery preview expired or changed. Inspect the backup again.",
                409, "preview_expired",
            ) from None

    def inspect(self, data, actor, row=None):
        self.require_admin(actor)
        revisions, manifest = self.read_archive(data)
        with self.repo.lock:
            self.repo.sync()
            control = self.repo.control.copy()
        if control.get("maintenance"):
            raise Problem("Storage maintenance is in progress.", 503, "maintenance")
        value = {
            "hash": hashlib.sha256(data).hexdigest(), "actor": actor,
            "generation": control["generation"], "sequence": control["sequence"],
            "expires": time.time() + 600,
        }
        warnings = [
            "This replaces the entire live workspace; it does not merge records.",
            "Current data is backed up before the verified restore is activated.",
            "All open account, contact and sales forms must be refreshed afterwards.",
        ]
        if manifest.get("legacy"):
            warnings.append(
                "Legacy JSON did not record its backup time; the displayed timestamp is validation time."
            )
        parents = {parent for revision in revisions for parent in revision["parents"]}
        heads = Counter(revision["entityId"] for revision in revisions if revision["id"] not in parents)
        if any(count > 1 for count in heads.values()):
            warnings.append(
                "This backup contains existing conflicts, which will remain available for review."
            )
        return {
            "token": self._token(value), "backup": row or self.backup_row("uploaded-backup", data, manifest),
            "generation": control["generation"], "warnings": warnings,
            "expiresAt": datetime.fromtimestamp(value["expires"], timezone.utc).isoformat(),
        }

    def archived_metadata(self, recovery):
        return {}

    def restore(self, data, actor, token, confirmation, write_id):
        self.require_admin(actor)
        identifier(write_id)
        if confirmation != "RESTORE":
            raise Problem("Type RESTORE to confirm full-workspace recovery.", 422, "restore_confirmation")
        revisions, manifest = self.read_archive(data)
        archive_hash = hashlib.sha256(data).hexdigest()
        operation = self.repo.workspace.collection("restoreOperations").document(write_id)
        owner = uuid4().hex
        prior = operation.get().to_dict()
        if prior and (prior["actor"] != actor or prior["archiveHash"] != archive_hash):
            raise Problem("Restore identifier was used for different content.", 409, "write_id_reused")
        if prior and prior.get("completed"):
            return prior["result"]
        preview = prior or self._read_token(token, actor, data)

        @firestore.transactional
        def begin(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            existing = operation.get(transaction=transaction).to_dict()
            if existing and (
                existing["actor"] != actor or existing["archiveHash"] != archive_hash
            ):
                raise Problem("Restore identifier was used for different content.", 409, "write_id_reused")
            if existing and existing.get("completed"):
                return existing
            if control.get("pendingRecordCleanup"):
                raise Problem("Finish the pending record cleanup before restoring.", 409, "cleanup_pending")
            if any(control[key] != preview[key] for key in ("generation", "sequence")):
                raise Problem(
                    "Live data changed. Inspect the backup again before restoring.", 409, "preview_stale"
                )
            maintenance = control.get("maintenance")
            if maintenance and maintenance.get("expires", float("inf")) > time.time():
                raise Problem(
                    "A restore is still running. Retry the same request shortly.",
                    503 if maintenance["writeId"] == write_id else 409, "restore_in_progress",
                )
            lease = control.get("backupLease")
            if lease and lease["expires"] > time.time():
                raise Problem("A backup is in progress. Retry after it finishes.", 409, "backup_in_progress")
            value = existing or {
                "actor": actor, "archiveHash": archive_hash,
                "generation": control["generation"], "sequence": control["sequence"], "completed": False,
            }
            transaction.set(operation, value)
            transaction.update(self.repo.workspace, {
                "maintenance": {
                    "kind": "restore", "writeId": write_id, "owner": owner,
                    "startedAt": now(), "expires": time.time() + 600,
                },
            })
            return value

        state = begin(self.repo.client.transaction())
        if state.get("completed"):
            return state["result"]
        recovery = None
        try:
            recovery_id = f"pre-restore-{hashlib.sha256(write_id.encode()).hexdigest()}"
            recovery = self.backup(
                recovery_id, actor, source="pre-restore", prune=False, restore_write_id=write_id,
            )
            target, statistics = self.repo.stage(revisions)

            @firestore.transactional
            def activate(transaction):
                control = self.repo.workspace.get(transaction=transaction).to_dict()
                existing = operation.get(transaction=transaction).to_dict()
                if existing.get("completed"):
                    return existing["result"], control
                if (
                    control["generation"] != state["generation"]
                    or control["sequence"] != state["sequence"]
                    or (control.get("maintenance") or {}).get("owner") != owner
                ):
                    raise Problem("Live workspace changed. Restore was not activated.", 409, "preview_stale")
                old = self.repo.workspace.collection("datasets").document(control["activeDataset"])
                updated = {
                    **control, **statistics, "activeDataset": target.id, "generation": target.id,
                    "sequence": control["sequence"] + 1, "maintenance": None,
                }
                result = {
                    "restored": True, "generation": target.id, "counts": manifest["counts"],
                    "recoveryBackup": recovery,
                }
                transaction.set(old, {
                    "state": "archived", "archivedAt": now(), **self.archived_metadata(recovery),
                }, merge=True)
                transaction.set(target, {"state": "active", "activatedAt": now()}, merge=True)
                transaction.set(self.repo.workspace, updated)
                transaction.update(operation, {"completed": True, "result": result})
                return result, updated

            with self.repo.lock:
                result, self.repo.control = activate(self.repo.client.transaction())
            try:
                self._prune()
            except Exception:
                self.repo.workspace.update({
                    "lastBackupError": "Restore succeeded; old-backup cleanup must be retried.",
                })
            return result
        except Exception:
            @firestore.transactional
            def release(transaction):
                control = self.repo.workspace.get(transaction=transaction).to_dict()
                existing = operation.get(transaction=transaction).to_dict()
                if existing.get("completed"):
                    return existing["result"]
                if (
                    control["generation"] == state["generation"]
                    and (control.get("maintenance") or {}).get("owner") == owner
                ):
                    transaction.update(self.repo.workspace, {"maintenance": None})
                return None

            confirmed = release(self.repo.client.transaction())
            if recovery is not None:
                try:
                    self._prune()
                except Exception:
                    self.repo.workspace.update({
                        "lastBackupError": "Restore backup cleanup must be retried.",
                    })
            if confirmed:
                return confirmed
            raise


def scheduled_operation_id():
    from zoneinfo import ZoneInfo

    local = datetime.now(ZoneInfo("Asia/Kolkata"))
    sunday = local.date() - timedelta(days=(local.weekday() + 1) % 7)
    return f"weekly-{sunday.isoformat()}"
