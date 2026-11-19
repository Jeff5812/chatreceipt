"""The WhatsApp channel end to end: signed webhooks in, real database, fake WhatsApp API and AI."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest

from app.db.pool import Database, DatabaseUnavailable
from app.documents.browser import DocumentRenderError
from app.extraction.provider import ErrorKind, ExtractionProviderError, ProviderResponse, ProviderUsage
from app.whatsapp import conversation, store
from tests.samples import bag_conversation_output, field, image_bytes, line_item
from tests.whatsapp_harness import ALICE, BOLA, MEDIA_HOST, PHONE_NUMBER_ID, VERIFY_TOKEN, Channel, FakeGraph, open_channel


class ScriptedProvider:
    """Answers with each scripted outcome in turn; the last one repeats."""

    name = "scripted"
    configured = True

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes) or [bag_conversation_output()]
        self.calls: list[list[Any]] = []

    async def extract(self, images: Any) -> ProviderResponse:
        self.calls.append(list(images))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return ProviderResponse(data=outcome, usage=ProviderUsage(model="scripted"), duration_ms=1)

    async def aclose(self) -> None:
        return None


class TalkingProvider(ScriptedProvider):
    """Also answers text-only requests, the way the AI reads typed corrections."""

    def __init__(self, *outcomes: Any, corrections: list[Any]) -> None:
        super().__init__(*outcomes)
        self.corrections = list(corrections)
        self.requests: list[Any] = []

    async def generate(self, request: Any) -> ProviderResponse:
        self.requests.append(request)
        if not self.corrections:
            raise AssertionError(f"The AI wasn't expected to be asked about: {request.parts[0].text.splitlines()[-1]!r}")
        answer = self.corrections.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ProviderResponse(data=answer, usage=ProviderUsage(model="scripted"), duration_ms=1)


def said(*changes: dict[str, str], intent: str = "correct", resolved: tuple[int, ...] = (), question: str = "") -> dict[str, Any]:
    """What the AI reports about a typed message."""
    return {"intent": intent, "changes": list(changes), "resolved_checks": list(resolved), "question": question}


def change(field: str, value: str = "", item_id: str = "", quantity: str = "", price: str = "") -> dict[str, str]:
    return {"field": field, "item_id": item_id, "value": value, "quantity": quantity, "price": price}


@pytest.fixture
def setup(clean_database: str) -> Iterator[Any]:
    opened: list[Any] = []

    def build(
        *outcomes: Any, graph: FakeGraph | None = None, corrections: list[Any] | None = None, dated: bool = True, **settings: Any
    ) -> tuple[Channel, ScriptedProvider]:
        # Screenshots usually show a date. Tests about missing dates pass dated=False.
        if dated:
            outcomes = tuple(
                {**outcome, "transaction_date": field("2026-11-01")}
                if isinstance(outcome, dict) and not (outcome.get("transaction_date") or {"found": True}).get("found", True)
                else outcome
                for outcome in (outcomes or (bag_conversation_output(),))
            )
        provider = ScriptedProvider(*outcomes) if corrections is None else TalkingProvider(*outcomes, corrections=corrections)
        client, channel = open_channel(clean_database, provider, graph=graph, **settings)
        opened.append(client)
        channel.graph.add_image("media-1", image_bytes())
        channel.graph.add_image("media-2", image_bytes(size=(400, 900)))
        return channel, provider

    yield build
    for client in opened:
        client.__exit__(None, None, None)


def saved(channel: Channel) -> list[dict[str, Any]]:
    return channel.rows("SELECT * FROM chatreceipt.transactions ORDER BY created_at")


def sign_in(channel: Channel, who: dict[str, str]) -> dict[str, str]:
    channel.say(who, "web")
    code = channel.graph.last_body().split("#code=")[1].split()[0]
    response = channel.client.post("/api/records/session", json={"code": code})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def draft_ready(channel: Channel, who: dict[str, str] = ALICE, media_id: str = "media-1") -> None:
    channel.say(who, "hi")
    channel.send_image(who, media_id)


# -- webhook security ---------------------------------------------------------------------------


def test_webhook_verification(setup: Any) -> None:
    channel, _ = setup()
    query = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"}
    accepted = channel.client.get("/api/whatsapp/webhook", params=query)
    assert (accepted.status_code, accepted.text) == (200, "1158201444")
    assert channel.client.get("/api/whatsapp/webhook", params={**query, "hub.verify_token": "wrong"}).status_code == 403
    assert channel.client.get("/api/whatsapp/webhook", params={**query, "hub.mode": "unsubscribe"}).status_code == 403


@pytest.mark.parametrize("signature", [None, "sha256=deadbeef", "md5=abc"])
def test_unsigned_or_forged_deliveries_are_refused(setup: Any, signature: str | None) -> None:
    channel, _ = setup()
    body = channel.payload(channel.text_event(ALICE, "hi"))
    assert channel.post(body, signature=signature).status_code == 401
    assert channel.rows("SELECT * FROM chatreceipt.whatsapp_inbound") == []


def test_events_for_another_business_number_are_ignored(setup: Any) -> None:
    channel, _ = setup()
    response = channel.post(channel.payload(channel.text_event(ALICE, "hi"), phone_number_id="999"))
    assert response.status_code == 200
    assert channel.rows("SELECT * FROM chatreceipt.customers") == []


def test_a_signed_but_unreadable_delivery_is_acknowledged_not_retried(setup: Any) -> None:
    channel, _ = setup()
    assert channel.post(b"{not json").status_code == 200


# -- the main journey ---------------------------------------------------------------------------


def test_a_new_customer_is_welcomed_with_the_menu(setup: Any) -> None:
    channel, _ = setup()
    channel.say(ALICE, "Hi ChatReceipt, I'd like to scan a receipt")
    assert "ChatReceipt doesn't keep the image" in channel.graph.last_body()
    assert channel.graph.offered() == {"Scan a receipt", "Recent records", "Get help"}
    assert channel.graph.sent[-1]["to"] == ALICE["phone"]


def test_valid_receipt_from_screenshot_to_saved_record_and_website(setup: Any) -> None:
    channel, provider = setup()
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")

    # Acknowledged, read by the existing extraction service, and summarised from backend totals.
    assert any("reading your screenshot" in body for body in channel.graph.bodies())
    assert len(provider.calls) == 1
    summary = channel.graph.last_body()
    assert "Bag" in summary and "₦17,000.00" in summary and "₦19,000.00" in summary
    assert "ChatReceipt doesn't verify payments" in summary
    assert channel.graph.offered() == {"Confirm", "Correct", "Cancel"}
    assert saved(channel) == []  # nothing is saved before the customer confirms

    channel.tap(ALICE, "Confirm")
    records = saved(channel)
    assert len(records) == 1
    assert records[0]["reference"] == "CR-000001"
    assert str(records[0]["total"].normalize()) in ("1.9E+4", "19000")
    assert records[0]["payment_status"] == "unpaid"
    assert "Saved as *CR-000001*" in channel.graph.last_body()

    channel.say(ALICE, "recent")
    assert "CR-000001" in channel.graph.last_body() and "₦19,000.00" in channel.graph.last_body()

    headers = sign_in(channel, ALICE)
    listing = channel.client.get("/api/records", headers=headers)
    assert listing.status_code == 200
    assert [row["reference"] for row in listing.json()["records"]] == ["CR-000001"]
    detail = channel.client.get(f"/api/records/{records[0]['id']}", headers=headers).json()
    assert detail["calculation"]["total"] == "19000.00"

    # The website's existing export accepts the saved draft unchanged.
    export = channel.client.post("/api/documents/pdf", json={"draft": detail["draft"]})
    assert export.status_code == 200 and export.content.startswith(b"%PDF")


def test_screenshots_sent_together_are_read_as_one_conversation(setup: Any) -> None:
    channel, provider = setup()
    channel.say(ALICE, "hi")
    first, second = channel.image_event(ALICE, "media-1"), channel.image_event(ALICE, "media-2")
    assert channel.deliver(first, second).status_code == 200
    channel.work()
    assert len(provider.calls) == 1
    assert [image.height for image in provider.calls[0]] == [800, 900]
    assert sum("reading your screenshot" in body for body in channel.graph.bodies()) == 1


def test_a_document_sent_as_an_image_file_is_accepted(setup: Any) -> None:
    channel, provider = setup()
    channel.say(ALICE, "hi")
    event = channel._message(ALICE, "document", document={"id": "media-1", "mime_type": "image/png", "filename": "shot.png"})
    channel.deliver(event)
    channel.work()
    assert len(provider.calls) == 1


# -- duplicates ---------------------------------------------------------------------------------


def test_a_repeated_webhook_delivery_is_processed_once(setup: Any) -> None:
    channel, provider = setup()
    channel.say(ALICE, "hi")
    event = channel.image_event(ALICE, "media-1", message_id="wamid.same")
    for _ in range(3):
        assert channel.deliver(event).status_code == 200
    channel.work()
    assert len(provider.calls) == 1
    assert sum("reading your screenshot" in body for body in channel.graph.bodies()) == 1
    assert len(channel.rows("SELECT * FROM chatreceipt.whatsapp_submissions")) == 1


def test_tapping_confirm_twice_saves_one_record(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    confirm = channel.graph.option("Confirm")
    channel.deliver(channel.tap_event(ALICE, confirm), channel.tap_event(ALICE, confirm))
    channel.work()
    assert len(saved(channel)) == 1
    assert "already saved as *CR-000001*" in channel.graph.last_body()


def test_the_same_screenshot_saved_before_is_pointed_out(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.tap(ALICE, "Confirm")
    channel.send_image(ALICE, "media-1", sha256="hash-media-1")
    assert "already saved a record from this screenshot (CR-000001)" in channel.graph.everything_said()


# -- invalid images -----------------------------------------------------------------------------


def test_an_unreadable_image_is_explained_and_never_reaches_the_ai(setup: Any) -> None:
    channel, provider = setup()
    channel.graph.add_image("media-bad", b"this is not an image")
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-bad")
    assert provider.calls == []
    assert "isn't a JPEG, PNG or WebP image" in channel.graph.last_body()
    assert channel.rows("SELECT status FROM chatreceipt.whatsapp_submissions") == [{"status": "failed"}]


def test_an_oversized_image_is_refused_before_download(setup: Any) -> None:
    channel, provider = setup()
    channel.graph.add_image("media-big", b"x" * 100, file_size=50 * 1024 * 1024)
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-big")
    assert provider.calls == []
    assert not any(request.url.host == MEDIA_HOST for request in channel.graph.requests)
    assert "couldn't download that image" in channel.graph.last_body()


def test_other_file_types_get_a_plain_explanation(setup: Any) -> None:
    channel, provider = setup()
    channel.say(ALICE, "hi")
    channel.deliver(channel._message(ALICE, "document", document={"id": "doc-1", "mime_type": "application/pdf"}))
    channel.work()
    assert provider.calls == []
    assert "I can only read screenshots" in channel.graph.last_body()


def test_the_access_token_is_never_sent_to_an_unknown_media_host(setup: Any) -> None:
    channel, provider = setup()
    channel.graph.add_image("media-evil", image_bytes(), url="https://evil.example/steal")
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-evil")
    assert provider.calls == []
    assert not any(request.url.host == "evil.example" for request in channel.graph.requests)


# -- the receipt the customer receives ----------------------------------------------------------


def test_confirming_sends_the_receipt_image_and_offers_a_pdf(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.tap(ALICE, "Confirm")

    image, note = channel.graph.sent[-2:]
    assert image["type"] == "image" and image["image"]["id"].startswith("media-out-")
    caption = image["image"]["caption"]
    assert "CR-000001" in caption and "₦19,000.00" in caption and "doesn't verify payments" in caption
    assert "Saved as *CR-000001*" in channel.graph.body_of(note)
    assert channel.graph.offered() == {"Get PDF", "Scan another", "Recent records"}

    renderer = channel.client.app.state.renderer
    assert [call[0] for call in renderer.calls] == ["png"]
    assert "CR-000001" in renderer.calls[0][1] and "Brown" not in caption
    assert b"image/png" in channel.graph.uploads[0] and b"ChatReceipt-CR-000001.png" in channel.graph.uploads[0]

    channel.tap(ALICE, "Get PDF")
    document = channel.graph.sent[-1]
    assert document["type"] == "document" and document["document"]["filename"] == "ChatReceipt-CR-000001.pdf"
    assert renderer.calls[-1][0] == "pdf" and b"application/pdf" in channel.graph.uploads[-1]
    assert channel.rows("SELECT id FROM chatreceipt.whatsapp_outbound WHERE payload <> '{}'::jsonb") == []


def test_a_customer_cannot_get_another_customers_receipt(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.tap(ALICE, "Confirm")
    alices_button = channel.graph.option("Get PDF")
    channel.say(BOLA, "hi")
    assert channel.deliver(channel.tap_event(BOLA, alices_button)).status_code == 200
    channel.work()
    assert "no longer available" in channel.graph.last_body()
    assert channel.graph.sent[-1]["to"] == BOLA["phone"]
    assert not any(message["type"] == "document" for message in channel.graph.sent)


def test_a_failed_receipt_still_saves_the_record_and_can_be_tried_again(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    renderer = channel.client.app.state.renderer
    renderer.error = DocumentRenderError("failed", "browser stopped")
    channel.tap(ALICE, "Confirm")

    assert len(saved(channel)) == 1
    assert [call[0] for call in renderer.calls] == ["png", "png", "png"]
    assert "couldn't create the receipt file" in channel.graph.everything_said()
    assert "Saved as *CR-000001*" in channel.graph.last_body()
    assert not any(message["type"] == "image" for message in channel.graph.sent)

    renderer.error = None
    channel.tap(ALICE, "Get PDF")
    assert channel.graph.sent[-1]["type"] == "document"


def test_a_temporary_upload_failure_is_retried_and_replies_stay_in_order(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.graph.upload_errors.append(httpx.Response(503, json={"error": {"code": 131000}}))
    channel.tap(ALICE, "Confirm")
    assert [message["type"] for message in channel.graph.sent[-2:]] == ["image", "interactive"]
    assert len(channel.graph.uploads) == 1


# -- the real conversation, and natural corrections ---------------------------------------------


def transfer_read_as_the_price() -> dict[str, Any]:
    """How the first real test's screenshot was read: a ₦20,000 transfer taken as the whole price."""
    return bag_conversation_output(
        seller=field("Bola Studio"),
        buyer=field("Emeka Daniel Obi"),
        transaction_date=field("2026-11-10"),
        line_items=[line_item("Transfer", "1", "20000")],
        delivery_fee=field(""),
        amount_paid=field("20000"),
        payment_status=field("paid"),
        payment_terms=field(""),
        ambiguities=[{"field": "agreed_total", "description": "The buyer said “No” when asked if ₦20,000 was the full amount."}],
    )


