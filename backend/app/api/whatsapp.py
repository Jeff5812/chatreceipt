"""The WhatsApp Cloud API webhook.

GET answers Meta's verification handshake. POST receives events: it verifies the signature, stores
each message once and answers immediately; a background worker does the rest. A delivery is only
acknowledged after it has been committed, so anything that fails here is delivered again by Meta.
"""

from __future__ import annotations

import hmac
import json
import re
from typing import Any

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.db.pool import Database, DatabaseUnavailable
from app.errors import APIError
from app.observability import log_event
from app.whatsapp import store
from app.whatsapp.client import signature_is_valid
from app.whatsapp.events import ParsedWebhook, parse_webhook

router = APIRouter()

# Meta documents webhook payloads of up to 3 MB.
MAX_WEBHOOK_BYTES = 3 * 1024 * 1024
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def _require_configured(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    if not settings.whatsapp_configured or request.app.state.database is None:
        raise APIError(503, "whatsapp_unavailable", "WhatsApp isn't available right now.")
    return settings


@router.get("/api/whatsapp/webhook", response_class=PlainTextResponse, include_in_schema=False)
async def verify_webhook(request: Request) -> PlainTextResponse:
    settings = _require_configured(request)
    params = request.query_params
    expected = settings.whatsapp_verify_token.get_secret_value()  # type: ignore[union-attr]
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")
    if (
        params.get("hub.mode") != "subscribe"
        or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
        or not _CHALLENGE.match(challenge)
    ):
        raise APIError(403, "verification_failed", "The webhook verification failed.")
    return PlainTextResponse(challenge)


def _store_delivery(database: Database, parsed: ParsedWebhook, business_number_id: str = "") -> int:
    stored = 0
    with database.transaction() as conn:
        for change in parsed.identity_changes:
            store.apply_identity_change(conn, change)
        for message in sorted(parsed.messages, key=lambda item: item.sent_at):
            stored += store.record_inbound(conn, message, business_number_id=business_number_id)
        for failure in parsed.failures:
            store.record_delivery_failure(conn, failure)
    return stored


@router.post("/api/whatsapp/webhook", include_in_schema=False)
async def receive_webhook(request: Request) -> dict[str, Any]:
    settings = _require_configured(request)
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise APIError(413, "request_too_large", "That request is too large.")
    secret = settings.whatsapp_app_secret.get_secret_value()  # type: ignore[union-attr]
    if not signature_is_valid(body, request.headers.get("x-hub-signature-256"), secret):
        log_event("whatsapp.webhook_rejected", reason="invalid_signature")
        raise APIError(401, "invalid_signature", "The request signature is not valid.")
    try:
        payload = json.loads(body)
    except ValueError:
        # Correctly signed but unreadable: redelivering the same bytes can't help.
        log_event("whatsapp.webhook_unreadable")
        return {"status": "ignored"}

    parsed = parse_webhook(payload, phone_number_id=settings.whatsapp_phone_number_id)
    try:
        stored = await run_in_threadpool(_store_delivery, request.app.state.database, parsed, settings.whatsapp_phone_number_id)
    except (DatabaseUnavailable, psycopg.Error) as exc:
        # Meta delivers again later. Only the type is logged: database errors can quote phone numbers.
        log_event("whatsapp.webhook_deferred", reason=type(exc).__name__)
        raise APIError(503, "temporarily_unavailable", "Please retry this delivery.") from None

    worker = request.app.state.whatsapp_worker
    if worker is not None and (stored or parsed.identity_changes):
        worker.notify()
    log_event(
        "whatsapp.webhook_received",
        messages=len(parsed.messages),
        new_messages=stored,
        duplicates=len(parsed.messages) - stored,
        skipped=parsed.skipped,
        delivery_failures=len(parsed.failures),
    )
    return {"status": "received"}
