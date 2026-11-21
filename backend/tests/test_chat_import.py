"""WhatsApp chat exports read deterministically: formats, owner resolution, attachments, and unsafe ZIPs."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.chat_import import MAX_ATTACHMENTS, chat_name_from, read_chat_export, transcript
from app.intelligence.conversation import MessageKind, Side
from app.names import phone_in_label, same_name
from app.uploads import UploadRejected
from tests.samples import image_bytes

LIMITS = {"max_text_bytes": 512 * 1024, "max_file_bytes": 10 * 1024 * 1024}

ANDROID = """18/11/2026, 10:30 - Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them.
18/11/2026, 10:38 - Tola's Bags: Good afternoon! The brown leather tote is ₦25,000.
18/11/2026, 10:39 - Ada Obi: Can you do ₦22,000?
18/11/2026, 10:41 - Tola's Bags: ₦23,000 is my final price.
Delivery to Yaba is ₦2,000 extra.
18/11/2026, 10:42 - Ada Obi: Okay, I'll take it. I'll transfer tomorrow morning.
19/11/2026, 08:05 - Ada Obi: IMG-20261119-WA0003.jpg (file attached)
19/11/2026, 08:05 - Ada Obi: Sent ₦25,000 now
19/11/2026, 08:20 - Tola's Bags: Received, thank you 🙏"""

IOS = """[18/11/2026, 09:12:04] Mama Nkechi Provisions: Good morning dear. Rice is ₦78,000 per bag, groundnut oil 5L is ₦9,800
[18/11/2026, 09:15:40] Chidi Okafor: I need 2 bags of rice and 3 kegs of oil
[18/11/2026, 09:16:02] Mama Nkechi Provisions: Total na ₦185,400. Pay half now, balance on delivery
[18/11/2026, 09:30:55] Chidi Okafor: \u200e<attached: 00000011-PHOTO-2026-11-18-09-30-55.jpg>
[18/11/2026, 09:31:10] Chidi Okafor: Paid ₦92,700. Abeg send am today"""

US = """11/18/26, 2:05 PM - +234 803 123 4567: How much for the iPhone 13 screen?
11/18/26, 2:07 PM - Chidi Phones: ₦45,000 with fixing
11/18/26, 2:10 PM - +234 803 123 4567: Ok I'm coming by 4pm"""

GROUP = """18/11/2026, 12:00 - Bisi: Who wants the ankara bundle? ₦15,000 each
18/11/2026, 12:02 - Ngozi Fabrics: I'll take 2
18/11/2026, 12:03 - Emeka: Put me down for 1
18/11/2026, 12:05 - Bisi: Noted. Ngozi 2, Emeka 1"""


