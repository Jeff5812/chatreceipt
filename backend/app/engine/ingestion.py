"""Accepting uploaded files and starting conversations.

Files are untrusted. Their type is decided by their content, never by their name or the browser's
claim. Identical content from the same customer is stored once, and a conversation can reference it
again without another copy.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.config import Settings
from app.db.pool import Connection, Database
from app.engine import jobs, transactions
from app.errors import APIError
from app.observability import log_event, metrics
from app.storage import ObjectStorage, upload_key
from app.uploads import UploadRejected, inspect_image

Row = dict[str, Any]

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_IMAGE_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")
MAX_SOURCES = 20


@dataclass(frozen=True)
class CheckedFile:
    kind: str
    content_type: str
    width: int | None
    height: int | None


def clean_filename(name: str) -> str:
    """The original name, for display only. Stored files are named by ID, so this never becomes a path."""
    base = re.split(r"[\\/]", name or "")[-1]
    return _CONTROL.sub("", base).strip()[:255]


def decode_text(data: bytes, *, max_bytes: int) -> str | None:
    """Text from a conversation export, or None when these bytes aren't UTF-8 text."""
    if data.startswith(_IMAGE_SIGNATURES) or b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if len(data) > max_bytes:
        raise UploadRejected("text_too_large", "This conversation is too long. Export a shorter part of the chat.", status_code=413)
    return text


def check_file(data: bytes, settings: Settings) -> CheckedFile:
    if not data:
        raise UploadRejected("empty_file", "This file is empty.")
    text = decode_text(data, max_bytes=settings.max_text_bytes)
    if text is not None:
        if not text.strip():
            raise UploadRejected("empty_file", "This file is empty.")
        return CheckedFile("text", "text/plain", None, None)
    content_type, width, height = inspect_image(data, max_file_bytes=settings.max_file_bytes)
    return CheckedFile("image", content_type, width, height)


def store_file(
    database: Database, storage: ObjectStorage, settings: Settings, customer_id: UUID, filename: str, data: bytes
) -> tuple[Row, bool]:
    """Validate and store one file. Returns the upload and whether this content was already stored."""
    checked = check_file(data, settings)
    digest = hashlib.sha256(data).hexdigest()
    with database.tenant(customer_id) as conn:
        existing = _stored_copy(conn, customer_id, digest)
    if existing is not None:
        metrics.inc("chatreceipt_uploads_total", outcome="duplicate")
        return existing, True

    upload_id = uuid4()
    key = upload_key(customer_id, upload_id)
    storage.put(key, data)  # outside the transaction: never hold one open during file I/O
    with database.tenant(customer_id) as conn:
        row = conn.execute(
            """
            INSERT INTO chatreceipt.uploads
                (id, customer_id, kind, original_filename, content_type, byte_size, sha256, width, height, storage_key, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(days => %s))
            ON CONFLICT (customer_id, sha256) WHERE status = 'stored' DO NOTHING
            RETURNING *
            """,
            (
                upload_id, customer_id, checked.kind, clean_filename(filename), checked.content_type, len(data), digest,
                checked.width, checked.height, key, settings.upload_retention_days,
            ),
        ).fetchone()
        if row is None:
            existing = _stored_copy(conn, customer_id, digest)  # the same file arrived twice at once
    if row is None:
        storage.delete(key)
        metrics.inc("chatreceipt_uploads_total", outcome="duplicate")
        return existing, True  # type: ignore[return-value]
    metrics.inc("chatreceipt_uploads_total", outcome="stored")
    log_event("upload.stored", upload_id=upload_id, kind=checked.kind, bytes=len(data))
    return row, False


def _stored_copy(conn: Connection, customer_id: UUID, digest: str) -> Row | None:
    return conn.execute(
        "SELECT * FROM chatreceipt.uploads WHERE customer_id = %s AND sha256 = %s AND status = 'stored'", (customer_id, digest)
    ).fetchone()


def get_upload(conn: Connection, customer_id: UUID, upload_id: UUID) -> Row | None:
    return conn.execute("SELECT * FROM chatreceipt.uploads WHERE id = %s AND customer_id = %s", (upload_id, customer_id)).fetchone()


def mark_deleted(conn: Connection, customer_id: UUID, upload_id: UUID) -> str | None:
    """Mark an upload deleted and return its storage key, so the caller can delete the file after commit."""
    row = conn.execute(
        "SELECT storage_key FROM chatreceipt.uploads WHERE id = %s AND customer_id = %s AND status = 'stored' FOR UPDATE",
        (upload_id, customer_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE chatreceipt.uploads SET status = 'deleted', storage_key = NULL, deleted_at = now() WHERE id = %s", (upload_id,)
    )
    return row["storage_key"]


def start_conversation(
    conn: Connection, settings: Settings, customer_id: UUID, upload_ids: list[UUID], title: str
) -> tuple[Row, Row, Row]:
    """Create the conversation, a transaction in ``processing`` and its job, together."""
    if not 1 <= len(upload_ids) <= MAX_SOURCES or len(set(upload_ids)) != len(upload_ids):
        raise APIError(422, "invalid_sources", f"Choose between 1 and {MAX_SOURCES} different uploads.")
    uploads = conn.execute(
        "SELECT id, kind, status FROM chatreceipt.uploads WHERE customer_id = %s AND id = ANY(%s)", (customer_id, upload_ids)
    ).fetchall()
    by_id = {row["id"]: row for row in uploads}
    if len(by_id) != len(upload_ids):
        raise APIError(404, "upload_not_found", "One of those uploads doesn't exist.")
    if any(row["status"] != "stored" for row in uploads):
        raise APIError(409, "upload_deleted", "One of those uploads has been deleted. Upload it again.")
    kinds = {row["kind"] for row in uploads}
    if kinds == {"text"} and len(uploads) > 1:
        raise APIError(422, "invalid_sources", "A text conversation must be a single file.")
    if len(kinds) > 1:
        raise APIError(422, "invalid_sources", "Use either screenshots or one text export, not both.")

    conversation = conn.execute(
        "INSERT INTO chatreceipt.conversations (customer_id, source_type, title) VALUES (%s, %s, %s) RETURNING *",
        (customer_id, "text" if kinds == {"text"} else "screenshots", title.strip()[:120]),
    ).fetchone()
    assert conversation is not None
    for position, upload_id in enumerate(upload_ids, start=1):
        conn.execute(
            "INSERT INTO chatreceipt.conversation_sources (conversation_id, customer_id, position, upload_id) VALUES (%s, %s, %s, %s)",
            (conversation["id"], customer_id, position, upload_id),
        )
    transaction = transactions.create_placeholder(conn, settings, customer_id, conversation["id"])
    job = jobs.enqueue(conn, customer_id, jobs.PROCESS_CONVERSATION, conversation["id"], max_attempts=settings.engine_job_max_attempts)
    return conversation, transaction, job
