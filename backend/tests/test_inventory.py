from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.domain import RecordData, today
from backend.app.inventory import source_key
from backend.app.main import create_app
from backend.app.manufacturing_backups import checked_data
from backend.app.manufacturing_storage import ManufacturingRepository
from backend.app.shared_backups import archive, check_part_links, read_archive
from backend.tests.test_firestore import headers, raw
from backend.tests.test_manufacturing import app as app
from backend.tests.test_manufacturing import command, create, detail, step
from backend.tests.test_manufacturing_records import apply, job, record, transition


@pytest.fixture
def inventory(app):
    client, settings = app
    product = create(
        client,
        "products",
        name="Inventory bearing",
        partNumber="INV-1",
        stockQuantity=10,
        minimumStockQuantity=5,
    )
    owner = sales(client, "account", {"name": "Inventory company"})
    return client, settings, product, owner


def sales(client, kind, data, current=None, suffix="", expected=200, method="POST"):
    repo = client.app.state.repo
    body = {
        "writeId": uuid4().hex,
        "entityId": current["entityId"] if current else uuid4().hex,
        "baseRevision": current["id"] if current else None,
        "data": data,
    }
    path = f"/api/{kind}s" + (f"/{current['entityId']}{suffix}" if current else "")
    response = client.request(method, path, headers=headers(repo), json=body)
    assert response.status_code == expected, response.text
    return response.json()["revision"] if expected == 200 else response


def sale(inventory, quantity=4, parts=None):
    client, _, product, owner = inventory
    return sales(
        client,
        "record",
        {
            "accountId": owner["entityId"],
            "parts": parts
            if parts is not None
            else [{"id": uuid4().hex, "productId": product["id"], "quantity": quantity}],
        },
    )


def advance(client, row, stage, **fields):
    return sales(client, "record", {"stage": stage, **fields}, row, "/stage")


def ordered(inventory, **values):
    row = sale(inventory, **values)
    for stage_name in ("follow_up", "order_received"):
        row = advance(inventory[0], row, stage_name)
    return row


def product(inventory):
    return detail(inventory[0], inventory[2])["entity"]


def shortages(client):
    result = client.get("/api/manufacturing/inventory/order-shortages")
    assert result.status_code == 200, result.text
    return result.json()["items"]


def adjust(inventory, quantity, minimum=5):
    client, _, initial, _ = inventory
    current = product(inventory)
    response, _ = command(
        client,
        f"/products/{initial['id']}/stock",
        {
            "stockQuantity": quantity,
            "minimumStockQuantity": minimum,
            "reason": "Physical count",
        },
        current["version"],
        current["id"],
    )
    assert response.status_code == 200, response.text


def process(inventory):
    client, _, initial, _ = inventory
    operation = create(client, "operations", name="Inspection")
    source = step(client, initial, operation)
    supplier = create(client, "suppliers", name="Inventory workshop", relationship="internal")
    response, _ = command(
        client,
        f"/steps/{source['id']}/assignments",
        {
            "supplierId": supplier["id"],
        },
        source["version"],
    )
    assert response.status_code == 200, response.text
    return supplier


def test_dispatch_competing_demand_reversal_retry_and_history_are_separate(inventory):
    client, settings, initial, _ = inventory
    rows = [ordered(inventory) for _ in range(3)]
    assert product(inventory)["stockQuantity"] == 10
    assert shortages(client)[0]["missingQuantity"] == 2
    assert shortages(client)[0]["requiredQuantity"] == 12
    for index in range(2):
        rows[index] = advance(client, rows[index], "dispatched")
    assert product(inventory)["stockQuantity"] == 2
    assert shortages(client)[0]["requiredQuantity"] == 4
    assert shortages(client)[0]["missingQuantity"] == 2
    sales(client, "record", {"stage": "dispatched"}, rows[2], "/stage", expected=409)
    assert client.get("/api/parts/history").json()["total"] == 0
    rows[0] = advance(client, rows[0], "delivered")
    rows[0] = sales(client, "record", {"received": True}, rows[0], "/payment")
    assert product(inventory)["stockQuantity"] == 2
    assert client.get("/api/parts/history").json()["items"][0]["data"]["quantity"] == 4
    rows[0] = advance(client, rows[0], "dispatched", reason="Delivery recorded too early")
    assert product(inventory)["stockQuantity"] == 2
    rows[0] = advance(client, rows[0], "order_received", reason="Dispatch recorded too early")
    assert product(inventory)["stockQuantity"] == 6
    assert shortages(client)[0]["missingQuantity"] == 2
    rows[0] = advance(client, rows[0], "dispatched")
    assert product(inventory)["stockQuantity"] == 2
    repo = ManufacturingRepository(settings)
    assert len(list(repo.collection("stockMovements").stream())) == 5
    adjust(inventory, 4)
    assert shortages(client) == []
    rows[2] = advance(client, rows[2], "dispatched")
    assert product(inventory)["stockQuantity"] == 0
    assert repo.read("inventorySources", source_key("sale", rows[2]["entityId"]))["posted"] == {
        initial["id"]: -4,
    }


