"""Reading a submission's screenshots or chat export, with the same checks and extraction as the website.

The images are fetched from WhatsApp when they're needed, validated by ``prepare_screenshots``,
read by ``run_extraction`` and then discarded. A chat export (.txt or .zip) is parsed by
``read_chat_export`` and read by ``run_chat_extraction``. Only the extracted draft is stored, with
who is who worked out from the customer's confirmed names and the people they dealt with before.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime
from collections.abc import Sequence
from typing import Any

from app.chat_import import read_chat_export
from app.config import Settings
from app.db.pool import Connection
from app.extraction.provider import ErrorKind, ExtractionProvider, ExtractionProviderError
from app.extraction.chat import run_chat_extraction
from app.extraction.service import run_extraction
from app.models import ExtractionResponse
from app.observability import log_event
from app.uploads import UploadedFile, UploadRejected, prepare_screenshots
from app.whatsapp import parties, store
from app.whatsapp.client import MediaError, WhatsAppClient
from app.whatsapp.events import CHAT_EXPORT_TYPES
from app.whatsapp.conversation import Turn
from app.whatsapp.messages import button_message, reply_id
from app.whatsapp.store import Row

DELAYED = "⏳ This is taking longer than usual. I'm still working on it and will send the details here."
TRY_AGAIN = "I couldn't read your screenshot right now, so nothing was saved. Tap *Try again* in a few minutes."
DOWNLOAD_FAILED = "I couldn't download that image from WhatsApp, so nothing was saved. Please send it again."
NO_TRANSACTION = (
    "I couldn't find a transaction in that screenshot, so nothing was saved. Send a screenshot that shows the "
    "items or the amount agreed."
)
DECLINED = "I can't process that screenshot, so nothing was saved. Please try a different one."
UNAVAILABLE = "Reading screenshots isn't available right now, so nothing was saved. Please try again later."
# The same, for a chat export.
TRY_AGAIN_CHAT = "I couldn't read your chat export right now, so nothing was saved. Tap *Try again* in a few minutes."
DOWNLOAD_FAILED_CHAT = "I couldn't download that file from WhatsApp, so nothing was saved. Please send it again."
NO_TRANSACTION_IN_CHAT = (
    "I couldn't find a transaction in that chat, so nothing was saved. Export the chat where the price or payment was "
    "agreed, or send screenshots of that part."
)
DECLINED_CHAT = "I can't process that chat, so nothing was saved. Please send screenshots of the part that matters instead."
UNAVAILABLE_CHAT = "Reading chats isn't available right now, so nothing was saved. Please try again later."

# Problems with a chat export, whose message already says what to do instead.
CHAT_EXPORT_CODES = frozenset({"not_a_chat_export", "bad_zip", "zip_unsafe_path", "zip_encrypted", "zip_suspicious", "zip_too_many_files", "text_too_large", "empty_file"})
_DOCUMENT_TYPES = CHAT_EXPORT_TYPES | {"application/octet-stream"}

_RETRYABLE_KINDS = frozenset({ErrorKind.TIMEOUT, ErrorKind.RATE_LIMITED, ErrorKind.UNAVAILABLE, ErrorKind.MALFORMED})
_MESSAGES = {ErrorKind.REFUSED: DECLINED, ErrorKind.NOT_CONFIGURED: UNAVAILABLE, ErrorKind.BAD_REQUEST: TRY_AGAIN}


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    retryable: bool


def failure_from(exc: Exception) -> Failure:
    if isinstance(exc, ExtractionProviderError):
        return Failure(f"ai_{exc.kind.value}", _MESSAGES.get(exc.kind, TRY_AGAIN), exc.kind in _RETRYABLE_KINDS)
    if isinstance(exc, UploadRejected):
        if exc.code in CHAT_EXPORT_CODES:
            return Failure(exc.code, f"{exc.message} Nothing was saved.", False)
        return Failure(exc.code, f"{exc.message} Nothing was saved. Please send a clearer screenshot.", False)
    if isinstance(exc, MediaError):
        return Failure(exc.code, DOWNLOAD_FAILED, exc.retryable)
    return Failure("unexpected_error", TRY_AGAIN, True)


def retry_delay(attempt: int, settings: Settings) -> float:
    """Exponential backoff with jitter, so retries from many customers don't arrive together."""
    delay = min(settings.whatsapp_retry_max_seconds, settings.whatsapp_retry_base_seconds * 2 ** max(attempt - 1, 0))
    return delay * random.uniform(0.8, 1.2)


