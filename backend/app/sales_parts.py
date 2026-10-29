"""Account machines and independently retained, revisioned delivery facts."""

from contextlib import contextmanager
from datetime import date

from fastapi import APIRouter, Query, Request, Response
from google.cloud.firestore_v1.base_query import FieldFilter

from .domain import (
    AccountData,
    AccountMachine,
    Identifier,
    MachineModelData,
    Model,
    Name,
    PartQuantity,
    PartsChange,
    Problem,
    RecordData,
    Revision,
    SalePart,
    SentPartData,
    Text,
    Write,
)
from .storage import digest


def source_entry_id(record_id, line_id):
    return "sent_" + digest([record_id, line_id])


class References:
    def __init__(self, repo):
        self.repo, self.expected, self.cache = repo, {}, {}
        self.catalog_repo = None

    def expect(self, ref, fields):
        self.expected[ref.path] = (ref, fields)

    def sales(self, entity_id, kind):
        if (entity_id, kind) in self.cache:
            return self.cache[(entity_id, kind)]
        row = self.repo.current(entity_id, kind)
        self.expect(
            self.repo.namespace.collection("entities").document(entity_id), {"heads": [row["id"]]},
        )
        self.cache[(entity_id, kind)] = row["data"]
        return row["data"]

    def catalog(self):
        from .manufacturing_storage import ManufacturingRepository

        if self.catalog_repo is not None:
            return self.catalog_repo
        if not self.repo.settings.manufacturing_enabled:
            raise Problem("Connect the shared ProductTrackr catalog to use parts.", 503, "parts_unavailable")
        catalog = ManufacturingRepository(self.repo.settings, client=self.repo.client)
        self.expect(catalog.workspace, {
            "generation": catalog.control["generation"],
            "activeDataset": catalog.control["activeDataset"], "maintenance": None,
        })
        if catalog.control.get("maintenance"):
            raise Problem("ProductTrackr maintenance is in progress.", 503, "maintenance")
        self.catalog_repo = catalog
        return catalog

    def snapshot(self, account_id, product_id, machine_id, *, validate=True):
        account = self.sales(account_id, "account")
        catalog = self.catalog()
        ref = catalog.ref("products", product_id)
        if (product_id, "product") not in self.cache:
            product = catalog.required(ref.get())
            self.expect(ref, {"version": product["version"]})
            self.cache[(product_id, "product")] = product
        product = self.cache[(product_id, "product")]
        result = {
            "accountId": account_id, "accountName": account["name"],
            "productId": product_id, "productName": product["name"],
            "productPartNumber": product.get("partNumber", ""), "machineId": machine_id,
        }
        if machine_id:
            machine = next(
                (m for m in account.get("machines", []) if m["id"] == machine_id), None,
            )
            if not machine:
                raise Problem("Choose a machine belonging to this account.")
            model = self.sales(machine["modelId"], "model")
            if validate and product.get("modelIds") and machine["modelId"] not in product["modelIds"]:
                raise Problem("This product is not compatible with the selected machine's model.")
            result.update(
                machineName=machine["name"], machineModelId=machine["modelId"],
                machineModelName=model["name"], serialNumber=machine.get("serialNumber", ""),
                manufacturingYear=machine.get("manufacturingYear"),
            )
        return result


