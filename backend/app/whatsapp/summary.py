"""A draft or saved record as readable WhatsApp messages.

All amounts come from the backend's calculation. Payment wording always says the status is what
the chat recorded, never that a payment was verified.

A draft is shown in full once, when it's first read (and whenever the customer asks to see it).
After a correction, the customer gets a short update instead: what changed, the money summary and
the one next question, so the same details aren't repeated message after message.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from app.calculations import calculate, parse_number
from app.currencies import Currency, format_money, get_currency
from app.documents.render import STATUS_LABELS, format_date
from app.models import Calculation, PaymentStatus, TransactionDraft
from app.whatsapp.messages import BUTTON_BODY_LIMIT, button_message, reply_id, text_message
from app.whatsapp.reconcile import CONFLICT_CODES, proposal, single_simple_item

MAX_ITEMS_SHOWN = 15
MAX_CHECKS_SHOWN = 4
NOT_VERIFIED = "_Recorded from the chat. ChatReceipt doesn't verify payments._"
# Characters of WhatsApp's fixed-width text that fit across a phone without wrapping.
TABLE_WIDTH = 26
CONFIRM_QUESTION = "Is this correct?"
SHOW_HINT = "Send “show” to see the whole record"
_ITEM_VALUE = re.compile(r"line_items\.[^.]+\.(?:quantity|unit_price)")
_STATUS_MARKS = {
    PaymentStatus.PAID: "🟢",
    PaymentStatus.PARTIALLY_PAID: "🟡",
    PaymentStatus.UNPAID: "🔴",
    PaymentStatus.REFUNDED: "↩️",
    PaymentStatus.NOT_SPECIFIED: "⚪",
}
# Fields that answer the same question. Correcting one settles the extraction's doubts about the
# others: once the person states the price, a doubt about which price was agreed is answered.
_GROUPS = {
    "line_items": "price", "stated_total": "price", "discounts": "price", "additional_fees": "price",
    "amount_paid": "payment", "stated_outstanding": "payment", "payment_status": "payment",
    "delivery_fee": "delivery", "seller": "parties", "buyer": "parties",
    "transaction_date": "dates", "due_date": "dates", "currency": "currency",
    "payment_terms": "terms", "delivery_terms": "terms", "notes": "terms",
}


def money(value: str | Decimal | None, currency: Currency | None, *, empty: str = "Not stated") -> str:
    if value is None or value == "":
        return empty
    try:
        number = value if isinstance(value, Decimal) else parse_number(value)
    except ValueError:
        return str(value)
    if number is None:
        return empty
    if currency is None:
        return f"{number:,f}".rstrip("0").rstrip(".") if "." in f"{number:f}" else f"{number:,f}"
    return format_money(number, currency)


def readable_date(value: str) -> str:
    try:
        return format_date(date.fromisoformat(value))
    except ValueError:
        return value


def _was_edited(path: str, edited: Iterable[str]) -> bool:
    return any(entry == path or entry.startswith(f"{path}.") for entry in edited)


def _mark(path: str, edited: Sequence[str]) -> str:
    return " _(edited by you)_" if _was_edited(path, edited) else ""


def _group(path: str) -> str | None:
    if path.startswith("line_items.") and path.endswith(".description"):
        return "items"  # renaming an item says nothing about its price
    return _GROUPS.get(path.split(".", 1)[0])


def open_reasons(review: Mapping[str, Any], edited: Sequence[str]) -> list[tuple[int, str]]:
    """The extraction's doubts the customer hasn't settled yet, with their positions in the review.

    A doubt is settled when the customer corrects that field or one answering the same question,
    or when the AI reports that a message answered it.
    """
    resolved = set(review.get("resolved") or [])
    edited_groups = {_group(path) for path in edited} - {None}
    reasons: list[tuple[int, str]] = []
    for index, reason in enumerate(review.get("reasons") or []):
        field = reason.get("field") or ""
        message = str(reason.get("message") or "")
        if not message or index in resolved:
            continue
        if field and (_group(field) in edited_groups or any(path == field or path.startswith(f"{field}.") or field.startswith(f"{path}.") for path in edited)):
            continue
        reasons.append((index, message))
    return reasons


def checks_for(
    calculation: Calculation, review: Mapping[str, Any], edited: Sequence[str], *, skip_conflicts: bool = False
) -> list[str]:
    """What the customer should look at: unsettled review notes, then calculation warnings."""
    checks: list[str] = []
    duplicate = review.get("duplicate_reference")
    if duplicate:
        checks.append(f"You already saved a record from this screenshot ({duplicate}). Save again only if this is a separate transaction.")
    checks += [message for _, message in open_reasons(review, edited)]
    checks += [
        issue.message
        for issue in calculation.issues
        if issue.severity == "warning" and issue.code != "missing_date" and not (skip_conflicts and issue.code in CONFLICT_CODES)
    ]
    return [check for check in dict.fromkeys(checks) if check]


def amount_table(rows: Sequence[tuple[str, str] | None]) -> str:
    """Labels on the left and amounts lined up on the right. ``None`` draws a rule across the table."""
    lines: list[str] = []
    for row in rows:
        if row is None:
            lines.append("-" * TABLE_WIDTH)
            continue
        label = " ".join(row[0].replace("`", "'").split())
        value = row[1]
        if len(label) + len(value) + 2 <= TABLE_WIDTH:
            lines.append(f"{label}{value:>{TABLE_WIDTH - len(label)}}")
        else:
            lines.append(label if len(label) <= TABLE_WIDTH else f"{label[:TABLE_WIDTH - 1]}…")
            lines.append(f"{value:>{TABLE_WIDTH}}")
    return "```" + "\n".join(lines) + "```"


def payment_line(status: PaymentStatus) -> str:
    return f"*Payment:* {_STATUS_MARKS[status]} {STATUS_LABELS[status]}"


def money_rows(draft: TransactionDraft, calculation: Calculation, currency: Currency | None, *, detailed: bool) -> list[tuple[str, str] | None]:
    rows: list[tuple[str, str] | None] = []
    if detailed:
        if draft.discounts or draft.additional_fees or draft.delivery_fee:
            rows.append(("Subtotal", money(calculation.items_subtotal, currency, empty="?")))
        discount_amounts = {row.id: row.amount for row in calculation.discounts}
        for row in draft.discounts:
            suffix = f" ({row.value}%)" if row.kind == "percent" and row.value else ""
            rows.append((f"{row.label.strip() or 'Discount'}{suffix}", f"−{money(discount_amounts.get(row.id), currency, empty='?')}"))
        fee_amounts = {row.id: row.amount for row in calculation.additional_fees}
        for row in draft.additional_fees:
            suffix = f" ({row.value}%)" if row.kind == "percent" and row.value else ""
            rows.append((f"{row.label.strip() or 'Fee'}{suffix}", money(fee_amounts.get(row.id), currency, empty="?")))
        if draft.delivery_fee:
            rows.append(("Delivery", money(calculation.delivery_fee, currency, empty="?")))
        if rows:
            rows.append(None)
    rows += [
        ("Agreed total", money(calculation.total, currency, empty="Not known yet")),
        None,
        ("Paid", money(calculation.amount_paid, currency)),
        ("Remaining", money(calculation.outstanding, currency)),
    ]
    return rows


def _money_block(draft: TransactionDraft, calculation: Calculation, currency: Currency | None, edited: Sequence[str], *, detailed: bool) -> list[str]:
    lines = [amount_table(money_rows(draft, calculation, currency, detailed=detailed))]
    changed = [name for path, name in (("delivery_fee", "Delivery"), ("amount_paid", "Paid")) if _was_edited(path, edited)]
    changed += [row.label.strip() or "Discount" for row in draft.discounts if _was_edited(f"discounts.{row.id}", edited)]
    changed += [row.label.strip() or "Fee" for row in draft.additional_fees if _was_edited(f"additional_fees.{row.id}", edited)]
    if detailed and changed:
        lines.append(f"{', '.join(changed)} _(edited by you)_")
    lines += ["", f"{payment_line(draft.payment_status)}{_mark('payment_status', edited)}"]
    if draft.payment_status != PaymentStatus.NOT_SPECIFIED:
        lines.append(NOT_VERIFIED)
    return lines


def _date_text(draft: TransactionDraft, review: Mapping[str, Any], edited: Sequence[str]) -> str:
    if not draft.transaction_date:
        return "Not found"
    label = str(review.get("date_label") or "")
    source = f" _(“{label.capitalize()}” in the chat)_" if label and not _was_edited("transaction_date", edited) else ""
    return f"{readable_date(draft.transaction_date)}{source}"


def describe(draft: TransactionDraft, calculation: Calculation, review: Mapping[str, Any], edited: Sequence[str]) -> str:
    currency = get_currency(draft.currency)
    lines = [
        f"*Seller:* {draft.seller.strip() or 'Not found'}{_mark('seller', edited)}",
        f"*Buyer:* {draft.buyer.strip() or 'Not found'}{_mark('buyer', edited)}",
        f"*Date:* {_date_text(draft, review, edited)}{_mark('transaction_date', edited)}",
    ]
    if draft.due_date:
        lines.append(f"*Due:* {readable_date(draft.due_date)}{_mark('due_date', edited)}")
    if currency is None:
        lines.append("*Currency:* Not found")

    amounts = {row.id: row.amount for row in calculation.line_items}
    lone = single_simple_item(draft)
    if lone is not None and lone.description.strip():
        lines += ["", f"*For:* {lone.description.strip()}{_mark(f'line_items.{lone.id}', edited)}"]
    else:
        lines += ["", "*Items*"]
        for number, item in enumerate(draft.line_items[:MAX_ITEMS_SHOWN], start=1):
            unit = money(item.unit_price, currency, empty="no price")
            total = money(amounts.get(item.id), currency, empty="?")
            lines.append(f"{number}. {item.description.strip() or 'No description'}{_mark(f'line_items.{item.id}', edited)}")
            lines.append(f"    {item.quantity or '?'} × {unit} = *{total}*")
        if not draft.line_items:
            lines.append("None found")
        if len(draft.line_items) > MAX_ITEMS_SHOWN:
            lines.append(f"…and {len(draft.line_items) - MAX_ITEMS_SHOWN} more")

    lines += ["", "*Money*", *_money_block(draft, calculation, currency, edited, detailed=True)]

    terms = [("Payment terms", draft.payment_terms), ("Delivery terms", draft.delivery_terms), ("Notes", draft.notes)]
    written = [(title, text.strip()) for title, text in terms if text.strip()]
    if written:
        lines.append("")
        lines += [f"*{title}:* {text[:200]}" for title, text in written]
    lines += _problems(draft, calculation, review, edited)
    return "\n".join(lines)


def _problems(draft: TransactionDraft, calculation: Calculation, review: Mapping[str, Any], edited: Sequence[str], *, limit: int = MAX_CHECKS_SHOWN) -> list[str]:
    lines: list[str] = []
    fixes = list(dict.fromkeys(issue.message for issue in calculation.issues if issue.severity == "error"))
    # When the next question asks for a value, listing it again as a problem says the same thing twice.
    if typed_value_needed(calculation, draft) or _no_items_only(calculation):
        fixes = []
    if fixes:
        lines += ["", "🛑 *Fix before saving*", *[f"• {message}" for message in fixes[:limit]]]
    # A disagreement the next question asks about isn't listed as a check as well.
    checks = checks_for(calculation, review, edited, skip_conflicts=proposal(draft) is not None)
    if checks:
        lines += ["", "⚠️ *Please check*", *[f"• {message}" for message in checks[:limit]]]
        if len(checks) > limit:
            lines.append(f"• …and {len(checks) - limit} more")
    return lines


def typed_value_needed(calculation: Calculation, draft: TransactionDraft | None = None) -> str:
    """The next item value to ask for, when that's all that stops a save.

    Either quantities or prices are missing, or a part payment is as large as the only item's
    price: the usual sign that the payment was taken for the price, so the full price is asked for.
    """
    errors = [issue for issue in calculation.issues if issue.severity == "error"]
    if errors and all(issue.code == "missing_value" and _ITEM_VALUE.fullmatch(issue.field or "") for issue in errors):
        return errors[0].field or ""
    if draft is not None and [issue.code for issue in errors] == ["invalid_part_payment"]:
        item = single_simple_item(draft)
        paid = calculation.amount_paid
        if item is not None and paid is not None and Decimal(paid) > 0:
            return f"line_items.{item.id}.unit_price"
    return ""


def _payment_mismatch_only(calculation: Calculation) -> bool:
    errors = {issue.code for issue in calculation.issues if issue.severity == "error"}
    return bool(errors) and errors <= {"invalid_part_payment", "paid_amount_mismatch", "overpaid"}


def _no_items_only(calculation: Calculation) -> bool:
    errors = [issue for issue in calculation.issues if issue.severity == "error"]
    return bool(errors) and all(issue.code == "no_items" for issue in errors)


def question_for(draft: TransactionDraft, target: str) -> str:
    """Ask for one missing item value in plain words, with what's already known."""
    _, row_id, attribute = target.split(".")
    name = next((row.description.strip() for row in draft.line_items if row.id == row_id), "") or "this item"
    if attribute != "unit_price":
        return f"*How many “{name}” were agreed?*\nJust type the number."
    currency = get_currency(draft.currency)
    paid = calculate(draft).amount_paid
    if single_simple_item(draft) is not None and paid and Decimal(paid) > 0:
        return f"*What was the full price of “{name}”?*\n{money(paid, currency)} has been paid so far. Just type the amount, like 60k."
    return f"*What's the price of one “{name}”?*\nJust type the amount, like 15000 or 15k."