def zipped(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def read(data: bytes | str, filename: str = "chat.txt", **options: object) -> object:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return read_chat_export(raw, filename, **LIMITS, **options)  # type: ignore[arg-type]


# -- formats ----------------------------------------------------------------------------------------


def test_an_android_export_is_read_with_its_sender_labels_and_multiline_messages() -> None:
    export = read(ANDROID, "WhatsApp Chat with Tola's Bags.txt")
    names = [person.name for person in export.conversation.participants]
    assert names == ["Tola's Bags", "Ada Obi"]
    final = next(message for message in export.conversation.messages if "final price" in message.text)
    assert final.text == "₦23,000 is my final price.\nDelivery to Yaba is ₦2,000 extra."  # one message, two lines
    assert final.sent_at == "2026-11-18T10:41:00"
    kinds = [message.kind for message in export.conversation.messages]
    assert kinds[0] == MessageKind.SYSTEM and MessageKind.MEDIA in kinds


def test_the_file_name_names_the_other_person_so_the_remaining_sender_is_the_owner() -> None:
    export = read(ANDROID, "WhatsApp Chat with Tola's Bags.txt")
    sides = {person.name: person.side for person in export.conversation.participants}
    assert sides == {"Tola's Bags": Side.CONTACT, "Ada Obi": Side.OWNER}
    assert (export.owner.source, export.contact.name) == ("file_name", "Tola's Bags")  # type: ignore[union-attr]
    lines = transcript(export)
    assert '"Ada Obi" (the phone\'s owner, who exported this chat)' in lines and '"Tola\'s Bags" (the other person)' in lines
    assert "[m3] p2 · 10:39 · Can you do ₦22,000?" in lines


def test_an_ios_zip_export_keeps_the_photos_sent_with_the_latest_messages() -> None:
    archive = zipped({
        "_chat.txt": IOS.encode("utf-8"),
        "00000011-PHOTO-2026-11-18-09-30-55.jpg": image_bytes("JPEG"),
        "00000002-AUDIO-2026-11-18-09-20-00.opus": b"voice note",
    })
    export = read(archive, "WhatsApp Chat - Mama Nkechi Provisions.zip")
    assert export.owner.source == "file_name" and export.contact.name == "Mama Nkechi Provisions"  # type: ignore[union-attr]
    [photo] = export.attachments
    assert photo.name == "00000011-PHOTO-2026-11-18-09-30-55.jpg" and photo.data.startswith(b"\xff\xd8")
    message = export.conversation.message(photo.message_key)
    assert message is not None and export.conversation.participant(message.participant).name == "Chidi Okafor"  # type: ignore[union-attr]
    assert export.media_in_archive == 2


def test_month_first_dates_and_twelve_hour_times_are_understood() -> None:
    export = read(US)
    first = export.conversation.messages[0]
    assert first.sent_at == "2026-11-18T14:05:00"


def test_a_sender_shown_only_as_a_phone_number_keeps_the_number() -> None:
    export = read(US, owner_names=["Chidi Phones"])
    sides = {person.name: person.side for person in export.conversation.participants}
    assert sides == {"+234 803 123 4567": Side.CONTACT, "Chidi Phones": Side.OWNER}
    assert export.owner.source == "your_name"
    assert phone_in_label("+234 803 123 4567") == "2348031234567"
    assert phone_in_label("Chidi Phones") == "" and phone_in_label("2023") == ""


def test_a_group_chat_is_marked_and_its_name_never_picks_the_owner() -> None:
    export = read(GROUP, "WhatsApp Chat with Lagos Ankara Group.txt")
    assert export.conversation.is_group
    assert export.owner.key is None  # the group's name isn't a person
    with_name = read(GROUP, "WhatsApp Chat with Lagos Ankara Group.txt", owner_names=["Bisi"])
    assert {person.name: person.side for person in with_name.conversation.participants}["Bisi"] == Side.OWNER


def test_without_a_file_name_or_a_matching_name_the_owner_stays_unknown() -> None:
    export = read(ANDROID, "chat.txt")
    assert export.owner.key is None and export.contact is None
    assert all(person.side == Side.UNKNOWN for person in export.conversation.participants)
    # Two participants matching the customer's names don't decide anything either.
    assert read(ANDROID, "chat.txt", owner_names=["Ada Obi", "Tola's Bags"]).owner.key is None


def test_a_long_chat_keeps_its_latest_messages_and_says_so() -> None:
    lines = [f"18/11/2026, 10:{minute % 60:02d} - {'Ada Obi' if minute % 2 else 'Tola'}: message {minute}" for minute in range(700)]
    export = read("\n".join(lines))
    assert len(export.conversation.messages) == 600
    assert export.conversation.messages[-1].text == "message 699"
    assert [note.code for note in export.conversation.notes] == ["truncated"]


def test_the_same_export_has_the_same_fingerprint_whatever_its_line_endings() -> None:
    assert read(ANDROID).fingerprint == read(ANDROID.replace("\n", "\r\n")).fingerprint
    assert read(ANDROID).fingerprint != read(IOS).fingerprint


@pytest.mark.parametrize(
    ("filename", "name"),
    [
        ("WhatsApp Chat with Tola's Bags.txt", "Tola's Bags"),
        ("WhatsApp Chat - Mama Nkechi Provisions.zip", "Mama Nkechi Provisions"),
        ("WhatsApp Chat with Chidi (1).zip", "Chidi"),
        ("_chat.txt", ""),
        ("notes.txt", ""),
    ],
)
def test_the_other_persons_name_comes_from_the_export_file_name(filename: str, name: str) -> None:
    assert chat_name_from(filename) == name


def test_names_are_compared_without_case_spacing_or_emoji() -> None:
    assert same_name("Tola's Bags 🛍️", "tolas bags")
    assert not same_name("Tola", "Tola's Bags") and not same_name("", "")


def test_only_the_latest_photos_are_kept() -> None:
    lines = [f"18/11/2026, 10:{index:02d} - Ada Obi: IMG-{index}.jpg (file attached)" for index in range(8)]
    lines.append("18/11/2026, 11:00 - Tola: Seen")
    files = {"chat.txt": "\n".join(lines).encode("utf-8"), **{f"IMG-{index}.jpg": image_bytes("JPEG") for index in range(8)}}
    export = read(zipped(files), "WhatsApp Chat with Tola.zip")
    assert [photo.name for photo in export.attachments] == [f"IMG-{index}.jpg" for index in range(8 - MAX_ATTACHMENTS, 8)]


# -- refused ----------------------------------------------------------------------------------------


def _encrypted(archive: bytes) -> bytes:
    """Mark every entry as encrypted, the way a password-protected ZIP is."""
    data = bytearray(archive)
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = data.find(signature)
        while start != -1:
            data[start + offset] |= 0x01
            start = data.find(signature, start + 4)
    return bytes(data)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "empty_file"),
        (b"just some notes, not a chat", "not_a_chat_export"),
        (zipped({"photo.jpg": b"not really"}), "not_a_chat_export"),
        (zipped({"../../etc/passwd.txt": ANDROID.encode()}), "zip_unsafe_path"),
        (zipped({"C:/Windows/chat.txt": ANDROID.encode()}), "zip_unsafe_path"),
        (_encrypted(zipped({"_chat.txt": ANDROID.encode()})), "zip_encrypted"),
        (zipped({"_chat.txt": ANDROID.encode(), "big.jpg": bytes(8 * 1024 * 1024)}), "zip_suspicious"),
        (b"PK\x03\x04 broken", "bad_zip"),
        (b"\xff\xfe\x00\xd8 not text", "not_a_chat_export"),
    ],
)
def test_unsafe_or_unreadable_files_are_refused_before_anything_is_read(data: bytes, code: str) -> None:
    with pytest.raises(UploadRejected) as refused:
        read(data, "WhatsApp Chat with Tola.zip")
    assert refused.value.code == code


def test_a_chat_longer_than_the_limit_is_refused() -> None:
    with pytest.raises(UploadRejected) as refused:
        read_chat_export(ANDROID.encode() * 50, "chat.txt", max_text_bytes=1024, max_file_bytes=1024)
    assert refused.value.code == "text_too_large" and refused.value.status_code == 413
