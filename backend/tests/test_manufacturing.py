import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from google.cloud import firestore

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.manufacturing_storage import ManufacturingRepository


@pytest.fixture
def app(tmp_path):
    if not os.getenv("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("Requires the explicitly configured local Firestore emulator.")
    settings = Settings(
        "demo", tmp_path, "", "", "", "", False,
        storage_backend="firestore", firestore_project="demo-producttrackr-tests",
        firestore_workspace=f"sales-{uuid4().hex}",
        manufacturing_enabled=True, manufacturing_project="demo-producttrackr-tests",
        manufacturing_workspace=uuid4().hex,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/manufacturing/meta").status_code == 200
        yield client, settings


def command(client, path, data, base=None, entity_id=None, method="POST", body=None, headers=None):
    meta = client.get("/api/manufacturing/meta").json()
    headers = headers if headers is not None else {
        "X-SalesTrackr-Request": "1", "X-ProductTrackr-Workspace": meta["workspaceId"],
        "X-ProductTrackr-Generation": meta["generation"],
    }
    body = body or {"writeId": uuid4().hex, "id": entity_id or uuid4().hex, "baseVersion": base, "data": data}
    return client.request(method, "/api/manufacturing" + path, json=body, headers=headers), body


def create(client, kind, **data):
    response, body = command(client, "/" + kind, data)
    assert response.status_code == 200, response.text
    return client.get(f"/api/manufacturing/{kind}/{body['id']}").json()["entity"]


def detail(client, product):
    response = client.get(f"/api/manufacturing/products/{product['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def step(client, product, operation):
    product = detail(client, product)["entity"]
    response, body = command(
        client, f"/products/{product['id']}/steps", {"operationId": operation["id"]}, product["version"],
    )
    assert response.status_code == 200, response.text
    return next(s for s in detail(client, product)["steps"] if s["id"] == body["id"])


def test_directories_retry_versions_and_restart(app):
    client, settings = app
    response, body = command(client, "/products", {"name": "Gear", "partNumber": "G-1"})
    assert response.status_code == 200
    assert command(client, "/products", {}, body=body)[0].json() == response.json()
    product = client.get(f"/api/manufacturing/products/{body['id']}").json()["entity"]
    edited, _ = command(
        client, f"/products/{product['id']}", {"name": "Gear revised"}, product["version"],
        product["id"], "PUT",
    )
    assert edited.status_code == 200
    stale, _ = command(
        client, f"/products/{product['id']}", {"name": "Lost change"},
        product["version"], product["id"], "PUT",
    )
    assert stale.status_code == 409
    assert client.get("/api/manufacturing/products?search=gear&limit=1").json()["total"] == 1
    repo = ManufacturingRepository(settings)
    assert repo.read("products", product["id"])["name"] == "Gear revised"
    assert repo.read("products", product["id"])["stepIds"] == []
    with TestClient(create_app(settings)) as restarted:
        restored = restarted.get(f"/api/manufacturing/products/{product['id']}").json()
        assert restored["entity"]["name"] == "Gear revised"
        assert restored["generation"] == client.get("/api/manufacturing/meta").json()["generation"]


def test_product_search_matches_name_and_part_number_once_with_pagination(app):
    client, _ = app
    gear = create(client, "products", name="Spur Gear", partNumber="SG-042")
    housing = create(client, "products", name="SG-042 Housing", partNumber="sg-043")
    create(client, "products", name="Unrelated", notes="SG-042", partNumber="")
    for search, expected in [
        ("gEaR", {gear["id"]}), (" SG-042 ", {gear["id"], housing["id"]}),
        ("g-04", {gear["id"], housing["id"]}), ("043", {housing["id"]}),
        ("unmatched", set()),
    ]:
        result = client.get("/api/manufacturing/products", params={"search": search}).json()
        assert {row["id"] for row in result["items"]} == expected
        assert result["total"] == len(expected)
    pages = [
        client.get("/api/manufacturing/products", params={
            "search": "sg-04", "offset": offset, "limit": 1,
        }).json()
        for offset in range(3)
    ]
    assert [page["total"] for page in pages] == [2, 2, 2]
    assert [row["id"] for page in pages for row in page["items"]] == [housing["id"], gear["id"]]
    edited, _ = command(
        client, f"/products/{gear['id']}", {"name": gear["name"], "partNumber": "GX-975"},
        gear["version"], gear["id"], "PUT",
    )
    assert edited.status_code == 200
    assert client.get("/api/manufacturing/products?search=gx-975").json()["items"][0]["id"] == gear["id"]
    assert client.get("/api/manufacturing/products?search=sg-042").json()["total"] == 1
    updated = detail(client, gear)["entity"]
    assert command(
        client, f"/products/{gear['id']}", {"name": gear["name"], "partNumber": ""},
        updated["version"], gear["id"], "PUT",
    )[0].status_code == 200
    assert client.get("/api/manufacturing/products?search=gx-975").json()["total"] == 0
    assert client.get("/api/manufacturing/products?search=GEAR").json()["total"] == 1


def test_legacy_product_search_index_preserves_product_values_versions_and_step_links(app):
    client, settings = app
    gear = create(client, "products", name="Legacy Gear", partNumber="LEG-042")
    operation = create(client, "operations", name="Cut")
    step(client, gear, operation)
    repo = ManufacturingRepository(settings)
    repo.ref("products", gear["id"]).update({"searchChars": firestore.DELETE_FIELD})
    repo.workspace.update({"productSearchVersion": 0})
    before = repo.detail("products", gear["id"])
    upgraded = ManufacturingRepository(settings)
    after = upgraded.detail("products", gear["id"])
    assert after["entity"].pop("searchChars") == sorted(set("legacy gear leg-042"))
    assert before == after
    assert upgraded.list("products", "leg-042")["items"][0]["id"] == gear["id"]
    version = upgraded.workspace.get().to_dict()["version"]
    ManufacturingRepository(settings)
    assert upgraded.workspace.get().to_dict()["version"] == version


def test_product_scoped_assignments_primary_contact_validation_and_retry(app):
    client, _ = app
    gear = create(client, "products", name="Gear")
    bracket = create(client, "products", name="Bracket")
    laser = create(client, "operations", name="Laser cutting")
    first, second = step(client, gear, laser), step(client, bracket, laser)
    a = create(client, "suppliers", name="Company A")
    b = create(client, "suppliers", name="Company B")
    contact = create(client, "contacts", name="A contact", supplierId=a["id"])
    wrong = create(client, "contacts", name="B contact", supplierId=b["id"])
    bad, _ = command(
        client, f"/steps/{first['id']}/assignments",
        {"supplierId": a["id"], "contactId": wrong["id"]}, first["version"],
    )
    assert bad.status_code == 422
    assigned, assignment = command(
        client, f"/steps/{first['id']}/assignments",
        {"supplierId": a["id"], "contactId": contact["id"]}, first["version"],
    )
    assert assigned.status_code == 200
    view = detail(client, gear)
    first = view["steps"][0]
    assert first["primaryAssignmentId"] == assignment["id"]
    assert detail(client, bracket)["steps"][0]["primaryAssignmentId"] is None
    assert detail(client, bracket)["assignments"] == []
    duplicate, _ = command(
        client, f"/steps/{first['id']}/assignments", {"supplierId": a["id"]}, first["version"],
    )
    assert duplicate.status_code == 409
    added, other = command(
        client, f"/steps/{first['id']}/assignments", {"supplierId": b["id"]}, first["version"],
    )
    assert added.status_code == 200
    first = detail(client, gear)["steps"][0]
    assert first["primaryAssignmentId"] == assignment["id"]
    not_own, _ = command(
        client, f"/steps/{second['id']}/primary", {"assignmentId": assignment["id"]}, second["version"],
    )
    assert not_own.status_code == 422
    missing_replacement, _ = command(
        client, f"/steps/{first['id']}/assignments/{assignment['id']}/remove", {},
        first["version"], assignment["id"],
    )
    assert missing_replacement.status_code == 422
    removed, removal = command(
        client, f"/steps/{first['id']}/assignments/{assignment['id']}/remove",
        {"replacementId": other["id"]}, first["version"], assignment["id"],
    )
    assert removed.status_code == 200
    assert command(
        client, f"/steps/{first['id']}/assignments/{assignment['id']}/remove", {}, body=removal,
    )[0].status_code == 200
    first = detail(client, gear)["steps"][0]
    assert first["primaryAssignmentId"] == other["id"]
    assert command(
        client, f"/steps/{first['id']}/assignments/{other['id']}/remove", {}, first["version"], other["id"],
    )[0].status_code == 200
    assert detail(client, gear)["steps"][0]["primaryAssignmentId"] is None
    assert client.get(f"/api/manufacturing/suppliers/{a['id']}").json()["contacts"][0]["id"] == contact["id"]


def test_people_internal_contacts_reverse_links_and_reorder(app):
    client, _ = app
    product = create(client, "products", name="Gear")
    operation = create(client, "operations", name="Inspection")
    first, second = step(client, product, operation), step(client, product, operation)
    person = create(client, "suppliers", name="Independent specialist", type="individual")
    internal = create(client, "suppliers", name="Our company", relationship="internal")
    contact = create(client, "contacts", name="Inspector", supplierId=internal["id"])
    assert command(
        client, "/contacts", {"name": "Duplicate person", "supplierId": person["id"]},
    )[0].status_code == 422
    response, assignment = command(
        client, f"/steps/{first['id']}/assignments",
        {"supplierId": internal["id"], "contactId": contact["id"]}, first["version"],
    )
    assert response.status_code == 200
    assert command(
        client, f"/steps/{second['id']}/assignments", {"supplierId": person["id"]}, second["version"],
    )[0].status_code == 200
    view = detail(client, product)
    order = [second["id"], first["id"]]
    assert command(
        client, f"/products/{product['id']}/reorder", {"stepIds": order}, view["entity"]["version"],
    )[0].status_code == 200
    view = detail(client, product)
    assert [s["id"] for s in view["steps"]] == order
    assert next(s for s in view["steps"] if s["id"] == first["id"])["primaryAssignmentId"] == assignment["id"]
    reverse = client.get(f"/api/manufacturing/contacts/{contact['id']}").json()
    assert reverse["products"][0]["id"] == product["id"]
    assert reverse["assignments"][0]["id"] == assignment["id"]
    assert client.get(f"/api/manufacturing/operations/{operation['id']}").json()["steps"]
    edited, _ = command(
        client, f"/steps/{first['id']}",
        {"operationId": operation["id"], "instructions": "Inspect carefully"},
        next(s for s in view["steps"] if s["id"] == first["id"])["version"], first["id"], "PUT",
    )
    assert edited.status_code == 200, edited.text


def test_workspace_fence_and_no_sales_data_reuse(app):
    client, settings = app
    assert client.get("/api/accounts").json()["total"] == 0
    for headers in [
        {"X-SalesTrackr-Request": "1"},
        {
            "X-SalesTrackr-Request": "1",
            "X-ProductTrackr-Workspace": "wrong", "X-ProductTrackr-Generation": "wrong",
        },
    ]:
        assert command(client, "/products", {"name": "Blocked"}, headers=headers)[0].status_code == 409
    assert client.get("/api/manufacturing/products").json()["total"] == 0
    create(client, "suppliers", name="Manufacturing only")
    assert client.get("/api/accounts").json()["total"] == 0
    with pytest.raises(RuntimeError):
        ManufacturingRepository(replace(settings, mode="production"))
    with pytest.raises(RuntimeError):
        ManufacturingRepository(replace(settings, manufacturing_workspace=settings.firestore_workspace))


def test_concurrent_assignments_preserve_one_primary(app):
    client, _ = app
    product = create(client, "products", name="Gear")
    operation = create(client, "operations", name="Cut")
    current = step(client, product, operation)
    suppliers = [create(client, "suppliers", name=f"Company {n}") for n in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda p: command(
                client, f"/steps/{current['id']}/assignments", {"supplierId": p["id"]}, current["version"],
            )[0].status_code,
            suppliers,
        ))
    assert sorted(results) == [200, 409]
    view = detail(client, product)
    assert len(view["assignments"]) == 1
    assert view["steps"][0]["primaryAssignmentId"] == view["assignments"][0]["id"]


def test_edits_preserve_relationships_and_reject_foreign_references(app):
    client, settings = app
    company = create(client, "suppliers", name="Company")
    another = create(client, "suppliers", name="Another company")
    contact = create(client, "contacts", name="First contact", supplierId=company["id"])
    second_contact = create(client, "contacts", name="Second contact", supplierId=company["id"])
    product = create(client, "products", name="Gear")
    operation = create(client, "operations", name="Cut")
    current = step(client, product, operation)
    added, assignment = command(
        client, f"/steps/{current['id']}/assignments",
        {"supplierId": company["id"], "contactId": contact["id"]}, current["version"],
    )
    assert added.status_code == 200
    current = detail(client, product)["steps"][0]
    changed, _ = command(
        client, f"/steps/{current['id']}/assignments/{assignment['id']}",
        {"supplierId": company["id"], "contactId": second_contact["id"], "notes": "New contact"},
        current["version"], assignment["id"], "PUT",
    )
    assert changed.status_code == 200
    view = detail(client, product)
    assert view["steps"][0]["primaryAssignmentId"] == assignment["id"]
    assert view["assignments"][0]["contactId"] == second_contact["id"]
    assert command(
        client, f"/contacts/{contact['id']}", {"name": "Moved", "supplierId": another["id"]},
        contact["version"], contact["id"], "PUT",
    )[0].status_code == 422
    assert command(
        client, f"/suppliers/{company['id']}", {"name": "Changed", "type": "individual"},
        company["version"], company["id"], "PUT",
    )[0].status_code == 422
    assert command(
        client, f"/products/{product['id']}/reorder", {"stepIds": [current["id"], current["id"]]},
        view["entity"]["version"],
    )[0].status_code == 422
    assert command(
        client, f"/products/{product['id']}/steps", {"operationId": uuid4().hex},
        view["entity"]["version"],
    )[0].status_code == 404
    response, body = command(client, "/products", {"name": "Receipt conflict"})
    assert response.status_code == 200
    body["data"]["name"] = "Different body"
    assert command(client, "/products", {}, body=body)[0].status_code == 409
    old_meta = client.get("/api/manufacturing/meta").json()
    repo = ManufacturingRepository(settings)
    repo.workspace.update({"generation": uuid4().hex})
    assert command(client, "/products", {"name": "Old page"}, headers={
        "X-SalesTrackr-Request": "1", "X-ProductTrackr-Workspace": old_meta["workspaceId"],
        "X-ProductTrackr-Generation": old_meta["generation"],
    })[0].status_code == 409


def test_manufacturing_is_opt_in_and_uses_shared_request_protection(tmp_path):
    settings = Settings("demo", tmp_path, "", "", "", "", False)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/manufacturing/meta").status_code == 503
        response = client.post("/api/manufacturing/products", json={
            "id": uuid4().hex, "writeId": uuid4().hex, "data": {"name": "Not authorized"},
        })
        assert response.status_code == 403
