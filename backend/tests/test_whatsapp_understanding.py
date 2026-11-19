"""Reading what people type about a draft, and keeping its figures consistent. No database or AI."""

from __future__ import annotations

from typing import Any

import pytest

from app.calculations import calculate
from app.extraction.provider import AIProviderError
from app.models import PaymentStatus, TransactionDraft
from app.whatsapp.reconcile import KEEP_LABEL, accept, proposal, settle_status
from app.whatsapp.summary import open_reasons, update_messages
from app.whatsapp.understanding import (
    AIChange,
    amount_reply,
    apply_changes,
    correction_request,
    item_reply,
    parse_correction,
)


def draft(**values: Any) -> TransactionDraft:
    base: dict[str, Any] = {
        "currency": "NGN",
        "seller": "Bola Studio",
        "buyer": "Emeka Daniel Obi",
        "transaction_date": "2026-11-10",
        "line_items": [{"id": "item-1", "description": "Transfer", "quantity": "1", "unit_price": "20000"}],
        "amount_paid": "20000",
        "payment_status": "paid",
    }
    base.update(values)
    return TransactionDraft.model_validate(base)


def change(field: str, value: str = "", **extra: str) -> AIChange:
    return AIChange(field=field, value=value, **extra)


# -- short replies that need no AI --------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("20k", "20000"), ("20 thousand", "20000"), ("1.5k", "1500"), ("250k", "250000"), ("₦20,000", "20000"),
        ("20,000 naira", "20000"), ("It's 20k", "20000"), ("It’s 20k", "20000"), ("20k is the price", "20000"),
        ("the price is 20k", "20000"), ("#20k", "20000"), ("₦ 20,000", "20000"), ("20000", "20000"),
        ("ok 20k", "20000"), ("na 20k abeg", "20000"),
    ],
)
def test_amounts_said_in_ordinary_words(typed: str, expected: str) -> None:
    assert amount_reply(typed) == expected


@pytest.mark.parametrize(
    "typed",
    [
        "It's a 20k for advance work", "-500", "10%", "10 percent", "2 for 15k", "seventeen", "40k was still remaining",
        "he paid 20k", "20k and 5k", "",
    ],
)
def test_replies_that_are_more_than_an_amount_are_left_for_the_ai(typed: str) -> None:
    assert amount_reply(typed) is None


def test_an_item_with_its_price() -> None:
    assert item_reply("Logo design, 60k") == ("Logo design", "60000")
    assert item_reply("It's for a carpentry job 45k") == ("carpentry job", "45000")
    assert item_reply("he paid 20k") is None
    assert item_reply("20k") is None
    assert item_reply("40k remaining") is None


# -- the payment status the figures imply -------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "paid", "status", "expected"),
    [
        ("60000", "20000", "paid", "partially_paid"),
        ("60000", "60000", "partially_paid", "paid"),
        ("60000", "0", "not_specified", "unpaid"),
        ("60000", "", "paid", "paid"),  # "paid" with no amount means the whole total
        ("60000", "20000", "refunded", "refunded"),
    ],
)
def test_settled_status(price: str, paid: str, status: str, expected: str) -> None:
    items = [{"id": "item-1", "description": "Work", "quantity": "1", "unit_price": price}]
    assert settle_status(draft(line_items=items, amount_paid=paid, payment_status=status)).payment_status.value == expected


# -- a stated balance that disagrees with the figures ------------------------------------------


def test_a_payment_read_as_the_price_is_caught_by_the_balance_mentioned() -> None:
    # The real conversation: a ₦20,000 transfer became the price, and the customer said 40k remained.
    current = draft(stated_outstanding="40000")
    assert any(issue.code == "stated_outstanding_mismatch" for issue in calculate(current).issues)
    fix = proposal(current)
    assert fix is not None
    assert "₦60,000.00" in fix.question and "₦20,000.00 paid" in fix.question and "₦40,000.00 still owed" in fix.question
    assert fix.accept_label == "Yes, ₦60,000" and fix.keep_label == KEEP_LABEL
    fixed = settle_status(accept(current, fix))
    calculation = calculate(fixed)
    assert (calculation.total, calculation.amount_paid, calculation.outstanding) == ("60000.00", "20000.00", "40000.00")
    assert fixed.payment_status == PaymentStatus.PARTIALLY_PAID
    assert proposal(fixed) is None


def test_with_no_payment_recorded_the_balance_tells_what_was_paid() -> None:
    current = draft(
        line_items=[{"id": "item-1", "description": "Work", "quantity": "1", "unit_price": "60000"}],
        amount_paid="",
        payment_status="not_specified",
        stated_outstanding="40000",
    )
    fix = proposal(current)
    assert fix is not None and fix.changes == {"amount_paid": "20000"}
    assert "₦20,000.00" in fix.question


