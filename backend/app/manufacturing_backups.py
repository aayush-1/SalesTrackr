"""Complete, isolated BuildTrackr archives and verified recovery."""

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
from .manufacturing_domain import MODELS, Assignment, Step
from .manufacturing_records import ManufacturingRecords
from .storage import DriveBackend, digest, encode

FILES = {
    "products": "products.json",
    "operations": "operations.json",
    "partners": "partners.json",
    "contacts": "contacts.json",
    "steps": "product-steps.json",
    "assignments": "partner-links.json",
    "manufacturingRecords": "manufacturing-records.json",
    "manufacturingRecordEvents": "activity-history.json",
    "sequences": "number-counters.json",
    "writes": "write-receipts.json",
}
RESERVED = {"datasets", "backups", "backupOperations", "restoreOperations", "cleanupOperations"}
HASH = re.compile(r"^[a-f0-9]{64}$")
NUMBER = re.compile(r"^MFG-[0-9]{5,}$")


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
    validate_value(Identifier, value["partnerId"])
    validate_value(Name, value["partnerName"])
    for field in ("email", "phone"):
        validate_value(Text, value[field])
    if value["type"] not in ("company", "individual") or value["relationship"] not in (
        "external",
        "internal",
    ):
        raise ValueError("Invalid actual partner.")
    contact = value["contact"]
    if contact is not None:
        if value["type"] != "company":
            raise ValueError("Individual partners cannot have company contacts.")
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
    validate_value(Text, record["productReference"])
    validate_value(Text, record["notes"])
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
            "currentPartnerId",
            "currentPartnerName",
            "currentContactName",
            "searchText",
            "searchChars",
        )
    ):
        raise ValueError("The stored work-in-process projections are inconsistent.")


def checked_data(data):
    try:
        if (
            set(data) != {*FILES, "settings"}
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
                if kind not in ("sequences", "writes", "manufacturingRecordEvents"):
                    entity_metadata(key, row)
        for kind, model in MODELS.items():
            for row in data[kind].values():
                if not set(model.model_fields).issubset(row):
                    raise ValueError("Incomplete master entity.")
                model.model_validate({key: row[key] for key in model.model_fields})
                if row["nameKey"] != row["name"].casefold():
                    raise ValueError("Invalid name index.")
        for product in data["products"].values():
            if (
                not isinstance(product["stepIds"], list)
                or len(set(product["stepIds"])) != len(product["stepIds"])
                or product["searchChars"]
                != sorted(set(f"{product['name']} {product['reference']}".casefold()))
            ):
                raise ValueError("Invalid Product process.")
            for key in product["stepIds"]:
                if data["steps"][key]["productId"] != product["id"]:
                    raise ValueError("Invalid Product operation ownership.")
        for contact in data["contacts"].values():
            if data["partners"][contact["partnerId"]]["type"] != "company":
                raise ValueError("Invalid company contact.")
        links = {}
        for row in data["assignments"].values():
            Assignment.model_validate({key: row[key] for key in Assignment.model_fields})
            source = data["steps"][row["stepId"]]
            partner = data["partners"][row["partnerId"]]
            if source["productId"] != row["productId"]:
                raise ValueError("Invalid partner link ownership.")
            if row["contactId"] and (
                partner["type"] != "company"
                or data["contacts"][row["contactId"]]["partnerId"] != partner["id"]
            ):
                raise ValueError("Invalid linked company contact.")
            group = links.setdefault(row["stepId"], {})
            if partner["id"] in group:
                raise ValueError("Duplicate partner link.")
            group[partner["id"]] = row["id"]
        for row in data["steps"].values():
            Step.model_validate({key: row[key] for key in Step.model_fields})
            if (
                row["id"] not in data["products"][row["productId"]]["stepIds"]
                or row["operationId"] not in data["operations"]
                or row["primaryAssignmentId"] not in (set(links.get(row["id"], {}).values()) or {None})
            ):
                raise ValueError("Invalid operation or primary partner.")
        numbers, events = set(), {}
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
                or event["action"] not in ("create", "assign", "reassign", "complete", "undo", "cancel")
                or (event["sequence"] == 1) != (event["action"] == "create")
            ):
                raise ValueError("Invalid activity history.")
            if event["action"] not in ("create", "cancel") and event["stepId"] not in {
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
            if event["action"] in ("reassign", "undo", "cancel") and not event.get("reason", "").strip():
                raise ValueError("Missing correction reason.")
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
            "This is not a complete, consistent BuildTrackr snapshot. No live data was changed.",
            422,
            "invalid_backup",
        ) from None


