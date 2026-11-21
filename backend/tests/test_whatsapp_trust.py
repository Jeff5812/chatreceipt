"""Payment checks, the other person's confirmation of a record, WhatsApp usernames and message costs.

Signed webhooks in, a real database, a fake WhatsApp API and a scripted AI. The sale throughout:
Tola's Bags sells Ada Obi a brown leather tote for ₦23,000 plus ₦2,000 delivery, and the chat says a
₦10,000 deposit was paid. Ada's WhatsApp number is BOLA's in the harness.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from app.extraction.schema import AIField
from app.extraction.service import payment_reference
from app.names import username_in_label
from app.whatsapp import store
from app.whatsapp.conversation import confirm_code_in
from app.whatsapp.events import Sender, parse_webhook
from tests.samples import bag_conversation_output, field, image_bytes, line_item
from tests.whatsapp_harness import ALICE, BOLA, PHONE_NUMBER_ID, Channel, open_channel
from tests.test_whatsapp_identity import ReadingProvider

NUMBER = "2349000000000"  # ChatReceipt's own number in these tests


def sale(**overrides: Any) -> dict[str, Any]:
    """The tote sale as the AI reads it from Tola's Bags' phone."""
    data = bag_conversation_output(
        seller=field("Tola's Bags"),
        buyer=field("Ada Obi"),
        owner_role=field("seller", quote="₦23,000 is my final price."),
        other_party=field("Ada Obi"),
        line_items=[line_item("Brown leather tote", "1", "23000", quote="₦23,000 is my final price.")],
        amount_paid=field("10000", quote="Received ₦10,000 deposit, thank you"),
        payment_status=field("partially_paid", quote="Received ₦10,000 deposit, thank you"),
        transaction_date=field("2026-11-18"),
    )
    data.update(overrides)
    return data


@pytest.fixture
def setup(clean_database: str) -> Iterator[Any]:
    opened: list[Any] = []

    def build(*outcomes: Any, **settings: Any) -> tuple[Channel, ReadingProvider]:
        provider = ReadingProvider(*(outcomes or (sale(),)))
        client, channel = open_channel(clean_database, provider, **{"whatsapp_display_number": "+234 900 000 0000", **settings})
        opened.append(client)
        for number in range(1, 6):
            channel.graph.add_image(f"media-{number}", image_bytes(size=(400 + number, 800)))
        return channel, provider

    yield build
    for client in opened:
        client.__exit__(None, None, None)


def say_as(channel: Channel, who: dict[str, str], body: str, profile_name: str, **contact: Any) -> None:
    """A message with the sender's WhatsApp profile, the way Meta delivers one."""
    contacts = [{"profile": {"name": profile_name, **contact}, **({"wa_id": who["phone"]} if who.get("phone") else {}), "user_id": who["user_id"]}]
    assert channel.post(channel.payload(channel.text_event(who, body), contacts=contacts)).status_code == 200
    channel.work()


def to(channel: Channel, who: dict[str, str]) -> list[str]:
    """Everything sent to one person, in order."""
    return [
        channel.graph.body_of(message)
        for message in channel.graph.sent
        if message.get("to") == who.get("phone") or (not who.get("phone") and message.get("recipient") == who["user_id"])
    ]


def saved(channel: Channel) -> list[dict[str, Any]]:
    return channel.rows("SELECT * FROM chatreceipt.transactions ORDER BY created_at")


def responses(channel: Channel) -> list[dict[str, Any]]:
    return channel.rows("SELECT * FROM chatreceipt.record_responses ORDER BY created_at")


def save_the_sale(channel: Channel, media_id: str = "media-1") -> dict[str, Any]:
    channel.send_image(ALICE, media_id)
    channel.tap(ALICE, "Confirm")
    return saved(channel)[-1]


# -- payment checks ---------------------------------------------------------------------------------


