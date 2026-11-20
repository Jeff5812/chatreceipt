"""The WhatsApp channel's pure logic: payloads, limits, typed values."""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models import TransactionDraft
from app.observability import configure_logging
from app.whatsapp.client import signature_is_valid
from app.whatsapp.conversation import command_of
from app.whatsapp.corrections import Menus, apply_text, read_amount, read_date
from app.whatsapp.events import parse_webhook
from app.whatsapp.messages import (
    BUTTON_BODY_LIMIT,
    BUTTON_TITLE_LIMIT,
    ROW_DESCRIPTION_LIMIT,
    ROW_ID_LIMIT,
    ROW_TITLE_LIMIT,
    Row,
    button_message,
    list_message,
    parse_reply_id,
    reply_id,
)
from app.whatsapp.summary import TABLE_WIDTH, amount_table, draft_messages
from tests.samples import valid_draft

SUBMISSION = "6f1f0a52-6c11-4f4e-9a55-1f0f6c2a9b10"


def delivery(*messages: dict[str, Any], **value: Any) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": "1"}, "messages": list(messages), **value}}]}],
    }


def test_amounts_line_up_and_fit_a_phone_screen() -> None:
    table = amount_table([("Subtotal", "₦30,000.00"), None, ("TOTAL", "₦32,500.00"), ("Loyalty discount for regular customers", "−₦1,000.00")])
    assert table.startswith("```") and table.endswith("```")
    lines = table.strip("`").splitlines()
    assert all(len(line) == TABLE_WIDTH for line in lines[:3])
    assert lines[0].endswith("₦30,000.00") and lines[2].endswith("₦32,500.00")
    assert lines[3].endswith("…") and len(lines[3]) == TABLE_WIDTH
    assert lines[4].endswith("−₦1,000.00") and len(lines[4]) == TABLE_WIDTH


def test_a_label_cannot_break_out_of_the_amount_table() -> None:
    assert amount_table([("Fee```*bold*", "₦1.00")]).count("```") == 2


def test_the_summary_shows_amounts_in_a_table_and_the_payment_status() -> None:
    body = draft_messages(draft(), submission_id=SUBMISSION, revision=1, review={}, edited=[])[0]["interactive"]["body"]["text"]
    assert "```" in body and "Agreed total" in body and "🔴 Unpaid" in body and "doesn't verify payments" in body


def test_access_logs_never_contain_the_webhook_verify_token() -> None:
    configure_logging()
    path = "/api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=secret-value&hub.challenge=1"
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", path, "1.1", 200), None
    )
    assert logging.getLogger("uvicorn.access").filter(record)
    assert "secret-value" not in record.getMessage()
    assert "/api/whatsapp/webhook" in record.getMessage()


def test_signature_check() -> None:
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert signature_is_valid(body, good, "secret")
    assert not signature_is_valid(body + b" ", good, "secret")
    assert not signature_is_valid(body, good, "other")
    assert not signature_is_valid(body, None, "secret")
    assert not signature_is_valid(body, good, "")


def test_parses_the_message_types_the_channel_uses() -> None:
    base = {"from": "2348000000001", "from_user_id": "NG.1", "timestamp": "1790000000"}
    parsed = parse_webhook(
        delivery(
            {**base, "id": "a", "type": "text", "text": {"body": "  hello "}},
            {**base, "id": "b", "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg; codecs=x", "sha256": "h"}},
            {**base, "id": "c", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "cr|ok||1|", "title": "Confirm"}}},
            {**base, "id": "d", "type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "cr|fix||1|", "title": "x"}}},
            {**base, "id": "e", "type": "document", "document": {"id": "m2", "mime_type": "image/png"}},
            {**base, "id": "f", "type": "document", "document": {"id": "m3", "mime_type": "application/pdf"}},
            {**base, "id": "g", "type": "sticker", "sticker": {"id": "m4"}},
            {**base, "id": "h", "type": "reaction", "reaction": {"emoji": "👍"}},
        ),
        phone_number_id="1",
    )
    assert [(m.message_id, m.kind) for m in parsed.messages] == [
        ("a", "text"), ("b", "image"), ("c", "reply"), ("d", "reply"), ("e", "image"), ("f", "unsupported"), ("g", "unsupported"),
    ]
    assert parsed.messages[0].text == "hello"
    assert parsed.messages[1].mime_type == "image/jpeg"
    assert parsed.messages[0].sent_at == datetime.fromtimestamp(1790000000, UTC)
    assert parsed.messages[0].sender.user_id == "NG.1"


