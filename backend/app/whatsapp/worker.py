"""The background loops behind the WhatsApp channel.

Three kinds of work, each safe to retry and safe to run in several processes at once:

- incoming messages, one at a time per customer, in order;
- reading screenshots, which is slow and runs outside any database transaction;
- sending replies, in order per customer, with retries for temporary failures.

Usually started inside the API process. To run it separately: ``python -m app.whatsapp.worker``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo
from typing import Any
from uuid import UUID

from app.config import Settings, get_settings
from app.db.pool import Database, DatabaseUnavailable
from app.documents.browser import BrowserRenderer, DocumentRenderError
from app.extraction.provider import AIProviderError, ExtractionProvider
from app.extraction.providers import create_provider
from app.models import TransactionDraft
from app.observability import configure_logging, log_event
from app.whatsapp import store
from app.whatsapp.client import CUSTOMER_WINDOW_CLOSED, WhatsAppAPIError, WhatsAppClient
from app.whatsapp.conversation import finish_instruction, handle_inbound
from app.whatsapp.messages import RECEIPT_REQUEST, text_message
from app.whatsapp.processing import (
    TRY_AGAIN,
    Failure,
    failure_from,
    finish_failure,
    finish_success,
    read_images,
    retry_delay,
)
from app.whatsapp.receipts import RECEIPT_FAILED, RECORD_GONE, DocumentRenderer, receipt_message
from app.whatsapp.store import Row
from app.whatsapp.summary import current_question, open_reasons
from app.whatsapp.understanding import AICorrection, understand

logger = logging.getLogger(__name__)

MAX_INBOUND_ATTEMPTS = 5
_RENDER_ATTEMPTS = 3
# A typed correction is tried this many times if workers stop part-way; then the customer is told.
_INSTRUCTION_ATTEMPTS = 2
# WhatsApp's typing indicator lasts about 25 seconds; renew it while a screenshot is still being read.
_TYPING_EVERY_SECONDS = 20.0
# Long enough to render, upload and send a receipt before another sender may pick the reply up.
_SEND_HOLD_SECONDS = 120.0
_MAINTENANCE_SECONDS = 3600.0
_APOLOGY = "Sorry, something went wrong with that message. Please send it again."


class WhatsAppWorker:
    def __init__(
        self, database: Database, client: WhatsAppClient, provider: ExtractionProvider, settings: Settings, renderer: DocumentRenderer
    ) -> None:
        self._db = database
        self._client = client
        self._provider = provider
        self._settings = settings
        self._renderer = renderer
        # Free-text corrections need a provider that can answer a text-only request.
        self._understands = callable(getattr(provider, "generate", None)) and bool(getattr(provider, "configured", False))
        self._tasks: list[asyncio.Task[None]] = []
        self._wakes: list[asyncio.Event] = []
        self._last_maintenance = 0.0

    # -- one unit of work each; True means there may be more -----------------------------------

    def _handle_inbound_now(self) -> bool:
        failed: Row | None = None
        try:
            with self._db.transaction() as conn:
                row = store.claim_inbound(conn)
                if row is None:
                    return False
                try:
                    postpone = handle_inbound(conn, self._settings, row, understanding=self._understands)
                except Exception:
                    failed = row
                    raise
                if postpone is not None:
                    store.postpone_inbound(conn, row["message_id"], seconds=postpone)
                else:
                    store.finish_inbound(conn, row["message_id"])
            return True
        except DatabaseUnavailable:
            raise
        except Exception as exc:
            if failed is None:
                raise
            logger.error("WhatsApp message handling failed (%s)", type(exc).__name__)
            self._record_inbound_failure(failed)
            return True

    def _record_inbound_failure(self, row: Row) -> None:
        attempts = row["attempts"] + 1
        give_up = attempts >= MAX_INBOUND_ATTEMPTS
        with self._db.transaction() as conn:
            store.retry_inbound(conn, row["message_id"], delay_seconds=retry_delay(attempts, self._settings), give_up=give_up)
            if give_up:
                customer = store.lock_customer(conn, row["customer_id"])
                if customer is not None:
                    store.enqueue(conn, customer, text_message(_APOLOGY), f"in:{row['message_id']}:failed")
        log_event("whatsapp.inbound_failed", attempts=attempts, gave_up=give_up, kind=row["kind"])

    async def handle_next_inbound(self) -> bool:
        return await asyncio.to_thread(self._handle_inbound_now)

    def _claim_submission(self) -> Row | None:
        with self._db.transaction() as conn:
            return store.claim_submission(conn, lease_seconds=self._settings.whatsapp_extraction_lease_seconds)

    def _finish(self, finish: Callable[..., None], claimed: Row, result: object) -> None:
        with self._db.transaction() as conn:
            finish(conn, self._settings, claimed, result)

    async def process_next_submission(self) -> bool:
        claimed = await asyncio.to_thread(self._claim_submission)
        if claimed is None:
            return False
        if claimed["attempts"] > self._settings.whatsapp_extraction_max_attempts:
            # Workers keep stopping part-way through this one: don't pay to read it forever.
            await asyncio.to_thread(self._finish, finish_failure, claimed, Failure("too_many_attempts", TRY_AGAIN, False))
            return True
        images = claimed["images"]
        typing = asyncio.create_task(self._keep_typing(images[-1]["message_id"])) if images else None
        try:
            # The day the first screenshot arrived, in the customers' time zone: what the chat's "Today" means.
            received_on = claimed["first_image_at"].astimezone(ZoneInfo(self._settings.whatsapp_timezone)).date()
            response = await read_images(
                images, client=self._client, provider=self._provider, settings=self._settings, received_on=received_on
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = failure_from(exc)
            await asyncio.to_thread(self._finish, finish_failure, claimed, failure)
        else:
            await asyncio.to_thread(self._finish, finish_success, claimed, response)
        finally:
            if typing is not None:
                typing.cancel()
        return True

    async def _keep_typing(self, message_id: str) -> None:
        """Show the customer that ChatReceipt is working, without sending a message for it."""
        while True:
            await self._client.show_typing(message_id)
            await asyncio.sleep(_TYPING_EVERY_SECONDS)

    def _claim_instruction(self) -> Row | None:
        with self._db.transaction() as conn:
            return store.claim_instruction(conn, lease_seconds=self._settings.whatsapp_understanding_timeout_seconds + 30)

    async def _understand(self, claimed: Row) -> AICorrection | None:
        instruction = claimed["instruction"] or {}
        if not self._understands or int(instruction.get("attempts") or 0) > _INSTRUCTION_ATTEMPTS:
            return None
        draft = TransactionDraft.model_validate(claimed["draft"])
        review = claimed["review"] or {}
        try:
            return await asyncio.wait_for(
                understand(
                    self._provider,
                    draft,
                    checks=[message for _, message in open_reasons(review, claimed["edited_fields"])],
                    question=current_question(draft, str(instruction.get("target") or "")),
                    earlier=list(claimed.get("dialogue") or []),
                    message=str(instruction.get("text") or ""),
                ),
                timeout=self._settings.whatsapp_understanding_timeout_seconds,
            )
        except (TimeoutError, AIProviderError) as exc:
            log_event("whatsapp.understanding_failed", reason=getattr(getattr(exc, "kind", None), "value", type(exc).__name__))
            return None

    async def interpret_next_instruction(self) -> bool:
        """Read one typed correction with the AI, then apply it in its own transaction."""
        claimed = await asyncio.to_thread(self._claim_instruction)
        if claimed is None:
            return False
        message_id = str((claimed["instruction"] or {}).get("message_id") or "")
        if message_id:
            await self._client.show_typing(message_id)
        started = time.monotonic()
        correction = await self._understand(claimed)
        log_event(
            "whatsapp.correction_understood",
            understood=correction is not None,
            intent=correction.intent if correction else None,
            changes=len(correction.changes) if correction else 0,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        await asyncio.to_thread(self._finish, finish_instruction, claimed, correction)
        return True

    def _claim_outbound(self) -> Row | None:
        with self._db.transaction() as conn:
            return store.claim_outbound(conn, hold_seconds=_SEND_HOLD_SECONDS)

    def _after_send(self, row: Row, provider_id: str | None, error: WhatsAppAPIError | None) -> None:
        with self._db.transaction() as conn:
            if error is None:
                store.mark_outbound_sent(conn, row["id"], provider_id or "")
                return
            code = str(error.code or error.status or "network")
            if error.retryable and row["attempts"] < self._settings.whatsapp_send_max_attempts:
                store.schedule_outbound_retry(conn, row["id"], delay_seconds=retry_delay(row["attempts"], self._settings), error_code=code)
            else:
                store.mark_outbound_failed(conn, row["id"], code)
                reason = "customer_window_closed" if error.code == CUSTOMER_WINDOW_CLOSED else "send_failed"
                log_event("whatsapp.reply_undelivered", reason=reason, code=code, attempts=row["attempts"])

    def _record_for(self, row: Row) -> Row | None:
        with self._db.transaction() as conn:
            return store.get_transaction(conn, row["customer_ref"], UUID(row["payload"]["transaction_id"]))

    async def _outgoing(self, row: Row) -> dict[str, Any]:
        """The message to send. A receipt request becomes a rendered, uploaded file here."""
        payload: dict[str, Any] = row["payload"]
        if payload.get("type") != RECEIPT_REQUEST:
            return payload
        record = await asyncio.to_thread(self._record_for, row)
        if record is None:
            return text_message(RECORD_GONE)
        try:
            return await receipt_message(record, payload, client=self._client, renderer=self._renderer)
        except (DocumentRenderError, ValueError) as exc:
            log_event("whatsapp.receipt_render_failed", format=payload["format"], attempts=row["attempts"], reason=type(exc).__name__)
            if row["attempts"] < _RENDER_ATTEMPTS:
                raise WhatsAppAPIError("receipt_render_failed", retryable=True) from exc
            return text_message(RECEIPT_FAILED)

    async def send_next_outbound(self) -> bool:
        row = await asyncio.to_thread(self._claim_outbound)
        if row is None:
            return False
        try:
            message = await self._outgoing(row)
            provider_id = await self._client.send(message, to_phone=row["to_phone"], to_user_id=row["to_user_id"])
        except WhatsAppAPIError as exc:
            await asyncio.to_thread(self._after_send, row, None, exc)
        else:
            await asyncio.to_thread(self._after_send, row, provider_id, None)
        return True

    def _maintain(self) -> None:
        with self._db.transaction() as conn:
            store.purge_expired(conn, draft_ttl_hours=self._settings.whatsapp_draft_ttl_hours)

    async def run_maintenance(self) -> bool:
        now = time.monotonic()
        if self._last_maintenance and now - self._last_maintenance < _MAINTENANCE_SECONDS:
            return False
        self._last_maintenance = now
        await asyncio.to_thread(self._maintain)
        return False

    # -- running -------------------------------------------------------------------------------

    def notify(self) -> None:
        """New work was stored: wake the loops instead of waiting for the next poll."""
        for wake in self._wakes:
            wake.set()

    async def _loop(self, step: Callable[[], Awaitable[bool]]) -> None:
        wake = asyncio.Event()
        self._wakes.append(wake)
        pause = 1.0
        while True:
            try:
                worked = await step()
                pause = 1.0
            except DatabaseUnavailable:
                log_event("whatsapp.database_unavailable")
                await asyncio.sleep(pause)
                pause = min(pause * 2, 30.0)
                continue
            except Exception as exc:
                # Only the type: database errors can quote the values involved, such as a phone number.
                logger.error("WhatsApp worker step failed (%s)", type(exc).__name__)
                await asyncio.sleep(pause)
                pause = min(pause * 2, 30.0)
                continue
            if worked:
                self.notify()
                continue
            try:
                await asyncio.wait_for(wake.wait(), self._settings.whatsapp_poll_seconds)
            except TimeoutError:
                pass
            wake.clear()

    def start(self) -> None:
        steps: list[Callable[[], Awaitable[bool]]] = [
            self.handle_next_inbound,
            self.send_next_outbound,
            self.run_maintenance,
            self.interpret_next_instruction,
            *[self.process_next_submission] * max(1, self._settings.whatsapp_extraction_concurrency),
        ]
        self._tasks = [asyncio.create_task(self._loop(step)) for step in steps]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._wakes = []

    async def drain(self) -> None:
        """Do all work that is due now. Used by tests and the local simulator."""
        while True:
            worked = False
            while await self.handle_next_inbound():
                worked = True
            while await self.interpret_next_instruction():
                worked = True
            worked = await self.process_next_submission() or worked
            while await self.send_next_outbound():
                worked = True
            if not worked:
                return


async def _main() -> None:
    settings = get_settings()
    configure_logging()
    if not settings.whatsapp_configured or settings.database_url is None:
        raise SystemExit("WhatsApp isn't configured. See docs/whatsapp-integration.md.")
    database = Database(
        settings.database_url.get_secret_value(),
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        statement_timeout_ms=settings.database_statement_timeout_ms,
    )
    database.open()
    provider = create_provider(settings)
    client = WhatsAppClient(settings)
    renderer = BrowserRenderer(channel=settings.browser_channel, timeout_seconds=settings.document_timeout_seconds)
    worker = WhatsAppWorker(database, client, provider, settings, renderer)
    worker.start()
    try:
        await asyncio.Event().wait()
    finally:
        await worker.stop()
        await client.aclose()
        await provider.aclose()
        renderer.close()
        database.close()


if __name__ == "__main__":
    asyncio.run(_main())