def test_quantities_must_be_explicit_whole_units(inventory):
    client, _, initial, owner = inventory
    for values in (
        {},
        {"stockQuantity": 0},
        {"stockQuantity": 1.5, "minimumStockQuantity": 0},
        {"stockQuantity": True, "minimumStockQuantity": 0},
    ):
        response, _ = command(client, "/products", {"name": "Invalid product", **values})
        assert response.status_code == 422
    for quantity in (None, 0, -1, 1.5, True, "4"):
        line = {"id": uuid4().hex, "productId": initial["id"]}
        if quantity is not None:
            line["quantity"] = quantity
        sales(client, "record", {"accountId": owner["entityId"], "parts": [line]}, expected=422)
    zero = create(client, "products", name="Explicit zero", stockQuantity=0, minimumStockQuantity=0)
    assert zero["stockQuantity"] == zero["minimumStockQuantity"] == 0
    assert product(inventory)["stockQuantity"] == 10


def test_new_empty_sale_is_tracked_and_sent_corrections_are_net_and_atomic(inventory):
    client, _, initial, _ = inventory
    row = sale(inventory, parts=[])
    assert row["data"]["inventoryTracked"]
    line = {"id": uuid4().hex, "productId": initial["id"], "quantity": 6}
    row = sales(client, "record", {"parts": [line]}, row, "/parts", method="PATCH")
    for stage_name in ("follow_up", "order_received", "dispatched"):
        row = advance(client, row, stage_name)
    assert product(inventory)["stockQuantity"] == 4
    sales(client, "record", {"parts": [{**line, "quantity": 3}]}, row, "/parts", expected=422, method="PATCH")
    row = sales(
        client,
        "record",
        {"parts": [{**line, "quantity": 3}], "reason": "Correct quantity"},
        row,
        "/parts",
        method="PATCH",
    )
    assert product(inventory)["stockQuantity"] == 7
    replacement = create(
        client,
        "products",
        name="Replacement",
        stockQuantity=1,
        minimumStockQuantity=0,
    )
    sales(
        client,
        "record",
        {
            "parts": [{**line, "productId": replacement["id"], "quantity": 2}],
            "reason": "Swap product",
        },
        row,
        "/parts",
        expected=409,
        method="PATCH",
    )
    assert product(inventory)["stockQuantity"] == 7
    assert detail(client, replacement)["entity"]["stockQuantity"] == 1
    row = sales(
        client,
        "record",
        {
            "parts": [{**line, "productId": replacement["id"], "quantity": 1}],
            "reason": "Swap product",
        },
        row,
        "/parts",
        method="PATCH",
    )
    assert product(inventory)["stockQuantity"] == 10
    assert detail(client, replacement)["entity"]["stockQuantity"] == 0
    body = {
        "writeId": uuid4().hex,
        "entityId": row["entityId"],
        "baseRevision": row["id"],
        "data": {"stage": "order_received", "reason": "Not dispatched"},
    }
    path = f"/api/records/{row['entityId']}/stage"
    first = client.post(path, headers=headers(client.app.state.repo), json=body)
    assert first.status_code == 200, first.text
    retry = client.post(path, headers=headers(client.app.state.repo), json=body)
    assert retry.status_code == 200 and retry.json()["revision"] == first.json()["revision"]
    assert detail(client, replacement)["entity"]["stockQuantity"] == 1


