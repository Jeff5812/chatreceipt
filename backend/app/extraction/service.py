"""Turn the model's validated answer into an editable draft, evidence and review flags.

Nothing the model reports is trusted as-is: names and text are cleaned and length-limited,
numbers must be plain non-negative numbers, dates must be real calendar dates, and anything that
fails is left empty and flagged for the person to check, never guessed.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import date, timedelta

from pydantic import ValidationError

from app.calculations import REVIEW_WARNING_CODES, compute, parse_number, to_calculation, to_plain
from app.config import Settings
from app.currencies import get_currency
from app.extraction.provider import (
    ErrorKind,
    ExtractionProvider,
    ExtractionProviderError,
    ProviderUsage,
)
from app.extraction.schema import (
    ADJUSTMENT_KINDS,
    CONFIDENCE_VALUES,
    PAYMENT_STATUS_VALUES,
    AIAdjustment,
    AIEvidence,
    DAY_LABELS,
    OWNER_ROLES,
    PERSON_ROLES,
    AIExtraction,
    AIField,
    AILineItem,
    AIOtherDeal,
    AIPerson,
)
from app.models import (
    AdjustmentDraft,
    Confidence,
    Evidence,
    ExtractionResponse,
    FieldMeta,
    LineItemDraft,
    OtherDeal,
    Parties,
    Person,
    PaymentStatus,
    ReviewReason,
    TransactionDraft,
)
from app.names import phone_in_label, same_name, username_in_label
from app.observability import log_event
from app.uploads import PreparedImage

MAX_EVIDENCE = 3
MAX_QUOTE_CHARS = 160
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NOT_NAMES = frozenset({"you", "me", "myself", "i", "unknown", "not shown"})

FIELD_LABELS = {
    "currency": "the currency",
    "seller": "the seller",
    "buyer": "the buyer",
    "transaction_date": "the transaction date",
    "due_date": "the due date",
    "delivery_fee": "the delivery fee",
    "stated_total": "the total mentioned",
    "stated_outstanding": "the balance mentioned",
    "amount_paid": "the amount paid",
    "payment_status": "the payment status",
    "payment_terms": "the payment terms",
    "delivery_terms": "the delivery terms",
    "notes": "the notes",
}
# Where each ambiguity the model reports shows up in the form.
AMBIGUITY_TARGETS: dict[str, str | None] = {
    **{name: name for name in FIELD_LABELS},
    "line_items": "line_items",
    "discounts": "discounts",
    "additional_fees": "additional_fees",
    "agreed_total": "stated_total",
    "outstanding_amount": "stated_outstanding",
    "screenshots": None,
    "other": None,
}


def clean_text(text: str, limit: int, *, multiline: bool = False) -> str:
    text = _CONTROL_CHARS.sub(" ", text)
    if multiline:
        lines = (" ".join(line.split()) for line in text.splitlines())
        text = "\n".join(line for line in lines if line)
    else:
        text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _sentence(label: str) -> str:
    return label[:1].upper() + label[1:]


class _Mapper:
    def __init__(self, screenshots: int) -> None:
        self.screenshots = screenshots
        self.meta: dict[str, FieldMeta] = {}
        self.labels: dict[str, str] = {}
        self.reasons: list[ReviewReason] = []
        self._flagged: set[str] = set()

    def reason(self, field: str | None, message: str) -> None:
        if field is not None:
            self._flagged.add(field)
        if not any(r.field == field and r.message == message for r in self.reasons):
            self.reasons.append(ReviewReason(field=field, message=message))

    def _evidence(self, items: list[AIEvidence]) -> list[Evidence]:
        evidence: list[Evidence] = []
        for item in items:
            quote = clean_text(item.quote, MAX_QUOTE_CHARS)
            if quote and 1 <= item.screenshot <= self.screenshots:
                evidence.append(Evidence(screenshot=item.screenshot, quote=quote))
            if len(evidence) == MAX_EVIDENCE:
                break
        return evidence

    def record(self, key: str, label: str, found: bool, confidence: str, evidence: list[AIEvidence], note: str) -> None:
        self.labels[key] = label
        self.meta[key] = FieldMeta(
            found=found,
            confidence=(Confidence(confidence) if confidence in CONFIDENCE_VALUES else Confidence.LOW)
            if found
            else None,
            evidence=self._evidence(evidence) if found else [],
            note=clean_text(note, 300) or None,
        )

    def number(self, key: str, raw: str, label: str) -> str:
        text = raw.strip()
        if not text:
            return ""
        try:
            value = parse_number(text)
        except ValueError:
            self.reason(key, f"Check {label}: the conversation gives “{clean_text(text, 40)}”, which isn't a plain number.")
            return ""
        if value is None:
            return ""
        if value < 0:
            self.reason(key, f"Check {label}: a negative value was found.")
            return ""
        return to_plain(value)

    def text(self, key: str, field: AIField, limit: int, *, multiline: bool = False) -> str:
        value = clean_text(field.value, limit, multiline=multiline) if field.found else ""
        self.record(key, FIELD_LABELS[key], bool(value), field.confidence, field.evidence, field.note)
        return value

    def party(self, key: str, field: AIField) -> str:
        """A seller's or buyer's name. “You” is how a chat shows the phone's owner, so it's no name."""
        if field.found and field.value.strip().strip(".").lower() in _NOT_NAMES:
            self.record(key, FIELD_LABELS[key], False, field.confidence, [], "The chat only shows this side as “You”, so no name was recorded.")
            return ""
        return self.text(key, field, 120)

    def parties(self, draft: TransactionDraft, owner: AIField, other: AIField) -> tuple[TransactionDraft, Parties]:
        """Which side the phone's owner is on, with the other side's name put in the other role.

        The name at the top of a chat is the other person's, so it can only go in the role the owner
        doesn't have. When the model gives the owner that very name, one of its readings is wrong:
        nothing is moved, the role is left unknown, and the person is asked to check.
        """
        other_name = clean_text(other.value, 120) if other.found else ""
        if other_name.strip(".").lower() in _NOT_NAMES:
            other_name = ""
        role = owner.value.strip().lower() if owner.found and owner.confidence in ("high", "medium") else ""
        if role not in OWNER_ROLES:
            role = ""
        if not role or not other_name:
            return draft, Parties(owner_role=role, other_party=other_name)  # type: ignore[arg-type]
        theirs = "buyer" if role == "seller" else "seller"
        if same_name(getattr(draft, role), other_name):
            self.reason("seller", "Check the seller and the buyer: they may be the wrong way round.")
            return draft, Parties(owner_role="", other_party=other_name)
        if not getattr(draft, theirs).strip():
            draft = draft.model_copy(update={theirs: other_name})
            self.record(theirs, FIELD_LABELS[theirs], True, other.confidence, other.evidence, "The name at the top of the chat.")
        return draft, Parties(owner_role=role, other_party=other_name)  # type: ignore[arg-type]

    def people(self, people: Sequence[AIPerson]) -> list[Person]:
        """The people with a part in the deal. A role the schema doesn't know becomes "other"."""
        found: list[Person] = []
        for person in people[:6]:
            name = clean_text(person.name, 120)
            if name.strip(".").lower() in _NOT_NAMES:
                name = ""
            if not name and not person.is_owner:
                continue
            role = person.role if person.role in PERSON_ROLES else "other"
            found.append(
                Person(
                    name=name, role=role, is_owner=person.is_owner, phone=phone_in_label(name),  # type: ignore[arg-type]
                    quote=clean_text(person.quote, MAX_QUOTE_CHARS),
                )
            )
        return found

    def other_deals(self, deals: Sequence[AIOtherDeal]) -> list[OtherDeal]:
        found = []
        for deal in deals[:10]:
            summary = clean_text(deal.summary, 200)
            key = deal.last_message.strip()
            if summary:
                found.append(OtherDeal(summary=summary, last_message=key if re.fullmatch(r"m\d{1,5}", key) else ""))
        return found

    def currency(self, field: AIField) -> str:
        code = field.value.strip().upper() if field.found else ""
        if code and get_currency(code) is None:
            self.reason("currency", "The currency in the conversation isn't supported yet. Choose one, and check the amounts.")
            code = ""
        self.record("currency", FIELD_LABELS["currency"], bool(code), field.confidence, field.evidence, field.note)
        return code

    def date(self, key: str, field: AIField) -> str:
        raw = field.value.strip() if field.found else ""
        if raw:
            valid = False
            if _ISO_DATE.match(raw):
                try:
                    date.fromisoformat(raw)
                    valid = True
                except ValueError:
                    valid = False
            if not valid:
                self.reason(key, f"{_sentence(FIELD_LABELS[key])} in the conversation couldn't be read as a calendar date.")
                raw = ""
        self.record(key, FIELD_LABELS[key], bool(raw), field.confidence, field.evidence, field.note)
        return raw

    def amount(self, key: str, field: AIField) -> str:
        value = self.number(key, field.value, FIELD_LABELS[key]) if field.found else ""
        self.record(key, FIELD_LABELS[key], bool(value), field.confidence, field.evidence, field.note)
        return value

    def payment_status(self, field: AIField) -> PaymentStatus:
        value = field.value.strip()
        if value not in PAYMENT_STATUS_VALUES:
            if value:
                self.reason("payment_status", "The payment status couldn't be determined. Choose it.")
            value = PaymentStatus.NOT_SPECIFIED.value
        self.record("payment_status", FIELD_LABELS["payment_status"], field.found, field.confidence, field.evidence, field.note)
        return PaymentStatus(value)

    def line_item(self, index: int, item: AILineItem) -> LineItemDraft:
        row_id = f"item-{index}"
        key = f"line_items.{row_id}"
        description = clean_text(item.description, 200)
        label = f"item {index}" + (f" ({description})" if description else "")
        row = LineItemDraft(
            id=row_id,
            description=description,
            quantity=self.number(f"{key}.quantity", item.quantity, f"the quantity of {label}"),
            unit_price=self.number(f"{key}.unit_price", item.unit_price, f"the unit price of {label}"),
        )
        self.record(key, label, True, item.confidence, item.evidence, item.note)
        return row

    def adjustment(self, group: str, noun: str, index: int, item: AIAdjustment) -> AdjustmentDraft:
        row_id = f"{noun}-{index}"
        key = f"{group}.{row_id}"
        label_text = clean_text(item.label, 120)
        label = f"the {noun}" + (f" “{label_text}”" if label_text else "")
        row = AdjustmentDraft(
            id=row_id,
            label=label_text,
            kind=item.kind if item.kind in ADJUSTMENT_KINDS else "amount",
            value=self.number(f"{key}.value", item.value, f"{label}"),
        )
        self.record(key, label, True, item.confidence, item.evidence, item.note)
        return row

    def flag_low_confidence(self) -> None:
        for key, meta in self.meta.items():
            if not meta.found or meta.confidence != Confidence.LOW:
                continue
            if any(flagged == key or flagged.startswith(f"{key}.") for flagged in self._flagged):
                continue
            label = self.labels[key]
            if meta.note:
                self.reason(key, f"Check {label}: {meta.note}")
            else:
                self.reason(key, f"Check {label}. The conversation doesn't state it clearly.")


