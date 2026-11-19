"""Integration events for other systems such as n8n, delivered reliably.

An event is written in the same database transaction as the change it reports, so a change can't
happen without its event, and an event can't exist for a change that was rolled back. The worker
then delivers it over HTTPS with a signature, retrying until the receiver accepts it.

Delivery is at least once, so receivers must ignore event IDs they have already processed. Payloads
carry IDs, states and totals only: never names or conversation text. ChatReceipt works normally
when no webhook is configured or the receiver is down.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb

from app.config import Settings
from app.db.pool import Connection

Row = dict[str, Any]

TRANSACTION_APPROVED = "transaction.approved"
TRANSACTION_COMPLETED = "transaction.completed"
TRANSACTION_CANCELLED = "transaction.cancelled"
RECEIPT_GENERATED = "receipt.generated"
PROCESSING_FAILED = "processing.failed"
SIGNATURE_HEADER = "X-ChatReceipt-Signature"
_HOLD_SECONDS = 60.0


def emit(conn: Connection, settings: Settings, customer_id: UUID, event_type: str, aggregate_id: UUID, data: dict[str, Any]) -> UUID:
    event_id = uuid4()
    payload = {"id": str(event_id), "type": event_type, "created_at": datetime.now(UTC).isoformat(), "data": data}
    conn.execute(
        """
        INSERT INTO chatreceipt.outbox_events (id, customer_id, event_type, aggregate_id, payload, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (event_id, customer_id, event_type, aggregate_id, Jsonb(payload), "pending" if settings.webhooks_configured else "skipped"),
    )
    return event_id


def signature(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def signature_is_valid(secret: str, header: str, body: bytes, *, tolerance_seconds: int = 300, now: float | None = None) -> bool:
    """What a receiver should check: the HMAC matches and the timestamp is recent, to stop replays."""
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        timestamp = int(fields["t"])
    except (ValueError, KeyError):
        return False
    if abs((now if now is not None else time.time()) - timestamp) > tolerance_seconds:
        return False
    return hmac.compare_digest(signature(secret, timestamp, body), header)


def claim(conn: Connection) -> Row | None:
    return conn.execute(
        """
        UPDATE chatreceipt.outbox_events e
        SET attempts = e.attempts + 1, next_attempt_at = now() + make_interval(secs => %s)
        WHERE e.id = (
            SELECT id FROM chatreceipt.outbox_events
            WHERE status = 'pending' AND next_attempt_at <= now()
            ORDER BY next_attempt_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING e.*
        """,
        (_HOLD_SECONDS,),
    ).fetchone()


def mark_delivered(conn: Connection, event_id: UUID) -> None:
    conn.execute(
        "UPDATE chatreceipt.outbox_events SET status = 'delivered', delivered_at = now(), last_error = NULL WHERE id = %s", (event_id,)
    )


def mark_undelivered(conn: Connection, event: Row, settings: Settings, error: str, *, delay_seconds: float) -> None:
    give_up = event["attempts"] >= settings.webhook_max_attempts
    conn.execute(
        """
        UPDATE chatreceipt.outbox_events
        SET status = %s, last_error = %s, next_attempt_at = now() + make_interval(secs => %s)
        WHERE id = %s
        """,
        ("failed" if give_up else "pending", error, delay_seconds, event["id"]),
    )


async def deliver(client: httpx.AsyncClient, settings: Settings, event: Row) -> str | None:
    """Send one event. Returns None on success, or a short reason it wasn't accepted."""
    body = json.dumps(event["payload"], separators=(",", ":"), sort_keys=True).encode("utf-8")
    secret = settings.webhook_secret.get_secret_value() if settings.webhook_secret else ""
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature(secret, int(time.time()), body),
        "X-ChatReceipt-Event-Id": str(event["id"]),
        "X-ChatReceipt-Event-Type": event["event_type"],
    }
    try:
        response = await client.post(settings.webhook_url, content=body, headers=headers, timeout=settings.webhook_timeout_seconds)
    except httpx.HTTPError as exc:
        return type(exc).__name__
    return None if 200 <= response.status_code < 300 else f"status_{response.status_code}"
