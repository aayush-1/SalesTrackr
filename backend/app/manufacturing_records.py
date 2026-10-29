import json
from copy import deepcopy
from datetime import date
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import Field

from .domain import Identifier, Model, PartQuantity, Problem, Text, now, today
from .manufacturing_domain import Command
from .storage import digest

RecordView = Literal["active", "all", "ready", "in_progress", "completed", "cancelled"]
RecordAction = Literal["assign", "complete", "reassign", "undo", "cancel", "quantity"]
SUMMARY_FIELDS = [
    "quantity",
    "inventoryTracked",
    "id",
    "number",
    "productId",
    "productName",
    "productPartNumber",
    "notes",
    "status",
    "active",
    "completedCount",
    "stepCount",
    "currentOperationId",
    "currentOperationName",
    "currentSupplierId",
    "currentSupplierName",
    "currentContactName",
    "createdAt",
    "updatedAt",
    "version",
    "startedOn",
    "completedOn",
    "searchText",
]


class NewManufacturingRecord(Model):
    productId: Identifier
    sourceToken: str = Field(pattern=r"^[a-f0-9]{64}$")
    notes: Text = ""
    quantity: PartQuantity


class QuantityCorrection(Model):
    quantity: PartQuantity
    reason: Text = Field(min_length=1)


class WorkAssignment(Model):
    stepId: Identifier
    supplierId: Identifier
    contactId: Identifier | None = None
    occurredOn: date | None = None
    note: Text = ""


class WorkReassignment(WorkAssignment):
    reason: Text = Field(min_length=1)


class WorkCompletion(Model):
    stepId: Identifier
    occurredOn: date | None = None
    note: Text = ""


class WorkCorrection(Model):
    stepId: Identifier
    reason: Text = Field(min_length=1)


class WorkCancellation(Model):
    reason: Text = Field(min_length=1)