def test_a_payment_the_chat_records_is_a_claim_until_the_seller_says_it_arrived(setup: Any) -> None:
    channel, _ = setup()
    record = save_the_sale(channel)
    assert record["payment_check"] == "claimed" and record["payment_checked_at"] is None
    # One "Saved" message carries the question, instead of a separate one.
    question = channel.graph.last_body()
    assert "Saved as *CR-000001*" in question and "Has ₦10,000.00 from Ada Obi reached your account?" in question
    assert "Check your bank app before you hand anything over" in question
    assert channel.graph.offered() == {"Yes, it's arrived", "Not yet", "Get PDF"}

    channel.tap(ALICE, "Not yet")
    assert saved(channel)[0]["payment_check"] == "not_received"
    assert "*CR-000001* now shows ₦10,000.00 as *not in your account yet*" in channel.graph.last_body()

    # "Who still owes me?" lists the balance and the payment that hasn't arrived, separately.
    channel.say(ALICE, "who still owes me?")
    owed = channel.graph.last_body()
    assert "Ada Obi: ₦15,000.00 (CR-000001)" in owed
    assert "*Paid in the chat, but not in your account yet*\n• Ada Obi: ₦10,000.00 (CR-000001)" in owed

    channel.tap(ALICE, "It's arrived")
    [record] = saved(channel)
    assert record["payment_check"] == "received" and record["payment_checked_at"] is not None
    assert channel.graph.last_body() == "✅ Noted: ₦10,000.00 reached your account for *CR-000001*."
    channel.say(ALICE, "who still owes me?")
    assert "not in your account" not in channel.graph.last_body() and "₦15,000.00" in channel.graph.last_body()


def test_an_unchecked_payment_is_listed_to_check(setup: Any) -> None:
    channel, _ = setup()
    save_the_sale(channel)
    channel.say(ALICE, "who still owes me?")
    assert "*Payments you haven't checked yet*\n• Ada Obi: ₦10,000.00 (CR-000001)" in channel.graph.last_body()
    channel.say(ALICE, "recent")
    assert "💰 payment not checked" in channel.graph.last_body()


def test_nothing_is_asked_when_the_customer_was_the_one_paying_or_nothing_was_paid(setup: Any) -> None:
    channel, _ = setup(sale(owner_role=field("buyer", quote="Okay, I'll take it."), other_party=field("Tola's Bags")), sale(amount_paid=field(""), payment_status=field("unpaid", quote="I will transfer tomorrow.")))
    bought = save_the_sale(channel, "media-1")
    assert bought["payment_check"] is None
    assert channel.graph.offered() == {"Get PDF", "Scan another", "Recent records"}
    unpaid = save_the_sale(channel, "media-2")
    assert unpaid["payment_check"] is None and "reached your account" not in channel.graph.last_body()


def test_a_payment_answer_only_works_on_the_customers_own_records(setup: Any) -> None:
    channel, _ = setup()
    record = save_the_sale(channel)
    say_as(channel, BOLA, "hi", "Ada Obi")
    forged = f"cr|pchk|||{record['id']}:received"
    assert channel.deliver(channel.tap_event(BOLA, forged)).status_code == 200
    channel.work()
    assert saved(channel)[0]["payment_check"] == "claimed"
    assert to(channel, BOLA)[-1] == "That record is no longer available."


def test_the_same_transfer_receipt_on_another_record_is_pointed_out(setup: Any) -> None:
    receipt = {"payment_reference": field("100004 2611 1808-0512", quote="Session ID: 100004 2611 1808-0512")}
    channel, _ = setup(sale(**receipt))
    first = save_the_sale(channel, "media-1")
    assert first["payment_reference"] == "100004261118080512"
    channel.send_image(ALICE, "media-2")  # a different screenshot of the same receipt
    draft = next(body for body in reversed(channel.graph.bodies()) if "*Seller:*" in body)
    assert "This transfer receipt (reference 100004261118080512) is already on your record CR-000001 with Ada Obi." in draft
    assert "can be a sign of a fake transfer: check your account before you hand anything over." in draft


def test_payment_references_are_compared_only_when_they_can_be_references() -> None:
    def ref(value: str) -> str:
        return payment_reference(AIField(found=bool(value), value=value, confidence="high", evidence=[], note=""))

    assert ref("FT26323 BXK9") == "FT26323BXK9"
    assert ref("0000-1234-5678") == "000012345678"
    assert ref("12345") == ""  # too short to tell receipts apart
    assert ref("ABCDEFGH") == ""  # no digits: a word, not a reference
    assert ref("") == ""


# -- the other person's confirmation --------------------------------------------------------------


