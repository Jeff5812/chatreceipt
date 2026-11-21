"""Who is who on WhatsApp receipts, chat exports sent as documents, clearing history and short answers.

Signed webhooks in, a real database, a fake WhatsApp API and a scripted AI. The conversations are
the kind ChatReceipt's customers send: a bag seller in Yaba, a provisions shop, a buyer whose sister
pays for her.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from app.extraction.provider import ProviderResponse, ProviderUsage
from tests.samples import bag_conversation_output, field, image_bytes, line_item
from tests.whatsapp_harness import ALICE, Channel, open_channel

CHAT = """18/11/2026, 10:38 - Tola's Bags: Good afternoon! The brown leather tote is ₦25,000.
18/11/2026, 10:39 - Ada Obi: Can you do ₦22,000?
18/11/2026, 10:41 - Tola's Bags: ₦23,000 is my final price. Delivery to Yaba is ₦2,000.
18/11/2026, 10:42 - Ada Obi: Okay, I'll take it. I'll transfer tomorrow.
19/11/2026, 08:05 - Ada Obi: IMG-20261119-WA0003.jpg (file attached)
19/11/2026, 08:06 - Tola's Bags: Received ₦10,000 deposit, thank you"""
EXPORT_NAME = "WhatsApp Chat with Tola's Bags.txt"


class ReadingProvider:
    """Reads screenshots and chat exports with scripted answers; the last answer repeats."""

    name = "scripted"
    configured = True

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes) or [bag_conversation_output()]
        self.calls: list[list[Any]] = []
        self.requests: list[Any] = []

    def _next(self) -> ProviderResponse:
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return ProviderResponse(data=outcome, usage=ProviderUsage(model="scripted"), duration_ms=1)

    async def extract(self, images: Any) -> ProviderResponse:
        self.calls.append(list(images))
        return self._next()

    async def generate(self, request: Any) -> ProviderResponse:
        if request.task != "extraction":
            raise AssertionError(f"The AI wasn't expected to read {request.task!r} here.")
        self.requests.append(request)
        return self._next()

    async def aclose(self) -> None:
        return None


@pytest.fixture
def setup(clean_database: str) -> Iterator[Any]:
    opened: list[Any] = []

    def build(*outcomes: Any, **settings: Any) -> tuple[Channel, ReadingProvider]:
        # Every reading is dated, so saving asks only about who is who.
        dated = tuple({**outcome, "transaction_date": field("2026-11-18")} if isinstance(outcome, dict) else outcome for outcome in outcomes)
        provider = ReadingProvider(*dated)
        client, channel = open_channel(clean_database, provider, **settings)
        opened.append(client)
        for number in range(1, 6):
            channel.graph.add_image(f"media-{number}", image_bytes(size=(400 + number, 800)))
        return channel, provider

    yield build
    for client in opened:
        client.__exit__(None, None, None)


def reading(**overrides: Any) -> dict[str, Any]:
    """A bag for ₦17,000 plus ₦2,000 delivery, unpaid. Names and sides come from the overrides."""
    return bag_conversation_output(**overrides)


def say_as(channel: Channel, body: str, profile_name: str, who: dict[str, str] = ALICE) -> None:
    """A message with the sender's WhatsApp profile name, the way Meta delivers one."""
    contacts = [{"profile": {"name": profile_name}, "wa_id": who["phone"], "user_id": who["user_id"]}]
    assert channel.post(channel.payload(channel.text_event(who, body), contacts=contacts)).status_code == 200
    channel.work()


def send_export(channel: Channel, media_id: str, data: bytes, filename: str = EXPORT_NAME, *, mime_type: str = "text/plain", sha256: str = "") -> None:
    channel.graph.add_image(media_id, data, mime_type=mime_type)
    document = {"id": media_id, "mime_type": mime_type, "filename": filename, "sha256": sha256 or f"hash-{media_id}"}
    assert channel.deliver(channel._message(ALICE, "document", document=document)).status_code == 200
    channel.work()


