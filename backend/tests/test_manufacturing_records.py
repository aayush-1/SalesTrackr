from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from backend.app.domain import today
from backend.app.manufacturing_storage import ManufacturingRepository
from backend.tests.test_manufacturing import app as app
from backend.tests.test_manufacturing import command, create, detail, step


def record(client, identifier):
    response = client.get(f"/api/manufacturing/records/{identifier}")
    assert response.status_code == 200, response.text
    return response.json()


def job(client, product, body=None):
    preview = client.get(f"/api/manufacturing/records/preview/{product['id']}")
    assert preview.status_code == 200, preview.text
    response, sent = command(
        client,
        "/records",
        {
            "productId": product["id"],
            "sourceToken": preview.json()["sourceToken"],
            "notes": "Test job",
            "quantity": 1,
        },
        body=body,
    )
    assert response.status_code == 200, response.text
    return record(client, sent["id"])["record"], sent


def transition(client, current, action, step_id=None, **data):
    if action not in ("cancel", "undo", "quantity"):
        data["stepId"] = step_id or current["operations"][current["currentIndex"]]["id"]
    response, body = command(
        client,
        f"/records/{current['id']}/{action}",
        data,
        current["version"],
        current["id"],
    )
    return response, body


def apply(client, current, action, **data):
    response, _ = transition(client, current, action, **data)
    assert response.status_code == 200, response.text
    return record(client, current["id"])["record"]


@pytest.fixture
def process(app):
    client, settings = app
    product = create(client, "products", name="Transmission Gear", partNumber="GEAR-042")
    operation = create(client, "operations", name="Machining")
    steps = [step(client, product, operation) for _ in range(3)]
    company = create(client, "suppliers", name="Company A")
    contact = create(client, "contacts", name="Alex", supplierId=company["id"])
    other_company = create(client, "suppliers", name="Company B", relationship="internal")
    other_contact = create(client, "contacts", name="Blair", supplierId=other_company["id"])
    individual = create(client, "suppliers", name="Independent specialist", type="individual")
    for source in steps:
        response, _ = command(
            client,
            f"/steps/{source['id']}/assignments",
            {
                "supplierId": company["id"],
                "contactId": contact["id"],
            },
            source["version"],
        )
        assert response.status_code == 200
        current = next(s for s in detail(client, product)["steps"] if s["id"] == source["id"])
        response, _ = command(
            client,
            f"/steps/{source['id']}/assignments",
            {
                "supplierId": individual["id"],
            },
            current["version"],
        )
        assert response.status_code == 200
    return client, settings, product, company, contact, other_company, other_contact, individual


def test_creation_snapshots_independent_jobs_and_idempotent_numbers(process):
    client, _, product, company, contact, *_ = process
    first, sent = job(client, product)
    duplicate, _ = command(client, "/records", {}, body=sent)
    assert duplicate.status_code == 200
    second, _ = job(client, product)
    assert first["number"] == "MFG-00001"
    assert second["number"] == "MFG-00002"
    assert [s["status"] for s in first["operations"]] == ["ready", "pending", "pending"]
    assert first["currentSupplierId"] is None
    assert len({s["id"] for s in first["operations"]}) == 3
    first = apply(client, first, "assign", supplierId=company["id"], contactId=contact["id"])
    assert first["status"] == "in_progress"
    assert record(client, second["id"])["record"]["status"] == "ready"
    original = record(client, second["id"])["record"]
    source = detail(client, product)
    assert (
        command(
            client,
            f"/products/{product['id']}/reorder",
            {
                "stepIds": list(reversed(source["entity"]["stepIds"])),
            },
            source["entity"]["version"],
        )[0].status_code
        == 200
    )
    current = detail(client, product)["entity"]
    assert (
        command(
            client,
            f"/products/{product['id']}",
            {
                "name": "Renamed Product",
                "partNumber": "NEW-ID",
            },
            current["version"],
            product["id"],
            "PUT",
        )[0].status_code
        == 200
    )
    source_step = source["steps"][0]
    assert (
        command(
            client,
            f"/steps/{source_step['id']}",
            {
                "operationId": source_step["operationId"],
                "instructions": "New process instructions",
            },
            source_step["version"],
            source_step["id"],
            "PUT",
        )[0].status_code
        == 200
    )
    operation_id = source_step["operationId"]
    operation = client.get(f"/api/manufacturing/operations/{operation_id}").json()["entity"]
    assert (
        command(
            client,
            f"/operations/{operation_id}",
            {"name": "Renamed operation"},
            operation["version"],
            operation_id,
            "PUT",
        )[0].status_code
        == 200
    )
    assert record(client, second["id"])["record"] == original
    assert original["productPartNumber"] == "GEAR-042"
    assert original["operations"][0]["name"] == "Machining"
    assert client.get(f"/api/manufacturing/records/{first['id']}/history").json()["total"] == 2


