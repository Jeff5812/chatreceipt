"""Saved records on the website, for customers who signed in with a link sent over WhatsApp.

The link carries a one-time code. Exchanging it starts a short session; the session's token decides
whose records are returned, and nothing the browser sends can name another customer.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.api.deps import rate_limit
from app.calculations import to_plain
from app.db.pool import Connection, Database, DatabaseUnavailable
from app.errors import APIError
from app.models import Calculation, PaymentStatus, TransactionDraft
from app.observability import log_event
from app.whatsapp import store

router = APIRouter()

T = TypeVar("T")


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class SessionResponse(BaseModel):
    token: str
    expires_at: datetime


class RecordSummary(BaseModel):
    id: UUID
    reference: str
    source: str
    currency: str
    seller: str
    buyer: str
    transaction_date: date | None
    total: str
    amount_paid: str | None
    outstanding: str | None
    payment_status: PaymentStatus
    created_at: datetime
    # The side the customer was on, when it was known: "seller", "buyer", or None.
    owner_role: str | None = None


class RecordList(BaseModel):
    records: list[RecordSummary]
    next_before: str | None


class RecordDetail(RecordSummary):
    draft: TransactionDraft
    calculation: Calculation


def _database(request: Request) -> Database:
    database: Database | None = request.app.state.database
    if database is None:
        raise APIError(503, "records_unavailable", "Saved records aren't available right now.")
    return database


async def _run(database: Database, work: Callable[[Connection], T]) -> T:
    def in_transaction() -> T:
        with database.transaction() as conn:
            return work(conn)

    try:
        return await run_in_threadpool(in_transaction)
    except DatabaseUnavailable as exc:
        raise APIError(503, "records_unavailable", "Saved records aren't available right now. Please try again.") from exc


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise APIError(401, "session_required", "Open your records with the link from WhatsApp.")
    return token.strip()


async def _customer(request: Request) -> UUID:
    token = _bearer(request)
    customer_id = await _run(_database(request), lambda conn: store.session_customer(conn, token))
    if customer_id is None:
        raise APIError(401, "session_expired", "This session has ended. Send “web” to ChatReceipt on WhatsApp for a new link.")
    return customer_id


CurrentCustomer = Annotated[UUID, Depends(_customer)]


def _text(value: Decimal | None) -> str | None:
    return to_plain(value) if value is not None else None


def _summary(row: store.Row) -> RecordSummary:
    return RecordSummary(
        id=row["id"],
        reference=row["reference"],
        source=row["source"],
        currency=row["currency"],
        seller=row["seller"],
        buyer=row["buyer"],
        transaction_date=row["transaction_date"],
        total=to_plain(row["total"]),
        amount_paid=_text(row["amount_paid"]),
        outstanding=_text(row["outstanding"]),
        payment_status=row["payment_status"],
        created_at=row["created_at"],
        owner_role=row.get("owner_role"),
    )


def _cursor(row: store.Row) -> str:
    raw = f"{row['created_at'].isoformat()}|{row['id']}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _read_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        created_at, record_id = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8").split("|")
        return datetime.fromisoformat(created_at), UUID(record_id)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise APIError(400, "invalid_cursor", "That page of records couldn't be found.") from exc


@router.post(
    "/api/records/session",
    response_model=SessionResponse,
    dependencies=[Depends(rate_limit("records_session"))],
)
async def start_session(request: Request, body: SessionRequest) -> SessionResponse:
    database = _database(request)
    hours = request.app.state.settings.records_session_ttl_hours
    session = await _run(database, lambda conn: store.exchange_sign_in_code(conn, body.code, session_ttl_hours=hours))
    if session is None:
        log_event("records.sign_in_refused")
        raise APIError(401, "link_expired", "This link has expired or was already used. Send “web” to ChatReceipt on WhatsApp for a new one.")
    return SessionResponse(token=session[0], expires_at=session[1])


@router.post("/api/records/session/end", status_code=204)
async def end_session(request: Request) -> None:
    token = _bearer(request)
    await _run(_database(request), lambda conn: store.revoke_session(conn, token))


@router.get("/api/records", response_model=RecordList)
async def list_records(
    request: Request,
    customer_id: CurrentCustomer,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    before: Annotated[str | None, Query(max_length=200)] = None,
) -> RecordList:
    position = _read_cursor(before) if before else None
    rows = await _run(
        _database(request),
        lambda conn: store.recent_transactions(conn, customer_id, limit=limit + 1, before=position),
    )
    page = rows[:limit]
    return RecordList(records=[_summary(row) for row in page], next_before=_cursor(page[-1]) if len(rows) > limit else None)


@router.get("/api/records/{record_id}", response_model=RecordDetail)
async def get_record(request: Request, record_id: str, customer_id: CurrentCustomer) -> RecordDetail:
    try:
        parsed = UUID(record_id)
    except ValueError as exc:
        raise APIError(404, "not_found", "That record doesn't exist.") from exc
    row = await _run(_database(request), lambda conn: store.get_transaction(conn, customer_id, parsed))
    if row is None:
        raise APIError(404, "not_found", "That record doesn't exist.")
    return RecordDetail(
        **_summary(row).model_dump(),
        draft=TransactionDraft.model_validate(row["draft"]),
        calculation=Calculation.model_validate(row["calculation"]),
    )
