from datetime import date, datetime, timezone
from typing import Annotated, Generic, Literal, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

STAGES = [
    "new_lead",
    "follow_up",
    "order_received",
    "dispatched",
    "delivered",
    "closed",
]
Stage = Literal[
    "new_lead",
    "follow_up",
    "order_received",
    "dispatched",
    "delivered",
    "closed",
]
EntityType = Literal["account", "contact", "record"]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10000)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{10,160}$")]


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_stage_dates(record: dict) -> dict:
    dates = record["stageDates"]
    if record.get("workflowVersion", 1) not in (1, 2):
        return dates
    dates = dict(dates)
    if record.get("workflowVersion", 1) == 1:
        dates.pop("quotation_pending", None)
    quoted_on = dates.pop("quotation_sent", None)
    if quoted_on is not None:
        dates.setdefault("follow_up", quoted_on)
    return dates


class Problem(Exception):
    def __init__(self, message: str, status: int = 422, code: str = "invalid_operation"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AccountData(Model):
    name: Name
    address: Text = ""
    taxReference: Annotated[str, StringConstraints(max_length=100)] = ""
    notes: Text = ""


class ContactData(Model):
    accountId: Identifier
    name: Name
    email: Annotated[str, StringConstraints(max_length=254)] = ""
    phone: Annotated[str, StringConstraints(max_length=80)] = ""
    designation: Annotated[str, StringConstraints(max_length=200)] = ""


class RecordFields(Model):
    accountId: Identifier
    contactId: Identifier | None = None
    productNotes: Text = ""
    notes: Text = ""
    quotationReference: Annotated[str, StringConstraints(max_length=100)] = ""


class NewRecord(RecordFields):
    acquiredOn: date | None = None


class ProductNotesChange(Model):
    productNotes: Text


class DiscardChange(Model):
    reason: Text = Field(min_length=1)


class FollowUp(Model):
    note: Name | Text
    occurredOn: date | None = None
    nextFollowUpOn: date | None = None


class FollowUpEntry(Model):
    id: str
    note: str
    occurredOn: date
    recordedAt: str


class RecordData(RecordFields):
    # Retain legacy enquiry titles in saved versions, not in new/edit forms.
    title: Name | None = None
    workflowVersion: Literal[1, 2, 3] = 1
    stageDates: dict[Stage, date]
    discarded: bool = False
    discardNote: Text = ""
    discardedOn: date | None = None
    # Archived financial fields remain readable; the current workflow uses receipt confirmation only.
    orderAmountPaise: int | None = Field(default=None, ge=1, le=10**14)
    amountReceivedPaise: int | None = Field(default=None, ge=0, le=10**14)
    paymentReceived: bool = False
    paymentReceivedOn: date | None = None
    paymentDueOn: date | None = None
    paymentNote: Text = ""
    feedbackReceived: bool = False
    feedbackReceivedOn: date | None = None
    stockUpdated: bool = False
    stockUpdatedOn: date | None = None
    nextFollowUpOn: date | None = None
    followUps: list[FollowUpEntry] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def read_legacy_workflow(cls, data: object):
        if (
            isinstance(data, dict)
            and data.get("workflowVersion", 1) in (1, 2)
            and isinstance(data.get("stageDates"), dict)
        ):
            return {
                **data,
                "stageDates": record_stage_dates(data),
            }
        return data

    @model_validator(mode="after")
    def validate_journey(self):
        reached = [stage for stage in STAGES if stage in self.stageDates]
        expected = STAGES[: STAGES.index(reached[-1]) + 1] if reached else []
        if self.workflowVersion == 1 and "follow_up" not in self.stageDates:
            expected = [s for s in expected if s != "follow_up"]
        if not reached or reached != expected:
            raise ValueError("Stages must form an unbroken journey starting with New lead.")
        dates = [self.stageDates[stage] for stage in reached]
        if dates != sorted(dates) or any(d > today() for d in dates):
            raise ValueError("Reached-stage dates must be chronological and not in the future.")
        if self.discarded:
            if not self.discardNote or self.discardedOn is None:
                raise ValueError("Discarding requires a remark and a discard date.")
            if not dates[-1] <= self.discardedOn <= today():
                raise ValueError("Discard date must be between the last reached stage and today.")
        elif self.discardNote or self.discardedOn is not None:
            raise ValueError("Discard details require a discarded record.")
        ordered = "order_received" in self.stageDates
        if self.paymentReceived and (not ordered or self.paymentReceivedOn is None):
            raise ValueError("Payment receipt requires Order in hand and a received date.")
        if self.paymentReceivedOn and (
            not ordered
            or self.paymentReceivedOn < self.stageDates["order_received"]
            or self.paymentReceivedOn > today()
        ):
            raise ValueError("Payment date must be between order confirmation and today.")
        if "closed" in self.stageDates:
            if not self.paymentReceived:
                raise ValueError("Closing requires delivery and payment receipt.")
            if self.paymentReceivedOn > self.stageDates["closed"]:
                raise ValueError("Closed date cannot precede payment receipt.")
        if self.feedbackReceived:
            if "closed" not in self.stageDates or self.feedbackReceivedOn is None:
                raise ValueError("Feedback receipt requires a closed record and a received date.")
            if not self.stageDates["closed"] <= self.feedbackReceivedOn <= today():
                raise ValueError("Feedback date must be between closure and today.")
        elif self.feedbackReceivedOn is not None:
            raise ValueError("A feedback date requires feedback receipt.")
        if self.stockUpdated:
            if not ordered or self.stockUpdatedOn is None:
                raise ValueError("Stock update requires Order in hand and an updated date.")
            if not self.stageDates["order_received"] <= self.stockUpdatedOn <= today():
                raise ValueError("Stock update date must be between order confirmation and today.")
        elif self.stockUpdatedOn is not None:
            raise ValueError("A stock update date requires stock update confirmation.")
        return self


class StageChange(Model):
    stage: Stage
    occurredOn: date | None = None
    quotationReference: Annotated[str, StringConstraints(max_length=100)] | None = None
    reason: Text = ""


class PaymentChange(Model):
    received: bool | None = None
    occurredOn: date | None = None
    note: Text | None = None
    reason: Text = ""


class FeedbackChange(Model):
    received: bool


class StockChange(Model):
    updated: bool


T = TypeVar("T")


class Write(Model, Generic[T]):
    writeId: Identifier
    entityId: Identifier
    baseRevision: Identifier | None = None
    data: T


class Prepare(Model):
    entityType: EntityType
    entityId: Identifier | None = None


class Resolution(Model):
    writeId: Identifier
    parents: list[Identifier] = Field(min_length=2, max_length=30)
    selectedRevision: Identifier
    reason: Name


class Revision(Model):
    schemaVersion: Literal[1] = 1
    id: Identifier
    entityId: Identifier
    entityType: EntityType
    parents: list[Identifier] = Field(default_factory=list, max_length=30)
    createdAt: str
    actor: str
    action: str
    reason: str = ""
    fingerprint: str
    data: dict


def record_stage(record: dict) -> str:
    dates = record_stage_dates(record)
    return next(s for s in reversed(STAGES) if s in dates)


def discard_record(record: RecordData, change: DiscardChange) -> RecordData:
    return RecordData.model_validate(
        {**record.model_dump(), "discarded": True, "discardNote": change.reason, "discardedOn": today()}
    )


def stage_change(record: RecordData, change: StageChange) -> RecordData:
    result = record.model_dump()
    current = STAGES.index(record_stage(result))
    target = STAGES.index(change.stage)
    if target > current + 1:
        raise Problem("Mark the next stage only; earlier stages need their actual dates.")
    if target <= current and not change.reason.strip():
        raise Problem("A correction or backtrack requires a reason.")
    if change.stage != "closed" and record.feedbackReceived:
        raise Problem("Undo feedback received before reopening the record.")
    if target < STAGES.index("order_received") and record.stockUpdated:
        raise Problem("Undo stock updated before removing order confirmation.")
    if change.stage == "dispatched":
        if not record.stockUpdated:
            raise Problem("Confirm the stock update before dispatching this sales record.")
        if (change.occurredOn or today()) < record.stockUpdatedOn:
            raise Problem("Dispatch date cannot precede the stock update date.")
    before_order = target < STAGES.index("order_received")
    if before_order and record.paymentReceived:
        raise Problem("Correct the payment before removing order confirmation.")
    if target <= current:
        result["stageDates"] = {s: d for s, d in result["stageDates"].items() if STAGES.index(s) <= target}
    result["stageDates"][change.stage] = change.occurredOn or today()
    if change.quotationReference is not None:
        if change.stage != "follow_up":
            raise Problem("A quotation number can be set during the Follow-up transition.")
        result["quotationReference"] = change.quotationReference
    if before_order:
        result.update(
            paymentReceived=False,
            paymentDueOn=None,
            paymentReceivedOn=None,
        )
    return RecordData.model_validate(result)


def payment_change(record: RecordData, change: PaymentChange) -> RecordData:
    if change.received is None and change.note is None:
        raise Problem("Enter payment details or confirm receipt.")
    if change.occurredOn is not None and change.received is not True:
        raise Problem("A receipt date requires Received confirmation.")
    if change.received is not None and "order_received" not in record.stageDates:
        raise Problem("Confirm the order before recording payment.")
    if "closed" in record.stageDates and change.received is False:
        raise Problem("Reopen the record before undoing payment receipt.")
    result = record.model_dump()
    if change.note is not None:
        result["paymentNote"] = change.note
    if change.received is not None:
        result["paymentReceived"] = change.received
        result["paymentReceivedOn"] = (
            (change.occurredOn or (record.paymentReceivedOn if record.paymentReceived else None) or today())
            if change.received
            else None
        )
    if change.received is True and record_stage(result) == "delivered":
        result["stageDates"]["closed"] = today()
    return RecordData.model_validate(result)


def feedback_change(record: RecordData, change: FeedbackChange) -> RecordData:
    if "closed" not in record.stageDates:
        raise Problem("Close the sales record before recording feedback.")
    return RecordData.model_validate(
        {
            **record.model_dump(),
            "feedbackReceived": change.received,
            "feedbackReceivedOn": (record.feedbackReceivedOn or today()) if change.received else None,
        }
    )


def stock_change(record: RecordData, change: StockChange) -> RecordData:
    if "order_received" not in record.stageDates:
        raise Problem("Confirm the order before confirming a stock update.")
    if "dispatched" in record.stageDates and not change.updated:
        raise Problem("Correct the record back to Order in hand before undoing stock confirmation.")
    result = {
        **record.model_dump(),
        "stockUpdated": change.updated,
        "stockUpdatedOn": (record.stockUpdatedOn or today()) if change.updated else None,
    }
    return RecordData.model_validate(result)


def add_followup(record: RecordData, entry: FollowUp) -> RecordData:
    if not entry.note.strip():
        raise Problem("Enter a follow-up note.")
    effective = entry.occurredOn or today()
    if effective > today() or effective < record.stageDates["new_lead"]:
        raise Problem("Follow-up date must be between the lead-acquired date and today.")
    result = record.model_dump()
    result["followUps"].append(
        {
            "id": uuid4().hex,
            "note": entry.note,
            "occurredOn": effective,
            "recordedAt": now(),
        }
    )
    result["nextFollowUpOn"] = entry.nextFollowUpOn
    return RecordData.model_validate(result)


MODELS = {"account": AccountData, "contact": ContactData, "record": RecordData}
