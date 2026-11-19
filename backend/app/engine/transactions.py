"""Reviewing transactions: corrections, approval and the rest of the lifecycle.

Every change goes through one write path that recalculates the totals with the backend's own rules,
adds a new immutable version, and records an event, all in one database transaction. Each change
must name the version it was based on, so a stale screen can't overwrite newer work and two
simultaneous approvals can't both succeed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.calculations import compute, parse_number, to_calculation
from app.config import Settings
from app.db.pool import Connection
from app.engine import outbox
from app.errors import APIError
from app.intelligence.fields import DUPLICATE_CHECK, LABELS, FieldClass, FieldSource, FieldState, FieldStatus
from app.intelligence.proposal import evaluate
from app.intelligence.review_policy import ReviewPolicy, policy_from_settings
from app.intelligence.validation import ValidationResult
from app.models import PaymentStatus, TransactionDraft
from app.observability import log_event, metrics
from app.records import duplicates
from app.records.lifecycle import EDITABLE_STATES, InvalidTransition, State, require_transition
from app.records.review import Operation, ReviewRejected, apply_operations

Row = dict[str, Any]

CUSTOMER = "customer"
SYSTEM = "system"


@dataclass(frozen=True)
class Change:
    """One new version of a transaction."""

    draft: TransactionDraft
    states: dict[str, FieldState]
    state: State
    reason: str
    event_type: str
    actor: str
    data: dict[str, Any] | None = None
    columns: dict[str, Any] | None = None


_UPDATABLE = frozenset(
    {
        "conversation_id", "extraction_run_id", "deal_key", "agreement_status", "summary", "extracted_values", "analysis",
        "approved_version", "approved_at", "completed_at", "state_reason",
    }
)
_JSON = frozenset({"extracted_values", "analysis"})


def draft_of(row: Row) -> TransactionDraft:
    return TransactionDraft.model_validate(row["draft"])


def states_of(row: Row) -> dict[str, FieldState]:
    return {path: FieldState.model_validate(state) for path, state in row["field_states"].items()}


def _dump_states(states: dict[str, FieldState]) -> dict[str, Any]:
    return {path: state.model_dump(mode="json") for path, state in states.items()}


def _number(text: str | None) -> Decimal | None:
    try:
        return parse_number(text) if text else None
    except ValueError:
        return None


def _snapshot(row: Row, change: Change, version: int, validation: ValidationResult) -> dict[str, Any]:
    return {
        "version": version,
        "state": change.state.value,
        "agreement_status": (change.columns or {}).get("agreement_status", row.get("agreement_status")),
        "draft": change.draft.model_dump(mode="json"),
        "calculation": validation.calculation.model_dump(mode="json"),
        "validation": {"outcome": validation.outcome, "can_approve": validation.can_approve, "failed": list(validation.failed)},
        "field_states": _dump_states(change.states),
    }


def _write_history(conn: Connection, row: Row, change: Change, version: int, from_state: str | None, validation: ValidationResult) -> None:
    computation = compute(change.draft)
    conn.execute("DELETE FROM chatreceipt.transaction_items WHERE transaction_id = %s", (row["id"],))
    for position, item in enumerate(change.draft.line_items, start=1):
        conn.execute(
            """
            INSERT INTO chatreceipt.transaction_items (transaction_id, customer_id, row_id, position, description, quantity, unit_price, line_total)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                row["id"], row["customer_id"], item.id, position, item.description, computation.quantities.get(item.id),
                computation.unit_prices.get(item.id), computation.line_amounts.get(item.id),
            ),
        )
    snapshot = _snapshot(row, change, version, validation)
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO chatreceipt.transaction_versions (transaction_id, customer_id, version, state, snapshot, snapshot_sha256, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (row["id"], row["customer_id"], version, change.state.value, Jsonb(snapshot), digest, change.reason),
    )
    conn.execute(
        """
        INSERT INTO chatreceipt.transaction_events (transaction_id, customer_id, event_type, from_state, to_state, version, actor, data)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (row["id"], row["customer_id"], change.event_type, from_state, change.state.value, version, change.actor, Jsonb(change.data or {})),
    )


def _figures(draft: TransactionDraft, validation: ValidationResult) -> dict[str, Any]:
    calculation = validation.calculation
    return {
        "currency": draft.currency.strip().upper(),
        "seller": draft.seller.strip(),
        "buyer": draft.buyer.strip(),
        "transaction_date": compute(draft).transaction_date,
        "total": _number(calculation.total),
        "amount_paid": _number(calculation.amount_paid),
        "outstanding": _number(calculation.outstanding),
        "payment_status": draft.payment_status.value,
        "review_required": not validation.can_approve,
    }


def apply_change(conn: Connection, settings: Settings, row: Row, change: Change) -> Row:
    """Write the next version of a transaction, with its totals, history and event."""
    _, validation = evaluate(change.draft, change.states, policy_from_settings(settings))
    extra = change.columns or {}
    unknown = set(extra) - _UPDATABLE
    if unknown:
        raise ValueError(f"Unknown columns: {sorted(unknown)}")
    values = {
        **_figures(change.draft, validation),
        "draft": Jsonb(change.draft.model_dump(mode="json")),
        "field_states": Jsonb(_dump_states(change.states)),
        "state": change.state.value,
        "version": row["version"] + 1,
        **{key: Jsonb(value) if key in _JSON else value for key, value in extra.items()},
    }
    assignments = ", ".join(f"{column} = %s" for column in values)
    updated = conn.execute(
        f"UPDATE chatreceipt.extracted_transactions SET {assignments}, updated_at = now() WHERE id = %s AND version = %s RETURNING *",  # noqa: S608
        [*values.values(), row["id"], row["version"]],
    ).fetchone()
    if updated is None:
        raise APIError(409, "version_conflict", "This transaction changed while you were working on it. Reload it and try again.")
    _write_history(conn, updated, change, updated["version"], row["state"], validation)
    return updated


def create_transaction(
    conn: Connection,
    settings: Settings,
    customer_id: UUID,
    *,
    origin: str,
    change: Change,
    conversation_id: UUID | None = None,
) -> Row:
    _, validation = evaluate(change.draft, change.states, policy_from_settings(settings))
    figures = _figures(change.draft, validation)
    extra = change.columns or {}
    row = conn.execute(
        """
        INSERT INTO chatreceipt.extracted_transactions (
            customer_id, conversation_id, extraction_run_id, deal_key, origin, state, agreement_status, summary, draft,
            field_states, extracted_values, analysis, currency, seller, buyer, transaction_date, total, amount_paid,
            outstanding, payment_status, review_required
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            customer_id, conversation_id, extra.get("extraction_run_id"), extra.get("deal_key"), origin, change.state.value,
            extra.get("agreement_status"), extra.get("summary", ""), Jsonb(change.draft.model_dump(mode="json")),
            Jsonb(_dump_states(change.states)), Jsonb(extra.get("extracted_values", {})), Jsonb(extra.get("analysis", {})),
            figures["currency"], figures["seller"], figures["buyer"], figures["transaction_date"], figures["total"],
            figures["amount_paid"], figures["outstanding"], figures["payment_status"], figures["review_required"],
        ),
    ).fetchone()
    assert row is not None
    _write_history(conn, row, change, 1, None, validation)
    return row