def saved(channel: Channel) -> list[dict[str, Any]]:
    return channel.rows("SELECT * FROM chatreceipt.transactions ORDER BY created_at")


def latest_draft(channel: Channel) -> str:
    """The latest whole draft sent, whether or not its question came as a separate message."""
    return next(body for body in reversed(channel.graph.bodies()) if "*Seller:*" in body)


def counterparties(channel: Channel) -> list[dict[str, Any]]:
    return channel.rows("SELECT name, phone, labels, last_role, receipts FROM chatreceipt.counterparties ORDER BY created_at")


# -- the customer's own side ----------------------------------------------------------------------


def test_who_is_who_is_asked_once_when_confirming_and_the_name_is_remembered(setup: Any) -> None:
    channel, _ = setup(
        reading(other_party=field("Ada Obi")),  # the chat doesn't show which side the phone's owner is on
        reading(other_party=field("Ada Obi"), owner_role=field("seller", quote="17k final.")),
    )
    say_as(channel, "hi", "Tola Adeyemi")
    channel.send_image(ALICE, "media-1")
    assert "*Seller:* Not found" in latest_draft(channel)

    channel.tap(ALICE, "Confirm")
    assert "Who are you in this chat?" in channel.graph.last_body() and "shown as “Ada Obi”" in channel.graph.last_body()
    assert channel.graph.offered() == {"I'm the seller", "I'm the buyer", "Neither"}
    assert saved(channel) == []  # nothing is saved while the question is open

    channel.tap(ALICE, "I'm the seller")
    assert "What name should go on your receipts?" in channel.graph.last_body()
    assert channel.graph.offered() == {"Tola Adeyemi", "Not now"}  # their WhatsApp name, one tap away

    channel.tap(ALICE, "Tola Adeyemi")
    [record] = saved(channel)
    assert (record["seller"], record["buyer"], record["owner_role"]) == ("Tola Adeyemi", "Ada Obi", "seller")
    assert "Your receipts will show you as *Tola Adeyemi*" in channel.graph.last_body()
    assert [(row["name"], row["last_role"]) for row in counterparties(channel)] == [("Ada Obi", "buyer")]

    # The next chat shows the side: their name is filled in and nothing is asked.
    channel.send_image(ALICE, "media-2")
    draft = latest_draft(channel)
    assert "*Seller:* Tola Adeyemi _(you)_" in draft and "*Buyer:* Ada Obi" in draft
    channel.tap(ALICE, "Confirm")
    assert [row["seller"] for row in saved(channel)] == ["Tola Adeyemi", "Tola Adeyemi"]
    assert counterparties(channel)[0]["receipts"] == 2


def test_their_usual_role_is_offered_first_next_time(setup: Any) -> None:
    channel, _ = setup(reading(other_party=field("Kemi Fabrics")))
    channel.say(ALICE, "my name is Ada's Thrift")
    assert "Your receipts will show you as *Ada's Thrift*" in channel.graph.last_body()
    channel.send_image(ALICE, "media-1")
    channel.tap(ALICE, "Confirm")
    channel.tap(ALICE, "I'm the buyer")  # their saved name goes on the buyer's side, no second question
    [record] = saved(channel)
    assert (record["seller"], record["buyer"], record["owner_role"]) == ("Kemi Fabrics", "Ada's Thrift", "buyer")

    channel.send_image(ALICE, "media-2")
    channel.tap(ALICE, "Confirm")
    buttons = [button["reply"]["title"] for button in channel.graph.sent[-1]["interactive"]["action"]["buttons"]]
    assert buttons == ["I'm the buyer", "I'm the seller", "Neither"]


