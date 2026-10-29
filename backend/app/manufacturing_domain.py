from typing import Literal

from pydantic import Field, model_validator

from .domain import Identifier, Model, Name, Text

Directory = Literal["products", "operations", "suppliers", "contacts"]


class Product(Model):
    name: Name
    partNumber: Text = ""
    notes: Text = ""


class Operation(Model):
    name: Name
    description: Text = ""


class Supplier(Model):
    name: Name
    type: Literal["company", "individual"] = "company"
    relationship: Literal["external", "internal"] = "external"
    address: Text = ""
    email: Text = ""
    phone: Text = ""
    notes: Text = ""

    @model_validator(mode="after")
    def internal_company(self):
        if self.type == "individual" and self.relationship == "internal":
            raise ValueError("Use an internal company supplier and its contact for in-house work.")
        return self


class Contact(Model):
    name: Name
    supplierId: Identifier
    designation: Text = ""
    email: Text = ""
    phone: Text = ""


class Step(Model):
    operationId: Identifier
    instructions: Text = ""


class Assignment(Model):
    supplierId: Identifier
    contactId: Identifier | None = None
    notes: Text = ""


class Reorder(Model):
    stepIds: list[Identifier]


class Primary(Model):
    assignmentId: Identifier


class RemoveAssignment(Model):
    replacementId: Identifier | None = None


class Command(Model):
    writeId: Identifier
    id: Identifier
    baseVersion: int | None = Field(default=None, ge=1)
    data: dict


MODELS = {"products": Product, "operations": Operation, "suppliers": Supplier, "contacts": Contact}