def create_placeholder(conn: Connection, settings: Settings, customer_id: UUID, conversation_id: UUID) -> Row:
    """The transaction a client can follow while its conversation is being read."""
    change = Change(TransactionDraft(), {}, State.PROCESSING, "conversation submitted", "processing_started", SYSTEM)
    return create_transaction(conn, settings, customer_id, origin="extraction", change=change, conversation_id=conversation_id)


def create_manual(conn: Connection, settings: Settings, customer_id: UUID, draft: TransactionDraft) -> Row:
    change = Change(draft, {}, State.DRAFT, "entered by hand", "created", CUSTOMER)
    return create_transaction(conn, settings, customer_id, origin="manual", change=change)


def load(conn: Connection, customer_id: UUID, transaction_id: UUID, *, lock: bool = False) -> Row:
    suffix = " FOR NO KEY UPDATE" if lock else ""
    row = conn.execute(
        f"SELECT * FROM chatreceipt.extracted_transactions WHERE id = %s AND customer_id = %s{suffix}",  # noqa: S608
        (transaction_id, customer_id),
    ).fetchone()
    if row is None:
        raise APIError(404, "not_found", "That transaction doesn't exist.")
    return row


def _editable_version(conn: Connection, customer_id: UUID, transaction_id: UUID, expected_version: int) -> Row:
    row = load(conn, customer_id, transaction_id, lock=True)
    if row["version"] != expected_version:
        raise APIError(
            409,
            "version_conflict",
            "This transaction changed while you were working on it. Reload it and try again.",
            details={"current_version": row["version"], "expected_version": expected_version},
        )
    return row