def test_not_now_twice_stops_the_name_question(setup: Any) -> None:
    channel, _ = setup(reading(other_party=field("Ada Obi"), owner_role=field("seller", quote="17k final.")))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    channel.tap(ALICE, "Confirm")
    assert channel.graph.offered() == {"Not now"}  # no WhatsApp name to offer
    channel.tap(ALICE, "Not now")
    assert saved(channel)[0]["seller"] == ""  # saved without a name rather than an invented one

    channel.send_image(ALICE, "media-2")
    channel.tap(ALICE, "Confirm")
    channel.say(ALICE, "skip")
    channel.send_image(ALICE, "media-3")
    channel.tap(ALICE, "Confirm")
    assert len(saved(channel)) == 3  # the third one saved straight away: no more asking
    assert "What name should go on your receipts?" not in channel.graph.everything_said().split("CR-000002")[-1]

    channel.say(ALICE, "my name is Tola's Bags")
    channel.send_image(ALICE, "media-4")
    assert "*Seller:* Tola's Bags _(you)_" in latest_draft(channel)


def test_a_typed_name_answers_the_question_without_its_lead_in(setup: Any) -> None:
    channel, _ = setup(reading(other_party=field("Ada Obi"), owner_role=field("seller", quote="17k final.")))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    channel.tap(ALICE, "Confirm")
    channel.say(ALICE, "It’s Tola’s Bags")
    assert saved(channel)[0]["seller"] == "Tola's Bags"
    assert channel.rows("SELECT party_name FROM chatreceipt.customers")[0]["party_name"] == "Tola's Bags"


def test_neither_side_saves_without_placing_the_customer(setup: Any) -> None:
    channel, _ = setup(reading(other_party=field("Bisi")))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    channel.tap(ALICE, "Confirm")
    channel.tap(ALICE, "Neither")
    [record] = saved(channel)
    assert record["owner_role"] is None and counterparties(channel) == []


# -- the other person ---------------------------------------------------------------------------------


def test_a_number_at_the_top_of_the_chat_is_kept_until_the_customer_names_them_then_remembered(setup: Any) -> None:
    number = "+234 803 123 4567"
    channel, _ = setup(reading(other_party=field(number), owner_role=field("seller", quote="17k final.")))
    channel.say(ALICE, "my name is Tola's Bags")
    channel.send_image(ALICE, "media-1")
    assert f"*Buyer:* {number}" in latest_draft(channel)  # the number, not an invented name
    channel.say(ALICE, "the buyer is Emeka Obi")
    channel.tap(ALICE, "Confirm")
    assert saved(channel)[0]["buyer"] == "Emeka Obi"
    assert [(row["name"], row["phone"]) for row in counterparties(channel)] == [("Emeka Obi", "2348031234567")]

    channel.send_image(ALICE, "media-2")
    assert "*Buyer:* Emeka Obi _(from your earlier receipts)_" in latest_draft(channel)
    assert "Please check" not in latest_draft(channel)


def test_a_name_that_disagrees_with_an_earlier_receipt_for_the_same_number_is_checked(setup: Any) -> None:
    number = "+234 803 123 4567"
    channel, _ = setup(
        reading(other_party=field(number), owner_role=field("seller", quote="17k final.")),
        reading(other_party=field(number), owner_role=field("seller", quote="17k final."), buyer=field("Ngozi Okafor", quote="This is Ngozi")),
    )
    channel.say(ALICE, "my name is Tola's Bags")
    channel.send_image(ALICE, "media-1")
    channel.say(ALICE, "the buyer is Emeka Obi")
    channel.tap(ALICE, "Confirm")

    channel.send_image(ALICE, "media-2")
    draft = latest_draft(channel)
    # The number is a reliable match, so the confirmed name stays; the other name is pointed out, not dropped.
    assert "*Buyer:* Emeka Obi _(from your earlier receipts)_" in draft
    assert "Your earlier receipts name this buyer “Emeka Obi”, but the chat says “Ngozi Okafor”" in draft