def test_manufacturing_final_credit_quantity_correction_and_consumed_undo(inventory):
    client, _, initial, _ = inventory
    supplier = process(inventory)
    current, _ = job(client, initial)
    response, _ = transition(client, current, "quantity", quantity=6, reason="Planned batch")
    assert response.status_code == 200, response.text
    current = record(client, current["id"])["record"]
    current = apply(client, current, "assign", supplierId=supplier["id"])
    assert product(inventory)["stockQuantity"] == 10
    response, body = transition(client, current, "complete")
    assert response.status_code == 200, response.text
    assert command(client, f"/records/{current['id']}/complete", {}, body=body)[0].status_code == 200
    current = record(client, current["id"])["record"]
    assert product(inventory)["stockQuantity"] == 16
    response, _ = command(
        client,
        f"/records/{current['id']}/quantity",
        {
            "quantity": 8,
            "reason": "Two additional finished units",
        },
        current["version"],
        current["id"],
    )
    assert response.status_code == 200, response.text
    assert product(inventory)["stockQuantity"] == 18
    sent = ordered(inventory, quantity=15)
    advance(client, sent, "dispatched")
    current = record(client, current["id"])["record"]
    response, _ = transition(
        client,
        current,
        "undo",
        stepId=current["operations"][-1]["id"],
        reason="Completion error",
    )
    assert response.status_code == 409
    assert record(client, current["id"])["record"]["status"] == "completed"
    assert product(inventory)["stockQuantity"] == 3
    adjust(inventory, 10)
    current = apply(
        client, current, "undo", stepId=current["operations"][-1]["id"], reason="Completion error"
    )
    assert product(inventory)["stockQuantity"] == 2
    apply(client, current, "complete")
    assert product(inventory)["stockQuantity"] == 10


def test_concurrent_dispatch_cannot_oversell(inventory):
    client, settings, _, _ = inventory
    rows = [ordered(inventory, quantity=7) for _ in range(2)]

    def dispatch(row):
        with TestClient(create_app(settings)) as other:
            body = {
                "writeId": uuid4().hex,
                "entityId": row["entityId"],
                "baseRevision": row["id"],
                "data": {"stage": "dispatched"},
            }
            return other.post(
                f"/api/records/{row['entityId']}/stage",
                json=body,
                headers=headers(other.app.state.repo),
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(dispatch, rows))
    assert sorted(results) == [200, 409]
    assert product(inventory)["stockQuantity"] == 3
    assert shortages(client)[0]["missingQuantity"] == 4


def test_legacy_and_manual_delivery_do_not_replay_stock(inventory):
    client, _, initial, owner = inventory
    repo = client.app.state.repo
    old = raw(
        "record",
        data=RecordData(
            accountId=owner["entityId"],
            stageDates={
                name: today()
                for name in ("new_lead", "follow_up", "order_received", "dispatched", "delivered")
            },
            parts=[{"id": uuid4().hex, "productId": initial["id"]}],
        ).model_dump(mode="json"),
    )
    old = repo.commit(old)["revision"]
    closed = sales(client, "record", {"received": True}, old, "/payment")
    assert not closed["data"]["inventoryTracked"]
    assert product(inventory)["stockQuantity"] == 10
    line = closed["data"]["parts"][0]
    sales(
        client,
        "record",
        {"parts": [{**line, "quantity": 100}], "reason": "Known old quantity"},
        closed,
        "/parts",
        method="PATCH",
    )
    body = {
        "writeId": uuid4().hex,
        "entityId": uuid4().hex,
        "baseRevision": None,
        "data": {
            "accountId": owner["entityId"],
            "productId": initial["id"],
            "sentOn": today().isoformat(),
            "quantity": 50,
            "acknowledgeDuplicate": True,
        },
    }
    response = client.post("/api/parts/history", headers=headers(repo), json=body)
    assert response.status_code == 200, response.text
    assert response.json()["revision"]["data"]["quantity"] == 50
    assert product(inventory)["stockQuantity"] == 10
    assert shortages(client) == []


