"""Reviewing transactions and issuing receipts."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.api.v1.deps import CustomerId, limit, run_system, run_tenant, write
from app.api.v1.presenters import job_view, receipt_view, transaction_detail, transaction_summary
from app.api.v1.schemas import (
    EraseRequest,
    HistoryView,
    JobView,
    ManualTransactionRequest,
    ReasonRequest,
    ReceiptList,
    ReceiptView,
    ReviewRequest,
    TransactionDetail,
    TransactionList,
    VersionedRequest,
    VoidRequest,
)
from app.db.pool import Connection
from app.engine import jobs, receipts, transactions
from app.errors import APIError
from app.models import PaymentStatus
from app.observability import log_event
from app.records.lifecycle import State
from app.storage import StorageError
from app.whatsapp import store as customer_store

router = APIRouter(prefix="/api/v1", tags=["Transactions"], dependencies=[Depends(limit("v1_writes"))])

Sort = Literal["-created_at", "created_at", "-total", "total", "-transaction_date", "transaction_date"]
# Sort keys come from this fixed list, never from the request text.
_SORTS: dict[str, tuple[str, str]] = {
    "created_at": ("t.created_at", "timestamptz"),
    "total": ("COALESCE(t.total, -1)", "numeric"),
    "transaction_date": ("COALESCE(t.transaction_date, DATE '0001-01-01')", "date"),
}


def _encode_cursor(value: Any, row_id: UUID) -> str:
    raw = json.dumps({"v": str(value), "id": str(row_id)}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str, kind: str) -> tuple[Any, UUID]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        row_id = UUID(data["id"])
        raw = data["v"]
        value: Any = datetime.fromisoformat(raw) if kind == "timestamptz" else date.fromisoformat(raw) if kind == "date" else Decimal(raw)
        return value, row_id
    except (ValueError, KeyError, TypeError, UnicodeError, InvalidOperation) as exc:
        raise APIError(400, "invalid_cursor", "That page of results couldn't be found.") from exc


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/transactions", response_model=TransactionList)
async def list_transactions(
    request: Request,
    customer_id: CustomerId,
    state: Annotated[list[State] | None, Query()] = None,
    payment_status: PaymentStatus | None = None,
    q: Annotated[str | None, Query(max_length=100, description="Search seller, buyer, summary and items.")] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort: Sort = "-created_at",
    limit_to: Annotated[int, Query(alias="limit", ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
) -> TransactionList:
    descending = sort.startswith("-")
    expression, kind = _SORTS[sort.lstrip("-")]
    conditions = ["t.customer_id = %s"]
    parameters: list[Any] = [customer_id]
    if state:
        conditions.append("t.state = ANY(%s)")
        parameters.append([entry.value for entry in state])
    if payment_status:
        conditions.append("t.payment_status = %s")
        parameters.append(payment_status.value)
    if date_from:
        conditions.append("t.transaction_date >= %s")
        parameters.append(date_from)
    if date_to:
        conditions.append("t.transaction_date <= %s")
        parameters.append(date_to)
    if q and q.strip():
        pattern = _like(q.strip())
        conditions.append(
            """(t.seller ILIKE %s OR t.buyer ILIKE %s OR t.summary ILIKE %s OR EXISTS (
                   SELECT 1 FROM chatreceipt.transaction_items i WHERE i.transaction_id = t.id AND i.description ILIKE %s))"""
        )
        parameters += [pattern, pattern, pattern, pattern]
    if cursor:
        value, row_id = _decode_cursor(cursor, kind)
        conditions.append(f"({expression}, t.id) {'<' if descending else '>'} (%s::{kind}, %s)")
        parameters += [value, row_id]
    direction = "DESC" if descending else "ASC"
    sql = f"""
        SELECT t.*, {expression} AS sort_value FROM chatreceipt.extracted_transactions t
        WHERE {' AND '.join(conditions)}
        ORDER BY {expression} {direction}, t.id {direction}
        LIMIT %s
    """  # noqa: S608

    rows = await run_tenant(request, customer_id, lambda conn: conn.execute(sql, [*parameters, limit_to + 1]).fetchall())
    page = rows[:limit_to]
    next_cursor = _encode_cursor(page[-1]["sort_value"], page[-1]["id"]) if len(rows) > limit_to else None
    return TransactionList(transactions=[transaction_summary(row) for row in page], next_cursor=next_cursor)


@router.post("/transactions", response_model=TransactionDetail, status_code=201)
async def create_transaction(request: Request, body: ManualTransactionRequest, customer_id: CustomerId) -> Any:
    """Enter a transaction by hand, without a conversation."""
    settings = request.app.state.settings

    def create(conn: Connection) -> TransactionDetail:
        row = transactions.create_manual(conn, settings, customer_id, body.draft)
        return transaction_detail(conn, settings, row)

    return await write(request, customer_id, "create_transaction", body.model_dump(mode="json"), create, status_code=201)


@router.get("/transactions/{transaction_id}", response_model=TransactionDetail)
async def get_transaction(request: Request, transaction_id: UUID, customer_id: CustomerId) -> TransactionDetail:
    settings = request.app.state.settings
    return await run_tenant(
        request, customer_id, lambda conn: transaction_detail(conn, settings, transactions.load(conn, customer_id, transaction_id))
    )


@router.get("/transactions/{transaction_id}/history", response_model=HistoryView)
async def get_history(request: Request, transaction_id: UUID, customer_id: CustomerId) -> HistoryView:
    """Everything that happened to this transaction: events, corrections and versions."""

    def load(conn: Connection) -> HistoryView:
        transactions.load(conn, customer_id, transaction_id)
        events = conn.execute(
            """
            SELECT event_type, from_state, to_state, version, actor, data, created_at
            FROM chatreceipt.transaction_events WHERE transaction_id = %s ORDER BY id
            """,
            (transaction_id,),
        ).fetchall()
        decisions = conn.execute(
            """
            SELECT operation, field_path, extracted_value, previous_value, new_value, reason, actor_id, from_version, to_version,
                   extraction_run_id, created_at
            FROM chatreceipt.review_decisions WHERE transaction_id = %s ORDER BY id
            """,
            (transaction_id,),
        ).fetchall()
        versions = conn.execute(
            "SELECT version, state, reason, snapshot_sha256, created_at FROM chatreceipt.transaction_versions WHERE transaction_id = %s ORDER BY version",
            (transaction_id,),
        ).fetchall()
        return HistoryView(events=events, decisions=decisions, versions=versions)

    return await run_tenant(request, customer_id, load)


@router.get("/transactions/{transaction_id}/versions/{version}")
async def get_version(request: Request, transaction_id: UUID, version: int, customer_id: CustomerId) -> dict[str, Any]:
    """A past version, exactly as it stood."""

    def load(conn: Connection) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM chatreceipt.transaction_versions WHERE transaction_id = %s AND version = %s AND customer_id = %s",
            (transaction_id, version, customer_id),
        ).fetchone()
        if row is None:
            raise APIError(404, "not_found", "That version doesn't exist.")
        return {"version": row["version"], "state": row["state"], "reason": row["reason"], "snapshot_sha256": row["snapshot_sha256"], "created_at": row["created_at"], "snapshot": row["snapshot"]}

    return await run_tenant(request, customer_id, load)


def _detail_after(settings: Any, change: Any) -> Any:
    def run(conn: Connection) -> TransactionDetail:
        return transaction_detail(conn, settings, change(conn))

    return run


@router.post("/transactions/{transaction_id}/review", response_model=TransactionDetail)
async def review_transaction(request: Request, transaction_id: UUID, body: ReviewRequest, customer_id: CustomerId) -> Any:
    """Correct values, add or remove rows, or confirm flagged values. All operations apply, or none do."""
    settings = request.app.state.settings
    work = _detail_after(settings, lambda conn: transactions.review(conn, settings, customer_id, transaction_id, body.expected_version, body.operations))
    return await write(request, customer_id, f"review:{transaction_id}", body.model_dump(mode="json"), work)


@router.post("/transactions/{transaction_id}/approve", response_model=TransactionDetail)
async def approve_transaction(request: Request, transaction_id: UUID, body: VersionedRequest, customer_id: CustomerId) -> Any:
    """Make this version the authoritative record. Refused while anything still needs checking."""
    settings = request.app.state.settings
    work = _detail_after(settings, lambda conn: transactions.approve(conn, settings, customer_id, transaction_id, body.expected_version))
    return await write(request, customer_id, f"approve:{transaction_id}", body.model_dump(mode="json"), work)


@router.post("/transactions/{transaction_id}/reject", response_model=TransactionDetail)
async def reject_transaction(request: Request, transaction_id: UUID, body: ReasonRequest, customer_id: CustomerId) -> Any:
    settings = request.app.state.settings
    work = _detail_after(settings, lambda conn: transactions.reject(conn, settings, customer_id, transaction_id, body.expected_version, body.reason))
    return await write(request, customer_id, f"reject:{transaction_id}", body.model_dump(mode="json"), work)


@router.post("/transactions/{transaction_id}/cancel", response_model=TransactionDetail)
async def cancel_transaction(request: Request, transaction_id: UUID, body: ReasonRequest, customer_id: CustomerId) -> Any:
    """Call the deal off. Any receipts for it are voided."""
    settings = request.app.state.settings
    work = _detail_after(settings, lambda conn: transactions.cancel(conn, settings, customer_id, transaction_id, body.expected_version, body.reason))
    return await write(request, customer_id, f"cancel:{transaction_id}", body.model_dump(mode="json"), work)


@router.post("/transactions/{transaction_id}/complete", response_model=TransactionDetail)
async def complete_transaction(request: Request, transaction_id: UUID, body: VersionedRequest, customer_id: CustomerId) -> Any:
    settings = request.app.state.settings
    work = _detail_after(settings, lambda conn: transactions.complete(conn, settings, customer_id, transaction_id, body.expected_version))
    return await write(request, customer_id, f"complete:{transaction_id}", body.model_dump(mode="json"), work)


@router.post("/transactions/{transaction_id}/reopen", response_model=TransactionDetail)
async def reopen_transaction(request: Request, transaction_id: UUID, body: ReasonRequest, customer_id: CustomerId) -> Any:
    """Open an approved record for correction. Receipts already issued don't change."""
    settings = request.app.state.settings
    work = _detail_after(settings, lambda conn: transactions.reopen(conn, settings, customer_id, transaction_id, body.expected_version, body.reason))
    return await write(request, customer_id, f"reopen:{transaction_id}", body.model_dump(mode="json"), work)


