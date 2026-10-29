import io
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pypdf import PdfReader

from backend.app.domain import Problem, today
from backend.app.main import create_app
from backend.app.manufacturing_spreadsheet import PATH
from backend.app.manufacturing_storage import ManufacturingRepository
from backend.app.shared_backups import archive, read_archive
from backend.tests.test_firestore import headers, write
from backend.tests.test_manufacturing import app as base_manufacturing_app
from backend.tests.test_manufacturing import command, create
from backend.tests.test_shared_backups import backup


@pytest.fixture
def workspace(manufacturing_app):
    client, settings = manufacturing_app
    repo = client.app.state.repo

    def save(kind, data, **kwargs):
        if kind == "record" and "parts" in data:
            data = {**data, "parts": [{"quantity": 1, **line} for line in data["parts"]]}
        response, _ = write(client, repo, kind, data, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()["revision"]

    model = save("model", {"name": "Lithrone"})
    other_model = save("model", {"name": "Spica"})
    account = save("account", {"name": "Demo printing company"})
    machines = []
    for index, model_row in enumerate([model, model, other_model]):
        machine = {
            "id": uuid4().hex, "name": f"Press {index + 1}", "modelId": model_row["entityId"],
        }
        account = save(
            "account", machine, current=account,
            route=f"/api/accounts/{account['entityId']}/machines/{machine['id']}", method="PUT",
        )
        machines.append(machine)
    common = create(client, "products", name="Common bearing", partNumber="BRG-1")
    specific = create(
        client, "products", name="Lithrone gear", modelIds=[model["entityId"]], partNumber="GEAR-1",
    )
    return client, repo, save, account, machines, common, specific, settings


@pytest.fixture
def manufacturing_app(tmp_path):
    yield from base_manufacturing_app.__wrapped__(tmp_path)


def sale(workspace, parts, delivered_on=None, close=True):
    client, _, save, account, *_ = workspace
    day = delivered_on or today() - timedelta(days=4)
    with patch("backend.app.domain.today", return_value=day):
        row = save("record", {
            "accountId": account["entityId"], "acquiredOn": day.isoformat(), "parts": parts,
        })
        for stage in ("follow_up", "order_received", "dispatched", "delivered"):
            row = save(
                "record", {"stage": stage, "occurredOn": day.isoformat()}, current=row,
                route=f"/api/records/{row['entityId']}/stage",
            )
        if close:
            row = save("record", {"received": True}, current=row,
                       route=f"/api/records/{row['entityId']}/payment")
    return row


def history(workspace, **params):
    response = workspace[0].get("/api/parts/history", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_models_machines_compatibility_and_detail_edits_preserve_links(workspace):
    client, repo, save, account, machines, common, specific, _ = workspace
    duplicate, _ = write(client, repo, "model", {"name": " LITHRONE "})
    assert duplicate.status_code == 409
    assert write(client, repo, "model", {"name": "Common"})[0].status_code == 422
    changed = save(
        "account", {"name": account["data"]["name"], "notes": "Company edit"}, current=account,
        route=f"/api/accounts/{account['entityId']}", method="PATCH",
    )
    assert changed["data"]["machines"] == account["data"]["machines"]
    catalog = client.get("/api/parts/catalog", params={"modelId": machines[2]["modelId"]}).json()
    assert {p["id"] for p in catalog["items"]} == {common["id"]}
    invalid, _ = write(client, repo, "record", {
        "accountId": account["entityId"],
        "parts": [{"id": uuid4().hex, "productId": specific["id"], "machineId": machines[2]["id"]}],
    })
    assert invalid.status_code == 422
    invalid_model, _ = command(client, "/products", {"name": "Invalid", "modelIds": [uuid4().hex]})
    assert invalid_model.status_code == 422


def test_model_catalog_includes_common_and_multi_model_parts_with_notes(workspace):
    client, _, _, _, machines, common, specific, _ = workspace
    shared = create(
        client, "products", name="Shared sensor", partNumber="SENSOR-2",
        modelIds=[machines[0]["modelId"], machines[2]["modelId"]], notes="Use the blue connector.",
    )
    for selected, expected in (
        (machines[0]["modelId"], {common["id"], specific["id"], shared["id"]}),
        (machines[2]["modelId"], {common["id"], shared["id"]}),
    ):
        response = client.get("/api/parts/catalog", params={"modelId": selected})
        assert response.status_code == 200
        assert {row["id"] for row in response.json()["items"]} == expected
        sensor = next(row for row in response.json()["items"] if row["id"] == shared["id"])
        assert sensor["partNumber"] == "SENSOR-2" and sensor["notes"] == "Use the blue connector."
        pdf = client.get("/api/parts/catalog/pdf", params={"modelId": selected})
        assert pdf.status_code == 200, pdf.text
        text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.content)).pages)
        assert "Master Product's List" in text and "Common" in text and common["name"] in text
        assert "Use the blue connector." in text and "SENSOR-2" in text
        assert (specific["name"] in text) == (specific["id"] in expected)
    assert client.get("/api/parts/catalog", params={"modelId": uuid4().hex}).status_code == 404
    assert client.get("/api/parts/catalog/pdf", params={"modelId": uuid4().hex}).status_code == 404