def test_identity_falls_back_to_the_contact_block_and_allows_a_missing_phone() -> None:
    parsed = parse_webhook(
        delivery({"id": "a", "type": "text", "text": {"body": "hi"}, "timestamp": "1"}, contacts=[{"user_id": "NG.7"}]),
        phone_number_id="1",
    )
    assert parsed.messages[0].sender.user_id == "NG.7" and parsed.messages[0].sender.phone is None


@pytest.mark.parametrize(
    "broken",
    [
        {"type": "text", "text": {"body": "no sender or id"}},
        {"id": "x", "type": "text", "text": {"body": "no sender"}},
        {"id": "x", "from": "2348000000001", "group_id": "g1", "type": "text", "text": {"body": "group"}},
        "not an object",
    ],
)
def test_malformed_messages_are_skipped_without_raising(broken: Any) -> None:
    parsed = parse_webhook(delivery(broken), phone_number_id="1")
    assert parsed.messages == []


def test_failed_delivery_statuses_are_read() -> None:
    parsed = parse_webhook(
        delivery(statuses=[{"id": "out1", "status": "failed", "errors": [{"code": 131026}]}, {"id": "out2", "status": "read"}]),
        phone_number_id="1",
    )
    assert [(f.message_id, f.code) for f in parsed.failures] == [("out1", 131026)]


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("17000", "17000"), ("17,000", "17000"), ("₦17,000", "17000"), ("N17000", "17000"), ("17k", "17000"), ("2.5k", "2500"),
        ("1.2m", "1200000"), ("$450", "450"), ("17000.50", "17000.5"), ("15000 naira", "15000"),
        ("#15,000", "15000"), ("# 15000", "15000"), ("#15k", "15000"), ("15000 only", "15000"), ("₦15,000 only", "15000"),
        ("15000/=", "15000"), ("15000 each", "15000"), ("15000 per one", "15000"), ("15000.", "15000"),
    ],
)
def test_amounts_people_actually_type(typed: str, expected: str) -> None:
    assert read_amount(typed) == expected


@pytest.mark.parametrize("typed", ["seventeen", "-500", "17,00", "12 or 13", "", "1.2.3"])
def test_amounts_that_cannot_be_read(typed: str) -> None:
    assert read_amount(typed) is None


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("2026-11-18", "2026-11-18"), ("18/11/2026", "2026-11-18"), ("3/4/26", "2026-04-03"), ("18 Nov 2026", "2026-11-18"),
        ("November 18, 2026", "2026-11-18"), ("18th November 2026", "2026-11-18"),
    ],
)
def test_dates_people_actually_type(typed: str, expected: str) -> None:
    assert read_date(typed) == expected


@pytest.mark.parametrize("typed", ["31/02/2026", "tomorrow", "2026", "13/13/2026"])
def test_dates_that_cannot_be_read(typed: str) -> None:
    assert read_date(typed) is None


def draft(**overrides: Any) -> TransactionDraft:
    return TransactionDraft.model_validate(valid_draft(**overrides))


def test_edits_use_the_calculation_rules() -> None:
    assert apply_text(draft(), "line_items.item-1.quantity", "0").error == "The quantity must be more than zero."
    assert "decimal" in apply_text(draft(currency="JPY", line_items=[], delivery_fee=""), "delivery_fee", "10.5").error
    assert apply_text(draft(), "line_items.item-9.quantity", "2").error
    assert apply_text(draft(), "currency", "XYZ").error
    assert apply_text(draft(), "currency", "usd").draft.currency == "USD"  # type: ignore[union-attr]
    assert apply_text(draft(), "delivery_fee", "none").draft.delivery_fee == ""  # type: ignore[union-attr]


def test_discounts_accept_amounts_and_percentages_and_can_be_removed() -> None:
    result = apply_text(draft(), "new:discounts", "10%")
    assert result.draft is not None
    row = result.draft.discounts[0]
    assert (row.kind, row.value, row.label) == ("percent", "10", "Discount")
    assert apply_text(draft(), "new:discounts", "150%").error
    removed = apply_text(result.draft, f"discounts.{row.id}.value", "remove")
    assert removed.draft is not None and removed.draft.discounts == []


def test_cross_field_problems_are_allowed_through_and_shown_on_the_summary() -> None:
    result = apply_text(draft(payment_status="unpaid"), "amount_paid", "5000")
    assert result.draft is not None and not result.error
    messages = draft_messages(result.draft, submission_id=SUBMISSION, revision=2, review={}, edited=["amount_paid"])
    text = str(messages)
    assert "Fix before saving" in text and "Confirm" not in text