@router.post("/transactions/{transaction_id}/enter-manually", response_model=TransactionDetail)
async def enter_manually(request: Request, transaction_id: UUID, body: VersionedRequest, customer_id: CustomerId) -> Any:
    """After reading failed, continue by entering the details by hand."""
    settings = request.app.state.settings
    work = _detail_after(settings, lambda conn: transactions.enter_manually(conn, settings, customer_id, transaction_id, body.expected_version))
    return await write(request, customer_id, f"enter_manually:{transaction_id}", body.model_dump(mode="json"), work)


@router.post("/transactions/{transaction_id}/retry", response_model=JobView, status_code=202, dependencies=[Depends(limit("v1_processing"))])
async def retry_transaction(request: Request, transaction_id: UUID, body: VersionedRequest, customer_id: CustomerId) -> Any:
    """Read the conversation again after a failure."""
    settings = request.app.state.settings

    def retry(conn: Connection) -> JobView:
        row = transactions.load(conn, customer_id, transaction_id, lock=True)
        if row["version"] != body.expected_version:
            raise APIError(409, "version_conflict", "This transaction changed while you were working on it. Reload it and try again.")
        if row["conversation_id"] is None:
            raise APIError(409, "no_conversation", "This transaction has no conversation to read again.")
        updated = transactions.mark_retrying(conn, settings, row)
        job = jobs.enqueue(conn, customer_id, jobs.PROCESS_CONVERSATION, updated["conversation_id"], max_attempts=settings.engine_job_max_attempts)
        return job_view(job)

    return await write(request, customer_id, f"retry:{transaction_id}", body.model_dump(mode="json"), retry, status_code=202)


