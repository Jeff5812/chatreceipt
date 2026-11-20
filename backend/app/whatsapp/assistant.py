"""Understand what someone wants when their message isn't a screenshot, a button or a fixed command.

Cheapest first:

1. ``quick_request`` recognises common phrasings without the AI ("clear my records", "who owes me",
   "how much did I sell this week"), which also saves the daily AI allowance.
2. Otherwise the turn interpreter (``understanding.py``) reads it in context and returns an intent and
   search filters, turned into a ``Request`` by ``request_from``.

Either way ChatReceipt answers from the person's own saved records. The AI never writes an amount, a
name or a record: a name or reference it returns is only used to search, and only if the message
really contains it. Every figure in a reply is added up here, from the database.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.currencies import get_currency
from app.whatsapp.summary import money
from app.whatsapp.understanding import AICorrection

# Requests answered from the customer's saved records or account.
RECORD_INTENTS = ("list_records", "find_records", "money_owed", "totals", "resend_receipt", "web_link", "delete_data")

PERIOD_LABELS = {
    "any": "", "today": "today", "yesterday": "yesterday", "this_week": "this week", "last_week": "last week",
    "this_month": "this month", "last_month": "last month", "this_year": "this year",
}


@dataclass(frozen=True)
class Request:
    """What someone asked for, with the filters to search their records by."""

    intent: str
    person: str = ""
    reference: str = ""
    period: str = "any"
    status: str = "any"
    format: str = "image"


_REFERENCE = re.compile(r"\bCR-?\s?(\d{6})\b", re.IGNORECASE)


def _reference_in(text: str) -> str:
    found = _REFERENCE.search(text)
    return f"CR-{found.group(1)}" if found else ""


def request_from(reading: AICorrection, message: str) -> Request:
    """The interpreter's answer as a records request. A name or reference the message doesn't contain is dropped."""
    person = " ".join(reading.person.split())[:80]
    if person and person.casefold() not in " ".join(message.split()).casefold():
        person = ""
    reference = _reference_in(reading.reference)
    if reference and reference != _reference_in(message):
        reference = ""
    return Request(reading.intent, person=person, reference=reference, period=reading.period, status=reading.status, format=reading.format)


# -- without the AI -------------------------------------------------------------------------------

_ERASE = re.compile(
    r"\b(clear|delete|erase|remove|wipe|reset)\b.{0,24}\b(my|all|every)\b.{0,16}\b(records?|data|history|account|receipts?|everything|info)\b"
    r"|\b(clear|delete|erase|wipe|reset) everything\b|\bdelete my data\b"
)
_OWED = re.compile(
    r"\bwho (still )?(owes?|is owing|dey owe|never pay|hasn ?t paid|has not paid|didn ?t pay|did not pay)\b"
    r"|\bhow much (am i|i dey|is) owed\b|\b(debtors|outstanding (money|balances?|payments?))\b"
    r"|\b(unpaid|owing) (records|receipts|customers|ones)\b"
)
_TOTALS = re.compile(
    r"\bhow much (did i|have i|i don|i) (make|made|sell|sold|earn|earned|record|recorded|collect|collected)\b"
    r"|\btotal (sales|sold|income|revenue|earnings)\b|\bmy (sales|earnings|income)\b|\bhow many (records|receipts|sales)\b"
)
_LIST = frozenset({
    "my records", "show my records", "show me my records", "see my records", "view my records", "list my records",
    "all my records", "my receipts", "show my receipts", "show me my receipts", "all my receipts", "my saved records",
})
_THANKS = frozenset({"thanks", "thank you", "thank you so much", "thanks a lot", "ok thanks", "okay thanks", "thank u", "thx", "tnx", "tanks", "nice one"})
_SEND = re.compile(r"\b(send|resend|share|give me|forward)\b.*\b(receipt|pdf|copy|image|picture)\b")


def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s-]", " ", text.casefold().replace("'", "")).split())


def _period(text: str) -> str:
    for period, label in PERIOD_LABELS.items():
        if label and re.search(rf"\b{label}\b", text):
            return period
    return "any"


def _format(text: str) -> str:
    if "both" in text:
        return "both"
    return "pdf" if "pdf" in text else "image"


def quick_request(text: str, *, has_draft: bool = False) -> Request | None:
    """Common requests read without the AI. With a draft open, only ones that can't be corrections."""
    plain = _normalise(text)
    if not plain:
        return None
    if _ERASE.search(plain):
        return Request("delete_data")
    reference = _reference_in(text)
    if reference:
        return Request("resend_receipt" if _SEND.search(plain) else "find_records", reference=reference, format=_format(plain))
    if _TOTALS.search(plain):
        return Request("totals", period=_period(plain))
    if plain in _LIST:
        return Request("list_records")
    if has_draft:
        # "he still owes 40k" or "thanks" can be about the draft; let the draft's own handling read them.
        return None
    if _OWED.search(plain):
        return Request("money_owed", period=_period(plain))
    if plain in _THANKS:
        return Request("thanks")
    return None


# -- answers, worked out from the records -----------------------------------------------------------


def period_range(period: str, today: date) -> tuple[date | None, date | None]:
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    previous_end = first - timedelta(days=1)
    return {
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "this_week": (monday, today),
        "last_week": (monday - timedelta(days=7), monday - timedelta(days=1)),
        "this_month": (first, today),
        "last_month": (previous_end.replace(day=1), previous_end),
        "this_year": (today.replace(month=1, day=1), today),
    }.get(period, (None, None))


def describe(request: Request) -> str:
    """The filters in words, for headings: "for Ada", "this week", "for CR-000012"."""
    parts = []
    if request.reference:
        parts.append(request.reference)
    elif request.person:
        parts.append(f"for {request.person}")
    if PERIOD_LABELS.get(request.period):
        parts.append(PERIOD_LABELS[request.period])
    return " ".join(parts)


def _other_party(row: Mapping[str, Any]) -> str:
    return row["buyer"] or row["seller"] or "Someone"


def owed_text(rows: Sequence[Mapping[str, Any]], heading: str) -> str:
    """Who still owes what, from records with an outstanding amount."""
    lines = [f"*Still owed {heading}*".replace("  ", " ").replace(" *", "*")]
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        currency = get_currency(row["currency"])
        lines.append(f"• {_other_party(row)}: {money(row['outstanding'], currency)} ({row['reference']})")
        totals[row["currency"]] += Decimal(row["outstanding"])
    lines.append("")
    lines.append("Total still owed: " + ", ".join(money(amount, get_currency(code)) for code, amount in totals.items()))
    return "\n".join(lines)


def totals_text(rows: Sequence[Mapping[str, Any]], heading: str) -> str:
    """Agreed, paid and still owed, per currency, for the records found."""
    by_currency: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
    for row in rows:
        sums = by_currency[row["currency"]]
        for index, column in enumerate(("total", "amount_paid", "outstanding")):
            sums[index] += Decimal(row[column] or 0)
    count = len(rows)
    lines = [f"*Your records {heading}*".replace("  ", " ").replace(" *", "*") + f" · {count} record{'s' if count != 1 else ''}"]
    for code, (total, paid, owed) in by_currency.items():
        currency = get_currency(code)
        lines.append(f"Agreed {money(total, currency)} · paid {money(paid, currency)} · still owed {money(owed, currency)}")
    return "\n".join(lines)