def test_reply_ids_round_trip_and_reject_anything_else() -> None:
    parsed = parse_reply_id(reply_id("ok", SUBMISSION, 3, "line_items.item-1.quantity"))
    assert parsed is not None and (parsed.action, parsed.submission_id, parsed.revision, parsed.arg) == ("ok", SUBMISSION, 3, "line_items.item-1.quantity")
    assert parse_reply_id("hello") is None
    assert parse_reply_id("cr|ok|not-a-uuid|1|") is None
    assert parse_reply_id("cr|ok||x|") is None


def test_outgoing_messages_stay_within_whatsapps_limits() -> None:
    button = button_message("x" * 5000, [("a", "A title that is far too long for a button")])["interactive"]
    assert len(button["body"]["text"]) <= BUTTON_BODY_LIMIT
    assert len(button["action"]["buttons"][0]["reply"]["title"]) <= BUTTON_TITLE_LIMIT
    rows = list_message("body", "button", [Row(f"id-{n}", "t" * 40, "d" * 100) for n in range(15)])["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 10
    assert all(len(row["title"]) <= ROW_TITLE_LIMIT and len(row["description"]) <= ROW_DESCRIPTION_LIMIT for row in rows)


def test_every_correction_menu_stays_within_the_row_limits() -> None:
    many = [{"id": f"item-{n}", "description": f"Item number {n} with a long description", "quantity": "1", "unit_price": "1000"} for n in range(1, 26)]
    menus = Menus(draft(line_items=many), SUBMISSION, 12)
    built = [menus.main(), menus.items(), menus.items(1), menus.items(3), menus.item("item-1"), menus.adjustments("discounts"), menus.payment(), menus.currencies(), menus.more()]
    for message in built:
        assert message is not None
        rows = message["interactive"]["action"]["sections"][0]["rows"]
        assert 1 <= len(rows) <= 10
        assert all(len(row["id"]) <= ROW_ID_LIMIT and len(row["title"]) <= ROW_TITLE_LIMIT for row in rows)
    assert any(row["title"] == "More items" for row in menus.items()["interactive"]["action"]["sections"][0]["rows"])


def test_a_long_draft_is_split_so_the_buttons_still_fit() -> None:
    many = [{"id": f"item-{n}", "description": f"Item number {n}", "quantity": "2", "unit_price": "1500"} for n in range(1, 21)]
    messages = draft_messages(draft(line_items=many), submission_id=SUBMISSION, revision=1, review={}, edited=[])
    assert [m["type"] for m in messages] == ["text", "interactive"]
    assert len(messages[0]["text"]["body"]) <= 4096
    assert messages[1]["interactive"]["body"]["text"] == "Is this correct?"


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("Hi ChatReceipt, I'd like to scan a receipt", "greeting"), ("HELLO!", "greeting"), ("recent", "recent"), ("Delete my data.", "erase"),
        ("confirm", "confirm"), ("yes", ""), ("what is this", ""),
    ],
)
def test_commands(text: str, command: str) -> None:
    assert command_of(text) == command


# -- what customers see before they type (app/whatsapp/profile.py) ---------------------------------


def test_every_first_chat_suggestion_and_slash_command_reaches_a_bot_command() -> None:
    from app.whatsapp import profile

    assert {prompt: command_of(prompt) for prompt in profile.PROMPTS} == {
        "Turn a chat into a receipt": "scan",
        "How does ChatReceipt work?": "help",
        "Show my recent records": "recent",
        "Open my records on the web": "web",
    }
    # Picking a command from WhatsApp's "/" menu sends "/name".
    assert {name: command_of(f"/{name}") for name, _ in profile.COMMANDS} == {
        "help": "help", "new": "restart", "show": "show", "retry": "retry", "recent": "recent", "web": "web", "cancel": "cancel",
    }


def test_the_profile_fits_metas_limits() -> None:
    from app.whatsapp import profile
    from scripts.whatsapp_profile import check_limits

    check_limits()
    assert len(profile.ABOUT) <= 139 and len(profile.PROMPTS) <= 4


def test_the_welcome_points_to_the_command_menu() -> None:
    from app.whatsapp.conversation import WELCOME

    assert "Type */*" in WELCOME and "delete my data" in WELCOME


# -- requests in their own words (app/whatsapp/assistant.py) ----------------------------------------


@pytest.mark.parametrize(
    ("text", "intent", "period"),
    [
        ("who still owes me?", "money_owed", "any"),
        ("Who dey owe me", "money_owed", "any"),
        ("how much did I sell this week", "totals", "this_week"),
        ("how many receipts", "totals", "any"),
        ("clear all my records", "delete_data", "any"),
        ("show my receipts", "list_records", "any"),
        ("thank you", "thanks", "any"),
    ],
)
def test_common_requests_are_read_without_the_ai(text: str, intent: str, period: str) -> None:
    from app.whatsapp.assistant import quick_request

    request = quick_request(text)
    assert request is not None and (request.intent, request.period) == (intent, period)


