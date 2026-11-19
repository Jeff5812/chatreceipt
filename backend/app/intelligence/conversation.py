"""A conversation as ChatReceipt reads it: participants, ordered messages and notes.

Every message gets a stable key (m1, m2, ...) that the AI cites as evidence and that the record keeps,
so any extracted value can be traced to the message, screenshot and text it came from. The source
text is kept exactly as read (``original_text``) next to a normalised copy (``text``).
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr

MAX_MESSAGE_CHARS = 4000

# Invisible formatting characters: WhatsApp's iOS export starts lines with U+200E, for example.
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Side(str, Enum):
    OWNER = "owner"  # the phone owner: "You", the right-hand bubbles
    CONTACT = "contact"  # the other side: the chat-header name, the left-hand bubbles
    UNKNOWN = "unknown"  # text exports don't say who exported the chat


class MessageKind(str, Enum):
    TEXT = "text"
    MEDIA = "media"  # image, voice note, sticker, document (described in brackets)
    SYSTEM = "system"  # date headers, encryption notices, missed calls
    DELETED = "deleted"


class Legibility(str, Enum):
    CLEAR = "clear"
    PARTIAL = "partial"
    UNREADABLE = "unreadable"


class NoteSeverity(str, Enum):
    INFO = "info"  # worth knowing; doesn't stop approval
    REVIEW = "review"  # a person should check before relying on the record


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Participant(_Frozen):
    key: str
    name: str  # as shown; empty when the conversation doesn't show it
    side: Side

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        if self.side == Side.OWNER:
            return "You (phone owner)"
        return "Unnamed participant"


class SourceRef(_Frozen):
    kind: Literal["screenshot", "text"]
    position: int  # screenshot number, 1-based; 0 for text
    index: int  # message number within the screenshot, or line number in the text


class Message(_Frozen):
    key: str
    seq: int
    participant: str | None  # participant key; None for system messages
    kind: MessageKind
    text: str
    original_text: str
    time: str = ""  # as shown: "10:38", "10:38 PM"
    day: str = ""  # nearest day label above the message, as shown: "Today", "12 March 2025"
    sent_at: str = ""  # ISO 8601, only when the source gives a full date and time (text exports)
    quoted: str = ""  # a replied-to message shown inside the bubble
    legibility: Legibility = Legibility.CLEAR
    source: SourceRef
    flags: tuple[str, ...] = ()


class ConversationNote(_Frozen):
    code: str
    severity: NoteSeverity
    message: str
    screenshots: tuple[int, ...] = ()
    messages: tuple[str, ...] = ()


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["screenshots", "text"]
    app: str = "unknown"
    is_group: bool = False
    participants: list[Participant]
    messages: list[Message]
    notes: list[ConversationNote] = []

    _by_key: dict[str, Message] = PrivateAttr(default_factory=dict)
    _people: dict[str, Participant] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        self._by_key = {message.key: message for message in self.messages}
        self._people = {participant.key: participant for participant in self.participants}

    def message(self, key: str) -> Message | None:
        return self._by_key.get(key)

    def participant(self, key: str | None) -> Participant | None:
        return self._people.get(key) if key else None

    def order(self, key: str) -> int:
        """Position of a message in the conversation; unknown keys sort last."""
        message = self._by_key.get(key)
        return message.seq if message else len(self.messages) + 1

    def transcript(self) -> str:
        """The conversation as numbered lines for the interpretation model."""
        lines = [_describe_source(self), "", "Participants:"]
        for participant in self.participants:
            lines.append(f"- {participant.key}: {_quote(participant.label)} ({_SIDE_TEXT[participant.side]})")
        lines += ["", "Messages:"]
        day = ""
        for message in self.messages:
            if message.day and message.day != day:
                day = message.day
                lines.append(f"--- {day} ---")
            speaker = message.participant or "system"
            when = f" · {message.time}" if message.time else ""
            text = message.text
            if message.quoted:
                text = f"(replying to: {_quote(message.quoted[:160])}) {text}"
            if message.legibility == Legibility.PARTIAL:
                text += " [partly unreadable]"
            elif message.legibility == Legibility.UNREADABLE:
                text = "[unreadable]"
            lines.append(f"[{message.key}] {speaker}{when} · {text}")
        return "\n".join(lines)


_SIDE_TEXT = {
    Side.OWNER: "phone owner, right-hand bubbles",
    Side.CONTACT: "contact, left-hand bubbles",
    Side.UNKNOWN: "side not shown",
}


def _describe_source(conversation: Conversation) -> str:
    kind = "group chat" if conversation.is_group else "chat"
    if conversation.source_type == "screenshots":
        return f"Source: screenshots of a {conversation.app} {kind}, read in order."
    return f"Source: a {conversation.app} {kind} as text."


def _quote(text: str) -> str:
    return '"' + text.replace('"', "'") + '"'


def normalise_text(text: str) -> str:
    """Unicode NFC, no invisible or control characters, single spaces, no blank lines."""
    text = unicodedata.normalize("NFC", text)
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub(" ", text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    text = "\n".join(line for line in lines if line).strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1].rstrip() + "…"
    return text


def comparable(text: str) -> str:
    """Text reduced for matching repeated messages: lower case, letters and digits only."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[^\w₦$£€]+", "", text)
