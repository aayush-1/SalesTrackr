"""Complete, isolated ProductTrackr archives and verified recovery."""

import hashlib
import io
import json
import re
import time
import zipfile
import zlib
from copy import deepcopy
from datetime import date, datetime, timezone
from uuid import uuid4

from google.cloud import firestore
from pydantic import TypeAdapter, ValidationError

from .backups import (
    BACKUP_SOURCES,
    MAX_ARCHIVE,
    MAX_EXPANDED,
    BackupManager,
    DriveBackupStore,
    LocalBackupStore,
    _json,
    identifier,
)
from .domain import Identifier, Name, Problem, Text, now, today
from .manufacturing_compat import LEGACY_COLLECTIONS, LEGACY_FILES, canonical_counts, canonical_data
from .manufacturing_domain import MODELS, Assignment, Step
from .manufacturing_drive import child_folder, relocate, require_private
from .manufacturing_records import ManufacturingRecords
from .storage import DriveBackend, digest, encode

FILES = {
    "products": "products.json",
    "operations": "operations.json",
    "suppliers": "suppliers.json",
    "contacts": "contacts.json",
    "steps": "product-steps.json",
    "assignments": "supplier-links.json",
    "manufacturingRecords": "manufacturing-records.json",
    "manufacturingRecordEvents": "activity-history.json",
    "sequences": "number-counters.json",
    "writes": "write-receipts.json",
    "inventorySources": "inventory-sources.json",
    "stockMovements": "stock-movements.json",
}
RESERVED = {
    "datasets", "backups", "backupOperations", "restoreOperations", "cleanupOperations", "productFolders",
}
HASH = re.compile(r"^[a-f0-9]{64}$")
NUMBER = re.compile(r"^MFG-[0-9]{5,}$")
PRODUCT_FOLDERS_FILE = "product-folders.json"


def validate_value(schema, value):
    return TypeAdapter(schema).validate_python(value)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("A timestamp must include a timezone.")
    return parsed


def actual_date(value):
    parsed = date.fromisoformat(value)
    if parsed > today() or parsed.isoformat() != value:
        raise ValueError("Invalid actual work date.")
    return value


def entity_metadata(key, row):
    if row["id"] != key or type(row["version"]) is not int or row["version"] < 1:
        raise ValueError("Invalid entity metadata.")
    timestamp(row["createdAt"])
    timestamp(row["updatedAt"])
    validate_value(Name, row["updatedBy"])


def assignment_snapshot(value):
    validate_value(Identifier, value["supplierId"])
    validate_value(Name, value["supplierName"])
    for field in ("email", "phone"):
        validate_value(Text, value[field])
    if value["type"] not in ("company", "individual") or value["relationship"] not in (
        "external",
        "internal",
    ):
        raise ValueError("Invalid actual supplier.")
    contact = value["contact"]
    if contact is not None:
        if value["type"] != "company":
            raise ValueError("Individual suppliers cannot have company contacts.")
        validate_value(Identifier, contact["id"])
        validate_value(Name, contact["name"])
        for field in ("designation", "email", "phone"):
            validate_value(Text, contact[field])


