"""Build the transaction record's HTML from a draft and its backend calculation."""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from app.calculations import Computation, parse_number, to_plain
from app.currencies import format_money
from app.models import PaymentStatus, TransactionDraft

DOCUMENTS_DIR = Path(__file__).resolve().parent
FONTS_DIR = DOCUMENTS_DIR / "fonts"
# The document loads nothing from the network: fonts are embedded and scripts are not allowed.
DOCUMENT_CSP = "default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:"

_environment = Environment(
    loader=FileSystemLoader(DOCUMENTS_DIR / "templates"),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

STATUS_LABELS = {
    PaymentStatus.NOT_SPECIFIED: "Not specified",
    PaymentStatus.UNPAID: "Unpaid",
    PaymentStatus.PARTIALLY_PAID: "Partially paid",
    PaymentStatus.PAID: "Paid",
    PaymentStatus.REFUNDED: "Refunded",
}
_STATUS_TONES = {
    PaymentStatus.NOT_SPECIFIED: "neutral",
    PaymentStatus.UNPAID: "open",
    PaymentStatus.PARTIALLY_PAID: "open",
    PaymentStatus.PAID: "settled",
    PaymentStatus.REFUNDED: "neutral",
}


def format_date(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


@lru_cache(maxsize=1)
def fonts_css() -> str:
    manifest = FONTS_DIR / "fonts.json"
    if not manifest.exists():
        return ""
    rules = []
    for face in json.loads(manifest.read_text(encoding="utf-8")):
        data = base64.b64encode((FONTS_DIR / face["file"]).read_bytes()).decode("ascii")
        rules.append(
            "@font-face{"
            f"font-family:'{face['family']}';font-style:{face['style']};font-weight:{face['weight']};"
            f"font-display:block;src:url(data:font/woff2;base64,{data}) format('woff2');"
            f"unicode-range:{face['unicodeRange']};"
            "}"
        )
    return "\n".join(rules)


def _percent_label(raw: str) -> str:
    value = parse_number(raw)
    return f"{to_plain(value)}%" if value is not None else ""


def build_view(draft: TransactionDraft, computation: Computation, prepared_on: date) -> dict[str, Any]:
    """Display-ready values. Only call with an exportable computation."""
    currency = computation.currency
    if currency is None or computation.total is None or computation.items_subtotal is None:
        raise ValueError("The record can't be built from an incomplete calculation.")

    def money(value: Any) -> str:
        return format_money(value, currency)

    items = []
    for item in draft.line_items:
        quantity = computation.quantities[item.id]
        items.append(
            {
                "description": item.description.strip(),
                "quantity": to_plain(quantity) if quantity is not None else "",
                "unit_price": money(computation.unit_prices[item.id]),
                "amount": money(computation.line_amounts[item.id]),
            }
        )

    summary = [{"label": "Subtotal", "value": money(computation.items_subtotal)}]
    for row in draft.discounts:
        label = row.label.strip() or "Discount"
        if row.kind == "percent":
            label = f"{label} ({_percent_label(row.value)})"
        summary.append({"label": label, "value": f"−{money(computation.discount_amounts[row.id])}"})
    for row in draft.additional_fees:
        label = row.label.strip() or "Additional fee"
        if row.kind == "percent":
            label = f"{label} ({_percent_label(row.value)})"
        summary.append({"label": label, "value": money(computation.fee_amounts[row.id])})
    if computation.delivery_fee is not None:
        summary.append({"label": "Delivery", "value": money(computation.delivery_fee)})

    terms = [
        {"title": "Payment terms", "text": draft.payment_terms.strip()},
        {"title": "Delivery terms", "text": draft.delivery_terms.strip()},
        {"title": "Notes", "text": draft.notes.strip()},
    ]
    return {
        "seller": draft.seller.strip() or None,
        "buyer": draft.buyer.strip() or None,
        "transaction_date": format_date(computation.transaction_date) if computation.transaction_date else None,
        "due_date": format_date(computation.due_date) if computation.due_date else None,
        "currency": f"{currency.name} ({currency.code})",
        "items": items,
        "summary": summary,
        "total": money(computation.total),
        "amount_paid": money(computation.amount_paid) if computation.amount_paid is not None else None,
        "outstanding": money(computation.outstanding) if computation.outstanding is not None else None,
        "status": draft.payment_status.value,
        "status_label": STATUS_LABELS[draft.payment_status],
        "status_tone": _STATUS_TONES[draft.payment_status],
        "terms": [term for term in terms if term["text"]],
        "prepared_on": format_date(prepared_on),
    }


def render_document_html(
    draft: TransactionDraft, computation: Computation, *, prepared_on: date, receipt: dict[str, Any] | None = None
) -> str:
    """The record as HTML. ``receipt`` adds an issued receipt's reference and the version it records."""
    view = build_view(draft, computation, prepared_on)
    template = _environment.get_template("record.html")
    # fonts_css comes from our own font files, so it's marked safe; everything else is escaped.
    return template.render(**view, receipt=receipt, fonts_css=Markup(fonts_css()), csp=DOCUMENT_CSP)


def document_filename(draft: TransactionDraft, computation: Computation, extension: str) -> str:
    parts = ["chatreceipt"]
    name = draft.seller.strip() or draft.buyer.strip()
    if name:
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:40].strip("-")
        if slug:
            parts.append(slug)
    if computation.transaction_date:
        parts.append(computation.transaction_date.isoformat())
    return "-".join(parts) + f".{extension}"