def test_supplier_work_lists_only_actual_current_assignments_with_search_and_pagination(process):
    client, _, product, company, contact, _, _, individual = process
    first, _ = job(client, product)
    first = apply(client, first, "assign", supplierId=company["id"], contactId=contact["id"])
    second, _ = job(client, product)
    second = apply(client, second, "assign", supplierId=individual["id"])
    job(client, product)
    endpoint = "/api/manufacturing/records/supplier-work"
    result = client.get(endpoint).json()
    assert result["total"] == result["supplierCount"] == 2
    assert result["jobCount"] == 2
    assert [row["id"] for row in result["items"]] == [company["id"], individual["id"]]
    current = client.get(endpoint, params={"supplierId": company["id"]}).json()
    assert current["supplierName"] == company["name"]
    assert current["items"][0]["id"] == first["id"]
    assert current["items"][0]["currentOperationName"] == "Machining"
    assert current["items"][0]["productPartNumber"] == "GEAR-042"
    assert client.get(endpoint, params={"offset": 1, "limit": 1}).json()["items"][0]["id"] == individual["id"]
    for term, expected in [
        ("company a", [company["id"]]), ("alex", [company["id"]]),
        ("gear-042", [company["id"], individual["id"]]),
        ("machining", [company["id"], individual["id"]]), ("missing", []),
    ]:
        found = client.get(endpoint, params={"search": term}).json()
        assert [row["id"] for row in found["items"]] == expected
    first = apply(client, first, "complete")
    assert first["status"] == "ready"
    assert client.get(endpoint).json()["items"][0]["id"] == individual["id"]
    assert client.get(endpoint, params={"supplierId": company["id"]}).json()["total"] == 0
    second = apply(client, second, "cancel", reason="No longer active")
    assert client.get(endpoint).json()["total"] == 0
    for _ in range(2):
        first = apply(client, first, "assign", supplierId=company["id"])
        first = apply(client, first, "complete")
    assert first["status"] == "completed"
    assert client.get(endpoint).json()["items"] == []


def test_supplier_work_groups_before_pagination_and_opens_all_current_supplier_jobs(process):
    client, _, product, company, contact, _, _, individual = process
    first, _ = job(client, product)
    first = apply(client, first, "assign", supplierId=company["id"], contactId=contact["id"])
    other = create(client, "products", name="Second part", partNumber="SECOND-01")
    operation_id = detail(client, product)["steps"][0]["operationId"]
    source = step(client, other, {"id": operation_id})
    response, _ = command(
        client, f"/steps/{source['id']}/assignments", {"supplierId": company["id"]}, source["version"],
    )
    assert response.status_code == 200
    assigned = [first]
    for _ in range(2):
        current, _ = job(client, other)
        assigned.append(apply(client, current, "assign", supplierId=company["id"]))
    unrelated, _ = job(client, product)
    apply(client, unrelated, "assign", supplierId=individual["id"])
    endpoint = "/api/manufacturing/records/supplier-work"
    result = client.get(endpoint, params={"limit": 1}).json()
    assert result["total"] == result["supplierCount"] == 2 and result["jobCount"] == 4
    assert result["items"] == [{
        "id": company["id"], "name": company["name"], "jobCount": 3, "productCount": 2,
    }]
    following = client.get(endpoint, params={"offset": 1, "limit": 1}).json()
    assert following["items"][0]["id"] == individual["id"]
    found = client.get(endpoint, params={"search": "SECOND-01"}).json()
    assert found["total"] == 1 and found["items"][0]["jobCount"] == 3
    pages = [
        client.get(endpoint, params={"supplierId": company["id"], "offset": offset, "limit": 1}).json()
        for offset in range(3)
    ]
    assert all(page["total"] == 3 for page in pages)
    assert {page["items"][0]["id"] for page in pages} == {row["id"] for row in assigned}


