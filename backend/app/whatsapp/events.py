"""Read WhatsApp Cloud API webhook deliveries into simple, validated values.

Call this only after the delivery's signature has been verified. Anything malformed is skipped
rather than raised: retrying a delivery can't repair it, so it must not block the rest of the batch.

The customer's identity is Meta's business-scoped user ID (``from_user_id``). The phone number
(``from``) is optional: Meta leaves it out for people who use a WhatsApp username.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.uploads import ACCEPTED_FORMATS

IMAGE_TYPES = frozenset(ACCEPTED_FORMATS.values())
MAX_TEXT_CHARS = 4096

Kind = Literal["text", "image", "reply", "unsupported"]


@dataclass(frozen=True)
class Sender:
    user_id: str | None
    phone: str | None


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender: Sender
    sent_at: datetime
    kind: Kind
    text: str = ""
    reply_id: str = ""
    media_id: str = ""
    mime_type: str = ""
    sha256: str = ""

    def payload(self) -> dict[str, str]:
        """What the conversation step needs later; empty values are left out."""
        values = {
            "text": self.text,
            "reply_id": self.reply_id,
            "media_id": self.media_id,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
        }
        return {key: value for key, value in values.items() if value}


@dataclass(frozen=True)
class IdentityChange:
    """Someone changed their number: the old identity continues under the new one."""

    previous_user_id: str
    user_id: str
    phone: str | None


@dataclass(frozen=True)
class DeliveryFailure:
    message_id: str
    code: int | None


@dataclass
class ParsedWebhook:
    messages: list[InboundMessage] = field(default_factory=list)
    identity_changes: list[IdentityChange] = field(default_factory=list)
    failures: list[DeliveryFailure] = field(default_factory=list)
    skipped: int = 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _token(value: Any, limit: int = 200) -> str:
    """A short identifier: a non-empty string with no whitespace, or empty."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text if text and len(text) <= limit and not any(c.isspace() for c in text) else ""


def _phone(value: Any) -> str | None:
    text = _token(value, 20)
    return text if text.isdigit() and len(text) >= 5 else None


def _timestamp(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(UTC)


def _mime(value: Any) -> str:
    return value.split(";")[0].strip().lower() if isinstance(value, str) else ""


def _sender(raw: dict[str, Any], contacts: list[dict[str, Any]]) -> Sender | None:
    only = contacts[0] if len(contacts) == 1 else {}
    user_id = _token(raw.get("from_user_id")) or _token(only.get("user_id")) or None
    phone = _phone(raw.get("from")) or _phone(only.get("wa_id"))
    return Sender(user_id=user_id, phone=phone) if user_id or phone else None


def _read_message(raw: dict[str, Any], contacts: list[dict[str, Any]], parsed: ParsedWebhook) -> None:
    kind = raw.get("type")
    if kind == "system":
        system = _dict(raw.get("system"))
        previous, current = _token(system.get("previous_user_id")), _token(system.get("user_id"))
        if previous and current and previous != current:
            parsed.identity_changes.append(IdentityChange(previous, current, _phone(system.get("wa_id"))))
        return
    if kind == "reaction":
        return  # a reaction to a message needs no reply

    message_id = _token(raw.get("id"))
    sender = _sender(raw, contacts)
    if not message_id or sender is None or raw.get("group_id"):
        parsed.skipped += 1
        return
    base: dict[str, Any] = {"message_id": message_id, "sender": sender, "sent_at": _timestamp(raw.get("timestamp"))}

    if kind == "text":
        body = _dict(raw.get("text")).get("body")
        text = body.strip()[:MAX_TEXT_CHARS] if isinstance(body, str) else ""
        parsed.messages.append(InboundMessage(**base, kind="text" if text else "unsupported", text=text))
    elif kind in ("image", "document"):
        media = _dict(raw.get(kind))
        media_id, mime_type = _token(media.get("id")), _mime(media.get("mime_type"))
        if media_id and mime_type in IMAGE_TYPES:
            parsed.messages.append(
                InboundMessage(**base, kind="image", media_id=media_id, mime_type=mime_type, sha256=_token(media.get("sha256")))
            )
        else:
            parsed.messages.append(InboundMessage(**base, kind="unsupported"))
    elif kind == "interactive":
        interactive = _dict(raw.get("interactive"))
        reply = _dict(interactive.get("button_reply")) or _dict(interactive.get("list_reply"))
        reply_id = _token(reply.get("id"), 256)
        parsed.messages.append(InboundMessage(**base, kind="reply", reply_id=reply_id) if reply_id else InboundMessage(**base, kind="unsupported"))
    elif kind == "button":
        text = _dict(raw.get("button")).get("text")
        parsed.messages.append(InboundMessage(**base, kind="text", text=text.strip()[:MAX_TEXT_CHARS]) if isinstance(text, str) and text.strip() else InboundMessage(**base, kind="unsupported"))
    elif kind == "request_welcome":
        parsed.messages.append(InboundMessage(**base, kind="text", text="hi"))  # the chat was opened for the first time
    else:
        parsed.messages.append(InboundMessage(**base, kind="unsupported"))


def _read_status(raw: dict[str, Any], parsed: ParsedWebhook) -> None:
    if raw.get("status") != "failed":
        return
    message_id = _token(raw.get("id"))
    if message_id:
        errors = _list(raw.get("errors"))
        code = _dict(errors[0]).get("code") if errors else None
        parsed.failures.append(DeliveryFailure(message_id, code if isinstance(code, int) else None))


def parse_webhook(body: Any, *, phone_number_id: str) -> ParsedWebhook:
    parsed = ParsedWebhook()
    root = _dict(body)
    if root.get("object") != "whatsapp_business_account":
        return parsed
    for entry in _list(root.get("entry")):
        for change in _list(_dict(entry).get("changes")):
            change = _dict(change)
            if change.get("field") != "messages":
                continue
            value = _dict(change.get("value"))
            if str(_dict(value.get("metadata")).get("phone_number_id") or "") != phone_number_id:
                parsed.skipped += 1  # another number on the same app
                continue
            contacts = [_dict(contact) for contact in _list(value.get("contacts"))]
            for raw in _list(value.get("messages")):
                _read_message(_dict(raw), contacts, parsed)
            for raw in _list(value.get("statuses")):
                _read_status(_dict(raw), parsed)
    return parsed