def advance_without_a_price() -> dict[str, Any]:
    """How the same screenshot reads under the corrected rule: a payment, with the price left unknown."""
    return bag_conversation_output(
        line_items=[line_item("Advance work", "1", "")],
        delivery_fee=field(""),
        amount_paid=field("20000"),
        payment_status=field("partially_paid"),
        payment_terms=field(""),
    )


def test_the_real_conversation_ends_with_the_right_figures(setup: Any) -> None:
    channel, provider = setup(
        transfer_read_as_the_price(),
        corrections=[said(change("item_description", "Advance work", item_id="item-1"), change("balance", "40k"))],
    )
    draft_ready(channel)
    assert "said “No”" in channel.graph.last_body()

    # "can you try again" reads the screenshot again instead of repeating the draft.
    channel.say(ALICE, "can you try again")
    assert len(provider.calls) == 2
    assert "Reading your screenshot again" in channel.graph.everything_said()
    assert "Here's what I found" in channel.graph.last_body()

    # "It's an advance work. 40k was still remaining" is understood, and the conflict is asked about once.
    channel.say(ALICE, "It's an advance work. 40k was still remaining")
    update = channel.graph.last_body()
    assert "Item: “Advance work”" in update and "Still owed: ₦40,000.00" in update
    assert "So the full price was *₦60,000.00* (₦20,000.00 paid + ₦40,000.00 still owed)?" in update
    assert channel.graph.offered() == {"Yes, ₦60,000", "No, keep as it is", "Correct"}
    assert "*Seller:*" not in update  # a short update, not the whole draft again

    channel.tap(ALICE, "Yes, ₦60,000")
    update = channel.graph.last_body()
    assert "Price of “Advance work”: ₦60,000.00" in update and "🟡 Partially paid" in update
    assert "said “No”" not in update  # settled by the corrected price
    assert "Confirm" in channel.graph.offered()

    channel.tap(ALICE, "Confirm")
    [record] = saved(channel)
    assert (str(record["total"].normalize()), str(record["amount_paid"].normalize()), str(record["outstanding"].normalize())) == ("6E+4", "2E+4", "4E+4")
    assert record["payment_status"] == "partially_paid"
    assert channel.graph.sent[-2]["type"] == "image"
    request = provider.requests[0].parts[0].text  # type: ignore[attr-defined]
    assert "It's an advance work. 40k was still remaining" in request and "can you try again" not in request


