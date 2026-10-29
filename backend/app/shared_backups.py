"""One verified recovery point and atomic activation for both Trackr workspaces."""

import hashlib
import io
import time
import zipfile
from datetime import datetime, timezone
from uuid import uuid4

from google.cloud import firestore

from .backups import (
    MAX_ARCHIVE,
    MAX_EXPANDED,
    BackupManager,
    DriveBackupStore,
    LocalBackupStore,
    _json,
    identifier,
)
from .backups import archive as sales_archive
from .backups import read_archive as read_sales_archive
from .domain import Problem, now
from .manufacturing_backups import archive as product_archive
from .manufacturing_backups import read_archive as read_product_archive
from .manufacturing_storage import ManufacturingRepository
from .storage import digest, encode


def archive(data, *, source, operation_id, actor, control):
    arguments = {"source": source, "operation_id": operation_id, "actor": actor}
    sales, sales_manifest = sales_archive(data["sales"], control=control, **arguments)
    products, product_manifest = product_archive(
        data["manufacturing"],
        control={"generation": control["manufacturingGeneration"],
                 "sequence": control["manufacturingSequence"]},
        **arguments,
    )
    files = {"salestrackr.zip": sales, "producttrackr.zip": products}
    manifest = {
        "format": "trackr-workspace-backup", "version": 1, "createdAt": now(),
        **arguments, "operationId": operation_id,
        "generation": control["generation"], "sequence": control["sequence"],
        "manufacturingGeneration": control["manufacturingGeneration"],
        "manufacturingSequence": control["manufacturingSequence"],
        "counts": {**sales_manifest["counts"], "products": product_manifest["counts"]["products"],
                   "manufacturingRecords": product_manifest["counts"]["manufacturingRecords"]},
        "dataHash": digest([sales_manifest["dataHash"], product_manifest["dataHash"]]),
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
    }
    manifest.pop("operation_id")
    manifest["checksum"] = digest(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as target:
        target.writestr("manifest.json", encode(manifest))
        for name, value in files.items():
            target.writestr(name, value)
    content = output.getvalue()
    read_archive(content)
    return content, manifest


def read_archive(content):
    if len(content) > MAX_ARCHIVE:
        raise Problem("Backups must be at most 64 MiB.", 413, "backup_size")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as source:
            entries = source.infolist()
            if (
                len(entries) != 3
                or {row.filename for row in entries}
                != {"manifest.json", "salestrackr.zip", "producttrackr.zip"}
                or sum(row.file_size for row in entries) > MAX_EXPANDED
                or any(row.flag_bits & 1 or row.is_dir() or row.file_size < 0
                       or row.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                       or ((row.external_attr >> 16) & 0o170000) == 0o120000 for row in entries)
            ):
                raise ValueError("Incomplete combined archive.")
            manifest = _json(source.read("manifest.json"))
            if (
                manifest["format"] != "trackr-workspace-backup" or manifest["version"] != 1
                or manifest["checksum"] != digest({k: v for k, v in manifest.items() if k != "checksum"})
                or set(manifest["files"]) != {"salestrackr.zip", "producttrackr.zip"}
            ):
                raise ValueError("Invalid combined manifest.")
            parts, expanded = {}, 0
            for name in manifest["files"]:
                parts[name] = source.read(name)
                if hashlib.sha256(parts[name]).hexdigest() != manifest["files"][name]:
                    raise ValueError("A workspace backup changed.")
                with zipfile.ZipFile(io.BytesIO(parts[name])) as child:
                    expanded += sum(row.file_size for row in child.infolist())
            if expanded > MAX_EXPANDED:
                raise ValueError("Combined expanded data is too large.")
            sales, sales_manifest = read_sales_archive(parts["salestrackr.zip"])
            products, product_manifest = read_product_archive(parts["producttrackr.zip"])
            if (
                manifest["dataHash"] != digest([sales_manifest["dataHash"], product_manifest["dataHash"]])
                or manifest["counts"] != {
                    **sales_manifest["counts"], "products": product_manifest["counts"]["products"],
                    "manufacturingRecords": product_manifest["counts"]["manufacturingRecords"],
                }
                or any(manifest[key] != sales_manifest[key] for key in ("source", "operationId", "actor"))
                or any(manifest[key] != product_manifest[key] for key in ("source", "operationId", "actor"))
            ):
                raise ValueError("Inconsistent combined snapshot.")
            return {"sales": sales, "manufacturing": products}, manifest
    except (Problem, ValueError, TypeError, KeyError, AttributeError, OSError, RuntimeError,
            zipfile.BadZipFile, EOFError, OverflowError, RecursionError):
        raise Problem(
            "Select a complete combined SalesTrackr + ProductTrackr ZIP. "
            "Separate legacy archives cannot replace both workspaces. No live data was changed.",
            422, "invalid_backup",
        ) from None


class SharedDriveStore(DriveBackupStore):
    app = "trackr-workspace-backup"

    def inventory(self):
        folder = self._folder()
        return [
            {"id": row["id"], "name": row["name"], "sizeBytes": int(row.get("size", 0)),
             "checksum": row.get("md5Checksum"), "createdAt": row["createdTime"]}
            for row in self.backend._list(
                f"'{folder}' in parents and trashed = false "
                f"and appProperties has {{ key='app' and value='{self.app}' }}",
                fields="id,name,size,md5Checksum,createdTime",
            )
        ]

    def _metadata(self, file_id):
        value = super()._metadata(file_id)
        if value and value.get("appProperties", {}).get("app") != self.app:
            raise Problem("This is not a combined workspace backup.", 409, "backup_changed")
        return value


class SharedRepository:
    def __init__(self, sales, products):
        self.sales, self.products = sales, products

    def __getattr__(self, key):
        return getattr(self.sales, key)

    def sync(self):
        self.sales.sync()
        self.products.sync()

    def snapshot(self):
        for _ in range(3):
            self.sync()
            before = [repo.workspace.get().to_dict() for repo in (self.sales, self.products)]
            sales, _ = self.sales.snapshot()
            products, _ = self.products.snapshot()
            after = [repo.workspace.get().to_dict() for repo in (self.sales, self.products)]
            if all(old[key] == new[key] for old, new in zip(before, after)
                   for key in ("generation", "sequence")):
                return {"sales": sales, "manufacturing": products}, {
                    **before[0], "manufacturingGeneration": before[1]["generation"],
                    "manufacturingSequence": before[1]["sequence"],
                }
        raise Problem("A workspace changed during backup. Retry the same backup.", 409, "snapshot_changed")


class SharedBackupManager(BackupManager):
    archive = staticmethod(archive)
    read_archive = staticmethod(read_archive)

    def __init__(self, sales, settings, store=None):
        if not settings.manufacturing_enabled:
            raise RuntimeError("Combined recovery requires ProductTrackr to be configured.")
        settings.validate_manufacturing()
        products = ManufacturingRepository(settings, client=sales.client)
        store = store or (
            LocalBackupStore(settings.data_dir / "shared-backups")
            if settings.mode == "demo" else SharedDriveStore(settings)
        )
        super().__init__(SharedRepository(sales, products), settings, store)

    def _catalog(self):
        return self.repo.workspace.collection("sharedBackups")

    def admin(self, actor):
        return self.settings.mode == "demo" or (
            actor.casefold() in self.settings.recovery_admins
            and actor.casefold() in (self.settings.manufacturing_admins or self.settings.recovery_admins)
        )

    @staticmethod
    def backup_row(file_id, data, manifest, *, name=None):
        return {
            "id": file_id, "name": name or f"Trackr-{manifest['source']}-{manifest['createdAt'][:10]}.zip",
            "createdAt": manifest["createdAt"], "source": manifest["source"], "protected": False,
            "counts": manifest["counts"], "sizeBytes": len(data), "dataHash": manifest["dataHash"],
        }

    def status(self, actor):
        result = super().status(actor)
        return {**result, "includesManufacturing": True,
                "manufacturingGeneration": self.repo.products.control["generation"],
                "location": "Local combined workspace backups" if self.settings.mode == "demo"
                else "SalesTrackr / Backups in private Google Drive"}

    def _lease(self, operation_id, actor, source, restore_write_id=None):
        owner = uuid4().hex
        operation = self.repo.workspace.collection("backupOperations").document(operation_id)

        @firestore.transactional
        def claim(transaction):
            controls = [repo.workspace.get(transaction=transaction).to_dict()
                        for repo in (self.repo.sales, self.repo.products)]
            prior = operation.get(transaction=transaction).to_dict()
            if prior and (prior["actor"] != actor or prior["source"] != source):
                raise Problem("Backup identifier was used for another request.", 409, "write_id_reused")
            if prior and prior.get("completed"):
                return prior, None
            for control in controls:
                if control.get("pendingRecordCleanup"):
                    raise Problem("Finish record cleanup before backing up either workspace.",
                                  503, "cleanup_pending")
                maintenance = control.get("maintenance")
                if maintenance and not (
                    source == "pre-restore" and maintenance["writeId"] == restore_write_id
                ):
                    raise Problem("Workspace maintenance is in progress.", 503, "maintenance")
                lease = control.get("backupLease") or {}
                if lease.get("expires", 0) > time.time():
                    raise Problem(
                        "Another backup is in progress.",
                        503 if lease.get("operationId") == operation_id else 409, "backup_in_progress",
                    )
            for repo in (self.repo.sales, self.repo.products):
                transaction.update(repo.workspace, {"backupLease": {
                    "owner": owner, "operationId": operation_id, "expires": time.time() + 600,
                }})
            value = prior or {"actor": actor, "source": source, "completed": False}
            if prior is None:
                transaction.create(operation, value)
            return value, owner

        prior, lease_owner = claim(self.repo.client.transaction())
        return operation, prior, lease_owner

    def _finish(self, operation, owner, row=None, error=None):
        @firestore.transactional
        def finish(transaction):
            controls = [repo.workspace.get(transaction=transaction).to_dict()
                        for repo in (self.repo.sales, self.repo.products)]
            if any((control.get("backupLease") or {}).get("owner") != owner for control in controls):
                raise Problem(
                    "Backup ownership changed. Retry the same operation.", 409, "backup_in_progress",
                )
            for repo in (self.repo.sales, self.repo.products):
                transaction.update(repo.workspace, {"backupLease": None, "lastBackupError": error})
            if row:
                transaction.update(operation, {"completed": True, "backupId": row["id"]})

        finish(self.repo.client.transaction())

    def inspect(self, data, actor, row=None):
        self.require_admin(actor)
        _, manifest = self.read_archive(data)
        self.repo.sync()
        sales, products = self.repo.control, self.repo.products.control
        if sales.get("maintenance") or products.get("maintenance"):
            raise Problem("Workspace maintenance is in progress.", 503, "maintenance")
        value = {
            "hash": hashlib.sha256(data).hexdigest(), "actor": actor,
            "generation": sales["generation"], "sequence": sales["sequence"],
            "manufacturingGeneration": products["generation"], "manufacturingSequence": products["sequence"],
            "expires": time.time() + 600,
        }
        return {
            "token": self._token(value), "backup": row or self.backup_row("uploaded-backup", data, manifest),
            "generation": sales["generation"],
            "expiresAt": datetime.fromtimestamp(value["expires"], timezone.utc).isoformat(),
            "warnings": [
                "This replaces BOTH SalesTrackr and ProductTrackr data and history. It is not a merge.",
                "Both current workspaces are backed up and restored datasets verified before activation.",
                "Both workspaces switch together; all old forms must be reloaded.",
                "Drive drawings and files are not inside the ZIP and are never deleted by recovery.",
            ],
        }

    def restore(self, data, actor, token, confirmation, write_id):
        self.require_admin(actor)
        identifier(write_id)
        if confirmation != "RESTORE":
            raise Problem("Type RESTORE to recover both workspaces.", 422, "restore_confirmation")
        values, manifest = self.read_archive(data)
        archive_hash = hashlib.sha256(data).hexdigest()
        operation = self.repo.workspace.collection("restoreOperations").document(write_id)
        prior = operation.get().to_dict()
        if prior and (prior["actor"] != actor or prior["archiveHash"] != archive_hash):
            raise Problem("Restore identifier was used for different content.", 409, "write_id_reused")
        if prior and prior.get("completed"):
            return prior["result"]
        preview = prior or self._read_token(token, actor, data)
        owner = uuid4().hex
        repos = (self.repo.sales, self.repo.products)

        @firestore.transactional
        def begin(transaction):
            controls = [repo.workspace.get(transaction=transaction).to_dict() for repo in repos]
            existing = operation.get(transaction=transaction).to_dict()
            if existing and (existing["actor"] != actor or existing["archiveHash"] != archive_hash):
                raise Problem("Restore identifier was reused.", 409, "write_id_reused")
            if existing and existing.get("completed"):
                return existing
            for prefix, control in zip(("", "manufacturing"), controls):
                keys = ("generation", "sequence") if not prefix else (
                    "manufacturingGeneration", "manufacturingSequence",
                )
                if any(
                    control[key] != preview[value] for key, value in zip(("generation", "sequence"), keys)
                ):
                    raise Problem("A workspace changed. Inspect the backup again.", 409, "preview_stale")
                if control.get("pendingRecordCleanup"):
                    raise Problem("Finish record cleanup before recovery.", 409, "cleanup_pending")
                maintenance = control.get("maintenance")
                if maintenance and maintenance.get("expires", float("inf")) > time.time():
                    raise Problem(
                        "Another recovery is in progress.",
                        503 if maintenance.get("writeId") == write_id else 409, "restore_in_progress",
                    )
                if (control.get("backupLease") or {}).get("expires", 0) > time.time():
                    raise Problem("A backup is in progress.", 409, "backup_in_progress")
            value = existing or {**preview, "actor": actor, "archiveHash": archive_hash, "completed": False}
            transaction.set(operation, value)
            for repo in repos:
                transaction.update(repo.workspace, {"maintenance": {
                    "kind": "shared-restore", "writeId": write_id, "owner": owner,
                    "startedAt": now(), "expires": time.time() + 600,
                }})
            return value

        state = begin(self.repo.client.transaction())
        if state.get("completed"):
            return state["result"]
        try:
            recovery = self.backup(
                f"pre-restore-{digest(write_id)}", actor, source="pre-restore",
                prune=False, restore_write_id=write_id,
            )
            sales_target, sales_stats = self.repo.sales.stage(values["sales"])
            product_target, product_stats = self.repo.products.stage(values["manufacturing"])

            @firestore.transactional
            def activate(transaction):
                controls = [repo.workspace.get(transaction=transaction).to_dict() for repo in repos]
                existing = operation.get(transaction=transaction).to_dict()
                if existing.get("completed"):
                    return existing["result"]
                for control, keys in zip(controls, (
                    ("generation", "sequence"), ("manufacturingGeneration", "manufacturingSequence"),
                )):
                    if (
                        any(control[key] != state[value] for key, value
                            in zip(("generation", "sequence"), keys))
                        or (control.get("maintenance") or {}).get("owner") != owner
                    ):
                        raise Problem("Recovery ownership changed. No datasets were activated.",
                                      409, "preview_stale")
                for repo, control, target, stats in zip(
                    repos, controls, (sales_target, product_target), (sales_stats, product_stats),
                ):
                    old = repo.workspace.collection("datasets").document(control["activeDataset"])
                    transaction.set(old, {
                        "state": "archived", "archivedAt": now(), "sharedBackupId": recovery["id"],
                        **({"namingVersion": control["namingVersion"]} if "namingVersion" in control else {}),
                    }, merge=True)
                    transaction.set(target, {"state": "active", "activatedAt": now()}, merge=True)
                    transaction.set(repo.workspace, {
                        **control, **stats, "activeDataset": target.id, "generation": target.id,
                        "sequence": control["sequence"] + 1, "maintenance": None,
                    })
                result = {
                    "restored": True, "generation": sales_target.id,
                    "manufacturingGeneration": product_target.id,
                    "counts": manifest["counts"], "recoveryBackup": recovery,
                }
                transaction.update(operation, {"completed": True, "result": result})
                return result

            result = activate(self.repo.client.transaction())
            self.repo.sync()
        except Exception:
            @firestore.transactional
            def release(transaction):
                controls = [repo.workspace.get(transaction=transaction).to_dict() for repo in repos]
                existing = operation.get(transaction=transaction).to_dict()
                if existing and existing.get("completed"):
                    return existing["result"]
                for repo, control in zip(repos, controls):
                    if (control.get("maintenance") or {}).get("owner") == owner:
                        transaction.update(repo.workspace, {"maintenance": None})
                return None

            confirmed = release(self.repo.client.transaction())
            if confirmed:
                return confirmed
            raise
        try:
            self._prune()
        except Exception:
            self.repo.workspace.update({
                "lastBackupError": "Recovery succeeded; backup rotation needs retrying.",
            })
        return result


class ManufacturingRecoveryView:
    """Manufacturing cleanup reads its slice of a combined safety backup."""

    read_archive = staticmethod(read_product_archive)

    def __init__(self, shared):
        self.shared = shared
        self.admin, self.require_admin = shared.admin, shared.require_admin
        self.backup = shared.backup

    def get(self, file_id):
        content, row = self.shared.get(file_id)
        with zipfile.ZipFile(io.BytesIO(content)) as source:
            return source.read("producttrackr.zip"), row