def _no_items_question(draft: TransactionDraft, calculation: Calculation) -> str:
    currency = get_currency(draft.currency)
    if calculation.amount_paid and Decimal(calculation.amount_paid) > 0:
        return (
            f"*I found a payment of {money(calculation.amount_paid, currency)}. What was it for, and what was the full price?*\n"
            "For example: “Logo design, 60k”."
        )
    return "*What was bought, and for how much?*\nFor example: “2 bags, 15k each”."


def next_step(draft: TransactionDraft, calculation: Calculation, *, submission_id: str, revision: int) -> tuple[str, list[tuple[str, str]]]:
    """The one question to ask now, and its buttons. Confirm is offered only when the record can be saved."""
    correct = (reply_id("fix", submission_id, revision), "Correct")
    cancel = (reply_id("drop", submission_id, revision), "Cancel")
    fix = proposal(draft)
    if not calculation.can_export and fix is not None and fix.changes and _payment_mismatch_only(calculation):
        # A stated balance already says what the price must be: offer that rather than ask for it.
        return fix.question, [(reply_id("settle", submission_id, revision, "yes"), fix.accept_label), (reply_id("settle", submission_id, revision, "keep"), fix.keep_label), correct]
    if not calculation.can_export:
        target = typed_value_needed(calculation, draft)
        if target:
            return question_for(draft, target), [correct, cancel]
        if _no_items_only(calculation):
            return _no_items_question(draft, calculation), [correct, cancel]
        return "Some details need fixing before this can be saved. Tell me what's right, or tap *Correct*.", [correct, cancel]
    if fix is not None:
        buttons: list[tuple[str, str]] = []
        if fix.accept_label:
            buttons.append((reply_id("settle", submission_id, revision, "yes"), fix.accept_label))
        buttons.append((reply_id("settle", submission_id, revision, "keep"), fix.keep_label))
        buttons.append(correct)
        return fix.question, buttons
    return CONFIRM_QUESTION, [(reply_id("ok", submission_id, revision), "Confirm"), correct, cancel]