def test_the_ai_marking_it_part_paid_still_leads_to_the_one_tap_fix(setup: Any) -> None:
    # What the real AI answered: it also set the status, which the ₦20,000 "price" contradicts.
    channel, _ = setup(
        transfer_read_as_the_price(),
        corrections=[said(change("item_description", "Advance work", item_id="item-1"), change("balance", "40k"), change("payment_status", "partially_paid"))],
    )
    draft_ready(channel)
    channel.say(ALICE, "It's an advance work. 40k was still remaining")
    assert channel.graph.offered() == {"Yes, ₦60,000", "No, keep as it is", "Correct"}
    channel.tap(ALICE, "Yes, ₦60,000")
    assert "Confirm" in channel.graph.offered() and "🟡 Partially paid" in channel.graph.last_body()


def test_a_part_payment_as_large_as_the_price_asks_for_the_full_price(setup: Any) -> None:
    channel, provider = setup(
        transfer_read_as_the_price(),
        corrections=[said(change("item_description", "Advance work", item_id="item-1"), change("item_price", "20k", item_id="item-1"), change("payment_status", "partially_paid"))],
    )
    draft_ready(channel)
    channel.say(ALICE, "It's a 20k for advance work")
    assert "What was the full price of “Advance work”?" in channel.graph.last_body()
    channel.say(ALICE, "60k")
    assert len(provider.requests) == 1  # the plain amount needed no AI
    assert "Confirm" in channel.graph.offered() and "₦40,000.00" in channel.graph.last_body()


def test_short_price_replies_need_no_ai(setup: Any) -> None:
    channel, provider = setup(advance_without_a_price(), corrections=[])
    draft_ready(channel)
    assert "What was the full price of “Advance work”?" in channel.graph.last_body()
    assert "₦20,000.00 has been paid so far" in channel.graph.last_body()
    channel.say(ALICE, "60k is the price")
    assert provider.requests == []
    assert "Price of “Advance work”: ₦60,000.00" in channel.graph.last_body()
    assert "Remaining" in channel.graph.last_body() and "₦40,000.00" in channel.graph.last_body()
    assert "Confirm" in channel.graph.offered()