def test_a_contact_name_only_suggests_and_two_people_with_one_label_are_never_merged(setup: Any) -> None:
    label = field("Mummy Ada")
    seller = field("seller", quote="17k final.")
    channel, _ = setup(
        reading(other_party=label, owner_role=seller),
        reading(other_party=label, owner_role=seller),
        reading(other_party=label, owner_role=seller, buyer=field("Chika Eze", quote="I'm Chika, Ada's neighbour")),
        reading(other_party=label, owner_role=seller),
    )
    channel.say(ALICE, "my name is Tola's Bags")
    channel.send_image(ALICE, "media-1")
    channel.say(ALICE, "the buyer is Ada Obi")
    channel.tap(ALICE, "Confirm")

    # The same contact name: the confirmed name is suggested, and marked.
    channel.send_image(ALICE, "media-2")
    assert "*Buyer:* Ada Obi _(from your earlier receipts)_" in latest_draft(channel)
    channel.tap(ALICE, "Confirm")

    # The chat names someone else under that contact name: what the chat shows is kept, with a check.
    channel.send_image(ALICE, "media-3")
    draft = latest_draft(channel)
    assert "*Buyer:* Chika Eze" in draft and "Your earlier receipts name someone shown as “Mummy Ada” “Ada Obi”" in draft
    channel.tap(ALICE, "Confirm")
    assert sorted(row["name"] for row in counterparties(channel)) == ["Ada Obi", "Chika Eze"]  # not overwritten

    # Two people now share the label, so it no longer suggests either.
    channel.send_image(ALICE, "media-4")
    assert "*Buyer:* Mummy Ada\n" in latest_draft(channel)


def test_someone_paying_for_the_buyer_is_shown_as_the_payer(setup: Any) -> None:
    channel, _ = setup(
        reading(
            seller=field("Tola's Bags"), buyer=field("Ada Obi"), amount_paid=field("19000", quote="Transfer from Ngozi Okafor"),
            payment_status=field("paid", quote="Transfer from Ngozi Okafor"),
            people=[
                {"name": "Tola's Bags", "role": "seller", "is_owner": True, "quote": "17k final."},
                {"name": "Ada Obi", "role": "buyer", "is_owner": False, "quote": "Okay, I will take it."},
                {"name": "Ngozi Okafor", "role": "payer", "is_owner": False, "quote": "Transfer from Ngozi Okafor"},
            ],
        )
    )
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    draft = latest_draft(channel)
    assert "*Buyer:* Ada Obi" in draft and "*Paid by:* Ngozi Okafor" in draft


def test_who_still_owes_me_leaves_out_what_the_customer_owes(setup: Any) -> None:
    channel, _ = setup(
        reading(other_party=field("Ada Obi"), owner_role=field("seller", quote="17k final.")),
        reading(other_party=field("Kemi Fabrics"), owner_role=field("buyer", quote="Okay, I will take it.")),
    )
    channel.say(ALICE, "my name is Tola's Bags")
    channel.send_image(ALICE, "media-1")
    channel.tap(ALICE, "Confirm")
    channel.send_image(ALICE, "media-2")
    channel.tap(ALICE, "Confirm")
    assert [(row["seller"], row["buyer"]) for row in saved(channel)] == [("Tola's Bags", "Ada Obi"), ("Kemi Fabrics", "Tola's Bags")]

    channel.say(ALICE, "who still owes me?")
    assert channel.graph.last_body().splitlines() == [
        "*Still owed*",
        "• Ada Obi: ₦19,000.00 (CR-000001)",
        "",
        "Total still owed: ₦19,000.00",
        "",
        "*You still owe*",
        "• Kemi Fabrics: ₦19,000.00 (CR-000002)",
        "",
        "Total you owe: ₦19,000.00",
    ]


# -- chat exports sent as documents ----------------------------------------------------------------------


def export_reading(**overrides: Any) -> dict[str, Any]:
    """What the AI reports for CHAT, read from Ada's phone: she bought the tote and paid a deposit."""
    return bag_conversation_output(
        line_items=[line_item("Brown leather tote", "1", "23000", quote="₦23,000 is my final price.")],
        delivery_fee=field("2000", quote="Delivery to Yaba is ₦2,000."),
        amount_paid=field("10000", quote="Received ₦10,000 deposit"),
        payment_status=field("partially_paid", quote="Received ₦10,000 deposit"),
        owner_role=field("buyer", quote="Okay, I'll take it."),
        other_party=field("Tola"),  # the export's file name settles who the other person is
        **overrides,
    )


