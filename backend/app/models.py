"""API schemas.

The transaction *draft* is what the person edits. Numbers in it are text, exactly as typed;
``app.calculations`` parses and checks them and reports problems per field instead of
rejecting the whole request, so the form can show each problem next to its field.
An empty string means "not provided".
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class PaymentStatus(str, Enum):
    NOT_SPECIFIED = "not_specified"
    UNPAID = "unpaid"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    REFUNDED = "refunded"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


RowId = Annotated[str, StringConstraints(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")]
NumberText = Annotated[str, StringConstraints(max_length=32)]
DateText = Annotated[str, StringConstraints(max_length=10)]
Name = Annotated[str, StringConstraints(max_length=120)]
Terms = Annotated[str, StringConstraints(max_length=1000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineItemDraft(_Strict):
    id: RowId
    description: Annotated[str, StringConstraints(max_length=200)] = ""
    quantity: NumberText = ""
    unit_price: NumberText = ""


class AdjustmentDraft(_Strict):
    """A discount or an additional fee: a fixed amount, or a percentage of the items subtotal."""

    id: RowId
    label: Name = ""
    kind: Literal["amount", "percent"] = "amount"
    value: NumberText = ""


class TransactionDraft(_Strict):
    currency: Annotated[str, StringConstraints(max_length=3)] = ""
    seller: Name = ""
    buyer: Name = ""
    transaction_date: DateText = ""
    due_date: DateText = ""
    line_items: list[LineItemDraft] = Field(default_factory=list, max_length=50)
    discounts: list[AdjustmentDraft] = Field(default_factory=list, max_length=10)
    additional_fees: list[AdjustmentDraft] = Field(default_factory=list, max_length=10)
    delivery_fee: NumberText = ""
    amount_paid: NumberText = ""
    payment_status: PaymentStatus = PaymentStatus.NOT_SPECIFIED
    payment_terms: Terms = ""
    delivery_terms: Terms = ""
    notes: Annotated[str, StringConstraints(max_length=2000)] = ""
    # A total the conversation states. Only used to cross-check the calculation.
    stated_total: NumberText = ""
    # A balance still owed that the conversation or the person states. Only used to cross-check
    # the calculation, never to change it: the backend's own outstanding figure is what's recorded.
    stated_outstanding: NumberText = ""

    @model_validator(mode="after")
    def _row_ids_are_unique(self) -> TransactionDraft:
        ids = [row.id for row in (*self.line_items, *self.discounts, *self.additional_fees)]
        if len(ids) != len(set(ids)):
            raise ValueError("Item, discount and fee ids must be unique.")
        return self


class Issue(BaseModel):
    code: str
    severity: Literal["error", "warning"]
    field: str | None = None
    message: str


class AmountLine(BaseModel):
    id: str
    amount: str | None


class Calculation(BaseModel):
    """Totals computed by the backend. Amounts are decimal strings; null means unknown."""

    currency: str | None
    minor_units: int | None
    line_items: list[AmountLine]
    items_subtotal: str | None
    discounts: list[AmountLine]
    discount_total: str | None
    additional_fees: list[AmountLine]
    fee_total: str | None
    delivery_fee: str | None
    total: str | None
    amount_paid: str | None
    outstanding: str | None
    issues: list[Issue]
    can_export: bool


class Evidence(BaseModel):
    screenshot: int
    quote: str


class FieldMeta(BaseModel):
    """What the extraction found for one field, shown next to it in the review form."""

    found: bool
    confidence: Confidence | None
    evidence: list[Evidence]
    note: str | None


class ReviewReason(BaseModel):
    field: str | None
    message: str


PersonRole = Literal["seller", "buyer", "payer", "payee", "agent", "other"]


class Person(BaseModel):
    """Someone with a part in the deal, as the conversation shows them."""

    name: str
    role: PersonRole
    is_owner: bool = False
    # Digits, when the conversation shows their number (an export's sender label, the chat header).
    phone: str = ""
    quote: str = ""


class Parties(BaseModel):
    """Who is who, seen from the person whose phone the screenshots or export came from."""

    # "seller" or "buyer" when the conversation shows which side the phone's owner is on, otherwise "".
    owner_role: Literal["seller", "buyer", ""] = ""
    # The other side's name as the top of the chat (or an export) shows it; it may be a phone number.
    other_party: str = ""
    # Digits of the other side's number, when the conversation shows it.
    other_phone: str = ""
    people: list[Person] = Field(default_factory=list)


class OtherDeal(BaseModel):
    """A separate deal in the same conversation, left out of this record."""

    summary: str
    last_message: str = ""


class ChatParticipant(BaseModel):
    key: str
    name: str
    side: Literal["owner", "contact", "unknown"]
    phone: str = ""
    messages: int = 0


class ChatSummary(BaseModel):
    """What was read from a WhatsApp chat export."""

    participants: list[ChatParticipant]
    group: bool
    messages: int
    first_day: str = ""
    last_day: str = ""
    # How the phone owner was worked out: "file_name", "your_name", or "" when it wasn't.
    owner_source: str = ""
    photos_read: int = 0
    media_in_archive: int = 0
    truncated: bool = False


class ExtractionResponse(BaseModel):
    contains_transaction: bool
    summary: str
    draft: TransactionDraft
    # "screenshots", or "chat_export" for a WhatsApp export (.txt or .zip).
    source: Literal["screenshots", "chat_export"] = "screenshots"
    parties: Parties = Field(default_factory=Parties)
    other_deals: list[OtherDeal] = Field(default_factory=list)
    chat: ChatSummary | None = None
    # Keyed by draft path: "seller", "currency", "line_items.item-1", "discounts.discount-1", ...
    field_meta: dict[str, FieldMeta]
    review_required: bool
    review_reasons: list[ReviewReason]
    calculation: Calculation
    # The day label ("today", "yesterday", a weekday) the transaction date was worked out from, if any.
    date_label: str = ""
    screenshots: int
    images_sent: int


class ClientStats(_Strict):
    """Counts the browser reports with an export, for measuring how often people correct things."""

    from_extraction: bool = False
    edited_fields: int = Field(0, ge=0, le=10_000)
    added_fields: int = Field(0, ge=0, le=10_000)


class DocumentRequest(_Strict):
    draft: TransactionDraft
    stats: ClientStats | None = None


class CurrencyInfo(BaseModel):
    code: str
    name: str
    symbol: str
    minor_units: int


class UploadLimits(BaseModel):
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    accepted_types: list[str]
    # A WhatsApp chat export (.txt, or .zip with media), read on its own rather than with screenshots.
    max_chat_export_bytes: int = 0


class ConfigResponse(BaseModel):
    currencies: list[CurrencyInfo]
    default_currency: str
    extraction_available: bool
    limits: UploadLimits