_UNSTATED = re.compile(
    r"\b(?:not|never|isn't|wasn't|aren't|no)\b[^.]{0,40}?\b(?:stated|mentioned|given|specified|provided|said|shown|written|explicit(?:ly)?)\b",
    re.IGNORECASE,
)


def _only_says_unstated(field: str, message: str, ai: AIExtraction) -> bool:
    """A doubt that only says the chat doesn't state a total or balance, which ChatReceipt adds up anyway.

    It would make the person tick "I've checked" for nothing. A doubt that quotes a figure, or one
    about a total the chat did state, is kept: that's where conflicting figures are reported.
    """
    stated = {"agreed_total": ai.agreed_total, "outstanding_amount": ai.outstanding_amount}.get(field)
    return stated is not None and not stated.found and not re.search(r"\d", message) and bool(_UNSTATED.search(message))


def _has_amounts(draft: TransactionDraft) -> bool:
    adjustments = [*draft.discounts, *draft.additional_fees]
    return bool(
        any(item.unit_price for item in draft.line_items)
        or any(row.value and row.kind == "amount" for row in adjustments)
        or draft.delivery_fee
        or draft.amount_paid
    )


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def date_from_label(label: str, received_on: date) -> date | None:
    """The date a WhatsApp day label stands for, seen from the day the screenshot was sent.

    WhatsApp writes "Today", "Yesterday" or a weekday for the last week. A weekday is the most recent
    one before that day: on the day itself WhatsApp would have written "Today".
    """
    if label == "today":
        return received_on
    if label == "yesterday":
        return received_on - timedelta(days=1)
    if label in _WEEKDAYS:
        return received_on - timedelta(days=(received_on.weekday() - _WEEKDAYS.index(label)) % 7 or 7)
    return None