def test_a_chat_export_sent_as_a_document_becomes_a_draft_with_the_other_person_from_the_file(setup: Any) -> None:
    channel, provider = setup(export_reading())
    send_export(channel, "doc-1", CHAT.encode())
    said = channel.graph.everything_said()
    # A first message gets one note that says the export is being read, not a second "Got it".
    assert "I'm reading your chat export now" in said and "Your chat is sent to our AI provider to be read" in said
    assert "Got your chat export" not in said
    [request] = provider.requests
    text = "\n".join(part.text for part in request.parts if hasattr(part, "text"))
    assert '"Ada Obi" (the phone\'s owner, who exported this chat)' in text and provider.calls == []

    draft = latest_draft(channel)
    assert "*Seller:* Tola's Bags" in draft and "*Buyer:* Not found" in draft
    assert "₦25,000.00" in draft and "₦15,000.00" in draft  # 23,000 + 2,000 agreed, 10,000 paid, 15,000 remaining

    # Their own name in the export is offered for their side.
    channel.tap(ALICE, "Confirm")
    assert channel.graph.offered() == {"Ada Obi", "Not now"}
    channel.tap(ALICE, "Ada Obi")
    [record] = saved(channel)
    assert (record["seller"], record["buyer"], record["owner_role"]) == ("Tola's Bags", "Ada Obi", "buyer")
    assert (record["total"], record["amount_paid"], record["outstanding"]) == (Decimal("25000"), Decimal("10000"), Decimal("15000"))


def test_the_same_export_sent_again_is_not_read_twice(setup: Any) -> None:
    channel, provider = setup(export_reading())
    send_export(channel, "doc-1", CHAT.encode(), sha256="same-file")
    send_export(channel, "doc-2", CHAT.encode(), sha256="same-file")  # forwarded again: a new ID, the same file
    assert channel.graph.last_body() == "I've already got that one 👍"
    assert len(provider.requests) == 1


def test_a_webhook_delivered_twice_is_handled_once(setup: Any) -> None:
    channel, provider = setup(export_reading())
    channel.graph.add_image("doc-1", CHAT.encode(), mime_type="text/plain")
    event = channel._message(ALICE, "document", message_id="wamid.export-1", document={"id": "doc-1", "mime_type": "text/plain", "filename": EXPORT_NAME})
    for _ in range(2):
        assert channel.deliver(event).status_code == 200
    channel.work()
    assert len(channel.rows("SELECT * FROM chatreceipt.whatsapp_inbound")) == 1
    assert len(provider.requests) == 1


def test_a_zip_export_sends_the_photo_with_the_chat(setup: Any) -> None:
    channel, provider = setup(export_reading())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(EXPORT_NAME, CHAT)
        archive.writestr("IMG-20261119-WA0003.jpg", image_bytes("JPEG"))
    send_export(channel, "doc-1", buffer.getvalue(), "WhatsApp Chat with Tola's Bags.zip", mime_type="application/zip")
    parts = provider.requests[0].parts
    assert any(getattr(part, "text", "").startswith("Source 2: a photo (IMG-20261119-WA0003.jpg)") for part in parts)
    assert "*Seller:* Tola's Bags" in latest_draft(channel)


def test_an_unsafe_zip_is_refused_and_nothing_is_read(setup: Any) -> None:
    channel, provider = setup(export_reading())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../escape.txt", CHAT)
    send_export(channel, "doc-1", buffer.getvalue(), "WhatsApp Chat with Tola's Bags.zip", mime_type="application/zip")
    assert "unsafe names" in channel.graph.last_body() and "Nothing was saved" in channel.graph.last_body()
    assert channel.graph.offered() == {"Send screenshots", "Get help"}  # reading the same file again can't help
    assert provider.requests == [] and saved(channel) == []