def test_a_reference_in_a_message_finds_or_resends_that_record() -> None:
    from app.whatsapp.assistant import Request, quick_request

    assert quick_request("resend CR-000012 as pdf") == Request("resend_receipt", reference="CR-000012", format="pdf")
    assert quick_request("what about CR000012") == Request("find_records", reference="CR-000012")


@pytest.mark.parametrize("text", ["he still owes 40k", "who still owes me?", "thanks", "it's 20k", "the buyer is Emeka"])
def test_with_a_draft_open_anything_that_could_be_a_correction_is_left_to_the_draft(text: str) -> None:
    from app.whatsapp.assistant import quick_request

    assert quick_request(text, has_draft=True) is None


def test_messages_that_arent_requests_are_left_for_the_ai() -> None:
    from app.whatsapp.assistant import quick_request

    assert quick_request("abeg show me wetin Ada buy") is None
    assert quick_request("   ") is None


def test_the_ais_wording_is_dropped_when_it_states_a_figure_or_a_verification() -> None:
    from app.whatsapp.understanding import safe_reply

    assert safe_reply("Got it, the buyer is Emeka.", "change the buyer to Emeka") == "Got it, the buyer is Emeka."
    assert safe_reply("Done, 20k is noted.", "he paid 20k") == "Done, 20k is noted."
    # Figures in replies come from ChatReceipt's own records, never the model, even when they look the same.
    assert safe_reply("He paid ₦20,000.", "he paid 20k") == ""
    assert safe_reply("Zainab owes you 5000.", "who owes me") == ""
    assert safe_reply("That's ₦ noted", "noted") == ""
    assert safe_reply("Payment verified ✅", "he paid") == ""
    assert safe_reply("", "anything") == ""


def test_a_name_or_reference_the_message_doesnt_contain_is_never_searched_for() -> None:
    from app.whatsapp.assistant import request_from
    from app.whatsapp.understanding import AICorrection

    asked = "show me what Ada bought"
    assert request_from(AICorrection(intent="find_records", person="Ada"), asked).person == "Ada"
    assert request_from(AICorrection(intent="find_records", person="Zainab"), asked).person == ""
    assert request_from(AICorrection(intent="resend_receipt", reference="CR-000009"), asked).reference == ""
    assert request_from(AICorrection(intent="resend_receipt", reference="cr000009"), "send CR-000009").reference == "CR-000009"


def test_periods_cover_whole_days_across_month_and_year_ends() -> None:
    from datetime import date

    from app.whatsapp.assistant import period_range

    wednesday = date(2026, 1, 7)
    assert period_range("today", wednesday) == (wednesday, wednesday)
    assert period_range("this_week", wednesday) == (date(2026, 1, 5), wednesday)
    assert period_range("last_week", wednesday) == (date(2025, 12, 29), date(2026, 1, 4))
    assert period_range("last_month", wednesday) == (date(2025, 12, 1), date(2025, 12, 31))
    assert period_range("this_year", wednesday) == (date(2026, 1, 1), wednesday)
    assert period_range("any", wednesday) == (None, None)


def test_amounts_owed_and_totals_are_added_up_per_currency() -> None:
    from decimal import Decimal

    from app.whatsapp.assistant import owed_text, totals_text

    rows = [
        {"buyer": "Ada", "seller": "", "currency": "NGN", "reference": "CR-000001", "total": Decimal("20000"), "amount_paid": Decimal("15000"), "outstanding": Decimal("5000")},
        {"buyer": "", "seller": "Tunde", "currency": "USD", "reference": "CR-000002", "total": Decimal("12.5"), "amount_paid": None, "outstanding": Decimal("12.5")},
        {"buyer": "Ada", "seller": "", "currency": "NGN", "reference": "CR-000003", "total": Decimal("2500"), "amount_paid": Decimal("0"), "outstanding": Decimal("2500")},
    ]
    assert owed_text(rows, "this week").splitlines() == [
        "*Still owed this week*",
        "• Ada: ₦5,000.00 (CR-000001)",
        "• Tunde: $12.50 (CR-000002)",
        "• Ada: ₦2,500.00 (CR-000003)",
        "",
        "Total still owed: ₦7,500.00, $12.50",
    ]
    assert totals_text(rows, "").splitlines() == [
        "*Your records* · 3 records",
        "Agreed ₦22,500.00 · paid ₦15,000.00 · still owed ₦7,500.00",
        "Agreed $12.50 · paid $0.00 · still owed $12.50",
    ]