def test_the_receipt_carries_a_link_the_other_person_confirms_with(setup: Any) -> None:
    channel, _ = setup()
    say_as(channel, ALICE, "hi", "Tola Adeyemi")
    record = save_the_sale(channel)
    code = record["confirm_code"]
    assert len(code) == 10 and confirm_code_in(f"CONFIRM {code}") == code
    caption = next(body for body in channel.graph.bodies() if body.startswith("🧾 *Receipt CR-000001*"))
    assert f"✅ *Ada Obi, is this right?* Tap to confirm it:\nhttps://wa.me/{NUMBER}?text=CONFIRM%20{code}" in caption
    assert caption.endswith("ChatReceipt doesn't verify payments._")
    assert "Forward the receipt above to Ada Obi. They can confirm it with one tap" in channel.graph.last_body()

    # Ada opens the link: her first message to ChatReceipt. She sees this record and nothing else.
    say_as(channel, BOLA, f"CONFIRM {code}", "Ada Obi")
    [view] = to(channel, BOLA)
    assert "I'm *ChatReceipt*. *Tola's Bags* saved a record of a deal with you" in view
    assert "🧾 *CR-000001* · " in view and "*For:* Brown leather tote" in view
    assert "₦25,000.00" in view and "₦10,000.00" in view and "₦15,000.00" in view
    assert "Your answer goes to them with your WhatsApp name and number" in view
    assert channel.graph.offered() == {"Yes, it's right", "Something's wrong"}

    channel.tap(BOLA, "Yes, it's right")
    [answer] = responses(channel)
    assert (answer["response"], answer["responder_phone"], answer["responder_name"], answer["note"]) == ("agreed", BOLA["phone"], "Ada Obi", None)
    assert to(channel, BOLA)[-1].startswith("✅ Thanks. I've told Tola's Bags you confirmed *CR-000001*.")
    assert to(channel, ALICE)[-1] == f"✅ *Ada Obi (+{BOLA['phone']})* confirmed your record *CR-000001*."

    channel.tap(BOLA, "Yes, it's right")
    assert to(channel, BOLA)[-1] == "You've already confirmed *CR-000001* 👍" and len(responses(channel)) == 1
    channel.say(ALICE, "recent")
    assert "✅ confirmed by them" in channel.graph.last_body()


def test_the_other_person_can_say_what_is_wrong(setup: Any) -> None:
    channel, _ = setup()
    record = save_the_sale(channel)
    say_as(channel, BOLA, f"confirm {record['confirm_code'].lower()}", "Ada Obi")  # typed by hand, in lower case
    channel.tap(BOLA, "Something's wrong")
    assert to(channel, BOLA)[-1] == "What isn't right about *CR-000001*? Tell me in one message, like “I paid 15k, not 10k”."
    channel.say(BOLA, "I paid 15k, not 10k")
    [answer] = responses(channel)
    assert (answer["response"], answer["note"]) == ("disputed", "I paid 15k, not 10k")
    assert to(channel, BOLA)[-1] == "Thanks. I've sent that to Tola's Bags, so you can sort it out together."
    notice = to(channel, ALICE)[-1]
    assert notice.startswith(f"⚠️ *Ada Obi (+{BOLA['phone']})* says something's wrong with *CR-000001*:\n“I paid 15k, not 10k”")
    channel.say(ALICE, "recent")
    assert "⚠️ disputed" in channel.graph.last_body()
    # The next message is an ordinary one again.
    channel.say(BOLA, "thanks")
    assert len(responses(channel)) == 1


def test_changing_their_mind_after_something_is_wrong_leaves_no_note_waiting(setup: Any) -> None:
    channel, _ = setup()
    record = save_the_sale(channel)
    say_as(channel, BOLA, f"CONFIRM {record['confirm_code']}", "Ada Obi")
    channel.tap(BOLA, "Something's wrong")
    channel.tap(BOLA, "Yes, it's right")
    channel.say(BOLA, "thanks")  # an ordinary message now, not a note about the record
    assert [answer["response"] for answer in responses(channel)] == ["agreed"]
    assert channel.rows("SELECT awaiting FROM chatreceipt.customers WHERE whatsapp_phone = %s", BOLA["phone"]) == [{"awaiting": None}]


def test_a_link_needs_the_right_code_and_never_shows_another_record(setup: Any) -> None:
    channel, _ = setup()
    record = save_the_sale(channel)
    channel.say(ALICE, f"CONFIRM {record['confirm_code']}")
    assert channel.graph.last_body().startswith("That's the link for your own record *CR-000001*.")
    say_as(channel, BOLA, "CONFIRM ABCDEFGHJK", "Ada Obi")
    assert to(channel, BOLA) == ["I couldn't find that record. Check the link you were sent, or ask the person who sent it."]
    assert channel.deliver(channel.tap_event(BOLA, "cr|ack|||ABCDEFGHJK:agreed")).status_code == 200
    channel.work()
    assert responses(channel) == [] and to(channel, BOLA)[-1].startswith("I couldn't find that record.")