def _require(row: Row, target: State) -> None:
    try:
        require_transition(State(row["state"]), target)
    except InvalidTransition as exc:
        raise APIError(
            409,
            "invalid_state",
            f"A transaction that is {exc.current.value.replace('_', ' ')} can't become {exc.target.value.replace('_', ' ')}.",
            details={"state": exc.current.value},
        ) from exc


def review(
    conn: Connection, settings: Settings, customer_id: UUID, transaction_id: UUID, expected_version: int, operations: list[Operation]
) -> Row:
    row = _editable_version(conn, customer_id, transaction_id, expected_version)
    if State(row["state"]) not in EDITABLE_STATES:
        raise APIError(409, "not_editable", "This transaction can't be changed in its current state. Reopen it first.", details={"state": row["state"]})
    try:
        outcome = apply_operations(draft_of(row), states_of(row), row["extracted_values"], operations)  # type: ignore[arg-type]
    except ReviewRejected as exc:
        problems = [{"index": p.index, "path": p.path, "code": p.code, "message": p.message} for p in exc.problems]
        raise APIError(422, "invalid_correction", problems[0]["message"], details={"problems": problems}) from exc
    change = Change(outcome.draft, outcome.states, State(row["state"]), "reviewed", "reviewed", CUSTOMER, {"operations": len(outcome.decisions)})
    updated = apply_change(conn, settings, row, change)
    for decision in outcome.decisions:
        conn.execute(
            """
            INSERT INTO chatreceipt.review_decisions (
                transaction_id, customer_id, from_version, to_version, operation, field_path, extracted_value,
                previous_value, new_value, reason, actor_id, extraction_run_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                row["id"], customer_id, row["version"], updated["version"], decision.operation, decision.path,
                Jsonb(decision.extracted_value), Jsonb(decision.previous_value), Jsonb(decision.new_value),
                decision.reason, customer_id, row["extraction_run_id"],
            ),
        )
        metrics.inc("chatreceipt_review_decisions_total", operation=decision.operation)
    return updated


def possible_duplicates(conn: Connection, row: Row, draft: TransactionDraft, total: Decimal | None) -> list[duplicates.Match]:
    """Other transactions of this customer that may be the same deal."""
    candidates = conn.execute(
        """
        SELECT t.id, t.seller, t.buyer, t.currency, t.total, t.transaction_date,
               COALESCE((SELECT array_agg(i.description) FROM chatreceipt.transaction_items i WHERE i.transaction_id = t.id), '{}') AS descriptions,
               EXISTS (
                   SELECT 1
                   FROM chatreceipt.conversation_sources mine
                   JOIN chatreceipt.uploads my_file ON my_file.id = mine.upload_id
                   JOIN chatreceipt.conversation_sources theirs ON theirs.conversation_id = t.conversation_id
                   JOIN chatreceipt.uploads their_file ON their_file.id = theirs.upload_id AND their_file.sha256 = my_file.sha256
                   WHERE mine.conversation_id = %(conversation)s::uuid
               ) AS same_source
        FROM chatreceipt.extracted_transactions t
        WHERE t.customer_id = %(customer)s AND t.id IS DISTINCT FROM %(id)s::uuid
          AND t.state NOT IN ('rejected', 'cancelled', 'failed', 'processing')
          AND (%(conversation)s::uuid IS NULL OR t.conversation_id IS DISTINCT FROM %(conversation)s::uuid)
        ORDER BY t.created_at DESC
        LIMIT 200
        """,
        {"conversation": row["conversation_id"], "customer": row["customer_id"], "id": row["id"]},
    ).fetchall()
    new = duplicates.Fingerprint(
        id=str(row["id"]),
        seller=draft.seller,
        buyer=draft.buyer,
        currency=draft.currency.strip().upper(),
        total=total,
        transaction_date=compute(draft).transaction_date,
        descriptions=tuple(item.description for item in draft.line_items),
    )
    matches = []
    for other in candidates:
        fingerprint = duplicates.Fingerprint(
            id=str(other["id"]), seller=other["seller"], buyer=other["buyer"], currency=other["currency"], total=other["total"],
            transaction_date=other["transaction_date"], descriptions=tuple(other["descriptions"]),
        )
        match = duplicates.find_match(new, fingerprint, same_source=other["same_source"])
        if match is not None:
            matches.append(match)
    return matches


def duplicate_state(matches: list[duplicates.Match], policy: ReviewPolicy) -> FieldState:
    reasons = [f"This may be the same deal as transaction {match.transaction_id}. {' '.join(match.reasons)}" for match in matches]
    status = FieldStatus.NEEDS_REVIEW if policy.block_possible_duplicates else FieldStatus.INFERRED
    return FieldState(
        path=DUPLICATE_CHECK,
        label=LABELS[DUPLICATE_CHECK],
        field_class=FieldClass.PROCESS,
        status=status,
        source=FieldSource.DERIVED,
        reasons=tuple(reasons),
        blocking=policy.is_blocking(FieldClass.PROCESS, status),
    )


def publish_record(conn: Connection, row: Row, draft: TransactionDraft) -> None:
    """Show an approved transaction on the customer's records page."""
    computation = compute(draft)
    calculation = to_calculation(draft, computation).model_dump(mode="json")
    values = (
        Jsonb(draft.model_dump(mode="json")), Jsonb(calculation), draft.currency.strip().upper(), draft.seller.strip(), draft.buyer.strip(),
        computation.transaction_date, computation.total, computation.amount_paid, computation.outstanding, draft.payment_status.value,
    )
    updated = conn.execute(
        """
        UPDATE chatreceipt.transactions
        SET draft = %s, calculation = %s, currency = %s, seller = %s, buyer = %s, transaction_date = %s, total = %s,
            amount_paid = %s, outstanding = %s, payment_status = %s, voided_at = NULL
        WHERE extracted_transaction_id = %s
        """,
        (*values, row["id"]),
    )
    if updated.rowcount:
        return
    customer = conn.execute(
        "SELECT next_record_no FROM chatreceipt.customers WHERE id = %s FOR NO KEY UPDATE", (row["customer_id"],)
    ).fetchone()
    assert customer is not None
    number = customer["next_record_no"]
    conn.execute(
        """
        INSERT INTO chatreceipt.transactions (
            draft, calculation, currency, seller, buyer, transaction_date, total, amount_paid, outstanding, payment_status,
            customer_id, record_no, reference, source, extracted_transaction_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'engine', %s)
        """,
        (*values, row["customer_id"], number, f"CR-{number:06d}", row["id"]),
    )
    conn.execute("UPDATE chatreceipt.customers SET next_record_no = %s WHERE id = %s", (number + 1, row["customer_id"]))


def _event_data(row: Row) -> dict[str, Any]:
    return {
        "transaction_id": str(row["id"]),
        "version": row["version"],
        "state": row["state"],
        "currency": row["currency"],
        "total": str(row["total"]) if row["total"] is not None else None,
        "payment_status": row["payment_status"],
    }


def approve(conn: Connection, settings: Settings, customer_id: UUID, transaction_id: UUID, expected_version: int) -> Row:
    row = _editable_version(conn, customer_id, transaction_id, expected_version)
    _require(row, State.APPROVED)
    draft, states = draft_of(row), states_of(row)
    policy = policy_from_settings(settings)

    effective, validation = evaluate(draft, states, policy)
    matches = possible_duplicates(conn, row, draft, row["total"])
    acknowledged = states.get(DUPLICATE_CHECK) is not None and states[DUPLICATE_CHECK].resolved
    if matches and policy.block_possible_duplicates and not acknowledged:
        raise APIError(
            409,
            "possible_duplicate",
            "This may be the same deal as another record. Check it, then confirm duplicate_check to continue.",
            details={"matches": [{"transaction_id": m.transaction_id, "reasons": list(m.reasons)} for m in matches]},
        )
    if not validation.can_approve:
        blocking = [
            {"path": state.path, "status": state.status.value, "reasons": list(state.reasons)}
            for state in effective.values()
            if state.blocking and not state.resolved
        ]
        raise APIError(
            422,
            "review_incomplete",
            "Some details need correcting or confirming before this can be approved.",
            details={"failed_rules": list(validation.failed), "fields": blocking},
        )
    change = Change(
        draft, states, State.APPROVED, "approved", "approved", CUSTOMER,
        columns={"approved_version": row["version"] + 1, "approved_at": _now(conn)},
    )
    updated = apply_change(conn, settings, row, change)
    publish_record(conn, updated, draft)
    outbox.emit(conn, settings, customer_id, outbox.TRANSACTION_APPROVED, updated["id"], _event_data(updated))
    metrics.inc("chatreceipt_transactions_total", outcome="approved")
    log_event("transaction.approved", transaction_id=updated["id"], version=updated["version"])
    return updated


def _now(conn: Connection) -> Any:
    row = conn.execute("SELECT now() AS at").fetchone()
    return row["at"]  # type: ignore[index]


def _transition(
    conn: Connection, settings: Settings, customer_id: UUID, transaction_id: UUID, expected_version: int,
    *, target: State, event_type: str, reason: str, columns: dict[str, Any] | None = None,
) -> Row:
    row = _editable_version(conn, customer_id, transaction_id, expected_version)
    _require(row, target)
    change = Change(draft_of(row), states_of(row), target, reason or event_type, event_type, CUSTOMER, {"reason": reason}, columns)
    return apply_change(conn, settings, row, change)


def reject(conn: Connection, settings: Settings, customer_id: UUID, transaction_id: UUID, expected_version: int, reason: str) -> Row:
    updated = _transition(conn, settings, customer_id, transaction_id, expected_version, target=State.REJECTED, event_type="rejected", reason=reason, columns={"state_reason": reason})
    metrics.inc("chatreceipt_transactions_total", outcome="rejected")
    return updated


def reopen(conn: Connection, settings: Settings, customer_id: UUID, transaction_id: UUID, expected_version: int, reason: str) -> Row:
    """Open an approved record for correction. Receipts already issued stay exactly as they were."""
    return _transition(conn, settings, customer_id, transaction_id, expected_version, target=State.AWAITING_REVIEW, event_type="reopened", reason=reason, columns={"state_reason": reason})


def complete(conn: Connection, settings: Settings, customer_id: UUID, transaction_id: UUID, expected_version: int) -> Row:
    row = _editable_version(conn, customer_id, transaction_id, expected_version)
    _require(row, State.COMPLETED)
    if row["payment_status"] != PaymentStatus.PAID.value or (row["outstanding"] or 0) != 0:
        raise APIError(422, "not_fully_paid", "Only a record that shows the full amount as paid can be completed.")
    change = Change(draft_of(row), states_of(row), State.COMPLETED, "completed", "completed", CUSTOMER, columns={"completed_at": _now(conn)})
    updated = apply_change(conn, settings, row, change)
    outbox.emit(conn, settings, customer_id, outbox.TRANSACTION_COMPLETED, updated["id"], _event_data(updated))
    return updated


def cancel(conn: Connection, settings: Settings, customer_id: UUID, transaction_id: UUID, expected_version: int, reason: str) -> Row:
    """Call the deal off: its receipts are voided and it leaves the customer's records."""
    updated = _transition(conn, settings, customer_id, transaction_id, expected_version, target=State.CANCELLED, event_type="cancelled", reason=reason, columns={"state_reason": reason})
    conn.execute(
        """
        UPDATE chatreceipt.receipts SET status = 'voided', void_reason = %s, voided_at = now()
        WHERE transaction_id = %s AND customer_id = %s AND status <> 'voided'
        """,
        (reason or "The transaction was cancelled.", transaction_id, customer_id),
    )
    conn.execute("UPDATE chatreceipt.transactions SET voided_at = now() WHERE extracted_transaction_id = %s", (transaction_id,))
    outbox.emit(conn, settings, customer_id, outbox.TRANSACTION_CANCELLED, updated["id"], _event_data(updated))
    return updated


def enter_manually(conn: Connection, settings: Settings, customer_id: UUID, transaction_id: UUID, expected_version: int) -> Row:
    return _transition(conn, settings, customer_id, transaction_id, expected_version, target=State.DRAFT, event_type="entered_manually", reason="entered by hand after reading failed")


def mark_retrying(conn: Connection, settings: Settings, row: Row) -> Row:
    _require(row, State.PROCESSING)
    change = Change(draft_of(row), {}, State.PROCESSING, "processing retried", "processing_retried", CUSTOMER, columns={"state_reason": ""})
    return apply_change(conn, settings, row, change)


def mark_failed(conn: Connection, settings: Settings, row: Row, *, code: str, message: str) -> Row:
    change = Change(
        draft_of(row), states_of(row), State.FAILED, "processing failed", "processing_failed", SYSTEM, {"code": code}, {"state_reason": message}
    )
    updated = apply_change(conn, settings, row, change)
    outbox.emit(conn, settings, row["customer_id"], outbox.PROCESSING_FAILED, updated["id"], {**_event_data(updated), "code": code})
    return updated