def test_creation_rejects_empty_and_changed_preview_but_not_unrelated_changes(process):
    client, _, product, *_ = process
    empty = create(client, "products", name="No operations")
    assert client.get(f"/api/manufacturing/records/preview/{empty['id']}").status_code == 422
    preview = client.get(f"/api/manufacturing/records/preview/{product['id']}").json()
    create(client, "products", name="Unrelated edit")
    response, _ = command(
        client, "/records", {"productId": product["id"], "sourceToken": preview["sourceToken"], "quantity": 1}
    )
    assert response.status_code == 200
    current = detail(client, product)["steps"][0]
    assert (
        command(
            client,
            f"/steps/{current['id']}",
            {
                "operationId": current["operationId"],
                "instructions": "Changed after preview",
            },
            current["version"],
            current["id"],
            "PUT",
        )[0].status_code
        == 200
    )
    stale, _ = command(
        client, "/records", {"productId": product["id"], "sourceToken": preview["sourceToken"], "quantity": 1}
    )
    assert stale.status_code == 409
    assert client.get("/api/manufacturing/records?view=all").json()["total"] == 1


def test_sequential_work_and_actual_date_rules(process):
    client, _, product, company, contact, _, wrong_contact, individual = process
    current, _ = job(client, product)
    assert transition(client, current, "complete")[0].status_code == 409
    assert (
        transition(
            client, current, "assign", step_id=current["operations"][1]["id"], supplierId=company["id"]
        )[0].status_code
        == 409
    )
    assert (
        transition(client, current, "assign", supplierId=company["id"], contactId=wrong_contact["id"])[
            0
        ].status_code
        == 422
    )
    assert (
        transition(client, current, "assign", supplierId=individual["id"], contactId=contact["id"])[
            0
        ].status_code
        == 422
    )
    future = (today() + timedelta(days=1)).isoformat()
    assert (
        transition(client, current, "assign", supplierId=company["id"], occurredOn=future)[0].status_code
        == 422
    )
    assigned_on = (today() - timedelta(days=2)).isoformat()
    current = apply(
        client, current, "assign", supplierId=company["id"], contactId=contact["id"], occurredOn=assigned_on
    )
    assert current["currentContactName"] == "Alex"
    assert (
        transition(client, current, "complete", occurredOn=(today() - timedelta(days=3)).isoformat())[
            0
        ].status_code
        == 422
    )
    current = apply(client, current, "complete", occurredOn=(today() - timedelta(days=1)).isoformat())
    assert current["status"] == "ready"
    assert current["completedCount"] == 1
    assert current["currentSupplierId"] is None
    assert (
        transition(client, current, "assign", supplierId=company["id"], occurredOn=assigned_on)[0].status_code
        == 422
    )
    for _ in range(2):
        current = apply(client, current, "assign", supplierId=individual["id"])
        current = apply(client, current, "complete")
    assert current["status"] == "completed"
    assert current["currentIndex"] is None
    assert current["completedCount"] == current["stepCount"] == 3
    assert client.get("/api/manufacturing/records").json()["total"] == 0
    assert (
        transition(client, current, "complete", step_id=current["operations"][-1]["id"])[0].status_code == 409
    )