def archive(data, *, source, operation_id, actor, control):
    counts = checked_data(data)
    files = {
        name: json.dumps(data[kind], ensure_ascii=False, indent=2).encode()
        for kind, name in {**FILES, "settings": "workspace-settings.json"}.items()
    }
    manifest = {
        "format": "buildtrackr-backup",
        "version": 1,
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
            expected = {"manifest.json", "workspace-settings.json", *FILES.values()}
            if (
                len(entries) != len(expected)
                or {entry.filename for entry in entries} != expected
                or sum(entry.file_size for entry in entries) > MAX_EXPANDED
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
            if (
                manifest["format"] != "buildtrackr-backup"
                or manifest["version"] != 1
                or manifest["source"] not in BACKUP_SOURCES
                or manifest["checksum"] != digest({k: v for k, v in manifest.items() if k != "checksum"})
                or set(manifest["files"]) != expected - {"manifest.json"}
            ):
                raise ValueError("Invalid manifest.")
            timestamp(manifest["createdAt"])
            data = {}
            for kind, name in {**FILES, "settings": "workspace-settings.json"}.items():
                raw = archive_file.read(name)
                if hashlib.sha256(raw).hexdigest() != manifest["files"][name]:
                    raise ValueError("File checksum mismatch.")
                data[kind] = _json(raw)
            if checked_data(data) != manifest["counts"] or digest(data) != manifest["dataHash"]:
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
            "Select a complete, supported BuildTrackr backup, not a SalesTrackr archive. "
            "No live data was changed.",
            422,
            "invalid_backup",
        ) from None


def backup_row(file_id, data, manifest, *, name=None):
    return {
        "id": file_id,
        "name": name or f"BuildTrackr-{manifest['source']}-{manifest['createdAt'][:10]}.zip",
        "createdAt": manifest["createdAt"],
        "source": manifest["source"],
        "protected": False,
        "counts": manifest["counts"],
        "sizeBytes": len(data),
        "dataHash": manifest["dataHash"],
    }


class ManufacturingDriveBackupStore(DriveBackupStore):
    app = "buildtrackr-backup"

    def __init__(self, settings):
        self.folder_id = settings.manufacturing_backup_folder_id
        if not self.folder_id or not settings.token_json:
            raise Problem(
                "Configure BuildTrackr's backup folder and reconnect Google Drive.", 503, "backup_folder"
            )
        self.sales_folders = {settings.root_id, settings.backup_folder_id} - {""}
        self.backend = DriveBackend(settings.token_json, self.folder_id, operator=True)
        self.folder_url = None

    def _folder(self):
        folder = identifier(self.folder_id)
        metadata = self.backend._execute(
            self.backend._api()
            .files()
            .get(
                fileId=folder,
                fields="id,parents,mimeType,trashed,appProperties",
            )
        )
        if (
            folder in self.sales_folders
            or metadata.get("trashed")
            or metadata.get("mimeType") != "application/vnd.google-apps.folder"
            or metadata.get("appProperties", {}).get("app") not in (None, "buildtrackr-backups")
            or self.sales_folders.intersection(metadata.get("parents", []))
        ):
            raise Problem(
                "Use BuildTrackr's separate private backup folder, not a SalesTrackr folder.",
                503,
                "backup_folder",
            )
        token = None
        while True:
            permissions = self.backend._execute(
                self.backend._api()
                .permissions()
                .list(
                    fileId=folder,
                    fields="nextPageToken,permissions(type)",
                    pageSize=100,
                    pageToken=token,
                )
            )
            if any(permission["type"] in ("anyone", "domain") for permission in permissions["permissions"]):
                raise Problem(
                    "Use a private BuildTrackr backup folder, not public or whole-domain sharing.",
                    503,
                    "backup_permissions",
                )
            token = permissions.get("nextPageToken")
            if not token:
                break
        self.folder_url = f"https://drive.google.com/drive/folders/{folder}"
        return folder

    def inventory(self):
        folder = self._folder()
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
                "and appProperties has { key='app' and value='buildtrackr-backup' }",
                fields="id,name,size,md5Checksum,createdTime",
            )
        ]

    def _metadata(self, file_id):
        metadata = super()._metadata(file_id)
        if metadata and metadata.get("appProperties", {}).get("app") != self.app:
            raise Problem("This file is not a managed BuildTrackr backup.", 409, "backup_changed")
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
                    / "buildtrackr-backups"
                    / digest(
                        {
                            "project": settings.manufacturing_project,
                            "workspace": settings.manufacturing_workspace,
                        }
                    )[:24]
                )
            )
        super().__init__(repository, settings, store)
        self.signing_key = repository.signing_key

    def check_backup_control(self, control):
        if control.get("pendingRecordCleanup"):
            raise Problem(
                "Resume the pending BuildTrackr record cleanup before creating backups.",
                503, "cleanup_pending",
            )

    def status(self, actor):
        status = super().status(actor)
        local = isinstance(self.store, LocalBackupStore)
        return {
            **status,
            **self.repo.meta(),
            "provider": "local" if local else "drive",
            "location": "Local simulated BuildTrackr backup folder"
            if local
            else "Private BuildTrackr Google Drive folder",
            "configuredFolderUrl": (
                f"https://drive.google.com/drive/folders/{self.settings.manufacturing_backup_folder_id}"
                if self.settings.manufacturing_backup_folder_id
                else None
            ),
        }

    def inspect(self, data, actor, row=None):
        self.require_admin(actor)
        _, manifest = self.read_archive(data)
        self.repo.sync()
        control = self.repo.control
        if control.get("maintenance"):
            raise Problem("BuildTrackr recovery is in progress.", 503, "maintenance")
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
                "This replaces all BuildTrackr products, operations, partners, contacts, process links, "
                "manufacturing records and activity history; it is not a merge.",
                "Current BuildTrackr data is backed up and verified before "
                "the restored dataset is activated.",
                "SalesTrackr data, settings and backups are not changed.",
                "All open BuildTrackr forms must be reloaded after recovery.",
            ],
        }

    def archived_metadata(self, recovery):
        return {"backupId": recovery["id"], "backupDataHash": recovery["dataHash"]}

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
                if self.repo.export_at(target, saved["settings"]) != saved:
                    continue
                references = [
                    document.reference
                    for kind in FILES
                    for document in target.collection(kind).select([]).stream()
                ]
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
