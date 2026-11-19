"""Read conversations supplied as text: WhatsApp chat exports and pasted "Name: message" lines.

Text with a recognisable structure is parsed here, deterministically, with no AI involved.
Unstructured text returns None; the pipeline then asks the AI provider to split it into
messages (see ``transcription.text_segmentation_request``).

WhatsApp exports write dates in the phone's locale, so 03/04/2025 can be 3 April or 4 March. The
whole export is checked: any first number above 12 proves day-first, any second number above 12
proves month-first. Otherwise the configured default is used and the record says the date was
ambiguous.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from app.intelligence.conversation import (
    Conversation,
    ConversationNote,
    Message,
    MessageKind,
    NoteSeverity,
    Participant,
    Side,
    SourceRef,
    normalise_text,
)

MAX_MESSAGES = 600
MAX_NAME_CHARS = 40

_TIME = r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[AaPp]\.?\s?[Mm]\.?)?"
_DATE = r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}"
_ANDROID = re.compile(rf"^(?P<date>{_DATE}),?\s+(?P<time>{_TIME})\s+[-–]\s+(?P<rest>.*)$")
_IOS = re.compile(rf"^\[(?P<date>{_DATE}),?\s+(?P<time>{_TIME})\]\s+(?P<rest>.*)$")
# Messages copied from WhatsApp (select, copy): "[10:38, 18/11/2026] Tola: text"
_COPIED = re.compile(rf"^\[(?P<time>{_TIME}),\s+(?P<date>{_DATE})\]\s+(?P<rest>.*)$")
_SENDER = re.compile(r"^(?P<name>[^:\n]{1,60}?):\s(?P<text>.*)$", re.DOTALL)
# "Ada: text", "[10:02] Ada: text", "10:02 - Ada: text"
_PASTED = re.compile(rf"^(?:\[?(?P<time>{_TIME})\]?\s*[-–]?\s*)?(?P<name>[^:\n\[\]]{{1,40}}?):\s+(?P<text>\S.*)$")

_MEDIA = re.compile(
    r"^(?:<media omitted>|<attached: [^>]+>|(?:image|video|audio|sticker|gif|document|contact card) omitted"
    r"|.+\.(?:jpg|jpeg|png|webp|opus|mp4|pdf) \(file attached\))$",
    re.IGNORECASE,
)
_DELETED = re.compile(r"^(?:this message was deleted|you deleted this message)\.?$", re.IGNORECASE)
_SYSTEM_PHRASES = (
    "messages and calls are end-to-end encrypted",
    "created group",
    "changed the subject",
    "changed this group's",
    "joined using this group's invite link",
    "security code with",
    "missed voice call",
    "missed video call",
    "is a contact",
    "disappearing messages",
)


@dataclass
class _RawMessage:
    line: int
    name: str | None  # None for system lines
    text: str
    date_parts: tuple[str, str, str] | None = None
    time: str = ""


def parse_text_conversation(text: str, *, day_first_default: bool = True) -> Conversation | None:
    """Parse a WhatsApp export or "Name: message" lines. None when the text has no such structure."""
    lines = _clean_lines(text)
    if not lines:
        return None
    for pattern in (_IOS, _COPIED, _ANDROID):
        raw = _parse_export(lines, pattern)
        if raw is not None:
            return _build(raw, app="whatsapp", day_first_default=day_first_default)
    raw = _parse_pasted(lines)
    if raw is not None:
        return _build(raw, app="unknown", day_first_default=day_first_default)
    return None


def _clean_lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ").replace(" ", " ")
    text = re.sub("[​-‏‪-‮⁠-⁤﻿]", "", text)
    return text.split("\n")


def _parse_export(lines: list[str], pattern: re.Pattern[str]) -> list[_RawMessage] | None:
    messages: list[_RawMessage] = []
    matched = 0
    for number, line in enumerate(lines, start=1):
        match = pattern.match(line.strip())
        if match is None:
            if messages and line.strip():
                messages[-1].text += "\n" + line  # a multi-line message continues
            continue
        matched += 1
        date_parts = tuple(re.split(r"[./-]", match["date"]))
        rest = match["rest"]
        sender = _SENDER.match(rest)
        name = sender["name"].strip() if sender else None
        body = sender["text"] if sender else rest
        if name is not None and (len(name) > MAX_NAME_CHARS or '"' in name or _is_system_text(rest)):
            name, body = None, rest
        messages.append(_RawMessage(number, name, body, date_parts=date_parts, time=match["time"]))  # type: ignore[arg-type]
    non_empty = sum(1 for line in lines if line.strip())
    # Most lines must start like an export line; multi-line messages account for the rest.
    if matched < 2 or matched < non_empty * 0.3:
        return None
    return messages


def _parse_pasted(lines: list[str]) -> list[_RawMessage] | None:
    messages: list[_RawMessage] = []
    matched = 0
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        match = _PASTED.match(stripped)
        if match and not match["name"].strip().isdigit():
            matched += 1
            messages.append(_RawMessage(number, match["name"].strip(), match["text"], time=match["time"] or ""))
        elif messages:
            messages[-1].text += "\n" + stripped
        else:
            return None  # text before the first "Name:" line means this isn't a labelled chat
    names = {message.name for message in messages}
    non_empty = sum(1 for line in lines if line.strip())
    if matched < 2 or matched < non_empty * 0.6 or len(names) > 8:
        return None
    return messages


def _is_system_text(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _SYSTEM_PHRASES)


def _kind(text: str) -> MessageKind:
    stripped = text.strip()
    if _DELETED.match(stripped):
        return MessageKind.DELETED
    if _MEDIA.match(stripped):
        return MessageKind.MEDIA
    return MessageKind.TEXT


def _day_first(parts: Iterable[tuple[str, str, str]], default: bool) -> tuple[bool, bool]:
    """(day_first, proven) for d/m/y dates; year-first dates are unambiguous and ignored."""
    firsts, seconds = [], []
    for a, b, _ in parts:
        if len(a) == 4:
            continue
        firsts.append(int(a))
        seconds.append(int(b))
    if any(value > 12 for value in firsts):
        return True, True
    if any(value > 12 for value in seconds):
        return False, True
    return default, not firsts


def _to_date(parts: tuple[str, str, str], day_first: bool) -> date | None:
    a, b, c = parts
    try:
        if len(a) == 4:
            return date(int(a), int(b), int(c))
        year = int(c) + (2000 if len(c) == 2 else 0)
        day, month = (int(a), int(b)) if day_first else (int(b), int(a))
        return date(year, month, day)
    except ValueError:
        return None


def parse_clock(text: str) -> int | None:
    """Minutes after midnight for "10:38", "10:38 PM", "22.38"; None when unreadable."""
    match = re.match(r"^\s*(\d{1,2})[:.](\d{2})(?:[:.]\d{2})?\s*([AaPp])?\.?\s*(?:[Mm]\.?)?\s*$", text or "")
    if match is None:
        return None
    hour, minute, half = int(match[1]), int(match[2]), (match[3] or "").lower()
    if minute > 59 or hour > 23 or (half and not 1 <= hour <= 12):
        return None
    if half == "p" and hour != 12:
        hour += 12
    elif half == "a" and hour == 12:
        hour = 0
    return hour * 60 + minute


def _build(raw: list[_RawMessage], *, app: str, day_first_default: bool) -> Conversation:
    notes: list[ConversationNote] = []
    if len(raw) > MAX_MESSAGES:
        dropped = len(raw) - MAX_MESSAGES
        raw = raw[-MAX_MESSAGES:]
        notes.append(
            ConversationNote(
                code="truncated",
                severity=NoteSeverity.REVIEW,
                message=f"This conversation is long, so only its last {MAX_MESSAGES} messages were read "
                f"({dropped} earlier messages were left out). Check nothing earlier changes the agreement.",
            )
        )
    dated = [message.date_parts for message in raw if message.date_parts]
    day_first, proven = _day_first(dated, day_first_default)
    if dated and not proven:
        order = "day/month" if day_first else "month/day"
        notes.append(
            ConversationNote(
                code="date_format_ambiguous",
                severity=NoteSeverity.REVIEW,
                message=f"The dates in this export could be day/month or month/day. They were read as "
                f"{order}; check the transaction date.",
            )
        )

    participants: dict[str, Participant] = {}
    messages: list[Message] = []
    for message in raw:
        key = None
        if message.name is not None:
            folded = message.name.casefold()
            if folded not in participants:
                participants[folded] = Participant(key=f"p{len(participants) + 1}", name=message.name, side=Side.UNKNOWN)
            key = participants[folded].key
        when = _to_date(message.date_parts, day_first) if message.date_parts else None
        minutes = parse_clock(message.time) if message.time else None
        sent_at = ""
        if when is not None and minutes is not None:
            sent_at = datetime(when.year, when.month, when.day, minutes // 60, minutes % 60).isoformat()
        text = normalise_text(message.text)
        kind = _kind(text) if key else MessageKind.SYSTEM
        seq = len(messages) + 1
        messages.append(
            Message(
                key=f"m{seq}",
                seq=seq,
                participant=key,
                kind=kind,
                text=text,
                original_text=message.text,
                time=message.time.strip(),
                day=when.isoformat() if when else "",
                sent_at=sent_at,
                source=SourceRef(kind="text", position=0, index=message.line),
            )
        )
    return Conversation(
        source_type="text",
        app=app,
        is_group=len(participants) > 2,
        participants=list(participants.values()),
        messages=messages,
        notes=notes,
    )