def test_a_disagreement_with_no_safe_fix_asks_which_is_right() -> None:
    items = [{"id": "a", "description": "Bag", "quantity": "1", "unit_price": "30000"}, {"id": "b", "description": "Belt", "quantity": "1", "unit_price": "5000"}]
    fix = proposal(draft(line_items=items, amount_paid="", payment_status="not_specified", stated_total="40000"))
    assert fix is not None and not fix.changes and "Which is right" in fix.question


def test_figures_that_agree_need_no_question() -> None:
    items = [{"id": "item-1", "description": "Work", "quantity": "1", "unit_price": "60000"}]
    assert proposal(draft(line_items=items, payment_status="partially_paid", stated_outstanding="40000")) is None


# -- applying what the AI reported -------------------------------------------------------------


def test_three_figures_in_one_message() -> None:
    # "The full work was 60k. He paid 20k and still owes 40k."
    applied = apply_changes(draft(), [change("total", "60k"), change("amount_paid", "20k"), change("balance", "40k")])
    assert applied.problems == []
    fixed = settle_status(applied.draft)
    calculation = calculate(fixed)
    assert (calculation.total, calculation.amount_paid, calculation.outstanding) == ("60000.00", "20000.00", "40000.00")
    assert fixed.payment_status == PaymentStatus.PARTIALLY_PAID
    assert proposal(fixed) is None
    assert "Full price of “Transfer”: ₦60,000.00" in applied.notes


def test_several_fields_in_one_message() -> None:
    # "Change the buyer to Emeka, the total to 60k, and mark 20k as paid."
    applied = apply_changes(draft(), [change("buyer", "Emeka"), change("total", "60k"), change("amount_paid", "20k")])
    assert applied.draft.buyer == "Emeka"
    assert applied.draft.line_items[0].unit_price == "60000" and applied.draft.amount_paid == "20000"
    assert set(applied.edited) >= {"buyer", "line_items.item-1.unit_price", "amount_paid"}


def test_a_status_and_a_part_payment_in_the_same_message_keep_the_amount() -> None:
    items = [{"id": "item-1", "description": "Work", "quantity": "1", "unit_price": "60000"}]
    applied = apply_changes(draft(line_items=items, amount_paid="", payment_status="unpaid"), [change("amount_paid", "20k"), change("payment_status", "partially_paid")])
    assert applied.draft.amount_paid == "20000" and applied.draft.payment_status == PaymentStatus.PARTIALLY_PAID
    assert applied.status_set


def test_item_changes_add_and_remove() -> None:
    applied = apply_changes(
        draft(),
        [change("item_description", "Advance work", item_id="item-1"), change("add_item", "Paint", quantity="2", price="5k")],
    )
    assert [row.description for row in applied.draft.line_items] == ["Advance work", "Paint"]
    assert applied.draft.line_items[1].unit_price == "5000" and applied.draft.line_items[1].quantity == "2"
    removed = apply_changes(applied.draft, [change("remove_item", item_id="item-2")])
    assert [row.description for row in removed.draft.line_items] == ["Advance work"]


def test_values_that_cant_be_used_are_reported_not_guessed() -> None:
    two = [{"id": "a", "description": "Bag", "quantity": "1", "unit_price": "3000"}, {"id": "b", "description": "Belt", "quantity": "1", "unit_price": "2000"}]
    applied = apply_changes(draft(line_items=two), [change("item_price", "9k", item_id="nope"), change("amount_paid", "lots"), change("balance", "-5")])
    assert applied.draft == draft(line_items=two)
    assert len(applied.problems) == 3


def test_a_total_with_several_items_is_kept_to_check_them_against() -> None:
    two = [{"id": "a", "description": "Bag", "quantity": "1", "unit_price": "3000"}, {"id": "b", "description": "Belt", "quantity": "1", "unit_price": "2000"}]
    applied = apply_changes(draft(line_items=two, amount_paid="", payment_status="not_specified"), [change("total", "6k")])
    assert applied.draft.stated_total == "6000"
    assert [row.unit_price for row in applied.draft.line_items] == ["3000", "2000"]  # never changed to fit


def test_unknown_changes_and_intents_from_the_ai_are_dropped() -> None:
    parsed = parse_correction({"intent": "launch", "changes": [{"field": "bank_account", "value": "x"}, {"field": "buyer", "value": "Ade"}], "resolved_checks": [], "question": "*Which?*"})
    assert parsed.intent == "unclear" and [c.field for c in parsed.changes] == ["buyer"] and parsed.question == "Which?"
    with pytest.raises(AIProviderError):
        parse_correction({"changes": "not a list"})


def test_the_request_gives_the_ai_the_record_and_the_question_being_answered() -> None:
    current = draft()
    request = correction_request(
        current, calculate(current), checks=["The buyer said No to ₦20,000."], question="What was the full price?", earlier=["40k remains"], message="It's 20k"
    )
    text = request.parts[0].text  # type: ignore[union-attr]
    assert '"id": "item-1"' in text and "1. The buyer said No to ₦20,000." in text
    assert "What was the full price?" in text and "- 40k remains" in text and text.endswith("It's 20k")
    assert request.task == "whatsapp_correction" and "never do arithmetic" in request.system


