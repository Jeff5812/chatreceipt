"""Reading conversations: text exports, pasted chats, screenshot merging and figure grounding."""

from __future__ import annotations

from decimal import Decimal

from app.intelligence.conversation import MessageKind, Side
from app.intelligence.money import amounts_in, mentions_amount, mentions_percent, mentions_quantity
from app.intelligence.screenshots import ScreenshotTranscript, TranscribedMessage, merge_screenshots, overlap_length
from app.intelligence.text_import import parse_clock, parse_text_conversation


def test_android_export_with_multiline_messages_and_system_lines() -> None:
    text = (
        "12/03/2025, 10:30 - Messages and calls are end-to-end encrypted. No one outside of this chat can read them.\n"
        "12/03/2025, 10:38 - Tola Bags: Good afternoon!\n"
        "The brown tote is ₦25,000.\n"
        "12/03/2025, 10:39 - Ada: Can you do 22k?\n"
        "12/03/2025, 10:40 - Ada: <Media omitted>\n"
        "13/03/2025, 09:02 - Tola Bags: This message was deleted"
    )
    convo = parse_text_conversation(text)
    assert convo is not None and convo.app == "whatsapp"
    assert [p.name for p in convo.participants] == ["Tola Bags", "Ada"]
    assert all(p.side == Side.UNKNOWN for p in convo.participants)
    kinds = [m.kind for m in convo.messages]
    assert kinds == [MessageKind.SYSTEM, MessageKind.TEXT, MessageKind.TEXT, MessageKind.MEDIA, MessageKind.DELETED]
    assert convo.messages[1].text == "Good afternoon!\nThe brown tote is ₦25,000."
    assert convo.messages[1].sent_at == "2025-03-12T10:38:00"
    assert convo.messages[4].day == "2025-03-13"  # 13 proves the dates are day-first
    assert not convo.notes


def test_ios_export_with_invisible_marks_and_seconds() -> None:
    text = (
        "‎[3/14/25, 9:15:02 PM] Lena Park: Can you build our landing page for $450?\n"
        "[3/14/25, 9:20:11 PM] Sam: Yes. I'll need a $150 deposit."
    )
    convo = parse_text_conversation(text)
    assert convo is not None
    assert convo.messages[0].sent_at == "2025-03-14T21:15:00"  # 14 in second place proves month-first
    assert convo.messages[1].participant == "p2"


def test_ambiguous_export_dates_are_read_with_the_default_and_noted() -> None:
    text = "03/04/2025, 10:00 - A: Hello\n03/04/2025, 10:01 - B: Hi"
    day_first = parse_text_conversation(text)
    month_first = parse_text_conversation(text, day_first_default=False)
    assert day_first.messages[0].day == "2025-04-03"  # type: ignore[union-attr]
    assert month_first.messages[0].day == "2025-03-04"  # type: ignore[union-attr]
    assert [note.code for note in day_first.notes] == ["date_format_ambiguous"]  # type: ignore[union-attr]


def test_copied_messages_and_pasted_name_lines() -> None:
    copied = "[10:38, 18/11/2026] Tola: Bag is 25k\n[10:40, 18/11/2026] Ada: Ok"
    convo = parse_text_conversation(copied)
    assert convo is not None and [m.text for m in convo.messages] == ["Bag is 25k", "Ok"]
    pasted = "Seller: The bag is ₦18,000.\nCustomer: Can you do 15k?\nSeller: 17k final.\nCustomer: Okay, I will take it."
    convo = parse_text_conversation(pasted)
    assert convo is not None and convo.app == "unknown"
    assert [p.name for p in convo.participants] == ["Seller", "Customer"]


def test_unstructured_text_is_not_guessed() -> None:
    assert parse_text_conversation("hi\nhow much is the bag\n25k\nok") is None
    assert parse_text_conversation("") is None


def test_clock_parsing() -> None:
    assert parse_clock("10:38") == 638
    assert parse_clock("10:38 PM") == 22 * 60 + 38
    assert parse_clock("12:05 am") == 5
    assert parse_clock("25:00") is None and parse_clock("noon") is None


def shot(number: int, *messages: tuple[str, str, str], header: str = "Tola Bags", **flags: bool) -> ScreenshotTranscript:
    return ScreenshotTranscript(
        screenshot=number,
        app="whatsapp",
        header_name=header,
        messages=[TranscribedMessage(side=side, time=time, text=text) for side, time, text in messages],  # type: ignore[arg-type]
        **flags,
    )


