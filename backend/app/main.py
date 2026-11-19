"""FastAPI application factory. Run with: uvicorn app.main:app --reload --port 8000"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.api import documents, extractions, meta, records, transactions, whatsapp
from app.api.v1 import intake as v1_intake
from app.api.v1 import records as v1_records
from app.config import Settings, get_settings
from app.db.pool import Database
from app.documents.browser import BrowserRenderer
from app.engine.worker import EngineWorker
from app.errors import install_error_handlers
from app.extraction.provider import ExtractionProvider
from app.extraction.providers import create_provider
from app.middleware import GuardMiddleware, RequestContextMiddleware
from app.observability import configure_logging, log_event
from app.rate_limit import RateLimiter
from app.storage import LocalStorage, ObjectStorage
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.worker import WhatsAppWorker


def create_app(
    *,
    settings: Settings | None = None,
    provider: ExtractionProvider | None = None,
    renderer: BrowserRenderer | None = None,
    database: Database | None = None,
    whatsapp_client: WhatsAppClient | None = None,
    storage: ObjectStorage | None = None,
    webhook_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.database is not None:
            await run_in_threadpool(app.state.database.open)
        if app.state.whatsapp_worker is not None and settings.whatsapp_worker_in_process:
            app.state.whatsapp_worker.start()
        if app.state.engine_worker is not None and settings.engine_worker_in_process:
            app.state.engine_worker.start()
        yield
        if app.state.engine_worker is not None:
            await app.state.engine_worker.stop()
        if app.state.whatsapp_worker is not None:
            await app.state.whatsapp_worker.stop()
        if app.state.whatsapp_client is not None:
            await app.state.whatsapp_client.aclose()
        if app.state.database is not None:
            await run_in_threadpool(app.state.database.close)
        await app.state.provider.aclose()
        await run_in_threadpool(app.state.renderer.close)

    app = FastAPI(
        title="ChatReceipt API",
        version="1.0.0",
        description="Turn conversations into reviewed, auditable transaction records.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.provider = provider or create_provider(settings)
    log_event("extraction.provider", provider=app.state.provider.name, configured=app.state.provider.configured)
    app.state.renderer = renderer or BrowserRenderer(
        channel=settings.browser_channel, timeout_seconds=settings.document_timeout_seconds
    )
    app.state.limiters = {
        "extraction": RateLimiter(settings.extraction_rate_limit, settings.extraction_rate_window_seconds),
        "documents": RateLimiter(settings.document_rate_limit, settings.document_rate_window_seconds),
        "calculate": RateLimiter(settings.calculate_rate_limit, settings.calculate_rate_window_seconds),
        "records_session": RateLimiter(settings.records_session_rate_limit, settings.records_session_rate_window_seconds),
        "v1_processing": RateLimiter(settings.v1_processing_rate_limit, settings.v1_processing_rate_window_seconds),
        "v1_uploads": RateLimiter(settings.v1_upload_rate_limit, settings.v1_upload_rate_window_seconds),
        "v1_writes": RateLimiter(settings.v1_write_rate_limit, settings.v1_write_rate_window_seconds),
    }

    # Saved records, WhatsApp and the v1 API are optional: without a database the website works as before.
    if database is None and settings.database_url is not None and settings.database_configured:
        database = Database(
            settings.database_url.get_secret_value(),
            min_size=settings.database_pool_min_size,
            max_size=settings.database_pool_max_size,
            statement_timeout_ms=settings.database_statement_timeout_ms,
        )
    app.state.database = database
    app.state.storage = storage or LocalStorage(settings.storage_dir)
    app.state.whatsapp_client = None
    app.state.whatsapp_worker = None
    app.state.engine_worker = None
    if settings.whatsapp_configured and database is not None:
        app.state.whatsapp_client = whatsapp_client or WhatsAppClient(settings)
        app.state.whatsapp_worker = WhatsAppWorker(
            database, app.state.whatsapp_client, app.state.provider, settings, app.state.renderer
        )
    if database is not None:
        app.state.engine_worker = EngineWorker(
            database, app.state.storage, app.state.provider, app.state.renderer, settings, webhook_transport=webhook_transport
        )

    install_error_handlers(app)
    for module in (meta, extractions, transactions, documents, whatsapp, records, v1_intake, v1_records):
        app.include_router(module.router)

    # Order matters: the last middleware added runs first. CORS wraps the guard so that its
    # 413/500 responses still carry CORS headers.
    app.add_middleware(GuardMiddleware, max_body_bytes=settings.max_request_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["Content-Disposition", "X-Request-ID", "Idempotency-Replayed"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)
    return app


app = create_app()