def prepare_delivery_write(repo, revision):
    """Prepare immutable versions outside the transaction; return reads to recheck before commit."""
    refs, extras = References(repo), []
    kind, data = revision["entityType"], revision["data"]
    parents = [repo._revision(key) for key in revision["parents"]]
    old = parents[0]["data"] if len(parents) == 1 else {}
    if kind == "account":
        for machine in data.get("machines", []):
            refs.sales(machine["modelId"], "model")
    elif kind == "sent_part":
        if data["source"] != "manual" or old.get("source", "manual") != "manual":
            raise Problem("Correct sale-generated deliveries through their sales record.")
        changed = any(old.get(key) != data.get(key) for key in ("accountId", "productId", "machineId"))
        if old and not revision["reason"].strip():
            raise Problem("Enter a reason for correcting or voiding this delivery.")
        if not old or changed:
            data = SentPartData.model_validate({
                **data,
                **refs.snapshot(data["accountId"], data["productId"], data.get("machineId")),
            }).model_dump(mode="json")
        else:
            data = {
                **old, "sentOn": data["sentOn"], "active": data["active"],
                "quantity": data.get("quantity"),
            }
    elif kind == "record":
        record = RecordData.model_validate(data)
        published = bool(record.stageDates.get("closed")) or any(
            parent["data"].get("partsPublished") or parent["data"]["stageDates"].get("closed")
            for parent in parents
        )
        data = {**data, "partsPublished": published}
        lines = {line.id: line.model_dump(mode="json") for line in record.parts}
        old_lines = {
            part["id"]: SalePart.model_validate(part).model_dump(mode="json")
            for parent in parents for part in parent["data"].get("parts", [])
        }
        changed = lines != old_lines or bool(lines or old_lines) and any(
            parent["data"]["accountId"] != data["accountId"] for parent in parents
        )
        if published and changed and parents and not revision["reason"].strip():
            # First publication is not a correction.
            if old.get("partsPublished") or old.get("stageDates", {}).get("closed"):
                raise Problem("Enter a reason for correcting delivered parts.")
        for line_id in sorted(set(lines) | set(old_lines)):
            line = lines.get(line_id)
            entry_id = source_entry_id(revision["entityId"], line_id)
            entry_ref = repo.namespace.collection("entities").document(entry_id)
            entry = entry_ref.get().to_dict()
            if entry and (entry["kind"] != "sent_part" or entry["conflicted"]):
                raise Problem("The linked delivery history requires review.", 409, "record_conflict")
            previous = repo._revision(entry["heads"][0]) if entry else None
            previous_data = previous["data"] if previous else None
            snapshot = None
            if line and (line != old_lines.get(line_id) or published and not previous):
                snapshot = refs.snapshot(data["accountId"], line["productId"], line.get("machineId"))
            if not published:
                continue
            active = bool(line and record.stageDates.get("delivered"))
            if not previous and not active:
                continue
            if active:
                same_assignment = previous_data and all(
                    previous_data.get(key) == value for key, value in {
                        "accountId": data["accountId"], "productId": line["productId"],
                        "machineId": line.get("machineId"),
                    }.items()
                )
                fields = previous_data if same_assignment else snapshot or refs.snapshot(
                    data["accountId"], line["productId"], line.get("machineId"),
                )
                updated = SentPartData.model_validate({
                    **fields, "sentOn": record.stageDates["delivered"], "source": "sale",
                    "sourceRecordId": revision["entityId"], "sourceLineId": line_id, "active": True,
                    "quantity": line.get("quantity"),
                }).model_dump(mode="json")
            else:
                updated = {**previous_data, "active": False}
            if previous_data and SentPartData.model_validate(updated) == SentPartData.model_validate(
                previous_data,
            ):
                continue
            entry_revision = Revision(
                id=digest([revision["id"], entry_id]), entityId=entry_id, entityType="sent_part",
                parents=[previous["id"]] if previous else [], createdAt=revision["createdAt"],
                actor=revision["actor"], action="sync_sale_delivery", reason=revision["reason"],
                fingerprint=digest([revision["fingerprint"], entry_id, updated]), data=updated,
            ).model_dump(mode="json")
            extras.append(entry_revision)
    return {**revision, "data": data}, extras, list(refs.expected.values())


class ManualDelivery(Model):
    accountId: Identifier
    productId: Identifier
    machineId: Identifier | None = None
    sentOn: date
    reason: Text = ""
    acknowledgeDuplicate: bool = False
    quantity: PartQuantity | None = None


class VoidDelivery(Model):
    reason: Name


