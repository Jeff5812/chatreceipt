"""POST /api/extractions/chat: a WhatsApp export in, the same draft out, with who's who settled by the file."""

from __future__ import annotations

import io
import zipfile
from typing import Any

from fastapi.testclient import TestClient

from app.extraction.provider import ErrorKind, ExtractionProviderError, ImagePart
from app.main import create_app
from tests.samples import FakeProvider, FakeRenderer, bag_conversation_output, field, image_bytes, line_item, make_settings

CHAT = """18/11/2026, 10:38 - Tola's Bags: Good afternoon! The brown leather tote is ₦25,000.
18/11/2026, 10:39 - Ada Obi: Can you do ₦22,000?
18/11/2026, 10:41 - Tola's Bags: ₦23,000 is my final price. Delivery to Yaba is ₦2,000.
18/11/2026, 10:42 - Ada Obi: Okay, I'll take it. My sister Ngozi will send the money.
19/11/2026, 08:05 - Ada Obi: IMG-20261119-WA0003.jpg (file attached)
19/11/2026, 08:06 - Tola's Bags: Received ₦25,000 from Ngozi Okafor. Thank you"""


def reading(**overrides: Any) -> dict[str, Any]:
    """What the AI reports for CHAT: the owner (Ada) bought, her sister paid."""
    return bag_conversation_output(
        line_items=[line_item("Brown leather tote", "1", "23000", quote="₦23,000 is my final price.")],
        delivery_fee=field("2000", quote="Delivery to Yaba is ₦2,000."),
        amount_paid=field("25000", quote="Received ₦25,000 from Ngozi Okafor."),
        payment_status=field("paid", quote="Received ₦25,000"),
        seller=field(""),
        buyer=field(""),
        owner_role=field("buyer", quote="Okay, I'll take it."),
        other_party=field("Someone Else"),  # wrong on purpose: the export's file name settles it
        people=[
            {"name": "Tola's Bags", "role": "seller", "is_owner": False, "quote": "₦23,000 is my final price."},
            {"name": "Ada Obi", "role": "buyer", "is_owner": True, "quote": "Okay, I'll take it."},
            {"name": "Ngozi Okafor", "role": "payer", "is_owner": False, "quote": "Received ₦25,000 from Ngozi Okafor."},
        ],
        other_deals=[{"summary": "A black clutch for ₦12,000, 2 November", "last_message": "m1"}],
        **overrides,
    )


def client_with(provider: FakeProvider) -> TestClient:
    return TestClient(create_app(settings=make_settings(), provider=provider, renderer=FakeRenderer()))


def send(client: TestClient, data: bytes, filename: str = "WhatsApp Chat with Tola's Bags.txt", **form: str) -> Any:
    return client.post("/api/extractions/chat", files={"file": (filename, data, "text/plain")}, data=form)


def test_an_export_becomes_a_draft_with_the_other_person_taken_from_the_file() -> None:
    provider = FakeProvider(data=reading())
    response = send(client_with(provider), CHAT.encode())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "chat_export"
    # The owner bought, so the other person in the file name is the seller; the AI's other_party is replaced.
    assert body["parties"]["owner_role"] == "buyer" and body["parties"]["other_party"] == "Tola's Bags"
    assert body["draft"]["seller"] == "Tola's Bags" and body["draft"]["buyer"] == ""
    assert body["field_meta"]["seller"]["note"] == "The name at the top of the chat."
    sides = {person["name"]: person["side"] for person in body["chat"]["participants"]}
    assert sides == {"Tola's Bags": "contact", "Ada Obi": "owner"}
    assert body["chat"]["owner_source"] == "file_name" and body["chat"]["messages"] == 6
    # Totals are the backend's: 23,000 + 2,000 delivery, fully paid.
    assert body["calculation"]["total"] == "25000.00" and body["calculation"]["outstanding"] == "0.00"
    [request] = provider.requests
    text = "\n".join(part.text for part in request.parts if hasattr(part, "text"))
    assert "This is a WhatsApp chat export" in text
    assert '- p2: "Ada Obi" (the phone\'s owner, who exported this chat)' in text
    assert "[m3] p1 · 10:41 · ₦23,000 is my final price." in text


