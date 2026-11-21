"""The JSON schema the model's answer must follow, and the model that re-validates it.

Every property is required and nothing is nullable: a missing value is an empty string with
``found: false``. Avoiding optional fields and unions keeps the schema simple for structured
output and makes "not in the conversation" explicit instead of implied.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.currencies import CURRENCY_CODES
from app.models import PaymentStatus

CONFIDENCE_VALUES = ("high", "medium", "low")
PAYMENT_STATUS_VALUES = tuple(status.value for status in PaymentStatus)
ADJUSTMENT_KINDS = ("amount", "percent")
OWNER_ROLES = ("seller", "buyer")
PERSON_ROLES = ("seller", "buyer", "payer", "payee", "agent", "other")
DAY_LABELS = ("", "today", "yesterday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
AMBIGUITY_FIELDS = (
    "currency",
    "seller",
    "buyer",
    "transaction_date",
    "due_date",
    "line_items",
    "discounts",
    "additional_fees",
    "delivery_fee",
    "agreed_total",
    "amount_paid",
    "outstanding_amount",
    "payment_status",
    "payment_terms",
    "delivery_terms",
    "notes",
    "screenshots",
    "other",
)


def _object(properties: dict[str, Any], description: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


_STRING: dict[str, Any] = {"type": "string"}
_CONFIDENCE: dict[str, Any] = {"type": "string", "enum": list(CONFIDENCE_VALUES)}
_EVIDENCE: dict[str, Any] = {
    "type": "array",
    "items": _object(
        {
            "screenshot": {"type": "integer", "description": "Screenshot number the quote is from."},
            "quote": {"type": "string", "description": "Short verbatim text from that screenshot."},
        }
    ),
}
_AMOUNT: dict[str, Any] = {
    "type": "string",
    "description": "Plain number in the currency's main unit, such as 17000 or 2500.50. "
    "Empty string when not found.",
}
_DATE: dict[str, Any] = {"type": "string", "description": "YYYY-MM-DD, or empty string."}
_SUPPORT: dict[str, Any] = {"confidence": _CONFIDENCE, "evidence": _EVIDENCE, "note": _STRING}


def _field(value: dict[str, Any], description: str) -> dict[str, Any]:
    return _object({"found": {"type": "boolean"}, "value": value, **_SUPPORT}, description)


_LINE_ITEM = _object(
    {
        "description": _STRING,
        "quantity": {"type": "string", "description": "Plain number, or empty string if unclear."},
        "unit_price": _AMOUNT,
        **_SUPPORT,
    }
)
_ADJUSTMENT = _object(
    {
        "label": _STRING,
        "kind": {"type": "string", "enum": list(ADJUSTMENT_KINDS)},
        "value": {
            "type": "string",
            "description": "Amount in the currency's main unit, or the percentage (10 for 10%).",
        },
        **_SUPPORT,
    }
)

EXTRACTION_SCHEMA: dict[str, Any] = _object(
    {
        "contains_transaction": {"type": "boolean"},
        "summary": _STRING,
        "currency": _field({"type": "string", "enum": [*CURRENCY_CODES, ""]}, "ISO currency code."),
        "seller": _field(_STRING, "Seller's name as shown in the conversation."),
        "buyer": _field(_STRING, "Buyer's name as shown in the conversation."),
        "owner_role": _field(
            {"type": "string", "enum": [*OWNER_ROLES, ""]},
            "Whether the phone's owner, whose messages are on the right, is the seller or the buyer.",
        ),
        "other_party": _field(_STRING, "The other person's name, username or number as the top of the chat shows it."),
        "people": {
            "type": "array",
            "description": "Everyone with a part in the deal, at most six.",
            "items": _object(
                {
                    "name": {"type": "string", "description": "As the conversation shows it."},
                    "role": {"type": "string", "enum": list(PERSON_ROLES)},
                    "is_owner": {"type": "boolean", "description": "True for the phone's owner."},
                    "quote": {"type": "string", "description": "Short verbatim text that shows their part."},
                }
            ),
        },
        "other_deals": {
            "type": "array",
            "description": "Other separate deals in the conversation, newest first. Empty when there is one.",
            "items": _object(
                {
                    "summary": {"type": "string", "description": "One plain line: what, for how much, when."},
                    "last_message": {"type": "string", "description": "Key of its last message in a chat export, such as m41; otherwise empty."},
                }
            ),
        },
        "transaction_date": _field(_DATE, "Date the agreement was made."),
        "date_label": {
            "type": "string",
            "enum": list(DAY_LABELS),
            "description": "When the agreement sits under a relative day label instead of a full date (WhatsApp shows Today, Yesterday or a weekday for recent days), that label in lower case. Empty otherwise.",
        },
        "due_date": _field(_DATE, "A stated payment or delivery deadline."),
        "line_items": {"type": "array", "items": _LINE_ITEM},
        "discounts": {"type": "array", "items": _ADJUSTMENT},
        "additional_fees": {"type": "array", "items": _ADJUSTMENT},
        "delivery_fee": _field(_AMOUNT, "Delivery, shipping or dispatch charge."),
        "agreed_total": _field(_AMOUNT, "A total stated explicitly in the conversation."),
        "amount_paid": _field(_AMOUNT, "An amount the conversation says was paid."),
        "outstanding_amount": _field(_AMOUNT, "A balance stated explicitly in the conversation."),
        "payment_status": _field(
            {"type": "string", "enum": list(PAYMENT_STATUS_VALUES)},
            "Payment status the conversation supports.",
        ),
        "payment_terms": _field(_STRING, "What was said about payment."),
        "delivery_terms": _field(_STRING, "What was said about delivery or collection."),
        "notes": _field(_STRING, "Other details that matter to the agreement."),
        "ambiguities": {
            "type": "array",
            "items": _object(
                {"field": {"type": "string", "enum": list(AMBIGUITY_FIELDS)}, "description": _STRING}
            ),
        },
        "review_required": {"type": "boolean"},
    }
)


class _AIModel(BaseModel):
    # Types are checked strictly enough to catch a malformed answer; allowed values (enums,
    # number formats, dates) are checked during mapping, where problems become review flags.
    model_config = ConfigDict(extra="ignore")


class AIEvidence(_AIModel):
    screenshot: int
    quote: str


class AIField(_AIModel):
    found: bool
    value: str
    confidence: str
    evidence: list[AIEvidence]
    note: str


class AILineItem(_AIModel):
    description: str
    quantity: str
    unit_price: str
    confidence: str
    evidence: list[AIEvidence]
    note: str


class AIAdjustment(_AIModel):
    label: str
    kind: str
    value: str
    confidence: str
    evidence: list[AIEvidence]
    note: str


class AIAmbiguity(_AIModel):
    field: str
    description: str


class AIPerson(_AIModel):
    name: str
    role: str
    is_owner: bool = False
    quote: str = ""


class AIOtherDeal(_AIModel):
    summary: str
    last_message: str = ""


def _not_found() -> AIField:
    return AIField(found=False, value="", confidence="low", evidence=[], note="")


class AIExtraction(_AIModel):
    contains_transaction: bool
    summary: str
    currency: AIField
    seller: AIField
    buyer: AIField
    # Optional when parsing, so an answer from before these fields existed still validates.
    owner_role: AIField = Field(default_factory=_not_found)
    other_party: AIField = Field(default_factory=_not_found)
    people: list[AIPerson] = Field(default_factory=list)
    other_deals: list[AIOtherDeal] = Field(default_factory=list)
    transaction_date: AIField
    date_label: str = ""
    due_date: AIField
    line_items: list[AILineItem]
    discounts: list[AIAdjustment]
    additional_fees: list[AIAdjustment]
    delivery_fee: AIField
    agreed_total: AIField
    amount_paid: AIField
    outstanding_amount: AIField
    payment_status: AIField
    payment_terms: AIField
    delivery_terms: AIField
    notes: AIField
    ambiguities: list[AIAmbiguity]
    review_required: bool
