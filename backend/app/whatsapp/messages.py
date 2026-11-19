"""Outgoing message builders, kept within the Cloud API's limits, and button IDs.

A button's ID comes back when the customer taps it. It names the action and, for anything about a
draft, the submission and the version the customer was looking at, so an old message can never act
on a newer draft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

TEXT_LIMIT = 4096
BUTTON_BODY_LIMIT = 1024
LIST_BODY_LIMIT = 4096
FOOTER_LIMIT = 60
CAPTION_LIMIT = 1024
BUTTON_TITLE_LIMIT = 20
MAX_BUTTONS = 3
LIST_BUTTON_LIMIT = 20
ROW_TITLE_LIMIT = 24
ROW_DESCRIPTION_LIMIT = 72
ROW_ID_LIMIT = 200
MAX_ROWS = 10

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PREFIX = "cr"
# Queued instead of a message: the sender renders the receipt when it is this reply's turn to go out.
RECEIPT_REQUEST = "chatreceipt_receipt"


def fit(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def text_message(body: str) -> dict[str, Any]:
    return {"type": "text", "text": {"body": fit(body, TEXT_LIMIT), "preview_url": False}}


def receipt_request(transaction_id: str, file_format: str, caption: str) -> dict[str, Any]:
    return {"type": RECEIPT_REQUEST, "transaction_id": transaction_id, "format": file_format, "caption": fit(caption, CAPTION_LIMIT)}


def image_message(media_id: str, caption: str) -> dict[str, Any]:
    return {"type": "image", "image": {"id": media_id, "caption": fit(caption, CAPTION_LIMIT)}}


def document_message(media_id: str, filename: str, caption: str) -> dict[str, Any]:
    return {"type": "document", "document": {"id": media_id, "filename": filename, "caption": fit(caption, CAPTION_LIMIT)}}


def button_message(body: str, buttons: list[tuple[str, str]], *, footer: str = "") -> dict[str, Any]:
    """Up to three reply buttons, each (id, title)."""
    interactive: dict[str, Any] = {
        "type": "button",
        "body": {"text": fit(body, BUTTON_BODY_LIMIT)},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": button_id, "title": fit(title, BUTTON_TITLE_LIMIT)}}
                for button_id, title in buttons[:MAX_BUTTONS]
            ]
        },
    }
    if footer:
        interactive["footer"] = {"text": fit(footer, FOOTER_LIMIT)}
    return {"type": "interactive", "interactive": interactive}


@dataclass(frozen=True)
class Row:
    id: str
    title: str
    description: str = ""


def list_message(body: str, button: str, rows: list[Row]) -> dict[str, Any]:
    """A pick-one list of up to ten rows."""
    return {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": fit(body, LIST_BODY_LIMIT)},
            "action": {
                "button": fit(button, LIST_BUTTON_LIMIT),
                "sections": [
                    {
                        "title": "Options",
                        "rows": [
                            {
                                "id": row.id[:ROW_ID_LIMIT],
                                "title": fit(row.title, ROW_TITLE_LIMIT),
                                **({"description": fit(row.description, ROW_DESCRIPTION_LIMIT)} if row.description else {}),
                            }
                            for row in rows[:MAX_ROWS]
                        ],
                    }
                ],
            },
        },
    }


@dataclass(frozen=True)
class ReplyId:
    action: str
    submission_id: str = ""
    revision: int = 0
    arg: str = ""


def reply_id(action: str, submission_id: str = "", revision: int = 0, arg: str = "") -> str:
    return "|".join((_PREFIX, action, submission_id, str(revision) if revision else "", arg))


def parse_reply_id(raw: str) -> ReplyId | None:
    parts = raw.split("|", 4)
    if len(parts) != 5 or parts[0] != _PREFIX or not parts[1]:
        return None
    _, action, submission_id, revision, arg = parts
    if submission_id and not _UUID.match(submission_id):
        return None
    if revision and not revision.isdigit():
        return None
    return ReplyId(action=action, submission_id=submission_id, revision=int(revision or 0), arg=arg)
