"""Guided corrections of a draft over chat.

The customer picks a field from a list and types the new value. Each value is parsed here and then
checked by the backend's own calculation rules, so WhatsApp can't accept anything the website's
review form would refuse. No AI is involved, and nothing is rounded or guessed: a value that can't
be read is explained and asked for again.

Problems between fields (a status of "Paid" with a part payment entered, say) are not refused here.
They appear on the summary under "Fix before saving", and Confirm stays unavailable until they're gone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.calculations import compute, parse_number, to_plain
from app.currencies import CURRENCIES, get_currency
from app.documents.render import STATUS_LABELS
from app.extraction.service import clean_text
from app.models import AdjustmentDraft, LineItemDraft, PaymentStatus, TransactionDraft
from app.whatsapp.messages import MAX_ROWS, Row, list_message, reply_id
from app.whatsapp.summary import money, readable_date

NEW_ITEM = "new_item"
NEW_DISCOUNT = "new:discounts"
NEW_FEE = "new:additional_fees"
MAX_ITEMS = 50
MAX_ADJUSTMENTS = 10
ITEMS_PER_PAGE = 8

_CLEAR = frozenset({"none", "clear", "nil", "remove", "delete", "-", "n/a", "na"})
_TEXT_FIELDS: dict[str, tuple[str, int]] = {
    "seller": ("the seller's name", 120),
    "buyer": ("the buyer's name", 120),
    "payment_terms": ("the payment terms", 1000),
    "delivery_terms": ("the delivery terms", 1000),
    "notes": ("the notes", 2000),
}
_AMOUNT_FIELDS = {"delivery_fee": "the delivery fee", "amount_paid": "the amount paid so far"}
_DATE_FIELDS = {"transaction_date": "the transaction date", "due_date": "the due date"}
_VALUE_CODES = frozenset(
    {
        "invalid_number", "too_many_decimals", "negative_value", "value_too_large", "non_positive_quantity",
        "invalid_percent", "invalid_date", "date_out_of_range",
    }
)
_AMOUNT = re.compile(
    # "#" is how many Nigerian keyboards write the naira sign.
    r"^\s*(?:₦|\#|ngn|naira|n(?=\s*\d)|\$|usd|£|gbp|€|eur)?\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<suffix>k|thousand|m|million)?\s*(?:naira|ngn|usd|gbp|eur)?\s*(?:/=|only|each|per\s+(?:one|unit|piece))?\s*\.?\s*$",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"^\s*(?P<number>\d+(?:\.\d+)?)\s*(?:%|percent|per cent)\s*$", re.IGNORECASE)
_QUANTITY = re.compile(r"^[^\d-]*(?P<number>\d+(?:\.\d+)?)[^\d.]*$")
_MONTHS = {
    name: number
    for number, names in enumerate(
        (("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
         ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"), ("december", "dec")),
        start=1,
    )
    for name in names
}
COMMON_CURRENCIES = ("NGN", "USD", "GBP", "EUR", "GHS", "KES", "ZAR")


@dataclass(frozen=True)
class EditResult:
    draft: TransactionDraft | None = None
    error: str = ""
    then: tuple[str, ...] = field(default_factory=tuple)  # further values to ask for


def read_amount(raw: str) -> str | None:
    """"₦17,000", "17k", "2.5m" and "17000.50" as a plain number; None when it can't be read."""
    match = _AMOUNT.match(raw)
    if match is None:
        return None
    number = match["number"]
    try:
        value = parse_number(number)
    except ValueError:
        return None
    if value is None:
        return None
    suffix = (match["suffix"] or "").lower()
    if suffix in ("k", "thousand"):
        value *= 1000
    elif suffix in ("m", "million"):
        value *= 1_000_000
    return to_plain(value)


_WEEKDAYS = {
    name: number
    for number, names in enumerate(
        (("monday", "mon"), ("tuesday", "tue", "tues"), ("wednesday", "wed"), ("thursday", "thu", "thur", "thurs"),
         ("friday", "fri"), ("saturday", "sat"), ("sunday", "sun")),
    )
    for name in names
}
_DATE_LEADING = re.compile(r"^(?:(?:it was|it's|its|it is|the date is|the date was|date|was|on|dated)\s+)+")


