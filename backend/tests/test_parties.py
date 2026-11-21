"""Who is who on a receipt: the pure rules in parties.py, names.py and the webhook parser.

The rules decide what to fill and what to ask without the conversation around them, so each case
is a draft, the state the extraction left, and what the customer confirmed before.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.extraction.schema import AIExtraction
from app.extraction.service import build_extraction_response
from app.models import TransactionDraft
from app.names import clean_name, name_key, phone_in_label, same_name
from app.whatsapp import parties
from app.whatsapp.assistant import owed_text
from app.whatsapp.events import parse_webhook
from tests.samples import bag_conversation_output, field, valid_draft


def draft(**overrides: Any) -> TransactionDraft:
    return TransactionDraft.model_validate(valid_draft(**overrides))


def remembered(name: str, how: str = "label") -> tuple[dict[str, Any], str]:
    return {"id": "8d1e2f3a-0000-4000-8000-000000000001", "name": name, "phone": None}, how


# -- reading the model's answer --------------------------------------------------------------------------


def mapped(**overrides: Any) -> Any:
    return build_extraction_response(AIExtraction.model_validate(bag_conversation_output(**overrides)), screenshots=1, images_sent=1)


def test_the_name_at_the_top_of_the_chat_goes_on_the_side_the_owner_isnt_on() -> None:
    response = mapped(owner_role=field("buyer", quote="Okay, I will take it."), other_party=field("Tola's Bags", quote="Tola's Bags"))
    assert (response.draft.seller, response.draft.buyer) == ("Tola's Bags", "")
    assert response.parties.owner_role == "buyer" and response.field_meta["seller"].note == "The name at the top of the chat."


def test_the_owner_given_the_other_persons_name_is_flagged_not_moved() -> None:
    response = mapped(
        owner_role=field("seller", quote="17k final."), other_party=field("Ada Obi", quote="Ada Obi"), seller=field("Ada Obi", quote="Ada Obi"),
    )
    assert response.draft.seller == "Ada Obi" and response.parties.owner_role == ""
    assert any("wrong way round" in reason.message for reason in response.review_reasons)


def test_an_unsure_reading_of_the_owners_side_is_not_used() -> None:
    response = mapped(owner_role=field("seller", confidence="low", quote="17k final."), other_party=field("Ada Obi", quote="Ada Obi"))
    assert response.parties.owner_role == "" and response.draft.buyer == ""
    assert response.parties.other_party == "Ada Obi"  # kept, to ask which side the customer is on


def test_a_total_the_chat_never_stated_isnt_a_check_but_conflicting_figures_are() -> None:
    unstated = [
        {"field": "agreed_total", "description": "The explicit agreed total is not stated in the conversation, only the item price and delivery fee."},
        {"field": "outstanding_amount", "description": "The outstanding amount isn't mentioned."},
    ]
    assert mapped(ambiguities=unstated).review_reasons == []
    conflict = [{"field": "agreed_total", "description": "The seller says the total is ₦20,000 but the buyer says ₦25,000."}]
    assert [reason.message for reason in mapped(ambiguities=conflict).review_reasons] == [conflict[0]["description"]]
    # A total the chat did state, called unclear, is still for the person to check.
    stated = [{"field": "agreed_total", "description": "The total stated isn't explicit about delivery."}]
    assert len(mapped(agreed_total=field("19000", quote="Total 19k"), ambiguities=stated).review_reasons) == 1


def test_an_answer_from_before_these_fields_existed_still_reads() -> None:
    legacy = bag_conversation_output()
    response = build_extraction_response(AIExtraction.model_validate(legacy), screenshots=1, images_sent=1)
    assert response.parties.owner_role == "" and response.other_deals == [] and response.parties.people == []


# -- asking ---------------------------------------------------------------------------------------------


def test_nothing_is_asked_when_both_names_are_known() -> None:
    assert parties.question_needed(draft(), {}, saved_name=None, skipped=0) == ""


def test_the_role_is_asked_when_the_chat_doesnt_show_which_side_the_customer_is_on() -> None:
    blank = draft(seller="", buyer="")
    assert parties.question_needed(blank, {"other_party": "Ada Obi"}, saved_name=None, skipped=0) == "role"
    # Asked once per receipt, and never after "Neither".
    assert parties.question_needed(blank, {"other_party": "Ada Obi", "asked": "role"}, saved_name=None, skipped=0) == ""
    assert parties.question_needed(blank, {"neither": True}, saved_name=None, skipped=0) == ""


def test_the_name_is_asked_only_while_the_customer_hasnt_declined_twice() -> None:
    their_side_blank = draft(seller="")
    state = {"owner_role": "seller", "other_party": "Ada"}
    assert parties.question_needed(their_side_blank, state, saved_name=None, skipped=0) == "name"
    assert parties.question_needed(their_side_blank, state, saved_name=None, skipped=1) == "name"
    assert parties.question_needed(their_side_blank, state, saved_name=None, skipped=2) == ""


def test_a_saved_name_or_a_named_side_means_no_name_question() -> None:
    assert parties.question_needed(draft(seller=""), {"owner_role": "seller"}, saved_name="Tola's Bags", skipped=0) == ""
    assert parties.question_needed(draft(buyer=""), {"owner_role": "seller"}, saved_name=None, skipped=0) == ""  # their side has a name


# -- filling --------------------------------------------------------------------------------------------


def test_the_saved_name_goes_on_the_customers_side_only_when_the_side_is_known() -> None:
    blank = draft(seller="")
    filled, state = parties.fill_saved_name(blank, {"owner_role": "seller"}, "Tola's Bags")
    assert filled.seller == "Tola's Bags" and state["you"] == "seller"
    unchanged, state = parties.fill_saved_name(blank, {"owner_role": ""}, "Tola's Bags")
    assert unchanged.seller == "" and "you" not in state


def test_the_saved_name_replaces_the_customers_own_export_label_but_not_another_name() -> None:
    from_export = draft(seller="Tola A.")
    filled, _ = parties.fill_saved_name(from_export, {"owner_role": "seller"}, "Tola's Bags", owner_label="Tola A.")
    assert filled.seller == "Tola's Bags"
    other_name = draft(seller="Tola Ventures Ltd")
    kept, state = parties.fill_saved_name(other_name, {"owner_role": "seller"}, "Tola's Bags", owner_label="Tola A.")
    assert kept.seller == "Tola Ventures Ltd" and "you" not in state


def test_answering_the_role_puts_the_chats_name_on_the_other_side() -> None:
    blank = draft(seller="", buyer="")
    placed, state, moved = parties.apply_role(blank, {"other_party": "Ada Obi"}, "seller")
    assert (placed.seller, placed.buyer, state["owner_role"], moved) == ("", "Ada Obi", "seller", [])


def test_names_read_the_wrong_way_round_are_swapped_when_the_customer_says_their_side() -> None:
    backwards = draft(seller="Ada Obi", buyer="Tola's Bags")
    placed, _, moved = parties.apply_role(backwards, {"other_party": "Ada Obi"}, "seller")
    assert (placed.seller, placed.buyer) == ("Tola's Bags", "Ada Obi") and moved == ["seller", "buyer"]


# -- remembering the other person ------------------------------------------------------------------------


def test_a_remembered_name_replaces_the_chats_label() -> None:
    labelled = draft(buyer="Mummy Ada")
    state = {"owner_role": "seller", "other_party": "Mummy Ada"}
    filled, state = parties.recall_counterparty(labelled, state, remembered("Ada Obi"))
    assert filled.buyer == "Ada Obi" and state["counterpart"]["was"] == "Mummy Ada"
    assert parties.marker(state, "buyer", edited=False) == " _(from your earlier receipts)_"
    assert parties.identity_checks(state) == []  # only the label changed: nothing to check


def test_a_label_match_never_replaces_a_name_the_conversation_gave() -> None:
    named = draft(buyer="Chika Eze")
    state = {"owner_role": "seller", "other_party": "Mummy Ada"}
    kept, state = parties.recall_counterparty(named, state, remembered("Ada Obi"))
    assert kept.buyer == "Chika Eze" and "counterpart" not in state
    [check] = parties.identity_checks(state)
    assert check["field"] == "buyer" and "“Ada Obi”" in check["message"]


def test_a_phone_match_uses_the_confirmed_name_and_points_out_the_chats_other_name() -> None:
    named = draft(buyer="Ngozi Okafor")
    state = {"owner_role": "seller", "other_party": "+234 803 123 4567"}
    filled, state = parties.recall_counterparty(named, state, remembered("Emeka Obi", "phone"))
    assert filled.buyer == "Emeka Obi"
    [check] = parties.identity_checks(state)
    assert "“Emeka Obi”, but the chat says “Ngozi Okafor”" in check["message"]


def test_nothing_is_recalled_without_knowing_the_customers_side() -> None:
    labelled = draft(buyer="Mummy Ada")
    unchanged, state = parties.recall_counterparty(labelled, {"owner_role": "", "other_party": "Mummy Ada"}, remembered("Ada Obi"))
    assert unchanged.buyer == "Mummy Ada" and state == {"owner_role": "", "other_party": "Mummy Ada"}


def test_what_is_remembered_after_saving() -> None:
    state = {"owner_role": "buyer", "other_party": "Kemi", "other_phone": "2348031234567", "counterpart": {"id": "abc"}}
    assert parties.counterpart_to_remember(draft(seller="Kemi Fabrics"), state) == {
        "name": "Kemi Fabrics", "phone": "2348031234567", "label": "Kemi", "role": "seller", "counterparty_id": "abc", "username": "",
    }
    assert parties.counterpart_to_remember(draft(), {"owner_role": ""}) is None  # neither side, or not known
    assert parties.counterpart_to_remember(draft(buyer=""), {"owner_role": "seller"}) is None  # no name to remember


# -- reading what the customer types -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "name"),
    [
        ("my name is Tola's Bags", "Tola's Bags"),
        ("My business name is Ada Couture.", "Ada Couture"),
        ("change my name to Mama Nkechi", "Mama Nkechi"),
        ("use Bisi Foods as my name", "Bisi Foods"),
    ],
)
def test_stated_names(typed: str, name: str) -> None:
    assert parties.stated_name(typed) == name


@pytest.mark.parametrize(
    "typed",
    [
        "my name is", "my name is what?", "the price is 20k", "my name is 12345", "call me later",
        "my name is not on the receipt", "my name is wrong", "my name is spelt wrongly",
    ],
)
def test_messages_that_dont_state_a_name(typed: str) -> None:
    assert parties.stated_name(typed) is None


@pytest.mark.parametrize(
    ("typed", "name"),
    [("Tola's Bags", "Tola's Bags"), ("It’s Ada Couture", "Ada Couture"), ("I'm Emeka", "Emeka"), ("*Kemi Fabrics*", "Kemi Fabrics")],
)
def test_answers_to_the_name_question(typed: str, name: str) -> None:
    assert parties.answered_name(typed) == name


@pytest.mark.parametrize("typed", ["why do you need my name?", "1234", "I sell bags and shoes and slippers in Yaba market every day"])
def test_answers_that_arent_a_name(typed: str) -> None:
    assert parties.answered_name(typed) == ""


@pytest.mark.parametrize(
    ("typed", "role"),
    [("I'm the seller", "seller"), ("seller", "seller"), ("I bought it", "buyer"), ("na me be the buyer", "buyer"), ("neither", "neither")],
)
def test_typed_roles(typed: str, role: str) -> None:
    assert parties.role_in(typed) == role


def test_the_usual_role_is_offered_first() -> None:
    assert parties.role_order("buyer") == ["buyer", "seller"]
    assert parties.role_order(None) == ["seller", "buyer"]


def test_long_names_are_shortened_for_a_button() -> None:
    title = parties.button_title("Mama Nkechi Provisions and Sons")
    assert len(title) <= 20 and title.endswith("…")


# -- names --------------------------------------------------------------------------------------------------


def test_names_are_compared_and_cleaned_the_same_way_everywhere() -> None:
    assert name_key("Tola's Bags 🛍️") == "tolasbags"
    assert same_name("ADA  OBI", "ada obi") and not same_name("Ada", "Ada Obi")
    assert clean_name('  "Ada\u0007 Obi"  ') == "Ada Obi"
    assert phone_in_label("+234 (803) 123-4567") == "2348031234567" and phone_in_label("Shop 12") == ""


# -- the webhook ----------------------------------------------------------------------------------------------


def _delivery(message: dict[str, Any], contacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"metadata": {"phone_number_id": "1"}, "messages": [message]}
    if contacts is not None:
        value["contacts"] = contacts
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": value}]}]}


def test_a_chat_export_document_is_kept_with_its_file_name_and_other_documents_are_not() -> None:
    base = {"from": "2348000000001", "id": "wamid.1", "timestamp": "1790000000", "type": "document"}
    export = parse_webhook(
        _delivery({**base, "document": {"id": "m1", "mime_type": "text/plain", "filename": "WhatsApp Chat with Tola's Bags.txt", "sha256": "abc"}}),
        phone_number_id="1",
    )
    [message] = export.messages
    assert (message.kind, message.filename, message.media_id) == ("document", "WhatsApp Chat with Tola's Bags.txt", "m1")
    # An iOS export arrives as a ZIP whose type some phones report as a plain binary file.
    zipped = parse_webhook(_delivery({**base, "document": {"id": "m2", "mime_type": "application/octet-stream", "filename": "WhatsApp Chat - Ada.zip"}}), phone_number_id="1")
    assert zipped.messages[0].kind == "document"
    pdf = parse_webhook(_delivery({**base, "document": {"id": "m3", "mime_type": "application/pdf", "filename": "invoice.pdf"}}), phone_number_id="1")
    assert pdf.messages[0].kind == "unsupported"
    # A path in the file name is never kept.
    sneaky = parse_webhook(_delivery({**base, "document": {"id": "m4", "mime_type": "text/plain", "filename": "../../WhatsApp Chat with Ada.txt"}}), phone_number_id="1")
    assert sneaky.messages[0].filename == "WhatsApp Chat with Ada.txt"


def test_the_senders_profile_name_is_read_but_is_not_their_identity() -> None:
    message = {"from": "2348000000001", "id": "wamid.2", "timestamp": "1790000000", "type": "text", "text": {"body": "hi"}}
    contacts = [{"profile": {"name": "Tola​  Adeyemi\n"}, "wa_id": "2348000000001"}]
    [parsed] = parse_webhook(_delivery(message, contacts), phone_number_id="1").messages
    assert parsed.sender.profile_name == "Tola Adeyemi"  # invisible characters and line breaks dropped
    assert parsed.sender.phone == "2348000000001"


# -- money owed, from the customer's side ------------------------------------------------------------------------


def test_totals_keep_purchases_apart_from_sales() -> None:
    from app.whatsapp.assistant import totals_text

    rows = [
        {"buyer": "Ada", "seller": "Tola's Bags", "currency": "NGN", "total": Decimal("19000"), "amount_paid": Decimal("4000"), "outstanding": Decimal("15000"), "owner_role": "seller"},
        {"buyer": "Tola's Bags", "seller": "Kemi Fabrics", "currency": "NGN", "total": Decimal("8000"), "amount_paid": Decimal("0"), "outstanding": Decimal("8000"), "owner_role": "buyer"},
    ]
    assert totals_text(rows, "this week").splitlines() == [
        "*Your records this week* · 2 records",
        "Agreed ₦19,000.00 · paid ₦4,000.00 · still owed ₦15,000.00",
        "As the buyer (1 record): agreed ₦8,000.00 · paid ₦0.00 · you still owe ₦8,000.00",
    ]


def test_money_the_customer_owes_is_listed_apart_from_money_owed_to_them() -> None:
    rows = [
        {"buyer": "Ada", "seller": "Tola's Bags", "currency": "NGN", "reference": "CR-000001", "outstanding": Decimal("5000"), "owner_role": "seller"},
        {"buyer": "Tola's Bags", "seller": "Kemi Fabrics", "currency": "NGN", "reference": "CR-000002", "outstanding": Decimal("8000"), "owner_role": "buyer"},
        {"buyer": "Emeka", "seller": "", "currency": "NGN", "reference": "CR-000003", "outstanding": Decimal("1000"), "owner_role": None},
    ]
    assert owed_text(rows, "").splitlines() == [
        "*Still owed*",
        "• Ada: ₦5,000.00 (CR-000001)",
        "• Emeka: ₦1,000.00 (CR-000003)",
        "",
        "Total still owed: ₦6,000.00",
        "",
        "*You still owe*",
        "• Kemi Fabrics: ₦8,000.00 (CR-000002)",
        "",
        "Total you owe: ₦8,000.00",
    ]