def test_someone_who_pays_for_the_buyer_is_kept_as_the_payer_not_the_buyer() -> None:
    body = send(client_with(FakeProvider(data=reading())), CHAT.encode()).json()
    roles = {person["name"]: person["role"] for person in body["parties"]["people"]}
    assert roles == {"Tola's Bags": "seller", "Ada Obi": "buyer", "Ngozi Okafor": "payer"}
    assert body["draft"]["buyer"] != "Ngozi Okafor"


def test_other_deals_in_the_same_chat_are_listed_not_merged() -> None:
    body = send(client_with(FakeProvider(data=reading())), CHAT.encode()).json()
    assert body["other_deals"] == [{"summary": "A black clutch for ₦12,000, 2 November", "last_message": "m1"}]
    assert [item["description"] for item in body["draft"]["line_items"]] == ["Brown leather tote"]


def test_asking_for_another_deal_tells_the_ai_which_one() -> None:
    provider = FakeProvider(data=reading())
    send(client_with(provider), CHAT.encode(), focus="A black clutch for ₦12,000, 2 November")
    text = "\n".join(part.text for part in provider.requests[0].parts if hasattr(part, "text"))
    assert "The person wants the record for this deal: A black clutch for ₦12,000, 2 November." in text


def test_a_zip_export_sends_the_photo_with_the_chat() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("WhatsApp Chat with Tola's Bags.txt", CHAT)
        archive.writestr("IMG-20261119-WA0003.jpg", image_bytes("JPEG"))
    provider = FakeProvider(data=reading())
    body = send(client_with(provider), buffer.getvalue(), "WhatsApp Chat with Tola's Bags.zip").json()
    assert body["chat"]["photos_read"] == 1 and body["images_sent"] == 1 and body["screenshots"] == 2
    parts = provider.requests[0].parts
    labels = [part.text for part in parts if hasattr(part, "text") and part.text.startswith("Source 2")]
    assert labels == ['Source 2: a photo (IMG-20261119-WA0003.jpg) sent with message [m5] by "Ada Obi"']
    assert sum(isinstance(part, ImagePart) for part in parts) == 1


def test_without_knowing_who_exported_the_chat_no_name_is_placed_by_role() -> None:
    body = send(client_with(FakeProvider(data=reading())), CHAT.encode(), "chat.txt").json()
    assert body["parties"]["other_party"] == "" and body["draft"]["seller"] == ""
    assert {person["side"] for person in body["chat"]["participants"]} == {"unknown"}


def test_unreadable_or_unsafe_exports_are_refused_with_a_reason() -> None:
    client = client_with(FakeProvider(data=reading()))
    assert send(client, b"hello there").json()["error"]["code"] == "not_a_chat_export"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.txt", CHAT)
    refused = send(client, buffer.getvalue(), "export.zip")
    assert (refused.status_code, refused.json()["error"]["code"]) == (400, "zip_unsafe_path")


def test_an_export_over_the_size_limit_is_refused_before_it_is_read() -> None:
    client = TestClient(create_app(settings=make_settings(max_chat_export_bytes=1024), provider=FakeProvider(data=reading()), renderer=FakeRenderer()))
    response = send(client, CHAT.encode() * 20)
    assert (response.status_code, response.json()["error"]["code"]) == (413, "file_too_large")


def test_a_provider_failure_says_so_in_terms_of_the_chat() -> None:
    response = send(client_with(FakeProvider(error=ExtractionProviderError(ErrorKind.UNAVAILABLE, "down"))), CHAT.encode())
    assert response.status_code == 502 and "reads chats" in response.json()["error"]["message"]