def _relative_date(text: str, today: date, *, future: bool) -> date | None:
    """Words like today, yesterday, tomorrow, Monday or "3 days ago", seen from today."""
    if text in ("today", "tdy", "tday", "this morning", "this evening", "tonight"):
        return today
    if not future and text in ("yesterday", "yday", "yest", "yesterday night", "last night"):
        return today - timedelta(days=1)
    if not future and text in ("day before yesterday", "the day before yesterday"):
        return today - timedelta(days=2)
    if future and text in ("tomorrow", "tmrw", "tmr", "tomorow"):
        return today + timedelta(days=1)
    if not future and (match := re.fullmatch(r"(\d{1,2}) days? ago", text)):
        return today - timedelta(days=int(match[1]))
    if match := re.fullmatch(r"(last |next |this )?([a-z]+)", text):
        weekday = _WEEKDAYS.get(match[2])
        if weekday is None:
            return None
        if future:
            ahead = (weekday - today.weekday()) % 7
            return today + timedelta(days=ahead or (7 if match[1] in ("next ", None) else 0))
        back = (today.weekday() - weekday) % 7
        return today - timedelta(days=back or (7 if match[1] == "last " else 0))
    return None


def _without_year(text: str, today: date, *, future: bool) -> date | None:
    """10 Nov, Nov 10 or 10/11 (day first), in the year that keeps it on the right side of today."""
    day_month: tuple[int, int] | None = None
    if match := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})", text):
        day_month = (int(match[1]), int(match[2]))
    elif (match := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?(?: of)? ([a-z]+)", text)) and match[2] in _MONTHS:
        day_month = (int(match[1]), _MONTHS[match[2]])
    elif (match := re.fullmatch(r"([a-z]+) (\d{1,2})(?:st|nd|rd|th)?", text)) and match[1] in _MONTHS:
        day_month = (int(match[2]), _MONTHS[match[1]])
    if day_month is None:
        return None
    try:
        candidate = date(today.year, day_month[1], day_month[0])
    except ValueError:
        return None
    if not future and candidate > today:
        return candidate.replace(year=today.year - 1) if (day_month[1], day_month[0]) != (2, 29) else None
    if future and candidate < today:
        return candidate.replace(year=today.year + 1) if (day_month[1], day_month[0]) != (2, 29) else None
    return candidate


def read_date(raw: str, today: date | None = None, *, future: bool = False) -> str | None:
    """A typed date as YYYY-MM-DD: 2026-11-18, 18/11/2026 (day first), 18 Nov 2026 or Nov 18 2026.

    Given today's date, it also reads "today", "yesterday", "Monday", "3 days ago", "10 Nov" and
    "10/11". A weekday or a date without a year is the most recent one, or for a due date
    (``future``) the next one.
    """
    text = " ".join(raw.strip().lower().replace(",", " ").split()).rstrip(".!")
    text = _DATE_LEADING.sub("", text).strip()
    if today is not None:
        found = _relative_date(text, today, future=future) or _without_year(text, today, future=future)
        if found is not None:
            return found.isoformat()
    parts: tuple[int, int, int] | None = None
    if match := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        parts = (int(match[1]), int(match[2]), int(match[3]))
    elif match := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})", text):
        year = int(match[3]) + (2000 if len(match[3]) == 2 else 0)
        parts = (year, int(match[2]), int(match[1]))
    elif (match := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]+) (\d{4})", text)) and match[2] in _MONTHS:
        parts = (int(match[3]), _MONTHS[match[2]], int(match[1]))
    elif (match := re.fullmatch(r"([a-z]+) (\d{1,2})(?:st|nd|rd|th)? (\d{4})", text)) and match[1] in _MONTHS:
        parts = (int(match[3]), _MONTHS[match[1]], int(match[2]))
    if parts is None:
        return None
    try:
        return date(*parts).isoformat()
    except ValueError:
        return None


def _problem(candidate: TransactionDraft, target: str) -> str:
    for issue in compute(candidate).issues:
        if issue.severity == "error" and issue.field == target and issue.code in _VALUE_CODES:
            return issue.message
    return ""


def _next_row_id(prefix: str, rows: list[Any]) -> str:
    numbers = [int(match[1]) for row in rows if (match := re.fullmatch(rf"{prefix}-(\d+)", row.id))]
    return f"{prefix}-{max(numbers, default=0) + 1}"