def test_machine_parts_pdf_matches_filters_and_includes_all_pages(workspace):
    client, _, save, account, machines, common, specific, _ = workspace
    wrong = create(client, "products", name="Wrong-model excluded product",
                   modelIds=[machines[2]["modelId"]])
    for index in range(101):
        create(client, "products", name=f"Bulk compatible part {index:03}", partNumber=f"BULK-{index:03}")
    response, _ = command(client, f"/products/{common['id']}", {
        "name": "Common bearing & <filter>", "partNumber": common["partNumber"],
    }, common["version"], common["id"], "PUT")
    assert response.status_code == 200
    day = (today() - timedelta(days=5)).isoformat()
    save("sent_part", {"accountId": account["entityId"], "productId": common["id"], "sentOn": day},
         route="/api/parts/history")
    save("sent_part", {"accountId": account["entityId"], "productId": specific["id"],
                      "machineId": machines[1]["id"], "sentOn": day}, route="/api/parts/history")
    response = client.get("/api/parts/coverage/pdf", params={
        "accountId": account["entityId"], "machineId": machines[0]["id"],
    })
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    reader = PdfReader(io.BytesIO(response.content))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) > 1
    assert account["data"]["name"] in text and "Press 1" in text and "Lithrone" in text
    assert "Common bearing & <filter>" in text and "BRG-1" in text
    assert "Bulk compatible part 100" in text and "BULK-100" in text
    assert wrong["name"] not in text and "This machine" not in text
    assert "Account delivery" in text and "Not sent" in text
    assert all("Part number" in page.extract_text() for page in reader.pages)
    assert "Potential Spares machine wise" in text
    assert 'filename="potential-spares-machine-wise.pdf"' in response.headers["content-disposition"]
    master = client.get("/api/parts/catalog/pdf", params={"modelId": machines[0]["modelId"]})
    assert master.status_code == 200, master.text
    master_text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(master.content)).pages)
    assert "Bulk compatible part 100" in master_text and "BULK-100" in master_text
    assert wrong["name"] not in master_text
    other = save("account", {"name": "No machines"})
    assert client.get("/api/parts/coverage/pdf", params={
        "accountId": other["entityId"], "machineId": machines[0]["id"],
    }).status_code == 422


