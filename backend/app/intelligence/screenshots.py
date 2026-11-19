"""Merge per-screenshot transcripts into one conversation.

People often screenshot a long chat in several overlapping pieces, sometimes out of order.
Rules applied here, deterministically:

- Screenshots stay in the order the person uploaded them. When times or day labels suggest a
  different order, the conversation keeps the upload order and gets a review note; it is never
  silently re-sorted.
- Repeated messages are removed only where consecutive screenshots overlap: the end of one
  screenshot matching the start of the next, in sequence, on the same side. A message repeated
  anywhere else ("ok", "ok") is real and stays.
- A one-message overlap only counts when the message is distinctive (long enough, or with the
  same time on both), so two unrelated "Okay" bubbles aren't merged.
- Screenshots that don't overlap get an informational note: messages between them may be missing.
"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.intelligence.conversation import (
    Conversation,
    ConversationNote,
    Legibility,
    Message,
    MessageKind,
    NoteSeverity,
    Participant,
    Side,
    SourceRef,
    comparable,
    normalise_text,
)
from app.intelligence.text_import import parse_clock

MIN_DISTINCT_CHARS = 12
SIMILARITY = 0.9
_RELATIVE_DAYS = {"yesterday": 0, "today": 1}


class TranscribedMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    side: Literal["incoming", "outgoing", "system"]
    sender: str = ""
    time: str = ""
    day: str = ""
    kind: MessageKind = MessageKind.TEXT
    text: str = ""
    quoted: str = ""
    legibility: Legibility = Legibility.CLEAR


class ScreenshotTranscript(BaseModel):
    model_config = ConfigDict(extra="ignore")

    screenshot: int = Field(ge=1)
    app: str = "unknown"
    header_name: str = ""
    is_group: bool = False
    cut_off_top: bool = False
    cut_off_bottom: bool = False
    messages: list[TranscribedMessage] = Field(default_factory=list, max_length=400)


def _same(a: TranscribedMessage, b: TranscribedMessage, *, edge: bool) -> bool:
    if a.side != b.side or a.kind != b.kind:
        return False
    if a.time and b.time and parse_clock(a.time) != parse_clock(b.time):
        return False
    ta, tb = comparable(a.text), comparable(b.text)
    if ta == tb:
        return True
    if not ta or not tb:
        return False
    # A bubble cut off by the screenshot edge reappears whole in the next screenshot.
    if edge and min(len(ta), len(tb)) >= 8 and (ta.startswith(tb) or tb.startswith(ta)):
        return True
    if min(len(ta), len(tb)) < MIN_DISTINCT_CHARS:
        return False
    return SequenceMatcher(None, ta, tb).ratio() >= SIMILARITY


def _distinctive(a: TranscribedMessage, b: TranscribedMessage) -> bool:
    return len(comparable(a.text)) >= MIN_DISTINCT_CHARS or bool(a.time and b.time)


def overlap_length(previous: list[TranscribedMessage], following: list[TranscribedMessage]) -> int:
    """How many messages at the start of ``following`` repeat the end of ``previous``."""
    for size in range(min(len(previous), len(following)), 0, -1):
        tail, head = previous[-size:], following[:size]
        if all(_same(x, y, edge=index in (0, size - 1)) for index, (x, y) in enumerate(zip(tail, head))):
            if size == 1 and not _distinctive(tail[0], head[0]):
                continue
            return size
    return 0


def _day_rank(label: str) -> int | None:
    return _RELATIVE_DAYS.get(label.strip().casefold())


def _looks_out_of_order(previous: list[TranscribedMessage], following: list[TranscribedMessage]) -> bool:
    prev_days = [m.day for m in previous if m.day]
    next_days = [m.day for m in following if m.day]
    if prev_days and next_days:
        a, b = _day_rank(prev_days[-1]), _day_rank(next_days[0])
        if a is not None and b is not None and b < a:
            return True
        if comparable(prev_days[-1]) != comparable(next_days[0]):
            return False  # different days we can't compare: no evidence either way
    prev_times = [t for t in (parse_clock(m.time) for m in previous if m.time) if t is not None]
    next_times = [t for t in (parse_clock(m.time) for m in following if m.time) if t is not None]
    return bool(prev_times and next_times and next_times[0] < prev_times[-1])


def merge_screenshots(transcripts: list[ScreenshotTranscript]) -> Conversation:
    transcripts = sorted(transcripts, key=lambda t: t.screenshot)
    notes: list[ConversationNote] = []

    headers = [t.header_name.strip() for t in transcripts if t.header_name.strip()]
    if len({comparable(header) for header in headers}) > 1:
        names = ", ".join(dict.fromkeys(headers))
        notes.append(
            ConversationNote(
                code="different_chats",
                severity=NoteSeverity.REVIEW,
                message=f"The screenshots show different chat names ({names}). Check they come from the same conversation.",
                screenshots=tuple(t.screenshot for t in transcripts if t.header_name.strip()),
            )
        )
    contact_name = headers[0] if headers else ""
    is_group = any(t.is_group for t in transcripts)
    apps = Counter(t.app for t in transcripts if t.app and t.app != "unknown")
    app = apps.most_common(1)[0][0] if apps else "unknown"

    kept: list[tuple[int, int, TranscribedMessage]] = []  # (screenshot, index in screenshot, message)
    previous: list[TranscribedMessage] = []
    for position, transcript in enumerate(transcripts):
        shot = transcript.screenshot
        content = [(index, m) for index, m in enumerate(transcript.messages, start=1) if m.side != "system"]
        if not transcript.messages:
            notes.append(
                ConversationNote(
                    code="no_messages",
                    severity=NoteSeverity.REVIEW,
                    message=f"No messages could be read in screenshot {shot}.",
                    screenshots=(shot,),
                )
            )
        skip_until = 0  # index (within the screenshot) of the last message repeated from the previous one
        if position and previous and content:
            size = overlap_length(previous, [m for _, m in content])
            if size:
                skip_until = content[size - 1][0]
                # Keep the fuller copy of a message the previous screenshot cut off.
                last_kept = kept[-1][2] if kept else None
                fuller = content[size - 1][1]
                if last_kept is not None and len(fuller.text) > len(last_kept.text) and _same(last_kept, fuller, edge=True):
                    kept[-1] = (kept[-1][0], kept[-1][1], last_kept.model_copy(update={"text": fuller.text}))
            else:
                prior = transcripts[position - 1].screenshot
                if _looks_out_of_order(previous, [m for _, m in content]):
                    notes.append(
                        ConversationNote(
                            code="order_uncertain",
                            severity=NoteSeverity.REVIEW,
                            message=f"Screenshot {shot} seems to come before screenshot {prior} in the conversation. "
                            "They were read in the order uploaded; check the order.",
                            screenshots=(prior, shot),
                        )
                    )
                else:
                    notes.append(
                        ConversationNote(
                            code="no_overlap",
                            severity=NoteSeverity.INFO,
                            message=f"Screenshots {prior} and {shot} don't overlap, so messages between them may be missing.",
                            screenshots=(prior, shot),
                        )
                    )
        for index, message in enumerate(transcript.messages, start=1):
            if index <= skip_until:
                continue
            kept.append((shot, index, message))
        if content:
            previous = [m for _, m in content]

    participants: dict[str, Participant] = {}

    def participant_for(message: TranscribedMessage) -> str | None:
        if message.side == "system":
            return None
        if message.side == "outgoing":
            lookup, name, side = "\x00owner", "", Side.OWNER
        elif is_group:
            name = message.sender.strip()
            lookup, side = (comparable(name) or "\x00member"), Side.CONTACT
        else:
            lookup, name, side = "\x00contact", contact_name, Side.CONTACT
        if lookup not in participants:
            participants[lookup] = Participant(key=f"p{len(participants) + 1}", name=name, side=side)
        return participants[lookup].key

    messages: list[Message] = []
    unreadable: list[str] = []
    day = ""
    for shot, index, item in kept:
        day = item.day.strip() or day
        seq = len(messages) + 1
        key = f"m{seq}"
        kind = MessageKind.SYSTEM if item.side == "system" else item.kind
        if item.legibility != Legibility.CLEAR:
            unreadable.append(key)
        messages.append(
            Message(
                key=key,
                seq=seq,
                participant=participant_for(item),
                kind=kind,
                text=normalise_text(item.text),
                original_text=item.text,
                time=item.time.strip(),
                day=day,
                quoted=normalise_text(item.quoted),
                legibility=item.legibility,
                source=SourceRef(kind="screenshot", position=shot, index=index),
            )
        )
    if unreadable:
        notes.append(
            ConversationNote(
                code="partly_unreadable",
                severity=NoteSeverity.REVIEW,
                message="Some messages couldn't be read fully. Check the details that come from them.",
                messages=tuple(unreadable),
            )
        )
    return Conversation(
        source_type="screenshots",
        app=app,
        is_group=is_group,
        participants=list(participants.values()),
        messages=messages,
        notes=notes,
    )
