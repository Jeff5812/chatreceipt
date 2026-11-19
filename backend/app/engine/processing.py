"""Reading a conversation into reviewable transactions.

Slow steps (file reads, AI calls) run outside any database transaction. Everything the run produced
is then saved in one transaction together with marking the job complete, so a crash either leaves
nothing behind or leaves the finished result, never half of it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.config import Settings
from app.db.pool import Connection, Database
from app.engine import jobs, transactions
from app.engine.failures import Failure
from app.engine.transactions import Change
from app.extraction.provider import StructuredModel
from app.intelligence import pipeline
from app.intelligence.conversation import Conversation
from app.intelligence.fields import DUPLICATE_CHECK
from app.intelligence.interpretation import Interpretation
from app.intelligence.proposal import Proposal
from app.intelligence.review_policy import policy_from_settings
from app.intelligence.screenshots import ScreenshotTranscript
from app.intelligence.versions import (
    ENGINE_VERSION,
    INTERPRETATION_PROMPT_VERSION,
    INTERPRETATION_SCHEMA_VERSION,
    TRANSCRIPTION_PROMPT_VERSION,
)
from app.observability import log_context, log_event, metrics
from app.records.lifecycle import State
from app.storage import ObjectStorage
from app.uploads import UploadedFile, prepare_screenshots

Row = dict[str, Any]


@dataclass(frozen=True)
class Services:
    database: Database
    storage: ObjectStorage
    model: StructuredModel
    settings: Settings


def _sources(conn: Connection, customer_id: UUID, conversation_id: UUID) -> tuple[Row | None, list[Row]]:
    conversation = conn.execute(
        "SELECT * FROM chatreceipt.conversations WHERE id = %s AND customer_id = %s", (conversation_id, customer_id)
    ).fetchone()
    sources = conn.execute(
        """
        SELECT s.position, u.id AS upload_id, u.kind, u.storage_key, u.status
        FROM chatreceipt.conversation_sources s
        JOIN chatreceipt.uploads u ON u.id = s.upload_id
        WHERE s.conversation_id = %s
        ORDER BY s.position
        """,
        (conversation_id,),
    ).fetchall()
    return conversation, sources


def _record_stage(stage: pipeline.StageRecord | None, stages: list[dict[str, Any]]) -> None:
    if stage is None:
        return
    stages.append(stage.__dict__)
    metrics.inc("chatreceipt_ai_calls_total", task=stage.task, outcome="ok", provider=stage.provider)
    metrics.observe("chatreceipt_ai_call_seconds", stage.duration_ms / 1000, task=stage.task)
    metrics.inc("chatreceipt_ai_tokens_total", stage.input_tokens, direction="input", task=stage.task)
    metrics.inc("chatreceipt_ai_tokens_total", stage.output_tokens, direction="output", task=stage.task)


async def _read_screenshots(services: Services, customer_id: UUID, sources: list[Row], stages: list[dict[str, Any]]) -> Conversation:
    database, storage, settings = services.database, services.storage, services.settings
    prompt = TRANSCRIPTION_PROMPT_VERSION

    def cached() -> dict[UUID, Any]:
        with database.tenant(customer_id) as conn:
            rows = conn.execute(
                "SELECT upload_id, transcript FROM chatreceipt.ocr_results WHERE upload_id = ANY(%s) AND provider = %s AND prompt_version = %s",
                ([source["upload_id"] for source in sources], services.model.name, prompt),
            ).fetchall()
        return {row["upload_id"]: row["transcript"] for row in rows}

    known = await asyncio.to_thread(cached)
    if all(source["upload_id"] in known for source in sources):
        transcripts = [
            ScreenshotTranscript.model_validate({**known[source["upload_id"]], "screenshot": number})
            for number, source in enumerate(sources, start=1)
        ]
        return pipeline.conversation_from_screenshots(transcripts)

    files = [
        UploadedFile(name=f"source-{source['position']}", data=await asyncio.to_thread(storage.get, source["storage_key"]))
        for source in sources
    ]
    images = await asyncio.to_thread(
        prepare_screenshots, files, max_files=settings.max_files, max_file_bytes=settings.max_file_bytes, max_total_bytes=settings.max_total_bytes
    )
    transcription = await pipeline.transcribe_screenshots(services.model, images)
    _record_stage(transcription.stage, stages)

    def remember() -> None:
        with database.tenant(customer_id) as conn:
            for source, transcript in zip(sources, transcription.transcripts):
                conn.execute(
                    """
                    INSERT INTO chatreceipt.ocr_results (upload_id, customer_id, provider, prompt_version, model, transcript)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (upload_id, provider, prompt_version) DO NOTHING
                    """,
                    (source["upload_id"], customer_id, services.model.name, prompt, transcription.stage.model, Jsonb(transcript.model_dump(mode="json"))),
                )

    await asyncio.to_thread(remember)  # kept, so a retry after a later failure doesn't pay to read the images again
    return pipeline.conversation_from_screenshots(transcription.transcripts)


async def read_conversation(services: Services, customer_id: UUID, sources: list[Row], stages: list[dict[str, Any]]) -> Conversation:
    if any(source["status"] != "stored" for source in sources):
        raise pipeline.EmptyConversationError("The uploaded files for this conversation have been deleted.")
    if sources and sources[0]["kind"] == "text":
        data = await asyncio.to_thread(services.storage.get, sources[0]["storage_key"])
        conversation, _, stage = await pipeline.conversation_from_text(
            services.model, data.decode("utf-8-sig"), day_first_default=services.settings.text_dates_day_first
        )
        _record_stage(stage, stages)
        return conversation
    return await _read_screenshots(services, customer_id, sources, stages)


def _save_run(
    conn: Connection, job: Row, conversation: Conversation, interpretation: Interpretation, raw: Any, model: StructuredModel, stages: list[dict[str, Any]], duration_ms: int
) -> Row:
    customer_id = job["customer_id"]
    run = conn.execute(
        """
        INSERT INTO chatreceipt.extraction_runs (
            customer_id, conversation_id, job_id, engine_version, transcription_prompt_version, interpretation_prompt_version,
            schema_version, provider, model, stages, interpretation, issues, notes, input_tokens, output_tokens, duration_ms
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            customer_id, job["subject_id"], job["id"], ENGINE_VERSION, TRANSCRIPTION_PROMPT_VERSION, INTERPRETATION_PROMPT_VERSION,
            INTERPRETATION_SCHEMA_VERSION, model.name, stages[-1]["model"] if stages else "", Jsonb(stages), Jsonb(raw),
            Jsonb(list(interpretation.issues)), Jsonb([note.model_dump(mode="json") for note in conversation.notes]),
            sum(stage["input_tokens"] for stage in stages), sum(stage["output_tokens"] for stage in stages), duration_ms,
        ),
    ).fetchone()
    assert run is not None
    roles = {assignment.participant: assignment for assignment in interpretation.roles}
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO chatreceipt.conversation_participants (run_id, customer_id, participant_key, name, side, role, role_confidence, role_evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    run["id"], customer_id, person.key, person.name, person.side.value,
                    roles[person.key].role.value if person.key in roles else "unknown",
                    roles[person.key].confidence.value if person.key in roles else None,
                    list(roles[person.key].evidence) if person.key in roles else [],
                )
                for person in conversation.participants
            ],
        )
        cursor.executemany(
            """
            INSERT INTO chatreceipt.conversation_messages (
                run_id, customer_id, message_key, seq, participant_key, kind, text, original_text, time_label, day_label,
                sent_at, quoted, legibility, source_kind, source_position, source_index
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    run["id"], customer_id, m.key, m.seq, m.participant, m.kind.value, m.text, m.original_text, m.time, m.day,
                    m.sent_at, m.quoted, m.legibility.value, m.source.kind, m.source.position, m.source.index,
                )
                for m in conversation.messages
            ],
        )
    return run


def _proposal_change(proposal: Proposal, run: Row, states: dict[str, Any]) -> Change:
    analysis = {
        "negotiation": proposal.negotiation.model_dump(mode="json"),
        "payment": proposal.payment.model_dump(mode="json"),
        "notes": list(proposal.notes),
        "model_agreement_status": proposal.model_agreement_status.value if proposal.model_agreement_status else None,
        "roles": proposal.roles,
        "item_map": proposal.item_map,
    }
    return Change(
        proposal.draft,
        states,
        State.AWAITING_REVIEW,
        "extracted",
        "extracted",
        transactions.SYSTEM,
        {"run_id": str(run["id"]), "deal_key": proposal.deal_key},
        {
            "extraction_run_id": run["id"],
            "deal_key": proposal.deal_key,
            "agreement_status": proposal.agreement_status.value,
            "summary": proposal.summary,
            "extracted_values": proposal.extracted,
            "analysis": analysis,
        },
    )


def _save_result(
    services: Services, job: Row, conversation: Conversation, analysis: pipeline.Analysis, proposals: list[Proposal], stages: list[dict[str, Any]], started: float
) -> dict[str, Any]:
    settings = services.settings
    customer_id, conversation_id = job["customer_id"], job["subject_id"]
    duration_ms = round((time.perf_counter() - started) * 1000)
    policy = policy_from_settings(settings)
    with services.database.tenant(customer_id) as conn:
        run = _save_run(conn, job, conversation, analysis.interpretation, analysis.raw, services.model, stages, duration_ms)
        placeholder = conn.execute(
            "SELECT * FROM chatreceipt.extracted_transactions WHERE conversation_id = %s AND state = 'processing' FOR NO KEY UPDATE",
            (conversation_id,),
        ).fetchone()
        saved: list[str] = []
        for index, proposal in enumerate(proposals):
            states = dict(proposal.base_states)
            base_row = placeholder if index == 0 and placeholder else {"id": None, "conversation_id": conversation_id, "customer_id": customer_id}
            matches = transactions.possible_duplicates(conn, base_row, proposal.draft, _total(proposal))
            if matches:
                states[DUPLICATE_CHECK] = transactions.duplicate_state(matches, policy)
            change = _proposal_change(proposal, run, states)
            if index == 0 and placeholder is not None:
                row = transactions.apply_change(conn, settings, placeholder, change)
            else:
                row = transactions.create_transaction(conn, settings, customer_id, origin="extraction", change=change, conversation_id=conversation_id)
            saved.append(str(row["id"]))
        if not proposals and placeholder is not None:
            message = "No transaction was found in this conversation. You can enter the details yourself."
            transactions.mark_failed(conn, settings, placeholder, code="no_transaction", message=message)
        result = {"run_id": str(run["id"]), "transaction_ids": saved, "contains_transaction": bool(proposals)}
        jobs.complete(conn, job, result, duration_ms=duration_ms)
    outcome = "transactions_found" if proposals else "no_transaction"
    metrics.inc("chatreceipt_extractions_total", outcome=outcome)
    metrics.observe("chatreceipt_extraction_seconds", duration_ms / 1000)
    log_event("extraction.saved", run_id=run["id"], transactions=len(saved), duration_ms=duration_ms, outcome=outcome)
    return result


def _total(proposal: Proposal) -> Any:
    return transactions._number(proposal.validation.calculation.total)


async def process_conversation(services: Services, job: Row) -> None:
    """Handle one process_conversation job from start to finish."""
    customer_id, conversation_id = job["customer_id"], job["subject_id"]
    started = time.perf_counter()
    stages: list[dict[str, Any]] = []

    def load() -> tuple[Row | None, list[Row]]:
        with services.database.tenant(customer_id) as conn:
            return _sources(conn, customer_id, conversation_id)

    record, sources = await asyncio.to_thread(load)
    if record is None:
        def drop() -> None:
            with services.database.tenant(customer_id) as conn:
                jobs.complete(conn, job, {"skipped": "conversation deleted"})

        await asyncio.to_thread(drop)
        return
    with log_context(conversation_id=conversation_id, stage="read"):
        conversation = await read_conversation(services, customer_id, sources, stages)
    with log_context(conversation_id=conversation_id, stage="interpret"):
        analysis = await pipeline.interpret(services.model, conversation)
    _record_stage(analysis.stage, stages)
    proposals = pipeline.propose(conversation, analysis.interpretation, policy=policy_from_settings(services.settings))
    await asyncio.to_thread(_save_result, services, job, conversation, analysis, proposals, stages, started)


def on_permanent_failure(conn: Connection, settings: Settings, job: Row, failure: Failure) -> None:
    """Show the failure on the transaction the customer is following."""
    placeholder = conn.execute(
        "SELECT * FROM chatreceipt.extracted_transactions WHERE conversation_id = %s AND state = 'processing' FOR NO KEY UPDATE",
        (job["subject_id"],),
    ).fetchone()
    if placeholder is not None:
        transactions.mark_failed(conn, settings, placeholder, code=failure.code, message=failure.message)
    metrics.inc("chatreceipt_extractions_total", outcome="failed", code=failure.code)
