"""Turn stored rows into the API's response shapes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.api.v1.schemas import (
    FieldView,
    JobView,
    ReceiptView,
    RuleView,
    TransactionDetail,
    TransactionSummary,
    UploadView,
    ValidationView,
)
from app.calculations import to_plain
from app.config import Settings
from app.db.pool import Connection
from app.engine.transactions import draft_of, states_of
from app.intelligence.proposal import evaluate
from app.intelligence.review_policy import policy_from_settings
from app.records.lifecycle import State, available_actions

Row = dict[str, Any]
MAX_QUOTE_CHARS = 240


def _text(value: Decimal | None) -> str | None:
    return to_plain(value) if value is not None else None


def upload_view(row: Row, *, duplicate: bool = False) -> UploadView:
    return UploadView(
        id=row["id"],
        kind=row["kind"],
        filename=row["original_filename"],
        content_type=row["content_type"],
        bytes=row["byte_size"],
        sha256=row["sha256"],
        width=row["width"],
        height=row["height"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        duplicate=duplicate,
    )


def job_view(row: Row) -> JobView:
    return JobView(
        id=row["id"],
        kind=row["kind"],
        subject_id=row["subject_id"],
        status=row["status"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        result=row["result"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def transaction_summary(row: Row) -> TransactionSummary:
    return TransactionSummary(
        id=row["id"],
        state=row["state"],
        version=row["version"],
        origin=row["origin"],
        agreement_status=row["agreement_status"],
        summary=row["summary"],
        currency=row["currency"],
        seller=row["seller"],
        buyer=row["buyer"],
        transaction_date=row["transaction_date"],
        total=_text(row["total"]),
        amount_paid=_text(row["amount_paid"]),
        outstanding=_text(row["outstanding"]),
        payment_status=row["payment_status"],
        review_required=row["review_required"],
        conversation_id=row["conversation_id"],
        state_reason=row["state_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _evidence(conn: Connection, row: Row, keys: set[str]) -> dict[str, dict[str, Any]]:
    if not keys or row["extraction_run_id"] is None:
        return {}
    messages = conn.execute(
        """
        SELECT message_key, text, source_kind, source_position FROM chatreceipt.conversation_messages
        WHERE run_id = %s AND message_key = ANY(%s)
        """,
        (row["extraction_run_id"], list(keys)),
    ).fetchall()
    return {
        message["message_key"]: {
            "message": message["message_key"],
            "screenshot": message["source_position"] if message["source_kind"] == "screenshot" else None,
            "quote": message["text"][:MAX_QUOTE_CHARS],
        }
        for message in messages
    }


def transaction_detail(conn: Connection, settings: Settings, row: Row) -> TransactionDetail:
    draft = draft_of(row)
    states, validation = evaluate(draft, states_of(row), policy_from_settings(settings))
    quotes = _evidence(conn, row, {key for state in states.values() for key in state.evidence})
    fields = {
        path: FieldView(
            path=state.path,
            label=state.label,
            field_class=state.field_class.value,
            status=state.status.value,
            source=state.source.value,
            confidence=state.confidence.value if state.confidence else None,
            grounded=state.grounded,
            blocking=state.blocking and not state.resolved,
            resolved=state.resolved,
            reasons=list(state.reasons),
            candidates=[candidate.model_dump(mode="json") for candidate in state.candidates],
            evidence=[quotes[key] for key in state.evidence if key in quotes],
        )
        for path, state in states.items()
    }
    return TransactionDetail(
        **transaction_summary(row).model_dump(),
        draft=draft,
        calculation=validation.calculation,
        validation=ValidationView(
            outcome=validation.outcome,
            can_approve=validation.can_approve,
            passed=list(validation.passed),
            failed=list(validation.failed),
            warnings=list(validation.warnings),
            missing=list(validation.missing),
            contradictions=list(validation.contradictions),
            review_required=list(validation.review_required),
            rules=[
                RuleView(rule=rule.rule, description=rule.description, outcome=rule.outcome, fields=list(rule.fields), messages=list(rule.messages))
                for rule in validation.rules
            ],
        ),
        fields=fields,
        analysis=row["analysis"],
        available_actions=available_actions(State(row["state"])),
        approved_version=row["approved_version"],
    )


def receipt_view(row: Row) -> ReceiptView:
    return ReceiptView(
        id=row["id"],
        reference=row["reference"],
        transaction_id=row["transaction_id"],
        transaction_version=row["transaction_version"],
        status=row["status"],
        document_status=row["document_status"],
        document_sha256=row["document_sha256"],
        replaces_receipt_id=row["replaces_receipt_id"],
        void_reason=row["void_reason"],
        issued_at=row["issued_at"],
        superseded_at=row["superseded_at"],
        voided_at=row["voided_at"],
    )
