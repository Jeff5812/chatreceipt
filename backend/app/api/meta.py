"""Health check and the public configuration the frontend needs."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from app.currencies import CURRENCIES, DEFAULT_CURRENCY
from app.db.migrate import MIGRATIONS_DIR
from app.db.pool import Database, DatabaseUnavailable
from app.errors import APIError
from app.models import ConfigResponse, CurrencyInfo, UploadLimits
from app.observability import metrics
from app.uploads import ACCEPTED_FORMATS

router = APIRouter()

_WORKER_STALE_SECONDS = 60


@router.get("/api/health")
async def health() -> dict[str, str]:
    """Whether this process is up."""
    return {"status": "ok"}


def _readiness(database: Database) -> dict[str, Any]:
    with database.transaction() as conn:
        applied = conn.execute("SELECT count(*) AS n FROM chatreceipt.schema_migrations").fetchone()
        workers = conn.execute(
            "SELECT count(*) AS n FROM chatreceipt.worker_heartbeats WHERE last_seen_at > now() - make_interval(secs => %s)",
            (_WORKER_STALE_SECONDS,),
        ).fetchone()
        queue = conn.execute(
            """
            SELECT count(*) AS n, COALESCE(EXTRACT(EPOCH FROM now() - min(run_after)), 0) AS oldest
            FROM chatreceipt.processing_jobs WHERE status IN ('queued', 'retrying') AND run_after <= now()
            """
        ).fetchone()
    expected = len(list(MIGRATIONS_DIR.glob("*.sql")))
    return {
        "database": "ok",
        "migrations": "ok" if applied["n"] >= expected else "pending",  # type: ignore[index]
        "workers_active": workers["n"],  # type: ignore[index]
        "jobs_waiting": queue["n"],  # type: ignore[index]
        "oldest_job_waiting_seconds": round(float(queue["oldest"])),  # type: ignore[index]
    }


@router.get("/api/health/ready")
async def ready(request: Request) -> JSONResponse:
    """Whether this instance can do its work: the database, the schema and the background workers."""
    database: Database | None = request.app.state.database
    if database is None:
        return JSONResponse({"status": "ok", "database": "not_configured"})
    try:
        details = await run_in_threadpool(_readiness, database)
    except DatabaseUnavailable:
        return JSONResponse({"status": "unavailable", "database": "unreachable"}, status_code=503)
    healthy = details["migrations"] == "ok"
    return JSONResponse({"status": "ok" if healthy else "unavailable", **details}, status_code=200 if healthy else 503)


@router.get("/api/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def prometheus_metrics(request: Request) -> PlainTextResponse:
    """Operational counters in the Prometheus format, for whoever holds METRICS_TOKEN."""
    expected = request.app.state.settings.metrics_token
    if expected is None or not expected.get_secret_value():
        raise APIError(404, "not_found", "That page or resource doesn't exist.")
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.get_secret_value().encode("utf-8")):
        raise APIError(401, "authentication_required", "A valid token is required.", headers={"WWW-Authenticate": "Bearer"})
    return PlainTextResponse(metrics.render())


@router.get("/api/config", response_model=ConfigResponse)
async def public_config(request: Request) -> ConfigResponse:
    settings = request.app.state.settings
    return ConfigResponse(
        currencies=[
            CurrencyInfo(code=c.code, name=c.name, symbol=c.symbol, minor_units=c.minor_units)
            for c in CURRENCIES.values()
        ],
        default_currency=DEFAULT_CURRENCY,
        extraction_available=request.app.state.provider.configured,
        limits=UploadLimits(
            max_files=settings.max_files,
            max_file_bytes=settings.max_file_bytes,
            max_total_bytes=settings.max_total_bytes,
            accepted_types=sorted(ACCEPTED_FORMATS.values()),
            max_chat_export_bytes=settings.max_chat_export_bytes,
        ),
    )
