"""WhatsApp chat exports, read into a conversation before any AI sees them.

WhatsApp's "Export chat" gives a .txt file ("WhatsApp Chat with Tola's Bags.txt" on Android,
"_chat.txt" on iOS), or a .zip with that file and the photos, voice notes and documents sent in the
chat ("WhatsApp Chat - Tola's Bags.zip" on iOS). Both are read here, deterministically:

- A ZIP is read in memory and nothing is written to disk, so its paths never reach the file system.
  Archives with unsafe paths, encryption or a suspicious compression ratio are refused, and every
  read is capped before it is decompressed.
- The text is parsed by ``app.intelligence.text_import`` (Android and iOS formats, multi-line
  messages, system lines, media references, the last 600 messages of a long chat) into a
  ``Conversation`` with stable message keys the AI cites as evidence.
- The phone owner is worked out from the file name, which names the other person in a one-to-one
  chat, or from the customer's own names. Otherwise their side stays unknown.
- Photos sent with the most recent messages (often payment receipts) are kept to send to the AI
  next to the chat.
"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from app.intelligence.conversation import Conversation, MessageKind, Participant, Side
from app.intelligence.text_import import parse_text_conversation
from app.names import phone_in_label, same_name
from app.uploads import UploadRejected

ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")
MAX_ZIP_ENTRIES = 3000
# An entry that grows more than this many times when decompressed is treated as a ZIP bomb.
MAX_COMPRESSION_RATIO = 150
MAX_ATTACHMENTS = 4
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

TOO_LONG = "This chat is too long to read. Export a shorter chat, or send screenshots of the part that matters."
BAD_ZIP = "This ZIP file can't be opened. Export the chat again and send the new file."
NOT_A_CHAT = (
    "This doesn't look like a WhatsApp chat export. In WhatsApp, open the chat, tap ⋮ or the name, "
    "choose Export chat, and send the file it makes."
)

_CHAT_NAMES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^WhatsApp Chat with (?P<name>.+)$",  # Android, English
        r"^WhatsApp Chat - (?P<name>.+)$",  # iOS ZIP, English
        r"^Chat de WhatsApp con (?P<name>.+)$",
        r"^Conversa do WhatsApp com (?P<name>.+)$",
        r"^WhatsApp-Chat mit (?P<name>.+)$",
        r"^Discussion WhatsApp avec (?P<name>.+)$",
    )
)
_ATTACHED = re.compile(r"<attached:\s*(?P<a>[^>]+?)\s*>|(?P<b>[^\s<>]+?\.\w{2,5})\s+\(file attached\)", re.IGNORECASE)


@dataclass(frozen=True)
class Attachment:
    name: str
    message_key: str
    data: bytes


@dataclass(frozen=True)
class Owner:
    """The participant who exported the chat, if known, and how that was worked out."""

    key: str | None
    source: str  # "file_name", "your_name" or "" when unknown


@dataclass(frozen=True)
class ChatExport:
    conversation: Conversation
    owner: Owner
    chat_name: str  # the other person or group, from the file name; "" when the name doesn't say
    attachments: list[Attachment]
    media_in_archive: int
    fingerprint: str  # SHA-256 of the chat text, to recognise the same export sent again

    @property
    def contact(self) -> Participant | None:
        """The other person in a one-to-one chat, once the owner is known."""
        if self.owner.key is None or self.conversation.is_group:
            return None
        return next((person for person in self.conversation.participants if person.key != self.owner.key), None)


def is_zip(data: bytes) -> bool:
    return data.startswith(ZIP_SIGNATURES)


def chat_name_from(filename: str) -> str:
    """The other person's name, or the group's, from an export's file name."""
    base = re.split(r"[\\/]", filename or "")[-1]
    stem = re.sub(r"\.(?:txt|zip)$", "", base, flags=re.IGNORECASE)
    stem = re.sub(r"\s*\(\d+\)$", "", stem).strip()  # "(1)" added when a file is downloaded twice
    for pattern in _CHAT_NAMES:
        found = pattern.match(stem)
        if found:
            return found["name"].strip()[:120]
    return ""