def build_extraction_response(
    ai: AIExtraction, *, screenshots: int, images_sent: int, received_on: date | None = None
) -> ExtractionResponse:
    m = _Mapper(screenshots)
    draft = TransactionDraft(
        currency=m.currency(ai.currency),
        seller=m.party("seller", ai.seller),
        buyer=m.party("buyer", ai.buyer),
        transaction_date=m.date("transaction_date", ai.transaction_date),
        due_date=m.date("due_date", ai.due_date),
        line_items=[m.line_item(i, item) for i, item in enumerate(ai.line_items[:50], start=1)],
        discounts=[m.adjustment("discounts", "discount", i, row) for i, row in enumerate(ai.discounts[:10], start=1)],
        additional_fees=[m.adjustment("additional_fees", "fee", i, row) for i, row in enumerate(ai.additional_fees[:10], start=1)],
        delivery_fee=m.amount("delivery_fee", ai.delivery_fee),
        amount_paid=m.amount("amount_paid", ai.amount_paid),
        payment_status=m.payment_status(ai.payment_status),
        payment_terms=m.text("payment_terms", ai.payment_terms, 1000, multiline=True),
        delivery_terms=m.text("delivery_terms", ai.delivery_terms, 1000, multiline=True),
        notes=m.text("notes", ai.notes, 2000, multiline=True),
        stated_total=m.amount("stated_total", ai.agreed_total),
        # Kept on the draft, so the calculation keeps checking it until the figures agree.
        stated_outstanding=m.amount("stated_outstanding", ai.outstanding_amount),
    )
    draft, parties = m.parties(draft, ai.owner_role, ai.other_party)
    parties = parties.model_copy(
        update={
            "other_phone": phone_in_label(parties.other_party),
            "other_username": username_in_label(parties.other_party),
            "people": m.people(ai.people),
        }
    )
    # A day label becomes a date only when we know the day the screenshot was sent, and the record
    # says where the date came from so the person can catch an old screenshot.
    date_label = ai.date_label.strip().lower() if ai.date_label.strip().lower() in DAY_LABELS[1:] else ""
    resolved = date_from_label(date_label, received_on) if date_label and received_on and not draft.transaction_date else None
    if resolved is not None:
        draft = draft.model_copy(update={"transaction_date": resolved.isoformat()})
        m.meta["transaction_date"] = FieldMeta(
            found=True,
            confidence=Confidence.MEDIUM,
            evidence=[],
            note=f"Worked out from “{date_label.capitalize()}” in the chat and the day the screenshot was sent.",
        )
    computation = compute(draft)

    for ambiguity in ai.ambiguities[:20]:
        message = clean_text(ambiguity.description, 300)
        if message and not _only_says_unstated(ambiguity.field, message, ai):
            m.reason(AMBIGUITY_TARGETS.get(ambiguity.field), message)
    # Amounts without a currency can't be checked or exported; say so plainly instead of leaving
    # only a form error, unless the model already explained it.
    if ai.contains_transaction and not draft.currency and _has_amounts(draft) and not any(r.field == "currency" for r in m.reasons):
        m.reason("currency", "The conversation doesn't show which currency these amounts are in. Choose it before you continue.")
    m.flag_low_confidence()
    if not ai.contains_transaction:
        m.reason(None, "No transaction was found in these screenshots. You can still enter the details yourself.")

    review_required = (
        ai.review_required
        or not ai.contains_transaction
        or bool(m.reasons)
        or computation.has_errors
        or any(issue.code in REVIEW_WARNING_CODES for issue in computation.issues)
    )
    return ExtractionResponse(
        contains_transaction=ai.contains_transaction,
        summary=clean_text(ai.summary, 400),
        draft=draft,
        parties=parties,
        other_deals=m.other_deals(ai.other_deals),
        field_meta=m.meta,
        review_required=review_required,
        review_reasons=m.reasons,
        calculation=to_calculation(draft, computation),
        screenshots=screenshots,
        images_sent=images_sent,
        date_label=date_label if resolved is not None else "",
        payment_reference=payment_reference(ai.payment_reference),
    )