def test_live_link_choices_and_current_work_survive_unlink(process):
    client, _, product, company, contact, other, other_contact, _ = process
    current, _ = job(client, product)
    assert transition(client, current, "assign", supplierId=other["id"])[0].status_code == 409
    source = detail(client, product)["steps"][0]
    linked, link = command(
        client,
        f"/steps/{source['id']}/assignments",
        {
            "supplierId": other["id"],
            "contactId": other_contact["id"],
        },
        source["version"],
    )
    assert linked.status_code == 200
    assert other["id"] in [c["supplier"]["id"] for c in record(client, current["id"])["eligibleSuppliers"]]
    current = apply(client, current, "assign", supplierId=company["id"], contactId=contact["id"])
    current = apply(
        client,
        current,
        "reassign",
        supplierId=other["id"],
        contactId=other_contact["id"],
        reason="Use the in-house workshop",
    )
    source = detail(client, product)["steps"][0]
    assert (
        command(
            client,
            f"/steps/{source['id']}/assignments/{link['id']}/remove",
            {},
            source["version"],
            link["id"],
        )[0].status_code
        == 200
    )
    assert other["id"] not in [
        c["supplier"]["id"] for c in record(client, current["id"])["eligibleSuppliers"]
    ]
    assert record(client, current["id"])["record"]["currentSupplierName"] == other["name"]
    assert (
        transition(client, current, "reassign", supplierId=other["id"], reason="Removed link")[0].status_code
        == 409
    )
    assert apply(client, current, "complete")["completedCount"] == 1


def test_empty_supplier_pool_can_be_linked_after_creation(app):
    client, _ = app
    product = create(client, "products", name="New process")
    operation = create(client, "operations", name="Cut")
    source = step(client, product, operation)
    current, _ = job(client, product)
    assert record(client, current["id"])["eligibleSuppliers"] == []
    supplier = create(client, "suppliers", name="Workshop")
    assert (
        command(
            client, f"/steps/{source['id']}/assignments", {"supplierId": supplier["id"]}, source["version"]
        )[0].status_code
        == 200
    )
    assert len(record(client, current["id"])["eligibleSuppliers"]) == 1
    assert apply(client, current, "assign", supplierId=supplier["id"])["status"] == "in_progress"


def test_reassignment_undo_cancellation_and_immutable_history(process):
    client, _, product, company, _, _, _, individual = process
    current, _ = job(client, product)
    current = apply(client, current, "assign", supplierId=company["id"])
    assert (
        transition(client, current, "reassign", supplierId=individual["id"], reason=" ")[0].status_code == 422
    )
    current = apply(client, current, "reassign", supplierId=individual["id"], reason="Move to specialist")
    first_step_id = current["operations"][0]["id"]
    current = apply(client, current, "complete", note="Checked dimensions")
    current = apply(client, current, "undo", stepId=first_step_id, reason="Completion entered by mistake")
    assert current["completedCount"] == 0
    assert current["operations"][0]["assignment"]["supplierId"] == individual["id"]
    assert current["operations"][1]["status"] == "pending"
    current = apply(client, current, "complete")
    current = apply(client, current, "assign", supplierId=company["id"])
    assert transition(client, current, "undo", stepId=first_step_id, reason="Too late")[0].status_code == 409
    assert transition(client, current, "cancel", reason=" ")[0].status_code == 422
    current = apply(client, current, "cancel", reason="Customer requirement withdrawn")
    assert current["status"] == "cancelled"
    assert current["operations"][1]["status"] == "cancelled"
    assert transition(client, current, "complete")[0].status_code == 409
    assert transition(client, current, "undo", stepId=first_step_id, reason="Cancelled")[0].status_code == 409
    history = client.get(f"/api/manufacturing/records/{current['id']}/history").json()
    assert history["total"] == 8
    assert [e["sequence"] for e in history["items"]] == list(range(8, 0, -1))
    reassignment = next(e for e in history["items"] if e["action"] == "reassign")
    assert reassignment["previousAssignment"]["supplierName"] == company["name"]
    assert reassignment["assignment"]["supplierName"] == individual["name"]
    first_completion = next(e for e in history["items"] if e.get("note") == "Checked dimensions")
    assert first_completion["action"] == "complete"
    second_page = client.get(f"/api/manufacturing/records/{current['id']}/history?offset=2&limit=2").json()
    assert [e["sequence"] for e in second_page["items"]] == [6, 5]


