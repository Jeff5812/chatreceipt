"""Keeping a draft's figures consistent after someone corrects them, without guessing.

ChatReceipt's calculation stays the only arithmetic. From it, this module works out two things:

- the payment status the amounts imply (``settle_status``), used only when the person hasn't
  chosen the status themselves; and
- when a balance or total someone stated disagrees with the calculation, the one change that
  would make them agree (``proposal``). It's offered as a yes-or-no question and never applied
  silently, because either figure could be the wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.calculations import compute, parse_number, to_plain
from app.currencies import Currency, format_money
from app.models import LineItemDraft, PaymentStatus, TransactionDraft

CONFLICT_CODES = frozenset({"stated_total_mismatch", "stated_outstanding_mismatch"})
KEEP_LABEL = "No, keep as it is"


def _number(raw: str) -> Decimal | None:
    try:
        return parse_number(raw)
    except ValueError:
        return None


def short_money(value: Decimal, currency: Currency) -> str:
    """₦60,000 rather than ₦60,000.00, for button titles that allow only 20 characters."""
    text = format_money(value, currency)
    return text[:-3] if currency.minor_units == 2 and text.endswith(".00") else text


def single_simple_item(draft: TransactionDraft) -> LineItemDraft | None:
    """The only item, when its price is the whole total: one unit, and nothing else charged."""
    if len(draft.line_items) != 1 or draft.discounts or draft.additional_fees or draft.delivery_fee.strip():
        return None
    item = draft.line_items[0]
    return item if _number(item.quantity) in (None, Decimal(1)) else None


def settle_status(draft: TransactionDraft) -> TransactionDraft:
    """The draft with the payment status its amounts imply. Refunds and unknown amounts are left alone."""
    if draft.payment_status == PaymentStatus.REFUNDED:
        return draft
    paid = _number(draft.amount_paid)
    if paid is None:
        return draft
    total = compute(draft).total
    status = draft.payment_status
    if total is None:
        owed = _number(draft.stated_outstanding)
        if paid > 0 and owed is not None and owed > 0:
            status = PaymentStatus.PARTIALLY_PAID
        elif paid > 0 and status == PaymentStatus.UNPAID:
            status = PaymentStatus.NOT_SPECIFIED
    elif paid == 0:
        status = PaymentStatus.UNPAID
    elif paid < total:
        status = PaymentStatus.PARTIALLY_PAID
    elif paid == total:
        status = PaymentStatus.PAID
    return draft if status == draft.payment_status else draft.model_copy(update={"payment_status": status})


@dataclass(frozen=True)
class Proposal:
    """A disagreement between a stated figure and the calculation, and how to settle it."""

    question: str
    keep_label: str
    # The stated figure that answering "keep" removes, accepting the calculation as right.
    keep_clears: str
    # Applied when the person accepts: draft field or item path -> new value. Empty when no single
    # change is safe to offer, so the person says what's right instead.
    changes: dict[str, str] = field(default_factory=dict)
    accept_label: str = ""


def proposal(draft: TransactionDraft) -> Proposal | None:
    computation = compute(draft)
    currency = computation.currency
    codes = {issue.code for issue in computation.issues}
    owed = _number(draft.stated_outstanding)
    # A balance someone stated while nothing says what was paid can't be checked, and would be lost.
    unchecked = owed is not None and computation.outstanding is None and computation.total is not None
    if currency is None or computation.total is None or not (codes & CONFLICT_CODES or unchecked):
        return None
    total, paid = computation.total, computation.amount_paid
    item = single_simple_item(draft)
    keep = KEEP_LABEL

    stated_total = _number(draft.stated_total)
    if "stated_total_mismatch" in codes and stated_total is not None:
        if item is not None:
            return Proposal(
                question=f"The chat mentions a total of *{format_money(stated_total, currency)}*. Was that the full price?",
                accept_label=f"Yes, {short_money(stated_total, currency)}",
                keep_label=keep,
                keep_clears="stated_total",
                changes=_price_changes(item, stated_total),
            )
        return Proposal(
            question=(
                f"The chat mentions a total of *{format_money(stated_total, currency)}*, but the items add up to "
                f"*{format_money(total, currency)}*. Which is right? If the items are wrong, just tell me what to change."
            ),
            keep_label=keep,
            keep_clears="stated_total",
        )

    outstanding = computation.outstanding
    if owed is None or ("stated_outstanding_mismatch" not in codes and not unchecked):
        return None
    price = computation.unit_prices.get(item.id) if item is not None else None
    if item is not None and paid is not None and (price is None or price == paid or paid == 0):
        # The usual slip: a payment was read as the price. Paid + still owed is the full price.
        full = paid + owed
        return Proposal(
            question=(
                f"So the full price was *{format_money(full, currency)}* "
                f"({format_money(paid, currency)} paid + {format_money(owed, currency)} still owed)?"
            ),
            accept_label=f"Yes, {short_money(full, currency)}",
            keep_label=keep,
            keep_clears="stated_outstanding",
            changes=_price_changes(item, full),
        )
    if not draft.amount_paid.strip() and draft.payment_status != PaymentStatus.PAID and owed <= total:
        paid_so_far = total - owed
        return Proposal(
            question=(
                f"So *{format_money(paid_so_far, currency)}* has been paid, "
                f"with {format_money(owed, currency)} still owed?"
            ),
            accept_label=f"Yes, {short_money(paid_so_far, currency)} paid",
            keep_label=keep,
            keep_clears="stated_outstanding",
            changes={"amount_paid": to_plain(paid_so_far)},
        )
    left = f"*{format_money(outstanding, currency)}*" if outstanding is not None else "a different amount"
    return Proposal(
        question=(
            f"You mentioned *{format_money(owed, currency)}* still owed, but these figures leave {left}. "
            "Which is right? If a figure is wrong, just tell me, like “he paid 20k”."
        ),
        keep_label=keep,
        keep_clears="stated_outstanding",
    )


def _price_changes(item: LineItemDraft, price: Decimal) -> dict[str, str]:
    return {f"line_items.{item.id}.unit_price": to_plain(price), f"line_items.{item.id}.quantity": "1"}


def accept(draft: TransactionDraft, fix: Proposal) -> TransactionDraft:
    """Apply a proposal's changes. Paths are ones ``proposal`` produced from this same draft."""
    updates: dict[str, object] = {}
    items = list(draft.line_items)
    for path, value in fix.changes.items():
        parts = path.split(".")
        if len(parts) == 3 and parts[0] == "line_items":
            items = [row.model_copy(update={parts[2]: value}) if row.id == parts[1] else row for row in items]
        else:
            updates[path] = value
    return draft.model_copy(update={**updates, "line_items": items})