def test_a_reply_with_more_than_a_price_is_understood_by_the_ai(setup: Any) -> None:
    channel, provider = setup(
        advance_without_a_price(),
        corrections=[said(change("total", "60k"), change("amount_paid", "20k"), change("balance", "40k"))],
    )
    draft_ready(channel)
    channel.say(ALICE, "The full work was 60k. He paid 20k and still owes 40k.")
    assert len(provider.requests) == 1
    assert "What was the full price" in provider.requests[0].parts[0].text  # the question being answered
    body = channel.graph.last_body()
    assert "Full price of “Advance work”: ₦60,000.00" in body and "🟡 Partially paid" in body
    assert "Confirm" in channel.graph.offered()


def test_several_corrections_in_one_message(setup: Any) -> None:
    channel, _ = setup(
        transfer_read_as_the_price(),
        corrections=[said(change("buyer", "Emeka"), change("total", "60k"), change("amount_paid", "20k"))],
    )
    draft_ready(channel)
    channel.say(ALICE, "Change the buyer to Emeka, the total to 60k, and mark 20k as paid.")
    body = channel.graph.last_body()
    assert "Buyer: Emeka" in body and "₦60,000.00" in body and "Paid so far: ₦20,000.00" in body
    assert "🟡 Partially paid" in body
    stored = channel.rows("SELECT draft, edited_fields FROM chatreceipt.whatsapp_submissions")[0]
    assert stored["draft"]["buyer"] == "Emeka" and stored["draft"]["payment_status"] == "partially_paid"
    assert {"buyer", "amount_paid", "line_items.item-1.unit_price"} <= set(stored["edited_fields"])


def test_when_the_ai_cant_be_reached_nothing_changes_and_the_customer_is_told(setup: Any) -> None:
    channel, _ = setup(transfer_read_as_the_price(), corrections=[ExtractionProviderError(ErrorKind.UNAVAILABLE, "down")])
    draft_ready(channel)
    before = channel.rows("SELECT draft FROM chatreceipt.whatsapp_submissions")[0]["draft"]
    channel.say(ALICE, "part of it is still owed for the work, the rest comes next week")
    assert "couldn't work that out just now" in channel.graph.last_body()
    assert channel.rows("SELECT draft, instruction FROM chatreceipt.whatsapp_submissions")[0] == {"draft": before, "instruction": None}


def test_an_unclear_message_gets_one_question(setup: Any) -> None:
    channel, _ = setup(transfer_read_as_the_price(), corrections=[said(intent="unclear", question="Is 20k the price or what was paid?")])
    draft_ready(channel)
    channel.say(ALICE, "20k sha")
    assert channel.graph.last_body().startswith("Is 20k the price or what was paid?")


def test_the_ai_can_recognise_a_request_to_read_again(setup: Any) -> None:
    channel, provider = setup(transfer_read_as_the_price(), corrections=[said(intent="read_again")])
    draft_ready(channel)
    channel.say(ALICE, "abeg check am again")
    assert len(provider.calls) == 2 and "Reading your screenshot again" in channel.graph.everything_said()


def test_a_tap_sent_while_a_correction_is_being_understood_waits_for_it(setup: Any) -> None:
    channel, _ = setup(
        transfer_read_as_the_price(),
        corrections=[said(change("item_description", "Advance work", item_id="item-1"), change("balance", "40k"))],
    )
    draft_ready(channel)
    confirm = channel.graph.option("Confirm")
    assert channel.deliver(channel.text_event(ALICE, "It's an advance work. 40k was still remaining")).status_code == 200
    assert channel.deliver(channel.tap_event(ALICE, confirm)).status_code == 200
    channel.work()
    # The correction was applied first, so the older Confirm button can't save the old figures.
    assert saved(channel) == []
    assert "out of date" in channel.graph.last_body()


def test_reading_again_after_corrections_asks_first(setup: Any) -> None:
    channel, provider = setup(transfer_read_as_the_price())
    draft_ready(channel)
    channel.tap(ALICE, "Correct")
    channel.tap(ALICE, "Seller")
    channel.say(ALICE, "Bola Works")
    channel.say(ALICE, "try again")
    assert channel.graph.offered() == {"Read it again", "Keep my draft"}
    assert len(provider.calls) == 1
    channel.tap(ALICE, "Read it again")
    assert len(provider.calls) == 2


def test_try_again_after_a_failure_reads_the_same_screenshot(setup: Any) -> None:
    channel, provider = setup(ExtractionProviderError(ErrorKind.BAD_REQUEST, "rejected"), transfer_read_as_the_price())
    draft_ready(channel)
    assert "nothing was saved" in channel.graph.last_body()
    assert "Try again" in channel.graph.offered()
    channel.tap(ALICE, "Try again")
    assert len(provider.calls) == 2
    assert "Here's what I found" in channel.graph.last_body()


def test_show_brings_back_the_whole_draft(setup: Any) -> None:
    channel, _ = setup(transfer_read_as_the_price(), corrections=[said(change("buyer", "Emeka"))])
    draft_ready(channel)
    channel.say(ALICE, "the buyer is Emeka")
    assert "*Seller:*" not in channel.graph.last_body()
    channel.say(ALICE, "show")
    assert "*Seller:* Bola Studio" in channel.graph.last_body() and "*Buyer:* Emeka _(edited by you)_" in channel.graph.last_body()


# -- missing details and corrections ------------------------------------------------------------


def test_missing_details_block_confirmation_until_corrected(setup: Any) -> None:
    channel, _ = setup(bag_conversation_output(line_items=[line_item("Bag", "1", "")], delivery_fee=field("")))
    draft_ready(channel)
    assert channel.graph.offered() == {"Correct", "Cancel"}
    assert "What's the price of one “Bag”?" in channel.graph.last_body()

    channel.tap(ALICE, "Correct")
    channel.tap(ALICE, "Items")
    channel.tap(ALICE, "Item 1")
    channel.tap(ALICE, "Unit price")
    channel.say(ALICE, "₦17,000")
    assert "Confirm" in channel.graph.offered()
    assert "Price of “Bag”: ₦17,000.00" in channel.graph.last_body()

    channel.tap(ALICE, "Confirm")
    records = saved(channel)
    assert len(records) == 1 and records[0]["edited_fields"] == 1
    assert records[0]["draft"]["line_items"][0]["unit_price"] == "17000"


