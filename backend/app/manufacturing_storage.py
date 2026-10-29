import secrets
import time
from copy import copy
from threading import RLock
from uuid import uuid4

from google.api_core.exceptions import AlreadyExists
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from .domain import Problem, now
from .manufacturing_compat import LEGACY_COLLECTIONS, canonical_counts, canonical_data
from .manufacturing_domain import (
    MODELS,
    Assignment,
    Primary,
    RemoveAssignment,
    Reorder,
    Step,
)
from .storage import digest


def deleted_record_receipt(record_id):
    return f"deleted-{digest({'manufacturingRecord': record_id})}"


class ManufacturingRepository:
    """Independent manufacturing data, with explicit emulator or production configuration."""

    def __init__(self, settings, client=None):
        settings.validate_manufacturing()
        self.mode = settings.mode
        self.client = client or firestore.Client(
            project=settings.manufacturing_project, database=settings.manufacturing_database,
            credentials=AnonymousCredentials() if settings.mode == "demo" else None,
        )
        self.workspace = self.client.collection("workspaces").document(settings.manufacturing_workspace)
        self.product_folder_context = {
            "identity": settings.manufacturing_identity(self.workspace.id),
            "rootId": settings.root_id,
        }
        self.lock = RLock()
        self.signing_key = secrets.token_bytes(32)
        try:
            self.workspace.create({
                "generation": uuid4().hex, "version": 0, "sequence": 0,
                "activeDataset": "legacy", "productSearchVersion": 1, "namingVersion": 1,
            })
        except AlreadyExists:
            @firestore.transactional
            def initialize(transaction):
                control = self.workspace.get(transaction=transaction).to_dict()
                if "activeDataset" not in control:
                    transaction.update(self.workspace, {
                        "activeDataset": "legacy", "sequence": control["version"],
                    })

            initialize(self.client.transaction())
        self.sync()
        self.migrate_legacy_naming()
        self.index_legacy_products()

    def migrate_legacy_naming(self):
        for _ in range(3):
            self.sync()
            before = self.control.copy()
            if before.get("namingVersion") == 1:
                return
            maintenance = before.get("maintenance") or {}
            if (
                maintenance and maintenance.get("expires", float("inf")) > time.time()
                or before.get("pendingRecordCleanup")
                or (before.get("backupLease") or {}).get("expires", 0) > time.time()
            ):
                raise Problem(
                    "Finish workspace maintenance before updating ProductTrackr.", 503, "maintenance",
                )
            data = self.export_at(self.dataset, {"weeklyEnabled": before.get("weeklyEnabled", True)})
            data["productFolders"] = self.product_folders(data["products"])
            if before.get("productSearchVersion") != 1:
                for product in data["products"].values():
                    product["searchChars"] = self.product_search_chars(product)
            target, statistics = self.stage(data)

            @firestore.transactional
            def activate(transaction):
                control = self.workspace.get(transaction=transaction).to_dict()
                maintenance = control.get("maintenance") or {}
                if (
                    any(control[key] != before[key] for key in ("generation", "sequence", "activeDataset"))
                    or control.get("namingVersion") == 1
                    or maintenance and maintenance.get("expires", float("inf")) > time.time()
                    or control.get("pendingRecordCleanup")
                    or (control.get("backupLease") or {}).get("expires", 0) > time.time()
                ):
                    return False
                old = self.workspace.collection("datasets").document(control["activeDataset"])
                transaction.set(old, {
                    "state": "archived", "archivedAt": now(), "namingVersion": 0,
                    "migration": "supplier-naming",
                }, merge=True)
                transaction.update(target, {"state": "active", "activatedAt": now()})
                spreadsheet = control.get("directorySpreadsheet")
                if spreadsheet and spreadsheet.get("result"):
                    spreadsheet = {
                        **spreadsheet, "result": {
                            **spreadsheet["result"],
                            "counts": canonical_counts(spreadsheet["result"]["counts"]),
                        },
                    }
                transaction.set(self.workspace, {
                    **control, **statistics, "activeDataset": target.id, "generation": target.id,
                    "sequence": control["sequence"] + 1, "maintenance": None,
                    **({"directorySpreadsheet": spreadsheet} if spreadsheet is not None else {}),
                })
                return True

            if activate(self.client.transaction()):
                self.sync()
                return
        raise Problem("ProductTrackr changed while updating. Retry shortly.", 503, "workspace_changed")

    def sync(self):
        self.control = self.workspace.get().to_dict()

    def bind(self):
        bound = copy(self)
        bound.sync()
        return bound

    @property
    def dataset(self):
        dataset_id = self.control["activeDataset"]
        return (
            self.workspace if dataset_id == "legacy"
            else self.workspace.collection("datasets").document(dataset_id)
        )

    def collection(self, kind):
        return self.dataset.collection(kind)

    @staticmethod
    def product_search_chars(data):
        return sorted(set(f"{data['name']} {data.get('partNumber', '')}".casefold()))

    def index_legacy_products(self):
        if self.workspace.get().to_dict().get("productSearchVersion") == 1:
            return

        @firestore.transactional
        def index(transaction, reference):
            control = self.workspace.get(transaction=transaction).to_dict()
            product = reference.get(transaction=transaction).to_dict()
            if product:
                transaction.update(reference, {"searchChars": self.product_search_chars(product)})
                transaction.update(self.workspace, {
                    "version": control["version"] + 1, "sequence": control["sequence"] + 1,
                })

        # Backfill only the search projection; preserve record versions and existing step links.
        for snapshot in self.collection("products").select([]).stream():
            index(self.client.transaction(), snapshot.reference)
        self.workspace.update({"productSearchVersion": 1})

    def ref(self, kind, entity_id):
        return self.collection(kind).document(entity_id)

    @staticmethod
    def check(value, expected):
        if (value or {}).get("version") != expected:
            raise Problem("This record changed. Refresh and review it before saving.", 409, "stale_edit")

    @staticmethod
    def required(snapshot):
        value = snapshot.to_dict()
        if not value:
            raise Problem("A manufacturing record was not found.", 404, "not_found")
        return value

    def read(self, kind, entity_id, transaction=None):
        return self.required(self.ref(kind, entity_id).get(transaction=transaction))

    def query(self, kind, field, value, transaction=None):
        from google.cloud.firestore_v1.base_query import FieldFilter

        return [
            item.to_dict() for item in self.collection(kind).where(
                filter=FieldFilter(field, "==", value),
            ).stream(transaction=transaction)
        ]

    @staticmethod
    def changed(old, fields, actor, entity_id):
        timestamp = now()
        return {
            **(old or {}), **fields, "id": entity_id, "version": (old or {}).get("version", 0) + 1,
            "createdAt": (old or {}).get("createdAt", timestamp), "updatedAt": timestamp, "updatedBy": actor,
        }

    def meta(self):
        control = self.workspace.get().to_dict()
        if control["generation"] != self.control["generation"]:
            raise Problem(
                "ProductTrackr was restored. Reload the workspace before continuing.",
                409, "workspace_changed",
            )
        return {
            "workspaceId": self.workspace.id, "generation": control["generation"],
            "mode": self.mode, "storageBackend": "firestore",
        }

    def export_at(self, target, preferences):
        from .backups import MAX_EXPANDED
        from .manufacturing_backups import FILES, RESERVED
        from .storage import encode

        root = target.path == self.workspace.path
        control = self.workspace.get().to_dict()
        descriptor = (
            self.workspace.collection("datasets").document("legacy").get().to_dict()
            if root and control["activeDataset"] != "legacy"
            else control if root else target.get().to_dict()
        ) or {}
        legacy = descriptor.get("namingVersion") != 1
        collections = {
            LEGACY_COLLECTIONS.get(kind, kind) if legacy else kind: kind for kind in FILES
        }
        allowed = set(collections) | (RESERVED if root else {"productFolders"})
        if any(collection.id not in allowed for collection in target.collections()):
            raise Problem(
                "An unknown ProductTrackr collection cannot be omitted from a backup.", 409, "backup_schema",
            )
        data, size = {"settings": preferences}, 0
        for stored_kind, kind in collections.items():
            data[kind] = {}
            for snapshot in target.collection(stored_kind).stream():
                value = snapshot.to_dict()
                size += len(encode(value)) + len(snapshot.id)
                if size > MAX_EXPANDED:
                    raise Problem("ProductTrackr data exceeds the 256 MiB backup limit.", 413, "backup_size")
                data[kind][snapshot.id] = value
        return canonical_data(data) if legacy else data

    def restored_product_folder(self, product_id):
        if self.control["activeDataset"] == "legacy":
            return None
        descriptor = self.dataset.get().to_dict()
        if descriptor.get("productFolders") != self.product_folder_context:
            return None
        return self.dataset.collection("productFolders").document(product_id).get().to_dict()

    def product_folders(self, products):
        folders = {}
        if self.control["activeDataset"] != "legacy":
            descriptor = self.dataset.get().to_dict()
            if descriptor.get("productFolders") == self.product_folder_context:
                folders = {
                    row.id: row.to_dict()
                    for row in self.dataset.collection("productFolders").stream()
                    if row.id in products
                }
        for row in self.workspace.collection("productFolders").stream():
            if row.id in products:
                mapping = row.to_dict()
                folders[row.id] = {
                    **mapping, "url": f"https://drive.google.com/drive/folders/{mapping['id']}",
                }
        return {**self.product_folder_context, "folders": folders}

    def snapshot(self):
        from .manufacturing_backups import checked_data

        for _ in range(3):
            before = self.workspace.get().to_dict()
            if before.get("pendingRecordCleanup"):
                raise Problem(
                    "Resume ProductTrackr record cleanup before backing up.", 503, "cleanup_pending",
                )
            if before["generation"] != self.control["generation"]:
                raise Problem("ProductTrackr changed. Reload before backing up.", 409, "workspace_changed")
            data = self.export_at(self.dataset, {"weeklyEnabled": before.get("weeklyEnabled", True)})
            data["productFolders"] = self.product_folders(data["products"])
            after = self.workspace.get().to_dict()
            if all(before[key] == after[key] for key in ("generation", "sequence")):
                checked_data(data)
                return data, before
        raise Problem(
            "ProductTrackr changed while taking its snapshot. Retry the same backup.",
            409, "snapshot_changed",
        )

    def stage(self, data):
        from .manufacturing_backups import FILES, checked_data
        from .storage import encode

        checked_data(data)
        target = self.workspace.collection("datasets").document(uuid4().hex)
        folder_data = data.get("productFolders")
        target.set({
            "state": "staging", "createdAt": now(), "namingVersion": 1,
            **({"productFolders": {
                key: value for key, value in folder_data.items() if key != "folders"
            }} if folder_data is not None else {}),
        })
        batch, count, size = self.client.batch(), 0, 0
        collections = {kind: data[kind] for kind in FILES}
        if folder_data is not None:
            # Stage recovery copies, never replace the workspace's live folder mappings.
            collections["productFolders"] = folder_data["folders"]
        for kind, rows in collections.items():
            for key, value in rows.items():
                length = len(encode(value))
                if count and (count >= 100 or size + length > 3 * 1024 * 1024):
                    batch.commit()
                    batch, count, size = self.client.batch(), 0, 0
                batch.set(target.collection(kind).document(key), value)
                count, size = count + 1, size + length
        if count:
            batch.commit()
        actual = self.export_at(target, data["settings"])
        if folder_data is not None:
            actual["productFolders"] = {
                **target.get().to_dict()["productFolders"],
                "folders": {
                    row.id: row.to_dict() for row in target.collection("productFolders").stream()
                },
            }
        checked_data(actual)
        if actual != data:
            raise Problem(
                "Staged ProductTrackr verification failed. Live data was not replaced.",
                503, "restore_verify",
            )
        target.update({"state": "verified", "dataHash": digest(actual)})
        return target, {
            "version": self.workspace.get().to_dict()["version"] + 1,
            "productSearchVersion": 1, "namingVersion": 1, **data["settings"],
        }

    def list(self, kind, search="", supplier_id=None, offset=0, limit=100):
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self.collection(kind)
        if supplier_id:
            query = query.where(filter=FieldFilter("supplierId", "==", supplier_id))
        prefix = search.strip().casefold()
        if prefix and kind == "products":
            query = query.where(filter=FieldFilter("searchChars", "array_contains", prefix[0]))
            ordered = query.order_by("nameKey").order_by("__name__")
            rows = (snapshot.to_dict() for snapshot in ordered.stream())
            matches = [
                row for row in rows
                if any(prefix in row.get(field, "").casefold() for field in ("name", "partNumber"))
            ]
            return {"items": matches[offset:offset + limit], "total": len(matches), **self.meta()}
        if prefix:
            query = query.where(filter=FieldFilter("nameKey", ">=", prefix)).where(
                filter=FieldFilter("nameKey", "<=", prefix + "\U0010ffff"),
            )
        query = query.order_by("nameKey").order_by("__name__")
        total = query.count().get()[0][0].value
        return {
            "items": [item.to_dict() for item in query.offset(offset).limit(limit).stream()],
            "total": total, **self.meta(),
        }

    def batch(self, kind, identifiers):
        refs = [self.ref(kind, key) for key in sorted(set(identifiers))]
        rows = []
        for start in range(0, len(refs), 100):
            rows.extend(self.required(item) for item in self.client.get_all(refs[start:start + 100]))
        return rows

    def detail(self, kind, entity_id):
        for _ in range(3):
            before = self.workspace.get().to_dict()
            entity = self.read(kind, entity_id)
            result = {
                "entity": entity, "products": [], "operations": [], "suppliers": [],
                "contacts": [], "steps": [], "assignments": [],
            }
            if kind == "products":
                steps = self.batch("steps", entity["stepIds"])
                by_id = {step["id"]: step for step in steps}
                result["steps"] = [by_id[key] for key in entity["stepIds"]]
                result["assignments"] = self.query("assignments", "productId", entity_id)
                result["products"] = [entity]
            elif kind == "operations":
                result["steps"] = self.query("steps", "operationId", entity_id)
                result["operations"] = [entity]
            else:
                field = "supplierId" if kind == "suppliers" else "contactId"
                result["assignments"] = self.query("assignments", field, entity_id)
                result["steps"] = self.batch("steps", [a["stepId"] for a in result["assignments"]])
                if kind == "suppliers":
                    result["suppliers"] = [entity]
                    result["contacts"] = self.query("contacts", "supplierId", entity_id)
                else:
                    result["contacts"] = [entity]
                    result["suppliers"] = [self.read("suppliers", entity["supplierId"])]
            if kind != "products":
                result["products"] = self.batch("products", [s["productId"] for s in result["steps"]])
            if kind != "operations":
                result["operations"] = self.batch("operations", [s["operationId"] for s in result["steps"]])
            if kind == "products":
                result["suppliers"] = self.batch(
                    "suppliers", [a["supplierId"] for a in result["assignments"]],
                )
                result["contacts"] = self.batch(
                    "contacts", [a["contactId"] for a in result["assignments"] if a["contactId"]],
                )
            after = self.workspace.get().to_dict()
            if before == after:
                return {
                    **result, "workspaceId": self.workspace.id, "generation": before["generation"],
                    "mode": self.mode, "storageBackend": "firestore",
                }
        raise Problem("Manufacturing data changed while loading. Please refresh.", 409, "read_changed")

    def mutate(self, action, target, body, actor, workspace, generation):
        receipt = self.ref("writes", body.writeId)
        fingerprint = digest({"action": action, "target": target, "body": body.model_dump(), "actor": actor})

        @firestore.transactional
        def commit(transaction):
            control = self.workspace.get(transaction=transaction).to_dict()
            prior = receipt.get(transaction=transaction).to_dict()
            if (
                workspace != self.workspace.id or generation != control["generation"]
                or self.control["generation"] != control["generation"]
            ):
                raise Problem("ProductTrackr changed. Reload before saving.", 409, "workspace_changed")
            if control.get("maintenance"):
                raise Problem(
                    "ProductTrackr recovery is in progress. Retry the same change shortly.",
                    503, "maintenance",
                )
            if control.get("pendingRecordCleanup"):
                raise Problem("Resume ProductTrackr record cleanup before saving.", 503, "cleanup_pending")
            if action.startswith("record:"):
                marker = self.ref("writes", deleted_record_receipt(body.id)).get(
                    transaction=transaction,
                ).to_dict()
                if marker:
                    raise Problem(
                        "This manufacturing record was permanently deleted. It cannot be saved or recreated.",
                        410, "record_deleted",
                    )
            if prior:
                if prior["result"]["generation"] != control["generation"]:
                    raise Problem(
                        "This write belongs to an older workspace. Reload before saving.",
                        409, "workspace_changed",
                    )
                if prior["fingerprint"] != fingerprint:
                    raise Problem("This request ID was used for different values.", 409, "write_id_reused")
                return prior["result"]
            # Serializing on the control document protects query-based relationship checks.
            writes, deletes = self.plan(transaction, action, target, body, actor)
            for reference, value in writes:
                transaction.set(reference, value)
            for reference in deletes:
                transaction.delete(reference)
            result = {"id": body.id, "workspaceId": workspace, "generation": generation}
            transaction.update(self.workspace, {
                "version": control["version"] + 1, "sequence": control["sequence"] + 1,
            })
            transaction.create(receipt, {"fingerprint": fingerprint, "result": result, "createdAt": now()})
            return result

        return commit(self.client.transaction())

    def plan(self, tx, action, target, body, actor):
        if action.startswith("record:"):
            from .manufacturing_records import ManufacturingRecords

            return ManufacturingRecords(self).plan(tx, action.split(":")[1], target, body, actor)
        writes, deletes = [], []

        def put(kind, old, fields, entity_id):
            value = self.changed(old, fields, actor, entity_id)
            writes.append((self.ref(kind, entity_id), value))
            return value

        if action.startswith("create:") or action.startswith("edit:"):
            verb, kind = action.split(":")
            fields = MODELS[kind].model_validate(body.data).model_dump()
            old = self.ref(kind, body.id).get(transaction=tx).to_dict()
            if verb == "create" and old:
                raise Problem("This manufacturing identifier already exists.", 409, "record_exists")
            if verb == "edit" and not old:
                raise Problem("Manufacturing record not found.", 404, "not_found")
            self.check(old, body.baseVersion)
            if kind == "suppliers" and old and old["type"] != fields["type"]:
                raise Problem("Supplier type cannot change; its company contacts must stay valid.")
            if kind == "contacts":
                supplier = self.read("suppliers", fields["supplierId"], tx)
                if supplier["type"] != "company":
                    raise Problem("Contacts belong to company suppliers, not independent people.")
                if old and old["supplierId"] != fields["supplierId"]:
                    raise Problem("Moving a contact between companies is not supported.")
            if kind == "products" and not old:
                fields["stepIds"] = []
            if kind == "products":
                fields["searchChars"] = self.product_search_chars(fields)
            put(kind, old, {**fields, "nameKey": fields["name"].casefold()}, body.id)
            return writes, deletes

        if action in ("add-step", "reorder"):
            product = self.read("products", target, tx)
            self.check(product, body.baseVersion)
            if action == "add-step":
                fields = Step.model_validate(body.data).model_dump()
                self.read("operations", fields["operationId"], tx)
                if self.ref("steps", body.id).get(transaction=tx).exists:
                    raise Problem("This operation step already exists.", 409, "record_exists")
                put("steps", None, {
                    **fields, "productId": target, "primaryAssignmentId": None,
                }, body.id)
                ordered = [*product["stepIds"], body.id]
            else:
                ordered = Reorder.model_validate(body.data).stepIds
                if len(set(ordered)) != len(ordered) or set(ordered) != set(product["stepIds"]):
                    raise Problem("Reordering must include every product operation exactly once.")
            put("products", product, {"stepIds": ordered}, target)
            return writes, deletes

        step = self.read("steps", target, tx)
        self.check(step, body.baseVersion)
        if action == "edit-step":
            fields = Step.model_validate(body.data).model_dump()
            if fields["operationId"] != step["operationId"]:
                raise Problem("Changing a linked operation type is not supported.")
            put("steps", step, fields, target)
            return writes, deletes

        assignments = self.query("assignments", "stepId", target, tx)
        by_id = {item["id"]: item for item in assignments}
        primary = step["primaryAssignmentId"]
        if action in ("assign", "edit-assignment"):
            fields = Assignment.model_validate(body.data).model_dump()
            supplier = self.read("suppliers", fields["supplierId"], tx)
            if fields["contactId"]:
                contact = self.read("contacts", fields["contactId"], tx)
                if supplier["type"] != "company" or contact["supplierId"] != supplier["id"]:
                    raise Problem("Choose a contact belonging to this company supplier.")
            old = by_id.get(body.id)
            if action == "edit-assignment" and not old:
                raise Problem("Assignment not found.", 404, "not_found")
            if action == "assign" and self.ref("assignments", body.id).get(transaction=tx).exists:
                raise Problem("Assignment ID is already in use.", 409, "record_exists")
            if old and old["supplierId"] != fields["supplierId"]:
                raise Problem("Remove the existing assignment before choosing a different supplier.")
            if any(a["supplierId"] == fields["supplierId"] and a["id"] != body.id for a in assignments):
                raise Problem(
                    "This supplier is already assigned to this product operation.", 409, "duplicate",
                )
            put("assignments", old, {**fields, "stepId": target, "productId": step["productId"]}, body.id)
            primary = primary or body.id
        elif action == "primary":
            primary = Primary.model_validate(body.data).assignmentId
            if primary not in by_id:
                raise Problem("The primary supplier must be assigned to this product operation.")
        elif action == "remove":
            removal = RemoveAssignment.model_validate(body.data)
            if body.id not in by_id:
                raise Problem("Assignment not found.", 404, "not_found")
            remaining = set(by_id) - {body.id}
            if primary == body.id:
                if not remaining and removal.replacementId:
                    raise Problem("The last assignment does not need a replacement primary supplier.")
                primary = removal.replacementId if remaining else None
                if remaining and primary not in remaining:
                    raise Problem("Choose a remaining supplier to replace the primary supplier.")
            elif removal.replacementId:
                raise Problem("A replacement is only needed when removing the primary supplier.")
            deletes.append(self.ref("assignments", body.id))
        else:
            raise Problem("Unknown manufacturing action.", 404, "not_found")
        put("steps", step, {"primaryAssignmentId": primary}, target)
        return writes, deletes