def _replace_item(draft: TransactionDraft, row_id: str, **values: str) -> TransactionDraft:
    rows = [row.model_copy(update=values) if row.id == row_id else row for row in draft.line_items]
    return draft.model_copy(update={"line_items": rows})


def _set_adjustment(draft: TransactionDraft, group: str, row_id: str, raw: str) -> EditResult:
    rows: list[AdjustmentDraft] = list(getattr(draft, group))
    if raw.strip().lower() in _CLEAR:
        return EditResult(draft.model_copy(update={group: [row for row in rows if row.id != row_id]}))
    percent = _PERCENT.match(raw)
    value = percent["number"] if percent else read_amount(raw)
    if value is None:
        return EditResult(error="Send an amount like 1500, or a percentage like 10%.")
    kind = "percent" if percent else "amount"
    updated = [row.model_copy(update={"kind": kind, "value": value}) if row.id == row_id else row for row in rows]
    candidate = draft.model_copy(update={group: updated})
    problem = _problem(candidate, f"{group}.{row_id}.value")
    return EditResult(error=problem) if problem else EditResult(candidate)


def apply_text(draft: TransactionDraft, target: str, raw: str, *, today: date | None = None) -> EditResult:
    """Apply a typed value to one field of the draft."""
    text = raw.strip()
    if not text:
        return EditResult(error="Send a value, or *back* to keep what's there.")
    clearing = text.lower() in _CLEAR

    if target in _TEXT_FIELDS:
        _, limit = _TEXT_FIELDS[target]
        if len(text) > limit:
            return EditResult(error=f"That's too long. Keep it under {limit} characters.")
        multiline = target in ("payment_terms", "delivery_terms", "notes")
        return EditResult(draft.model_copy(update={target: "" if clearing else clean_text(text, limit, multiline=multiline)}))

    if target in _AMOUNT_FIELDS:
        if clearing:
            return EditResult(draft.model_copy(update={target: ""}))
        value = read_amount(text)
        if value is None:
            return EditResult(error="Send the amount as a number, for example 17000 or 17k.")
        candidate = draft.model_copy(update={target: value})
        problem = _problem(candidate, target)
        return EditResult(error=problem) if problem else EditResult(candidate)

    if target in _DATE_FIELDS:
        if clearing:
            return EditResult(draft.model_copy(update={target: ""}))
        value = read_date(text, today, future=target == "due_date")
        if value is None:
            return EditResult(error="Send the date like *today*, *yesterday*, *10 Nov* or 10/11/2026.")
        candidate = draft.model_copy(update={target: value})
        problem = _problem(candidate, target)
        return EditResult(error=problem) if problem else EditResult(candidate)

    if target == "currency":
        currency = get_currency(text)
        if currency is None:
            codes = ", ".join(list(CURRENCIES)[:8])
            return EditResult(error=f"Send a currency code such as {codes}.")
        return EditResult(draft.model_copy(update={"currency": currency.code}))

    if target == NEW_ITEM:
        if len(draft.line_items) >= MAX_ITEMS:
            return EditResult(error=f"A record can have up to {MAX_ITEMS} items.")
        row_id = _next_row_id("item", draft.line_items)
        row = LineItemDraft(id=row_id, description=clean_text(text, 200))
        return EditResult(
            draft.model_copy(update={"line_items": [*draft.line_items, row]}),
            then=(f"line_items.{row_id}.quantity", f"line_items.{row_id}.unit_price"),
        )

    if target in (NEW_DISCOUNT, NEW_FEE):
        group = target.removeprefix("new:")
        rows = list(getattr(draft, group))
        if len(rows) >= MAX_ADJUSTMENTS:
            return EditResult(error=f"A record can have up to {MAX_ADJUSTMENTS} of these.")
        noun = "discount" if group == "discounts" else "fee"
        row_id = _next_row_id(noun, rows)
        label = "Discount" if group == "discounts" else "Additional fee"
        added = draft.model_copy(update={group: [*rows, AdjustmentDraft(id=row_id, label=label, kind="amount", value="")]})
        return _set_adjustment(added, group, row_id, text)

    parts = target.split(".")
    if len(parts) == 3 and parts[0] == "line_items":
        _, row_id, attribute = parts
        if not any(row.id == row_id for row in draft.line_items):
            return EditResult(error="That item is no longer on this record.")
        if attribute == "description":
            return EditResult(_replace_item(draft, row_id, description=clean_text(text, 200)))
        if attribute == "quantity":
            match = _QUANTITY.match(text)
            if match is None:
                return EditResult(error="Send the quantity as a number, for example 2.")
            candidate = _replace_item(draft, row_id, quantity=match["number"])
        elif attribute == "unit_price":
            value = read_amount(text)
            if value is None:
                return EditResult(error="Send the price as a number, for example 17000 or 17k.")
            candidate = _replace_item(draft, row_id, unit_price=value)
        else:
            return EditResult(error="That detail can't be changed here.")
        problem = _problem(candidate, target)
        return EditResult(error=problem) if problem else EditResult(candidate)
    if len(parts) == 3 and parts[0] in ("discounts", "additional_fees") and parts[2] == "value":
        if not any(row.id == parts[1] for row in getattr(draft, parts[0])):
            return EditResult(error="That entry is no longer on this record.")
        return _set_adjustment(draft, parts[0], parts[1], text)
    return EditResult(error="That detail can't be changed here.")


