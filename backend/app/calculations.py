"""Deterministic transaction arithmetic. This is the only place totals are computed.

Rules:
- Line amount = quantity × unit price, rounded half-up to the currency's precision.
- A percentage discount or fee applies to the items subtotal, rounded the same way.
- Total = items subtotal − discounts + additional fees + delivery fee.
- Amount paid is the amount entered. If none is entered, "paid" means the full total and
  "unpaid" means nothing; any other status leaves it unknown.
- Outstanding = total − amount paid, when both are known.
- Entered values are never rounded or adjusted. An amount with more decimal places than the
  currency allows is reported as an error rather than silently fixed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.currencies import Currency, format_money, get_currency
from app.models import (
    AdjustmentDraft,
    AmountLine,
    Calculation,
    Issue,
    PaymentStatus,
    TransactionDraft,
)

MAX_AMOUNT = Decimal("1000000000000")  # exclusive
MAX_QUANTITY = Decimal("1000000")  # exclusive
QUANTITY_PLACES = 3
PERCENT_PLACES = 2
EARLIEST_DATE = date(1990, 1, 1)
LATEST_DATE = date(2100, 12, 31)

_PLAIN_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_GROUPED_NUMBER = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Warnings that mean the extracted figures need a second look (used for review flags).
REVIEW_WARNING_CODES = frozenset({"possible_duplicate", "stated_total_mismatch", "stated_outstanding_mismatch", "status_not_set"})


def parse_number(raw: str) -> Decimal | None:
    """Parse '17000', '17,000.50' or '-3'. Blank gives None; anything else raises ValueError."""
    text = raw.strip()
    if not text:
        return None
    if _GROUPED_NUMBER.match(text):
        text = text.replace(",", "")
    elif not _PLAIN_NUMBER.match(text):
        raise ValueError("not a number")
    return Decimal(text)


def decimal_places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def to_plain(value: Decimal) -> str:
    """'17000', '2500.5': a canonical string without exponent notation or trailing zeros."""
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


@dataclass
class Computation:
    """Parsed values and totals, as Decimals. ``None`` means unknown."""

    currency: Currency | None
    transaction_date: date | None
    due_date: date | None
    quantities: dict[str, Decimal | None]
    unit_prices: dict[str, Decimal | None]
    line_amounts: dict[str, Decimal | None]
    items_subtotal: Decimal | None
    discount_amounts: dict[str, Decimal | None]
    discount_total: Decimal | None
    fee_amounts: dict[str, Decimal | None]
    fee_total: Decimal | None
    delivery_fee: Decimal | None
    total: Decimal | None
    amount_paid: Decimal | None
    outstanding: Decimal | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    @property
    def can_export(self) -> bool:
        return self.total is not None and not self.has_errors


class _Issues:
    def __init__(self) -> None:
        self.items: list[Issue] = []

    def error(self, code: str, field_path: str | None, message: str) -> None:
        self.items.append(Issue(code=code, severity="error", field=field_path, message=message))

    def warning(self, code: str, field_path: str | None, message: str) -> None:
        self.items.append(Issue(code=code, severity="warning", field=field_path, message=message))


def _sentence(label: str) -> str:
    return label[:1].upper() + label[1:]


def _round(value: Decimal, currency: Currency) -> Decimal:
    return value.quantize(currency.quantum, rounding=ROUND_HALF_UP)


def _too_many_decimals(currency: Currency) -> str:
    if currency.minor_units == 0:
        return f"{currency.code} amounts don't use decimals."
    places = "place" if currency.minor_units == 1 else "places"
    return f"{currency.code} amounts can have at most {currency.minor_units} decimal {places}."


def _money(
    raw: str,
    field_path: str,
    label: str,
    currency: Currency | None,
    issues: _Issues,
    *,
    required: bool,
) -> Decimal | None:
    try:
        value = parse_number(raw)
    except ValueError:
        issues.error(
            "invalid_number",
            field_path,
            f"Enter {label} as a number, for example 17000 or 17,000.50.",
        )
        return None
    if value is None:
        if required:
            issues.error("missing_value", field_path, f"Enter {label}.")
        return None
    if value < 0:
        issues.error("negative_value", field_path, f"{_sentence(label)} can't be negative.")
        return None
    if value >= MAX_AMOUNT:
        issues.error("value_too_large", field_path, f"{_sentence(label)} is too large.")
        return None
    if currency is not None and decimal_places(value) > currency.minor_units:
        issues.error("too_many_decimals", field_path, _too_many_decimals(currency))
        return None
    return value


def _quantity(raw: str, field_path: str, issues: _Issues) -> Decimal | None:
    try:
        value = parse_number(raw)
    except ValueError:
        issues.error("invalid_number", field_path, "Enter the quantity as a number.")
        return None
    if value is None:
        issues.error("missing_value", field_path, "Enter a quantity.")
        return None
    if value <= 0:
        issues.error("non_positive_quantity", field_path, "The quantity must be more than zero.")
        return None
    if value >= MAX_QUANTITY:
        issues.error("value_too_large", field_path, "The quantity is too large.")
        return None
    if decimal_places(value) > QUANTITY_PLACES:
        issues.error("too_many_decimals", field_path, "Use up to 3 decimal places for quantities.")
        return None
    return value


def _percent(raw: str, field_path: str, issues: _Issues) -> Decimal | None:
    try:
        value = parse_number(raw)
    except ValueError:
        issues.error("invalid_number", field_path, "Enter the percentage as a number, for example 10.")
        return None
    if value is None:
        issues.error("missing_value", field_path, "Enter the percentage.")
        return None
    if value <= 0 or value > 100:
        issues.error("invalid_percent", field_path, "Enter a percentage above 0 and up to 100.")
        return None
    if decimal_places(value) > PERCENT_PLACES:
        issues.error("too_many_decimals", field_path, "Use up to 2 decimal places for percentages.")
        return None
    return value


def _date(raw: str, field_path: str, label: str, issues: _Issues) -> date | None:
    text = raw.strip()
    if not text:
        return None
    try:
        if not _ISO_DATE.match(text):
            raise ValueError("not YYYY-MM-DD")
        value = date.fromisoformat(text)
    except ValueError:
        issues.error("invalid_date", field_path, f"Enter {label} as a valid date.")
        return None
    if not EARLIEST_DATE <= value <= LATEST_DATE:
        issues.error("date_out_of_range", field_path, f"Enter {label} between 1990 and 2100.")
        return None
    return value


def _currency(code: str, issues: _Issues) -> Currency | None:
    if not code.strip():
        issues.error("missing_currency", "currency", "Choose a currency.")
        return None
    currency = get_currency(code)
    if currency is None:
        issues.error("unsupported_currency", "currency", f"“{code.strip()}” isn't a supported currency.")
    return currency


def _adjustments(
    rows: list[AdjustmentDraft],
    group: str,
    noun: str,
    items_subtotal: Decimal | None,
    currency: Currency | None,
    issues: _Issues,
) -> tuple[dict[str, Decimal | None], Decimal | None]:
    amounts: dict[str, Decimal | None] = {}
    for row in rows:
        value_path = f"{group}.{row.id}.value"
        if row.kind == "percent":
            percent = _percent(row.value, value_path, issues)
            if percent is not None and items_subtotal is not None and currency is not None:
                amounts[row.id] = _round(items_subtotal * percent / 100, currency)
            else:
                amounts[row.id] = None
        else:
            amounts[row.id] = _money(
                row.value, value_path, f"the {noun} amount", currency, issues, required=True
            )
    if all(amount is not None for amount in amounts.values()):
        total = sum((amount for amount in amounts.values() if amount is not None), Decimal(0))
    else:
        total = None
    return amounts, total


def compute(draft: TransactionDraft) -> Computation:
    issues = _Issues()
    currency = _currency(draft.currency, issues)

    transaction_date = _date(draft.transaction_date, "transaction_date", "the transaction date", issues)
    due_date = _date(draft.due_date, "due_date", "the due date", issues)
    if not draft.transaction_date.strip():
        issues.warning("missing_date", "transaction_date", "No transaction date yet. Add it if you know it.")
    if transaction_date and due_date and due_date < transaction_date:
        issues.warning("due_before_transaction", "due_date", "The due date is before the transaction date.")

    # Items
    if not draft.line_items:
        issues.error("no_items", "line_items", "Add at least one item.")
    quantities: dict[str, Decimal | None] = {}
    unit_prices: dict[str, Decimal | None] = {}
    line_amounts: dict[str, Decimal | None] = {}
    seen: set[tuple[str, Decimal | None]] = set()
    for item in draft.line_items:
        base = f"line_items.{item.id}"
        description = item.description.strip()
        if not description:
            issues.error("missing_description", f"{base}.description", "Describe this item.")
        quantity = _quantity(item.quantity, f"{base}.quantity", issues)
        price = _money(item.unit_price, f"{base}.unit_price", "the unit price", currency, issues, required=True)
        quantities[item.id] = quantity
        unit_prices[item.id] = price
        if quantity is not None and price is not None and currency is not None:
            line_amounts[item.id] = _round(quantity * price, currency)
        else:
            line_amounts[item.id] = None
        key = (" ".join(description.lower().split()), price)
        if description and key in seen:
            issues.warning(
                "possible_duplicate",
                base,
                f"This looks the same as another item (“{description}”). Check it isn't counted twice.",
            )
        seen.add(key)

    known_lines = [amount for amount in line_amounts.values() if amount is not None]
    if draft.line_items and len(known_lines) == len(line_amounts):
        items_subtotal: Decimal | None = sum(known_lines, Decimal(0))
    else:
        items_subtotal = None

    discount_amounts, discount_total = _adjustments(
        draft.discounts, "discounts", "discount", items_subtotal, currency, issues
    )
    fee_amounts, fee_total = _adjustments(
        draft.additional_fees, "additional_fees", "fee", items_subtotal, currency, issues
    )
    delivery_fee = _money(draft.delivery_fee, "delivery_fee", "the delivery fee", currency, issues, required=False)
    delivery_invalid = bool(draft.delivery_fee.strip()) and delivery_fee is None

    total: Decimal | None = None
    if (
        items_subtotal is not None
        and discount_total is not None
        and fee_total is not None
        and not delivery_invalid
    ):
        total = items_subtotal - discount_total + fee_total + (delivery_fee or Decimal(0))
        if total < 0:
            issues.error(
                "negative_total",
                "discounts",
                "The discounts are larger than the total. Check the discount amounts.",
            )
            total = None

    amount_paid = _payment(draft, total, currency, issues)
    outstanding = total - amount_paid if total is not None and amount_paid is not None else None

    # Cross-check against a total the conversation states, if any.
    try:
        stated_total = parse_number(draft.stated_total)
    except ValueError:
        stated_total = None
    if stated_total is not None and total is not None and currency is not None and stated_total != total:
        issues.warning(
            "stated_total_mismatch",
            "stated_total",
            f"The conversation mentions a total of {format_money(stated_total, currency)}, but these "
            f"details add up to {format_money(total, currency)}. Check the items, fees and delivery.",
        )
    # And against a balance someone states, which says what the price and payments must add up to.
    try:
        stated_outstanding = parse_number(draft.stated_outstanding)
    except ValueError:
        stated_outstanding = None
    if (
        stated_outstanding is not None
        and outstanding is not None
        and currency is not None
        and stated_outstanding != outstanding
    ):
        issues.warning(
            "stated_outstanding_mismatch",
            "stated_outstanding",
            f"The balance mentioned is {format_money(stated_outstanding, currency)}, but these details leave "
            f"{format_money(outstanding, currency)} outstanding. Check the price and the amount paid.",
        )

    return Computation(
        currency=currency,
        transaction_date=transaction_date,
        due_date=due_date,
        quantities=quantities,
        unit_prices=unit_prices,
        line_amounts=line_amounts,
        items_subtotal=items_subtotal,
        discount_amounts=discount_amounts,
        discount_total=discount_total,
        fee_amounts=fee_amounts,
        fee_total=fee_total,
        delivery_fee=delivery_fee,
        total=total,
        amount_paid=amount_paid,
        outstanding=outstanding,
        issues=issues.items,
    )


def _payment(
    draft: TransactionDraft,
    total: Decimal | None,
    currency: Currency | None,
    issues: _Issues,
) -> Decimal | None:
    status = draft.payment_status
    entered_text = draft.amount_paid.strip()
    entered = _money(draft.amount_paid, "amount_paid", "the amount paid", currency, issues, required=False)
    if entered_text and entered is None:
        return None  # invalid entry; the problem is already reported

    if entered is None:
        if status == PaymentStatus.PARTIALLY_PAID:
            issues.error("missing_part_payment", "amount_paid", "Enter how much has been paid so far.")
            return None
        if status == PaymentStatus.PAID:
            return total
        if status == PaymentStatus.UNPAID:
            return Decimal(0)
        return None

    if status == PaymentStatus.UNPAID and entered > 0:
        issues.error(
            "unpaid_with_payment",
            "amount_paid",
            "The status is “Unpaid” but an amount paid is entered. Change the status or clear the amount.",
        )
    elif status == PaymentStatus.NOT_SPECIFIED and entered > 0:
        issues.warning("status_not_set", "payment_status", "An amount paid is entered. Set the payment status to match.")

    if total is not None and currency is not None:
        if status == PaymentStatus.PAID and entered != total:
            issues.error(
                "paid_amount_mismatch",
                "amount_paid",
                f"The status is “Paid” but the amount paid isn't the full total of "
                f"{format_money(total, currency)}. Use “Partially paid” or correct the amount.",
            )
        elif status == PaymentStatus.PARTIALLY_PAID and not Decimal(0) < entered < total:
            issues.error(
                "invalid_part_payment",
                "amount_paid",
                "A part payment must be more than zero and less than the total.",
            )
        elif status in (PaymentStatus.NOT_SPECIFIED, PaymentStatus.UNPAID) and entered > total:
            issues.error("overpaid", "amount_paid", "The amount paid is more than the total.")
    return entered


def _as_text(value: Decimal | None, currency: Currency | None) -> str | None:
    if value is None:
        return None
    if currency is not None:
        return str(value.quantize(currency.quantum))
    return to_plain(value)


def to_calculation(draft: TransactionDraft, computation: Computation) -> Calculation:
    currency = computation.currency
    return Calculation(
        currency=currency.code if currency else None,
        minor_units=currency.minor_units if currency else None,
        line_items=[
            AmountLine(id=item.id, amount=_as_text(computation.line_amounts.get(item.id), currency))
            for item in draft.line_items
        ],
        items_subtotal=_as_text(computation.items_subtotal, currency),
        discounts=[
            AmountLine(id=row.id, amount=_as_text(computation.discount_amounts.get(row.id), currency))
            for row in draft.discounts
        ],
        discount_total=_as_text(computation.discount_total, currency),
        additional_fees=[
            AmountLine(id=row.id, amount=_as_text(computation.fee_amounts.get(row.id), currency))
            for row in draft.additional_fees
        ],
        fee_total=_as_text(computation.fee_total, currency),
        delivery_fee=_as_text(computation.delivery_fee, currency),
        total=_as_text(computation.total, currency),
        amount_paid=_as_text(computation.amount_paid, currency),
        outstanding=_as_text(computation.outstanding, currency),
        issues=computation.issues,
        can_export=computation.can_export,
    )


def calculate(draft: TransactionDraft) -> Calculation:
    return to_calculation(draft, compute(draft))
