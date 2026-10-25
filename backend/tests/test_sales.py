from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.domain import (
    STAGES,
    AccountData,
    FeedbackChange,
    FollowUp,
    PaymentChange,
    Problem,
    RecordData,
    Revision,
    StageChange,
    add_followup,
    feedback_change,
    now,
    payment_change,
    record_stage,
    stage_change,
    today,
)
from backend.app.main import create_app
from backend.app.queries import VIEWS, query_records
from backend.app.storage import LocalBackend, Repository, digest

HEADERS = {"X-SalesTrackr-Request": "1"}


@pytest.fixture
def client(tmp_path):
    settings = Settings("demo", tmp_path, "", "", "", "", False)
    with TestClient(create_app(settings)) as client:
        yield client


def prepare(client, kind, entity_id=None):
    response = client.post(
        "/api/writes/prepare", json={"entityType": kind, "entityId": entity_id}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()


def write(client, kind, data, route=None, current=None, method="POST", ticket=None):
    ticket = ticket or prepare(client, kind, current["entityId"] if current else None)
    body = {**ticket, "baseRevision": current["id"] if current else None, "data": data}
    response = client.request(
        method,
        route or f"/api/{kind}s",
        json=body,
        headers=HEADERS,
    )
    return response, body


def new_sale(client):
    response, _ = write(client, "account", {"name": "Example customer"})
    assert response.status_code == 200, response.text
    account = response.json()["revision"]
    response, _ = write(
        client, "record", {"accountId": account["entityId"], "productNotes": "Roller and belt enquiry"}
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def advance(client, revision, stage, **extra):
    response, _ = write(
        client, "record", {"stage": stage, **extra}, f"/api/records/{revision['entityId']}/stage", revision
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def test_record_detail_bundles_linked_data_with_one_synchronization(client):
    row = new_sale(client)
    response, _ = write(
        client, "contact", {"accountId": row["data"]["accountId"], "name": "Example contact"}
    )
    contact = response.json()["revision"]
    response, _ = write(
        client, "record",
        {"accountId": row["data"]["accountId"], "contactId": contact["entityId"]},
        f"/api/records/{row['entityId']}", row, method="PATCH",
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    row = saved["revision"]
    assert saved["revisionIsCurrent"] is True
    assert saved["related"]["contact"] == contact
    repo = client.app.state.repo
    with patch.object(repo, "sync", wraps=repo.sync) as sync:
        for kind, entity_id in (
            ("record", row["entityId"]),
            ("account", row["data"]["accountId"]),
            ("contact", contact["entityId"]),
        ):
            assert client.get(f"/api/entities/{kind}/{entity_id}").status_code == 200
        assert sync.call_count == 3
        sync.reset_mock()
        with patch.object(repo, "read_history", wraps=repo.read_history) as history:
            response = client.get(f"/api/records/{row['entityId']}/detail")
            assert response.status_code == 200, response.text
            sync.assert_called_once_with(force=True)
            history.assert_not_called()
    detail = response.json()
    assert detail["revision"] == row
    assert detail["related"] == saved["related"]
    assert detail["related"]["account"] == repo.current(row["data"]["accountId"], "account")
    assert detail["related"]["errors"] == {}
    assert detail["complete"] is True


def test_record_detail_retains_link_errors_without_hiding_the_sale(client):
    row = new_sale(client)
    repo = client.app.state.repo
    current = repo.current

    def missing_account(entity_id, kind=None):
        if kind == "account":
            raise Problem("Account has conflicting versions.", 409, "record_conflict")
        return current(entity_id, kind)

    with patch.object(repo, "current", side_effect=missing_account):
        response = client.get(f"/api/records/{row['entityId']}/detail")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["revision"] == row
    assert result["related"] == {
        "account": None, "contact": None, "errors": {"account": "Account has conflicting versions."}
    }
    assert client.get(f"/api/records/{uuid4().hex}/detail").status_code == 404


def test_save_retry_identifies_a_superseded_revision(client):
    original = new_sale(client)
    route = f"/api/records/{original['entityId']}/product-notes"
    response, body = write(
        client, "record", {"productNotes": "First update"}, route, original, method="PATCH"
    )
    assert response.status_code == 200, response.text
    first = response.json()["revision"]
    assert response.json()["revisionIsCurrent"] is True
    response, _ = write(
        client, "record", {"productNotes": "Newer update"}, route, first, method="PATCH"
    )
    assert response.status_code == 200, response.text
    newest = response.json()["revision"]
    retry = client.patch(route, json=body, headers=HEADERS)
    assert retry.status_code == 200, retry.text
    assert retry.json()["revision"] == first
    assert retry.json()["revisionIsCurrent"] is False
    detail = client.get(f"/api/records/{original['entityId']}/detail").json()
    assert detail["revision"] == newest
    assert client.get(f"/api/entities/record/{original['entityId']}/history").json()["total"] == 3


def test_full_journey_and_independent_payment(client):
    row = new_sale(client)
    assert row["data"]["stageDates"] == {"new_lead": today().isoformat()}
    assert row["data"]["workflowVersion"] == 3
    for stage in STAGES[1:3]:
        row = advance(client, row, stage)
    orders = client.get("/api/records?view=orders").json()
    assert orders["total"] == 1
    assert client.get("/api/records?view=pending").json()["total"] == 1
    assert "pendingPaise" not in client.get("/api/dashboard").json()
    response, _ = write(client, "record", {"received": True}, f"/api/records/{row['entityId']}/payment", row)
    assert response.status_code == 200, response.text
    row = response.json()["revision"]
    assert row["data"]["paymentReceivedOn"] == today().isoformat()
    assert len(row["data"]["stageDates"]) == 3
    assert client.get("/api/records?view=pending").json()["total"] == 0
    assert client.get("/api/records?view=orders").json()["total"] == 1
    row = advance(client, row, "dispatched")
    assert client.get("/api/records?view=orders").json()["total"] == 0
    row = advance(client, row, "delivered")
    row = advance(client, row, "closed")
    assert client.get("/api/records?view=dispatched").json()["total"] == 0
    assert client.get("/api/records?view=delivered").json()["total"] == 0
    assert client.get("/api/records?view=closed").json()["total"] == 1
    assert client.get(f"/api/entities/record/{row['entityId']}/history").json()["total"] == 7
    storage = client.app.state.repo.backend
    assert len(list(storage.directory.glob("*.json"))) == 2
    assert storage.get(row["entityId"])["current"] == row
    assert len(storage.get(row["entityId"])["history"]) == 6


def test_receipt_closes_delivered_sale_and_is_retry_safe(client):
    row = new_sale(client)
    for stage in STAGES[1:5]:
        row = advance(client, row, stage)
    stage_dates = row["data"]["stageDates"]
    route = f"/api/records/{row['entityId']}/payment"
    response, _ = write(client, "record", {"note": "Awaiting bank confirmation"}, route, row)
    assert response.status_code == 200, response.text
    row = response.json()["revision"]
    assert row["data"]["paymentReceived"] is False
    assert row["data"]["stageDates"] == stage_dates
    rejected, _ = write(client, "record", {"stage": "closed"}, f"/api/records/{row['entityId']}/stage", row)
    assert rejected.status_code == 422
    response, body = write(client, "record", {"received": True, "note": "Bank confirmed"}, route, row)
    assert response.status_code == 200, response.text
    row = response.json()["revision"]
    assert row["data"]["paymentReceived"] is True
    assert row["data"]["stageDates"] == {**stage_dates, "closed": today().isoformat()}
    assert row["data"]["paymentReceivedOn"] == today().isoformat()
    assert row["data"]["orderAmountPaise"] is None
    assert row["data"]["amountReceivedPaise"] is None
    assert row["data"]["paymentNote"] == "Bank confirmed"
    assert row["reason"] == ""
    retry = client.post(route, json=body, headers=HEADERS)
    assert retry.status_code == 200
    assert retry.json()["revision"]["id"] == row["id"]
    assert client.get("/api/records?view=pending").json()["total"] == 0
    assert client.get("/api/dashboard").json()["counts"]["closed"] == 1
    assert client.get("/api/records?view=paid").json()["total"] == 1
    rejected, _ = write(client, "record", {"received": False}, route, row)
    assert rejected.status_code == 422
    assert "Reopen" in rejected.json()["error"]["message"]
    history = client.get(f"/api/entities/record/{row['entityId']}/history").json()["items"]
    assert len(history) == 7
    assert history[1]["data"]["paymentNote"] == "Awaiting bank confirmation"
    assert "closed" not in history[1]["data"]["stageDates"]


def test_feedback_receipt_queue_retry_history_and_reopening(client):
    row = new_sale(client)
    route = f"/api/records/{row['entityId']}/feedback"
    assert row["data"]["feedbackReceived"] is False
    assert row["data"]["feedbackReceivedOn"] is None
    for invalid in ({}, {"received": True}, {"received": False}):
        rejected, _ = write(client, "record", invalid, route, row)
        assert rejected.status_code == 422
    for stage in STAGES[1:5]:
        row = advance(client, row, stage)
        assert client.get("/api/records?view=feedback-pending").json()["total"] == 0
    rejected, _ = write(client, "record", {"received": True}, route, row)
    assert rejected.status_code == 422
    response, _ = write(client, "record", {"received": True}, route.replace("/feedback", "/payment"), row)
    assert response.status_code == 200, response.text
    closed = response.json()["revision"]
    pending = client.get("/api/records?view=feedback-pending").json()
    assert [item["entityId"] for item in pending["items"]] == [closed["entityId"]]
    assert client.get("/api/dashboard").json()["counts"]["feedback-pending"] == 1
    response, body = write(client, "record", {"received": True}, route, closed)
    assert response.status_code == 200, response.text
    received = response.json()["revision"]
    assert received["action"] == "feedback"
    assert received["data"] == {
        **closed["data"], "feedbackReceived": True, "feedbackReceivedOn": today().isoformat()
    }
    retry = client.post(route, json=body, headers=HEADERS)
    assert retry.status_code == 200 and retry.json()["revision"] == received
    stale, _ = write(client, "record", {"received": False}, route, closed)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_edit"
    assert client.get("/api/records?view=feedback-pending").json()["total"] == 0
    counts = client.get("/api/dashboard").json()["counts"]
    assert counts["feedback-pending"] == 0 and counts["closed"] == counts["paid"] == 1
    history = client.get(f"/api/entities/record/{row['entityId']}/history").json()["items"]
    assert history[0] == received and history[1] == closed
    recovered = Repository(client.app.state.repo.backend)
    recovered.sync(force=True)
    assert recovered.current(row["entityId"]) == received
    reopened, _ = write(
        client, "record", {"stage": "delivered", "reason": "Correct closure"},
        route.replace("/feedback", "/stage"), received,
    )
    assert reopened.status_code == 422
    assert "Undo feedback" in reopened.json()["error"]["message"]
    response, _ = write(client, "record", {"received": False}, route, received)
    assert response.status_code == 200, response.text
    undone = response.json()["revision"]
    assert undone["data"]["feedbackReceived"] is False
    assert undone["data"]["feedbackReceivedOn"] is None
    assert client.get("/api/records?view=feedback-pending").json()["total"] == 1
    reopened = advance(client, undone, "delivered", reason="Correct closure")
    assert client.get("/api/records?view=feedback-pending").json()["total"] == 0
    advance(client, reopened, "closed")
    assert client.get("/api/records?view=feedback-pending").json()["total"] == 1


@pytest.mark.parametrize("stage_index", range(5))
def test_feedback_requires_closure(stage_index):
    for received in (False, True):
        with pytest.raises(Problem, match="Close the sales record"):
            feedback_change(record_at(stage_index), FeedbackChange(received=received))


def test_feedback_dates_and_legacy_records():
    closed = record_at(5, True)
    legacy = closed.model_dump(mode="json", exclude={"feedbackReceived", "feedbackReceivedOn"})
    normalized = RecordData.model_validate(legacy)
    assert not normalized.feedbackReceived and normalized.feedbackReceivedOn is None
    assert query_records([{"entityId": uuid4().hex, "data": legacy}], {}, "feedback-pending")["total"] == 1
    old_date = closed.stageDates["closed"]
    received = RecordData.model_validate({
        **closed.model_dump(), "feedbackReceived": True, "feedbackReceivedOn": old_date
    })
    assert feedback_change(received, FeedbackChange(received=True)).feedbackReceivedOn == old_date
    assert feedback_change(received, FeedbackChange(received=False)).feedbackReceivedOn is None
    invalid_cases = [
        {**closed.model_dump(), "feedbackReceived": True},
        {**closed.model_dump(), "feedbackReceivedOn": today()},
        {**closed.model_dump(), "feedbackReceived": True, "feedbackReceivedOn": old_date - timedelta(days=1)},
        {**closed.model_dump(), "feedbackReceived": True, "feedbackReceivedOn": today() + timedelta(days=1)},
        {**record_at(4).model_dump(), "feedbackReceived": True, "feedbackReceivedOn": today()},
    ]
    for data in invalid_cases:
        with pytest.raises(ValidationError, match="Feedback|feedback"):
            RecordData.model_validate(data)
    with pytest.raises(ValidationError, match="Feedback date"):
        stage_change(received, StageChange(stage="closed", occurredOn=today(), reason="Correct date"))


def test_feedback_cannot_bypass_action_and_is_retained_on_discard(client):
    row = new_sale(client)
    for key, value in (("feedbackReceived", True), ("feedbackReceivedOn", today().isoformat())):
        rejected, _ = write(
            client, "record", {"accountId": row["data"]["accountId"], key: value},
            f"/api/records/{row['entityId']}", row, method="PATCH",
        )
        assert rejected.status_code == 422
    for stage in STAGES[1:5]:
        row = advance(client, row, stage)
    for suffix in ("payment", "feedback"):
        response, _ = write(
            client, "record", {"received": True}, f"/api/records/{row['entityId']}/{suffix}", row
        )
        assert response.status_code == 200, response.text
        row = response.json()["revision"]
    response, _ = write(
        client, "record", {"reason": "Archived duplicate"}, f"/api/records/{row['entityId']}/discard", row,
    )
    assert response.status_code == 200, response.text
    discarded = response.json()["revision"]
    assert discarded["data"]["feedbackReceived"] is True
    assert discarded["data"]["feedbackReceivedOn"] == row["data"]["feedbackReceivedOn"]
    assert client.get("/api/records?view=feedback-pending").json()["total"] == 0


@pytest.mark.parametrize("stage_index", range(7))
@pytest.mark.parametrize("workflow_version", [1, 2])
def test_legacy_workflow_and_financial_history_remain_readable(tmp_path, stage_index, workflow_version):
    old_stages = [
        "new_lead",
        "quotation_pending",
        "quotation_sent",
        "order_received",
        "dispatched",
        "delivered",
        "closed",
    ]
    if workflow_version == 2:
        old_stages[1:3] = ["quotation_sent", "follow_up"]
    data = record_at(0).model_dump(mode="json")
    for key in ("discarded", "discardNote", "discardedOn", "feedbackReceived", "feedbackReceivedOn"):
        data.pop(key)
    if workflow_version == 1:
        data.pop("workflowVersion")
    else:
        data["workflowVersion"] = 2
    data["stageDates"] = {
        s: (today() - timedelta(days=10 - i)).isoformat() for i, s in enumerate(old_stages[: stage_index + 1])
    }
    data["quotationReference"] = "OLD-Q-001" if "quotation_sent" in data["stageDates"] else ""
    if stage_index >= 3:
        data.update(
            orderAmountPaise=100000,
            amountReceivedPaise=50000,
            paymentReceivedOn=data["stageDates"]["order_received"],
        )
    if stage_index == 6:
        data.update(amountReceivedPaise=100000, paymentReceived=True)
    original = revision("record", uuid4().hex, data)
    backend = LocalBackend(tmp_path)
    backend.create(original)
    repo = Repository(backend)
    repo.sync(force=True)
    current = repo.current(original["entityId"])
    normalized = RecordData.model_validate(current["data"])
    assert normalized.workflowVersion == workflow_version
    assert "quotation_pending" not in normalized.stageDates
    assert "quotation_sent" not in normalized.stageDates
    expected_stage = {"quotation_pending": "new_lead", "quotation_sent": "follow_up"}.get(
        old_stages[stage_index], old_stages[stage_index]
    )
    assert record_stage(normalized.model_dump()) == record_stage(current["data"]) == expected_stage
    followup_on = data["stageDates"].get("follow_up", data["stageDates"].get("quotation_sent"))
    assert normalized.model_dump(mode="json")["stageDates"].get("follow_up") == followup_on
    assert query_records([current], {}, "followups")["total"] == int(expected_stage == "follow_up")
    assert query_records([current], {}, "leads")["total"] == int(expected_stage == "new_lead")
    if stage_index < 3:
        target = STAGES[STAGES.index(expected_stage) + 1]
        changed = stage_change(
            normalized,
            StageChange(stage=target, quotationReference="Q-NEW" if target == "follow_up" else None),
        )
    else:
        changed = payment_change(normalized, PaymentChange(received=True, note="Receipt confirmed"))
        assert changed.orderAmountPaise == 100000
        assert changed.amountReceivedPaise == data["amountReceivedPaise"]
        if stage_index == 5:
            assert "closed" in changed.stageDates
        assert changed.model_dump(mode="json")["stageDates"]["follow_up"] == followup_on
    updated = revision("record", original["entityId"], changed.model_dump(mode="json"), [original["id"]])
    repo.commit(updated)
    recovered = Repository(backend)
    recovered.sync(force=True)
    assert recovered.current(original["entityId"])["id"] == updated["id"]
    assert recovered.history(original["entityId"])[1]["id"] == original["id"]
    assert next(r for r in backend.revisions(original["entityId"]) if r["id"] == original["id"]) == original
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_receipt_date_validation_and_note_only_updates():
    ordered = record_at(2)
    with pytest.raises(ValidationError):
        PaymentChange(amountReceivedPaise=100)
    with pytest.raises(ValidationError):
        StageChange(stage="order_received", orderAmountPaise=100)
    with pytest.raises(Problem, match="Confirm"):
        payment_change(record_at(), PaymentChange(received=True))
    assert payment_change(record_at(), PaymentChange(note="Reference only")).paymentNote == "Reference only"
    noted = payment_change(ordered, PaymentChange(note="Bank reference"))
    assert payment_change(noted, PaymentChange(received=True)).paymentNote == "Bank reference"
    delivered = record_at(4, True)
    assert payment_change(delivered, PaymentChange(note="Verified")).stageDates == delivered.stageDates
    with pytest.raises(Problem, match="confirm receipt"):
        payment_change(ordered, PaymentChange())
    with pytest.raises(Problem, match="receipt date"):
        payment_change(ordered, PaymentChange(note="Note", occurredOn=today()))
    for invalid_date in (today() + timedelta(days=1), ordered.stageDates["new_lead"]):
        with pytest.raises(ValidationError, match="Payment date"):
            payment_change(ordered, PaymentChange(received=True, occurredOn=invalid_date))
    paid = payment_change(ordered, PaymentChange(received=True, occurredOn=today() - timedelta(days=2)))
    note_edit = payment_change(paid, PaymentChange(note="Receipt verified"))
    assert note_edit.paymentReceivedOn == paid.paymentReceivedOn
    assert note_edit.paymentReceived
    dated = payment_change(paid, PaymentChange(received=True, occurredOn=today() - timedelta(days=1)))
    assert dated.paymentReceivedOn == today() - timedelta(days=1)
    with pytest.raises(Problem, match="Correct the payment"):
        stage_change(paid, StageChange(stage="follow_up", reason="Wrong stage"))
    corrected_order = stage_change(
        paid,
        StageChange(
            stage="order_received", occurredOn=ordered.stageDates["order_received"], reason="Date checked"
        ),
    )
    assert corrected_order.paymentReceivedOn == paid.paymentReceivedOn
    assert corrected_order.paymentReceived
    with pytest.raises(ValidationError, match="received date"):
        RecordData.model_validate({**paid.model_dump(), "paymentReceivedOn": None})


@pytest.mark.parametrize("reference", [None, "", "  ", "Q-555"])
def test_followup_transition_with_optional_quotation_number(client, reference):
    row = new_sale(client)
    route = f"/api/records/{row['entityId']}/stage"
    for body in (
        {"stage": "quotation_pending"},
        {"stage": "quotation_sent"},
        {"stage": "order_received"},
    ):
        result, _ = write(client, "record", body, route, row)
        assert result.status_code == 422
    row = advance(
        client, row, "follow_up", **({"quotationReference": reference} if reference is not None else {})
    )
    assert row["data"]["quotationReference"] == (reference or "").strip()
    assert set(row["data"]["stageDates"]) == {"new_lead", "follow_up"}
    assert client.get("/api/records?view=followups").json()["total"] == 1
    assert client.get("/api/records?view=leads").json()["total"] == 0
    assert row["data"]["nextFollowUpOn"] is None
    wrong_stage, _ = write(
        client, "record", {"stage": "order_received", "quotationReference": "Q-WRONG"}, route, row
    )
    assert wrong_stage.status_code == 422
    row = advance(client, row, "order_received")
    assert row["data"]["orderAmountPaise"] is None
    assert client.get("/api/records?view=followups").json()["total"] == 0
    changed, _ = write(
        client,
        "record",
        {"accountId": row["data"]["accountId"], "quotationReference": ""},
        f"/api/records/{row['entityId']}",
        row,
        method="PATCH",
    )
    assert changed.status_code == 200
    assert changed.json()["revision"]["data"]["quotationReference"] == ""


def test_legacy_quote_maps_to_followup_across_reads_edits_and_history(client):
    row = new_sale(client)
    quoted = revision(
        "record",
        row["entityId"],
        {
            **row["data"],
            "workflowVersion": 2,
            "quotationReference": "OLD-QUOTE",
            "stageDates": {**row["data"]["stageDates"], "quotation_sent": today().isoformat()},
        },
        [row["id"]],
    )
    client.app.state.repo.commit(quoted)
    assert client.get("/api/records?view=followups").json()["items"][0]["id"] == quoted["id"]
    assert client.get("/api/records?view=leads").json()["total"] == 0
    counts = client.get("/api/dashboard").json()["counts"]
    assert counts["followups"] == 1 and counts["leads"] == 0
    assert client.get("/api/records?stage=follow_up").json()["total"] == 1
    edited, _ = write(
        client,
        "record",
        {"accountId": row["data"]["accountId"], "quotationReference": "", "notes": "Details updated"},
        f"/api/records/{row['entityId']}",
        quoted,
        method="PATCH",
    )
    assert edited.status_code == 200, edited.text
    row = edited.json()["revision"]
    assert row["data"]["quotationReference"] == ""
    assert row["data"]["stageDates"] == {"new_lead": today().isoformat(), "follow_up": today().isoformat()}
    row = advance(client, row, "order_received")
    row = advance(client, row, "new_lead", reason="Wrong enquiry")
    assert record_stage(row["data"]) == "new_lead"
    assert set(row["data"]["stageDates"]) == {"new_lead"}
    assert client.get("/api/records?view=followups").json()["total"] == 0
    assert client.get("/api/records?view=leads").json()["total"] == 1
    history = client.get(f"/api/entities/record/{row['entityId']}/history").json()["items"]
    assert next(r for r in history if r["id"] == quoted["id"]) == quoted


def test_current_workflow_requires_followup_but_not_quotation():
    lead = record_at()
    for dates in (
        {"new_lead": today(), "quotation_sent": today()},
        {"new_lead": today(), "order_received": today()},
    ):
        with pytest.raises(ValidationError):
            RecordData.model_validate({**lead.model_dump(), "stageDates": dates})
    lead.quotationReference = "Existing reference"
    followed = stage_change(lead, StageChange(stage="follow_up"))
    assert followed.quotationReference == "Existing reference"
    legacy_gap = RecordData.model_validate(
        {
            **lead.model_dump(),
            "workflowVersion": 1,
            "stageDates": {"new_lead": today(), "order_received": today()},
        }
    )
    assert (
        record_stage(stage_change(legacy_gap, StageChange(stage="dispatched")).model_dump()) == "dispatched"
    )


def test_stage_tabs_and_dashboard_use_only_current_status(client):
    account, _ = write(client, "account", {"name": "Stage view examples"})
    account_id = account.json()["revision"]["entityId"]
    rows = []
    for stage_index, paid in [(i, i == 5) for i in range(6)] + [(2, True)]:
        data = record_at(stage_index, paid).model_dump(mode="json")
        data["accountId"] = account_id
        row = revision("record", uuid4().hex, data)
        client.app.state.repo.commit(row)
        rows.append(row)
    counts = client.get("/api/dashboard").json()["counts"]
    expected = {
        "all": list(range(7)),
        "leads": [0],
        "followups": [1],
        "orders": [2, 6],
        "dispatched": [3],
        "delivered": [4],
        "closed": [5],
        "pending": [2, 3, 4],
        "paid": [5, 6],
        "feedback-pending": [5],
        "discarded": [],
    }
    assert set(counts) == set(expected)
    for view, indexes in expected.items():
        response = client.get("/api/records", params={"view": view})
        assert response.status_code == 200, response.text
        result = response.json()
        assert {row["entityId"] for row in result["items"]} == {rows[i]["entityId"] for i in indexes}
        assert result["total"] == counts[view] == len(indexes)
    for removed_view in ("quotes", "quoted", "delivery"):
        assert client.get("/api/records", params={"view": removed_view}).status_code == 422


def test_idempotent_retry_and_stale_write(client):
    row = new_sale(client)
    ticket = prepare(client, "record", row["entityId"])
    response, body = write(
        client,
        "record",
        {"stage": "follow_up", "quotationReference": "Q-RETRY"},
        f"/api/records/{row['entityId']}/stage",
        row,
        ticket=ticket,
    )
    assert response.status_code == 200
    retry = client.post(f"/api/records/{row['entityId']}/stage", json=body, headers=HEADERS)
    assert retry.status_code == 200
    assert retry.json()["revision"]["id"] == response.json()["revision"]["id"]
    body["data"]["quotationReference"] = "Q-DIFFERENT"
    mismatch = client.post(f"/api/records/{row['entityId']}/stage", json=body, headers=HEADERS)
    assert mismatch.status_code == 409
    stale, _ = write(
        client,
        "record",
        {"stage": "follow_up", "quotationReference": "Q-STALE"},
        f"/api/records/{row['entityId']}/stage",
        row,
    )
    assert stale.status_code == 409


def test_all_entity_edits_reuse_files_and_history_remains_paginated(client):
    row = new_sale(client)
    account_id = row["data"]["accountId"]
    account = client.get(f"/api/entities/account/{account_id}").json()["revision"]
    response, _ = write(client, "contact", {"accountId": account_id, "name": "Synthetic buyer"})
    contact = response.json()["revision"]
    storage = client.app.state.repo.backend
    original_files = {path.name for path in storage.directory.glob("*.json")}
    old_request = None
    revisions = [row]
    for index in range(5):
        response, _ = write(
            client, "account", {"name": f"Synthetic company {index}"},
            f"/api/accounts/{account_id}", account, method="PATCH",
        )
        assert response.status_code == 200
        account = response.json()["revision"]
        response, _ = write(
            client, "contact", {"accountId": account_id, "name": f"Synthetic buyer {index}"},
            f"/api/contacts/{contact['entityId']}", contact, method="PATCH",
        )
        assert response.status_code == 200
        contact = response.json()["revision"]
        response, body = write(
            client, "record", {"productNotes": f"Synthetic requirement {index}"},
            f"/api/records/{row['entityId']}/product-notes", row, method="PATCH",
        )
        assert response.status_code == 200
        if old_request is None:
            old_request = body
        row = response.json()["revision"]
        revisions.append(row)
    assert {path.name for path in storage.directory.glob("*.json")} == original_files
    assert len(original_files) == 3
    history = client.get(
        f"/api/entities/record/{row['entityId']}/history", params={"offset": 1, "limit": 2}
    ).json()
    assert history["total"] == 6
    assert history["items"] == list(reversed(revisions))[1:3]
    retry = client.patch(
        f"/api/records/{row['entityId']}/product-notes", json=old_request, headers=HEADERS
    )
    assert retry.status_code == 200 and retry.json()["revision"] == revisions[1]
    assert storage.get(row["entityId"])["current"] == row
    assert len(storage.revisions(row["entityId"])) == 6


def test_reusing_an_operation_id_for_another_entity_is_rejected(client):
    response, used = write(client, "account", {"name": "Synthetic first company"})
    assert response.status_code == 200
    ticket = prepare(client, "account")
    ticket["writeId"] = used["writeId"]
    storage = client.app.state.repo.backend
    before = {path.name for path in storage.directory.glob("*.json")}
    rejected, _ = write(client, "account", {"name": "Synthetic second company"}, ticket=ticket)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "write_id_reused"
    assert {path.name for path in storage.directory.glob("*.json")} == before


def test_product_note_edit_changes_only_product_notes_and_preserves_retries(client):
    original = new_sale(client)
    contact, _ = write(client, "contact", {"accountId": original["data"]["accountId"], "name": "Buyer"})
    original = revision(
        "record",
        original["entityId"],
        {
            **original["data"],
            "contactId": contact.json()["revision"]["entityId"],
            "notes": "Keep general notes",
            "quotationReference": "Q-KEEP",
            "paymentNote": "Keep payment details",
            "nextFollowUpOn": today().isoformat(),
        },
        [original["id"]],
    )
    client.app.state.repo.commit(original)
    route = f"/api/records/{original['entityId']}/product-notes"
    result, body = write(
        client, "record", {"productNotes": "Updated rollers and belts"}, route, original, method="PATCH"
    )
    assert result.status_code == 200, result.text
    updated = result.json()["revision"]
    assert updated["data"] == {**original["data"], "productNotes": "Updated rollers and belts"}
    assert updated["action"] == "edit_product_notes"
    retry = client.patch(route, json=body, headers=HEADERS)
    assert retry.status_code == 200 and retry.json()["revision"]["id"] == updated["id"]
    stale, _ = write(client, "record", {"productNotes": "Stale edit"}, route, original, method="PATCH")
    assert stale.status_code == 409
    for invalid in ({"productNotes": "Changed", "notes": "Do not overwrite"}, {"productNotes": "x" * 10001}):
        rejected, _ = write(client, "record", invalid, route, updated, method="PATCH")
        assert rejected.status_code == 422
    cleared, _ = write(client, "record", {"productNotes": ""}, route, updated, method="PATCH")
    assert cleared.status_code == 200
    assert cleared.json()["revision"]["data"] == {**original["data"], "productNotes": ""}
    history = client.get(f"/api/entities/record/{updated['entityId']}/history").json()["items"]
    assert history[2] == original


def test_discard_requires_a_remark_and_cannot_be_set_by_normal_edit(client):
    row = new_sale(client)
    route = f"/api/records/{row['entityId']}/discard"
    for invalid in ({}, {"reason": ""}, {"reason": " \n "}, {"reason": "x" * 10001}):
        result, _ = write(client, "record", invalid, route, row)
        assert result.status_code == 422
    for fields in ({"discarded": True}, {"discardNote": "Bypassed"}, {"discardedOn": today().isoformat()}):
        result, _ = write(
            client,
            "record",
            {"accountId": row["data"]["accountId"], **fields},
            f"/api/records/{row['entityId']}",
            row,
            method="PATCH",
        )
        assert result.status_code == 422
    assert client.get("/api/records?view=discarded").json()["total"] == 0
    assert client.get(f"/api/entities/record/{row['entityId']}/history").json()["total"] == 1


@pytest.mark.parametrize(("stage_index", "paid"), [(i, i == 5) for i in range(6)] + [(2, True)])
def test_discard_at_any_stage_keeps_history_and_exits_queues(client, stage_index, paid):
    row = new_sale(client)
    for stage in STAGES[1 : stage_index + 1]:
        if stage == "closed":
            result, _ = write(
                client, "record", {"received": True}, f"/api/records/{row['entityId']}/payment", row
            )
            assert result.status_code == 200, result.text
            row = result.json()["revision"]
        else:
            row = advance(client, row, stage)
    if paid and not row["data"]["paymentReceived"]:
        result, _ = write(
            client, "record", {"received": True}, f"/api/records/{row['entityId']}/payment", row
        )
        assert result.status_code == 200, result.text
        row = result.json()["revision"]
    before = row
    route = f"/api/records/{row['entityId']}"
    result, body = write(
        client, "record", {"reason": "  Customer no longer needs these parts  "}, route + "/discard", row
    )
    assert result.status_code == 200, result.text
    row = result.json()["revision"]
    assert row["data"] == {
        **before["data"],
        "discarded": True,
        "discardNote": "Customer no longer needs these parts",
        "discardedOn": today().isoformat(),
    }
    assert row["reason"] == row["data"]["discardNote"]
    retry = client.post(route + "/discard", json=body, headers=HEADERS)
    assert retry.status_code == 200 and retry.json()["revision"]["id"] == row["id"]
    counts = client.get("/api/dashboard").json()["counts"]
    for view in VIEWS:
        expected = int(view in ("all", "discarded"))
        assert client.get("/api/records", params={"view": view}).json()["total"] == counts[view] == expected
    mutations = [
        ("", {"accountId": row["data"]["accountId"]}, "PATCH"),
        ("/product-notes", {"productNotes": "After discard"}, "PATCH"),
        ("/stage", {"stage": "new_lead", "reason": "Correction"}, "POST"),
        ("/payment", {"note": "After discard"}, "POST"),
        ("/feedback", {"received": True}, "POST"),
        ("/followups", {"note": "After discard"}, "POST"),
        ("/discard", {"reason": "Again"}, "POST"),
    ]
    for suffix, data, method in mutations:
        rejected, _ = write(client, "record", data, route + suffix, row, method=method)
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "discarded_record"
    stale, _ = write(
        client, "record", {"productNotes": "Outdated tab"}, route + "/product-notes", before, method="PATCH"
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_edit"
    history = client.get(f"/api/entities/record/{row['entityId']}/history").json()["items"]
    assert history[0] == row and history[1] == before
    recovered = Repository(client.app.state.repo.backend)
    recovered.sync(force=True)
    assert recovered.current(row["entityId"]) == row


def test_discard_metadata_validation():
    data = record_at(2).model_dump()
    for fields in (
        {"discarded": True},
        {"discardNote": "Remark without discard"},
        {"discardedOn": today()},
        {"discarded": True, "discardNote": "Remark", "discardedOn": today() + timedelta(days=1)},
        {"discarded": True, "discardNote": "Remark", "discardedOn": data["stageDates"]["new_lead"]},
    ):
        with pytest.raises(ValidationError):
            RecordData.model_validate({**data, **fields})


def test_account_contacts_references_and_normal_edit(client):
    row = new_sale(client)
    other, _ = write(client, "account", {"name": "Other account"})
    contact, _ = write(
        client, "contact", {"name": "Wrong contact", "accountId": other.json()["revision"]["entityId"]}
    )
    contact_id = contact.json()["revision"]["entityId"]
    result, _ = write(client, "record", {"accountId": row["data"]["accountId"], "contactId": contact_id})
    assert result.status_code == 422
    data = {
        key: row["data"][key]
        for key in ("accountId", "contactId", "notes", "productNotes", "quotationReference")
    }
    data["notes"] = "A note-only change"
    result, _ = write(client, "record", data, f"/api/records/{row['entityId']}", row, method="PATCH")
    assert result.status_code == 200
    assert result.json()["revision"]["data"]["stageDates"] == row["data"]["stageDates"]
    data["paymentReceived"] = True
    rejected, _ = write(client, "record", data, f"/api/records/{row['entityId']}", row, method="PATCH")
    assert rejected.status_code == 422
    data.pop("paymentReceived")
    data["amountReceivedPaise"] = 100
    rejected, _ = write(client, "record", data, f"/api/records/{row['entityId']}", row, method="PATCH")
    assert rejected.status_code == 422


def test_company_headings_and_location_follow_account_changes(client):
    first = new_sale(client)
    account_id = first["data"]["accountId"]
    second, _ = write(client, "record", {"accountId": account_id, "productNotes": "Cooling pump"})
    assert second.status_code == 200, second.text
    assert not first["data"].get("title")
    assert not second.json()["revision"]["data"].get("title")
    account = client.get(f"/api/entities/account/{account_id}").json()["revision"]
    updated, _ = write(
        client,
        "account",
        {"name": "Renamed company", "address": "Pune, Maharashtra"},
        f"/api/accounts/{account_id}",
        account,
        method="PATCH",
    )
    assert updated.status_code == 200, updated.text
    for search in ("renamed", "maharashtra"):
        results = client.get("/api/records", params={"search": search}).json()
        assert results["total"] == 2
        assert len({r["entityId"] for r in results["items"]}) == 2
        assert all(r["accountName"] == "Renamed company" for r in results["items"])
        assert all(r["accountLocation"] == "Pune, Maharashtra" for r in results["items"])
    result = client.get("/api/records", params={"search": "Cooling pump"}).json()
    assert result["total"] == 1
    assert result["items"][0]["entityId"] == second.json()["revision"]["entityId"]


def test_legacy_title_survives_reload_and_title_free_edit(client):
    row = new_sale(client)
    legacy = revision(
        "record", row["entityId"], {**row["data"], "title": "Legacy enquiry title"}, [row["id"]]
    )
    repo = client.app.state.repo
    repo.commit(legacy)
    recovered = Repository(repo.backend)
    recovered.sync(force=True)
    assert recovered.current(row["entityId"])["data"]["title"] == "Legacy enquiry title"
    result, _ = write(
        client,
        "record",
        {"accountId": row["data"]["accountId"], "productNotes": "Updated requirement"},
        f"/api/records/{row['entityId']}",
        legacy,
        method="PATCH",
    )
    assert result.status_code == 200, result.text
    assert result.json()["revision"]["data"]["title"] == "Legacy enquiry title"
    assert result.json()["revision"]["data"]["stageDates"] == row["data"]["stageDates"]
    history = client.get(f"/api/entities/record/{row['entityId']}/history").json()["items"]
    assert history[1]["data"]["title"] == "Legacy enquiry title"
    assert client.get("/api/records?search=Legacy").json()["total"] == 0
    assert client.get("/api/records?search=Updated").json()["total"] == 1


def test_correct_stage_preserves_history(client):
    row = new_sale(client)
    row = advance(client, row, "follow_up")
    row = advance(client, row, "order_received")
    row = advance(client, row, "new_lead", reason="Follow-up was not actually started")
    assert "follow_up" not in row["data"]["stageDates"]
    history = client.get(f"/api/entities/record/{row['entityId']}/history").json()
    assert history["total"] == 4
    assert "follow_up" in history["items"][1]["data"]["stageDates"]


def test_guards_and_unknown_routes(client):
    assert client.post("/api/accounts", json={}).status_code == 403
    assert (
        client.post(
            "/api/accounts", json={}, headers={**HEADERS, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.get("/api/not-a-route").status_code == 404
    assert client.get("/api/me").json()["mode"] == "demo"
    assert client.get("/healthz").json() == {"status": "ok"}


def record_at(stage_index=0, paid=False):
    acquired = today() - timedelta(days=60)
    return RecordData(
        accountId=uuid4().hex,
        workflowVersion=3,
        quotationReference="Q-TEST" if stage_index >= 1 else "",
        stageDates={s: acquired + timedelta(days=i) for i, s in enumerate(STAGES[: stage_index + 1])},
        paymentReceived=paid,
        paymentReceivedOn=acquired + timedelta(days=2) if paid else None,
    )


@pytest.mark.parametrize("stage_index", range(5))
def test_next_stage(stage_index):
    record = record_at(stage_index, paid=stage_index >= 2)
    next_stage = STAGES[stage_index + 1]
    result = stage_change(
        record,
        StageChange(stage=next_stage, quotationReference="Q-NEXT" if next_stage == "follow_up" else None),
    )
    assert result.stageDates[next_stage] == today()


def test_dates_and_receipt_invariants():
    record = record_at()
    with pytest.raises(Problem):
        stage_change(record, StageChange(stage="order_received"))
    with pytest.raises(ValidationError):
        stage_change(
            record,
            StageChange(stage="follow_up", quotationReference="Q", occurredOn=today() + timedelta(days=1)),
        )
    with pytest.raises(ValidationError):
        stage_change(
            record,
            StageChange(stage="follow_up", quotationReference="Q", occurredOn=today() - timedelta(days=61)),
        )
    with pytest.raises(ValidationError):
        stage_change(record_at(4), StageChange(stage="closed"))
    with pytest.raises(Problem):
        payment_change(record, PaymentChange(received=True))
    with pytest.raises(Problem):
        payment_change(record_at(5, True), PaymentChange(received=False, reason="Correction"))
    corrected = payment_change(record_at(2, True), PaymentChange(received=False))
    assert not corrected.paymentReceived and corrected.paymentReceivedOn is None
    paid = payment_change(record_at(2), PaymentChange(received=True))
    assert paid.paymentReceivedOn == today()
    followed = add_followup(record, FollowUp(note="Call customer", nextFollowUpOn=today()))
    assert followed.stageDates == record.stageDates
    assert followed.followUps[0].occurredOn == today()


def test_date_views_do_not_hide_older_unpaid_orders():
    data = record_at(4).model_dump(mode="json")
    data["stageDates"]["dispatched"] = today().isoformat()
    data["stageDates"]["delivered"] = today().isoformat()
    revision = {"entityId": uuid4().hex, "data": data}
    pending = query_records([revision], {}, "pending")
    assert pending["total"] == 1 and "pendingPaise" not in pending
    assert query_records([revision], {}, "orders")["total"] == 0
    assert (
        query_records([revision], {}, "delivered", start=today(), end=today() + timedelta(days=1))["total"]
        == 0
    )
    assert query_records([revision], {}, "delivered", end=today())["total"] == 1
    assert query_records([revision], {}, "dispatched")["total"] == 0
    assert query_records([revision], {}, "leads", start=today())["total"] == 0


@pytest.mark.parametrize(
    ("view", "stage_index", "paid"),
    [
        ("all", 5, True),
        ("leads", 0, False),
        ("followups", 1, False),
        ("orders", 2, False),
        ("dispatched", 3, False),
        ("delivered", 4, False),
        ("closed", 5, True),
        ("pending", 4, False),
        ("paid", 3, True),
        ("feedback-pending", 5, True),
        ("discarded", 1, False),
    ],
)
def test_every_view_filters_acquisition_date_and_shows_current_progress(view, stage_index, paid):
    acquired = today() - timedelta(days=60)
    rows = []
    for index, offset in enumerate((-1, 0, 1, 1)):
        data = record_at(stage_index, paid).model_dump(mode="json")
        data["stageDates"] = {
            s: (acquired + timedelta(days=offset) if s == "new_lead" else today()).isoformat()
            for s in STAGES[: stage_index + 1]
        }
        if paid:
            data["paymentReceivedOn"] = today().isoformat()
        if stage_index >= 2:
            data["paymentDueOn"] = today().isoformat()
        data["nextFollowUpOn"] = today().isoformat()
        if view == "discarded":
            data.update(discarded=True, discardNote="No longer needed", discardedOn=today().isoformat())
        rows.append({"entityId": f"{index:032d}", "data": data})
    results = query_records(rows, {}, view, start=acquired, end=acquired + timedelta(days=1))
    assert results["total"] == 1
    assert results["items"][0]["entityId"] == rows[1]["entityId"]
    assert results["items"][0]["data"] == rows[1]["data"]
    assert results["dateBasis"] == "new_lead"
    assert query_records(rows, {}, view, start=today())["total"] == 0
    all_results = query_records(rows, {}, view)
    assert all_results["total"] == 4
    assert [r["entityId"] for r in all_results["items"]] == [rows[i]["entityId"] for i in (3, 2, 1, 0)]
    page = query_records(rows, {}, view, offset=1, limit=2)
    assert [r["entityId"] for r in page["items"]] == [rows[i]["entityId"] for i in (2, 1)]
    assert page["total"] == 4


def test_acquired_month_boundaries_and_current_stage_filter():
    end = today().replace(day=1)
    start = (end - timedelta(days=1)).replace(day=1)
    rows = []
    for acquired, stage_index in (
        (start - timedelta(days=1), 2),
        (start, 1),
        (end - timedelta(days=1), 2),
        (end, 2),
    ):
        data = record_at(stage_index).model_dump(mode="json")
        data["stageDates"] = {
            s: (acquired if s == "new_lead" else today()).isoformat() for s in STAGES[: stage_index + 1]
        }
        rows.append({"entityId": uuid4().hex, "data": data})
    result = query_records(rows, {}, start=start, end=end)
    assert {r["entityId"] for r in result["items"]} == {rows[1]["entityId"], rows[2]["entityId"]}
    result = query_records(rows, {}, start=start, end=end, stage="order_received")
    assert [r["entityId"] for r in result["items"]] == [rows[2]["entityId"]]
    assert query_records(rows, {}, "followups", start=start, end=end)["total"] == 1
    assert query_records(rows, {}, "orders", start=start, end=end)["total"] == 1


def test_api_rejects_alternate_date_basis(client):
    response = client.get("/api/records?dateBasis=quotation_sent")
    assert response.status_code == 422
    assert "lead acquired date" in response.json()["error"]["message"]


def revision(kind, entity_id, data, parents=None):
    return Revision(
        id=uuid4().hex,
        entityId=entity_id,
        entityType=kind,
        parents=parents or [],
        createdAt=now(),
        actor="test",
        action="test",
        fingerprint=digest(data),
        data=data,
    ).model_dump(mode="json")


def test_two_instance_stale_edit_and_restart_recovery(tmp_path):
    backend = LocalBackend(tmp_path)
    root = revision("account", uuid4().hex, AccountData(name="Initial").model_dump())
    backend.create(root)
    left, right = Repository(backend), Repository(backend)
    left.sync()
    right.sync()
    a = revision(
        "account",
        root["entityId"],
        {"name": "Left", "address": "", "taxReference": "", "notes": ""},
        [root["id"]],
    )
    b = revision(
        "account",
        root["entityId"],
        {"name": "Right", "address": "", "taxReference": "", "notes": ""},
        [root["id"]],
    )
    backend.create(a)
    with pytest.raises(Problem, match="changed") as error:
        backend.create(b)
    assert error.value.code == "stale_edit"
    left.sync(force=True)
    right.sync(force=True)
    assert not left.conflicts() and not right.conflicts()
    assert left.current(root["entityId"]) == right.current(root["entityId"]) == a
    recovered = Repository(backend)
    recovered.sync(force=True)
    assert not recovered.conflicts()
    assert recovered.current(root["entityId"])["id"] == a["id"]
    assert len(recovered.history(root["entityId"])) == 2
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_external_changes_are_not_silently_accepted(tmp_path):
    backend = LocalBackend(tmp_path)
    root = revision("account", uuid4().hex, AccountData(name="Original").model_dump())
    backend.create(root)
    repo = Repository(backend)
    repo.sync()
    (tmp_path / f"{root['entityId']}.json").write_text('{"bad":true}', encoding="utf-8")
    with pytest.raises(Problem):
        repo.sync(force=True)
    assert repo.error


def test_production_forbids_demo(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "salestrackr")
    monkeypatch.setenv("APP_MODE", "demo")
    with pytest.raises(RuntimeError, match="forbidden"):
        Settings.load()


def test_production_requires_verified_identity(tmp_path):
    settings = Settings(
        "production", tmp_path, "root", "unused", "audience", "https://app.example.com", False
    )
    with TestClient(create_app(settings, Repository(LocalBackend(tmp_path)))) as client:
        assert client.get("/api/me").status_code == 401
        assert (
            client.get(
                "/api/me", headers={"X-Goog-Authenticated-User-Email": "spoof@example.com"}
            ).status_code
            == 401
        )
        assert client.get("/api/me", headers={"X-Goog-IAP-JWT-Assertion": "invalid"}).status_code == 401
        assert client.get("/healthz").status_code == 200
