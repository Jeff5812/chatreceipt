"""What the conversation says about payment, worked out by ChatReceipt's own rules.

Promises, requests, claims and confirmations are different things:

- "I'll pay tomorrow" (payment_intention) and "send your account details" (payment_request) mean
  payment is expected: unpaid.
- "I've sent it" (payment_claim) is the payer's claim. "Received, thanks" (payment_confirmation)
  is the payee acknowledging it. Both are statements in the conversation; ChatReceipt records
  which ones exist and never claims to have verified a payment.
- A payee saying the money hasn't arrived (payment_dispute) makes the payment contradictory
  until a later confirmation settles it.
- Whether a payment is full or partial is decided by comparing the amounts stated with the total
  the backend calculates, when both are known. A payment stated as "the full amount" without a
  figure is recorded as paid with no amount, so the backend uses the calculated total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.calculations import to_plain
from app.intelligence.conversation import Conversation
from app.intelligence.fields import Candidate
from app.intelligence.interpretation import Deal, Event, EventType, Interpretation, Role
from app.intelligence.money import mentions_amount
from app.intelligence.negotiation import Conflict, weakest
from app.models import Confidence, PaymentStatus

Evidence = Literal["claimed_by_payer", "confirmed_by_payee", "claimed_and_confirmed"]


class Payment(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[str, ...]
    amount: Decimal | None
    covers: Literal["full", "part", "unknown"]
    evidence: Evidence
    disputed: bool = False
    grounded: bool | None = None


class PaymentResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: PaymentStatus
    amount_paid: Decimal | None  # None when no figure is stated (or only "in full")
    payments: tuple[Payment, ...] = ()
    promises: tuple[str, ...] = ()  # message keys of payment intentions and requests
    refund_messages: tuple[str, ...] = ()
    confidence: Confidence | None = None
    confirmed_by_payee: bool = False
    grounded: bool | None = None
    conflicts: tuple[Conflict, ...] = ()
    summary: str = ""  # one sentence about what the conversation says, for the record's reviewer


@dataclass
class _Draft:
    messages: list[str]
    amount: Decimal | None
    covers: str
    claimed: bool = False
    confirmed: bool = False
    disputed: bool = False
    confidences: list[Confidence] = field(default_factory=list)
    grounded: list[bool] = field(default_factory=list)


def resolve_payments(
    deal: Deal,
    interpretation: Interpretation,
    conversation: Conversation,
    *,
    total: Decimal | None,
) -> PaymentResolution:
    payees = set(interpretation.participants_with(Role.SELLER))
    drafts: list[_Draft] = []
    by_message: dict[str, _Draft] = {}
    promises: list[str] = []
    refunds: list[tuple[str, Decimal | None, str | None]] = []
    conflicts: list[Conflict] = []
    unprompted: list[Event] = []  # payment confirmations with no claim before them

    def grounded(message_key: str, amount: Decimal | None) -> bool | None:
        if amount is None:
            return None
        message = conversation.message(message_key)
        return message is not None and mentions_amount(f"{message.text}\n{message.quoted}", amount)

    for event in deal.events:
        kind = event.type
        if kind in (EventType.PAYMENT_INTENTION, EventType.PAYMENT_REQUEST):
            promises.append(event.message)
        elif kind == EventType.PAYMENT_CLAIM:
            by_payee = event.actor in payees
            draft = _Draft([event.message], event.amount, event.covers or "unknown", claimed=not by_payee, confirmed=by_payee)
            draft.confidences.append(event.confidence)
            if (g := grounded(event.message, event.amount)) is not None:
                draft.grounded.append(g)
            drafts.append(draft)
            by_message[event.message] = draft
        elif kind == EventType.PAYMENT_CONFIRMATION:
            target = by_message.get(event.responds_to or "")
            if target is None:
                open_claims = [d for d in drafts if d.claimed and not d.confirmed]
                target = open_claims[-1] if open_claims else None
            if target is None:
                unprompted.append(event)
                target = _Draft([], event.amount, event.covers or "unknown")
                drafts.append(target)
            target.messages.append(event.message)
            target.confirmed = True
            target.disputed = False  # a later confirmation settles an earlier dispute
            target.confidences.append(event.confidence)
            if (g := grounded(event.message, event.amount)) is not None:
                target.grounded.append(g)
            if event.amount is not None:
                if target.amount is not None and target.amount != event.amount:
                    conflicts.append(
                        Conflict(
                            code="payment_amount_mismatch",
                            subject="amount_paid",
                            message="The amount the payer says they paid and the amount the payee confirms are different.",
                            messages=tuple(target.messages),
                            candidates=(
                                Candidate(value=to_plain(target.amount), source="payment_claim", messages=(target.messages[0],)),
                                Candidate(value=to_plain(event.amount), source="payment_confirmation", messages=(event.message,)),
                            ),
                        )
                    )
                elif target.amount is None:
                    target.amount = event.amount
            if event.covers and target.covers == "unknown":
                target.covers = event.covers
        elif kind == EventType.PAYMENT_DISPUTE:
            target = by_message.get(event.responds_to or "") or (drafts[-1] if drafts else None)
            if target is not None:
                target.disputed = True
                target.messages.append(event.message)
        elif kind == EventType.REFUND:
            refunds.append((event.message, event.amount, event.covers))

    # "Thanks, received your payment" before any price has been given can't be about this deal's
    # payment in the order shown: the messages are probably out of order. (A chat that starts after
    # the price, with only the seller's "received", is not affected.)
    priced = [
        e.seq for e in deal.events
        if e.type in (EventType.OFFER, EventType.COUNTER_OFFER, EventType.REQUEST, EventType.ACCEPTANCE)
        and (e.unit_price is not None or e.amount is not None)
    ]
    first_price = min(priced, default=None)
    for event in unprompted:
        if first_price is not None and event.seq < first_price:
            conflicts.append(
                Conflict(
                    code="payment_before_price",
                    subject="payment_status",
                    message=f"Message {event.message} confirms a payment before any price appears in the conversation. "
                    "The messages may be out of order: check the payment.",
                    messages=(event.message,),
                )
            )

    payments = [
        Payment(
            messages=tuple(dict.fromkeys(d.messages)),
            amount=d.amount,
            covers=d.covers if d.covers in ("full", "part") else "unknown",  # type: ignore[arg-type]
            evidence="claimed_and_confirmed" if d.claimed and d.confirmed else ("confirmed_by_payee" if d.confirmed else "claimed_by_payer"),
            disputed=d.disputed,
            grounded=all(d.grounded) if d.grounded else None,
        )
        for d in drafts
    ]
    for payment in payments:
        if payment.disputed:
            conflicts.append(
                Conflict(
                    code="payment_disputed",
                    subject="payment_status",
                    message="A payment is claimed, but the other side says it hasn't arrived or disputes it.",
                    messages=payment.messages,
                    candidates=(
                        Candidate(value=PaymentStatus.PAID.value, source="payment_claim", messages=payment.messages[:1]),
                        Candidate(value=PaymentStatus.UNPAID.value, source="payment_dispute", messages=payment.messages[-1:]),
                    ),
                )
            )

    valid = [p for p in payments if not p.disputed]
    confidence = weakest(*(c for d in drafts for c in d.confidences)) if drafts else None
    grounded_all = [p.grounded for p in valid if p.grounded is not None]
    status: PaymentStatus
    amount_paid: Decimal | None = None

    if refunds:
        refund_amount = sum((amount for _, amount, _ in refunds if amount is not None), Decimal(0))
        paid_amount = sum((p.amount for p in valid if p.amount is not None), Decimal(0))
        full_refund = any(covers == "full" for _, _, covers in refunds) or (paid_amount > 0 and refund_amount >= paid_amount)
        if full_refund:
            status = PaymentStatus.REFUNDED
        else:
            status = PaymentStatus.PARTIALLY_PAID if valid else PaymentStatus.NOT_SPECIFIED
            conflicts.append(
                Conflict(
                    code="partial_refund",
                    subject="amount_paid",
                    message="Part of the payment was refunded. Check the amount paid and the payment status.",
                    messages=tuple(message for message, _, _ in refunds),
                )
            )
    elif valid:
        known = [p.amount for p in valid if p.amount is not None]
        all_known = len(known) == len(valid)
        any_full = any(p.covers == "full" for p in valid)
        paid_sum = sum(known, Decimal(0))
        if all_known:
            amount_paid = paid_sum
            if total is not None:
                status = PaymentStatus.PAID if paid_sum >= total else PaymentStatus.PARTIALLY_PAID
            else:
                status = PaymentStatus.PAID if any_full else PaymentStatus.PARTIALLY_PAID
        elif any_full:
            status = PaymentStatus.PAID  # "paid in full" with no figure: the backend uses the total
            amount_paid = None
        else:
            status = PaymentStatus.PARTIALLY_PAID  # how much is unknown: the review form asks for it
            amount_paid = paid_sum if known else None
            if known:
                conflicts.append(
                    Conflict(
                        code="payment_amount_unknown",
                        subject="amount_paid",
                        message="One of the payments in the conversation has no amount. Check the amount paid.",
                        messages=tuple(m for p in valid for m in p.messages),
                    )
                )
    elif payments:
        status = PaymentStatus.NOT_SPECIFIED  # every payment is disputed: the conflict says so
    elif promises:
        status = PaymentStatus.UNPAID
    else:
        status = PaymentStatus.NOT_SPECIFIED

    return PaymentResolution(
        status=status,
        amount_paid=amount_paid,
        payments=tuple(payments),
        promises=tuple(promises),
        refund_messages=tuple(message for message, _, _ in refunds),
        confidence=confidence,
        confirmed_by_payee=any(p.evidence != "claimed_by_payer" for p in valid),
        grounded=all(grounded_all) if grounded_all else None,
        conflicts=tuple(conflicts),
        summary=_summary(status, valid, promises),
    )


def _summary(status: PaymentStatus, payments: list[Payment], promises: list[str]) -> str:
    if status == PaymentStatus.REFUNDED:
        return "The conversation says the payment was refunded."
    if payments:
        claimed = any(p.evidence in ("claimed_by_payer", "claimed_and_confirmed") for p in payments)
        confirmed = any(p.evidence in ("confirmed_by_payee", "claimed_and_confirmed") for p in payments)
        if claimed and confirmed:
            text = "The buyer says they paid and the seller confirms receiving it."
        elif confirmed:
            text = "The seller says they received payment."
        else:
            text = "The buyer says they paid; the seller doesn't confirm it in the conversation."
        return text + " This is what the conversation says; ChatReceipt doesn't verify payments."
    if promises:
        return "Payment is promised or requested, but the conversation doesn't report any payment."
    return ""