# -- what the customer sees --------------------------------------------------------------------


def test_doubts_about_a_figure_go_once_that_figure_is_corrected() -> None:
    review = {"reasons": [{"field": "stated_total", "message": "The buyer said No to ₦20,000."}, {"field": "seller", "message": "Check the seller."}]}
    assert [message for _, message in open_reasons(review, [])] == ["The buyer said No to ₦20,000.", "Check the seller."]
    assert [message for _, message in open_reasons(review, ["line_items.item-1.description"])] == ["The buyer said No to ₦20,000.", "Check the seller."]
    assert [message for _, message in open_reasons(review, ["line_items.item-1.unit_price"])] == ["Check the seller."]
    assert open_reasons({**review, "resolved": [1]}, ["amount_paid"]) == [(0, "The buyer said No to ₦20,000.")]


def test_an_update_is_short_and_ends_with_the_next_question() -> None:
    current = draft(line_items=[{"id": "item-1", "description": "Advance work", "quantity": "1", "unit_price": "60000"}], payment_status="partially_paid")
    [message] = update_messages(current, submission_id="6f1f0a52-6c11-4f4e-9a55-1f0f6c2a9b10", revision=3, review={}, edited=["line_items.item-1.unit_price"], notes=["Price of “Advance work”: ₦60,000.00"])
    body = message["interactive"]["body"]["text"]
    assert body.startswith("✅ *Updated*\n• Price of “Advance work”: ₦60,000.00")
    assert "Remaining" in body and "₦40,000.00" in body and "🟡 Partially paid" in body
    assert "*Seller:*" not in body  # not the whole record again
    assert body.endswith("Is this correct?")
    assert {button["reply"]["title"] for button in message["interactive"]["action"]["buttons"]} == {"Confirm", "Correct", "Cancel"}


# -- dates typed in ordinary words -------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("today", "2026-11-19"), ("Yesterday.", "2026-11-18"), ("it was yesterday", "2026-11-18"), ("monday", "2026-11-16"),
        ("last thursday", "2026-11-12"), ("3 days ago", "2026-11-16"), ("10 Nov", "2026-11-10"), ("nov 10", "2026-11-10"),
        ("10/11", "2026-11-10"), ("on 10th November", "2026-11-10"), ("25 Dec", "2025-12-25"), ("18/11/2026", "2026-11-18"),
        ("18.11.2026", "2026-11-18"), ("2026-11-18", "2026-11-18"),
    ],
)
def test_dates_people_type(typed: str, expected: str) -> None:
    from datetime import date

    from app.whatsapp.corrections import read_date

    assert read_date(typed, date(2026, 11, 19)) == expected  # a Thursday


@pytest.mark.parametrize(("typed", "expected"), [("friday", "2026-11-20"), ("tomorrow", "2026-11-20"), ("10 Jan", "2027-01-10"), ("next thursday", "2026-11-26")])
def test_due_dates_look_forward(typed: str, expected: str) -> None:
    from datetime import date

    from app.whatsapp.corrections import read_date

    assert read_date(typed, date(2026, 11, 19), future=True) == expected


# -- common messages read without the AI --------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "field", "value"),
    [
        ("The buyer paid 35k", "amount_paid", "35000"), ("him don pay 20k", "amount_paid", "20000"),
        ("He still owes me 40k", "balance", "40000"), ("40k was still remaining", "balance", "40000"), ("balance: 40k", "balance", "40000"),
        ("35k is the final agreed price", "total", "35000"), ("The full price is 60k", "total", "60000"), ("na 60k be the price", "total", "60000"),
        ("the buyer is Emeka Obi", "buyer", "Emeka Obi"), ("seller: Bola Studio", "seller", "Bola Studio"),
    ],
)
def test_common_messages_need_no_ai(typed: str, field: str, value: str) -> None:
    from app.whatsapp.understanding import quick_changes

    changes = quick_changes(typed)
    assert changes is not None and [(c.field, c.value) for c in changes] == [(field, value)]


@pytest.mark.parametrize("typed", ["The buyer is who stated I don send the money", "It's an advance work. 40k was still remaining", "thanks", "show"])
def test_anything_else_is_left_for_the_ai(typed: str) -> None:
    from app.whatsapp.understanding import quick_changes

    assert quick_changes(typed) is None


def test_a_sentence_is_never_stored_as_a_name() -> None:
    # The second real test: the AI read "The buyer is who stated I don send the money" as a new name.
    applied = apply_changes(draft(), [change("buyer", "who stated I don send the money")])
    assert applied.draft.buyer == "Emeka Daniel Obi"
    assert applied.problems and "doesn't look like a name" in applied.problems[0]