def payment_reference(field: AIField) -> str:
    """A transfer receipt's reference in a form that can be compared: upper-case letters and digits only.

    Anything too short or too long to be a reference is dropped rather than compared: a false match
    would accuse someone of reusing a receipt.
    """
    if not field.found:
        return ""
    compact = "".join(character for character in field.value.upper() if character.isascii() and character.isalnum())
    return compact if 6 <= len(compact) <= 40 and any(character.isdigit() for character in compact) else ""


def parse_ai_answer(data: object) -> AIExtraction:
    """The model's answer checked against the schema. Raises a MALFORMED provider error when it doesn't fit."""
    try:
        return AIExtraction.model_validate(data)
    except ValidationError as exc:
        # Only the count goes into the error: the details would echo conversation content.
        raise ExtractionProviderError(ErrorKind.MALFORMED, f"the answer didn't match the schema ({exc.error_count()} problems)") from exc


def estimate_cost_usd(usage: ProviderUsage, settings: Settings, provider: str = "anthropic") -> float:
    input_rate, output_rate = settings.prices_per_mtok(provider)
    input_price = input_rate / 1_000_000
    output_price = output_rate / 1_000_000
    return (
        usage.input_tokens * input_price
        + usage.cache_write_tokens * input_price * 1.25
        + usage.cache_read_tokens * input_price * 0.1
        + usage.output_tokens * output_price
    )