class ManufacturingRecords:
    def __init__(self, repository):
        self.repo = repository

    def many(self, kind, identifiers, transaction=None):
        refs = [self.repo.ref(kind, key) for key in sorted(set(identifiers))]
        result = {}
        for start in range(0, len(refs), 100):
            for snapshot in self.repo.client.get_all(refs[start : start + 100], transaction=transaction):
                value = self.repo.required(snapshot)
                result[value["id"]] = value
        return result

    def source(self, product_id, transaction=None):
        product = self.repo.read("products", product_id, transaction)
        if not product["stepIds"]:
            raise Problem("Add at least one operation to this Product process before creating a record.")
        if len(product["stepIds"]) > 100:
            raise Problem("A manufacturing record supports up to 100 operation steps.")
        steps = self.many("steps", product["stepIds"], transaction)
        operations = self.many("operations", [s["operationId"] for s in steps.values()], transaction)
        ordered = []
        for step_id in product["stepIds"]:
            step = steps[step_id]
            if step["productId"] != product_id:
                raise Problem("The Product process has an invalid operation link.", 409, "invalid_process")
            ordered.append(
                {
                    "sourceStepId": step_id,
                    "operationId": step["operationId"],
                    "name": operations[step["operationId"]]["name"],
                    "instructions": step["instructions"],
                }
            )
        source = {
            "productId": product_id,
            "productName": product["name"],
            "productPartNumber": product["partNumber"],
            "operations": ordered,
        }
        if len(json.dumps(source).encode()) > 700_000:
            raise Problem("This process is too large to copy safely into one manufacturing record.")
        return source

    def preview(self, product_id):
        for _ in range(3):
            before = self.repo.workspace.get().to_dict()
            source = self.source(product_id)
            links = self.repo.query("assignments", "productId", product_id)
            suppliers = self.many("suppliers", [link["supplierId"] for link in links])
            suggestions = {
                step["sourceStepId"]: [
                    suppliers[link["supplierId"]]["name"]
                    for link in links
                    if link["stepId"] == step["sourceStepId"]
                ]
                for step in source["operations"]
            }
            if before == self.repo.workspace.get().to_dict():
                return {
                    "process": source,
                    "sourceToken": digest(source),
                    "suggestions": suggestions,
                    "today": today().isoformat(),
                    **self.repo.meta(),
                }
        raise Problem(
            "The Product process changed while loading. Refresh and review it.", 409, "read_changed"
        )

    @staticmethod
    def work_date(value, minimum=None):
        actual = value or today()
        if actual > today():
            raise Problem("Actual work dates cannot be in the future.")
        if minimum and actual.isoformat() < minimum:
            raise Problem(f"The work date cannot be before {minimum}.")
        return actual.isoformat()

    @staticmethod
    def project(record):
        current = record["operations"][record["currentIndex"]] if record["currentIndex"] is not None else None
        assignment = (current or {}).get("assignment") or {}
        record.update(
            {
                "active": record["status"] in ("ready", "in_progress"),
                "stepCount": len(record["operations"]),
                "completedCount": sum(s["status"] == "completed" for s in record["operations"]),
                "currentOperationId": (current or {}).get("operationId"),
                "currentOperationName": (current or {}).get("name"),
                "currentSupplierId": assignment.get("supplierId"),
                "currentSupplierName": assignment.get("supplierName"),
                "currentContactName": (assignment.get("contact") or {}).get("name"),
                "searchText": [
                    record["number"].casefold(),
                    record["productName"].casefold(),
                    record["productPartNumber"].casefold(),
                ],
            }
        )
        record["searchChars"] = sorted(set(" ".join(record["searchText"])))
        return record

    def eligible(self, record, transaction=None):
        current = record["operations"][record["currentIndex"]]
        source = self.repo.ref("steps", current["sourceStepId"]).get(transaction=transaction).to_dict()
        if not source:
            raise Problem(
                "The source Product operation is unavailable; its supplier choices cannot be resolved.",
                409,
                "source_unavailable",
            )
        if source["productId"] != record["productId"] or source["operationId"] != current["operationId"]:
            raise Problem("The source operation link is no longer valid.", 409, "source_unavailable")
        links = self.repo.query("assignments", "stepId", source["id"], transaction)
        suppliers = self.many("suppliers", [link["supplierId"] for link in links], transaction)
        choices = [
            {
                "supplier": suppliers[link["supplierId"]],
                "suggestedContactId": link["contactId"],
                "primary": source["primaryAssignmentId"] == link["id"],
            }
            for link in links
        ]
        return sorted(
            choices, key=lambda choice: (not choice["primary"], choice["supplier"]["name"].casefold())
        )

    def choose_assignment(self, record, command, transaction):
        choices = self.eligible(record, transaction)
        choice = next((c for c in choices if c["supplier"]["id"] == command.supplierId), None)
        if not choice:
            raise Problem(
                "This supplier is not currently linked to the source Product operation. Refresh the choices.",
                409,
                "supplier_not_linked",
            )
        supplier = choice["supplier"]
        contact = None
        if command.contactId:
            contact = self.repo.read("contacts", command.contactId, transaction)
            if supplier["type"] != "company" or contact["supplierId"] != supplier["id"]:
                raise Problem("Choose a contact belonging to this company supplier.")
            contact = {key: contact[key] for key in ("id", "name", "designation", "email", "phone")}
        return {
            "supplierId": supplier["id"],
            "supplierName": supplier["name"],
            "type": supplier["type"],
            "relationship": supplier["relationship"],
            "email": supplier["email"],
            "phone": supplier["phone"],
            "contact": contact,
        }

    def plan(self, transaction, action, target, body, actor):
        writes = []
        event = {"recordId": body.id, "action": action, "actor": actor, "recordedAt": now()}
        if action == "create":
            data = NewManufacturingRecord.model_validate(body.data)
            if body.baseVersion is not None:
                raise Problem("A new manufacturing record cannot have a base version.")
            if self.repo.ref("manufacturingRecords", body.id).get(transaction=transaction).exists:
                raise Problem("This manufacturing record already exists.", 409, "record_exists")
            source = self.source(data.productId, transaction)
            if digest(source) != data.sourceToken:
                raise Problem(
                    "The Product process changed. Reload and review its operations.", 409, "process_changed"
                )
            counter_ref = self.repo.ref("sequences", "manufacturing-records")
            counter = counter_ref.get(transaction=transaction).to_dict() or {"value": 0}
            number = counter["value"] + 1
            record = {
                **source,
                "number": f"MFG-{number:05d}",
                "notes": data.notes,
                "quantity": data.quantity,
                "inventoryTracked": True,
                "currentIndex": 0,
                "status": "ready",
                "startedOn": None,
                "completedOn": None,
                "cancelledAt": None,
                "cancelReason": "",
                "operations": [
                    {
                        **step,
                        "id": uuid4().hex,
                        "status": "ready" if i == 0 else "pending",
                        "assignment": None,
                        "startedOn": None,
                        "assignedOn": None,
                        "completedOn": None,
                        "assignmentNote": "",
                        "completionNote": "",
                    }
                    for i, step in enumerate(source["operations"])
                ],
            }
            old = None
            writes.append((counter_ref, {"value": number}))
            event["note"] = data.notes
        else:
            old = self.repo.read("manufacturingRecords", target, transaction)
            self.repo.check(old, body.baseVersion)
            record = deepcopy(old)
            if record["status"] == "cancelled":
                raise Problem("Cancelled manufacturing records are read-only.", 409, "record_cancelled")
            if record["status"] == "completed" and action not in ("undo", "quantity"):
                raise Problem("This manufacturing record is already completed.", 409, "record_completed")
            if action == "quantity":
                data = QuantityCorrection.model_validate(body.data)
                event.update(
                    reason=data.reason, previousQuantity=record.get("quantity"), quantity=data.quantity,
                )
                record["quantity"] = data.quantity
            elif action == "cancel":
                data = WorkCancellation.model_validate(body.data)
                record["status"] = "cancelled"
                record["cancelledAt"], record["cancelReason"] = now(), data.reason
                record["operations"][record["currentIndex"]]["status"] = "cancelled"
                event["reason"] = data.reason
            elif action == "undo":
                data = WorkCorrection.model_validate(body.data)
                index = (
                    (record["currentIndex"] - 1)
                    if record["status"] == "ready"
                    else (len(record["operations"]) - 1 if record["status"] == "completed" else -1)
                )
                if index < 0 or record["operations"][index]["id"] != data.stepId:
                    raise Problem(
                        "Only the last completion can be undone, before the next operation starts.",
                        409,
                        "cannot_undo",
                    )
                previous = record["operations"][index]
                if previous["status"] != "completed":
                    raise Problem("This operation is not completed.", 409, "cannot_undo")
                event.update(
                    {
                        "stepId": previous["id"],
                        "operationName": previous["name"],
                        "previousCompletedOn": previous["completedOn"],
                        "reason": data.reason,
                    }
                )
                if record["status"] == "ready":
                    record["operations"][record["currentIndex"]]["status"] = "pending"
                previous["status"], previous["completedOn"], previous["completionNote"] = (
                    "in_progress",
                    None,
                    "",
                )
                record["currentIndex"], record["status"], record["completedOn"] = index, "in_progress", None
            else:
                current = record["operations"][record["currentIndex"]]
                models = {"assign": WorkAssignment, "reassign": WorkReassignment, "complete": WorkCompletion}
                data = models[action].model_validate(body.data)
                if data.stepId != current["id"]:
                    raise Problem("Only the current operation can be changed.", 409, "wrong_step")
                event.update({"stepId": current["id"], "operationName": current["name"]})
                if action in ("assign", "reassign"):
                    expected = "ready" if action == "assign" else "in_progress"
                    if current["status"] != expected:
                        raise Problem(
                            "The operation is not in the expected state. Refresh it.", 409, "wrong_state"
                        )
                    previous_date = (
                        record["operations"][record["currentIndex"] - 1]["completedOn"]
                        if record["currentIndex"]
                        else None
                    )
                    actual = self.work_date(
                        data.occurredOn,
                        current["assignedOn"] if action == "reassign" else previous_date,
                    )
                    assignment = self.choose_assignment(record, data, transaction)
                    if action == "reassign":
                        previous = current["assignment"]
                        if assignment["supplierId"] == previous["supplierId"] and (
                            assignment["contact"] or {}
                        ).get("id") == (previous["contact"] or {}).get("id"):
                            raise Problem("Choose a different supplier or contact when reassigning.")
                        event.update({"previousAssignment": current["assignment"], "reason": data.reason})
                    current.update(
                        {
                            "assignment": assignment,
                            "assignedOn": actual,
                            "assignmentNote": data.note,
                            "startedOn": current["startedOn"] or actual,
                            "status": "in_progress",
                        }
                    )
                    record["status"], record["startedOn"] = "in_progress", record["startedOn"] or actual
                    event.update({"assignment": assignment, "occurredOn": actual, "note": data.note})
                else:
                    if current["status"] != "in_progress" or not current["assignment"]:
                        raise Problem(
                            "Assign and start this operation before completing it.", 409, "wrong_state"
                        )
                    actual = self.work_date(data.occurredOn, current["assignedOn"])
                    current.update(
                        {"status": "completed", "completedOn": actual, "completionNote": data.note}
                    )
                    event.update(
                        {"assignment": current["assignment"], "occurredOn": actual, "note": data.note}
                    )
                    next_index = record["currentIndex"] + 1
                    if next_index == len(record["operations"]):
                        record["currentIndex"], record["status"], record["completedOn"] = (
                            None,
                            "completed",
                            actual,
                        )
                    else:
                        record["currentIndex"], record["status"] = next_index, "ready"
                        record["operations"][next_index]["status"] = "ready"
        saved = self.repo.changed(old, self.project(record), actor, body.id)
        if len(json.dumps(saved).encode()) > 900_000:
            raise Problem("This manufacturing record is too large to save safely.")
        event.update({"id": body.writeId, "sequence": saved["version"]})
        from .inventory import Inventory

        writes.extend(Inventory(self.repo).manufacturing(
            transaction, saved, body, actor, old is None, event.get("reason", ""),
        ))
        writes.extend(
            [
                (self.repo.ref("manufacturingRecords", body.id), saved),
                (self.repo.ref("manufacturingRecordEvents", body.writeId), event),
            ]
        )
        return writes, []

    def detail(self, record_id):
        for _ in range(3):
            before = self.repo.workspace.get().to_dict()
            record = self.repo.read("manufacturingRecords", record_id)
            choices, error = [], ""
            if record["active"]:
                try:
                    choices = self.eligible(record)
                except Problem as problem:
                    error = problem.message
            if before == self.repo.workspace.get().to_dict():
                return {
                    "record": record,
                    "eligibleSuppliers": choices,
                    "eligibilityError": error,
                    "today": today().isoformat(),
                    **self.repo.meta(),
                }
        raise Problem("This record changed while loading. Refresh it.", 409, "read_changed")

    def summary(self):
        query = self.repo.collection("manufacturingRecords")
        counts = {
            state: query.where(filter=FieldFilter("status", "==", state)).count().get()[0][0].value
            for state in ("ready", "in_progress", "completed", "cancelled")
        }
        return {
            "counts": {
                **counts,
                "active": counts["ready"] + counts["in_progress"],
                "all": sum(counts.values()),
            },
            **self.repo.meta(),
        }

    def list(
        self,
        view="active",
        search="",
        product_id=None,
        supplier_id=None,
        operation_id=None,
        offset=0,
        limit=50,
    ):
        conditions = [
            (field, value)
            for field, value in (
                ("productId", product_id),
                ("currentSupplierId", supplier_id),
                ("currentOperationId", operation_id),
            )
            if value
        ]
        if view == "active":
            conditions.append(("active", True))
        elif view != "all":
            conditions.append(("status", view))
        query = self.repo.collection("manufacturingRecords")
        term = search.strip().casefold()
        if conditions:
            field, value = conditions[0]
            query = query.where(filter=FieldFilter(field, "==", value))
        elif term:
            query = query.where(filter=FieldFilter("searchChars", "array_contains", term[0]))
        query = query.order_by("createdAt", direction="DESCENDING").order_by(
            "__name__", direction="DESCENDING"
        )
        if term or len(conditions) > 1:
            rows = (item.to_dict() for item in query.select(SUMMARY_FIELDS).stream())
            matches = [
                row
                for row in rows
                if all(row.get(field) == value for field, value in conditions)
                and (not term or any(term in text for text in row["searchText"]))
            ]
            items, total = matches[offset : offset + limit], len(matches)
        else:
            total = query.count().get()[0][0].value
            items = [
                item.to_dict() for item in query.select(SUMMARY_FIELDS).offset(offset).limit(limit).stream()
            ]
        return {"items": items, "total": total, **self.repo.meta()}

    def history(self, record_id, offset, limit):
        self.repo.read("manufacturingRecords", record_id)
        query = (
            self.repo.collection("manufacturingRecordEvents")
            .where(
                filter=FieldFilter("recordId", "==", record_id),
            )
            .order_by("sequence", direction="DESCENDING")
        )
        return {
            "items": [item.to_dict() for item in query.offset(offset).limit(limit).stream()],
            "total": query.count().get()[0][0].value,
            **self.repo.meta(),
        }

    def supplier_work(self, search="", offset=0, limit=50, supplier_id=None):
        term = search.strip().casefold()
        for _ in range(3):
            before = self.repo.workspace.get().to_dict()
            query = self.repo.collection("manufacturingRecords").where(
                filter=FieldFilter("status", "==", "in_progress"),
            ).select(SUMMARY_FIELDS)
            rows = [
                row for snapshot in query.stream()
                for row in [snapshot.to_dict()]
                if row.get("currentSupplierId")
                and (not supplier_id or row["currentSupplierId"] == supplier_id)
            ]
            rows.sort(key=lambda row: (
                row["currentSupplierName"].casefold(), row["currentSupplierId"],
                row["productName"].casefold(), row["number"], row["id"],
            ))
            groups = {}
            for row in rows:
                supplier = groups.setdefault(row["currentSupplierId"], {
                    "id": row["currentSupplierId"], "name": row["currentSupplierName"],
                    "jobCount": 0, "products": set(), "matches": False,
                })
                supplier["jobCount"] += 1
                supplier["products"].add(row["productId"])
                supplier["matches"] |= not term or any(
                    term in (row.get(field) or "").casefold() for field in (
                        "currentSupplierName", "productName", "productPartNumber",
                        "currentOperationName", "number", "currentContactName",
                    )
                )
            suppliers = [
                {"id": group["id"], "name": group["name"], "jobCount": group["jobCount"],
                 "productCount": len(group["products"])}
                for group in groups.values() if group["matches"]
            ]
            if before == self.repo.workspace.get().to_dict():
                if supplier_id:
                    return {
                        "items": rows[offset:offset + limit], "total": len(rows),
                        "supplierName": rows[0]["currentSupplierName"] if rows else None,
                        **self.repo.meta(),
                    }
                return {
                    "items": suppliers[offset:offset + limit], "total": len(suppliers),
                    "supplierCount": len(suppliers),
                    "jobCount": sum(group["jobCount"] for group in suppliers),
                    **self.repo.meta(),
                }
        raise Problem("Supplier work changed while loading. Refresh it.", 409, "read_changed")