def test_final_completion_can_reopen_only_the_last_step(process):
    client, _, product, company, *_ = process
    current, _ = job(client, product)
    for _ in range(3):
        current = apply(client, current, "assign", supplierId=company["id"])
        current = apply(client, current, "complete")
    assert transition(client, current, "cancel", reason="Already complete")[0].status_code == 409
    assert (
        transition(client, current, "undo", stepId=current["operations"][0]["id"], reason="Not the last one")[
            0
        ].status_code
        == 409
    )
    current = apply(
        client, current, "undo", stepId=current["operations"][-1]["id"], reason="Recheck last step"
    )
    assert current["status"] == "in_progress"
    assert current["completedCount"] == 2
    assert current["completedOn"] is None


def test_concurrent_and_retried_completion_is_exactly_once(process):
    client, _, product, company, *_ = process
    current, _ = job(client, product)
    current = apply(client, current, "assign", supplierId=company["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: transition(client, current, "complete"), range(2)))
    assert sorted(response.status_code for response, _ in results) == [200, 409]
    success = next(body for response, body in results if response.status_code == 200)
    assert command(client, f"/records/{current['id']}/complete", {}, body=success)[0].status_code == 200
    stored = record(client, current["id"])["record"]
    assert stored["completedCount"] == 1
    assert stored["currentIndex"] == 1
    assert client.get(f"/api/manufacturing/records/{current['id']}/history").json()["total"] == 3
    success["data"]["note"] = "Different retry"
    assert command(client, f"/records/{current['id']}/complete", {}, body=success)[0].status_code == 409


def test_record_queries_counts_filters_search_and_pagination(process):
    client, _, product, company, *_ = process
    waiting, _ = job(client, product)
    running, _ = job(client, product)
    running = apply(client, running, "assign", supplierId=company["id"])
    cancelled, _ = job(client, product)
    apply(client, cancelled, "cancel", reason="Sample cancellation")
    assert client.get("/api/manufacturing/records/summary").json()["counts"] == {
        "ready": 1,
        "in_progress": 1,
        "completed": 0,
        "cancelled": 1,
        "active": 2,
        "all": 3,
    }
    assert client.get("/api/manufacturing/records").json()["total"] == 2
    assert client.get("/api/manufacturing/records?view=all&search=gEaR-042").json()["total"] == 3
    assert client.get(f"/api/manufacturing/records?view=all&search={waiting['number']}").json()["total"] == 1
    assert client.get("/api/manufacturing/records?search=does-not-exist").json()["total"] == 0
    filtered = client.get(
        "/api/manufacturing/records",
        params={
            "view": "active",
            "productId": product["id"],
            "supplierId": company["id"],
            "operationId": running["currentOperationId"],
            "search": "Transmission",
        },
    ).json()
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == running["id"]
    assert "operations" not in filtered["items"][0]
    pages = [client.get(f"/api/manufacturing/records?view=all&limit=1&offset={n}").json() for n in range(3)]
    assert all(page["total"] == 3 for page in pages)
    assert len({page["items"][0]["id"] for page in pages}) == 3


def test_record_commands_preserve_workspace_fences_and_route_identifiers(process):
    client, _, product, company, *_ = process
    current, _ = job(client, product)
    response, body = transition(client, current, "assign", supplierId=company["id"])
    assert response.status_code == 200
    body["id"] = uuid4().hex
    assert command(client, f"/records/{current['id']}/complete", {}, body=body)[0].status_code == 422
    assert (
        command(
            client,
            f"/records/{current['id']}/complete",
            {},
            entity_id=current["id"],
            headers={
                "X-SalesTrackr-Request": "1",
            },
        )[0].status_code
        == 409
    )


def test_assignment_labels_stay_frozen_and_reassignment_dates_stay_sequential(process):
    client, _, product, company, contact, _, _, individual = process
    current, _ = job(client, product)
    original_date = (today() - timedelta(days=4)).isoformat()
    current = apply(
        client,
        current,
        "assign",
        supplierId=company["id"],
        contactId=contact["id"],
        occurredOn=original_date,
    )
    assert (
        command(
            client,
            f"/suppliers/{company['id']}",
            {"name": "Renamed company"},
            company["version"],
            company["id"],
            "PUT",
        )[0].status_code
        == 200
    )
    assert (
        command(
            client,
            f"/contacts/{contact['id']}",
            {
                "name": "Renamed contact",
                "supplierId": company["id"],
            },
            contact["version"],
            contact["id"],
            "PUT",
        )[0].status_code
        == 200
    )
    actual = record(client, current["id"])["record"]["operations"][0]["assignment"]
    assert actual["supplierName"] == "Company A" and actual["contact"]["name"] == "Alex"
    assert (
        transition(
            client,
            current,
            "reassign",
            supplierId=company["id"],
            contactId=contact["id"],
            reason="Same responsibility",
        )[0].status_code
        == 422
    )
    assert (
        transition(
            client,
            current,
            "reassign",
            supplierId=individual["id"],
            reason="Move work",
            occurredOn=(today() - timedelta(days=5)).isoformat(),
        )[0].status_code
        == 422
    )
    current = apply(
        client,
        current,
        "reassign",
        supplierId=individual["id"],
        reason="Move work",
        occurredOn=(today() - timedelta(days=2)).isoformat(),
    )
    assert transition(client, current, "complete", occurredOn=original_date)[0].status_code == 422
    history = client.get(f"/api/manufacturing/records/{current['id']}/history").json()["items"]
    assert history[0]["previousAssignment"]["contact"]["name"] == "Alex"
    assert history[1]["assignment"]["supplierName"] == "Company A"


def test_unavailable_source_step_does_not_substitute_another_occurrence_or_block_completion(process):
    client, settings, product, company, *_ = process
    current, _ = job(client, product)
    current = apply(client, current, "assign", supplierId=company["id"])
    repo = ManufacturingRepository(settings)
    repo.ref("steps", current["operations"][0]["sourceStepId"]).delete()
    view = record(client, current["id"])
    assert view["eligibleSuppliers"] == []
    assert "source Product operation is unavailable" in view["eligibilityError"]
    assert apply(client, current, "complete")["completedCount"] == 1
    view = record(client, current["id"])
    assert len(view["eligibleSuppliers"]) == 2
    assert view["record"]["currentIndex"] == 1


def test_snapshot_step_and_document_bounds_are_validated_before_creation(process):
    client, settings, product, *_ = process
    repo = ManufacturingRepository(settings)
    first = detail(client, product)["steps"][0]
    step_ids = [uuid4().hex for _ in range(101)]
    repo.ref("products", product["id"]).update({"stepIds": step_ids})
    assert client.get(f"/api/manufacturing/records/preview/{product['id']}").status_code == 422
    batch = repo.client.batch()
    for step_id in step_ids[:100]:
        batch.set(repo.ref("steps", step_id), {**first, "id": step_id, "instructions": "x" * 10000})
    batch.update(repo.ref("products", product["id"]), {"stepIds": step_ids[:100]})
    batch.commit()
    response = client.get(f"/api/manufacturing/records/preview/{product['id']}")
    assert response.status_code == 422 and "too large" in response.json()["error"]["message"]
    assert client.get("/api/manufacturing/records?view=all").json()["total"] == 0
