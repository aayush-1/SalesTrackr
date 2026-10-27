"""Backed-up, resumable cleanup of selected completed manufacturing jobs."""

import time
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from google.cloud import firestore
from pydantic import Field, model_validator

from .domain import Identifier, Model, Problem, now
from .manufacturing_backups import ManufacturingBackupManager
from .manufacturing_records import ManufacturingRecords
from .manufacturing_storage import deleted_record_receipt
from .storage import digest


class Selection(Model):
    id: Identifier
    baseVersion: int = Field(ge=1)


class CleanupWrite(Model):
    writeId: Identifier
    records: list[Selection] = Field(min_length=1, max_length=50)
    confirmation: str

    @model_validator(mode="after")
    def unique_records(self):
        if len({row.id for row in self.records}) != len(self.records):
            raise ValueError("Select each manufacturing record only once.")
        return self


class ManufacturingCleanup:
    def __init__(self, repository, recovery):
        self.repo, self.recovery = repository, recovery

    def listing(self, actor, offset=0, limit=50):
        page = ManufacturingRecords(self.repo).list(view="completed", offset=offset, limit=limit)
        control = self.repo.workspace.get().to_dict()
        operation_id = control.get("pendingRecordCleanup")
        pending = None
        if operation_id and self.recovery.admin(actor):
            operation = (
                self.repo.workspace.collection("cleanupOperations").document(operation_id).get().to_dict()
            )
            if operation and operation["actor"] == actor and not operation.get("completed"):
                pending = {"request": operation["request"], "generation": operation["generation"]}
        return {
            **page,
            "canDelete": self.recovery.admin(actor),
            "pending": pending,
            "cleanupInProgress": bool(operation_id),
            "lastError": control.get("lastCleanupError"),
        }

    def _validate(self, records, selections):
        for key, version in selections.items():
            row = records.get(key)
            if not row or row["version"] != version:
                raise Problem("A selected job changed or was removed. Refresh the list.", 409, "stale_edit")
            if row["status"] != "completed":
                raise Problem(
                    "Only completed manufacturing records can be deleted.", 409, "cleanup_ineligible"
                )

    def _remove_batch(self, documents, operation, owner, generation):
        @firestore.transactional
        def remove(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            state = operation.get(transaction=transaction).to_dict()
            self._guard(control, operation.id, owner, generation)
            references = [reference for reference, _ in documents]
            expected = {reference.path: value for reference, value in documents}
            rows = list(self.repo.client.get_all(references, transaction=transaction))
            existing = [row for row in rows if row.exists]
            if any(row.to_dict() != expected[row.reference.path] for row in existing):
                raise Problem("Saved history changed. Cleanup stopped safely.", 409, "cleanup_changed")
            events = sum(row.reference.parent.id == "manufacturingRecordEvents" for row in existing)
            receipts = len(existing) - events
            for row in existing:
                transaction.delete(row.reference)
            transaction.update(
                operation,
                {
                    "eventsRemoved": state["eventsRemoved"] + events,
                    "receiptsRemoved": state["receiptsRemoved"] + receipts,
                },
            )
            transaction.update(
                self.repo.workspace,
                {
                    "version": control["version"] + int(bool(existing)),
                    "sequence": control["sequence"] + int(bool(existing)),
                    "maintenance.expires": time.time() + 600,
                },
            )

        remove(self.repo.client.transaction())

    def _guard(self, control, write_id, owner, generation):
        maintenance = control.get("maintenance") or {}
        if (
            control["generation"] != generation
            or control["activeDataset"] != self.repo.control["activeDataset"]
            or control.get("pendingRecordCleanup") != write_id
            or maintenance.get("owner") != owner
            or maintenance.get("expires", 0) <= time.time()
        ):
            raise Problem("Cleanup ownership changed. Resume the same cleanup.", 409, "cleanup_pending")

    def delete(self, body, actor, generation):
        self.recovery.require_admin(actor)
        if body.confirmation != "DELETE":
            raise Problem("Type DELETE to permanently remove selected jobs.", 422, "delete_confirmation")
        operation = self.repo.workspace.collection("cleanupOperations").document(body.writeId)
        selections = {row.id: row.baseVersion for row in body.records}
        fingerprint = digest({"records": sorted(selections.items()), "generation": generation})
        prior = operation.get().to_dict()
        if prior and (prior["actor"] != actor or prior["requestHash"] != fingerprint):
            raise Problem("Cleanup identifier was used for another request.", 409, "write_id_reused")
        if prior and prior.get("completed"):
            return prior["result"]
        if generation != self.repo.control["generation"]:
            raise Problem("BuildTrackr changed. Reload before deleting jobs.", 409, "workspace_changed")
        if not prior:
            control = self.repo.workspace.get().to_dict()
            if control.get("pendingRecordCleanup"):
                raise Problem("Resume the existing BuildTrackr cleanup first.", 409, "cleanup_pending")
            references = [self.repo.ref("manufacturingRecords", key) for key in selections]
            current = {row.id: row.to_dict() for row in self.repo.client.get_all(references)}
            self._validate(current, selections)
            backup = self.recovery.backup(f"cleanup-{digest(body.writeId)}", actor)
        else:
            backup = prior["backup"]
        owner = uuid4().hex

        @firestore.transactional
        def claim(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            existing = operation.get(transaction=transaction).to_dict()
            if existing and (existing["actor"] != actor or existing["requestHash"] != fingerprint):
                raise Problem("Cleanup identifier was used for another request.", 409, "write_id_reused")
            if existing and existing.get("completed"):
                return existing
            if (
                control["generation"] != generation
                or control["activeDataset"] != self.repo.control["activeDataset"]
            ):
                raise Problem("BuildTrackr changed. Reload before deleting jobs.", 409, "workspace_changed")
            if control.get("pendingRecordCleanup") not in (None, body.writeId):
                raise Problem("Resume the existing BuildTrackr cleanup first.", 409, "cleanup_pending")
            maintenance = control.get("maintenance")
            if maintenance and maintenance.get("expires", float("inf")) > time.time():
                raise Problem("Storage maintenance is in progress. Retry shortly.", 503, "maintenance")
            if (control.get("backupLease") or {}).get("expires", 0) > time.time():
                raise Problem("A backup is in progress. Retry shortly.", 503, "backup_in_progress")
            if not existing:
                refs = [self.repo.ref("manufacturingRecords", key) for key in selections]
                records = {
                    row.id: row.to_dict() for row in self.repo.client.get_all(refs, transaction=transaction)
                }
                self._validate(records, selections)
                existing = {
                    "actor": actor,
                    "generation": generation,
                    "requestHash": fingerprint,
                    "request": body.model_dump(),
                    "backup": backup,
                    "completed": False,
                    "deletedIds": [],
                    "eventsRemoved": 0,
                    "receiptsRemoved": 0,
                    "createdAt": now(),
                }
                transaction.create(operation, existing)
            transaction.update(
                self.repo.workspace,
                {
                    "pendingRecordCleanup": body.writeId,
                    "lastCleanupError": None,
                    "maintenance": {
                        "kind": "record-cleanup",
                        "owner": owner,
                        "writeId": body.writeId,
                        "expires": time.time() + 600,
                    },
                },
            )
            return existing

        state = claim(self.repo.client.transaction())
        if state.get("completed"):
            return state["result"]
        try:
            content, _ = self.recovery.get(state["backup"]["id"])
            saved, _ = self.recovery.read_archive(content)
            self._validate(saved["manufacturingRecords"], selections)
            for record_id in selections:
                self._mark_record(
                    record_id, saved["manufacturingRecords"][record_id], operation, owner, generation
                )
                events = {
                    key: value
                    for key, value in saved["manufacturingRecordEvents"].items()
                    if value["recordId"] == record_id
                }
                documents = [
                    (self.repo.ref("manufacturingRecordEvents", key), value) for key, value in events.items()
                ] + [
                    (self.repo.ref("writes", key), saved["writes"][key])
                    for key in events
                    if key in saved["writes"]
                ]
                for offset in range(0, len(documents), 100):
                    self._remove_batch(documents[offset : offset + 100], operation, owner, generation)
            result = self._complete(operation, owner, generation)
        finally:
            self._release(operation, owner)
        return result

    def _mark_record(self, record_id, expected, operation, owner, generation):
        reference = self.repo.ref("manufacturingRecords", record_id)
        marker = self.repo.ref("writes", deleted_record_receipt(record_id))

        @firestore.transactional
        def mark(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            state = operation.get(transaction=transaction).to_dict()
            self._guard(control, operation.id, owner, generation)
            record = reference.get(transaction=transaction).to_dict()
            receipt = marker.get(transaction=transaction).to_dict()
            if record_id in state["deletedIds"]:
                if record is not None or not receipt or receipt["result"].get("cleanupId") != operation.id:
                    raise Problem("A deleted job changed. Cleanup stopped safely.", 409, "cleanup_changed")
                return
            if receipt or record != expected or record["status"] != "completed":
                raise Problem("A selected job changed. Cleanup stopped safely.", 409, "stale_edit")
            # Keep one tiny receipt so retrying an old create cannot resurrect a deleted job.
            transaction.create(
                marker,
                {
                    "fingerprint": digest({"deletedManufacturingRecord": record_id}),
                    "createdAt": now(),
                    "result": {
                        "id": record_id,
                        "workspaceId": self.repo.workspace.id,
                        "generation": generation,
                        "deleted": True,
                        "cleanupId": operation.id,
                    },
                },
            )
            transaction.delete(reference)
            transaction.update(operation, {"deletedIds": [*state["deletedIds"], record_id]})
            transaction.update(
                self.repo.workspace,
                {
                    "version": control["version"] + 1,
                    "sequence": control["sequence"] + 1,
                    "maintenance.expires": time.time() + 600,
                },
            )

        mark(self.repo.client.transaction())

    def _complete(self, operation, owner, generation):
        @firestore.transactional
        def finish(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            state = operation.get(transaction=transaction).to_dict()
            self._guard(control, operation.id, owner, generation)
            result = {
                "deletedIds": state["deletedIds"],
                "deletedCount": len(state["deletedIds"]),
                "activityEventsRemoved": state["eventsRemoved"],
                "writeReceiptsRemoved": state["receiptsRemoved"],
                "retryMarkersKept": len(state["deletedIds"]),
                "recoveryBackup": state["backup"],
                "workspaceId": self.repo.workspace.id,
                "generation": generation,
            }
            transaction.update(operation, {"completed": True, "completedAt": now(), "result": result})
            transaction.update(
                self.repo.workspace,
                {
                    "pendingRecordCleanup": None,
                    "maintenance": None,
                    "lastCleanupError": None,
                },
            )
            return result

        return finish(self.repo.client.transaction())

    def _release(self, operation, owner):
        @firestore.transactional
        def release(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            state = operation.get(transaction=transaction).to_dict()
            if (control.get("maintenance") or {}).get("owner") != owner:
                return
            fields = {"maintenance": None, "lastCleanupError": "Job cleanup was interrupted. Retry it."}
            if not state["deletedIds"]:
                fields.update({"pendingRecordCleanup": None, "lastCleanupError": None})
                transaction.delete(operation)
            transaction.update(self.repo.workspace, fields)

        release(self.repo.client.transaction())


def manufacturing_cleanup_router(repository, settings):
    router = APIRouter(prefix="/settings/record-cleanup")

    def service():
        repo = repository()
        return ManufacturingCleanup(repo, ManufacturingBackupManager(repo, settings))

    @router.get("")
    def listing(request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=50)):
        return service().listing(request.state.actor, offset, limit)

    @router.post("")
    def remove(body: CleanupWrite, request: Request):
        cleanup = service()
        if request.headers.get("x-buildtrackr-workspace") != cleanup.repo.workspace.id:
            raise Problem("BuildTrackr changed. Reload before deleting jobs.", 409, "workspace_changed")
        return cleanup.delete(body, request.state.actor, request.headers.get("x-buildtrackr-generation"))

    return router