def test_deleting_their_data_keeps_the_answer_but_not_who_gave_it(setup: Any) -> None:
    channel, _ = setup()
    record = save_the_sale(channel)
    say_as(channel, BOLA, f"CONFIRM {record['confirm_code']}", "Ada Obi")
    channel.tap(BOLA, "Yes, it's right")
    channel.say(BOLA, "delete my data")
    channel.tap(BOLA, "Delete everything")
    [answer] = responses(channel)
    assert answer["response"] == "agreed"
    assert (answer["responder_id"], answer["responder_phone"], answer["responder_name"]) == (None, None, None)
    assert len(saved(channel)) == 1  # Tola's record is Tola's


def test_old_records_get_a_link_when_their_receipt_is_sent_again(setup: Any) -> None:
    channel, _ = setup()
    record = save_the_sale(channel)
    channel.rows("UPDATE chatreceipt.transactions SET confirm_code = NULL RETURNING id")  # saved before codes existed
    channel.deliver(channel.tap_event(ALICE, f"cr|receipt|||{record['id']}"))  # "Send the receipt"
    channel.work()
    code = saved(channel)[0]["confirm_code"]
    assert code and f"CONFIRM%20{code}" in channel.graph.everything_said()


def test_without_chatreceipts_number_receipts_have_no_link(setup: Any) -> None:
    channel, _ = setup(whatsapp_display_number="")
    save_the_sale(channel)
    assert "wa.me" not in channel.graph.everything_said()
    assert "Forward the receipt above to the other person" in channel.graph.everything_said()


def test_the_records_page_shows_the_check_and_the_answer(setup: Any) -> None:
    channel, _ = setup()
    record = save_the_sale(channel)
    channel.tap(ALICE, "Yes, it's arrived")
    say_as(channel, BOLA, f"CONFIRM {record['confirm_code']}", "Ada Obi")
    channel.tap(BOLA, "Yes, it's right")
    channel.say(ALICE, "web")
    code = channel.graph.last_body().split("#code=")[1].split()[0]
    token = channel.client.post("/api/records/session", json={"code": code}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    [listed] = channel.client.get("/api/records", headers=headers).json()["records"]
    assert (listed["payment_check"], listed["response"]) == ("received", "agreed")
    detail = channel.client.get(f"/api/records/{record['id']}", headers=headers).json()
    [answer] = detail["responses"]
    assert (answer["response"], answer["name"], answer["contact"]) == ("agreed", "Ada Obi", f"+{BOLA['phone']}")


# -- WhatsApp usernames ------------------------------------------------------------------------------


KEMI = {"user_id": "NG.3000000000000000003"}  # a username and no number


def test_someone_with_a_username_and_no_number_is_answered_by_their_user_id(setup: Any) -> None:
    channel, _ = setup()
    say_as(channel, KEMI, "hi", "Kemi", username="kemi.styles")
    [customer] = channel.rows("SELECT whatsapp_user_id, whatsapp_phone, whatsapp_username FROM chatreceipt.customers")
    assert customer == {"whatsapp_user_id": KEMI["user_id"], "whatsapp_phone": None, "whatsapp_username": "kemi.styles"}
    reply = channel.graph.sent[-1]
    assert reply["recipient"] == KEMI["user_id"] and "to" not in reply


def test_a_username_confirms_a_record_and_is_shown_to_the_seller(setup: Any) -> None:
    channel, _ = setup()
    record = save_the_sale(channel)
    say_as(channel, KEMI, f"CONFIRM {record['confirm_code']}", "Kemi", username="kemi.styles")
    channel.tap(KEMI, "Yes, it's right")
    [answer] = responses(channel)
    assert (answer["responder_phone"], answer["responder_username"]) == (None, "kemi.styles")
    assert to(channel, ALICE)[-1] == "✅ *Kemi (@kemi.styles)* confirmed your record *CR-000001*."


def test_usernames_in_webhooks_are_read_without_the_at_sign_and_bad_ones_are_dropped() -> None:
    def sender(username: Any) -> Sender:
        body = {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                "contacts": [{"profile": {"name": "Ada", "username": username}, "user_id": "NG.1"}],
                "messages": [{"id": "wamid.1", "from_user_id": "NG.1", "timestamp": "1790000000", "type": "text", "text": {"body": "hi"}}],
            }}]}],
        }
        return parse_webhook(json.loads(json.dumps(body)), phone_number_id=PHONE_NUMBER_ID).messages[0].sender

    assert sender("Ada_Closet").username == "Ada_Closet"
    assert sender("@ada.closet").username == "ada.closet"
    assert sender("ad").username == "" and sender("ada closet").username == "" and sender(7).username == ""
    assert sender("Ada_Closet").phone is None and sender("Ada_Closet").user_id == "NG.1"


