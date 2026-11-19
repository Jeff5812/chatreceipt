"""Build a reviewable proposal for one deal: an editable draft with a state for every field.

The draft uses the same ``TransactionDraft`` the review form and the document renderer already use,
so the backend's calculations and documents work unchanged. Values come only from the rules'
results (``negotiation``, ``payments``); when the rules find competing values the field is left
empty, marked contradictory and the candidates are listed. Nothing is guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.calculations import compute, parse_number, to_plain
from app.intelligence.conversation import Conversation, NoteSeverity, Side
from app.intelligence.money import amounts_in
from app.intelligence.fields import (
    AGREEMENT,
    CONVERSATION,
    INSTRUCTIONS,
    ITEMS_NOT_AGREED,
    LABELS,
    Candidate,
    FieldClass,
    FieldSource,
    FieldState,
    FieldStatus,
    field_class,
)
from app.intelligence.interpretation import AgreementStatus, Deal, Interpretation, Role, Value
from app.intelligence.negotiation import (
    Charge,
    Conflict,
    ItemResolution,
    ItemStatus,
    NegotiationResult,
    agreement_status,
    resolve_negotiation,
)
from app.intelligence.payments import PaymentResolution, resolve_payments
from app.intelligence.review_policy import DEFAULT_POLICY, ReviewPolicy
from app.intelligence.validation import INVALID_VALUE_CODES, ValidationResult, validate
from app.models import AdjustmentDraft, Confidence, LineItemDraft, PaymentStatus, TransactionDraft

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_AGREEMENT_REASONS = {
    AgreementStatus.QUOTATION: "A price was given, but the conversation doesn't show it being accepted, so this may be a quotation rather than an agreement.",
    AgreementStatus.NEGOTIATION: "Prices were exchanged, but the conversation doesn't show a price being accepted.",
    AgreementStatus.ENQUIRY: "The conversation asks about items, but no price was given or agreed.",
    AgreementStatus.AGREEMENT_UNCLEAR: "The agreement was changed, withdrawn or contradicted after it was made. Check what was finally agreed.",
    AgreementStatus.CANCELLED: "The conversation says the deal was called off.",
}


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    deal_key: str
    summary: str
    agreement_status: AgreementStatus
    model_agreement_status: AgreementStatus | None
    draft: TransactionDraft
    # What the conversation supports. Stored, and re-evaluated against the draft after every change.
    base_states: dict[str, FieldState]
    # The base states with the current calculation problems applied. Shown to people.
    field_states: dict[str, FieldState]
    extracted: dict[str, str]  # each field's value as proposed, kept for the correction history
    item_map: dict[str, str]  # draft row id -> item key in the interpretation
    roles: dict[str, str]  # participant key -> role
    negotiation: NegotiationResult
    payment: PaymentResolution
    notes: tuple[str, ...]
    validation: ValidationResult

    @property
    def review_required(self) -> bool:
        return not self.validation.can_approve


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


class _Builder:
    def __init__(self, conversation: Conversation, interpretation: Interpretation, deal: Deal, policy: ReviewPolicy) -> None:
        self.conversation = conversation
        self.interpretation = interpretation
        self.deal = deal
        self.policy = policy
        self.states: dict[str, FieldState] = {}
        self.extracted: dict[str, str] = {}
        self.notes: list[str] = []
        self.item_map: dict[str, str] = {}

    def put(
        self,
        path: str,
        value: str,
        *,
        label: str | None = None,
        confidence: Confidence | None = None,
        evidence: Iterable[str] = (),
        reasons: Iterable[str] = (),
        candidates: Iterable[Candidate] = (),
        grounded: bool | None = None,
        implicit: bool = False,
        conflict: bool = False,
        conditional: bool = False,
        status: FieldStatus | None = None,
        source: FieldSource | None = None,
    ) -> None:
        cls = field_class(path)
        final = status or self.policy.status_for(
            cls,
            has_value=bool(value),
            confidence=confidence,
            grounded=grounded,
            implicit=implicit,
            conflict=conflict,
            conditional=conditional,
        )
        if source is None:
            source = FieldSource.NONE if not value else (FieldSource.DERIVED if implicit else FieldSource.EXTRACTED)
        reasons = [r for r in reasons if r]
        if final == FieldStatus.NEEDS_REVIEW and confidence == Confidence.LOW and not reasons:
            reasons.append("The conversation doesn't state this clearly.")
        self.states[path] = FieldState(
            path=path,
            label=label or LABELS.get(path, path),
            field_class=cls,
            status=final,
            source=source,
            confidence=confidence if (value or conflict) else None,
            evidence=tuple(dict.fromkeys(evidence)),
            reasons=tuple(dict.fromkeys(reasons)),
            candidates=tuple(candidates),
            grounded=grounded,
            blocking=self.policy.is_blocking(cls, final),
        )
        if path not in (AGREEMENT, CONVERSATION, INSTRUCTIONS, ITEMS_NOT_AGREED):
            self.extracted[path] = value

    def escalate(self, path: str, reason: str, *, status: FieldStatus = FieldStatus.NEEDS_REVIEW) -> None:
        """Add a reason to a field and raise it to needs_review (contradictions stay contradictions)."""
        state = self.states.get(path)
        if state is None:
            return
        new_status = state.status
        if state.status in (FieldStatus.CONFIRMED, FieldStatus.INFERRED, FieldStatus.MISSING) and status == FieldStatus.NEEDS_REVIEW:
            new_status = status
        elif status == FieldStatus.CONTRADICTORY:
            new_status = status
        self.states[path] = state.model_copy(
            update={
                "status": new_status,
                "reasons": tuple(dict.fromkeys([*state.reasons, reason])),
                "blocking": self.policy.is_blocking(state.field_class, new_status),
            }
        )

    # -- parties, currency ------------------------------------------------------------------

    def party(self, role: Role) -> str:
        path = "seller" if role == Role.SELLER else "buyer"
        keys = self.interpretation.participants_with(role)
        if not keys:
            self.put(path, "", reasons=[f"It isn't clear from the conversation who the {role.value} is."])
            return ""
        if len(keys) > 1:
            names = [self.conversation.participant(k).label for k in keys if self.conversation.participant(k)]  # type: ignore[union-attr]
            self.put(
                path,
                "",
                conflict=True,
                reasons=[f"More than one participant acts as the {role.value}."],
                candidates=[Candidate(value=name, source="participant") for name in names],
            )
            return ""
        participant = self.conversation.participant(keys[0])
        assignment = next(a for a in self.interpretation.roles if a.participant == keys[0])
        name = participant.name if participant else ""
        reasons = [assignment.note]
        if not name:
            if participant is not None and participant.side == Side.OWNER:
                reasons.append(f"The {role.value} is the phone's owner (“You”), whose name the conversation doesn't show.")
            else:
                reasons.append(f"The {role.value}'s name isn't shown in the conversation.")
        self.put(path, name, confidence=assignment.confidence, evidence=assignment.evidence, reasons=reasons)
        return name

    def currency(self, has_amounts: bool) -> str:
        value = self.deal.currency
        reasons = [value.note]
        if not value.value and has_amounts:
            reasons.append("The conversation doesn't show which currency these amounts are in. Choose it before you continue.")
        self.put("currency", value.value, confidence=value.confidence, evidence=value.evidence, reasons=reasons)
        return value.value

    # -- items ------------------------------------------------------------------------------

    def items(self, negotiation: NegotiationResult) -> list[LineItemDraft]:
        conflicts = _by_subject(negotiation.conflicts)
        active = [r for r in negotiation.items if r.status != ItemStatus.REMOVED]
        for removed in (r for r in negotiation.items if r.status == ItemStatus.REMOVED):
            self.notes.append(f"“{removed.description}” was removed from the order, so it isn't in the record.")
        if any(r.status == ItemStatus.AGREED for r in active):
            included = [r for r in active if r.status in (ItemStatus.AGREED, ItemStatus.UNCLEAR)]
            left_out = [r for r in active if r not in included]
            if left_out:
                described = "; ".join(_left_out_text(r) for r in left_out)
                self.put(
                    ITEMS_NOT_AGREED,
                    "",
                    status=FieldStatus.INFERRED,
                    source=FieldSource.DERIVED,
                    reasons=[f"Discussed but not agreed, so not in the record: {described}. Add any that are part of the deal."],
                    evidence=[m for r in left_out for p in r.history for m in [p.message]],
                )
        else:
            included = active

        rows: list[LineItemDraft] = []
        for number, resolution in enumerate(included, start=1):
            row_id = f"item-{number}"
            self.item_map[row_id] = resolution.item
            base = f"line_items.{row_id}"
            item = self.deal.item(resolution.item)
            description = resolution.description
            if resolution.unit and resolution.unit.casefold() not in description.casefold():
                description = f"{description} (per {resolution.unit})"
            self.put(
                f"{base}.description",
                description,
                label=f"the description of item {number}",
                confidence=Confidence.HIGH if description else None,
                evidence=item.evidence if item else (),
            )
            quantity = self._quantity(base, number, resolution, conflicts)
            price = self._price(base, number, resolution, conflicts)
            rows.append(LineItemDraft(id=row_id, description=description[:200], quantity=quantity, unit_price=price))
        return rows

    def _quantity(self, base: str, number: int, r: ItemResolution, conflicts: dict[str, list[Conflict]]) -> str:
        path = f"{base}.quantity"
        label = f"the quantity of item {number}"
        found = conflicts.get(f"item:{r.item}:quantity", [])
        if found:
            self.put(path, "", label=label, conflict=True, reasons=[c.message for c in found],
                     candidates=[c for conflict in found for c in conflict.candidates], evidence=r.quantity_messages)
            return ""
        if r.quantity is None:
            self.put(path, "", label=label, reasons=["The conversation doesn't say how many."])
            return ""
        value = to_plain(r.quantity)
        reasons = []
        if r.quantity_basis == "implicit":
            reasons.append("No quantity was stated for this item, so the quantity is 1.")
        if r.quantity_grounded is False:
            reasons.append(f"This quantity doesn't appear in message {r.quantity_messages[0]}, where it was taken from. Check it.")
        self.put(
            path,
            value,
            label=label,
            confidence=r.confidence or Confidence.MEDIUM,
            evidence=r.quantity_messages,
            grounded=r.quantity_grounded,
            implicit=r.quantity_basis == "implicit",
            reasons=reasons,
        )
        return value

    def _price(self, base: str, number: int, r: ItemResolution, conflicts: dict[str, list[Conflict]]) -> str:
        path = f"{base}.unit_price"
        label = f"the unit price of item {number}"
        found = conflicts.get(f"item:{r.item}:unit_price", [])
        if found or r.status in (ItemStatus.UNCLEAR, ItemStatus.NEGOTIATING):
            self.put(path, "", label=label, conflict=True, reasons=[c.message for c in found],
                     candidates=[c for conflict in found for c in conflict.candidates],
                     evidence=[m for conflict in found for m in conflict.messages])
            return ""
        if r.unit_price is None:
            self.put(path, "", label=label, reasons=["No price was given for this item."])
            return ""
        value = to_plain(r.unit_price)
        reasons: list[str] = []
        status: FieldStatus | None = None
        if r.status == ItemStatus.OFFERED:
            reasons.append(f"This price was offered in message {r.price_messages[0]}, but the conversation doesn't show it being accepted.")
            status = FieldStatus.NEEDS_REVIEW
        if r.price_grounded is False:
            reasons.append(f"This price doesn't appear in message {r.price_messages[0]}, where it was taken from. Check it.")
        if r.condition:
            reasons.append(f"The agreed price depends on a condition: {r.condition}.")
        if r.bundle:
            reasons.append("One price covers several units, so the record shows them as one line.")
        self.put(
            path,
            value,
            label=label,
            confidence=r.confidence,
            evidence=r.price_messages,
            grounded=r.price_grounded,
            conditional=bool(r.condition),
            reasons=reasons,
            status=status,
        )
        return value

    # -- charges -----------------------------------------------------------------------------

    def delivery(self, negotiation: NegotiationResult) -> str:
        conflicts = _by_subject(negotiation.conflicts).get("delivery_fee", [])
        charge = negotiation.delivery_fee
        reading = self.deal.agreed_delivery_fee
        reading_value = _number(reading)
        if conflicts:
            self.put("delivery_fee", "", conflict=True, reasons=[c.message for c in conflicts],
                     candidates=[c for conflict in conflicts for c in conflict.candidates],
                     evidence=[m for conflict in conflicts for m in conflict.messages])
            return ""
        if charge is None:
            reasons = [note for note in negotiation.notes if "delivery fee" in note]
            if reading_value is not None:
                self.put("delivery_fee", "", conflict=True,
                         reasons=["The analysis mentions a delivery fee, but no message states one. Check it."],
                         candidates=[Candidate(value=to_plain(reading_value), source="model_reading", note=reading.note)])
                return ""
            self.put("delivery_fee", "", reasons=reasons)
            return ""
        value = to_plain(charge.value)
        if reading_value is not None and reading_value != charge.value:
            self.put("delivery_fee", "", conflict=True,
                     reasons=["The delivery fee in the conversation and the analysis's reading of it disagree."],
                     candidates=[Candidate(value=value, source=charge.basis, messages=charge.messages),
                                 Candidate(value=to_plain(reading_value), source="model_reading", note=reading.note)],
                     evidence=charge.messages)
            return ""
        reasons = []
        if charge.grounded is False:
            reasons.append(f"This fee doesn't appear in message {charge.messages[0]}, where it was taken from. Check it.")
        self.put("delivery_fee", value, confidence=charge.confidence, evidence=charge.messages, grounded=charge.grounded, reasons=reasons)
        return value

    def adjustments(self, charges: tuple[Charge, ...], group: str, noun: str, conflicts: list[Conflict]) -> list[AdjustmentDraft]:
        rows: list[AdjustmentDraft] = []
        conflicted_messages = {m for conflict in conflicts for m in conflict.messages}
        for number, charge in enumerate(charges, start=1):
            row_id = f"{noun}-{number}"
            path = f"{group}.{row_id}.value"
            label = f"the {noun}" + (f" “{charge.label}”" if charge.label else "")
            if set(charge.messages) & conflicted_messages:
                self.put(path, "", label=label, conflict=True, reasons=[c.message for c in conflicts],
                         candidates=[c for conflict in conflicts for c in conflict.candidates])
                value = ""
            else:
                value = to_plain(charge.value)
                reasons = []
                if charge.grounded is False:
                    reasons.append(f"This amount doesn't appear in message {charge.messages[0]}, where it was taken from. Check it.")
                self.put(path, value, label=label, confidence=charge.confidence, evidence=charge.messages,
                         grounded=charge.grounded, reasons=reasons)
            self.extracted[f"{group}.{row_id}.label"] = charge.label
            self.extracted[f"{group}.{row_id}.kind"] = charge.kind
            rows.append(AdjustmentDraft(id=row_id, label=charge.label[:120], kind=charge.kind, value=value))
        return rows

    # -- payment -----------------------------------------------------------------------------

    def payment(self, payment: PaymentResolution) -> tuple[PaymentStatus, str]:
        conflicts = _by_subject(payment.conflicts)
        reading_status = self.deal.agreed_payment_status.value
        status_conflicts = list(conflicts.get("payment_status", []))
        # With no payment messages at all, the analysis calling the deal "unpaid" and the rules'
        # "not specified" both mean nothing has been paid. The record keeps "not specified", which
        # is what the chat says; any other disagreement is still a conflict for a person.
        silent = not payment.payments and not payment.promises and not payment.refund_messages
        same_meaning = silent and payment.status == PaymentStatus.NOT_SPECIFIED and reading_status == PaymentStatus.UNPAID.value
        if reading_status in PaymentStatus._value2member_map_ and reading_status != payment.status.value and not same_meaning:
            status_conflicts.append(
                Conflict(
                    code="reading_differs",
                    subject="payment_status",
                    message="The payment messages and the analysis's reading of the payment status disagree.",
                    candidates=(
                        Candidate(value=payment.status.value, source="payment_messages"),
                        Candidate(value=reading_status, source="model_reading", note=self.deal.agreed_payment_status.note),
                    ),
                )
            )
        evidence = [m for p in payment.payments for m in p.messages] + list(payment.promises) + list(payment.refund_messages)
        status_value = payment.status
        if status_conflicts:
            status_value = PaymentStatus.NOT_SPECIFIED
            self.put("payment_status", status_value.value, conflict=True, reasons=[c.message for c in status_conflicts],
                     candidates=[c for conflict in status_conflicts for c in conflict.candidates], evidence=evidence)
        else:
            discussed = payment.status != PaymentStatus.NOT_SPECIFIED
            self.put(
                "payment_status",
                status_value.value if discussed else "",
                confidence=payment.confidence or (Confidence.HIGH if payment.promises else None),
                evidence=evidence,
                reasons=[payment.summary],
                grounded=payment.grounded,
            )

        amount_conflicts = conflicts.get("amount_paid", [])
        reading_amount = _number(self.deal.agreed_amount_paid)
        amount = payment.amount_paid
        if amount_conflicts:
            self.put("amount_paid", "", conflict=True, reasons=[c.message for c in amount_conflicts],
                     candidates=[c for conflict in amount_conflicts for c in conflict.candidates], evidence=evidence)
            return status_value, ""
        if amount is not None and reading_amount is not None and amount != reading_amount:
            self.put("amount_paid", "", conflict=True,
                     reasons=["The payment messages and the analysis's reading of the amount paid disagree."],
                     candidates=[Candidate(value=to_plain(amount), source="payment_messages"),
                                 Candidate(value=to_plain(reading_amount), source="model_reading")],
                     evidence=evidence)
            return status_value, ""
        if amount is None:
            reasons = []
            if status_value == PaymentStatus.PAID:
                reasons.append("The conversation says the full amount was paid, so the amount paid is the total.")
            self.put("amount_paid", "", reasons=reasons, evidence=evidence)
            return status_value, ""
        value = to_plain(amount)
        reasons = []
        if payment.grounded is False:
            reasons.append("A payment amount doesn't appear in the message it was taken from. Check it.")
        self.put("amount_paid", value, confidence=payment.confidence, evidence=evidence, grounded=payment.grounded, reasons=reasons)
        return status_value, value

    # -- dates and terms ----------------------------------------------------------------------

    def date(self, name: str, fallback_message: str | None = None) -> str:
        value = self.deal.terms.get(name, Value())
        raw = value.value
        if raw:
            valid = bool(_ISO_DAY.match(raw))
            if valid:
                try:
                    date.fromisoformat(raw)
                except ValueError:
                    valid = False
            if not valid:
                self.put(name, "", status=FieldStatus.INVALID, reasons=[f"{_sentence(LABELS[name])} in the conversation couldn't be read as a calendar date."])
                return ""
            self.put(name, raw, confidence=value.confidence, evidence=value.evidence, reasons=[value.note])
            return raw
        if name == "transaction_date" and fallback_message:
            message = self.conversation.message(fallback_message)
            if message is not None and _ISO_DAY.match(message.day):
                self.put(name, message.day, confidence=Confidence.MEDIUM, evidence=[message.key], implicit=True,
                         reasons=[f"Taken from the date of message {message.key}, where the deal was agreed."])
                return message.day
        self.put(name, "", reasons=[value.note])
        return ""

    def text(self, name: str, limit: int) -> str:
        value = self.deal.terms.get(name, Value())
        text = value.value.strip()[:limit]
        self.put(name, text, confidence=value.confidence, evidence=value.evidence, reasons=[value.note])
        return text

    def stated_total(self) -> str:
        value = self.deal.agreed_stated_total
        number = _number(value)
        if number is None:
            self.put("stated_total", "", reasons=[value.note])
            return ""
        text = to_plain(number)
        evidence = value.evidence
        grounded = None
        if evidence:
            grounded = any(
                (message := self.conversation.message(key)) is not None and number.normalize() in amounts_in(message.text)
                for key in evidence
            )
        self.put("stated_total", text, confidence=value.confidence, evidence=evidence, grounded=grounded, reasons=[value.note])
        return text


def _number(value: Value) -> Decimal | None:
    try:
        number = parse_number(value.value)
    except ValueError:
        return None
    return number if number is not None and number >= 0 else None


def _by_subject(conflicts: Iterable[Conflict]) -> dict[str, list[Conflict]]:
    grouped: dict[str, list[Conflict]] = {}
    for conflict in conflicts:
        grouped.setdefault(conflict.subject, []).append(conflict)
    return grouped


def _left_out_text(resolution: ItemResolution) -> str:
    offered = [p for p in resolution.history if p.unit_price or p.amount]
    if offered:
        price = offered[-1].unit_price or offered[-1].amount
        return f"“{resolution.description}” (offered at {price})"
    return f"“{resolution.description}”"


def _map_ambiguity(field: str, messages: tuple[str, ...], builder: _Builder) -> list[str]:
    direct = {
        "currency": "currency", "seller": "seller", "buyer": "buyer", "delivery_fee": "delivery_fee",
        "stated_total": "stated_total", "amount_paid": "amount_paid", "payment_status": "payment_status",
        "transaction_date": "transaction_date", "due_date": "due_date", "agreement": AGREEMENT,
        "screenshots": CONVERSATION, "other": CONVERSATION,
    }
    if field in direct:
        return [direct[field]]
    if field in ("items", "quantity", "unit_price"):
        aspects = ("quantity",) if field == "quantity" else ("unit_price",) if field == "unit_price" else ("quantity", "unit_price")
        matches = [
            path
            for path, state in builder.states.items()
            if path.startswith("line_items.") and path.rsplit(".", 1)[-1] in aspects and set(state.evidence) & set(messages)
        ]
        return matches or [AGREEMENT]
    if field in ("discounts", "additional_fees"):
        matches = [path for path in builder.states if path.startswith(f"{field}.")]
        return matches or [AGREEMENT]
    return [CONVERSATION]


def build_proposal(
    conversation: Conversation,
    interpretation: Interpretation,
    deal: Deal,
    *,
    policy: ReviewPolicy = DEFAULT_POLICY,
) -> Proposal:
    b = _Builder(conversation, interpretation, deal, policy)
    negotiation = resolve_negotiation(deal, interpretation, conversation)
    b.notes.extend(note for note in negotiation.notes if "delivery fee" not in note)

    seller = b.party(Role.SELLER)
    buyer = b.party(Role.BUYER)
    items = b.items(negotiation)
    grouped = _by_subject(negotiation.conflicts)
    discounts = b.adjustments(negotiation.discounts, "discounts", "discount", grouped.get("discounts", []))
    fees = b.adjustments(negotiation.additional_fees, "additional_fees", "fee", grouped.get("additional_fees", []))
    delivery = b.delivery(negotiation)
    has_amounts = any(row.unit_price for row in items) or bool(delivery) or any(row.value for row in (*discounts, *fees))
    currency = b.currency(has_amounts)

    agreed_items = [r for r in negotiation.items if r.status == ItemStatus.AGREED]
    acceptance_message = agreed_items[0].price_messages[-1] if agreed_items and agreed_items[0].price_messages else None
    draft = TransactionDraft(
        currency=currency,
        seller=seller[:120],
        buyer=buyer[:120],
        transaction_date=b.date("transaction_date", acceptance_message),
        due_date=b.date("due_date"),
        line_items=items,
        discounts=discounts,
        additional_fees=fees,
        delivery_fee=delivery,
        payment_terms=b.text("payment_terms", 1000),
        delivery_terms=b.text("delivery_terms", 1000),
        notes=b.text("notes", 2000),
        stated_total=b.stated_total(),
    )

    # Payment depends on the total the backend calculates from the agreed items and charges.
    total = compute(draft).total
    payment = resolve_payments(deal, interpretation, conversation, total=total)
    status, amount_paid = b.payment(payment)
    draft = draft.model_copy(update={"payment_status": status, "amount_paid": amount_paid})

    resolved = agreement_status(
        negotiation, fully_paid_and_confirmed=payment.status == PaymentStatus.PAID and payment.confirmed_by_payee
    )
    if resolved in (AgreementStatus.AGREEMENT_REACHED, AgreementStatus.COMPLETED):
        model = deal.agreement_status
        if model is not None and model not in (AgreementStatus.AGREEMENT_REACHED, AgreementStatus.COMPLETED):
            b.put(AGREEMENT, "", status=FieldStatus.NEEDS_REVIEW, source=FieldSource.DERIVED,
                  reasons=[f"ChatReceipt's rules found an agreement, but the analysis reads the deal as “{model.value.replace('_', ' ')}”. Check that the price was accepted."])
        else:
            b.put(AGREEMENT, "", status=FieldStatus.CONFIRMED, source=FieldSource.DERIVED)
    else:
        b.put(AGREEMENT, "", status=FieldStatus.NEEDS_REVIEW, source=FieldSource.DERIVED, reasons=[_AGREEMENT_REASONS[resolved]])

    review_notes = [note.message for note in conversation.notes if note.severity == NoteSeverity.REVIEW]
    if review_notes or interpretation.issues:
        b.put(CONVERSATION, "", status=FieldStatus.NEEDS_REVIEW, source=FieldSource.DERIVED,
              reasons=[*review_notes, *interpretation.issues],
              evidence=[m for note in conversation.notes for m in note.messages])
    b.notes.extend(note.message for note in conversation.notes if note.severity == NoteSeverity.INFO)
    if interpretation.instruction_like_messages:
        keys = ", ".join(interpretation.instruction_like_messages)
        b.put(INSTRUCTIONS, "", status=FieldStatus.NEEDS_REVIEW, source=FieldSource.DERIVED,
              evidence=interpretation.instruction_like_messages,
              reasons=[f"Message {keys} contains text addressed to an AI or trying to change the record. It was treated as part of the conversation and ignored. Check the record doesn't depend on it."])

    for ambiguity in (*deal.ambiguities, *interpretation.ambiguities):
        for path in _map_ambiguity(ambiguity.field, ambiguity.messages, b):
            if path in b.states:
                b.escalate(path, ambiguity.description)
            elif path in (CONVERSATION, AGREEMENT):
                b.put(path, "", status=FieldStatus.NEEDS_REVIEW, source=FieldSource.DERIVED, reasons=[ambiguity.description],
                      evidence=ambiguity.messages)

    base_states = dict(b.states)
    states, validation = evaluate(draft, base_states, policy)
    return Proposal(
        deal_key=deal.key,
        summary=deal.summary,
        agreement_status=resolved,
        model_agreement_status=deal.agreement_status,
        draft=draft,
        base_states=base_states,
        field_states=states,
        extracted=b.extracted,
        item_map=b.item_map,
        roles={assignment.participant: assignment.role.value for assignment in interpretation.roles},
        negotiation=negotiation,
        payment=payment,
        notes=tuple(dict.fromkeys(b.notes)),
        validation=validation,
    )


def evaluate(
    draft: TransactionDraft, base_states: dict[str, FieldState], policy: ReviewPolicy = DEFAULT_POLICY
) -> tuple[dict[str, FieldState], ValidationResult]:
    """The draft's current problems, applied to what the conversation supports.

    Problems are worked out afresh each time, so fixing one field clears a problem it caused on another.
    """
    states = apply_validation(base_states, validate(draft, base_states), policy)
    return states, validate(draft, states)


def apply_validation(
    states: dict[str, FieldState], validation: ValidationResult, policy: ReviewPolicy = DEFAULT_POLICY
) -> dict[str, FieldState]:
    """Mark fields with calculation errors as blocking, and unusable values as invalid."""
    updated = dict(states)
    for issue in validation.calculation.issues:
        if issue.severity != "error" or not issue.field:
            continue
        state = updated.get(issue.field)
        if state is None:
            state = FieldState(
                path=issue.field, label=LABELS.get(issue.field, issue.field), field_class=field_class(issue.field),
                status=FieldStatus.MISSING, source=FieldSource.NONE,
            )
        status = FieldStatus.INVALID if issue.code in INVALID_VALUE_CODES else state.status
        updated[issue.field] = state.model_copy(
            update={"status": status, "blocking": True, "reasons": tuple(dict.fromkeys([*state.reasons, issue.message]))}
        )
    for issue in validation.calculation.issues:
        if issue.code == "stated_total_mismatch" and "stated_total" in updated and not updated["stated_total"].resolved:
            state = updated["stated_total"]
            updated["stated_total"] = state.model_copy(
                update={
                    "status": FieldStatus.CONTRADICTORY,
                    "blocking": policy.is_blocking(FieldClass.FINANCIAL, FieldStatus.CONTRADICTORY),
                    "reasons": tuple(dict.fromkeys([*state.reasons, issue.message])),
                }
            )
    return updated


def build_proposals(
    conversation: Conversation, interpretation: Interpretation, *, policy: ReviewPolicy = DEFAULT_POLICY
) -> list[Proposal]:
    return [build_proposal(conversation, interpretation, deal, policy=policy) for deal in interpretation.deals]