def remove_item(draft: TransactionDraft, row_id: str) -> TransactionDraft:
    return draft.model_copy(update={"line_items": [row for row in draft.line_items if row.id != row_id]})


def set_payment_status(draft: TransactionDraft, status: PaymentStatus) -> TransactionDraft:
    """A chosen status replaces any part-payment figure; a part payment then asks for its amount."""
    keep = status == PaymentStatus.PARTIALLY_PAID
    return draft.model_copy(update={"payment_status": status, "amount_paid": draft.amount_paid if keep else ""})


def prompt_for(draft: TransactionDraft, target: str) -> str:
    back = "Send *back* to keep what's there."
    if target in _TEXT_FIELDS:
        return f"Send {_TEXT_FIELDS[target][0]}, or *none* to leave it blank. {back}"
    if target in _AMOUNT_FIELDS:
        return f"Send {_AMOUNT_FIELDS[target]} (for example 2000 or 2k), or *none* to leave it blank. {back}"
    if target in _DATE_FIELDS:
        return f"Send {_DATE_FIELDS[target]}, like *today*, *yesterday* or *10 Nov*, or *none* to leave it blank. {back}"
    if target == "currency":
        return f"Send the currency code, for example NGN or USD. {back}"
    if target == NEW_ITEM:
        return f"What's the item called? {back}"
    if target == NEW_DISCOUNT:
        return f"Send the discount as an amount (1500) or a percentage (10%). {back}"
    if target == NEW_FEE:
        return f"Send the fee as an amount (500) or a percentage (5%). {back}"
    parts = target.split(".")
    if len(parts) == 3 and parts[0] == "line_items":
        name = next((row.description for row in draft.line_items if row.id == parts[1]), "this item") or "this item"
        wording = {"description": f"the new name for “{name}”", "quantity": f"the quantity of “{name}”", "unit_price": f"the price of one “{name}” (for example 17000 or 17k)"}
        return f"Send {wording.get(parts[2], 'the new value')}. {back}"
    if len(parts) == 3:
        return f"Send the new amount (1500) or percentage (10%), or *remove* to take it off. {back}"
    return back