async def read_images(
    images: list[dict[str, str]],
    *,
    client: WhatsAppClient,
    provider: ExtractionProvider,
    settings: Settings,
    received_on: date | None = None,
    owner_names: Sequence[str] = (),
) -> ExtractionResponse:
    exports = [image for image in images if image.get("kind") == "chat_export"]
    if exports:
        document = exports[-1]
        data = await client.download_media(document["media_id"], max_bytes=settings.max_chat_export_bytes, allowed_types=_DOCUMENT_TYPES)
        export = await asyncio.to_thread(
            read_chat_export, data, document.get("filename", ""),
            max_text_bytes=settings.max_chat_text_bytes, max_file_bytes=settings.max_file_bytes, owner_names=owner_names,
        )
        return await run_chat_extraction(provider, export, settings, focus=document.get("focus", ""))
    uploads = [
        UploadedFile(name=f"whatsapp-{number}", data=await client.download_media(image["media_id"], max_bytes=settings.max_file_bytes))
        for number, image in enumerate(images, start=1)
    ]
    prepared = await asyncio.to_thread(
        prepare_screenshots,
        uploads,
        max_files=settings.max_files,
        max_file_bytes=settings.max_file_bytes,
        max_total_bytes=settings.max_total_bytes,
    )
    return await run_extraction(provider, prepared, settings, received_on=received_on)


def finish_success(conn: Connection, settings: Settings, claimed: Row, response: ExtractionResponse) -> None:
    locked = store.lock_claimed_submission(conn, claimed["id"], claimed["lease_token"])
    if locked is None:
        return  # cancelled, or taken over by another worker: this result is discarded
    customer, submission = locked
    turn = Turn(conn, settings, customer, f"sub:{submission['id']}:{submission['attempts']}:{submission['images_version']}")
    if submission["images_version"] != claimed["images_version"]:
        store.restart_collection(
            conn, submission["id"], submission["images"], wait_seconds=settings.whatsapp_album_wait_seconds, revision=submission["revision"]
        )
        return
    export = next((image for image in submission["images"] if image.get("kind") == "chat_export"), None)
    if not response.contains_transaction:
        store.update_submission(conn, submission["id"], status="failed", failure_code="no_transaction", lease_token=None, lease_expires_at=None)
        turn.send(_after_failure(NO_TRANSACTION_IN_CHAT if export else NO_TRANSACTION, export=export is not None))
        log_event("whatsapp.submission_finished", outcome="no_transaction", images=len(submission["images"]), export=export is not None)
        return
    hashes = [image["sha256"] for image in submission["images"] if image.get("sha256")]
    draft, state = identify_parties(conn, customer, response)
    review: dict[str, Any] = {
        "reasons": [*(reason.model_dump() for reason in response.review_reasons), *parties.identity_checks(state)],
        "duplicate_reference": store.find_saved_with_images(conn, customer["id"], hashes),
        "parties": state,
    }
    if response.date_label:
        review["date_label"] = response.date_label  # shown next to the date, so an old screenshot is caught
    if response.source == "chat_export":
        review["source"] = "chat_export"
    # Another deal from the same export keeps the first reading's list, so the numbers stay the same.
    deals = (export or {}).get("deals") or [deal.summary for deal in response.other_deals[:3]]
    if deals:
        review["other_deals"] = deals[:3]
    if (export or {}).get("focus"):
        review["focus"] = export["focus"]  # type: ignore[index]
    updated = store.update_submission(
        conn,
        submission["id"],
        status="awaiting_confirmation",
        draft=draft.model_dump(mode="json"),
        review=review,
        revision=submission["revision"] + 1,
        lease_token=None,
        lease_expires_at=None,
        failure_code=None,
    )
    turn.show_draft(updated, lead="🧾 *Here's what I found*")
    log_event(
        "whatsapp.submission_finished",
        outcome="draft_ready",
        images=len(submission["images"]),
        attempts=submission["attempts"],
        review_required=response.review_required,
        can_save=response.calculation.can_export,
    )