def ready_to_save(draft: TransactionDraft, calculation: Calculation) -> bool:
    """Saving needs a complete calculation and no stated figure left disagreeing with it."""
    return calculation.can_export and proposal(draft) is None


def _with_question(body: str, question: str, buttons: list[tuple[str, str]], *, footer: str = "") -> list[dict[str, Any]]:
    combined = f"{body}\n\n{question}"
    if len(combined) <= BUTTON_BODY_LIMIT:
        return [button_message(combined, buttons, footer=footer)]
    return [text_message(body), button_message(question, buttons, footer=footer)]


def draft_messages(
    draft: TransactionDraft,
    *,
    submission_id: str,
    revision: int,
    review: Mapping[str, Any],
    edited: Sequence[str],
    lead: str = "",
) -> list[dict[str, Any]]:
    """The whole draft and the question that goes with it."""
    calculation = calculate(draft)
    body = describe(draft, calculation, review, edited)
    question, buttons = next_step(draft, calculation, submission_id=submission_id, revision=revision)
    return _with_question(f"{lead}\n\n{body}" if lead else body, question, buttons)


def update_messages(
    draft: TransactionDraft,
    *,
    submission_id: str,
    revision: int,
    review: Mapping[str, Any],
    edited: Sequence[str],
    notes: Sequence[str],
    problems: Sequence[str] = (),
    lead: str = "✅ *Updated*",
) -> list[dict[str, Any]]:
    """A short reply after a correction: what changed, the money summary and the next question."""
    calculation = calculate(draft)
    currency = get_currency(draft.currency)
    lines = [lead, *[f"• {note}" for note in notes]]
    if problems:
        lines += ["", *[f"⚠️ {problem}" for problem in problems]]
    lines += ["", *_money_block(draft, calculation, currency, edited, detailed=False)]
    lines += _problems(draft, calculation, review, edited, limit=2)
    question, buttons = next_step(draft, calculation, submission_id=submission_id, revision=revision)
    return _with_question("\n".join(lines), question, buttons, footer=SHOW_HINT)