def read_chat_export(
    data: bytes,
    filename: str,
    *,
    max_text_bytes: int,
    max_file_bytes: int,
    owner_names: Sequence[str] = (),
    day_first_default: bool = True,
) -> ChatExport:
    """A .txt export or a .zip export read into a conversation. Raises UploadRejected when it can't be."""
    if not data:
        raise UploadRejected("empty_file", "This file is empty.")
    if not is_zip(data):
        if len(data) > max_text_bytes:
            raise UploadRejected("text_too_large", TOO_LONG, status_code=413)
        return _from_text(_decode(data), [filename], owner_names=owner_names, day_first_default=day_first_default)
    archive = _open_zip(data)
    with archive:
        chat, images, media = _scan(archive)
        text = _decode(_read_entry(archive, chat, max_text_bytes, too_large=TOO_LONG))
        names = [filename, PurePosixPath(chat.filename.replace("\\", "/")).name]
        export = _from_text(text, names, owner_names=owner_names, day_first_default=day_first_default)
        attachments = _attachments(archive, export.conversation, images, max_file_bytes)
    return replace(export, attachments=attachments, media_in_archive=media)


def _from_text(text: str, names: Sequence[str], *, owner_names: Sequence[str], day_first_default: bool) -> ChatExport:
    conversation = parse_text_conversation(text, day_first_default=day_first_default)
    if conversation is None or not conversation.participants:
        raise UploadRejected("not_a_chat_export", NOT_A_CHAT, status_code=422)
    chat_name = next((name for name in map(chat_name_from, names) if name), "")
    conversation, owner = resolve_owner(conversation, chat_name=chat_name, owner_names=owner_names)
    fingerprint = hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()
    return ChatExport(conversation, owner, chat_name, [], 0, fingerprint)


def resolve_owner(conversation: Conversation, *, chat_name: str, owner_names: Sequence[str]) -> tuple[Conversation, Owner]:
    """Mark the phone owner's side, from the file name or from the customer's own names.

    In a one-to-one chat the file name is the other person's name as the exporting phone shows it,
    so the remaining participant is the owner. Otherwise a participant whose name matches one of the
    customer's own names is. A name alone never decides between several matches.
    """
    people = conversation.participants
    owner_key, source = None, ""
    if chat_name and len(people) == 2:
        named = [person for person in people if same_name(person.name, chat_name)]
        if len(named) == 1:
            owner_key, source = next(person.key for person in people if person is not named[0]), "file_name"
    if owner_key is None:
        mine = [person for person in people if any(name and same_name(person.name, name) for name in owner_names)]
        if len(mine) == 1:
            owner_key, source = mine[0].key, "your_name"
    if owner_key is None:
        return conversation, Owner(None, "")
    sided = [person.model_copy(update={"side": Side.OWNER if person.key == owner_key else Side.CONTACT}) for person in people]
    rebuilt = Conversation(
        source_type=conversation.source_type,
        app=conversation.app,
        is_group=conversation.is_group,
        participants=sided,
        messages=conversation.messages,
        notes=conversation.notes,
    )
    return rebuilt, Owner(owner_key, source)


def transcript(export: ChatExport) -> str:
    """The chat as numbered lines for the AI, with who is who stated plainly."""
    conversation = export.conversation
    kind = "group chat" if conversation.is_group else "chat"
    lines = [f"A WhatsApp {kind}, exported as text.", "", "Participants:"]
    for person in conversation.participants:
        if person.side == Side.OWNER:
            role = "the phone's owner, who exported this chat"
        elif person.side == Side.CONTACT:
            role = "the other person" if not conversation.is_group else "another member"
        else:
            role = "not known whether this is the phone's owner"
        lines.append(f'- {person.key}: "{person.name}" ({role})')
    lines += ["", "Messages:"]
    day = ""
    for message in conversation.messages:
        if message.day and message.day != day:
            day = message.day
            lines.append(f"--- {day} ---")
        speaker = message.participant or "system"
        when = f" · {message.time}" if message.time else ""
        lines.append(f"[{message.key}] {speaker}{when} · {message.text}")
    return "\n".join(lines)


# -- inside a ZIP ---------------------------------------------------------------------------------


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError) as exc:
        raise UploadRejected("bad_zip", BAD_ZIP) from exc