def test_sales_history_pdf_uses_all_filters_and_preserves_delivery_snapshots(workspace):
    client, _, save, account, machines, common, specific, _ = workspace
    wrong = create(client, "products", name="Spica only", modelIds=[machines[2]["modelId"]])
    day = (today() - timedelta(days=10)).isoformat()

    def delivery(product, machine_id=None, **extra):
        return save("sent_part", {
            "accountId": account["entityId"], "productId": product["id"],
            "machineId": machine_id, "sentOn": day, **extra,
        }, route="/api/parts/history")

    delivery(common, quantity=7)
    delivery(specific, machines[0]["id"], quantity=13)
    delivery(specific, machines[1]["id"], quantity=17)
    delivery(wrong)
    voided = delivery(specific)
    save("sent_part", {"reason": "Not delivered"}, current=voided,
         route=f"/api/parts/history/{voided['entityId']}/void")
    sale(workspace, [{"id": uuid4().hex, "productId": specific["id"], "machineId": machines[0]["id"]}],
         delivered_on=today() - timedelta(days=9))
    other = save("account", {"name": "Excluded account"})
    save("sent_part", {"accountId": other["entityId"], "productId": common["id"], "sentOn": day},
         route="/api/parts/history")
    filters = {"accountId": account["entityId"], "machineId": machines[0]["id"],
               "start": day, "end": day}

    def pdf(**params):
        response = client.get("/api/parts/history/pdf", params={**filters, **params})
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["cache-control"] == "no-store"
        return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages)

    text = pdf()
    assert "Sales History" in text and "Deliveries: 2" in text
    assert common["name"] in text and specific["name"] in text
    assert "13" in text and "7" in text and "Manual" in text
    assert "Press 2" not in text and wrong["name"] not in text and other["data"]["name"] not in text
    assert "Withdrawn" not in text
    text = pdf(includeVoided=True)
    assert "Deliveries: 3" in text and "Withdrawn" in text and "Not recorded" in " ".join(text.split())
    unassigned = pdf(machineId="unassigned", includeVoided=True)
    assert "Deliveries: 3" in unassigned and wrong["name"] in unassigned and "Press 1" not in unassigned
    assert "Deliveries: 0" in pdf(start=today().isoformat(), end=today().isoformat())
    assert "No deliveries found." in pdf(start=today().isoformat(), end=today().isoformat())
    assert "Sales record" in pdf(end=today().isoformat())
    assert client.get("/api/parts/history/pdf", params={"machineId": machines[0]["id"]}).status_code == 422
    assert client.get("/api/parts/history/pdf", params={**filters, "end": "2000-01-01"}).status_code == 422
    assert client.get("/api/parts/history/pdf", params={
        **filters, "accountId": other["entityId"],
    }).status_code == 422


def test_history_pdf_includes_every_matching_page_and_refuses_truncation(workspace):
    client, _, save, account, _, common, specific, _ = workspace
    day = (today() - timedelta(days=1)).isoformat()
    for index in range(52):
        save("sent_part", {
            "accountId": account["entityId"], "productId": common["id"], "sentOn": day,
            "quantity": index + 1, "acknowledgeDuplicate": True,
        }, route="/api/parts/history")
    save("sent_part", {
        "accountId": account["entityId"], "productId": specific["id"],
        "sentOn": (today() - timedelta(days=2)).isoformat(),
    }, route="/api/parts/history")
    response = client.get("/api/parts/history/pdf")
    assert response.status_code == 200, response.text
    reader = PdfReader(io.BytesIO(response.content))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) > 1
    assert "Deliveries: 53" in text and specific["name"] in text
    assert all("Delivered" in page.extract_text() for page in reader.pages)
    with patch("backend.app.parts_pdf.MAX_REPORT_ROWS", 1):
        for endpoint, params in (
            ("/api/parts/history/pdf", {}),
            ("/api/parts/catalog/pdf", {"modelId": workspace[4][0]["modelId"]}),
        ):
            limited = client.get(endpoint, params=params)
            assert limited.status_code == 413 and limited.json()["error"]["code"] == "report_size"