def identify_parties(conn: Connection, customer: Row, response: ExtractionResponse) -> tuple[Any, dict[str, Any]]:
    """Who is who on a new draft, from the chat and from what the customer confirmed before.

    Their own side gets the name they confirmed for receipts; the other side gets the name they
    confirmed for that person, matched by phone number or, as a marked suggestion, by chat label.
    """
    found = response.parties
    state: dict[str, Any] = {"owner_role": found.owner_role, "other_party": found.other_party, "other_phone": found.other_phone}
    owner_label = ""
    if response.chat is not None:
        owner_label = next((person.name for person in response.chat.participants if person.side == "owner"), "")
        contact = next((person for person in response.chat.participants if person.side == "contact"), None)
        if contact is not None and not response.chat.group:
            state["other_phone"] = state["other_phone"] or contact.phone
    if owner_label and not parties.phone_in_label(owner_label):
        state["owner_label"] = owner_label  # their own name in the export, offered when their name is asked
    payer = next(
        (person.name for person in found.people if person.role == "payer" and person.name and not parties.same_name(person.name, response.draft.buyer)),
        "",
    )
    if payer:
        state["paid_by"] = payer
    draft, state = parties.fill_saved_name(response.draft, state, customer.get("party_name"), owner_label=owner_label)
    match = None
    if state["owner_role"] and (state["other_phone"] or state["other_party"]):
        match = store.find_counterparty(conn, customer["id"], phone=state["other_phone"], label=state["other_party"])
    draft, state = parties.recall_counterparty(draft, state, match)
    log_event(
        "whatsapp.parties_read",
        source=response.source,
        owner_role=state["owner_role"] or "unknown",
        saved_name_used=bool(state.get("you")),
        counterpart_match=(state.get("counterpart") or {}).get("match", "none"),
        seller=bool(draft.seller.strip()),
        buyer=bool(draft.buyer.strip()),
        third_party_payer=bool(payer),
    )
    return draft, state


def finish_failure(conn: Connection, settings: Settings, claimed: Row, failure: Failure) -> None:
    locked = store.lock_claimed_submission(conn, claimed["id"], claimed["lease_token"])
    if locked is None:
        return
    customer, submission = locked
    turn = Turn(conn, settings, customer, f"sub:{submission['id']}:{submission['attempts']}:failure")
    if failure.retryable and submission["attempts"] < settings.whatsapp_extraction_max_attempts:
        # A quick retry the customer won't notice needs no message; a real wait does.
        waited = (datetime.now(UTC) - submission["first_image_at"]).total_seconds()
        notify = not submission["delay_notified"] and waited >= settings.whatsapp_delay_notice_seconds
        store.schedule_submission_retry(
            conn, submission["id"], delay_seconds=retry_delay(submission["attempts"], settings), delay_notified=submission["delay_notified"] or notify
        )
        if notify:
            turn.say(DELAYED)
        log_event("whatsapp.submission_retry", code=failure.code, attempts=submission["attempts"])
        return
    store.update_submission(conn, submission["id"], status="failed", failure_code=failure.code, lease_token=None, lease_expires_at=None)
    export = any(image.get("kind") == "chat_export" for image in submission["images"])
    message = failure.message
    if export:
        message = {TRY_AGAIN: TRY_AGAIN_CHAT, DOWNLOAD_FAILED: DOWNLOAD_FAILED_CHAT, DECLINED: DECLINED_CHAT, UNAVAILABLE: UNAVAILABLE_CHAT}.get(message, message)
    turn.send(_after_failure(message, export=export))
    log_event("whatsapp.submission_finished", outcome="failed", code=failure.code, attempts=submission["attempts"], export=export)


def _after_failure(message: str, *, export: bool = False) -> dict[str, Any]:
    return button_message(
        message,
        [
            (reply_id("menu", arg="retry"), "Try again"),
            (reply_id("menu", arg="scan"), "Send screenshots" if export else "New screenshot"),
            (reply_id("menu", arg="help"), "Get help"),
        ],
    )