def checked_record(record, collections):
    if len(json.dumps(record).encode()) > 900_000:
        raise ValueError("Manufacturing record exceeds its storage limit.")
    validate_value(Identifier, record["productId"])
    if record["productId"] not in collections["products"] or not NUMBER.fullmatch(record["number"]):
        raise ValueError("Invalid manufacturing product or number.")
    validate_value(Name, record["productName"])
    validate_value(Text, record["productPartNumber"])
    validate_value(Text, record["notes"])
    if record.get("quantity") is not None:
        from .domain import PartQuantity

        validate_value(PartQuantity, record["quantity"])
    steps = record["operations"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ValueError("Invalid operation route.")
    completed = sum(step["status"] == "completed" for step in steps)
    status = record["status"]
    if status == "completed":
        if completed != len(steps) or record["currentIndex"] is not None:
            raise ValueError("Invalid completed record.")
    elif status not in ("ready", "in_progress", "cancelled") or (
        type(record["currentIndex"]) is not int
        or record["currentIndex"] != completed
        or completed >= len(steps)
    ):
        raise ValueError("Invalid current operation.")
    ids, previous_completion = set(), None
    for index, step in enumerate(steps):
        validate_value(Identifier, step["id"])
        validate_value(Identifier, step["sourceStepId"])
        validate_value(Identifier, step["operationId"])
        validate_value(Name, step["name"])
        for field in ("instructions", "assignmentNote", "completionNote"):
            validate_value(Text, step[field])
        if step["id"] in ids or step["operationId"] not in collections["operations"]:
            raise ValueError("Invalid copied operation.")
        ids.add(step["id"])
        expected = "completed" if index < completed else status if index == completed else "pending"
        if step["status"] != expected:
            raise ValueError("Non-sequential operation states.")
        owner = step["assignment"]
        if owner is None:
            if step["status"] in ("completed", "in_progress") or any(
                step[field] is not None for field in ("startedOn", "assignedOn", "completedOn")
            ):
                raise ValueError("Missing actual assignment.")
        else:
            if step["status"] in ("ready", "pending"):
                raise ValueError("A future operation is already assigned.")
            assignment_snapshot(owner)
            started, assigned = actual_date(step["startedOn"]), actual_date(step["assignedOn"])
            if assigned < started or previous_completion and started < previous_completion:
                raise ValueError("Assignment dates are out of order.")
            if step["status"] == "completed":
                completion = actual_date(step["completedOn"])
                if completion < assigned:
                    raise ValueError("Completion predates assignment.")
                previous_completion = completion
            elif step["completedOn"] is not None:
                raise ValueError("An unfinished operation has a completion date.")
    if record["startedOn"] != steps[0]["startedOn"]:
        raise ValueError("The record start date does not match its first operation.")
    if record["completedOn"] != (steps[-1]["completedOn"] if status == "completed" else None):
        raise ValueError("The record completion date is inconsistent.")
    if status == "cancelled":
        validate_value(Name, record["cancelReason"][:200])
        validate_value(Text, record["cancelReason"])
        timestamp(record["cancelledAt"])
    elif record["cancelledAt"] is not None or record["cancelReason"]:
        raise ValueError("An active record contains cancellation details.")
    projected = ManufacturingRecords.project(deepcopy(record))
    if any(
        projected[key] != record.get(key)
        for key in (
            "active",
            "stepCount",
            "completedCount",
            "currentOperationId",
            "currentOperationName",
            "currentSupplierId",
            "currentSupplierName",
            "currentContactName",
            "searchText",
            "searchChars",
        )
    ):
        raise ValueError("The stored work-in-process projections are inconsistent.")


def checked_data(data, *, directories_only=False):
    try:
        if canonical_data(data) != data:
            raise ValueError("Snapshots must use current manufacturing names.")
        if (
            set(data) - {"productFolders"} != {*FILES, "settings"}
            or data["settings"]
            != {
                "weeklyEnabled": data["settings"]["weeklyEnabled"],
            }
            or type(data["settings"]["weeklyEnabled"]) is not bool
        ):
            raise ValueError("Missing collections or invalid workspace settings.")
        for kind in FILES:
            if not isinstance(data[kind], dict):
                raise ValueError("Invalid collection.")
            for key, row in data[kind].items():
                identifier(key)
                if not isinstance(row, dict):
                    raise ValueError("Invalid document.")
                if kind not in ("sequences", "writes", "manufacturingRecordEvents",
                                "inventorySources", "stockMovements"):
                    entity_metadata(key, row)
        if "productFolders" in data:
            folders = data["productFolders"]
            if (
                set(folders) != {"identity", "rootId", "folders"}
                or set(folders["identity"]) != {"project", "database", "workspace"}
                or not isinstance(folders["folders"], dict)
                or set(folders["folders"]) - set(data["products"])
            ):
                raise ValueError("Invalid product folder mappings.")
            for value in folders["identity"].values():
                validate_value(Name, value)
            if folders["rootId"]:
                identifier(folders["rootId"])
            elif folders["rootId"] != "" or folders["folders"]:
                raise ValueError("Product folders require their original Drive root.")
            ids = set()
            for key, mapping in folders["folders"].items():
                identifier(key)
                if set(mapping) != {"id", "parentId", "url"} or mapping["id"] in ids:
                    raise ValueError("Invalid or duplicate product folder.")
                identifier(mapping["id"])
                identifier(mapping["parentId"])
                if mapping["url"] != f"https://drive.google.com/drive/folders/{mapping['id']}":
                    raise ValueError("Invalid product folder URL.")
                ids.add(mapping["id"])
        for kind, model in MODELS.items():
            for row in data[kind].values():
                required = set(model.model_fields) - {"modelIds", "stockQuantity", "minimumStockQuantity"}
                if not required.issubset(row):
                    raise ValueError("Incomplete master entity.")
                model.model_validate({key: row[key] for key in model.model_fields if key in row})
                if row["nameKey"] != row["name"].casefold():
                    raise ValueError("Invalid name index.")
        for product in data["products"].values():
            if (
                not isinstance(product["stepIds"], list)
                or len(set(product["stepIds"])) != len(product["stepIds"])
                or product["searchChars"]
                != sorted(set(f"{product['name']} {product['partNumber']}".casefold()))
            ):
                raise ValueError("Invalid Product process.")
            for key in product["stepIds"]:
                if data["steps"][key]["productId"] != product["id"]:
                    raise ValueError("Invalid Product operation ownership.")
        for contact in data["contacts"].values():
            if data["suppliers"][contact["supplierId"]]["type"] != "company":
                raise ValueError("Invalid company contact.")
        links = {}
        for row in data["assignments"].values():
            Assignment.model_validate({key: row[key] for key in Assignment.model_fields})
            source = data["steps"][row["stepId"]]
            supplier = data["suppliers"][row["supplierId"]]
            if source["productId"] != row["productId"]:
                raise ValueError("Invalid supplier link ownership.")
            if row["contactId"] and (
                supplier["type"] != "company"
                or data["contacts"][row["contactId"]]["supplierId"] != supplier["id"]
            ):
                raise ValueError("Invalid linked company contact.")
            group = links.setdefault(row["stepId"], {})
            if supplier["id"] in group:
                raise ValueError("Duplicate supplier link.")
            group[supplier["id"]] = row["id"]
        for row in data["steps"].values():
            Step.model_validate({key: row[key] for key in Step.model_fields})
            if (
                row["id"] not in data["products"][row["productId"]]["stepIds"]
                or row["operationId"] not in data["operations"]
                or row["primaryAssignmentId"] not in (set(links.get(row["id"], {}).values()) or {None})
            ):
                raise ValueError("Invalid operation or primary supplier.")
        numbers, events = set(), {}
        from .inventory import checked_inventory

        if not directories_only:
            checked_inventory(data)
        for key, record in data["manufacturingRecords"].items():
            checked_record(record, data)
            if record["number"] in numbers:
                raise ValueError("Duplicate manufacturing number.")
            numbers.add(record["number"])
            events[key] = set()
        for key, event in data["manufacturingRecordEvents"].items():
            record = data["manufacturingRecords"][event["recordId"]]
            if (
                event["id"] != key
                or type(event["sequence"]) is not int
                or not 1 <= event["sequence"] <= record["version"]
                or event["sequence"] in events[event["recordId"]]
                or event["action"] not in (
                    "create", "assign", "reassign", "complete", "undo", "cancel", "quantity",
                )
                or (event["sequence"] == 1) != (event["action"] == "create")
            ):
                raise ValueError("Invalid activity history.")
            if event["action"] not in ("create", "cancel", "quantity") and event["stepId"] not in {
                step["id"] for step in record["operations"]
            }:
                raise ValueError("History references an unknown operation.")
            validate_value(Name, event["actor"])
            timestamp(event["recordedAt"])
            for field in ("assignment", "previousAssignment"):
                if field in event:
                    assignment_snapshot(event[field])
            for field in ("occurredOn", "previousCompletedOn"):
                if field in event:
                    actual_date(event[field])
            for field in ("note", "reason"):
                if field in event:
                    validate_value(Text, event[field])
            if event["action"] in ("reassign", "undo", "cancel", "quantity") and not event.get(
                "reason", "",
            ).strip():
                raise ValueError("Missing correction reason.")
            if event["action"] == "quantity":
                from .domain import PartQuantity

                validate_value(PartQuantity, event["quantity"])
                if event.get("previousQuantity") is not None:
                    validate_value(PartQuantity, event["previousQuantity"])
            events[event["recordId"]].add(event["sequence"])
        for key, record in data["manufacturingRecords"].items():
            if len(events[key]) != record["version"]:
                raise ValueError("Incomplete activity history.")
        if set(data["sequences"]) - {"manufacturing-records"}:
            raise ValueError("Unknown number counter.")
        counter = data["sequences"].get("manufacturing-records", {"value": 0})
        if (
            set(counter) != {"value"}
            or type(counter["value"]) is not int
            or counter["value"] < max((int(number[4:]) for number in numbers), default=0)
        ):
            raise ValueError("The manufacturing number counter is incomplete.")
        for receipt in data["writes"].values():
            if not HASH.fullmatch(receipt["fingerprint"]):
                raise ValueError("Invalid write receipt.")
            validate_value(Identifier, receipt["result"]["id"])
            validate_value(Identifier, receipt["result"]["generation"])
            validate_value(Identifier, receipt["result"]["workspaceId"])
            timestamp(receipt["createdAt"])
        return {kind: len(data[kind]) for kind in FILES}
    except (
        Problem,
        ValidationError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        IndexError,
        OverflowError,
    ):
        raise Problem(
            "This is not a complete, consistent ProductTrackr snapshot. No live data was changed.",
            422,
            "invalid_backup",
        ) from None


def archive(data, *, source, operation_id, actor, control):
    counts = checked_data(data)
    names = {**FILES, "settings": "workspace-settings.json"}
    if "productFolders" in data:
        names["productFolders"] = PRODUCT_FOLDERS_FILE
    files = {
        name: json.dumps(data[kind], ensure_ascii=False, indent=2).encode()
        for kind, name in names.items()
    }
    manifest = {
        "format": "producttrackr-backup",
        "version": 3,
        "createdAt": now(),
        "source": source,
        "operationId": operation_id,
        "actor": actor,
        "generation": control["generation"],
        "sequence": control["sequence"],
        "counts": counts,
        "dataHash": digest(data),
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    manifest["checksum"] = digest(manifest)
    manifest_bytes = encode(manifest)
    if sum(len(value) for value in files.values()) + len(manifest_bytes) > MAX_EXPANDED:
        raise Problem("The backup exceeds the 256 MiB expanded-data limit.", 413, "backup_size")
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("manifest.json", manifest_bytes)
        for name, content in files.items():
            output.writestr(name, content)
    return target.getvalue(), manifest


def read_archive(content):
    if len(content) > MAX_ARCHIVE:
        raise Problem("Backups must be at most 64 MiB.", 413, "backup_size")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive_file:
            entries = archive_file.infolist()
            if (
                sum(entry.file_size for entry in entries) > MAX_EXPANDED
                or any(
                    entry.flag_bits & 1
                    or entry.is_dir()
                    or entry.file_size < 0
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                    for entry in entries
                )
            ):
                raise ValueError("Unsafe or incomplete archive.")
            manifest = _json(archive_file.read("manifest.json"))
            version = manifest["version"]
            if type(version) is not int or version not in (1, 2, 3):
                raise ValueError("Unsupported archive version.")
            names = {
                **FILES, **(LEGACY_FILES if version < 3 else {}),
                "settings": "workspace-settings.json",
            }
            if PRODUCT_FOLDERS_FILE in archive_file.namelist():
                names["productFolders"] = PRODUCT_FOLDERS_FILE
            expected = {"manifest.json", *names.values()}
            if (
                len(entries) != len(expected)
                or {entry.filename for entry in entries} != expected
                or manifest["format"] != "producttrackr-backup"
                or version < 3 and version != (2 if "productFolders" in names else 1)
                or manifest["source"] not in BACKUP_SOURCES
                or manifest["checksum"] != digest({k: v for k, v in manifest.items() if k != "checksum"})
                or set(manifest["files"]) != expected - {"manifest.json"}
            ):
                raise ValueError("Invalid manifest.")
            timestamp(manifest["createdAt"])
            data = {}
            for kind, name in names.items():
                raw = archive_file.read(name)
                if hashlib.sha256(raw).hexdigest() != manifest["files"][name]:
                    raise ValueError("File checksum mismatch.")
                data[kind] = _json(raw)
            if version < 3:
                data = {
                    LEGACY_COLLECTIONS.get(kind, kind): rows
                    for kind, rows in data.items()
                }
            raw_hash = digest(data)
            if version < 3:
                # Legacy checksums cover legacy keys, before conversion to current entity names.
                data = canonical_data(data)
            counts = canonical_counts(manifest["counts"]) if version < 3 else manifest["counts"]
            if checked_data(data) != counts or raw_hash != manifest["dataHash"]:
                raise ValueError("Snapshot integrity mismatch.")
            return data, manifest
    except (
        Problem,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        RuntimeError,
        RecursionError,
        OverflowError,
    ):
        raise Problem(
            "Select a complete, supported ProductTrackr backup, not a SalesTrackr archive. "
            "No live data was changed.",
            422,
            "invalid_backup",
        ) from None


def backup_row(file_id, data, manifest, *, name=None):
    return {
        "id": file_id,
        "name": name or f"ProductTrackr-{manifest['source']}-{manifest['createdAt'][:10]}.zip",
        "createdAt": manifest["createdAt"],
        "source": manifest["source"],
        "protected": False,
        "counts": canonical_counts(manifest["counts"]),
        "sizeBytes": len(data),
        "dataHash": manifest["dataHash"],
    }


class ManufacturingDriveBackupStore(DriveBackupStore):
    app = "producttrackr-backup"

    def __init__(self, settings):
        self.settings = settings
        self.folder_id = settings.root_id
        token = settings.token_json
        if not self.folder_id or not token:
            raise Problem(
                "Configure the SalesTrackr root and reconnect Google Drive.", 503, "backup_folder"
            )
        self.backend = DriveBackend(token, self.folder_id, operator=True)
        self.folder_url = None

    def _root_folder(self):
        folder = identifier(self.folder_id)
        metadata = self.backend._execute(
            self.backend._api()
            .files()
            .get(
                fileId=folder,
                fields="id,name,parents,mimeType,trashed,appProperties",
            )
        )
        if (
            metadata.get("trashed")
            or metadata.get("mimeType") != "application/vnd.google-apps.folder"
            or metadata.get("appProperties", {}).get("app") == "salestrackr-firestore-backups"
        ):
            raise Problem(
                "Use the shared SalesTrackr root, not its Backups folder.",
                503,
                "backup_folder",
            )
        require_private(self.backend, folder)
        return folder

    def _folder(self):
        folder = DriveBackupStore(self.settings)._folder()
        self.folder_url = f"https://drive.google.com/drive/folders/{folder}"
        return folder

    def products_folder(self):
        return child_folder(self.backend, self._root_folder(), "products")

    def inventory(self):
        folder = self._folder()
        for item in self.backend._list(
            f"'{self.folder_id}' in parents and trashed = false and mimeType = 'application/zip' "
            "and appProperties has { key='app' and value='producttrackr-backup' }", fields="id",
        ):
            relocate(
                self.backend, item["id"], self.folder_id, folder, "application/zip", {"app": self.app},
            )
        return [
            {
                "id": item["id"],
                "name": item["name"],
                "sizeBytes": int(item.get("size", 0)),
                "checksum": item.get("md5Checksum"),
                "createdAt": item["createdTime"],
            }
            for item in self.backend._list(
                f"'{folder}' in parents and trashed = false and mimeType = 'application/zip' "
                "and appProperties has { key='app' and value='producttrackr-backup' }",
                fields="id,name,size,md5Checksum,createdTime",
            )
        ]

    def _metadata(self, file_id):
        folder = self._folder()
        try:
            metadata = self.backend._execute(self.backend._api().files().get(
                fileId=identifier(file_id), fields="id,parents,trashed",
            ))
        except Problem as error:
            if error.code == "drive_missing":
                return None
            raise
        if not metadata.get("trashed") and metadata.get("parents") == [self.folder_id]:
            relocate(
                self.backend, file_id, self.folder_id, folder, "application/zip", {"app": self.app},
            )
        metadata = super()._metadata(file_id)
        if metadata and metadata.get("appProperties", {}).get("app") != self.app:
            raise Problem("This file is not a managed ProductTrackr backup.", 409, "backup_changed")
        return metadata


class ManufacturingBackupManager(BackupManager):
    archive = staticmethod(archive)
    read_archive = staticmethod(read_archive)
    backup_row = staticmethod(backup_row)

    def __init__(self, repository, settings, store=None):
        if store is None:
            store = (
                ManufacturingDriveBackupStore(settings)
                if settings.manufacturing_backup_storage == "drive"
                else LocalBackupStore(
                    settings.data_dir
                    / "producttrackr-backups"
                    / digest(settings.manufacturing_identity(repository.workspace.id))[:24]
                )
            )
        super().__init__(repository, settings, store)
        self.signing_key = repository.signing_key

    def admin(self, actor):
        return (
            self.settings.mode == "demo"
            or actor.casefold() in (self.settings.manufacturing_admins or self.settings.recovery_admins)
        )

    def check_backup_control(self, control):
        if control.get("pendingRecordCleanup"):
            raise Problem(
                "Resume the pending ProductTrackr record cleanup before creating backups.",
                503, "cleanup_pending",
            )

    def status(self, actor):
        status = super().status(actor)
        local = isinstance(self.store, LocalBackupStore)
        return {
            **status,
            **self.repo.meta(),
            "provider": "local" if local else "drive",
            "schedule": {**status["schedule"], "active": False},
            "location": "Local simulated ProductTrackr backup folder"
            if local
            else "SalesTrackr / Backups in private Google Drive",
            "configuredFolderUrl": (
                f"https://drive.google.com/drive/folders/{self.settings.root_id}"
                if self.settings.root_id
                else None
            ),
        }

    def inspect(self, data, actor, row=None):
        self.require_admin(actor)
        _, manifest = self.read_archive(data)
        self.repo.sync()
        control = self.repo.control
        if control.get("maintenance"):
            raise Problem("ProductTrackr recovery is in progress.", 503, "maintenance")
        value = {
            "hash": hashlib.sha256(data).hexdigest(),
            "actor": actor,
            "generation": control["generation"],
            "sequence": control["sequence"],
            "expires": time.time() + 600,
        }
        return {
            "token": self._token(value),
            "backup": row or self.backup_row("uploaded-backup", data, manifest),
            "generation": control["generation"],
            "expiresAt": datetime.fromtimestamp(value["expires"], timezone.utc).isoformat(),
            "warnings": [
                "This replaces all ProductTrackr products, operations, suppliers, contacts, process links, "
                "manufacturing records and activity history; it is not a merge.",
                "Current ProductTrackr data is backed up and verified before "
                "the restored dataset is activated.",
                "SalesTrackr data, settings and backups are not changed.",
                "Saved product Drive links reconnect only in their original workspace and Drive root. "
                "Drawings and files stay in Drive and are not restored from the ZIP.",
                "All open ProductTrackr forms must be reloaded after recovery.",
            ],
        }

    def archived_metadata(self, recovery):
        return {
            "backupId": recovery["id"], "backupDataHash": recovery["dataHash"],
            "namingVersion": self.repo.control.get("namingVersion", 0),
        }

    def restore(self, *args, **kwargs):
        result = super().restore(*args, **kwargs)
        self.finish_cleanup()
        return result

    def completed_restore(self, *args, **kwargs):
        result = super().completed_restore(*args, **kwargs)
        if result:
            self.finish_cleanup()
        return result

    def completed_upload_restore(self, data, actor, confirmation, write_id):
        self.require_admin(actor)
        identifier(write_id)
        prior = self.repo.workspace.collection("restoreOperations").document(write_id).get().to_dict()
        if prior and prior.get("completed"):
            return self.restore(data, actor, "", confirmation, write_id)
        return None

    def finish_cleanup(self):
        try:
            self.cleanup_archived()
        except Exception:
            self.repo.workspace.update(
                {
                    "lastBackupError": "Restore succeeded. An archived manufacturing dataset was retained "
                    "because cleanup could not be verified.",
                }
            )

    def cleanup_archived(self):
        owner = uuid4().hex

        @firestore.transactional
        def claim(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            if (
                control.get("maintenance")
                or control.get("pendingRecordCleanup")
                or (control.get("backupLease") or {}).get("expires", 0) > time.time()
            ):
                return False
            transaction.update(
                self.repo.workspace,
                {
                    "maintenance": {
                        "kind": "archive-cleanup",
                        "owner": owner,
                        "writeId": owner,
                        "expires": time.time() + 600,
                    }
                },
            )
            return True

        if not claim(self.repo.client.transaction()):
            return
        try:
            for snapshot in self.repo.workspace.collection("datasets").stream():
                descriptor = snapshot.to_dict()
                if descriptor.get("state") != "archived" or not descriptor.get("backupId"):
                    continue
                data, _ = self.get(descriptor["backupId"])
                saved, manifest = self.read_archive(data)
                if manifest["dataHash"] != descriptor["backupDataHash"]:
                    continue
                target = self.repo.workspace if snapshot.id == "legacy" else snapshot.reference
                if self.repo.export_at(target, saved["settings"]) != {
                    key: value for key, value in saved.items() if key != "productFolders"
                }:
                    continue
                if target.path != self.repo.workspace.path:
                    folder_context = descriptor.get("productFolders")
                    if folder_context is not None:
                        backed_up = saved.get("productFolders") or {}
                        if folder_context != {
                            key: value for key, value in backed_up.items() if key != "folders"
                        } or any(
                            backed_up.get("folders", {}).get(row.id) != row.to_dict()
                            for row in target.collection("productFolders").stream()
                        ):
                            continue
                references = [
                    document.reference
                    for kind in FILES
                    for document in target.collection(
                        kind if descriptor.get("namingVersion") == 1
                        else LEGACY_COLLECTIONS.get(kind, kind)
                    ).select([]).stream()
                ]
                if target.path != self.repo.workspace.path:
                    references.extend(
                        document.reference
                        for document in target.collection("productFolders").select([]).stream()
                    )
                for start in range(0, len(references) + 1, 100):

                    @firestore.transactional
                    def remove(transaction):
                        control = self.repo.workspace.get(transaction=transaction).to_dict()
                        archived = snapshot.reference.get(transaction=transaction).to_dict()
                        if (
                            control["activeDataset"] == snapshot.id
                            or (control.get("maintenance") or {}).get("owner") != owner
                            or archived != descriptor
                        ):
                            raise Problem("Archived data changed; cleanup stopped.", 409, "cleanup_changed")
                        for reference in references[start : start + 100]:
                            transaction.delete(reference)
                        if start + 100 >= len(references):
                            transaction.update(snapshot.reference, {"state": "deleted", "deletedAt": now()})

                    remove(self.repo.client.transaction())
                    if start + 100 >= len(references):
                        break
        finally:

            @firestore.transactional
            def release(transaction):
                control = self.repo.workspace.get(transaction=transaction).to_dict()
                if (control.get("maintenance") or {}).get("owner") == owner:
                    transaction.update(self.repo.workspace, {"maintenance": None})

            release(self.repo.client.transaction())
