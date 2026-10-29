"""Stock effects planned inside the same transaction as their source command."""

from collections import defaultdict
from datetime import datetime
from typing import Literal

from pydantic import Field, StrictInt, TypeAdapter, model_validator

from .domain import Identifier, Model, PartQuantity, Problem, StockQuantity, Text, now
from .storage import digest, encode


class InventorySource(Model):
    id: Identifier
    source: Literal["sale", "manufacturing"]
    sourceId: Identifier
    sourceVersion: str
    posted: dict[Identifier, StrictInt]
    demand: dict[Identifier, PartQuantity]
    updatedAt: str
    updatedBy: str

    @model_validator(mode="after")
    def signs(self):
        if any(
            type(value) is not int or (value >= 0 if self.source == "sale" else value <= 0)
            for value in self.posted.values()
        ):
            raise ValueError("Invalid posted stock quantities.")
        if self.source == "manufacturing" and self.demand:
            raise ValueError("Manufacturing is not sales demand.")
        if self.id != source_key(self.source, self.sourceId):
            raise ValueError("Invalid inventory source identity.")
        return self


class StockMovement(Model):
    id: Identifier
    productId: Identifier
    source: Literal["sale", "manufacturing", "manual", "opening"]
    sourceId: Identifier
    writeId: Identifier
    beforeQuantity: StockQuantity | None
    afterQuantity: StockQuantity
    delta: StrictInt | None
    actor: str
    reason: Text = ""
    recordedAt: str
    productVersion: int = Field(strict=True, ge=1)
    minimumBefore: StockQuantity | None = None
    minimumAfter: StockQuantity | None = None

    @model_validator(mode="after")
    def arithmetic(self):
        if self.delta != (
            self.afterQuantity - self.beforeQuantity if self.beforeQuantity is not None else None
        ):
            raise ValueError("Invalid stock movement arithmetic.")
        if self.beforeQuantity is None and self.source not in ("manual", "opening"):
            raise ValueError("Stock must be initialized before posting a source.")
        if self.id != digest([self.source, self.sourceId, self.writeId, self.productId]):
            raise ValueError("Invalid stock movement identity.")
        return self


def source_key(kind, entity_id):
    return digest([kind, entity_id])


def sale_quantities(data):
    totals = defaultdict(int)
    for line in data.get("parts", []):
        if line.get("quantity") is None:
            raise Problem("Enter a quantity for every sales part.")
        totals[line["productId"]] += line["quantity"]
    sent = "dispatched" in data["stageDates"]
    return (
        {key: -value for key, value in totals.items()} if sent else {},
        dict(totals) if not sent and not data.get("discarded") else {},
    )


def checked_inventory(data):
    demand, effects, by_product = defaultdict(int), defaultdict(lambda: defaultdict(int)), defaultdict(list)
    for key, row in data["inventorySources"].items():
        value = InventorySource.model_validate(row)
        if value.id != key or datetime.fromisoformat(value.updatedAt).tzinfo is None:
            raise ValueError("Invalid inventory source metadata.")
        for product_id, quantity in value.demand.items():
            demand[product_id] += quantity
        if (set(value.posted) | set(value.demand)) - set(data["products"]):
            raise ValueError("Inventory source refers to a missing product.")
    for key, row in data["stockMovements"].items():
        value = StockMovement.model_validate(row)
        if value.id != key or datetime.fromisoformat(value.recordedAt).tzinfo is None:
            raise ValueError("Invalid movement metadata.")
        if value.productId not in data["products"]:
            raise ValueError("A stock movement refers to a missing product.")
        by_product[value.productId].append(value)
        if value.source in ("sale", "manufacturing"):
            source_id = source_key(value.source, value.sourceId)
            if source_id not in data["inventorySources"]:
                raise ValueError("A stock movement has no retained source.")
            effects[source_id][value.productId] += value.delta
        elif value.sourceId != value.productId:
            raise ValueError("Invalid manual stock source.")
    for key, state in data["inventorySources"].items():
        if state["posted"] != {product: amount for product, amount in effects[key].items() if amount}:
            raise ValueError("The posted quantities do not match their movement history.")
    for product_id, product in data["products"].items():
        if product.get("requiredQuantity", 0) != demand[product_id]:
            raise ValueError("Sales demand does not match its retained sources.")
        chain = sorted(by_product[product_id], key=lambda value: value.productVersion)
        if chain:
            if chain[0].beforeQuantity is not None or len({row.productVersion for row in chain}) != len(
                chain
            ):
                raise ValueError("Missing or repeated stock initialization.")
            for previous, current in zip(chain, chain[1:]):
                if current.beforeQuantity != previous.afterQuantity:
                    raise ValueError("Stock history is discontinuous.")
            if (
                chain[-1].afterQuantity != product.get("stockQuantity")
                or chain[-1].productVersion > product["version"]
            ):
                raise ValueError("Stock does not match its last movement.")
        elif product.get("stockQuantity") is not None:
            raise ValueError("Stock is missing its opening history.")
        if product.get("stockQuantity") is not None:
            projected = project_stock(product)
            if any(
                product.get(key) != projected[key]
                for key in ("requiredQuantity", "belowMinimum", "shortageQuantity", "hasShortage")
            ):
                raise ValueError("Stock projections are inconsistent.")
    for record in data["manufacturingRecords"].values():
        state = data["inventorySources"].get(source_key("manufacturing", record["id"]))
        if record.get("inventoryTracked") or state:
            expected = {record["productId"]: record["quantity"]} if record["status"] == "completed" else {}
            if not record.get("inventoryTracked") or not state or state["posted"] != expected:
                raise ValueError("Manufacturing output does not match its stock source.")