def test_a_changed_user_id_keeps_the_same_customer(setup: Any) -> None:
    channel, _ = setup()
    say_as(channel, KEMI, "hi", "Kemi", username="kemi.styles")
    system = {
        "id": "wamid.sys1", "timestamp": "1790000100", "type": "system",
        "system": {"body": "User changed from x to y", "type": "user_changed_user_id", "user_id": "NG.3000000000000000099", "previous_user_id": KEMI["user_id"]},
    }
    assert channel.deliver(system).status_code == 200
    channel.work()
    [customer] = channel.rows("SELECT whatsapp_user_id FROM chatreceipt.customers")
    assert customer["whatsapp_user_id"] == "NG.3000000000000000099"


def test_a_username_in_a_chat_label_is_only_the_at_form() -> None:
    assert username_in_label("@Ada_Closet") == "ada_closet"
    assert username_in_label(" @ada.closet ") == "ada.closet"
    assert username_in_label("Ada Closet") == "" and username_in_label("ada_closet") == "" and username_in_label("@ab") == ""


def test_a_contact_known_by_username_is_recognised_on_the_next_receipt(setup: Any) -> None:
    channel, _ = setup(
        sale(other_party=field("@ada_closet")),
        sale(other_party=field("@ada_closet"), buyer=field("")),  # the next chat shows only the username
    )
    save_the_sale(channel, "media-1")
    [entry] = channel.rows("SELECT name, phone, username FROM chatreceipt.counterparties")
    assert entry == {"name": "Ada Obi", "phone": None, "username": "ada_closet"}
    channel.send_image(ALICE, "media-2")
    draft = next(body for body in reversed(channel.graph.bodies()) if "*Seller:*" in body)
    assert "*Buyer:* Ada Obi _(from your earlier receipts)_" in draft


def test_a_username_remembered_for_one_person_is_never_moved_to_another(clean_database: str) -> None:
    from app.db.pool import Database

    database = Database(clean_database)
    database.open()
    try:
        with database.transaction() as conn:
            customer_id = store.resolve_customer(conn, Sender(user_id="NG.4000000000000000004", phone=None))
            store.remember_counterparty(conn, customer_id, name="Ada Obi", phone="2348011111111", label="", role="buyer", username="ada_closet")
            store.remember_counterparty(conn, customer_id, name="Another Ada", phone="2348022222222", label="", role="buyer", username="ada_closet")
            rows = conn.execute("SELECT name, phone, username FROM chatreceipt.counterparties ORDER BY phone").fetchall()
            assert rows == [
                {"name": "Ada Obi", "phone": "2348011111111", "username": "ada_closet"},
                {"name": "Another Ada", "phone": "2348022222222", "username": None},
            ]
            found = store.find_counterparty(conn, customer_id, phone="", label="", username="ADA_CLOSET")
            assert found is not None and (found[0]["name"], found[1]) == ("Ada Obi", "username")
            # A username first seen without a number gets the number when a later chat shows it.
            store.remember_counterparty(conn, customer_id, name="Kemi", phone="", label="", role="buyer", username="kemi.styles")
            store.remember_counterparty(conn, customer_id, name="Kemi Styles", phone="2348033333333", label="", role="buyer", username="kemi.styles")
            kemi = conn.execute("SELECT name, phone, receipts FROM chatreceipt.counterparties WHERE username = 'kemi.styles'").fetchone()
            assert kemi == {"name": "Kemi Styles", "phone": "2348033333333", "receipts": 2}
    finally:
        database.close()


# -- what each receipt costs in messages --------------------------------------------------------------


def test_a_regular_customer_gets_no_reading_message_and_three_messages_per_receipt(setup: Any) -> None:
    channel, _ = setup(sale(), sale(amount_paid=field(""), payment_status=field("unpaid", quote="I will transfer tomorrow.")))
    channel.send_image(ALICE, "media-1")
    assert "I'm reading your screenshot now" in channel.graph.everything_said()  # the first-contact note says so
    assert "Got it. I'm reading your screenshot now." not in channel.graph.everything_said()
    channel.tap(ALICE, "Confirm")

    before = len(channel.graph.sent)
    channel.send_image(ALICE, "media-2")
    new = channel.graph.bodies()[before:]
    assert len(new) == 1 and "*Seller:*" in new[0]  # just the draft, with its question
    typing = [request for request in channel.graph.requests if b'"typing_indicator"' in request.content]
    assert typing  # WhatsApp's typing indicator showed it was being read, at no cost
    channel.tap(ALICE, "Confirm")
    assert len(channel.graph.sent) - before == 3  # the draft, the receipt and "Saved"