@router.post("/transactions/{transaction_id}/receipts", response_model=ReceiptView, status_code=201, tags=["Receipts"])
async def issue_receipt(request: Request, transaction_id: UUID, customer_id: CustomerId) -> Any:
    """Issue a receipt for the approved version. Asking again returns the same receipt."""
    settings = request.app.state.settings

    def issue(conn: Connection) -> ReceiptView:
        receipt, _ = receipts.issue(conn, settings, customer_id, transaction_id)
        return receipt_view(receipt)

    return await write(request, customer_id, f"issue_receipt:{transaction_id}", {"transaction_id": str(transaction_id)}, issue, status_code=201)


@router.get("/receipts", response_model=ReceiptList, tags=["Receipts"])
async def list_receipts(
    request: Request,
    customer_id: CustomerId,
    transaction_id: UUID | None = None,
    limit_to: Annotated[int, Query(alias="limit", ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
) -> ReceiptList:
    conditions = ["customer_id = %s"]
    parameters: list[Any] = [customer_id]
    if transaction_id:
        conditions.append("transaction_id = %s")
        parameters.append(transaction_id)
    if cursor:
        value, row_id = _decode_cursor(cursor, "timestamptz")
        conditions.append("(issued_at, id) < (%s, %s)")
        parameters += [value, row_id]
    sql = f"SELECT * FROM chatreceipt.receipts WHERE {' AND '.join(conditions)} ORDER BY issued_at DESC, id DESC LIMIT %s"  # noqa: S608
    rows = await run_tenant(request, customer_id, lambda conn: conn.execute(sql, [*parameters, limit_to + 1]).fetchall())
    page = rows[:limit_to]
    next_cursor = _encode_cursor(page[-1]["issued_at"].isoformat(), page[-1]["id"]) if len(rows) > limit_to else None
    return ReceiptList(receipts=[receipt_view(row) for row in page], next_cursor=next_cursor)


@router.get("/receipts/{receipt_id}", response_model=ReceiptView, tags=["Receipts"])
async def get_receipt(request: Request, receipt_id: UUID, customer_id: CustomerId) -> ReceiptView:
    return receipt_view(await run_tenant(request, customer_id, lambda conn: receipts.get(conn, customer_id, receipt_id)))


@router.get("/receipts/{receipt_id}/document", tags=["Receipts"], response_class=Response)
async def get_receipt_document(request: Request, receipt_id: UUID, customer_id: CustomerId) -> Response:
    row = await run_tenant(request, customer_id, lambda conn: receipts.get(conn, customer_id, receipt_id))
    if row["document_status"] != "ready" or not row["document_key"]:
        raise APIError(409, "document_not_ready", "The receipt's PDF isn't ready yet.", details={"document_status": row["document_status"]})
    try:
        data = await run_in_threadpool(request.app.state.storage.get, row["document_key"])
    except StorageError as exc:
        raise APIError(503, "temporarily_unavailable", "The receipt couldn't be read right now. Please try again.") from exc
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{row["reference"]}.pdf"', "Cache-Control": "private, no-store"},
    )


@router.post("/receipts/{receipt_id}/void", response_model=ReceiptView, tags=["Receipts"])
async def void_receipt(request: Request, receipt_id: UUID, body: VoidRequest, customer_id: CustomerId) -> Any:
    def void(conn: Connection) -> ReceiptView:
        return receipt_view(receipts.void(conn, customer_id, receipt_id, body.reason))

    return await write(request, customer_id, f"void_receipt:{receipt_id}", body.model_dump(mode="json"), void)


@router.delete("/account", status_code=204, tags=["Account"])
async def erase_account(request: Request, body: EraseRequest, customer_id: CustomerId) -> Response:
    """Permanently delete this account: every record, conversation, receipt and file."""

    def erase(conn: Connection) -> None:
        customer_store.delete_customer_data(conn, customer_id)

    await run_system(request, erase)
    log_event("account.erased")
    return Response(status_code=204)