def test_master_pdf_preserves_long_escaped_notes_and_rejects_mixed_snapshots(workspace):
    from backend.app.parts_pdf import model_parts_pdf

    client, _, _, _, machines, _, specific, _ = workspace
    notes = "Start & <notes>\n" + "Keep this complete note. " * 390 + "\nFinal note."
    product = {**specific, "notes": notes}
    content = model_parts_pdf("Lithrone", [product])
    reader = PdfReader(io.BytesIO(content))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) > 1 and "Start & <notes>" in text and "Final note." in text
    assert all("Part number" in page.extract_text() for page in reader.pages)
    original = model_parts_pdf

    def changed(*args):
        result = original(*args)
        repo = client.app.state.repo
        control = repo.workspace.get().to_dict()
        repo.workspace.update({"sequence": control["sequence"] + 1})
        return result

    with patch("backend.app.parts_pdf.model_parts_pdf", side_effect=changed):
        response = client.get("/api/parts/catalog/pdf", params={"modelId": machines[0]["modelId"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "report_changed"


def test_closure_corrections_reopening_reactivation_and_retry_do_not_duplicate(workspace):
    client, repo, save, account, machines, common, specific, _ = workspace
    line = {"id": uuid4().hex, "productId": common["id"], "machineId": machines[0]["id"]}
    row = sale(workspace, [line], close=False)
    assert history(workspace)["total"] == 0
    response, ticket = write(client, repo, "record", {"received": True},
                             current=row, route=f"/api/records/{row['entityId']}/payment")
    assert response.status_code == 200, response.text
    row = response.json()["revision"]
    retried = client.post(f"/api/records/{row['entityId']}/payment", json=ticket, headers=headers(repo))
    assert retried.json()["revision"] == row
    entries = history(workspace)["items"]
    assert len(entries) == 1 and entries[0]["data"]["sentOn"] == row["data"]["stageDates"]["delivered"]
    entry_id = entries[0]["entityId"]
    next_line = {"id": uuid4().hex, "productId": specific["id"]}
    row = save("record", {"parts": [line, next_line], "reason": "Missed a gear"}, current=row,
               route=f"/api/records/{row['entityId']}/parts", method="PATCH")
    assert history(workspace)["total"] == 2
    corrected_day = today() - timedelta(days=1)
    row = save("record", {
        "stage": "delivered", "occurredOn": corrected_day.isoformat(), "reason": "Correct delivery date",
    }, current=row, route=f"/api/records/{row['entityId']}/stage")
    assert history(workspace)["total"] == 2
    assert {e["data"]["sentOn"] for e in history(workspace)["items"]} == {corrected_day.isoformat()}
    row = save("record", {"stage": "dispatched", "reason": "Delivery was recorded too early"},
               current=row, route=f"/api/records/{row['entityId']}/stage")
    assert history(workspace)["total"] == 0
    assert history(workspace, includeVoided=True)["total"] == 2
    row = save("record", {"stage": "delivered"}, current=row,
               route=f"/api/records/{row['entityId']}/stage")
    assert history(workspace)["total"] == 2
    row = save("record", {"parts": [next_line], "reason": "Bearing not included"}, current=row,
               route=f"/api/records/{row['entityId']}/parts", method="PATCH")
    assert history(workspace)["total"] == 1
    audit = client.get(f"/api/entities/sent_part/{entry_id}/history").json()
    assert audit["total"] == 5


def test_manual_history_dates_coverage_voids_and_audit(workspace):
    client, repo, save, account, machines, common, specific, _ = workspace
    delivered = today() - timedelta(days=30)
    row = save("sent_part", {
        "accountId": account["entityId"], "productId": common["id"], "sentOn": delivered.isoformat(),
    }, route="/api/parts/history")
    for selected in machines:
        coverage = client.get("/api/parts/coverage", params={
            "accountId": account["entityId"], "machineId": selected["id"],
        }).json()
        bearing = next(p for p in coverage["items"] if p["id"] == common["id"])
        assert bearing["lastSentOn"] == delivered.isoformat()
        assert not bearing["machineSpecified"]
    duplicate, _ = write(client, repo, "sent_part", {
        "accountId": account["entityId"], "productId": common["id"], "sentOn": delivered.isoformat(),
    }, route="/api/parts/history")
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "duplicate_delivery"
    row = save("sent_part", {
        "accountId": account["entityId"], "productId": common["id"],
        "sentOn": (delivered + timedelta(days=1)).isoformat(), "reason": "Corrected old delivery date",
    }, current=row, route=f"/api/parts/history/{row['entityId']}", method="PATCH")
    assert history(workspace, end=delivered.isoformat())["total"] == 0
    row = save("sent_part", {"reason": "Entered twice"}, current=row,
               route=f"/api/parts/history/{row['entityId']}/void")
    assert not row["data"]["active"] and history(workspace)["total"] == 0
    assert history(workspace, includeVoided=True)["total"] == 1
    assert client.get(f"/api/entities/sent_part/{row['entityId']}/history").json()["total"] == 3


def test_deleted_sale_history_and_combined_restore_roundtrip(workspace):
    client, repo, save, _, machines, common, _, settings = workspace
    row = sale(workspace, [{
        "id": uuid4().hex, "productId": common["id"], "machineId": machines[0]["id"],
    }])
    assert history(workspace)["items"][0]["sourceRecordAvailable"]
    row = save("record", {"received": True}, current=row,
               route=f"/api/records/{row['entityId']}/feedback")
    deleted = client.post("/api/settings/record-cleanup", headers=headers(repo), json={
        "writeId": uuid4().hex, "confirmation": "DELETE",
        "records": [{"entityId": row["entityId"], "baseRevision": row["id"]}],
    })
    assert deleted.status_code == 200, deleted.text
    assert history(workspace)["total"] == 1
    assert not history(workspace)["items"][0]["sourceRecordAvailable"]
    assert client.get(f"/api/records/{row['entityId']}/detail").status_code == 404
    archive = backup(client)
    recovery = client.app.state.recovery
    content, _ = recovery.get(archive["id"])
    data, manifest = read_archive(content)
    assert manifest["counts"]["sent_part"] == 1 and manifest["counts"]["model"] == 2
    assert not any(r["entityType"] == "record" for r in data["sales"])
    preview = recovery.inspect(content, "local-demo")
    recovery.restore(content, "local-demo", preview["token"], "RESTORE", uuid4().hex)
    with TestClient(create_app(replace(settings))) as restarted:
        entries = restarted.get("/api/parts/history").json()
        assert entries["total"] == 1
        assert entries["items"][0]["data"]["sourceRecordId"] == row["entityId"]


def test_related_product_change_cannot_partially_close_a_sale(workspace):
    client, repo, _, _, machines, common, _, settings = workspace
    row = sale(workspace, [{
        "id": uuid4().hex, "productId": common["id"], "machineId": machines[0]["id"],
    }], close=False)
    original = repo._store_payload
    changed = False

    def store(value):
        nonlocal changed
        if not changed:
            changed = True
            ref = ManufacturingRepository(settings, client=repo.client).ref("products", common["id"])
            ref.update({"version": common["version"] + 1})
        return original(value)

    with patch.object(repo, "_store_payload", side_effect=store):
        response, body = write(client, repo, "record", {"received": True}, current=row,
                               route=f"/api/records/{row['entityId']}/payment")
    assert response.status_code == 409
    assert not repo.current(row["entityId"])["data"]["stageDates"].get("closed")
    assert history(workspace)["total"] == 0
    retry = client.post(f"/api/records/{row['entityId']}/payment", json=body, headers=headers(repo))
    assert retry.status_code == 200 and history(workspace)["total"] == 1


def test_publication_requires_closure_and_account_corrections_require_a_reason(workspace):
    client, repo, save, account, _, common, *_ = workspace
    row = sale(workspace, [{"id": uuid4().hex, "productId": common["id"]}], close=False)
    premature = {
        **row, "id": uuid4().hex, "parents": [row["id"]], "fingerprint": uuid4().hex,
        "data": {**row["data"], "partsPublished": True},
    }
    repo.commit(premature)
    row = repo.current(row["entityId"])
    assert not row["data"]["partsPublished"] and history(workspace)["total"] == 0
    row = save("record", {"received": True}, current=row,
               route=f"/api/records/{row['entityId']}/payment")
    other = save("account", {"name": "Another customer"})
    changed, _ = write(client, repo, "record", {"accountId": other["entityId"]},
                       current=row, route=f"/api/records/{row['entityId']}", method="PATCH")
    assert changed.status_code == 422
    assert history(workspace)["items"][0]["data"]["accountId"] == account["entityId"]


def test_repeated_product_lines_cannot_mix_reference_versions_during_publication(workspace):
    client, repo, _, _, machines, common, *_ = workspace
    row = sale(workspace, [
        {"id": uuid4().hex, "productId": common["id"], "machineId": machine["id"]}
        for machine in machines[:2]
    ], close=False)
    original = ManufacturingRepository.required
    changed = False

    def required(snapshot):
        nonlocal changed
        value = original(snapshot)
        if snapshot.id == common["id"] and not changed:
            changed = True
            snapshot.reference.update({"version": value["version"] + 1})
        return value

    with patch.object(ManufacturingRepository, "required", side_effect=required):
        response, body = write(client, repo, "record", {"received": True}, current=row,
                               route=f"/api/records/{row['entityId']}/payment")
    assert response.status_code == 409 and history(workspace)["total"] == 0
    assert not repo.current(row["entityId"])["data"]["stageDates"].get("closed")
    retry = client.post(f"/api/records/{row['entityId']}/payment", json=body, headers=headers(repo))
    assert retry.status_code == 200 and history(workspace)["total"] == 2


def test_products_workbook_has_models_and_preserves_its_file_id(workspace):
    client, repo, save, _, machines, common, specific, _ = workspace
    meta = client.get("/api/manufacturing/meta").json()
    request_headers = {
        "X-SalesTrackr-Request": "1", "X-ProductTrackr-Workspace": meta["workspaceId"],
        "X-ProductTrackr-Generation": meta["generation"],
    }

    def export():
        response = client.post(PATH, headers=request_headers, json={"writeId": uuid4().hex})
        assert response.status_code == 200, response.text
        content = client.get(f"{PATH}/download").content
        return response.json()["id"], load_workbook(io.BytesIO(content))

    file_id, book = export()
    values = list(book["Products"].values)
    rows = {row[1]: dict(zip(values[0], row)) for row in values[1:]}
    assert rows[common["name"]]["Models"] == "Common"
    assert rows[specific["name"]]["Models"] == "Lithrone"
    model = repo.current(machines[0]["modelId"], "model")
    save("model", {"name": "Lithrone revised"}, current=model,
         route=f"/api/models/{model['entityId']}", method="PATCH")
    next_id, book = export()
    assert next_id == file_id
    values = list(book["Products"].values)
    rows = {row[1]: dict(zip(values[0], row)) for row in values[1:]}
    assert rows[specific["name"]]["Models"] == "Lithrone revised"


@pytest.mark.parametrize("field", ["accountId", "productId", "machineId", "machineModelId"])
def test_combined_backup_rejects_missing_delivery_references(workspace, field):
    client, _, save, account, machines, common, *_ = workspace
    row = save("sent_part", {
        "accountId": account["entityId"], "productId": common["id"],
        "machineId": machines[0]["id"], "sentOn": today().isoformat(),
    }, route="/api/parts/history")
    saved = backup(client)
    content, _ = client.app.state.recovery.get(saved["id"])
    data, manifest = read_archive(content)
    entry = next(version for version in data["sales"] if version["id"] == row["id"])
    entry["data"][field] = uuid4().hex
    with pytest.raises(Problem) as failure:
        archive(data, source="manual", operation_id=uuid4().hex, actor="local-demo", control=manifest)
    assert failure.value.code == "invalid_backup"
