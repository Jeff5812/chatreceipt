"""What every v1 route shares: the signed-in customer, per-customer limits and safe writes."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from typing import Annotated, Any, TypeVar
from uuid import UUID

from fastapi import Depends, Request, Security
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from app.db.pool import Connection, Database, DatabaseUnavailable
from app.errors import APIError
from app.whatsapp import store

T = TypeVar("T")

_bearer = HTTPBearer(auto_error=False, description="A session token.")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


def database_of(request: Request) -> Database:
    database: Database | None = request.app.state.database
    if database is None:
        raise APIError(503, "storage_unavailable", "Saved transactions aren't available right now.")
    return database


async def run_system(request: Request, work: Callable[[Connection], T]) -> T:
    database = database_of(request)

    def in_transaction() -> T:
        with database.transaction() as conn:
            return work(conn)

    try:
        return await run_in_threadpool(in_transaction)
    except DatabaseUnavailable as exc:
        raise APIError(503, "temporarily_unavailable", "ChatReceipt is temporarily unavailable. Please try again.") from exc


async def run_tenant(request: Request, customer_id: UUID, work: Callable[[Connection], T]) -> T:
    """Run ``work`` in a transaction restricted by the database to this customer's rows."""
    database = database_of(request)

    def in_transaction() -> T:
        with database.tenant(customer_id) as conn:
            return work(conn)

    try:
        return await run_in_threadpool(in_transaction)
    except DatabaseUnavailable as exc:
        raise APIError(503, "temporarily_unavailable", "ChatReceipt is temporarily unavailable. Please try again.") from exc


async def current_customer(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)]
) -> UUID:
    unauthenticated = {"WWW-Authenticate": "Bearer"}
    if credentials is None or not credentials.credentials:
        raise APIError(401, "authentication_required", "Sign in to use this.", headers=unauthenticated)
    token = credentials.credentials
    customer_id = await run_system(request, lambda conn: store.session_customer(conn, token))
    if customer_id is None:
        raise APIError(401, "session_expired", "This session has ended. Sign in again.", headers=unauthenticated)
    return customer_id


CustomerId = Annotated[UUID, Depends(current_customer)]


def limit(name: str) -> Callable[..., Any]:
    """Limit expensive routes per customer rather than per IP address."""

    async def dependency(request: Request, customer_id: CustomerId) -> None:
        retry_after = request.app.state.limiters[name].hit(str(customer_id))
        if retry_after is not None:
            raise APIError(
                429,
                "rate_limited",
                "Too many requests. Please wait a few minutes and try again.",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )

    return dependency


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(json.dumps(jsonable_encoder(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


async def write(
    request: Request,
    customer_id: UUID,
    operation: str,
    payload: Any,
    work: Callable[[Connection], Any],
    *,
    status_code: int = 200,
) -> JSONResponse:
    """Run a change in one transaction. With an Idempotency-Key, a retry gets the first answer back.

    The key is stored in the same transaction as the change, so a request either happened and is
    remembered, or didn't happen and can safely be sent again. Requests that fail aren't remembered.
    """
    key = request.headers.get("idempotency-key")
    if key is not None and not _IDEMPOTENCY_KEY.match(key):
        raise APIError(400, "invalid_idempotency_key", "The Idempotency-Key must be 8 to 128 letters, digits or _ . : - characters.")
    fingerprint = _fingerprint({"operation": operation, "payload": payload})

    def in_transaction(conn: Connection) -> tuple[int, Any, bool]:
        if key is not None:
            inserted = conn.execute(
                """
                INSERT INTO chatreceipt.idempotency_keys (customer_id, key, operation, request_sha256) VALUES (%s, %s, %s, %s)
                ON CONFLICT (customer_id, key) DO NOTHING RETURNING key
                """,
                (customer_id, key, operation, fingerprint),
            ).fetchone()
            if inserted is None:
                stored = conn.execute(
                    "SELECT * FROM chatreceipt.idempotency_keys WHERE customer_id = %s AND key = %s", (customer_id, key)
                ).fetchone()
                if stored is None or stored["request_sha256"] != fingerprint:
                    raise APIError(422, "idempotency_key_reused", "This Idempotency-Key was already used for a different request.")
                return stored["response_status"], stored["response_body"], True
        body = jsonable_encoder(work(conn))
        if key is not None:
            conn.execute(
                "UPDATE chatreceipt.idempotency_keys SET response_status = %s, response_body = %s WHERE customer_id = %s AND key = %s",
                (status_code, Jsonb(body), customer_id, key),
            )
        return status_code, body, False

    status, body, replayed = await run_tenant(request, customer_id, in_transaction)
    worker = getattr(request.app.state, "engine_worker", None)
    if worker is not None:
        worker.notify()
    return JSONResponse(body, status_code=status, headers={"Idempotency-Replayed": "true"} if replayed else None)