def test_overlapping_screenshots_drop_only_the_repeated_messages() -> None:
    first = shot(1, ("incoming", "10:38", "The brown leather tote is ₦25,000."), ("outgoing", "10:39", "Can you do ₦22,000?"), ("incoming", "10:41", "₦23,000 is my final price."))
    second = shot(2, ("outgoing", "10:39", "Can you do ₦22,000?"), ("incoming", "10:41", "₦23,000 is my final price."), ("outgoing", "10:42", "Okay, I'll take it."))
    convo = merge_screenshots([first, second])
    assert [m.text for m in convo.messages] == [
        "The brown leather tote is ₦25,000.", "Can you do ₦22,000?", "₦23,000 is my final price.", "Okay, I'll take it.",
    ]
    assert convo.messages[3].source.position == 2 and convo.messages[3].source.index == 3
    assert [p.side for p in convo.participants] == [Side.CONTACT, Side.OWNER]
    assert convo.participants[0].name == "Tola Bags"
    assert not convo.notes


def test_repeated_short_messages_inside_a_screenshot_are_kept() -> None:
    convo = merge_screenshots([shot(1, ("outgoing", "", "ok"), ("outgoing", "", "ok"))])
    assert len(convo.messages) == 2


def test_a_single_short_message_is_not_proof_of_overlap() -> None:
    first = shot(1, ("incoming", "", "Price?"), ("outgoing", "", "Ok"))
    second = shot(2, ("outgoing", "", "Ok"), ("incoming", "", "Delivery tomorrow"))
    assert overlap_length(first.messages, second.messages) == 0
    convo = merge_screenshots([first, second])
    assert len(convo.messages) == 4
    assert [n.code for n in convo.notes] == ["no_overlap"]


def test_a_bubble_cut_off_at_the_edge_is_merged_with_its_full_copy() -> None:
    first = shot(1, ("incoming", "10:00", "Hello there, welcome"), ("outgoing", "10:02", "I want the blue sneakers in size"), cut_off_bottom=True)
    second = shot(2, ("outgoing", "10:02", "I want the blue sneakers in size 43 please"), ("incoming", "10:03", "₦30,000"))
    convo = merge_screenshots([first, second])
    assert [m.text for m in convo.messages] == ["Hello there, welcome", "I want the blue sneakers in size 43 please", "₦30,000"]


def test_screenshots_that_seem_out_of_order_keep_the_upload_order_and_are_flagged() -> None:
    later = shot(1, ("incoming", "11:40", "Payment received, thank you"))
    earlier = shot(2, ("incoming", "10:02", "The price is ₦15,000"), ("outgoing", "10:05", "Okay"))
    convo = merge_screenshots([later, earlier])
    assert convo.messages[0].text == "Payment received, thank you"
    assert [n.code for n in convo.notes] == ["order_uncertain"]


def test_screenshots_from_different_chats_are_flagged() -> None:
    convo = merge_screenshots([shot(1, ("incoming", "", "Hi")), shot(2, ("incoming", "", "Hello"), header="Gadget Hub")])
    assert "different_chats" in [n.code for n in convo.notes]


def test_group_chats_have_one_participant_per_sender() -> None:
    transcript = ScreenshotTranscript(
        screenshot=1,
        is_group=True,
        messages=[
            TranscribedMessage(side="incoming", sender="Kemi", text="Who wants the last bag?"),
            TranscribedMessage(side="incoming", sender="Bayo", text="Me, how much?"),
            TranscribedMessage(side="outgoing", text="₦15,000"),
        ],
    )
    convo = merge_screenshots([transcript])
    assert [(p.name, p.side) for p in convo.participants] == [("Kemi", Side.CONTACT), ("Bayo", Side.CONTACT), ("", Side.OWNER)]


def test_transcript_for_the_model_cites_message_keys_and_days() -> None:
    convo = merge_screenshots([shot(1, ("incoming", "10:38", "Bag is 25k"))])
    transcript = convo.transcript()
    assert "[m1] p1 · 10:38 · Bag is 25k" in transcript
    assert '- p1: "Tola Bags" (contact' in transcript


def test_amounts_in_text() -> None:
    assert amounts_in("₦25,000 or 23k, delivery 2.5k") >= {Decimal(25000), Decimal(23000), Decimal(2500)}
    assert Decimal("1200000") in amounts_in("1.2m")
    assert Decimal("12500.5") in amounts_in("N12,500.50")
    assert mentions_amount("Last price 23k", Decimal(23000))
    assert not mentions_amount("Last price 23k", Decimal(25000))
    assert not mentions_amount("₦23,000", Decimal(23))  # "k" shorthand never matches the bare number
    assert mentions_percent("10% off", Decimal(10))
    assert mentions_quantity("I need two bags", Decimal(2))
    assert mentions_quantity("make it 3", Decimal(3))


def test_text_the_ai_had_to_split_into_messages_needs_a_person_to_check_the_speakers() -> None:
    from app.intelligence.screenshots import ScreenshotTranscript, TranscribedMessage
    from app.intelligence.transcription import segmented_conversation

    transcript = ScreenshotTranscript(
        screenshot=1,
        messages=[TranscribedMessage(side="incoming", sender="Ngozi", text="how much is the iron"),
                  TranscribedMessage(side="incoming", sender="Home Store", text="N18,000")],
    )
    convo = segmented_conversation(transcript)
    assert [(note.code, note.severity.value) for note in convo.notes] == [("ai_segmented", "review")]
