"""Receipts: issued from an approved, stored version of a transaction, and never changed afterwards.

A receipt points at one exact version. If the transaction is corrected later, the receipt already
issued stays as it was; issuing again creates a new receipt that replaces it, and the old one is
marked superseded. Asking for a receipt twice for the same version returns the same receipt.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.calculations import compute
from app.config import Settings
from app.db.pool import Connection, Database
from app.documents.browser import BrowserRenderer
from app.documents.render import render_document_html
from app.engine import jobs, outbox, transactions
from app.errors import APIError
from app.models import TransactionDraft
from app.observability import log_event, metrics
from app.records.lifecycle import RECEIPT_STATES, State
from app.storage import ObjectStorage, receipt_key

Row = dict[str, Any]


def issue(conn: Connection, settings: Settings, customer_id: UUID, transaction_id: UUID) -> tuple[Row, bool]:
    """Issue a receipt for the approved version. Returns the receipt and whether it was just created."""
    row = transactions.load(conn, customer_id, transaction_id, lock=True)
    if State(row["state"]) not in RECEIPT_STATES or row["approved_version"] is None:
        raise APIError(409, "not_approved", "A receipt can only be issued for an approved transaction.", details={"state": row["state"]})
    version = row["approved_version"]
    existing = conn.execute(
        "SELECT * FROM chatreceipt.receipts WHERE transaction_id = %s AND transaction_version = %s AND status <> 'voided'",
        (transaction_id, version),
    ).fetchone()
    if existing is not None:
        return existing, False

    customer = conn.execute("SELECT next_receipt_no FROM chatreceipt.customers WHERE id = %s FOR NO KEY UPDATE", (customer_id,)).fetchone()
    assert customer is not None
    number = customer["next_receipt_no"]
    previous = conn.execute(
        "SELECT id FROM chatreceipt.receipts WHERE transaction_id = %s AND status = 'issued' FOR UPDATE", (transaction_id,)
    ).fetchone()
    if previous is not None:
        conn.execute("UPDATE chatreceipt.receipts SET status = 'superseded', superseded_at = now() WHERE id = %s", (previous["id"],))
    receipt = conn.execute(
        """
        INSERT INTO chatreceipt.receipts (customer_id, transaction_id, transaction_version, number, reference, replaces_receipt_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (customer_id, transaction_id, version, number, f"RCT-{number:06d}", previous["id"] if previous else None),
    ).fetchone()
    assert receipt is not None
    conn.execute("UPDATE chatreceipt.customers SET next_receipt_no = %s WHERE id = %s", (number + 1, customer_id))
    conn.execute(
        """
        INSERT INTO chatreceipt.transaction_events (transaction_id, customer_id, event_type, from_state, to_state, version, actor, data)
        VALUES (%s, %s, 'receipt_issued', %s, %s, %s, 'customer', %s)
        """,
        (transaction_id, customer_id, row["state"], row["state"], row["version"], Jsonb({"receipt_id": str(receipt["id"]), "reference": receipt["reference"]})),
    )
    jobs.enqueue(conn, customer_id, jobs.RENDER_RECEIPT, receipt["id"], max_attempts=settings.engine_job_max_attempts)
    metrics.inc("chatreceipt_receipts_total", outcome="issued")
    return receipt, True


def get(conn: Connection, customer_id: UUID, receipt_id: UUID) -> Row:
    row = conn.execute("SELECT * FROM chatreceipt.receipts WHERE id = %s AND customer_id = %s", (receipt_id, customer_id)).fetchone()
    if row is None:
        raise APIError(404, "not_found", "That receipt doesn't exist.")
    return row


def void(conn: Connection, customer_id: UUID, receipt_id: UUID, reason: str) -> Row:
    row = conn.execute(
        "SELECT * FROM chatreceipt.receipts WHERE id = %s AND customer_id = %s FOR UPDATE", (receipt_id, customer_id)
    ).fetchone()
    if row is None:
        raise APIError(404, "not_found", "That receipt doesn't exist.")
    if row["status"] == "voided":
        return row
    updated = conn.execute(
        "UPDATE chatreceipt.receipts SET status = 'voided', void_reason = %s, voided_at = now() WHERE id = %s RETURNING *", (reason, receipt_id)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO chatreceipt.transaction_events (transaction_id, customer_id, event_type, version, actor, data)
        VALUES (%s, %s, 'receipt_voided', %s, 'customer', %s)
        """,
        (row["transaction_id"], customer_id, row["transaction_version"], Jsonb({"receipt_id": str(receipt_id), "reason": reason})),
    )
    return updated  # type: ignore[return-value]


async def render(database: Database, storage: ObjectStorage, renderer: BrowserRenderer, settings: Settings, job: Row) -> None:
    """Create the receipt's PDF from the exact version it was issued for."""
    customer_id, receipt_id = job["customer_id"], job["subject_id"]
    with database.tenant(customer_id) as conn:
        receipt = get(conn, customer_id, receipt_id)
        version = conn.execute(
            "SELECT snapshot FROM chatreceipt.transaction_versions WHERE transaction_id = %s AND version = %s",
            (receipt["transaction_id"], receipt["transaction_version"]),
        ).fetchone()
    assert version is not None
    draft = TransactionDraft.model_validate(version["snapshot"]["draft"])
    issued_on: date = receipt["issued_at"].date()
    html = render_document_html(
        draft,
        compute(draft),
        prepared_on=issued_on,
        receipt={"reference": receipt["reference"], "version": receipt["transaction_version"]},
    )
    data = await renderer.render(html, "pdf")
    key = receipt_key(customer_id, receipt_id)
    storage.put(key, data)
    with database.tenant(customer_id) as conn:
        conn.execute(
            """
            UPDATE chatreceipt.receipts SET document_status = 'ready', document_key = %s, document_sha256 = %s, document_bytes = %s
            WHERE id = %s
            """,
            (key, hashlib.sha256(data).hexdigest(), len(data), receipt_id),
        )
        outbox.emit(
            conn, settings, customer_id, outbox.RECEIPT_GENERATED, receipt_id,
            {"receipt_id": str(receipt_id), "reference": receipt["reference"], "transaction_id": str(receipt["transaction_id"]), "transaction_version": receipt["transaction_version"]},
        )
        jobs.complete(conn, job, {"receipt_id": str(receipt_id)})
    log_event("receipt.generated", receipt_id=receipt_id, bytes=len(data))


def mark_render_failed(conn: Connection, job: Row) -> None:
    conn.execute("UPDATE chatreceipt.receipts SET document_status = 'failed' WHERE id = %s", (job["subject_id"],))
