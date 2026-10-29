"""Populate a dedicated localhost-emulator preview without touching existing workspaces."""

import json
import os
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.domain import today
from backend.app.main import create_app
from backend.app.manufacturing_spreadsheet import SpreadsheetExport


def seed():
    settings = Settings.load()
    if (
        settings.mode != "demo" or not settings.manufacturing_enabled
        or settings.firestore_workspace != "machine-sales-preview"
        or settings.manufacturing_workspace != "machine-products-preview"
        or settings.firestore_project != "demo-machine-sales-local"
        or settings.storage_backend != "firestore"
        or settings.manufacturing_backup_storage != "local"
        or os.getenv("FIRESTORE_EMULATOR_HOST") not in ("127.0.0.1:8085", "localhost:8085")
        or os.getenv("GOOGLE_TOKEN_JSON")
    ):
        raise RuntimeError("Use only the dedicated, credential-free machine-sales emulator preview.")
    settings.validate_manufacturing()
    output = settings.data_dir / "scenarios.json"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.repo
        if repo.meta:
            if output.exists():
                print("Existing machine-sales preview retained; mock data was not overwritten.")
                return
            raise RuntimeError("This preview workspace is not empty. Existing data was preserved.")
        generation = client.get("/api/me").json()["generation"]
        headers = {"X-SalesTrackr-Request": "1", "X-SalesTrackr-Generation": generation}

        def checked(response):
            if response.status_code != 200:
                raise RuntimeError(f"{response.request.url.path}: {response.status_code}: {response.text}")
            return response.json()

        def save(kind, data, current=None, path=None, method="POST"):
            return checked(client.request(method, path or f"/api/{kind}s", headers=headers, json={
                "writeId": uuid4().hex, "entityId": current["entityId"] if current else uuid4().hex,
                "baseRevision": current["id"] if current else None, "data": data,
            }))["revision"]

        def product(name, number, models):
            meta = checked(client.get("/api/manufacturing/meta"))
            entity_id = uuid4().hex
            checked(client.post("/api/manufacturing/products", headers={
                "X-SalesTrackr-Request": "1", "X-ProductTrackr-Workspace": meta["workspaceId"],
                "X-ProductTrackr-Generation": meta["generation"],
            }, json={
                "writeId": uuid4().hex, "id": entity_id, "baseVersion": None,
                "data": {"name": name, "partNumber": number, "modelIds": models,
                         "stockQuantity": 1000, "minimumStockQuantity": 5,
                         "notes": "Synthetic local demonstration product."},
            }))
            return {"id": entity_id, "name": name}

        model_names = ["Lithrone", "Enthrone", "Spica", "Jthrone", "Komori"]
        models = [save("model", {"name": name}) for name in model_names]
        common = [product(name, f"COM-{index + 1:03}", []) for index, name in enumerate([
            "Universal bearing", "Drive belt", "Seal kit", "Lubrication filter", "Fastener set", "Air hose",
        ])]
        specific = {
            index: [product(
                f"{name} {part}", f"M{index + 1}-{part_index + 1:03}", [models[index]["entityId"]],
            )
                    for part_index, part in enumerate(["feed roller", "drive gear", "gripper"])]
            for index, name in enumerate(model_names)
        }
        multi = [
            product(name, f"MULTI-{index + 1:03}", [models[i]["entityId"] for i in model_indexes])
            for index, (name, model_indexes) in enumerate([
                ("Shared optical sensor", [0, 1]), ("Transfer bush", [2, 3]), ("Pneumatic valve", [1, 4]),
            ])
        ]
        companies = [
            ("Atlas Printworks", [0, 0, 1, 2]), ("Bright Page Press", [1, 3]),
            ("Cedar Packaging", [2, 2, 4]), ("Delta Offset", [0, 3]),
            ("Evergreen Graphics", [4]), ("Foxtrot Print Services", [1, 2, 3]),
            ("No-machine Trading", []), ("Quiet Account", [0, 1]),
        ]
        accounts, machine_sets = [], []
        for index, (name, model_indexes) in enumerate(companies):
            account = save("account", {
                "name": name, "address": f"Synthetic Industrial Estate, Block {index + 1}",
                "notes": "Mock customer for the machine-sales local preview.",
            })
            machines = []
            for position, model_index in enumerate(model_indexes):
                machine = {
                    "id": uuid4().hex, "name": f"Press {position + 1}",
                    "modelId": models[model_index]["entityId"],
                    "serialNumber": f"DEMO-{index + 1:02}-{position + 1:02}" if position % 2 == 0 else "",
                    "manufacturingYear": 2016 + position if position % 2 == 0 else None,
                }
                account = save("account", machine, account,
                               f"/api/accounts/{account['entityId']}/machines/{machine['id']}", "PUT")
                machines.append(machine)
            accounts.append(account)
            machine_sets.append(machines)

        cases = {}
        current_day = today()

        def part(product_row, machine=None):
            return {"id": uuid4().hex, "productId": product_row["id"],
                    "quantity": 1,
                    "machineId": machine["id"] if machine else None}

        def record(account_index, label, parts, days_ago, stage="closed"):
            day = current_day - timedelta(days=days_ago)
            with patch("backend.app.domain.today", return_value=day):
                row = save("record", {
                    "accountId": accounts[account_index]["entityId"], "acquiredOn": day.isoformat(),
                    "productNotes": f"MOCK: {label}", "parts": parts,
                })
                journey = ["new_lead", "follow_up", "order_received", "dispatched", "delivered", "closed"]
                for reached in journey[1:journey.index(stage) + 1]:
                    if reached == "closed":
                        row = save(
                            "record", {"received": True}, row, f"/api/records/{row['entityId']}/payment",
                        )
                    else:
                        row = save("record", {"stage": reached, "occurredOn": day.isoformat()}, row,
                                   f"/api/records/{row['entityId']}/stage")
            cases[label] = row["entityId"]
            return row

        for index in range(6):
            machines = machine_sets[index]
            model_index = companies[index][1][0]
            record(index, f"{companies[index][0]} - completed multi-part delivery", [
                part(common[0], machines[0]), part(specific[model_index][0]), part(common[1]),
            ], (index + 1) * 20)
            record(index, f"{companies[index][0]} - recent account-wide delivery", [
                part(common[0]), part(specific[model_index][1]),
            ], (index + 1) * 2)
            record(index, f"{companies[index][0]} - delivered, payment pending", [
                part(common[2], machines[0]),
            ], index + 10, "delivered")
            record(index, f"{companies[index][0]} - draft parts, no sent history", [
                part(specific[model_index][2], machines[0]),
            ], index + 3, ["new_lead", "follow_up", "order_received"][index % 3])

        corrected = record(0, "Closed sale with corrected product and removed bearing", [
            part(common[0], machine_sets[0][1]), part(specific[0][0]),
        ], 40)
        lines = corrected["data"]["parts"]
        corrected = save("record", {
            "parts": [{**lines[1], "productId": specific[0][2]["id"]}],
            "reason": "Corrected the catalog part and removed a bearing that was not delivered.",
        }, corrected, f"/api/records/{corrected['entityId']}/parts", "PATCH")
        corrected = save("record", {
            "stage": "delivered", "occurredOn": (current_day - timedelta(days=39)).isoformat(),
            "reason": "Corrected the historical delivery date.",
        }, corrected, f"/api/records/{corrected['entityId']}/stage")
        save("record", {"stage": "closed"}, corrected, f"/api/records/{corrected['entityId']}/stage")

        reopened = record(1, "Reopened for payment, delivery remains in history", [
            part(common[3], machine_sets[1][0]),
        ], 30)
        reopened = save("record", {
            "stage": "delivered", "occurredOn": (current_day - timedelta(days=30)).isoformat(),
            "reason": "Reopened to correct payment without undoing delivery.",
        }, reopened, f"/api/records/{reopened['entityId']}/stage")
        save("record", {"received": False}, reopened, f"/api/records/{reopened['entityId']}/payment")

        undone = record(2, "Delivery undone, historical entries withdrawn", [part(common[4])], 45)
        save("record", {
            "stage": "dispatched", "occurredOn": (current_day - timedelta(days=45)).isoformat(),
            "reason": "The Delivered milestone was an error; parts are still in transit.",
        }, undone, f"/api/records/{undone['entityId']}/stage")

        reinstated = record(3, "Delivery reinstated without a duplicate shipment", [
            part(common[4], machine_sets[3][1]),
        ], 60)
        reinstated = save("record", {
            "stage": "dispatched", "occurredOn": (current_day - timedelta(days=60)).isoformat(),
            "reason": "Delivery was temporarily withdrawn pending confirmation.",
        }, reinstated, f"/api/records/{reinstated['entityId']}/stage")
        save("record", {
            "stage": "delivered", "occurredOn": (current_day - timedelta(days=59)).isoformat(),
            "reason": "Confirmed delivery from the signed dispatch note.",
        }, reinstated, f"/api/records/{reinstated['entityId']}/stage")

        late_parts = record(4, "Parts added after the sale was already closed", [], 15)
        save("record", {
            "parts": [part(specific[4][1], machine_sets[4][0]), part(common[3])],
            "reason": "Added catalog parts missed when the sales record was closed.",
        }, late_parts, f"/api/records/{late_parts['entityId']}/parts", "PATCH")

        for index in range(7):
            model_index = companies[index][1][0] if companies[index][1] else None
            for slot, product_row in enumerate([
                common[5], specific[model_index][2] if model_index is not None else common[1], common[2],
            ]):
                machine = machine_sets[index][0] if slot == 1 and machine_sets[index] else None
                save("sent_part", {
                    "accountId": accounts[index]["entityId"], "productId": product_row["id"],
                    "machineId": machine["id"] if machine else None,
                    "sentOn": (current_day - timedelta(days=160 - slot * 50 + index)).isoformat(),
                }, path="/api/parts/history")

        manual = save("sent_part", {
            "accountId": accounts[0]["entityId"], "productId": multi[0]["id"],
            "sentOn": (current_day - timedelta(days=200)).isoformat(),
        }, path="/api/parts/history")
        save("sent_part", {
            "accountId": accounts[0]["entityId"], "productId": multi[0]["id"],
            "sentOn": (current_day - timedelta(days=198)).isoformat(),
            "reason": "Corrected the date from the old paper delivery note.",
        }, manual, f"/api/parts/history/{manual['entityId']}", "PATCH")
        manual_void = save("sent_part", {
            "accountId": accounts[2]["entityId"], "productId": common[1]["id"],
            "sentOn": (current_day - timedelta(days=12)).isoformat(),
        }, path="/api/parts/history")
        save("sent_part", {"reason": "This was an erroneous duplicate manual entry."}, manual_void,
             f"/api/parts/history/{manual_void['entityId']}/void")

        removed = record(3, "Deleted source sale, delivery history retained", [part(common[5])], 90)
        removed = save("record", {"received": True}, removed, f"/api/records/{removed['entityId']}/feedback")
        checked(client.post("/api/settings/record-cleanup", headers=headers, json={
            "writeId": uuid4().hex, "confirmation": "DELETE",
            "records": [{"entityId": removed["entityId"], "baseRevision": removed["id"]}],
        }))
        catalog = client.app.state.recovery.repo.products.bind()
        SpreadsheetExport(catalog, settings).export(
            uuid4().hex, "local-demo", catalog.workspace.id, catalog.control["generation"],
        )
        report = {
            "origin": settings.public_origin, "models": models, "accounts": accounts,
            "machines": machine_sets, "commonProducts": common, "specificProducts": specific,
            "multiModelProducts": multi, "scenarios": cases,
            "activeDeliveries": checked(client.get("/api/parts/history"))["total"],
            "allDeliveries": checked(client.get("/api/parts/history?includeVoided=true"))["total"],
            "deletedSourceRecord": removed["entityId"],
        }
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({
            "accounts": len(accounts), "models": len(models), "machines": sum(map(len, machine_sets)),
            "products": len(common) + sum(map(len, specific.values())) + len(multi),
            "salesScenarios": len(cases), "activeDeliveries": report["activeDeliveries"],
            "allDeliveries": report["allDeliveries"], "origin": settings.public_origin,
        }, indent=2))


if __name__ == "__main__":
    seed()