def project_stock(product):
    stock, minimum = product.get("stockQuantity"), product.get("minimumStockQuantity")
    required = product.get("requiredQuantity", 0)
    return {
        **product,
        "requiredQuantity": required,
        "belowMinimum": stock is not None and minimum is not None and stock < minimum,
        "shortageQuantity": max(required - stock, 0) if stock is not None else 0,
        "hasShortage": stock is not None and required > stock,
    }


def check_budget(writes, extra_writes=0):
    if (
        len(writes) + extra_writes > 450
        or sum(len(encode(value)) + len(reference.path) + 1024 for reference, value in writes)
        > 4 * 1024 * 1024
    ):
        raise Problem(
            "Too many product changes for one save. Make smaller corrections.", 422, "stock_save_size"
        )


class Inventory:
    def __init__(self, repository):
        self.repo = repository

    def control(self, tx):
        control = self.repo.workspace.get(transaction=tx).to_dict()
        if any(control[key] != self.repo.control[key] for key in ("generation", "activeDataset")):
            raise Problem("ProductTrackr changed. Reload before saving.", 409, "workspace_changed")
        if control.get("maintenance") or control.get("pendingRecordCleanup"):
            raise Problem("ProductTrackr maintenance is in progress.", 503, "maintenance")
        return control

    def movement(self, product, after, *, kind, source_id, write_id, actor, reason, minimum=None):
        before = product.get("stockQuantity")
        movement_id = digest([kind, source_id, write_id, product["id"]])
        value = StockMovement(
            id=movement_id,
            productId=product["id"],
            source=kind,
            sourceId=source_id,
            writeId=write_id,
            beforeQuantity=before,
            afterQuantity=after,
            delta=after - before if before is not None else None,
            actor=actor,
            reason=reason,
            recordedAt=now(),
            productVersion=product["version"] + (kind != "opening"),
            minimumBefore=product.get("minimumStockQuantity") if kind != "opening" else None,
            minimumAfter=minimum if minimum is not None else product.get("minimumStockQuantity"),
        ).model_dump()
        return self.repo.ref("stockMovements", movement_id), value

    def product_change(self, product, stock, required, actor, minimum=None):
        TypeAdapter(StockQuantity).validate_python(stock)
        if required < 0:
            raise Problem("The stock demand index is inconsistent.", 503, "inventory_index")
        fields = {"stockQuantity": stock, "requiredQuantity": required}
        if minimum is not None:
            fields["minimumStockQuantity"] = minimum
        updated = self.repo.changed(product, fields, actor, product["id"])
        return project_stock(updated)

    def reconcile(
        self, tx, *, kind, source_id, source_version, posted, demand, create, write_id, actor, reason
    ):
        key = source_key(kind, source_id)
        ref = self.repo.ref("inventorySources", key)
        old = ref.get(transaction=tx).to_dict()
        if not old and not create:
            raise Problem(
                "Inventory source state is missing. Restore a complete backup.", 503, "inventory_source"
            )
        if create and old:
            raise Problem("This inventory source already exists.", 409, "record_exists")
        previous, previous_demand = (old or {}).get("posted", {}), (old or {}).get("demand", {})
        if previous == posted and previous_demand == demand and old:
            return []
        if previous and posted != previous and not reason.strip():
            raise Problem("Enter a reason for correcting posted quantities.")
        products = {}
        for product_id in sorted(set(previous) | set(posted) | set(previous_demand) | set(demand)):
            products[product_id] = self.repo.read("products", product_id, tx)
        writes = []
        for product_id, product in products.items():
            if product.get("stockQuantity") is None or product.get("minimumStockQuantity") is None:
                raise Problem(
                    f"Set stock and minimum quantities for {product['name']} first.", 422, "stock_not_set"
                )
            delta = posted.get(product_id, 0) - previous.get(product_id, 0)
            demand_delta = demand.get(product_id, 0) - previous_demand.get(product_id, 0)
            if not delta and not demand_delta:
                continue
            after = product["stockQuantity"] + delta
            if after < 0:
                raise Problem(
                    f"Insufficient stock for {product['name']}: {product['stockQuantity']} in stock; "
                    f"{-delta} required for this change.",
                    409,
                    "insufficient_stock",
                )
            updated = self.product_change(
                product,
                after,
                product.get("requiredQuantity", 0) + demand_delta,
                actor,
            )
            writes.append((self.repo.ref("products", product_id), updated))
            if delta:
                writes.append(
                    self.movement(
                        product,
                        after,
                        kind=kind,
                        source_id=source_id,
                        write_id=write_id,
                        actor=actor,
                        reason=reason,
                    )
                )
        state = InventorySource(
            id=key,
            source=kind,
            sourceId=source_id,
            sourceVersion=str(source_version),
            posted=posted,
            demand=demand,
            updatedAt=now(),
            updatedBy=actor,
        ).model_dump()
        writes.append((ref, state))
        return writes

    def sale(self, tx, revision):
        data = revision["data"]
        create = not revision["parents"] and revision["action"] == "create_record"
        tracked = data.get("inventoryTracked", False)
        if not tracked:
            return []
        posted, demand = sale_quantities(data)
        return self.reconcile(
            tx,
            kind="sale",
            source_id=revision["entityId"],
            source_version=revision["id"],
            posted=posted,
            demand=demand,
            create=create,
            write_id=revision["id"],
            actor=revision["actor"],
            reason=revision["reason"],
        )

    def manufacturing(self, tx, record, body, actor, create, reason):
        if not record.get("inventoryTracked"):
            return []
        posted = {record["productId"]: record["quantity"]} if record["status"] == "completed" else {}
        # A new unfinished job still needs an initialized product, despite posting no output yet.
        if create:
            product = self.repo.read("products", record["productId"], tx)
            if product.get("stockQuantity") is None or product.get("minimumStockQuantity") is None:
                raise Problem("Set this product's stock and minimum quantities before creating a job.")
        return self.reconcile(
            tx,
            kind="manufacturing",
            source_id=record["id"],
            source_version=record["version"],
            posted=posted,
            demand={},
            create=create,
            write_id=body.writeId,
            actor=actor,
            reason=reason,
        )

    def manual(self, product, data, body, actor):
        after = data.stockQuantity
        updated = self.product_change(
            product,
            after,
            product.get("requiredQuantity", 0),
            actor,
            data.minimumStockQuantity,
        )
        writes = [(self.repo.ref("products", product["id"]), updated)]
        if (
            product.get("stockQuantity") != after
            or product.get("minimumStockQuantity") != data.minimumStockQuantity
        ):
            writes.append(
                self.movement(
                    product,
                    after,
                    kind="manual",
                    source_id=product["id"],
                    write_id=body.writeId,
                    actor=actor,
                    reason=data.reason,
                    minimum=data.minimumStockQuantity,
                )
            )
        return writes
