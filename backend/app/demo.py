from datetime import timedelta
from uuid import uuid4

from .domain import STAGES, AccountData, ContactData, RecordData, Revision, now, today
from .storage import digest


def seed(repo):
    if repo.meta:
        return
    examples = [
        ("Northstar Packaging", "Roller assembly enquiry", 0, False),
        ("Bluebird Printworks", "Feeder belt replacement", 1, False),
        ("Cedar Industries", "Press maintenance spares", 1, False),
        ("Atlas Paper Co.", "Gripper delivery set", 2, False),
        ("Meadow Graphics", "Cooling pump and seals", 2, True),
        ("Summit Offset", "Fountain roller assembly", 3, False),
        ("Harbor Print Studio", "Filter and coupling set", 4, False),
        ("Pinecrest Press", "Drive belt order", 5, True),
    ]
    for index, (name, requirement, stage, paid) in enumerate(examples):
        account_id, contact_id, record_id = uuid4().hex, uuid4().hex, uuid4().hex
        acquired = today() - timedelta(days=16 + index)
        dates = {s: acquired + timedelta(days=i * 2) for i, s in enumerate(STAGES[: stage + 1])}
        data = RecordData(
            accountId=account_id,
            workflowVersion=3,
            contactId=contact_id,
            productNotes=f"{requirement}. Confirm specifications before quotation.",
            notes="Sample record for exploring the local demo. Not imported customer data.",
            quotationReference=f"DEMO-Q-{100 + index}" if stage >= 1 else "",
            stageDates=dates,
            paymentReceived=paid,
            paymentReceivedOn=dates.get("order_received") if paid else None,
            nextFollowUpOn=today() if stage < 2 else None,
        )
        entities = [
            (
                "account",
                account_id,
                AccountData(name=name, address="Demo city, India").model_dump(mode="json"),
            ),
            (
                "contact",
                contact_id,
                ContactData(
                    accountId=account_id,
                    name="Demo contact",
                    email=f"contact{index}@example.com",
                    designation="Purchasing",
                ).model_dump(mode="json"),
            ),
            ("record", record_id, data.model_dump(mode="json")),
        ]
        for kind, entity_id, content in entities:
            revision = Revision(
                id=repo.backend.allocate(),
                entityId=entity_id,
                entityType=kind,
                createdAt=now(),
                actor="local-demo",
                action="demo_seed",
                fingerprint=digest(content),
                data=content,
            ).model_dump(mode="json")
            repo.backend.create(revision)
            repo._apply(revision)