def test_a_missing_price_can_simply_be_typed(setup: Any) -> None:
    channel, _ = setup(bag_conversation_output(line_items=[line_item("Bag", "1", "")], delivery_fee=field("")))
    draft_ready(channel)
    assert "What's the price of one “Bag”?" in channel.graph.last_body()
    assert channel.graph.offered() == {"Correct", "Cancel"}

    channel.say(ALICE, "seventeen")
    assert "Send the price as a number" in channel.graph.last_body()
    assert saved(channel) == []

    channel.say(ALICE, "17k")
    assert "Confirm" in channel.graph.offered() and "₦17,000.00" in channel.graph.last_body()
    assert "Price of “Bag”: ₦17,000.00" in channel.graph.last_body()
    assert saved(channel) == []  # typing the price never saves the record by itself

    channel.tap(ALICE, "Confirm")
    assert saved(channel)[0]["draft"]["line_items"][0]["unit_price"] == "17000"


def test_each_missing_price_is_asked_for_in_turn(setup: Any) -> None:
    channel, _ = setup(bag_conversation_output(line_items=[line_item("Bag", "1", ""), line_item("Belt", "2", "")], delivery_fee=field("")))
    draft_ready(channel)
    assert "“Bag”" in channel.graph.last_body().rsplit("\n\n", 1)[-1]
    channel.say(ALICE, "17000")
    assert "“Belt”" in channel.graph.last_body().rsplit("\n\n", 1)[-1]
    channel.say(ALICE, "5000")
    assert "Confirm" in channel.graph.offered()


