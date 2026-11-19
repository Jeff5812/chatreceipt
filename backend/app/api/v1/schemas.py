"""Request and response shapes of the v1 API. Database rows are never returned directly."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models import Calculation, PaymentStatus, TransactionDraft
from app.records.lifecycle import State
from app.records.review import Operation


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationRequest(_Request):
    """Either uploads already stored, or a pasted conversation."""

    upload_ids: list[UUID] = Field(default_factory=list, max_length=20)
    text: str | None = Field(default=None, max_length=512 * 1024)
    title: str = Field(default="", max_length=120)


class VersionedRequest(_Request):
    expected_version: int = Field(ge=1)


class ReasonRequest(VersionedRequest):
    reason: str = Field(min_length=1, max_length=500)


class ReviewRequest(VersionedRequest):
    operations: list[Operation] = Field(min_length=1, max_length=50)


class ManualTransactionRequest(_Request):
    draft: TransactionDraft


class VoidRequest(_Request):
    reason: str = Field(min_length=1, max_length=500)


class EraseRequest(_Request):
    confirm: Literal["DELETE MY DATA"]


class UploadView(BaseModel):
    id: UUID
    kind: str
    filename: str
    content_type: str
    bytes: int
    sha256: str
    width: int | None
    height: int | None
    status: str
    created_at: datetime
    expires_at: datetime
    duplicate: bool = False


class UploadList(BaseModel):
    uploads: list[UploadView]


class JobView(BaseModel):
    id: UUID
    kind: str
    subject_id: UUID
    status: str
    attempts: int
    max_attempts: int
    error_code: str | None
    error_message: str | None
    result: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ConversationAccepted(BaseModel):
    conversation_id: UUID
    transaction_id: UUID
    job: JobView


class ConversationView(BaseModel):
    id: UUID
    source_type: str
    title: str
    created_at: datetime
    upload_ids: list[UUID]
    transaction_ids: list[UUID]
    latest_run_id: UUID | None


class ParticipantView(BaseModel):
    key: str
    name: str
    side: str
    role: str
    role_confidence: str | None
    role_evidence: list[str]


class MessageView(BaseModel):
    key: str
    seq: int
    participant: str | None
    kind: str
    text: str
    original_text: str
    time: str
    day: str
    legibility: str
    source_kind: str
    screenshot: int


class ConversationMessages(BaseModel):
    run_id: UUID | None
    participants: list[ParticipantView]
    messages: list[MessageView]


class TransactionSummary(BaseModel):
    id: UUID
    state: State
    version: int
    origin: str
    agreement_status: str | None
    summary: str
    currency: str
    seller: str
    buyer: str
    transaction_date: date | None
    total: str | None
    amount_paid: str | None
    outstanding: str | None
    payment_status: PaymentStatus
    review_required: bool
    conversation_id: UUID | None
    state_reason: str
    created_at: datetime
    updated_at: datetime


class TransactionList(BaseModel):
    transactions: list[TransactionSummary]
    next_cursor: str | None


class EvidenceView(BaseModel):
    message: str
    screenshot: int | None
    quote: str


class FieldView(BaseModel):
    path: str
    label: str
    field_class: str
    status: str
    source: str
    confidence: str | None
    grounded: bool | None
    blocking: bool
    resolved: bool
    reasons: list[str]
    candidates: list[dict[str, Any]]
    evidence: list[EvidenceView]


class RuleView(BaseModel):
    rule: str
    description: str
    outcome: str
    fields: list[str]
    messages: list[str]


class ValidationView(BaseModel):
    outcome: str
    can_approve: bool
    passed: list[str]
    failed: list[str]
    warnings: list[str]
    missing: list[str]
    contradictions: list[str]
    review_required: list[str]
    rules: list[RuleView]


class TransactionDetail(TransactionSummary):
    draft: TransactionDraft
    calculation: Calculation
    validation: ValidationView
    fields: dict[str, FieldView]
    analysis: dict[str, Any]
    available_actions: list[str]
    approved_version: int | None
    payment_notice: str = "Payment details are recorded from the conversation. ChatReceipt doesn't verify payments."


class HistoryView(BaseModel):
    events: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    versions: list[dict[str, Any]]


class ReceiptView(BaseModel):
    id: UUID
    reference: str
    transaction_id: UUID
    transaction_version: int
    status: str
    document_status: str
    document_sha256: str | None
    replaces_receipt_id: UUID | None
    void_reason: str
    issued_at: datetime
    superseded_at: datetime | None
    voided_at: datetime | None


class ReceiptList(BaseModel):
    receipts: list[ReceiptView]
    next_cursor: str | None