def manufacturing_records_router(repository):
    router = APIRouter(prefix="/records")

    def service():
        return ManufacturingRecords(repository())

    @router.get("/summary")
    def summary():
        return service().summary()

    @router.get("/supplier-work")
    def supplier_work(
        search: str = Query("", max_length=200),
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
        supplierId: Identifier | None = None,
    ):
        return service().supplier_work(search, offset, limit, supplierId)

    @router.get("/preview/{product_id}")
    def preview(product_id: Identifier):
        return service().preview(product_id)

    @router.get("")
    def listing(
        view: RecordView = "active",
        search: str = Query("", max_length=200),
        productId: Identifier | None = None,
        supplierId: Identifier | None = None,
        operationId: Identifier | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ):
        return service().list(view, search, productId, supplierId, operationId, offset, limit)

    def write(request, action, body):
        return repository().mutate(
            "record:" + action,
            body.id,
            body,
            request.state.actor,
            request.headers.get("x-producttrackr-workspace"),
            request.headers.get("x-producttrackr-generation"),
        )

    @router.post("")
    def create(body: Command, request: Request):
        return write(request, "create", body)

    @router.get("/{record_id}/history")
    def history(record_id: Identifier, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        return service().history(record_id, offset, limit)

    @router.get("/{record_id}")
    def detail(record_id: Identifier):
        return service().detail(record_id)

    @router.post("/{record_id}/{action}")
    def transition(record_id: Identifier, action: RecordAction, body: Command, request: Request):
        if body.id != record_id:
            raise Problem("The manufacturing record identifier does not match this page.")
        return write(request, action, body)

    return router