def sales_parts_router(repo, settings, mutate):
    router = APIRouter(prefix="/api")

    def ready():
        if settings.storage_backend != "firestore" or not settings.manufacturing_enabled:
            raise Problem(
                "This feature requires the shared ProductTrackr workspace.", 503, "parts_unavailable",
            )
        repo.sync()

    def page(kind, offset=0, limit=200):
        query = repo._entities(kind).order_by("nameSort").order_by("entityId")
        return {
            "items": [repo._revision(item.to_dict()["heads"][0])
                      for item in query.offset(offset).limit(limit).stream()],
            "total": repo._count(query), **repo.metadata(),
        }

    @router.get("/models")
    def models(offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=200)):
        ready()
        return page("model", offset, limit)

    @router.post("/models")
    def create_model(body: Write[MachineModelData], request: Request):
        ready()
        return mutate("model", body, "create_model", request, lambda _: body.data, create=True)

    @router.patch("/models/{entity_id}")
    def edit_model(entity_id: Identifier, body: Write[MachineModelData], request: Request):
        ready()
        if entity_id != body.entityId:
            raise Problem("The model identifier does not match.")
        return mutate("model", body, "edit_model", request, lambda _: body.data)

    @router.put("/accounts/{entity_id}/machines/{machine_id}")
    def save_machine(
        entity_id: Identifier, machine_id: Identifier, body: Write[AccountMachine], request: Request,
    ):
        ready()
        if entity_id != body.entityId or machine_id != body.data.id:
            raise Problem("The machine/account identifier does not match.")

        def transform(old):
            machines = [m for m in old.get("machines", []) if m["id"] != machine_id]
            return AccountData.model_validate({
                **old, "machines": [*machines, body.data.model_dump(mode="json")],
            })

        return mutate("account", body, "save_machine", request, transform)

    @router.patch("/records/{entity_id}/parts")
    def save_parts(entity_id: Identifier, body: Write[PartsChange], request: Request):
        ready()
        if entity_id != body.entityId:
            raise Problem("The sales record identifier does not match.")
        return mutate("record", body, "edit_parts", request, lambda old: RecordData.model_validate({
            **old, "parts": [part.model_dump(mode="json") for part in body.data.parts],
        }))

    def catalog(model_id=None, search="", offset=0, limit=200):
        from .manufacturing_storage import ManufacturingRepository

        products = ManufacturingRepository(settings, client=repo.client)
        query = products.collection("products")
        if model_id:
            query = query.where(filter=FieldFilter("modelScopes", "array_contains_any", [model_id, "*"]))
        query = query.order_by("nameKey").order_by("__name__")
        if search:
            rows = [row.to_dict() for row in query.stream()]
            rows = [row for row in rows
                    if search.casefold() in f"{row['name']} {row.get('partNumber', '')}".casefold()]
            selected, total = rows[offset:offset + limit], len(rows)
        else:
            selected = [row.to_dict() for row in query.offset(offset).limit(limit).stream()]
            total = repo._count(query)
        return {"items": selected, "total": total, **repo.metadata(), "catalog": products.meta()}

    @router.get("/parts/catalog")
    def product_catalog(
        modelId: Identifier | None = None, search: str = Query("", max_length=200),
        offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=200),
    ):
        ready()
        if modelId:
            repo.current(modelId, "model")
        return catalog(modelId, search, offset, limit)

    def history_query(account_id=None, start=None, end=None, include_voided=False):
        query = repo._entities("sent_part")
        if not include_voided:
            query = query.where(filter=FieldFilter("active", "==", True))
        if account_id:
            query = query.where(filter=FieldFilter("accountId", "==", account_id))
        if start:
            query = query.where(filter=FieldFilter("sentOn", ">=", start.isoformat()))
        if end:
            query = query.where(filter=FieldFilter("sentOn", "<=", end.isoformat()))
        return query.order_by("sentOn", direction="DESCENDING").order_by("entityId")

    def machine(account_id, machine_id):
        account = repo.current(account_id, "account")["data"]
        row = next((m for m in account.get("machines", []) if m["id"] == machine_id), None)
        if not row:
            raise Problem("Choose a machine belonging to this account.")
        return row

    def compatible_ids(model_id):
        ids, offset = set(), 0
        while True:
            result = catalog(model_id, offset=offset)
            ids.update(row["id"] for row in result["items"])
            offset += len(result["items"])
            if offset >= result["total"]:
                return ids
            if not result["items"]:
                raise Problem("The catalog changed. Refresh and retry.", 409, "catalog_changed")

    def history_data(accountId=None, machineId="", start=None, end=None, includeVoided=False,
                     offset=0, limit=50):
        if start and end and start > end:
            raise Problem("The start date must not follow the end date.")
        query = history_query(accountId, start, end, includeVoided)
        if machineId:
            if not accountId:
                raise Problem("Select an account before filtering machines.")
            if machineId == "unassigned":
                query = query.where(filter=FieldFilter("machineId", "==", ""))
                rows = [r.to_dict() for r in query.offset(offset).limit(limit).stream()]
                total = repo._count(query)
            else:
                selected = machine(accountId, machineId)
                eligible = compatible_ids(selected["modelId"])
                query = query.where(filter=FieldFilter("machineId", "in", [machineId, ""]))
                matches = [
                    r.to_dict() for r in query.stream()
                    if r.to_dict()["machineId"] == machineId or r.to_dict()["productId"] in eligible
                ]
                rows, total = matches[offset:offset + limit], len(matches)
        else:
            rows = [r.to_dict() for r in query.offset(offset).limit(limit).stream()]
            total = repo._count(query)
        items = [repo._revision(row["heads"][0]) for row in rows]
        source_ids = {
            row["data"]["sourceRecordId"] for row in items if row["data"]["source"] == "sale"
        }
        sources = {
            snapshot.id for snapshot in repo.client.get_all(
                [repo.namespace.collection("entities").document(key) for key in source_ids],
                field_paths=["kind"],
            ) if snapshot.exists and snapshot.to_dict().get("kind") == "record"
        } if source_ids else set()
        return {
            "items": [
                {**row, "sourceRecordAvailable": row["data"]["sourceRecordId"] in sources}
                if row["data"]["source"] == "sale" else row for row in items
            ],
            "total": total, **repo.metadata(),
        }

    @router.get("/parts/history")
    def history(
        accountId: Identifier | None = None, machineId: str = "", start: date | None = None,
        end: date | None = None, includeVoided: bool = False,
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
    ):
        ready()
        return history_data(accountId, machineId, start, end, includeVoided, offset, limit)

    @contextmanager
    def pdf_snapshot():
        ready()
        products = References(repo).catalog()
        stores = (repo, products)
        before = [store.workspace.get().to_dict() for store in stores]
        if any(control.get("maintenance") for control in before):
            raise Problem("Storage maintenance is in progress. Please retry shortly.", 503, "maintenance")
        if any(control[key] != store.control[key] for store, control in zip(stores, before)
               for key in ("generation", "activeDataset")):
            raise Problem("The workspace changed. Refresh and download again.", 409, "report_changed")
        yield
        after = [store.workspace.get().to_dict() for store in stores]
        if any(old[key] != new[key] for old, new in zip(before, after)
               for key in ("generation", "sequence", "activeDataset")) or any(
                   control.get("maintenance") for control in after):
            raise Problem("Parts changed while preparing the PDF. Download again.", 409, "report_changed")

    def pdf_response(content, filename):
        return Response(content, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store",
        })

    @router.get("/parts/history/pdf")
    def history_pdf(
        accountId: Identifier | None = None, machineId: str = "", start: date | None = None,
        end: date | None = None, includeVoided: bool = False,
    ):
        from .parts_pdf import MAX_REPORT_ROWS, sales_history_pdf

        with pdf_snapshot():
            result = history_data(accountId, machineId, start, end, includeVoided, 0, MAX_REPORT_ROWS + 1)
            if result["total"] > MAX_REPORT_ROWS:
                raise Problem("Too many deliveries for one PDF. Narrow the filters.", 413, "report_size")
            account_name = repo.current(accountId, "account")["data"]["name"] if accountId else "All accounts"
            machine_name = "All machines"
            if machineId == "unassigned":
                machine_name = "Machine not specified"
            elif machineId:
                selected = machine(accountId, machineId)
                machine_name = " - ".join(filter(None, [selected["name"], selected.get("serialNumber")]))
            filters = [
                f"Account: {account_name}", f"Machine: {machine_name}",
                f"From: {start.isoformat() if start else 'Any date'}",
                f"To: {end.isoformat() if end else 'Any date'}",
                f"Include withdrawn entries: {'Yes' if includeVoided else 'No'}",
            ]
            return pdf_response(sales_history_pdf(filters, result["items"]), "sales-history.pdf")

    @router.get("/parts/catalog/pdf")
    def catalog_pdf(modelId: Identifier):
        from .parts_pdf import MAX_REPORT_ROWS, model_parts_pdf

        with pdf_snapshot():
            model = repo.current(modelId, "model")["data"]
            result = catalog(modelId, limit=MAX_REPORT_ROWS + 1)
            if result["total"] > MAX_REPORT_ROWS:
                raise Problem("Too many products for one PDF.", 413, "report_size")
            return pdf_response(model_parts_pdf(model["name"], result["items"]), "master-products-list.pdf")

    def coverage_data(account_id, machine_id, offset, limit):
        selected = machine(account_id, machine_id)
        result = catalog(selected["modelId"], offset=offset, limit=limit)
        ids = {row["id"] for row in result["items"]}
        query = history_query(account_id).where(
            filter=FieldFilter("machineId", "in", [machine_id, ""]),
        )
        latest = {}
        for snapshot in query.stream():
            row = snapshot.to_dict()
            if row["productId"] in ids:
                latest.setdefault(row["productId"], row)
        return {
            **result, "machine": selected,
            "items": [{
                **product, "lastSentOn": latest.get(product["id"], {}).get("sentOn"),
                "deliveryId": latest.get(product["id"], {}).get("entityId"),
                "machineSpecified": bool(latest.get(product["id"], {}).get("machineId")),
            } for product in result["items"]],
        }

    @router.get("/parts/coverage")
    def coverage(
        accountId: Identifier, machineId: Identifier,
        offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
    ):
        ready()
        return coverage_data(accountId, machineId, offset, limit)

    @router.get("/parts/coverage/pdf")
    def coverage_pdf(accountId: Identifier, machineId: Identifier):
        from .parts_pdf import MAX_REPORT_ROWS, machine_parts_pdf

        with pdf_snapshot():
            result = coverage_data(accountId, machineId, 0, MAX_REPORT_ROWS + 1)
            if result["total"] > MAX_REPORT_ROWS:
                raise Problem("This machine has too many compatible parts for one PDF.", 413, "report_size")
            account = repo.current(accountId, "account")["data"]
            model = repo.current(result["machine"]["modelId"], "model")["data"]
            content = machine_parts_pdf(account, result["machine"], model["name"], result["items"])
            return pdf_response(content, "potential-spares-machine-wise.pdf")

    def manual_transform(body, old=None):
        if old and old["source"] != "manual":
            raise Problem("Correct this delivery through its sales record.")
        if not body.acknowledgeDuplicate:
            query = history_query(body.accountId, body.sentOn, body.sentOn)
            if any(
                row.to_dict()["productId"] == body.productId
                and row.to_dict()["machineId"] == (body.machineId or "")
                for row in query.stream()
            ) and (not old or any(
                old.get(key) != getattr(body, key) for key in ("accountId", "productId", "machineId")
            ) or old["sentOn"] != body.sentOn.isoformat()):
                raise Problem(
                    "A delivery with this product, machine and date already exists. "
                    "Confirm if this is a separate delivery.", 409, "duplicate_delivery",
                )
        same = old and all(old.get(key) == getattr(body, key) for key in (
            "accountId", "productId", "machineId",
        ))
        return SentPartData.model_validate({
            **(old if same else References(repo).snapshot(
                body.accountId, body.productId, body.machineId,
            )),
            "sentOn": body.sentOn, "source": "manual", "active": (old or {}).get("active", True),
            "quantity": body.quantity,
        })

    @router.post("/parts/history")
    def add_delivery(body: Write[ManualDelivery], request: Request):
        ready()
        return mutate("sent_part", body, "create_manual_delivery", request,
                      lambda _: manual_transform(body.data), create=True)

    @router.patch("/parts/history/{entity_id}")
    def edit_delivery(entity_id: Identifier, body: Write[ManualDelivery], request: Request):
        ready()
        if entity_id != body.entityId:
            raise Problem("The delivery identifier does not match.")
        return mutate("sent_part", body, "correct_manual_delivery", request,
                      lambda old: manual_transform(body.data, old))

    @router.post("/parts/history/{entity_id}/void")
    def void_delivery(entity_id: Identifier, body: Write[VoidDelivery], request: Request):
        ready()
        if entity_id != body.entityId:
            raise Problem("The delivery identifier does not match.")
        return mutate("sent_part", body, "void_manual_delivery", request,
                      lambda old: SentPartData.model_validate({**old, "active": False}))

    return router
