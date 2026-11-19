"""A durable job queue in PostgreSQL.

Why no Redis or Celery: every job's result is written to this same database. Marking a job complete
in the transaction that saves its result makes "the work was done but the worker crashed before
acknowledging it" impossible, which a separate broker can't offer. It is also one less service to run.

- A worker claims a job with ``FOR UPDATE SKIP LOCKED``, so several workers never take the same job.
- A claim is a lease. If the worker dies, the lease expires and another worker retries the job.
- Every claim gets a new token. A worker that lost its lease can't complete the job (fencing).
- The partial unique index on (kind, subject_id) allows one active job per subject.
"""

from __future__ import annotations

import random
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.config import Settings
from app.db.pool import Connection
from app.engine.failures import Failure

Row = dict[str, Any]

PROCESS_CONVERSATION = "process_conversation"
RENDER_RECEIPT = "render_receipt"
ACTIVE_STATUSES = ("queued", "running", "retrying")


class LeaseLost(Exception):
    """Another worker took this job over, so this worker's result must be discarded."""


def enqueue(conn: Connection, customer_id: UUID, kind: str, subject_id: UUID, *, max_attempts: int) -> Row:
    """Create a job, or return the one already active for this subject."""
    row = conn.execute(
        """
        INSERT INTO chatreceipt.processing_jobs (customer_id, kind, subject_id, max_attempts)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (kind, subject_id) WHERE status IN ('queued', 'running', 'retrying') DO NOTHING
        RETURNING *
        """,
        (customer_id, kind, subject_id, max_attempts),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT * FROM chatreceipt.processing_jobs WHERE kind = %s AND subject_id = %s AND status IN ('queued', 'running', 'retrying')",
        (kind, subject_id),
    ).fetchone()  # type: ignore[return-value]


def claim(conn: Connection, worker_id: str, *, lease_seconds: float) -> Row | None:
    """Take the next due job, including one whose worker stopped before finishing it."""
    return conn.execute(
        """
        UPDATE chatreceipt.processing_jobs j
        SET status = 'running', attempts = j.attempts + 1, lease_token = gen_random_uuid(),
            lease_expires_at = now() + make_interval(secs => %s), locked_by = %s,
            started_at = COALESCE(j.started_at, now()), updated_at = now()
        WHERE j.id = (
            SELECT id FROM chatreceipt.processing_jobs
            WHERE (status IN ('queued', 'retrying') AND run_after <= now())
               OR (status = 'running' AND lease_expires_at < now())
            ORDER BY run_after, created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING j.*
        """,
        (lease_seconds, worker_id),
    ).fetchone()


def renew_lease(conn: Connection, job: Row, *, lease_seconds: float) -> bool:
    updated = conn.execute(
        """
        UPDATE chatreceipt.processing_jobs SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
        WHERE id = %s AND lease_token = %s AND status = 'running'
        """,
        (lease_seconds, job["id"], job["lease_token"]),
    )
    return updated.rowcount == 1


def record_attempt(conn: Connection, job: Row, *, outcome: str, error_code: str | None, duration_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO chatreceipt.job_attempts (job_id, customer_id, attempt, worker, outcome, error_code, duration_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (job_id, attempt) DO NOTHING
        """,
        (job["id"], job["customer_id"], job["attempts"], job["locked_by"] or "", outcome, error_code, duration_ms),
    )


def complete(conn: Connection, job: Row, result: dict[str, Any], *, duration_ms: int = 0) -> None:
    """Mark the job done, in the same transaction as its result. Raises LeaseLost if it isn't ours."""
    updated = conn.execute(
        """
        UPDATE chatreceipt.processing_jobs
        SET status = 'completed', result = %s, error_code = NULL, error_message = NULL, lease_token = NULL,
            lease_expires_at = NULL, finished_at = now(), updated_at = now()
        WHERE id = %s AND lease_token = %s AND status = 'running'
        """,
        (Jsonb(result), job["id"], job["lease_token"]),
    )
    if updated.rowcount != 1:
        raise LeaseLost(str(job["id"]))
    record_attempt(conn, job, outcome="completed", error_code=None, duration_ms=duration_ms)


def retry_delay(attempt: int, settings: Settings) -> float:
    """Exponential backoff with jitter, so retries from many jobs don't arrive together."""
    delay = min(settings.engine_retry_max_seconds, settings.engine_retry_base_seconds * 2 ** max(attempt - 1, 0))
    return delay * random.uniform(0.8, 1.2)


def fail_attempt(conn: Connection, job: Row, failure: Failure, settings: Settings, *, duration_ms: int = 0) -> str:
    """Schedule a retry or give up. Returns the job's new status, or 'lease_lost'."""
    retry = failure.retryable and job["attempts"] < job["max_attempts"]
    status = "retrying" if retry else "failed"
    delay = retry_delay(job["attempts"], settings) if retry else 0
    updated = conn.execute(
        """
        UPDATE chatreceipt.processing_jobs
        SET status = %s, error_code = %s, error_message = %s, run_after = now() + make_interval(secs => %s),
            lease_token = NULL, lease_expires_at = NULL, updated_at = now(),
            finished_at = CASE WHEN %s = 'failed' THEN now() ELSE NULL END
        WHERE id = %s AND lease_token = %s AND status = 'running'
        """,
        (status, failure.code, failure.message, delay, status, job["id"], job["lease_token"]),
    )
    if updated.rowcount != 1:
        return "lease_lost"
    record_attempt(conn, job, outcome=status, error_code=failure.code, duration_ms=duration_ms)
    return status


def cancel(conn: Connection, customer_id: UUID, job_id: UUID) -> Row | None:
    """Cancel a job that hasn't started. A running job finishes its current attempt."""
    return conn.execute(
        """
        UPDATE chatreceipt.processing_jobs
        SET status = 'cancelled', finished_at = now(), updated_at = now(), lease_token = NULL, lease_expires_at = NULL
        WHERE id = %s AND customer_id = %s AND status IN ('queued', 'retrying')
        RETURNING *
        """,
        (job_id, customer_id),
    ).fetchone()


def get(conn: Connection, customer_id: UUID, job_id: UUID) -> Row | None:
    return conn.execute(
        "SELECT * FROM chatreceipt.processing_jobs WHERE id = %s AND customer_id = %s", (job_id, customer_id)
    ).fetchone()


def beat(conn: Connection, worker_id: str) -> None:
    conn.execute(
        """
        INSERT INTO chatreceipt.worker_heartbeats (worker_id) VALUES (%s)
        ON CONFLICT (worker_id) DO UPDATE SET last_seen_at = now()
        """,
        (worker_id,),
    )
