"""Build a guarded, separate inventory preview using normal application commands."""

import json
import os
from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.domain import RecordData, Revision, now, today
from backend.app.inventory import source_key
from backend.app.main import create_app
from backend.app.manufacturing_spreadsheet import SpreadsheetExport
from backend.app.shared_backups import archive, read_archive


def seed():
    settings = Settings.load()
    if (
        settings.mode != "demo"
        or settings.storage_backend != "firestore"
        or not settings.manufacturing_enabled
        or settings.firestore_project != "demo-inventory-local"
        or settings.manufacturing_project != "demo-inventory-local"
        or settings.firestore_workspace != "inventory-sales-local"
        or settings.manufacturing_workspace != "inventory-products-local"
        or settings.manufacturing_backup_storage != "local"
        or os.getenv("FIRESTORE_EMULATOR_HOST") not in ("127.0.0.1:8085", "localhost:8085")
        or os.getenv("GOOGLE_TOKEN_JSON")
    ):
        raise RuntimeError("Use only the dedicated, credential-free localhost inventory preview.")
    settings.validate_manufacturing()
    output = settings.data_dir / "scenarios.json"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.repo
        recovery = client.app.state.recovery
        catalog = recovery.repo.products.bind()
        if repo.meta or any(catalog.collection("products").limit(1).stream()):
            if output.exists():
                print("Existing inventory preview retained; no mock data was overwritten.")
                return
            raise RuntimeError("Preview is not empty. Existing data was preserved.")
        cases = {}
        headers = {"X-SalesTrackr-Request": "1", "X-SalesTrackr-Generation": repo.generation}

        def checked(response, expected=200):
            if response.status_code != expected:
                raise RuntimeError(f"{response.request.url.path}: {response.status_code}: {response.text}")
            return response.json()

        def sales(kind, data, current=None, suffix="", method="POST", expected=200, path=None):
            response = client.request(
                method, path or f"/api/{kind}s" + (f"/{current['entityId']}{suffix}" if current else ""),
                headers=headers, json={
                    "writeId": uuid4().hex, "entityId": current["entityId"] if current else uuid4().hex,
                    "baseRevision": current["id"] if current else None, "data": data,
                },
            )
            result = checked(response, expected)
            return result["revision"] if expected == 200 else result

        def manufacture(path, data, current=None, method="POST", expected=200):
            meta = checked(client.get("/api/manufacturing/meta"))
            entity_id = (uuid4().hex if current is None or path.endswith(("/steps", "/assignments"))
                         else current["id"])
            return checked(client.request(
                method, f"/api/manufacturing{path}", headers={
                    "X-SalesTrackr-Request": "1", "X-ProductTrackr-Workspace": meta["workspaceId"],
                    "X-ProductTrackr-Generation": meta["generation"],
                }, json={
                    "writeId": uuid4().hex, "id": entity_id,
                    "baseVersion": current["version"] if current else None, "data": data,
                },
            ), expected)

        def detail(product):
            return checked(client.get(f"/api/manufacturing/products/{product['id']}"))

        operation = manufacture("/operations", {"name": "Machining"})
        inspection = manufacture("/operations", {"name": "Final inspection"})
        supplier = manufacture("/suppliers", {"name": "Local workshop", "relationship": "internal"})
        models = [sales("model", {"name": name}) for name in ("Lithrone", "Enthrone", "Spica", "Jthrone")]
        accounts, machines = [], []
        for index, name in enumerate((
            "Atlas Printworks", "Bright Page Press", "Cedar Packaging", "Delta Offset",
            "Evergreen Graphics", "Foxtrot Print Services", "No-machine Trading", "Quiet Account",
        )):
            account = sales("account", {
                "name": name, "address": f"Synthetic Industrial Estate, Block {index + 1}",
                "notes": "Synthetic inventory preview account.",
            })
            presses = []
            for position in range(0 if index == 6 else 4 if index == 0 else 2):
                machine = {
                    "id": uuid4().hex, "name": f"Press {position + 1}",
                    "modelId": models[(index + max(0, position - 1)) % len(models)]["entityId"],
                    "serialNumber": f"INV-{index + 1:02}-{position + 1:02}" if position % 2 == 0 else "",
                    "manufacturingYear": 2018 + position if position % 2 == 0 else None,
                }
                account = sales("account", machine, account, f"/machines/{machine['id']}", "PUT")
                presses.append(machine)
            accounts.append(account)
            machines.append(presses)

        products = []

        def product(name, stock, minimum, model_ids=None, process=True):
            created = manufacture("/products", {
                "name": name, "partNumber": f"INV-{len(products) + 1:03}",
                "stockQuantity": stock, "minimumStockQuantity": minimum, "modelIds": model_ids or [],
                "notes": "Synthetic local inventory demonstration product.",
            })
            row = detail(created)["entity"]
            products.append(row)
            if process:
                for source in (operation, inspection):
                    created_step = manufacture(
                        f"/products/{row['id']}/steps", {"operationId": source["id"]},
                        detail(row)["entity"],
                    )
                    source_step = next(
                        item for item in detail(row)["steps"] if item["id"] == created_step["id"]
                    )
                    manufacture(f"/steps/{source_step['id']}/assignments",
                                {"supplierId": supplier["id"]}, source_step)
            return row

        def part(row, quantity, machine=None):
            return {"id": uuid4().hex, "productId": row["id"], "quantity": quantity,
                    "machineId": machine["id"] if machine else None}

        def stage(row, target, reason=""):
            return sales("record", {"stage": target, "reason": reason}, row, "/stage")

        def sale(label, parts, target="order_received", account=0):
            row = sales("record", {
                "accountId": accounts[account]["entityId"], "parts": parts, "productNotes": label,
            })
            for reached in ("follow_up", "order_received", "dispatched", "delivered", "closed"):
                if target == "new_lead":
                    break
                row = (sales("record", {"received": True}, row, "/payment")
                       if reached == "closed" else stage(row, reached))
                if reached == target:
                    break
            cases[label] = row["entityId"]
            return row

        def job(row, quantity, status="ready"):
            preview = checked(client.get(f"/api/manufacturing/records/preview/{row['id']}"))
            saved = manufacture("/records", {
                "productId": row["id"], "sourceToken": preview["sourceToken"], "quantity": quantity,
                "notes": f"Mock {status} job for {row['name']}",
            })
            saved = checked(client.get(f"/api/manufacturing/records/{saved['id']}"))["record"]
            while status in ("in_progress", "completed") and saved["status"] != "completed":
                step_id = saved["operations"][saved["currentIndex"]]["id"]
                manufacture(f"/records/{saved['id']}/assign",
                            {"stepId": step_id, "supplierId": supplier["id"]}, saved)
                saved = checked(client.get(f"/api/manufacturing/records/{saved['id']}"))["record"]
                if status == "in_progress":
                    break
                manufacture(f"/records/{saved['id']}/complete", {"stepId": step_id}, saved)
                saved = checked(client.get(f"/api/manufacturing/records/{saved['id']}"))["record"]
            if status == "cancelled":
                manufacture(f"/records/{saved['id']}/cancel", {"reason": "Mock cancelled job"}, saved)
            return saved

        competing = product("Universal bearing - three competing orders", 10, 5)
        first = sale("First 4 units dispatched", [part(competing, 4, machines[0][0])], "dispatched")
        sale("Second 4 units dispatched", [part(competing, 4, machines[0][1])], "dispatched")
        blocked = sale("Third order needs 4; only 2 remain", [part(competing, 4)])
        sales("record", {"stage": "dispatched"}, blocked, "/stage", expected=409)
        job(competing, 2, "in_progress")
        stage(first, "delivered")

        low = product("Feed roller - below minimum", 1, 8, [models[0]["entityId"]])
        job(low, 7)
        job(low, 2, "cancelled")
        product("Drive belt - exactly at minimum", 5, 5)
        empty = product("Seal kit - no stock or process", 0, 3, process=False)
        sale("Zero stock; process setup needed", [part(empty, 3)], "new_lead", 6)
        common = product("Common filter - healthy stock", 100, 10)
        for index, model in enumerate(models):
            specific = product(f"{model['data']['name']} drive gear", 12, 3, [model["entityId"]])
            sale(f"{model['data']['name']} draft demand", [part(specific, 15)], "new_lead", index)
            sale(f"{model['data']['name']} completed delivery", [part(common, index + 1)],
                 "closed", index)
        product("Shared optical sensor", 6, 2, [model["entityId"] for model in models[:2]])

        for index in range(10):
            row = product(f"Catalog spare {index + 1:02}", index * 2, 5, process=index % 3 != 0)
            sale(f"Open catalog enquiry {index + 1:02}", [part(row, index + 1)],
                 ("new_lead", "follow_up", "order_received")[index % 3], index % len(accounts))

        repeated = product("Repeated machine lines - combined shortage", 6, 2)
        sale("Two presses need 4 each; 2 short", [
            part(repeated, 4, machines[0][0]), part(repeated, 4, machines[0][1]),
        ])
        output_product = product("Manufactured output - already consumed", 0, 0)
        consumed = job(output_product, 6, "completed")
        sale("Completed output has all been dispatched", [part(output_product, 6)], "dispatched", 2)
        manufacture(f"/records/{consumed['id']}/undo", {
            "stepId": consumed["operations"][-1]["id"], "reason": "Mock blocked completion undo",
        }, consumed, expected=409)

        corrected = product("Sent quantity correction - net difference", 10, 2)
        sent = sale("Dispatched 3 corrected to 5", [part(corrected, 3)], "closed", 3)
        sales("record", {"parts": [{**sent["data"]["parts"][0], "quantity": 5}],
                         "reason": "Corrected the dispatch note quantity."}, sent, "/parts", "PATCH")
        reversal = product("Reversal and redispatch - no duplicate deduction", 10, 2)
        sent = sale("Dispatched 4, returned, then dispatched again", [part(reversal, 4)], "dispatched", 4)
        sent = stage(sent, "order_received", "Dispatch recorded before collection.")
        stage(sent, "dispatched")

        counted = product("Physical count - manually adjusted stock", 7, 2)
        manufacture(f"/products/{counted['id']}/stock", {
            "stockQuantity": 9, "minimumStockQuantity": 2, "reason": "Physical count found two extra units.",
        }, detail(counted)["entity"])
        for index in range(7):
            sales("sent_part", {
                "accountId": accounts[index]["entityId"], "productId": counted["id"],
                "machineId": machines[index][0]["id"] if machines[index] else None,
                "sentOn": (today() - timedelta(days=30 * (index + 1))).isoformat(),
                **({"quantity": index + 2} if index % 2 == 0 else {}),
            }, path="/api/parts/history")

        removed = sale("Deleted source - stock and delivery retained", [part(common, 2)], "closed", 5)
        removed = sales("record", {"received": True}, removed, "/feedback")
        checked(client.post("/api/settings/record-cleanup", headers=headers, json={
            "writeId": uuid4().hex, "confirmation": "DELETE",
            "records": [{"entityId": removed["entityId"], "baseRevision": removed["id"]}],
        }))
        retained = product("Deleted manufacturing job - output retained", 0, 1)
        removed_job = job(retained, 3, "completed")
        meta = checked(client.get("/api/manufacturing/meta"))
        checked(client.post("/api/manufacturing/settings/record-cleanup", headers={
            "X-SalesTrackr-Request": "1", "X-ProductTrackr-Workspace": meta["workspaceId"],
            "X-ProductTrackr-Generation": meta["generation"],
        }, json={"writeId": uuid4().hex, "confirmation": "DELETE",
                 "records": [{"id": removed_job["id"], "baseVersion": removed_job["version"]}]}))

        legacy = Revision(
            id=uuid4().hex, entityId=uuid4().hex, entityType="record", parents=[], createdAt=now(),
            actor="local-demo", action="historical_preview", fingerprint=uuid4().hex,
            data=RecordData(
                accountId=accounts[0]["entityId"], productNotes="Historical sale - quantity not recorded",
                stageDates={key: today() for key in
                            ("new_lead", "follow_up", "order_received", "dispatched", "delivered")},
                parts=[{"id": uuid4().hex, "productId": common["id"]}],
            ).model_dump(mode="json"),
        ).model_dump(mode="json")
        legacy = repo.commit(legacy)["revision"]
        sales("record", {"received": True}, legacy, "/payment")
        legacy_job = job(common, 1)
        # Model a job created before quantity tracking, only in this initially empty mock workspace.
        from google.cloud import firestore

        catalog.ref("manufacturingRecords", legacy_job["id"]).update({
            "quantity": firestore.DELETE_FIELD, "inventoryTracked": firestore.DELETE_FIELD,
        })
        catalog.ref("inventorySources", source_key("manufacturing", legacy_job["id"])).delete()
        data, control = recovery.repo.snapshot()
        content, manifest = archive(data, source="manual", operation_id=uuid4().hex,
                                    actor="local-demo", control=control)
        recovered, _ = read_archive(content)
        if recovered != data:
            raise RuntimeError("The mock combined backup did not round-trip.")
        (settings.data_dir / "inventory-preview.zip").write_bytes(content)
        catalog = recovery.repo.products.bind()
        spreadsheet = SpreadsheetExport(catalog, settings).export(
            uuid4().hex, "local-demo", catalog.workspace.id, catalog.control["generation"],
        )
        report = {
            "origin": settings.public_origin, "models": models, "accounts": accounts, "machines": machines,
            "products": products, "salesScenarios": cases,
            "blockedSale": blocked["entityId"], "consumedOutputJob": consumed["id"],
            "legacyJob": legacy_job["id"], "deletedSale": removed["entityId"],
            "deletedJob": removed_job["id"], "backup": manifest["counts"], "spreadsheet": spreadsheet,
            "lowStock": checked(client.get("/api/manufacturing/inventory/low-stock"))["total"],
            "shortages": checked(client.get("/api/manufacturing/inventory/order-shortages"))["total"],
        }
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items()
                          if key in ("origin", "lowStock", "shortages")}, indent=2))


if __name__ == "__main__":
    seed()
