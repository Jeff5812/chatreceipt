"""Field-level states: how well each value in a record is supported, and why.

Every field of a proposed record gets a state instead of one confidence for the whole record:

- confirmed: stated explicitly in the conversation (or confirmed or entered by a person).
- inferred: supported, but it needs a reasonable reading (an implied quantity of 1, a currency
  taken from context, medium model confidence).
- missing: the conversation doesn't give it.
- contradictory: the conversation gives competing values, or the model's reading disagrees with
  ChatReceipt's own rules. The value is left empty and the candidates are listed.
- invalid: the value fails validation (not a number, too many decimals, negative...).
- needs_review: low confidence, a figure that isn't in the message it cites, or a price that
  depends on a condition.

The review policy (``review_policy``) decides which of these must be resolved before approval.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict

from app.models import Confidence


class FieldStatus(str, Enum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    MISSING = "missing"
    CONTRADICTORY = "contradictory"
    INVALID = "invalid"
    NEEDS_REVIEW = "needs_review"


class FieldSource(str, Enum):
    EXTRACTED = "extracted"  # read from the conversation
    DERIVED = "derived"  # worked out by ChatReceipt's rules, e.g. an implied quantity of 1
    USER = "user"  # entered, corrected or confirmed by a person
    NONE = "none"  # no value


class FieldClass(str, Enum):
    FINANCIAL = "financial"  # money, quantities, currency, payment status
    PARTY = "party"  # seller and buyer names
    DATE = "date"
    DESCRIPTION = "description"  # item descriptions, terms, notes
    PROCESS = "process"  # checks about the record as a whole: agreement, duplicates, screenshots


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str
    source: str  # "accepted_offer", "model_reading", "offer", "stated", ...
    messages: tuple[str, ...] = ()
    note: str = ""


class FieldState(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    label: str
    field_class: FieldClass
    status: FieldStatus
    source: FieldSource
    confidence: Confidence | None = None
    evidence: tuple[str, ...] = ()  # message keys
    reasons: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    grounded: bool | None = None  # every figure appears in the message it cites
    blocking: bool = False  # must be corrected or confirmed before approval
    resolved: bool = False  # a person corrected or confirmed it


# Process checks use these paths; they aren't fields of the draft.
AGREEMENT = "agreement"
DUPLICATE_CHECK = "duplicate_check"
CONVERSATION = "conversation"
INSTRUCTIONS = "instructions"
ITEMS_NOT_AGREED = "items_not_agreed"
PROCESS_PATHS = frozenset({AGREEMENT, DUPLICATE_CHECK, CONVERSATION, INSTRUCTIONS, ITEMS_NOT_AGREED})

_FINANCIAL = re.compile(
    r"^(currency|delivery_fee|amount_paid|payment_status|stated_total"
    r"|line_items\.[^.]+\.(quantity|unit_price)|(discounts|additional_fees)\.[^.]+\.(value|kind))$"
)

LABELS = {
    "currency": "the currency",
    "seller": "the seller",
    "buyer": "the buyer",
    "transaction_date": "the transaction date",
    "due_date": "the due date",
    "delivery_fee": "the delivery fee",
    "stated_total": "the total mentioned",
    "amount_paid": "the amount paid",
    "payment_status": "the payment status",
    "payment_terms": "the payment terms",
    "delivery_terms": "the delivery terms",
    "notes": "the notes",
    AGREEMENT: "the agreement",
    DUPLICATE_CHECK: "possible duplicates",
    CONVERSATION: "the screenshots",
    INSTRUCTIONS: "instructions inside the chat",
    ITEMS_NOT_AGREED: "items discussed but not agreed",
}


def field_class(path: str) -> FieldClass:
    if path in PROCESS_PATHS:
        return FieldClass.PROCESS
    if _FINANCIAL.match(path):
        return FieldClass.FINANCIAL
    if path in ("seller", "buyer"):
        return FieldClass.PARTY
    if path in ("transaction_date", "due_date"):
        return FieldClass.DATE
    return FieldClass.DESCRIPTION
