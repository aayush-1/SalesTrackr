from datetime import date
from difflib import SequenceMatcher
from unicodedata import normalize

from .domain import STAGES, Problem, record_stage, today

VIEWS = {
    "all": "All records",
    "leads": "New leads",
    "followups": "Follow-ups",
    "orders": "Order in hand",
    "dispatched": "Dispatched",
    "delivered": "Delivered",
    "closed": "Closed",
    "pending": "Payment pending",
    "paid": "Payment received",
    "feedback-pending": "Feedback pending",
    "stock-pending": "Stock update pending",
    "discarded": "Discarded",
}


def account_name_key(name):
    folded = normalize("NFKC", name).casefold()
    return "".join(character for character in folded if character.isalnum()) or folded.strip()


def account_matches(revisions, name, exclude=None, limit=5):
    key = account_name_key(name)
    if not key:
        return {"items": [], "total": 0, "exactCount": 0}
    accounts = {}
    for revision in revisions:
        if revision["entityType"] == "account" and revision["entityId"] != exclude:
            accounts.setdefault(revision["entityId"], []).append(revision)
    found = []
    for versions in accounts.values():
        best = None
        for revision in versions:
            other = account_name_key(revision["data"]["name"])
            exact = key == other
            shorter, longer = sorted((key, other), key=len)
            prefix = len(shorter) >= 6 and longer.startswith(shorter) and len(shorter) / len(longer) >= 0.5
            score = 1.0 if exact else 0.0
            if not exact and min(len(key), len(other)) >= 4:
                if 2 * len(shorter) / (len(key) + len(other)) >= 0.84:
                    score = SequenceMatcher(None, key, other).ratio()
            if not exact and score < 0.84 and not prefix:
                continue
            candidate = {
                "revision": revision, "match": "exact" if exact else "similar",
                "conflicted": len(versions) > 1, "score": max(score, 0.84 if prefix else 0),
            }
            if best is None or candidate["score"] > best["score"]:
                best = candidate
        if best is not None:
            found.append(best)
    found.sort(key=lambda match: (
        -match["score"], match["revision"]["data"]["name"].casefold(), match["revision"]["entityId"],
    ))
    return {
        "items": [{key: value for key, value in match.items() if key != "score"} for match in found[:limit]],
        "total": len(found), "exactCount": sum(match["match"] == "exact" for match in found),
    }


def matches(data, view):
    if view == "all":
        return True
    if view == "discarded":
        return data.get("discarded", False)
    if data.get("discarded", False):
        return False
    stage = record_stage(data)
    return {
        "leads": stage == "new_lead",
        "orders": stage == "order_received",
        "dispatched": stage == "dispatched",
        "delivered": stage == "delivered",
        "closed": stage == "closed",
        "pending": "order_received" in data["stageDates"] and not data["paymentReceived"],
        "paid": data["paymentReceived"],
        "feedback-pending": stage == "closed" and not data.get("feedbackReceived", False),
        "stock-pending": "order_received" in data["stageDates"] and not data.get("stockUpdated", False),
        "followups": stage == "follow_up",
    }[view]


def query_records(
    revisions,
    accounts,
    view="all",
    search="",
    account_id=None,
    stage=None,
    date_basis=None,
    start: date | None = None,
    end: date | None = None,
    due="all",
    offset=0,
    limit=50,
):
    if view not in VIEWS:
        raise Problem("Unknown record view.")
    if start and end and start > end:
        raise Problem("The start date must not follow the end date.")
    if date_basis not in (None, "new_lead"):
        raise Problem("Date filters always use the lead acquired date.")
    if stage is not None and stage not in STAGES:
        raise Problem("Unknown sales stage.")
    if due not in ("all", "today", "overdue"):
        raise Problem("Unknown due-date filter.")
    if due != "all" and view != "followups":
        raise Problem("Due-date filtering is available in Follow-ups only.")
    items = []
    for revision in revisions:
        data = revision["data"]
        if not matches(data, view) or (account_id and data["accountId"] != account_id):
            continue
        if stage and record_stage(data) != stage:
            continue
        account = accounts.get(data["accountId"])
        name = account["name"] if account else "Account unavailable / conflicting"
        location = account["address"] if account else ""
        if search.casefold() not in f"{name} {location} {data['productNotes']}".casefold():
            continue
        acquired = data["stageDates"]["new_lead"]
        if (start and acquired < start.isoformat()) or (end and acquired >= end.isoformat()):
            continue
        if due != "all":
            due_date = data["nextFollowUpOn"]
            if (
                not due_date
                or (due == "today" and due_date != today().isoformat())
                or (due == "overdue" and due_date >= today().isoformat())
            ):
                continue
        items.append({**revision, "accountName": name, "accountLocation": location})
    items.sort(key=lambda r: (r["data"]["stageDates"]["new_lead"], r["entityId"]), reverse=True)
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "dateBasis": "new_lead",
    }
