"""Administrator-only, restartable removal of completed sales records and their payloads."""

import time
from uuid import uuid4

from google.cloud import firestore
from pydantic import Field, model_validator

from .domain import Identifier, Model, Problem, now
from .firestore_storage import filtered
from .storage import digest


def eligible(data):
    return data.get("discarded") is True or bool(
        data.get("stageDates", {}).get("closed") and data.get("feedbackReceived") is True
    )


class Selection(Model):
    entityId: Identifier
    baseRevision: Identifier


class CleanupWrite(Model):
    writeId: Identifier
    records: list[Selection] = Field(min_length=1, max_length=500)
    confirmation: str

    @model_validator(mode="after")
    def unique_records(self):
        if len({row.entityId for row in self.records}) != len(self.records):
            raise ValueError("Select each sales record only once.")
        return self


class RecordCleanup:
    def __init__(self, repo, recovery):
        self.repo, self.recovery = repo, recovery

    def _purge_payload(self, payload, guard):
        refs = list(payload.collection("parts").list_documents())
        for encoding in payload.collection("encodings").list_documents():
            refs.extend(encoding.collection("parts").list_documents())
            refs.append(encoding)
        refs.append(payload)
        for start in range(0, len(refs), 400):

            @firestore.transactional
            def remove(transaction):
                # Shared blobs must not be deleted by an expired worker after a restore.
                guard(self.repo.workspace.get(transaction=transaction).to_dict())
                for ref in refs[start : start + 400]:
                    transaction.delete(ref)

            remove(self.repo.client.transaction())
        if payload.get().exists or any(next(c.list_documents(), None) for c in payload.collections()):
            raise Problem("Payload cleanup is incomplete. Resume the same cleanup.", 503, "cleanup_pending")

    def listing(self, actor):
        self.repo.sync()
        query = filtered(
            self.repo.namespace.collection("entities"),
            "views",
            "array_contains_any",
            ["closed", "discarded"],
        )
        rows = [
            item.to_dict()
            for item in query.stream()
            if item.to_dict()["kind"] == "record" and not item.to_dict()["conflicted"]
        ]
        accounts = self.repo._account_data(row["accountId"] for row in rows)
        items = [
            {
                "entityId": row["entityId"],
                "baseRevision": row["heads"][0],
                "accountName": accounts.get(row["accountId"], {}).get("name", "Account unavailable"),
                "productNotes": row["queryData"]["productNotes"],
                "discarded": row["queryData"]["discarded"],
                "closedOn": row["queryData"]["stageDates"].get("closed"),
            }
            for row in rows
            if eligible(row["queryData"])
        ]
        items.sort(key=lambda row: (row["accountName"].casefold(), row["entityId"]))
        pending = None
        operation_id = self.repo.control.get("pendingRecordCleanup")
        if operation_id and self.recovery.admin(actor):
            operation = (
                self.repo.workspace.collection("cleanupOperations").document(operation_id).get().to_dict()
            )
            if operation and operation["actor"] == actor:
                pending = {"writeId": operation_id, "records": operation["records"], "confirmation": "DELETE"}
        return {
            "items": items,
            "total": len(items),
            "generation": self.repo.generation,
            "canDelete": self.recovery.admin(actor),
            "pending": pending,
            "cleanupInProgress": bool(operation_id),
            "lastError": self.repo.control.get("lastCleanupError"),
        }

    def delete(self, body, actor, generation):
        self.recovery.require_admin(actor)
        if body.confirmation != "DELETE":
            raise Problem(
                "Type DELETE to permanently remove the selected records.", 422, "delete_confirmation"
            )
        self.repo.sync()
        fingerprint = digest(
            {"records": [row.model_dump() for row in body.records], "generation": generation}
        )
        operation = self.repo.workspace.collection("cleanupOperations").document(body.writeId)
        prior = operation.get().to_dict()
        if prior and (prior["actor"] != actor or prior["requestHash"] != fingerprint):
            raise Problem("Cleanup identifier was used for another request.", 409, "write_id_reused")
        if prior and prior.get("completed"):
            return prior["result"]
        self.repo.check_generation(generation)
        namespace = self.repo.namespace
        selections = {row.entityId: row.baseRevision for row in body.records}
        if not prior:
            refs = [namespace.collection("entities").document(key) for key in selections]
            rows = {item.id: item.to_dict() for item in self.repo.client.get_all(refs)}
            if any(
                not rows.get(key) or rows[key]["kind"] != "record" or rows[key]["heads"] != [head]
                for key, head in selections.items()
            ):
                raise Problem(
                    "A selected record changed or was removed. Refresh the list.", 409, "stale_edit"
                )
            refs = [namespace.collection("revisions").document(head) for head in selections.values()]
            versions = self.repo._payloads([item.to_dict() for item in self.repo.client.get_all(refs)])
            if {raw["entityId"]: raw["id"] for raw in versions} != selections or any(
                raw["entityType"] != "record" or not eligible(raw["data"]) for raw in versions
            ):
                raise Problem(
                    "Only discarded or closed-with-feedback records can be deleted.",
                    409,
                    "cleanup_ineligible",
                )
            if self.recovery.settings.manufacturing_enabled:
                self.recovery.backup(f"sales-cleanup-{digest(body.writeId)}", actor)
        owner = uuid4().hex

        @firestore.transactional
        def begin(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            self.repo.check_generation(generation, {**control, "maintenance": None})
            existing = operation.get(transaction=transaction).to_dict()
            if existing and existing.get("completed"):
                return existing
            if existing and (existing["actor"] != actor or existing["requestHash"] != fingerprint):
                raise Problem("Cleanup identifier was used for another request.", 409, "write_id_reused")
            pending = control.get("pendingRecordCleanup")
            if pending and pending != body.writeId:
                raise Problem(
                    "Resume the existing record cleanup before starting another.", 409, "cleanup_pending"
                )
            maintenance = control.get("maintenance")
            if maintenance and maintenance.get("expires", float("inf")) > time.time():
                raise Problem("Storage maintenance is in progress. Retry shortly.", 503, "maintenance")
            if (control.get("backupLease") or {}).get("expires", 0) > time.time():
                raise Problem("A backup is in progress. Retry after it finishes.", 409, "backup_in_progress")
            if not existing:
                refs = [namespace.collection("entities").document(key) for key in selections]
                rows = {
                    item.id: item.to_dict()
                    for item in self.repo.client.get_all(refs, transaction=transaction)
                }
                if any(not rows.get(key) or rows[key]["heads"] != [head] for key, head in selections.items()):
                    raise Problem("A selected record changed. Refresh the list.", 409, "stale_edit")
                existing = {
                    "actor": actor,
                    "requestHash": fingerprint,
                    "completed": False,
                    "records": [row.model_dump() for row in body.records],
                    "startedAt": now(),
                }
                transaction.create(operation, existing)
            transaction.update(
                self.repo.workspace,
                {
                    "pendingRecordCleanup": body.writeId,
                    "lastCleanupError": None,
                    "maintenance": {
                        "kind": "cleanup",
                        "writeId": body.writeId,
                        "owner": owner,
                        "startedAt": now(),
                        "expires": time.time() + 600,
                    },
                },
            )
            return existing

        state = begin(self.repo.client.transaction())
        if state.get("completed"):
            return state["result"]

        def guard(control):
            maintenance = control.get("maintenance") or {}
            if (
                control["generation"] != generation
                or maintenance.get("owner") != owner
                or maintenance.get("expires", 0) <= time.time()
            ):
                raise Problem("Cleanup ownership changed. Resume the same cleanup.", 409, "cleanup_pending")

        @firestore.transactional
        def heartbeat(transaction):
            control = self.repo.workspace.get(transaction=transaction).to_dict()
            guard(control)
            transaction.update(self.repo.workspace, {"maintenance.expires": time.time() + 600})

        deleted, kept, revision_count = [], [], 0
        try:
            for entity_id, head in selections.items():
                marker = namespace.collection("recordDeletions").document(entity_id)
                entity = namespace.collection("entities").document(entity_id)
                existing = marker.get().to_dict()
                if not existing:
                    current = self.repo.current(entity_id, "record")
                    if current["id"] != head or not eligible(current["data"]):
                        kept.append(
                            {"entityId": entity_id, "reason": "Changed since selection; kept safely."}
                        )
                        continue

                @firestore.transactional
                def mark(transaction):
                    control = self.repo.workspace.get(transaction=transaction).to_dict()
                    guard(control)
                    prior_marker = marker.get(transaction=transaction).to_dict()
                    row = entity.get(transaction=transaction).to_dict()
                    if prior_marker:
                        if prior_marker["operationId"] != body.writeId:
                            raise Problem("This record belongs to another cleanup.", 409, "cleanup_pending")
                        return prior_marker
                    if not row or row["kind"] != "record" or row["heads"] != [head]:
                        raise Problem("A selected record changed. Resume the cleanup.", 409, "stale_edit")
                    value = {"operationId": body.writeId, "entityType": "record", "deletedAt": now()}
                    updated = {
                        **control,
                        "sequence": control["sequence"] + 1,
                        "entityCounts": {
                            **control["entityCounts"],
                            "record": control["entityCounts"]["record"] - 1,
                        },
                        "dashboard": {
                            key: count - int(key in row.get("views", []))
                            for key, count in control["dashboard"].items()
                        },
                    }
                    transaction.delete(entity)
                    transaction.create(marker, value)
                    transaction.set(self.repo.workspace, updated)
                    return value

                marked = mark(self.repo.client.transaction())
                heartbeat(self.repo.client.transaction())
                namespaces = {
                    item.path: item for item in self.repo.workspace.collection("datasets").list_documents()
                }
                namespaces[namespace.path] = namespace
                namespaces = list(namespaces.values())
                if not marked.get("planReady"):
                    hashes = set()
                    for dataset in namespaces:
                        entries = [
                            item.to_dict()
                            for item in filtered(
                                dataset.collection("revisions"),
                                "entityId",
                                "==",
                                entity_id,
                            ).stream()
                        ]
                        for start in range(0, len(entries), 100):
                            for raw in self.repo._payloads(entries[start : start + 100]):
                                if raw["entityType"] != "record" or raw["entityId"] != entity_id:
                                    raise Problem(
                                        "A cleanup payload belongs to another record.", 503, "data_altered"
                                    )
                                hashes.add(digest(raw))
                    for start in range(0, len(hashes), 400):
                        batch = self.repo.client.batch()
                        for key in sorted(hashes)[start : start + 400]:
                            batch.set(marker.collection("payloads").document(key), {})
                        batch.commit()
                    marker.update({"planReady": True, "revisionCount": len(hashes)})
                for dataset in namespaces:
                    heartbeat(self.repo.client.transaction())
                    row = dataset.collection("entities").document(entity_id).get().to_dict()
                    if row and row["kind"] != "record":
                        raise Problem("A cleanup identifier belongs to another entity.", 503, "data_altered")
                    refs = [
                        item.reference
                        for item in filtered(
                            dataset.collection("revisions"),
                            "entityId",
                            "==",
                            entity_id,
                        ).stream()
                    ]
                    refs.append(dataset.collection("entities").document(entity_id))
                    for start in range(0, len(refs), 400):
                        batch = self.repo.client.batch()
                        for ref in refs[start : start + 400]:
                            batch.delete(ref)
                        batch.commit()
                for pending in marker.collection("payloads").list_documents():
                    heartbeat(self.repo.client.transaction())
                    if len(pending.id) != 64 or any(c not in "0123456789abcdef" for c in pending.id):
                        raise Problem("Invalid cleanup payload identifier.", 503, "data_altered")
                    payload = self.repo.workspace.collection("payloads").document(pending.id)
                    self._purge_payload(payload, guard)
                    pending.delete()
                marker.update({"completed": True})
                self.repo.workspace.collection("recordDeletions").document(entity_id).set(
                    {
                        "entityType": "record",
                        "completed": True,
                        "deletedAt": now(),
                        "operationId": body.writeId,
                    }
                )
                revision_count += marker.get().to_dict()["revisionCount"]
                deleted.append(entity_id)
            result = {
                "deletedIds": deleted,
                "deletedCount": len(deleted),
                "savedVersionsRemoved": revision_count,
                "kept": kept,
            }

            @firestore.transactional
            def finish(transaction):
                control = self.repo.workspace.get(transaction=transaction).to_dict()
                guard(control)
                transaction.update(
                    operation,
                    {
                        "completed": True,
                        "completedAt": now(),
                        "result": result,
                        "records": firestore.DELETE_FIELD,
                    },
                )
                transaction.update(
                    self.repo.workspace,
                    {
                        "maintenance": None,
                        "pendingRecordCleanup": None,
                        "lastCleanupAt": now(),
                        "lastCleanupError": None,
                    },
                )

            finish(self.repo.client.transaction())
            self.repo.sync()
            return result
        except Exception:

            @firestore.transactional
            def release(transaction):
                control = self.repo.workspace.get(transaction=transaction).to_dict()
                if (control.get("maintenance") or {}).get("owner") == owner:
                    transaction.update(
                        self.repo.workspace,
                        {
                            "maintenance": None,
                            "lastCleanupError": (
                                "Cleanup stopped. Some records may already be removed. "
                                "Resume the same cleanup."
                            ),
                        },
                    )

            release(self.repo.client.transaction())
            raise
