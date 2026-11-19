"""The background loops behind the engine. Safe to run in several processes at once.

Usually started inside the API process. To run it separately: ``python -m app.engine.worker``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.db.pool import Database, DatabaseUnavailable
from app.documents.browser import BrowserRenderer
from app.engine import jobs, outbox, processing, receipts
from app.engine.failures import Failure, classify
from app.engine.jobs import LeaseLost
from app.extraction.provider import StructuredModel
from app.extraction.providers import create_provider
from app.observability import configure_logging, log_context, log_event, metrics
from app.storage import LocalStorage, ObjectStorage, customer_prefix

logger = logging.getLogger(__name__)

Row = dict[str, Any]

_HEARTBEAT_SECONDS = 15.0
_MAINTENANCE_SECONDS = 3600.0


class EngineWorker:
    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        model: StructuredModel,
        renderer: BrowserRenderer,
        settings: Settings,
        *,
        webhook_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._db = database
        self._storage = storage
        self._renderer = renderer
        self._settings = settings
        self._services = processing.Services(database, storage, model, settings)
        self._webhook_transport = webhook_transport
        self._http: httpx.AsyncClient | None = None
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._tasks: list[asyncio.Task[None]] = []
        self._wakes: list[asyncio.Event] = []
        self._last_beat = 0.0
        self._last_maintenance = 0.0

    # -- jobs ------------------------------------------------------------------------------------

    def _claim(self) -> Row | None:
        with self._db.transaction() as conn:
            return jobs.claim(conn, self.worker_id, lease_seconds=self._settings.engine_lease_seconds)

    def _renew(self, job: Row) -> bool:
        with self._db.transaction() as conn:
            return jobs.renew_lease(conn, job, lease_seconds=self._settings.engine_lease_seconds)

    async def _keep_lease(self, job: Row) -> None:
        interval = max(self._settings.engine_lease_seconds / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            try:
                if not await asyncio.to_thread(self._renew, job):
                    return
            except DatabaseUnavailable:
                continue

    async def _execute(self, job: Row) -> None:
        if job["kind"] == jobs.PROCESS_CONVERSATION:
            await processing.process_conversation(self._services, job)
        elif job["kind"] == jobs.RENDER_RECEIPT:
            await receipts.render(self._db, self._storage, self._renderer, self._settings, job)
        else:
            raise ValueError(f"Unknown job kind: {job['kind']}")

    def _record_failure(self, job: Row, failure: Failure, duration_ms: int) -> str:
        with self._db.tenant(job["customer_id"]) as conn:
            status = jobs.fail_attempt(conn, job, failure, self._settings, duration_ms=duration_ms)
            if status == "failed":
                if job["kind"] == jobs.PROCESS_CONVERSATION:
                    processing.on_permanent_failure(conn, self._settings, job, failure)
                else:
                    receipts.mark_render_failed(conn, job)
        return status

    async def run_next_job(self) -> bool:
        job = await asyncio.to_thread(self._claim)
        if job is None:
            return False
        started = time.perf_counter()
        with log_context(job_id=job["id"], job_kind=job["kind"], attempt=job["attempts"], customer=job["customer_id"]):
            if job["attempts"] > job["max_attempts"]:
                failure = Failure("too_many_attempts", "This couldn't be finished after several attempts.", False)
                await asyncio.to_thread(self._record_failure, job, failure, 0)
                return True
            keeper = asyncio.create_task(self._keep_lease(job))
            try:
                await self._execute(job)
            except asyncio.CancelledError:
                raise
            except LeaseLost:
                log_event("job.lease_lost")
                metrics.inc("chatreceipt_jobs_total", kind=job["kind"], outcome="lease_lost")
            except Exception as exc:
                failure = classify(exc)
                duration_ms = round((time.perf_counter() - started) * 1000)
                status = await asyncio.to_thread(self._record_failure, job, failure, duration_ms)
                metrics.inc("chatreceipt_jobs_total", kind=job["kind"], outcome=status)
                log_event("job.failed", code=failure.code, status=status, retryable=failure.retryable, duration_ms=duration_ms)
            else:
                duration = time.perf_counter() - started
                metrics.inc("chatreceipt_jobs_total", kind=job["kind"], outcome="completed")
                metrics.observe("chatreceipt_job_seconds", duration, kind=job["kind"])
                log_event("job.completed", duration_ms=round(duration * 1000))
            finally:
                keeper.cancel()
        return True

    # -- integration events ------------------------------------------------------------------------

    def _claim_event(self) -> Row | None:
        with self._db.transaction() as conn:
            return outbox.claim(conn)

    def _after_delivery(self, event: Row, error: str | None) -> None:
        with self._db.transaction() as conn:
            if error is None:
                outbox.mark_delivered(conn, event["id"])
            else:
                outbox.mark_undelivered(conn, event, self._settings, error, delay_seconds=jobs.retry_delay(event["attempts"], self._settings))

    async def deliver_next_event(self) -> bool:
        if not self._settings.webhooks_configured:
            return False
        event = await asyncio.to_thread(self._claim_event)
        if event is None:
            return False
        if self._http is None:
            self._http = httpx.AsyncClient(transport=self._webhook_transport, follow_redirects=False)
        error = await outbox.deliver(self._http, self._settings, event)
        await asyncio.to_thread(self._after_delivery, event, error)
        metrics.inc("chatreceipt_webhook_deliveries_total", outcome="delivered" if error is None else "retried")
        return True

    # -- housekeeping --------------------------------------------------------------------------------

    def _beat(self) -> None:
        with self._db.transaction() as conn:
            jobs.beat(conn, self.worker_id)

    def _expired_uploads(self) -> list[Row]:
        with self._db.transaction() as conn:
            return conn.execute(
                """
                UPDATE chatreceipt.uploads u SET status = 'deleted', storage_key = NULL, deleted_at = now()
                FROM (
                    SELECT id, storage_key FROM chatreceipt.uploads
                    WHERE status = 'stored' AND expires_at < now()
                    LIMIT 200
                    FOR UPDATE SKIP LOCKED
                ) old
                WHERE u.id = old.id
                RETURNING old.storage_key
                """
            ).fetchall()

    def maintain(self) -> None:
        """Delete expired uploads, files of erased accounts and old queue entries."""
        for row in self._expired_uploads():
            self._storage.delete(row["storage_key"])
        with self._db.transaction() as conn:
            erasures = conn.execute("SELECT customer_ref FROM chatreceipt.storage_erasures LIMIT 100").fetchall()
        for erasure in erasures:
            self._storage.delete_prefix(customer_prefix(erasure["customer_ref"]))
            with self._db.transaction() as conn:
                conn.execute("DELETE FROM chatreceipt.storage_erasures WHERE customer_ref = %s", (erasure["customer_ref"],))
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM chatreceipt.idempotency_keys WHERE created_at < now() - interval '7 days'")
            conn.execute("DELETE FROM chatreceipt.outbox_events WHERE status <> 'pending' AND created_at < now() - interval '90 days'")
            conn.execute("DELETE FROM chatreceipt.worker_heartbeats WHERE last_seen_at < now() - interval '1 day'")

    async def housekeeping(self) -> bool:
        now = time.monotonic()
        if now - self._last_beat >= _HEARTBEAT_SECONDS or not self._last_beat:
            self._last_beat = now
            await asyncio.to_thread(self._beat)
        if now - self._last_maintenance >= _MAINTENANCE_SECONDS or not self._last_maintenance:
            self._last_maintenance = now
            await asyncio.to_thread(self.maintain)
        return False

    # -- running ---------------------------------------------------------------------------------------

    def notify(self) -> None:
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
                log_event("engine.database_unavailable")
                await asyncio.sleep(pause)
                pause = min(pause * 2, 30.0)
                continue
            except Exception as exc:
                # Only the type: database errors can quote the values involved.
                logger.error("Engine worker step failed (%s)", type(exc).__name__)
                await asyncio.sleep(pause)
                pause = min(pause * 2, 30.0)
                continue
            if worked:
                continue
            try:
                await asyncio.wait_for(wake.wait(), self._settings.engine_poll_seconds)
            except TimeoutError:
                pass
            wake.clear()

    def start(self) -> None:
        steps: list[Callable[[], Awaitable[bool]]] = [
            self.housekeeping,
            self.deliver_next_event,
            *[self.run_next_job] * max(1, self._settings.engine_job_concurrency),
        ]
        self._tasks = [asyncio.create_task(self._loop(step)) for step in steps]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._wakes = []
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def drain(self) -> None:
        """Do all work that is due now. Used by tests and the demonstration."""
        while True:
            worked = await self.run_next_job()
            worked = await self.deliver_next_event() or worked
            if not worked:
                return


async def _main() -> None:
    settings = get_settings()
    configure_logging()
    if settings.database_url is None or not settings.database_configured:
        raise SystemExit("DATABASE_URL isn't set.")
    database = Database(
        settings.database_url.get_secret_value(),
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        statement_timeout_ms=settings.database_statement_timeout_ms,
    )
    database.open()
    model = create_provider(settings)
    renderer = BrowserRenderer(channel=settings.browser_channel, timeout_seconds=settings.document_timeout_seconds)
    worker = EngineWorker(database, LocalStorage(settings.storage_dir), model, renderer, settings)  # type: ignore[arg-type]
    worker.start()
    try:
        await asyncio.Event().wait()
    finally:
        await worker.stop()
        await model.aclose()
        renderer.close()
        database.close()


if __name__ == "__main__":
    asyncio.run(_main())