def test_another_deal_in_the_same_export_gets_its_own_record_and_the_numbers_stay(setup: Any) -> None:
    deals = [{"summary": "A black clutch for ₦12,000, 2 November", "last_message": "m1"}]
    channel, provider = setup(export_reading(other_deals=deals), export_reading(other_deals=[{"summary": "Brown leather tote for ₦23,000", "last_message": "m6"}]))
    send_export(channel, "doc-1", CHAT.encode())
    assert "*Other deals in this chat*\n1. A black clutch for ₦12,000, 2 November" in latest_draft(channel)

    channel.say(ALICE, "deal 1")
    assert channel.graph.offered() == {"Start a new record", "Keep this draft"}
    channel.tap(ALICE, "Start a new record")
    assert len(provider.requests) == 2
    focus = "\n".join(part.text for part in provider.requests[1].parts if hasattr(part, "text"))
    assert "The person wants the record for this deal: A black clutch for ₦12,000, 2 November." in focus
    # The list is the first reading's, so "deal 1" still means the clutch.
    assert "1. A black clutch for ₦12,000, 2 November _(this record)_" in latest_draft(channel)


def test_screenshots_are_never_mixed_into_a_chat_export(setup: Any) -> None:
    channel, provider = setup(export_reading(), reading(seller=field("Tola's Bags"), buyer=field("Ada Obi")))
    channel.say(ALICE, "hi")
    channel.graph.add_image("doc-1", CHAT.encode(), mime_type="text/plain")
    channel.deliver(channel._message(ALICE, "document", document={"id": "doc-1", "mime_type": "text/plain", "filename": EXPORT_NAME}))
    channel.deliver(channel.image_event(ALICE, "media-1"))  # while the export is still being read
    channel.work()
    assert "I'm reading your chat export. Send the screenshot again when I've finished." in channel.graph.everything_said()
    assert provider.calls == []

    channel.send_image(ALICE, "media-1")  # now the export's draft is waiting
    assert channel.graph.offered() == {"Start a new record", "Keep this draft"}
    channel.tap(ALICE, "Keep this draft")
    assert "keeping this draft" in channel.graph.last_body()
    assert channel.rows("SELECT held_images FROM chatreceipt.whatsapp_submissions WHERE status = 'awaiting_confirmation'")[0]["held_images"] == []


def test_messages_while_an_export_is_read_talk_about_the_chat_not_a_screenshot(setup: Any) -> None:
    channel, _ = setup(export_reading())
    channel.say(ALICE, "hi")
    channel.graph.add_image("doc-1", CHAT.encode(), mime_type="text/plain")
    channel.deliver(channel._message(ALICE, "document", document={"id": "doc-1", "mime_type": "text/plain", "filename": EXPORT_NAME}))
    channel.deliver(channel.text_event(ALICE, "hello"))  # before the export has been read
    channel.work()
    said = channel.graph.everything_said()
    assert "Hi again 👋 I'm still reading your chat export." in said and "reading your screenshot" not in said


def test_their_own_name_in_an_export_is_marked_as_theirs(setup: Any) -> None:
    channel, _ = setup(export_reading(buyer=field("Ada Obi", quote="Okay, I'll take it.")))
    send_export(channel, "doc-1", CHAT.encode())
    assert "*Buyer:* Ada Obi _(you)_" in latest_draft(channel)
    assert "Send *deal" not in latest_draft(channel)  # no other deals, so no deal hint
    channel.tap(ALICE, "Confirm")
    assert saved(channel)[0]["buyer"] == "Ada Obi"  # their side was named, so nothing was asked


def test_one_other_deal_is_offered_as_deal_1_alone(setup: Any) -> None:
    deals = [{"summary": "A black clutch for ₦12,000, 2 November", "last_message": "m1"}]
    channel, _ = setup(export_reading(other_deals=deals))
    send_export(channel, "doc-1", CHAT.encode())
    assert "Send *deal 1* for a receipt of it." in latest_draft(channel)