def question_messages(
    draft: TransactionDraft, *, submission_id: str, revision: int, lead: str
) -> list[dict[str, Any]]:
    """Just a short message and the next question, when nothing changed."""
    question, buttons = next_step(draft, calculate(draft), submission_id=submission_id, revision=revision)
    return _with_question(lead, question, buttons, footer=SHOW_HINT)


def current_question(draft: TransactionDraft, pending_target: str = "") -> str:
    """The question the customer is answering, in words, for the AI's context."""
    if pending_target:
        from app.whatsapp.corrections import prompt_for

        return prompt_for(draft, pending_target)
    question, _ = next_step(draft, calculate(draft), submission_id="", revision=0)
    return question.replace("*", "")


def receipt_caption(reference: str, draft: TransactionDraft) -> str:
    """The words under the receipt image, which stay with it when it's forwarded."""
    calculation = calculate(draft)
    currency = get_currency(draft.currency)
    return (
        f"🧾 *Receipt {reference}*\n\n"
        f"*Total:* {money(calculation.total, currency)}\n"
        f"*Paid:* {money(calculation.amount_paid, currency)}\n"
        f"*Remaining:* {money(calculation.outstanding, currency)}\n"
        f"{payment_line(draft.payment_status)}\n\n"
        f"{NOT_VERIFIED}"
    )


def saved_text(reference: str) -> str:
    return f"✅ Saved as *{reference}*.\n\nForward the receipt above to the other person, or tap *Get PDF* for a copy you can print."


def recent_text(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = ["*Your recent records*"]
    for number, row in enumerate(rows, start=1):
        currency = get_currency(row["currency"])
        when = format_date(row["transaction_date"]) if row["transaction_date"] else format_date(row["created_at"].date())
        people = " · ".join(part for part in (row["seller"], row["buyer"]) if part)
        status = STATUS_LABELS[PaymentStatus(row["payment_status"])]
        lines.append("")
        lines.append(f"{number}. *{row['reference']}* · {when}")
        if people:
            lines.append(f"    {people}")
        lines.append(f"    {money(row['total'], currency)} · {status}")
    return "\n".join(lines)
