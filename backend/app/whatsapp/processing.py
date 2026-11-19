"""Reading a submission's screenshots, with the same checks and extraction service as the website.

The images are fetched from WhatsApp when they're needed, validated by ``prepare_screenshots``,
read by ``run_extraction`` and then discarded. Only the extracted draft is stored.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.config import Settings
from app.db.pool import Connection
from app.extraction.provider import ErrorKind, ExtractionProvider, ExtractionProviderError
from app.extraction.service import run_extraction
from app.models import ExtractionResponse
from app.observability import log_event
from app.uploads import UploadedFile, UploadRejected, prepare_screenshots
from app.whatsapp import store
from app.whatsapp.client import MediaError, WhatsAppClient
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
) -> ExtractionResponse:
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
    if not response.contains_transaction:
        store.update_submission(conn, submission["id"], status="failed", failure_code="no_transaction", lease_token=None, lease_expires_at=None)
        turn.send(_after_failure(NO_TRANSACTION))
        log_event("whatsapp.submission_finished", outcome="no_transaction", images=len(submission["images"]))
        return
    hashes = [image["sha256"] for image in submission["images"] if image.get("sha256")]
    review: dict[str, Any] = {
        "reasons": [reason.model_dump() for reason in response.review_reasons],
        "duplicate_reference": store.find_saved_with_images(conn, customer["id"], hashes),
    }
    if response.date_label:
        review["date_label"] = response.date_label  # shown next to the date, so an old screenshot is caught
    updated = store.update_submission(
        conn,
        submission["id"],
        status="awaiting_confirmation",
        draft=response.draft.model_dump(mode="json"),
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
    turn.send(_after_failure(failure.message))
    log_event("whatsapp.submission_finished", outcome="failed", code=failure.code, attempts=submission["attempts"])


def _after_failure(message: str) -> dict[str, Any]:
    return button_message(
        message,
        [(reply_id("menu", arg="retry"), "Try again"), (reply_id("menu", arg="scan"), "New screenshot"), (reply_id("menu", arg="help"), "Get help")],
    )
