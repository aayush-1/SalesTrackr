from fastapi import APIRouter, Query
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.field_path import FieldPath

from .domain import Identifier, Problem


def inventory_router(get_repo):
    router = APIRouter(prefix="/inventory")

    def report(shortages, offset, limit):
        repo = get_repo()
        for _ in range(3):
            before = repo.workspace.get().to_dict()
            query = (
                repo.collection("products")
                .where(
                    filter=FieldFilter("hasShortage" if shortages else "belowMinimum", "==", True),
                )
                .order_by("nameKey")
                .order_by("__name__")
            )
            total = query.count().get()[0][0].value
            items = []
            for snapshot in query.offset(offset).limit(limit).stream():
                product = snapshot.to_dict()
                jobs = (
                    repo.collection("manufacturingRecords")
                    .where(
                        filter=FieldFilter("productId", "==", product["id"]),
                    )
                    .where(filter=FieldFilter("active", "==", True))
                )
                item = {
                    **product,
                    "missingQuantity": product["shortageQuantity"]
                    if shortages
                    else product["minimumStockQuantity"] - product["stockQuantity"],
                    "openJobs": [
                        {key: row.to_dict().get(key) for key in ("id", "number", "quantity")}
                        for row in jobs.limit(5).stream()
                    ],
                    "openJobCount": jobs.count().get()[0][0].value,
                }
                if shortages:
                    demand = repo.collection("inventorySources").where(
                        filter=FieldFilter(
                            FieldPath("demand", product["id"]).to_api_repr(),
                            ">",
                            0,
                        )
                    )
                    item["salesCount"] = demand.count().get()[0][0].value
                    item["sales"] = [
                        {
                            "recordId": row.to_dict()["sourceId"],
                            "quantity": row.to_dict()["demand"][product["id"]],
                        }
                        for row in demand.limit(5).stream()
                    ]
                items.append(item)
            after = repo.workspace.get().to_dict()
            if all(before[key] == after[key] for key in ("generation", "sequence")):
                return {"items": items, "total": total, **repo.meta()}
        raise Problem("Stock changed while loading. Refresh and retry.", 409, "read_changed")

    @router.get("/low-stock")
    def low_stock(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return report(False, offset, limit)

    @router.get("/order-shortages")
    def shortages(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return report(True, offset, limit)

    @router.get("/products/{product_id}/demand")
    def demand(
        product_id: Identifier,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
    ):
        for _ in range(3):
            repo = get_repo()
            repo.read("products", product_id)
            query = repo.collection("inventorySources").where(
                filter=FieldFilter(FieldPath("demand", product_id).to_api_repr(), ">", 0),
            )
            sales = repo.client.collection("workspaces").document(repo.settings.firestore_workspace)
            before = sales.get().to_dict()
            stock_before = repo.workspace.get().to_dict()
            if stock_before["generation"] != repo.control["generation"]:
                continue
            namespace = sales.collection("datasets").document(before["activeDataset"])
            sources = [row.to_dict() for row in query.offset(offset).limit(limit).stream()]
            records = {
                row.id: row.to_dict() for row in repo.client.get_all(
                    [namespace.collection("entities").document(source["sourceId"]) for source in sources],
                    field_paths=["accountId", "kind"],
                ) if row.exists
            } if sources else {}
            account_ids = {row["accountId"] for row in records.values() if row.get("accountId")}
            accounts = {
                row.id: row.to_dict() for row in repo.client.get_all(
                    [namespace.collection("entities").document(key) for key in account_ids],
                    field_paths=["name", "kind"],
                ) if row.exists
            } if account_ids else {}
            total = query.count().get()[0][0].value
            after = sales.get().to_dict()
            stock_after = repo.workspace.get().to_dict()
            if any(
                old[key] != new[key]
                for old, new in ((before, after), (stock_before, stock_after))
                for key in ("generation", "sequence")
            ):
                continue
            items = []
            for source in sources:
                record = records.get(source["sourceId"], {})
                account = accounts.get(record.get("accountId"), {})
                if record.get("kind") != "record" or account.get("kind") != "account" or not account.get(
                    "name",
                ):
                    raise Problem(
                        "A contributing sale or company is unavailable. Refresh the sales record.",
                        409, "reference_changed",
                    )
                items.append({
                    "recordId": source["sourceId"], "companyName": account["name"],
                    "quantity": source["demand"][product_id],
                })
            return {"items": items, "total": total, **repo.meta()}
        raise Problem("Sales changed while loading. Refresh and retry.", 409, "read_changed")

    @router.get("/products/{product_id}/movements")
    def movements(
        product_id: Identifier,
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
    ):
        repo = get_repo()
        repo.read("products", product_id)
        query = (
            repo.collection("stockMovements")
            .where(
                filter=FieldFilter("productId", "==", product_id),
            )
            .order_by("productVersion", direction="DESCENDING")
            .order_by("__name__", direction="DESCENDING")
        )
        items = [row.to_dict() for row in query.offset(offset).limit(limit).stream()]
        sales = repo.client.collection("workspaces").document(repo.settings.firestore_workspace)
        control = sales.get().to_dict()
        for item in items:
            if item["source"] == "sale":
                ref = (
                    sales.collection("datasets")
                    .document(control["activeDataset"])
                    .collection(
                        "entities",
                    )
                    .document(item["sourceId"])
                )
                item["sourceAvailable"] = ref.get().exists
            elif item["source"] == "manufacturing":
                item["sourceAvailable"] = repo.ref("manufacturingRecords", item["sourceId"]).get().exists
        return {
            "items": items,
            "total": query.count().get()[0][0].value,
            **repo.meta(),
        }

    return router