def test_text_is_not_taken_as_a_value_when_nothing_is_missing(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.say(ALICE, "20000")
    assert "You have a draft waiting" in channel.graph.last_body()
    stored = channel.rows("SELECT draft FROM chatreceipt.whatsapp_submissions")[0]["draft"]
    assert stored["line_items"][0]["unit_price"] == "17000"


def test_a_typed_confirm_cannot_save_an_incomplete_draft(setup: Any) -> None:
    channel, _ = setup(bag_conversation_output(line_items=[line_item("Bag", "1", "")]))
    draft_ready(channel)
    channel.say(ALICE, "confirm")
    assert saved(channel) == []
    assert "can't be saved yet" in channel.graph.last_body()


@pytest.mark.parametrize(
    ("value", "explanation"),
    [("seventeen", "Send the price as a number"), ("-500", "Send the price as a number"), ("17000.555", "at most 2 decimal places")],
)
def test_invalid_corrections_are_refused_and_asked_again(setup: Any, value: str, explanation: str) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.tap(ALICE, "Correct")
    channel.tap(ALICE, "Items")
    channel.tap(ALICE, "Item 1")
    channel.tap(ALICE, "Unit price")
    channel.say(ALICE, value)
    assert explanation in channel.graph.last_body()
    stored = channel.rows("SELECT draft FROM chatreceipt.whatsapp_submissions")[0]["draft"]
    assert stored["line_items"][0]["unit_price"] == "17000"
    channel.say(ALICE, "back")
    assert "Confirm" in channel.graph.offered()


def test_a_correction_updates_the_backend_totals(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.tap(ALICE, "Correct")
    channel.tap(ALICE, "Delivery fee")
    channel.say(ALICE, "3.5k")
    assert "₦3,500.00" in channel.graph.last_body() and "₦20,500.00" in channel.graph.last_body()


def test_part_payment_asks_for_the_amount_and_updates_the_balance(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.tap(ALICE, "Correct")
    channel.tap(ALICE, "Payment")
    channel.tap(ALICE, "Partially paid")
    assert "amount paid" in channel.graph.last_body()
    channel.say(ALICE, "5000")
    summary = channel.graph.last_body()
    assert "₦5,000.00" in summary and "₦14,000.00" in summary and "Confirm" in channel.graph.offered()


def test_items_can_be_added_and_removed(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.tap(ALICE, "Correct")
    channel.tap(ALICE, "Items")
    channel.tap(ALICE, "Add an item")
    channel.say(ALICE, "Leather belt")
    channel.say(ALICE, "2")
    channel.say(ALICE, "5k")
    assert "Leather belt" in channel.graph.last_body() and "₦29,000.00" in channel.graph.last_body()
    channel.tap(ALICE, "Correct")
    channel.tap(ALICE, "Items")
    channel.tap(ALICE, "Item 2")
    channel.tap(ALICE, "Remove this item")
    assert "Removed “Leather belt”" in channel.graph.last_body() and "₦19,000.00" in channel.graph.last_body()
    stored = channel.rows("SELECT draft FROM chatreceipt.whatsapp_submissions")[0]["draft"]
    assert [row["description"] for row in stored["line_items"]] == ["Bag"]


def test_an_old_button_cannot_confirm_a_changed_draft(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    stale_confirm = channel.graph.option("Confirm")
    channel.tap(ALICE, "Correct")
    channel.tap(ALICE, "Seller")
    channel.say(ALICE, "Tola Bags")
    channel.deliver(channel.tap_event(ALICE, stale_confirm))
    channel.work()
    assert saved(channel) == []
    assert "out of date" in channel.graph.last_body()
    channel.tap(ALICE, "Confirm")
    assert saved(channel)[0]["seller"] == "Tola Bags"


def test_cancelling_saves_nothing(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.tap(ALICE, "Cancel")
    assert saved(channel) == []
    assert "Cancelled" in channel.graph.last_body()
    channel.say(ALICE, "cancel")
    assert "no draft" in channel.graph.last_body()


def test_screenshots_sent_while_a_draft_waits_can_be_added_or_start_afresh(setup: Any) -> None:
    channel, provider = setup()
    draft_ready(channel)
    channel.send_image(ALICE, "media-2")
    assert channel.graph.offered() == {"Add to this record", "Start a new record"}
    channel.tap(ALICE, "Add to this record")
    assert len(provider.calls) == 2 and len(provider.calls[1]) == 2

    channel.graph.add_image("media-3", image_bytes(size=(400, 700)))
    channel.send_image(ALICE, "media-3")
    channel.tap(ALICE, "Start a new record")
    assert len(provider.calls) == 3 and len(provider.calls[2]) == 1
    statuses = [row["status"] for row in channel.rows("SELECT status FROM chatreceipt.whatsapp_submissions ORDER BY created_at")]
    assert statuses == ["cancelled", "awaiting_confirmation"]


def test_a_conversation_without_a_transaction_is_explained(setup: Any) -> None:
    channel, _ = setup(bag_conversation_output(contains_transaction=False, line_items=[]))
    draft_ready(channel)
    assert "couldn't find a transaction" in channel.graph.last_body()
    assert saved(channel) == []


# -- AI failures --------------------------------------------------------------------------------


def test_temporary_ai_failures_are_retried_and_the_customer_is_told_once(setup: Any) -> None:
    channel, provider = setup(
        ExtractionProviderError(ErrorKind.UNAVAILABLE), ExtractionProviderError(ErrorKind.TIMEOUT), bag_conversation_output()
    )
    draft_ready(channel)
    assert len(provider.calls) == 3
    assert sum("taking longer than usual" in body for body in channel.graph.bodies()) == 1
    assert "Confirm" in channel.graph.offered()


def test_ai_failures_that_never_clear_end_with_a_clear_message(setup: Any) -> None:
    channel, provider = setup(ExtractionProviderError(ErrorKind.RATE_LIMITED), whatsapp_extraction_max_attempts=3)
    draft_ready(channel)
    assert len(provider.calls) == 3
    assert "nothing was saved" in channel.graph.last_body()
    assert channel.rows("SELECT status, failure_code FROM chatreceipt.whatsapp_submissions") == [{"status": "failed", "failure_code": "ai_rate_limited"}]
    channel.send_image(ALICE, "media-2")  # the customer can simply try again
    assert len(channel.rows("SELECT id FROM chatreceipt.whatsapp_submissions")) == 2


def test_a_refused_screenshot_is_not_retried(setup: Any) -> None:
    channel, provider = setup(ExtractionProviderError(ErrorKind.REFUSED))
    draft_ready(channel)
    assert len(provider.calls) == 1
    assert "can't process that screenshot" in channel.graph.last_body()


def test_a_malformed_ai_answer_is_retried(setup: Any) -> None:
    channel, provider = setup({"nope": 1}, bag_conversation_output())
    draft_ready(channel)
    assert len(provider.calls) == 2
    assert "Confirm" in channel.graph.offered()


def test_a_worker_that_stopped_mid_read_is_taken_over_without_a_second_draft(setup: Any) -> None:
    channel, provider = setup()
    channel.say(ALICE, "hi")
    channel.deliver(channel.image_event(ALICE, "media-1"))
    worker = channel.client.app.state.whatsapp_worker  # type: ignore[attr-defined]
    channel.client.portal.call(worker.handle_next_inbound)
    stale = worker._claim_submission()  # this worker "crashes" here
    assert stale is not None
    with channel.database.transaction() as conn:
        conn.execute("UPDATE chatreceipt.whatsapp_submissions SET lease_expires_at = now() - interval '1 second'")
    channel.work()
    assert len(provider.calls) == 1 and "Confirm" in channel.graph.offered()

    # If the first worker wakes up and finishes late, its result is discarded.
    from app.whatsapp.processing import Failure, finish_failure

    before = len(channel.graph.sent)
    with channel.database.transaction() as conn:
        finish_failure(conn, channel.client.app.state.settings, stale, Failure("x", "late", False))  # type: ignore[attr-defined]
    channel.work()
    assert len(channel.graph.sent) == before


# -- WhatsApp API failures ----------------------------------------------------------------------


def _graph_error(status: int, code: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "x"}})


def test_replies_are_retried_after_temporary_failures_and_stay_in_order(setup: Any) -> None:
    graph = FakeGraph()
    graph.send_errors.extend([_graph_error(500, 131000), _graph_error(429, 130429)])
    channel, _ = setup(graph=graph)
    draft_ready(channel)
    bodies = channel.graph.bodies()
    assert "ChatReceipt doesn't keep the image" in bodies[0]
    assert "reading your screenshot" in bodies[1]
    assert "Here's what I found" in bodies[2]
    assert channel.rows("SELECT count(*) AS n FROM chatreceipt.whatsapp_outbound WHERE status <> 'sent'") == [{"n": 0}]


def test_a_reply_outside_the_customer_window_is_not_retried_forever(setup: Any) -> None:
    graph = FakeGraph()
    graph.send_errors.append(_graph_error(400, 131047))
    channel, _ = setup(graph=graph)
    channel.say(ALICE, "hi")
    channel.say(ALICE, "help")
    rows = channel.rows("SELECT status, attempts, last_error_code, payload FROM chatreceipt.whatsapp_outbound ORDER BY id")
    assert rows[0]["status"] == "failed" and rows[0]["attempts"] == 1 and rows[0]["last_error_code"] == "131047"
    assert rows[0]["payload"] == {}
    assert rows[1]["status"] == "sent"


def test_a_temporary_media_failure_is_retried(setup: Any) -> None:
    graph = FakeGraph()
    graph.lookup_errors.append(_graph_error(503, 2))
    channel, provider = setup(graph=graph)
    draft_ready(channel)
    assert len(provider.calls) == 1 and "Confirm" in channel.graph.offered()


def test_expired_media_asks_the_customer_to_send_it_again(setup: Any) -> None:
    channel, provider = setup()
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-gone")
    assert provider.calls == []
    assert "couldn't download that image" in channel.graph.last_body()


# -- customer isolation -------------------------------------------------------------------------


def test_one_customer_cannot_act_on_anothers_draft(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel, ALICE)
    alices_confirm = channel.graph.option("Confirm")
    channel.say(BOLA, "hi")
    channel.deliver(channel.tap_event(BOLA, alices_confirm))
    channel.work()
    assert saved(channel) == []
    assert "no longer available" in channel.graph.last_body()
    assert channel.graph.sent[-1]["to"] == BOLA["phone"]


def test_one_customer_cannot_read_anothers_records(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel, ALICE)
    channel.tap(ALICE, "Confirm")
    record_id = saved(channel)[0]["id"]

    channel.say(BOLA, "hi")
    channel.say(BOLA, "recent")
    assert "CR-000001" not in channel.graph.last_body()
    headers = sign_in(channel, BOLA)
    assert channel.client.get("/api/records", headers=headers).json()["records"] == []
    assert channel.client.get(f"/api/records/{record_id}", headers=headers).status_code == 404


def test_records_require_a_valid_session(setup: Any) -> None:
    channel, _ = setup()
    assert channel.client.get("/api/records").status_code == 401
    assert channel.client.get("/api/records", headers={"Authorization": "Bearer made-up"}).status_code == 401


def test_a_sign_in_link_works_once(setup: Any) -> None:
    channel, _ = setup()
    channel.say(ALICE, "hi")
    channel.say(ALICE, "web")
    code = channel.graph.last_body().split("#code=")[1].split()[0]
    assert channel.client.post("/api/records/session", json={"code": code}).status_code == 200
    second = channel.client.post("/api/records/session", json={"code": code})
    assert second.status_code == 401 and second.json()["error"]["code"] == "link_expired"


def test_an_expired_sign_in_link_is_refused(setup: Any) -> None:
    channel, _ = setup()
    channel.say(ALICE, "hi")
    channel.say(ALICE, "web")
    code = channel.graph.last_body().split("#code=")[1].split()[0]
    with channel.database.transaction() as conn:
        conn.execute("UPDATE chatreceipt.web_sign_in_codes SET expires_at = now() - interval '1 minute'")
    assert channel.client.post("/api/records/session", json={"code": code}).status_code == 401


def test_a_session_can_be_ended(setup: Any) -> None:
    channel, _ = setup()
    channel.say(ALICE, "hi")
    headers = sign_in(channel, ALICE)
    assert channel.client.post("/api/records/session/end", headers=headers).status_code == 204
    assert channel.client.get("/api/records", headers=headers).status_code == 401


def test_a_reassigned_phone_number_does_not_inherit_the_previous_owners_records(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel, ALICE)
    channel.tap(ALICE, "Confirm")

    new_owner = {"user_id": "NG.9999999999999999999", "phone": ALICE["phone"]}
    channel.say(new_owner, "hi")
    channel.say(new_owner, "recent")
    assert "CR-000001" not in channel.graph.last_body()
    customers = channel.rows("SELECT whatsapp_user_id, whatsapp_phone FROM chatreceipt.customers ORDER BY created_at")
    assert customers[0] == {"whatsapp_user_id": ALICE["user_id"], "whatsapp_phone": None}
    assert customers[1] == {"whatsapp_user_id": new_owner["user_id"], "whatsapp_phone": ALICE["phone"]}


def test_a_customer_who_changes_number_keeps_their_records(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel, ALICE)
    channel.tap(ALICE, "Confirm")

    moved = {"user_id": "NG.2000000000000000001", "phone": "2348111111111"}
    system = channel._message(
        ALICE,
        "system",
        system={"type": "user_changed_number", "user_id": moved["user_id"], "previous_user_id": ALICE["user_id"], "wa_id": moved["phone"], "body": "changed"},
    )
    channel.deliver(system)
    channel.say(moved, "recent")
    assert "CR-000001" in channel.graph.last_body()


def test_customers_without_a_shared_phone_number_are_addressed_by_user_id(setup: Any) -> None:
    channel, _ = setup()
    username_user = {"user_id": "NG.3000000000000000001"}
    channel.say(username_user, "hi")
    assert channel.graph.sent[-1]["recipient"] == username_user["user_id"]
    assert "to" not in channel.graph.sent[-1]


def test_deleting_my_data_removes_everything_and_says_goodbye(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    channel.tap(ALICE, "Confirm")
    channel.say(ALICE, "delete my data")
    assert channel.graph.offered() == {"Delete everything", "Keep my data"}
    channel.tap(ALICE, "Keep my data")
    assert len(saved(channel)) == 1

    channel.say(ALICE, "delete my data")
    channel.tap(ALICE, "Delete everything")
    assert saved(channel) == []
    assert channel.rows("SELECT * FROM chatreceipt.customers") == []
    assert "have been deleted" in channel.graph.last_body()


# -- database failure and abuse limits ----------------------------------------------------------


class FlakyDatabase(Database):
    down = False

    def transaction(self) -> Any:
        if FlakyDatabase.down:
            raise DatabaseUnavailable("down")
        return super().transaction()


def test_a_delivery_is_refused_while_the_database_is_down_and_accepted_on_redelivery(clean_database: str) -> None:
    provider = ScriptedProvider()
    client, channel = open_channel(clean_database, provider, database_factory=FlakyDatabase)
    try:
        event = channel.text_event(ALICE, "hi", message_id="wamid.retry")
        FlakyDatabase.down = True
        assert channel.deliver(event).status_code == 503
        FlakyDatabase.down = False
        assert channel.deliver(event).status_code == 200
        channel.work()
        assert len(channel.graph.sent) == 1
    finally:
        FlakyDatabase.down = False
        client.__exit__(None, None, None)


def test_a_failure_while_handling_a_message_rolls_back_and_retries(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    channel, _ = setup()
    real = conversation.handle_inbound
    calls = {"n": 0}

    def fails_once(conn: Any, settings: Any, row: Any, **options: Any) -> None:
        calls["n"] += 1
        real(conn, settings, row, **options)
        if calls["n"] == 1:
            raise psycopg.errors.UniqueViolation("conflict")

    monkeypatch.setattr("app.whatsapp.worker.handle_inbound", fails_once)
    channel.say(ALICE, "hi")
    assert calls["n"] == 2
    assert len(channel.graph.sent) == 1  # the first attempt's reply was rolled back with it
    assert channel.rows("SELECT status FROM chatreceipt.whatsapp_inbound") == [{"status": "handled"}]


def test_a_message_that_keeps_failing_gets_an_apology_and_stops(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    channel, _ = setup()

    def always_fails(conn: Any, settings: Any, row: Any, **options: Any) -> None:
        raise ValueError("bug")

    monkeypatch.setattr("app.whatsapp.worker.handle_inbound", always_fails)
    channel.say(ALICE, "hi")
    assert channel.rows("SELECT status, attempts FROM chatreceipt.whatsapp_inbound") == [{"status": "failed", "attempts": 5}]
    assert "something went wrong" in channel.graph.last_body()


def test_the_image_limit_stops_ai_spending_and_warns_once(setup: Any) -> None:
    channel, provider = setup(whatsapp_images_per_hour=1)
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    channel.tap(ALICE, "Cancel")
    channel.send_image(ALICE, "media-2")
    warning_count = sum("limit for screenshots" in body for body in channel.graph.bodies())
    channel.graph.add_image("media-3", image_bytes(size=(400, 700)))
    channel.send_image(ALICE, "media-3")
    assert len(provider.calls) == 1
    assert warning_count == 1
    assert sum("limit for screenshots" in body for body in channel.graph.bodies()) == 1


def test_message_floods_stop_getting_replies(setup: Any) -> None:
    channel, _ = setup(whatsapp_messages_per_minute=2)
    for _ in range(6):
        channel.say(ALICE, "hello")
    assert sum("very quickly" in body for body in channel.graph.bodies()) == 1
    assert len(channel.graph.sent) == 3


def test_two_workers_handling_the_same_confirmation_save_one_record(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    confirm = channel.graph.option("Confirm")
    channel.deliver(*[channel.tap_event(ALICE, confirm) for _ in range(6)])
    worker = channel.client.app.state.whatsapp_worker  # type: ignore[attr-defined]

    async def compete() -> None:
        async def run() -> None:
            while await worker.handle_next_inbound():
                pass

        await asyncio.gather(run(), run(), run())

    channel.client.portal.call(compete)
    channel.work()
    assert len(saved(channel)) == 1
    assert channel.rows("SELECT count(*) AS n FROM chatreceipt.whatsapp_inbound WHERE status <> 'handled'") == [{"n": 0}]


def test_screenshots_arriving_close_together_wait_for_each_other_within_a_limit(setup: Any) -> None:
    channel, provider = setup(whatsapp_album_wait_seconds=30, whatsapp_album_max_wait_seconds=45)
    channel.say(ALICE, "hi")
    channel.send_image(ALICE, "media-1")
    assert provider.calls == []  # still waiting for the rest of the album
    first = channel.rows("SELECT run_after, first_image_at FROM chatreceipt.whatsapp_submissions")[0]
    channel.send_image(ALICE, "media-2")
    second = channel.rows("SELECT run_after, first_image_at, images FROM chatreceipt.whatsapp_submissions")[0]
    assert len(second["images"]) == 2 and second["run_after"] >= first["run_after"]
    assert (second["run_after"] - second["first_image_at"]).total_seconds() <= 45


def test_old_drafts_and_finished_data_are_purged(setup: Any) -> None:
    channel, _ = setup()
    draft_ready(channel)
    with channel.database.transaction() as conn:
        conn.execute("UPDATE chatreceipt.whatsapp_submissions SET updated_at = now() - interval '4 days'")
        store.purge_expired(conn, draft_ttl_hours=72)
    row = channel.rows("SELECT status, draft, images FROM chatreceipt.whatsapp_submissions")[0]
    assert (row["status"], row["draft"], row["images"]) == ("cancelled", None, [])
    channel.tap(ALICE, "Confirm")  # the button from the expired draft
    assert saved(channel) == []
    assert "no longer available" in channel.graph.last_body()


def test_phone_number_id_is_in_the_send_path(setup: Any) -> None:
    channel, _ = setup()
    channel.say(ALICE, "hi")
    sends = [r for r in channel.graph.requests if r.method == "POST"]
    assert sends and sends[0].url.path == f"/v26.0/{PHONE_NUMBER_ID}/messages"
    assert sends[0].headers["authorization"] == "Bearer test-access-token"


def test_data_is_cleared_once_it_is_no_longer_needed(setup: Any) -> None:
    channel, _ = setup()
    channel.say(ALICE, "hello there")
    inbound = channel.rows("SELECT payload FROM chatreceipt.whatsapp_inbound")[0]
    outbound = channel.rows("SELECT payload, to_phone FROM chatreceipt.whatsapp_outbound")[0]
    assert inbound["payload"] == {}
    assert outbound == {"payload": {}, "to_phone": None}


def test_the_website_is_unaffected_when_whatsapp_is_not_configured() -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.samples import FakeProvider, FakeRenderer, make_settings

    app = create_app(settings=make_settings(), provider=FakeProvider(data=bag_conversation_output()), renderer=FakeRenderer())
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/whatsapp/webhook", content=b"{}").status_code == 503
        assert client.get("/api/records", headers={"Authorization": "Bearer x"}).status_code == 503


# -- dates, albums and corrections that need no AI ----------------------------------------------

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def lagos_today() -> Any:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Africa/Lagos")).date()


def test_a_receipt_with_no_date_asks_for_one_with_one_tap(setup: Any) -> None:
    channel, _ = setup(dated=False)
    draft_ready(channel)
    channel.tap(ALICE, "Confirm")
    assert "What date was this?" in channel.graph.last_body()
    assert saved(channel) == []
    today = lagos_today()
    channel.tap(ALICE, f"Today · {today.day} {_MONTHS[today.month - 1]}")
    [record] = saved(channel)
    assert record["transaction_date"] == today
    assert "Saved as *CR-000001*" in channel.graph.last_body()


@pytest.mark.parametrize(("answer", "typed"), [("No date", False), ("skip", True), ("confirm", True)])
def test_a_receipt_can_still_be_saved_without_a_date(setup: Any, answer: str, typed: bool) -> None:
    channel, _ = setup(dated=False)
    draft_ready(channel)
    channel.tap(ALICE, "Confirm")
    if typed:
        channel.say(ALICE, answer)
    else:
        channel.tap(ALICE, answer)
    [record] = saved(channel)
    assert record["transaction_date"] is None


def test_a_typed_date_answers_the_date_question(setup: Any) -> None:
    from app.whatsapp.corrections import read_date

    channel, _ = setup(dated=False)
    draft_ready(channel)
    channel.tap(ALICE, "Confirm")
    channel.say(ALICE, "10 Nov")
    [record] = saved(channel)
    assert record["transaction_date"].isoformat() == read_date("10 Nov", lagos_today())


def test_a_date_worked_out_from_today_in_the_chat_shows_where_it_came_from(setup: Any) -> None:
    from app.whatsapp.summary import readable_date

    channel, _ = setup(bag_conversation_output(date_label="today"), dated=False)
    draft_ready(channel)
    today = lagos_today()
    assert f"*Date:* {readable_date(today.isoformat())} _(“Today” in the chat)_" in channel.graph.last_body()
    channel.tap(ALICE, "Confirm")
    [record] = saved(channel)
    assert record["transaction_date"] == today


def test_photos_sent_together_get_no_unsupported_reply(setup: Any) -> None:
    channel, _ = setup()
    channel.say(ALICE, "hi")
    album = channel._message(ALICE, "unsupported")  # what WhatsApp delivers ahead of an album's photos
    assert channel.deliver(album, channel.image_event(ALICE, "media-1"), channel.image_event(ALICE, "media-2")).status_code == 200
    channel.work()
    assert "I can only read screenshots" not in channel.graph.everything_said()
    assert "Here's what I found" in channel.graph.last_body()


def test_a_common_correction_needs_no_ai(setup: Any) -> None:
    channel, provider = setup(transfer_read_as_the_price(), corrections=[])
    draft_ready(channel)
    channel.say(ALICE, "The buyer paid 15k")
    assert provider.requests == []
    body = channel.graph.last_body()
    assert "Paid so far: ₦15,000.00" in body and "🟡 Partially paid" in body