def test_screenshots_and_exports_share_the_reading_limits(setup: Any) -> None:
    channel, provider = setup(export_reading(), whatsapp_images_per_hour=1)
    channel.send_image(ALICE, "media-1")
    channel.tap(ALICE, "Cancel")
    send_export(channel, "doc-1", CHAT.encode())
    assert "reached the limit" in channel.graph.last_body()
    assert provider.requests == []


# -- clearing the conversation, and short answers ---------------------------------------------------------


def test_clear_history_forgets_the_draft_and_the_conversation_but_keeps_records(setup: Any) -> None:
    channel, _ = setup(reading(seller=field("Tola's Bags"), buyer=field("Ada Obi")))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    channel.tap(ALICE, "Confirm")
    channel.send_image(ALICE, "media-2")  # a second draft, still open

    channel.say(ALICE, "help")
    assert "Clear history" in channel.graph.offered()
    channel.tap(ALICE, "Clear history")
    assert "Clear this chat's history?" in channel.graph.last_body()
    assert channel.graph.offered() == {"Clear history", "Delete all my data", "Keep everything"}
    channel.tap(ALICE, "Clear history")
    assert "Your saved records are still here" in channel.graph.last_body()

    assert channel.rows("SELECT 1 FROM chatreceipt.whatsapp_submissions WHERE status = 'awaiting_confirmation'") == []
    assert len(saved(channel)) == 1
    [customer] = channel.rows("SELECT memory FROM chatreceipt.customers")
    assert customer["memory"] == []


def test_keep_everything_clears_nothing(setup: Any) -> None:
    channel, _ = setup(reading(seller=field("Tola's Bags"), buyer=field("Ada Obi")))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    channel.say(ALICE, "clear")
    channel.tap(ALICE, "Keep everything")
    assert channel.graph.last_body() == "Nothing was cleared."
    assert len(channel.rows("SELECT 1 FROM chatreceipt.whatsapp_submissions WHERE status = 'awaiting_confirmation'")) == 1


@pytest.mark.parametrize("answer", ["yes", "That's correct", "ok", "Yes it's correct"])
def test_yes_to_is_this_correct_saves_the_record(setup: Any, answer: str) -> None:
    channel, _ = setup(reading(seller=field("Tola's Bags"), buyer=field("Ada Obi")))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    assert "Is this correct?" in channel.graph.last_body()
    channel.say(ALICE, answer)
    assert len(saved(channel)) == 1


def test_no_to_is_this_correct_opens_the_corrections(setup: Any) -> None:
    channel, _ = setup(reading(seller=field("Tola's Bags"), buyer=field("Ada Obi")))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    channel.say(ALICE, "no")
    assert saved(channel) == []
    assert "Tell me what's wrong in your own words" in channel.graph.everything_said()


def test_correct_on_its_own_means_the_correct_button_not_yes(setup: Any) -> None:
    channel, _ = setup(reading(seller=field("Tola's Bags"), buyer=field("Ada Obi")))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    channel.say(ALICE, "correct")
    assert saved(channel) == []  # a saved record can't be edited, so an unclear word never saves one
    assert {"Seller", "Buyer", "Items"} <= channel.graph.offered()  # the list of details to change


def test_yes_doesnt_answer_who_are_you_so_the_question_is_asked_again(setup: Any) -> None:
    channel, _ = setup(reading(other_party=field("Ada Obi")))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    channel.tap(ALICE, "Confirm")
    channel.say(ALICE, "yes")
    assert saved(channel) == []
    assert "Who are you in this chat?" in channel.graph.last_body()


def test_other_deals_in_screenshots_are_listed_without_offering_deal_n(setup: Any) -> None:
    deals = [{"summary": "2 bags of rice for ₦156,000, 12 November", "last_message": ""}]
    channel, _ = setup(reading(seller=field("Mama Nkechi"), buyer=field("Chidi"), other_deals=deals))
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    draft = latest_draft(channel)
    assert "1. 2 bags of rice for ₦156,000, 12 November" in draft and "send only its screenshots" in draft
    channel.say(ALICE, "deal 1")
    assert channel.graph.last_body().startswith("To make a receipt for another deal in these screenshots")