class Menus:
    """The correction lists. Every row carries the submission and version it was built from."""

    def __init__(self, draft: TransactionDraft, submission_id: str, revision: int) -> None:
        self.draft = draft
        self.submission_id = submission_id
        self.revision = revision
        self.currency = get_currency(draft.currency)

    def _id(self, action: str, arg: str = "") -> str:
        return reply_id(action, self.submission_id, self.revision, arg)

    def _show(self, value: str) -> str:
        return money(value, self.currency, empty="Not set")

    def main(self) -> dict[str, Any]:
        d = self.draft
        count = len(d.line_items)
        rows = [
            Row(self._id("ask", "seller"), "Seller", d.seller or "Not set"),
            Row(self._id("ask", "buyer"), "Buyer", d.buyer or "Not set"),
            Row(self._id("nav", "items"), "Items", f"{count} item{'' if count == 1 else 's'}"),
            Row(self._id("ask", "delivery_fee"), "Delivery fee", self._show(d.delivery_fee)),
            Row(self._id("nav", "adj:discounts"), "Discounts", f"{len(d.discounts)} added" if d.discounts else "None"),
            Row(self._id("nav", "adj:additional_fees"), "Other fees", f"{len(d.additional_fees)} added" if d.additional_fees else "None"),
            Row(self._id("nav", "pay"), "Payment", STATUS_LABELS[d.payment_status]),
            Row(self._id("nav", "cur"), "Currency", d.currency or "Not set"),
            Row(self._id("ask", "transaction_date"), "Date", readable_date(d.transaction_date) if d.transaction_date else "Not set"),
            Row(self._id("nav", "more"), "More details", "Due date, terms and notes"),
        ]
        return list_message(
            "What needs correcting? You can also just tell me, like “the price is 20k” or “he still owes 40k”.",
            "Choose a detail",
            rows,
        )

    def items(self, page: int = 0) -> dict[str, Any]:
        items = self.draft.line_items
        start = max(page, 0) * ITEMS_PER_PAGE
        rows = [
            Row(self._id("nav", f"item:{row.id}"), f"Item {start + number}", f"{row.description or 'No description'} · {row.quantity or '?'} × {self._show(row.unit_price)}")
            for number, row in enumerate(items[start : start + ITEMS_PER_PAGE], start=1)
        ]
        if start + ITEMS_PER_PAGE < len(items):
            rows.append(Row(self._id("nav", f"items:{page + 1}"), "More items"))
        rows.append(Row(self._id("ask", NEW_ITEM), "Add an item"))
        return list_message("Which item needs correcting?", "Choose an item", rows[:MAX_ROWS])

    def item(self, row_id: str) -> dict[str, Any] | None:
        row = next((entry for entry in self.draft.line_items if entry.id == row_id), None)
        if row is None:
            return None
        base = f"line_items.{row_id}"
        rows = [
            Row(self._id("ask", f"{base}.description"), "Name", row.description or "Not set"),
            Row(self._id("ask", f"{base}.quantity"), "Quantity", row.quantity or "Not set"),
            Row(self._id("ask", f"{base}.unit_price"), "Unit price", self._show(row.unit_price)),
            Row(self._id("rm", row_id), "Remove this item"),
            Row(self._id("nav", "items"), "Back to items"),
        ]
        return list_message(f"What should change on “{row.description or 'this item'}”?", "Choose a detail", rows)

    def adjustments(self, group: str) -> dict[str, Any]:
        rows_in_draft: list[AdjustmentDraft] = list(getattr(self.draft, group))
        noun = "discount" if group == "discounts" else "fee"
        rows = [
            Row(
                self._id("ask", f"{group}.{row.id}.value"),
                row.label or noun.capitalize(),
                f"{row.value}%" if row.kind == "percent" else self._show(row.value),
            )
            for row in rows_in_draft[: MAX_ROWS - 1]
        ]
        rows.append(Row(self._id("ask", NEW_DISCOUNT if group == "discounts" else NEW_FEE), f"Add a {noun}"))
        return list_message(f"Choose a {noun} to change, or add one.", "Choose", rows)

    def payment(self) -> dict[str, Any]:
        rows = [Row(self._id("pay", status.value), STATUS_LABELS[status]) for status in PaymentStatus]
        return list_message("What does the chat say about payment?", "Choose a status", rows)

    def currencies(self) -> dict[str, Any]:
        rows = [Row(self._id("cur", code), code, CURRENCIES[code].name) for code in COMMON_CURRENCIES]
        rows.append(Row(self._id("ask", "currency"), "Another currency"))
        return list_message("Which currency are the amounts in?", "Choose a currency", rows)

    def more(self) -> dict[str, Any]:
        d = self.draft
        rows = [
            Row(self._id("ask", "due_date"), "Due date", readable_date(d.due_date) if d.due_date else "Not set"),
            Row(self._id("ask", "payment_terms"), "Payment terms", d.payment_terms or "Not set"),
            Row(self._id("ask", "delivery_terms"), "Delivery terms", d.delivery_terms or "Not set"),
            Row(self._id("ask", "notes"), "Notes", d.notes or "Not set"),
        ]
        return list_message("Which detail needs correcting?", "Choose a detail", rows)