async def run_extraction(
    provider: ExtractionProvider,
    images: Sequence[PreparedImage],
    settings: Settings,
    *,
    received_on: date | None = None,
) -> ExtractionResponse:
    screenshots = max(image.screenshot for image in images)
    started = time.perf_counter()

    def elapsed_ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    try:
        result = await provider.extract(images)
        ai = parse_ai_answer(result.data)
    except ExtractionProviderError as exc:
        log_event(
            "extraction.failed",
            kind=exc.kind.value,
            detail=exc.detail,
            provider=provider.name,
            screenshots=screenshots,
            images_sent=len(images),
            duration_ms=elapsed_ms(),
        )
        raise

    response = build_extraction_response(ai, screenshots=screenshots, images_sent=len(images), received_on=received_on)
    usage = result.usage
    log_event(
        "extraction.completed",
        provider=provider.name,
        model=usage.model,
        duration_ms=elapsed_ms(),
        provider_ms=result.duration_ms,
        screenshots=screenshots,
        images_sent=len(images),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        estimated_cost_usd=round(estimate_cost_usd(usage, settings, provider.name), 4),
        fallback_used=usage.fallback_used,
        contains_transaction=response.contains_transaction,
        line_items=len(response.draft.line_items),
        review_required=response.review_required,
        review_reasons=len(response.review_reasons),
        request_id=result.request_id,
    )
    return response