def _scan(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, dict[str, zipfile.ZipInfo], int]:
    """The chat's text entry, the images by file name, and how many media files the export holds."""
    entries = archive.infolist()
    if len(entries) > MAX_ZIP_ENTRIES:
        raise UploadRejected("zip_too_many_files", "This ZIP file holds too many files to be a chat export.")
    texts: list[zipfile.ZipInfo] = []
    images: dict[str, zipfile.ZipInfo] = {}
    media = 0
    for entry in entries:
        if entry.is_dir():
            continue
        if not _safe_path(entry.filename):
            raise UploadRejected("zip_unsafe_path", "This ZIP file contains files with unsafe names, so it wasn't opened.")
        if entry.flag_bits & 0x1:
            raise UploadRejected("zip_encrypted", "This ZIP file is password-protected. Export the chat again without a password.")
        if stat.S_ISLNK(entry.external_attr >> 16):
            continue  # a link, not a file: never followed
        if entry.file_size > 1024 * 1024 and entry.file_size > MAX_COMPRESSION_RATIO * max(entry.compress_size, 1):
            raise UploadRejected("zip_suspicious", "This ZIP file looks unsafe, so it wasn't opened.")
        path = PurePosixPath(entry.filename.replace("\\", "/"))
        suffix = path.suffix.lower()
        if suffix == ".txt":
            texts.append(entry)
            continue
        media += 1
        if suffix in IMAGE_SUFFIXES:
            images[path.name.casefold()] = entry
    chat = _choose_chat(texts)
    if chat is None:
        raise UploadRejected("not_a_chat_export", NOT_A_CHAT, status_code=422)
    return chat, images, media


def _attachments(archive: zipfile.ZipFile, conversation: Conversation, images: dict[str, zipfile.ZipInfo], max_file_bytes: int) -> list[Attachment]:
    """The photos the latest messages refer to, newest last, read through the capped reader."""
    if not images:
        return []
    found: list[Attachment] = []
    for message in reversed(conversation.messages):
        for match in _ATTACHED.finditer(message.original_text or message.text):
            name = PurePosixPath((match["a"] or match["b"] or "").strip()).name
            entry = images.get(name.casefold())
            if entry is None or any(item.name.casefold() == name.casefold() for item in found):
                continue
            try:
                data = _read_entry(archive, entry, max_file_bytes, too_large="")
            except UploadRejected:
                continue  # an unreadable or oversized photo is left out; the chat itself is still read
            found.append(Attachment(name, message.key, data))
        if len(found) >= MAX_ATTACHMENTS:
            break
    return list(reversed(found[:MAX_ATTACHMENTS]))


def _choose_chat(texts: list[zipfile.ZipInfo]) -> zipfile.ZipInfo | None:
    if not texts:
        return None

    def preference(entry: zipfile.ZipInfo) -> tuple[int, int]:
        name = PurePosixPath(entry.filename.replace("\\", "/")).name
        known = name.casefold() == "_chat.txt" or bool(chat_name_from(name))
        return (1 if known else 0, entry.file_size)

    return max(texts, key=preference)


def _read_entry(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, limit: int, *, too_large: str) -> bytes:
    if entry.file_size > limit:
        raise UploadRejected("text_too_large", too_large or "This file is too large.", status_code=413)
    try:
        with archive.open(entry) as handle:
            data = handle.read(limit + 1)
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, NotImplementedError) as exc:
        raise UploadRejected("bad_zip", BAD_ZIP) from exc
    if len(data) > limit:
        raise UploadRejected("text_too_large", too_large or "This file is too large.", status_code=413)
    return data


def _safe_path(name: str) -> bool:
    """No absolute paths, drive letters, parent directories or control characters."""
    if not name or len(name) > 400 or any(ord(character) < 32 for character in name):
        return False
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return False
    return ".." not in re.split(r"[\\/]", name)


def _decode(data: bytes) -> str:
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadRejected("not_a_chat_export", NOT_A_CHAT, status_code=422) from exc
    if "\x00" in text or not text.strip():
        raise UploadRejected("not_a_chat_export", NOT_A_CHAT, status_code=422)
    return text