def test_shortage_threshold_manual_adjustments_and_backup_are_coherent(inventory):
    client, settings, _, _ = inventory
    sale(inventory, quantity=12)
    assert shortages(client)[0]["missingQuantity"] == 2
    adjust(inventory, 4)
    low = client.get("/api/manufacturing/inventory/low-stock").json()["items"]
    assert low[0]["missingQuantity"] == 1
    adjust(inventory, 5)
    assert client.get("/api/manufacturing/inventory/low-stock").json()["total"] == 0
    adjust(inventory, 12)
    assert shortages(client) == []
    recovery = client.app.state.recovery
    data, control = recovery.repo.snapshot()
    content, _ = archive(data, source="manual", operation_id=uuid4().hex, actor="local-demo", control=control)
    recovered, manifest = read_archive(content)
    assert manifest["version"] == 1
    assert recovered == data
    broken = deepcopy(data)
    next(iter(broken["manufacturing"]["inventorySources"].values()))["demand"] = {}
    with pytest.raises(ValueError):
        check_part_links(broken["sales"], broken["manufacturing"])
    preview = recovery.inspect(content, "local-demo")
    recovery.restore(content, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    with TestClient(create_app(settings)) as restarted:
        restored = restarted.get(f"/api/manufacturing/products/{inventory[2]['id']}").json()["entity"]
        assert restored["stockQuantity"] == 12
        assert restored["requiredQuantity"] == 12
        assert shortages(restarted) == []


def test_repeated_machine_lines_are_aggregated_before_any_dispatch_write(inventory):
    client, _, initial, owner = inventory
    model = sales(client, "model", {"name": "Inventory model"})
    machines = []
    for name in ("Press A", "Press B"):
        machine_id = uuid4().hex
        owner = sales(client, "account", {
            "id": machine_id, "name": name, "modelId": model["entityId"],
        }, owner, f"/machines/{machine_id}", method="PUT")
        machines.append(machine_id)
    lines = [
        {"id": uuid4().hex, "productId": initial["id"], "machineId": machine, "quantity": quantity}
        for machine, quantity in zip(machines, (6, 5))
    ]
    row = ordered(inventory, parts=lines)
    sales(client, "record", {"stage": "dispatched"}, row, "/stage", expected=409)
    assert product(inventory)["stockQuantity"] == 10
    assert shortages(client)[0]["missingQuantity"] == 1
    lines[1]["quantity"] = 4
    row = sales(client, "record", {"parts": lines}, row, "/parts", method="PATCH")
    advance(client, row, "dispatched")
    assert product(inventory)["stockQuantity"] == 0
    assert shortages(client) == []


def test_legacy_unset_stock_requires_explicit_setup_and_manufacturing_quantity(inventory):
    from google.cloud import firestore

    client, settings, initial, _ = inventory
    repo = ManufacturingRepository(settings)
    repo.ref("products", initial["id"]).update({
        "stockQuantity": firestore.DELETE_FIELD, "minimumStockQuantity": firestore.DELETE_FIELD,
    })
    response = sales(client, "record", {
        "accountId": inventory[3]["entityId"],
        "parts": [{"id": uuid4().hex, "productId": initial["id"], "quantity": 1}],
    }, expected=422)
    assert response.json()["error"]["code"] == "stock_not_set"
    assert product(inventory).get("stockQuantity") is None
    # This fixture represents a pre-inventory product, including absence of an opening movement.
    for row in repo.collection("stockMovements").stream():
        row.reference.delete()
    adjust(inventory, 0)
    process(inventory)
    preview = client.get(f"/api/manufacturing/records/preview/{initial['id']}").json()
    for quantity in (None, 0, 1.5, True):
        data = {"productId": initial["id"], "sourceToken": preview["sourceToken"]}
        if quantity is not None:
            data["quantity"] = quantity
        response, _ = command(client, "/records", data)
        assert response.status_code == 422
    assert product(inventory)["stockQuantity"] == 0
    checked_data(repo.snapshot()[0])


def test_existing_manufacturing_never_starts_automatic_stock_tracking(inventory):
    from google.cloud import firestore

    client, settings, initial, _ = inventory
    supplier = process(inventory)
    current, _ = job(client, initial)
    repo = ManufacturingRepository(settings)
    repo.ref("manufacturingRecords", current["id"]).update({
        "quantity": firestore.DELETE_FIELD, "inventoryTracked": firestore.DELETE_FIELD,
    })
    repo.ref("inventorySources", source_key("manufacturing", current["id"])).delete()
    current = record(client, current["id"])["record"]
    current = apply(client, current, "assign", supplierId=supplier["id"])
    current = apply(client, current, "complete")
    assert product(inventory)["stockQuantity"] == 10
    current = apply(client, current, "quantity", quantity=20, reason="Recovered the old batch quantity")
    assert product(inventory)["stockQuantity"] == 10
    assert not current.get("inventoryTracked")
    checked_data(repo.snapshot()[0])


def test_stock_history_follows_committed_product_order_despite_clock_skew(inventory):
    client, settings, initial, _ = inventory
    repo = ManufacturingRepository(settings)
    opening = next(iter(repo.collection("stockMovements").stream()))
    opening.reference.update({"recordedAt": "2099-01-01T00:00:00+00:00"})
    adjust(inventory, 6)
    adjust(inventory, 8)
    response = client.get(f"/api/manufacturing/inventory/products/{initial['id']}/movements")
    assert response.status_code == 200
    assert [row["afterQuantity"] for row in response.json()["items"]] == [8, 6, 10]


def test_discard_and_source_cleanup_preserve_only_shipped_effects(inventory):
    client, settings, initial, _ = inventory
    before = ordered(inventory, quantity=4)
    sent = ordered(inventory, quantity=3)
    before = sales(client, "record", {"reason": "Not proceeding"}, before, "/discard")
    assert product(inventory)["requiredQuantity"] == 3
    sent = advance(client, sent, "dispatched")
    sent = sales(client, "record", {"reason": "Archive sent enquiry"}, sent, "/discard")
    assert product(inventory)["stockQuantity"] == 7
    assert product(inventory)["requiredQuantity"] == 0
    response = client.post("/api/settings/record-cleanup", headers=headers(client.app.state.repo), json={
        "writeId": uuid4().hex, "confirmation": "DELETE",
        "records": [{"entityId": row["entityId"], "baseRevision": row["id"]} for row in (before, sent)],
    })
    assert response.status_code == 200, response.text
    assert product(inventory)["stockQuantity"] == 7
    stock = ManufacturingRepository(settings)
    assert stock.read("inventorySources", source_key("sale", sent["entityId"]))["posted"] == {
        initial["id"]: -3,
    }
    data, control = client.app.state.recovery.repo.snapshot()
    content, _ = archive(data, source="manual", operation_id=uuid4().hex,
                         actor="local-demo", control=control)
    assert read_archive(content)[0] == data


def test_metadata_edits_cannot_overwrite_a_newer_stock_balance(inventory):
    client, _, initial, _ = inventory
    previous = product(inventory)
    adjust(inventory, 6)
    response, _ = command(client, f"/products/{initial['id']}", {
        "name": "Stale product form",
    }, previous["version"], initial["id"], "PUT")
    assert response.status_code == 409
    current = product(inventory)
    response, _ = command(client, f"/products/{initial['id']}", {
        "name": "Stock through metadata", "stockQuantity": 10, "minimumStockQuantity": 5,
    }, current["version"], initial["id"], "PUT")
    assert response.status_code == 422
    assert product(inventory)["stockQuantity"] == 6


def test_shortage_breakdown_shows_current_company_names_and_paginates(inventory):
    client, _, initial, owner = inventory
    first = sale(inventory, quantity=6)
    other = sales(client, "account", {"name": "Second print company"})
    second = sale((client, inventory[1], initial, other), quantity=6)
    endpoint = f"/api/manufacturing/inventory/products/{initial['id']}/demand"
    pages = [client.get(endpoint, params={"offset": offset, "limit": 1}).json() for offset in (0, 1)]
    assert all(page["total"] == 2 and len(page["items"]) == 1 for page in pages)
    assert {page["items"][0]["companyName"] for page in pages} == {
        owner["data"]["name"], "Second print company",
    }
    sales(client, "account", {"name": "Renamed print company"}, owner, method="PATCH")
    response = client.get(endpoint)
    assert response.status_code == 200
    assert {row["recordId"]: (row["companyName"], row["quantity"]) for row in response.json()["items"]} == {
        first["entityId"]: ("Renamed print company", 6), second["entityId"]: ("Second print company", 6),
    }
