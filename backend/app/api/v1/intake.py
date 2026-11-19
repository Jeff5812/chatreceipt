"""Uploads, conversations and processing jobs."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.v1.deps import CustomerId, limit, run_tenant, write
from app.api.v1.presenters import job_view, upload_view
from app.api.v1.schemas import (
    ConversationAccepted,
    ConversationMessages,
    ConversationRequest,
    ConversationView,
    JobView,
    MessageView,
    ParticipantView,
    UploadList,
    UploadView,
)
from app.db.pool import Connection, DatabaseUnavailable
from app.engine import ingestion, jobs, transactions
from app.errors import APIError
from app.observability import log_event, metrics
from app.records.lifecycle import State
from app.storage import StorageError
from app.uploads import UploadRejected

router = APIRouter(prefix="/api/v1", tags=["Uploads and conversations"])

_NOT_FOUND = "That doesn't exist."


async def _store(request: Request, customer_id: UUID, filename: str, data: bytes) -> tuple[dict[str, Any], bool]:
    state = request.app.state
    try:
        return await run_in_threadpool(ingestion.store_file, state.database, state.storage, state.settings, customer_id, filename, data)
    except UploadRejected as exc:
        metrics.inc("chatreceipt_uploads_total", outcome="rejected")
        log_event("upload.rejected", code=exc.code)
        raise APIError(exc.status_code, exc.code, exc.message) from exc
    except (StorageError, DatabaseUnavailable) as exc:
        raise APIError(503, "temporarily_unavailable", "The file couldn't be saved right now. Please try again.") from exc


@router.post("/uploads", response_model=UploadList, status_code=201, dependencies=[Depends(limit("v1_uploads"))])
async def create_uploads(
    request: Request, customer_id: CustomerId, files: Annotated[list[UploadFile], File(description="Screenshots in conversation order, or one text export.")]
) -> UploadList:
    settings = request.app.state.settings
    if not 1 <= len(files) <= settings.max_files:
        raise APIError(400, "invalid_file_count", f"Upload between 1 and {settings.max_files} files at a time.")
    stored: list[UploadView] = []
    for upload in files:
        data = await upload.read(settings.max_file_bytes + 1)
        await upload.close()
        row, duplicate = await _store(request, customer_id, upload.filename or "", data)
        stored.append(upload_view(row, duplicate=duplicate))
    return UploadList(uploads=stored)


@router.get("/uploads/{upload_id}", response_model=UploadView)
async def get_upload(request: Request, upload_id: UUID, customer_id: CustomerId) -> UploadView:
    row = await run_tenant(request, customer_id, lambda conn: ingestion.get_upload(conn, customer_id, upload_id))
    if row is None:
        raise APIError(404, "not_found", _NOT_FOUND)
    return upload_view(row)


@router.delete("/uploads/{upload_id}", status_code=204)
async def delete_upload(request: Request, upload_id: UUID, customer_id: CustomerId) -> Response:
    """Delete the stored file now. What was already read from it stays on the record."""
    key = await run_tenant(request, customer_id, lambda conn: ingestion.mark_deleted(conn, customer_id, upload_id))
    if key is None:
        raise APIError(404, "not_found", _NOT_FOUND)
    await run_in_threadpool(request.app.state.storage.delete, key)
    return Response(status_code=204)


@router.post("/conversations", response_model=ConversationAccepted, status_code=202, dependencies=[Depends(limit("v1_processing"))])
async def create_conversation(request: Request, body: ConversationRequest, customer_id: CustomerId) -> Any:
    """Start reading a conversation. Follow the job, then review the transaction it produces."""
    settings = request.app.state.settings
    if bool(body.text) == bool(body.upload_ids):
        raise APIError(422, "invalid_sources", "Send either upload_ids or text.")
    upload_ids = list(body.upload_ids)
    if body.text:
        row, _ = await _store(request, customer_id, "pasted-conversation.txt", body.text.encode("utf-8"))
        upload_ids = [row["id"]]

    def start(conn: Connection) -> ConversationAccepted:
        conversation, transaction, job = ingestion.start_conversation(conn, settings, customer_id, upload_ids, body.title)
        return ConversationAccepted(conversation_id=conversation["id"], transaction_id=transaction["id"], job=job_view(job))

    return await write(request, customer_id, "create_conversation", body.model_dump(mode="json"), start, status_code=202)


@router.get("/conversations/{conversation_id}", response_model=ConversationView)
async def get_conversation(request: Request, conversation_id: UUID, customer_id: CustomerId) -> ConversationView:
    def load(conn: Connection) -> ConversationView:
        row = conn.execute(
            "SELECT * FROM chatreceipt.conversations WHERE id = %s AND customer_id = %s", (conversation_id, customer_id)
        ).fetchone()
        if row is None:
            raise APIError(404, "not_found", _NOT_FOUND)
        sources = conn.execute(
            "SELECT upload_id FROM chatreceipt.conversation_sources WHERE conversation_id = %s ORDER BY position", (conversation_id,)
        ).fetchall()
        linked = conn.execute(
            "SELECT id FROM chatreceipt.extracted_transactions WHERE conversation_id = %s ORDER BY created_at", (conversation_id,)
        ).fetchall()
        run = conn.execute(
            "SELECT id FROM chatreceipt.extraction_runs WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 1", (conversation_id,)
        ).fetchone()
        return ConversationView(
            id=row["id"],
            source_type=row["source_type"],
            title=row["title"],
            created_at=row["created_at"],
            upload_ids=[source["upload_id"] for source in sources],
            transaction_ids=[item["id"] for item in linked],
            latest_run_id=run["id"] if run else None,
        )

    return await run_tenant(request, customer_id, load)


@router.get("/conversations/{conversation_id}/messages", response_model=ConversationMessages)
async def get_messages(request: Request, conversation_id: UUID, customer_id: CustomerId) -> ConversationMessages:
    """The conversation as ChatReceipt read it: who said what, in order, and where each message came from."""

    def load(conn: Connection) -> ConversationMessages:
        exists = conn.execute(
            "SELECT 1 FROM chatreceipt.conversations WHERE id = %s AND customer_id = %s", (conversation_id, customer_id)
        ).fetchone()
        if exists is None:
            raise APIError(404, "not_found", _NOT_FOUND)
        run = conn.execute(
            "SELECT id FROM chatreceipt.extraction_runs WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 1", (conversation_id,)
        ).fetchone()
        if run is None:
            return ConversationMessages(run_id=None, participants=[], messages=[])
        people = conn.execute(
            "SELECT * FROM chatreceipt.conversation_participants WHERE run_id = %s ORDER BY participant_key", (run["id"],)
        ).fetchall()
        messages = conn.execute("SELECT * FROM chatreceipt.conversation_messages WHERE run_id = %s ORDER BY seq", (run["id"],)).fetchall()
        return ConversationMessages(
            run_id=run["id"],
            participants=[
                ParticipantView(
                    key=p["participant_key"], name=p["name"], side=p["side"], role=p["role"], role_confidence=p["role_confidence"], role_evidence=p["role_evidence"]
                )
                for p in people
            ],
            messages=[
                MessageView(
                    key=m["message_key"], seq=m["seq"], participant=m["participant_key"], kind=m["kind"], text=m["text"],
                    original_text=m["original_text"], time=m["time_label"], day=m["day_label"], legibility=m["legibility"],
                    source_kind=m["source_kind"], screenshot=m["source_position"],
                )
                for m in messages
            ],
        )

    return await run_tenant(request, customer_id, load)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(request: Request, conversation_id: UUID, customer_id: CustomerId) -> Response:
    """Delete the conversation and everything read from it. Approved records keep their figures."""

    def remove(conn: Connection) -> bool:
        active = conn.execute(
            "SELECT 1 FROM chatreceipt.processing_jobs WHERE subject_id = %s AND status IN ('queued', 'running', 'retrying')", (conversation_id,)
        ).fetchone()
        if active:
            raise APIError(409, "still_processing", "This conversation is still being read. Try again when it has finished.")
        return conn.execute(
            "DELETE FROM chatreceipt.conversations WHERE id = %s AND customer_id = %s", (conversation_id, customer_id)
        ).rowcount == 1

    if not await run_tenant(request, customer_id, remove):
        raise APIError(404, "not_found", _NOT_FOUND)
    return Response(status_code=204)


@router.get("/jobs/{job_id}", response_model=JobView, tags=["Jobs"])
async def get_job(request: Request, job_id: UUID, customer_id: CustomerId) -> JobView:
    row = await run_tenant(request, customer_id, lambda conn: jobs.get(conn, customer_id, job_id))
    if row is None:
        raise APIError(404, "not_found", _NOT_FOUND)
    return job_view(row)


@router.post("/jobs/{job_id}/cancel", response_model=JobView, tags=["Jobs"])
async def cancel_job(request: Request, job_id: UUID, customer_id: CustomerId) -> Any:
    settings = request.app.state.settings

    def cancel(conn: Connection) -> JobView:
        job = jobs.get(conn, customer_id, job_id)
        if job is None:
            raise APIError(404, "not_found", _NOT_FOUND)
        cancelled = jobs.cancel(conn, customer_id, job_id)
        if cancelled is None:
            raise APIError(409, "job_not_cancellable", "This job has already started or finished.", details={"status": job["status"]})
        if job["kind"] == jobs.PROCESS_CONVERSATION:
            row = conn.execute(
                "SELECT * FROM chatreceipt.extracted_transactions WHERE conversation_id = %s AND state = %s FOR NO KEY UPDATE",
                (job["subject_id"], State.PROCESSING.value),
            ).fetchone()
            if row is not None:
                transactions.mark_failed(conn, settings, row, code="cancelled", message="Reading this conversation was cancelled.")
        return job_view(cancelled)

    return await write(request, customer_id, "cancel_job", {"job_id": str(job_id)}, cancel)
