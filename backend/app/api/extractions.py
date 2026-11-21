"""POST /api/extractions: screenshots in, an editable transaction draft out. /chat does the same for a WhatsApp export."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.deps import rate_limit
from app.errors import APIError
from app.extraction.provider import ErrorKind, ExtractionProviderError
from app.chat_import import read_chat_export
from app.extraction.chat import run_chat_extraction
from app.extraction.service import run_extraction
from app.models import ExtractionResponse
from app.observability import log_event
from app.uploads import UploadedFile, UploadRejected, prepare_screenshots

router = APIRouter()

_FALLBACK = "You can enter the details yourself instead."
_PROVIDER_ERRORS: dict[ErrorKind, tuple[int, str, str]] = {
    ErrorKind.NOT_CONFIGURED: (503, "extraction_unavailable", f"Reading screenshots isn't available right now. {_FALLBACK}"),
    ErrorKind.TIMEOUT: (504, "extraction_timeout", f"Reading the screenshots took too long. Try again, or enter the details yourself."),
    ErrorKind.RATE_LIMITED: (503, "extraction_busy", "The service that reads screenshots is busy. Please try again in a minute."),
    ErrorKind.UNAVAILABLE: (502, "extraction_failed", "The service that reads screenshots couldn't be reached. Please try again shortly."),
    ErrorKind.REFUSED: (422, "extraction_declined", f"These screenshots couldn't be processed. Try different screenshots. {_FALLBACK}"),
    ErrorKind.MALFORMED: (502, "extraction_unreadable", "The details couldn't be read from these screenshots. Try again, or enter them yourself."),
    ErrorKind.BAD_REQUEST: (502, "extraction_failed", "Something went wrong while reading the screenshots. Try again, or enter the details yourself."),
}


@router.post(
    "/api/extractions",
    response_model=ExtractionResponse,
    dependencies=[Depends(rate_limit("extraction"))],
)
async def create_extraction(
    request: Request,
    files: Annotated[list[UploadFile], File(description="Screenshots, in conversation order.")],
) -> ExtractionResponse:
    settings = request.app.state.settings
    provider = request.app.state.provider
    if not provider.configured:
        status, code, message = _PROVIDER_ERRORS[ErrorKind.NOT_CONFIGURED]
        raise APIError(status, code, message)

    uploads: list[UploadedFile] = []
    for upload in files:
        uploads.append(UploadedFile(name=upload.filename or "", data=await upload.read()))
        await upload.close()

    try:
        images = await run_in_threadpool(
            prepare_screenshots,
            uploads,
            max_files=settings.max_files,
            max_file_bytes=settings.max_file_bytes,
            max_total_bytes=settings.max_total_bytes,
        )
    except UploadRejected as exc:
        log_event("upload.rejected", code=exc.code, file_index=exc.file_index, files=len(uploads))
        raise APIError(exc.status_code, exc.code, exc.message, details={"file_index": exc.file_index}) from exc
    finally:
        uploads.clear()  # drop the raw bytes as soon as they're no longer needed

    try:
        return await run_extraction(provider, images, settings)
    except ExtractionProviderError as exc:
        status, code, message = _PROVIDER_ERRORS[exc.kind]
        raise APIError(status, code, message) from exc


_CHAT_ERRORS: dict[ErrorKind, tuple[int, str, str]] = {
    ErrorKind.NOT_CONFIGURED: (503, "extraction_unavailable", f"Reading chats isn't available right now. {_FALLBACK}"),
    ErrorKind.TIMEOUT: (504, "extraction_timeout", "Reading the chat took too long. Try again, or enter the details yourself."),
    ErrorKind.RATE_LIMITED: (503, "extraction_busy", "The service that reads chats is busy. Please try again in a minute."),
    ErrorKind.UNAVAILABLE: (502, "extraction_failed", "The service that reads chats couldn't be reached. Please try again shortly."),
    ErrorKind.REFUSED: (422, "extraction_declined", f"This chat couldn't be processed. {_FALLBACK}"),
    ErrorKind.MALFORMED: (502, "extraction_unreadable", "The details couldn't be read from this chat. Try again, or enter them yourself."),
    ErrorKind.BAD_REQUEST: (502, "extraction_failed", "Something went wrong while reading the chat. Try again, or enter the details yourself."),
}


@router.post(
    "/api/extractions/chat",
    response_model=ExtractionResponse,
    dependencies=[Depends(rate_limit("extraction"))],
)
async def create_chat_extraction(
    request: Request,
    file: Annotated[UploadFile, File(description="A WhatsApp chat export: the .txt file, or the .zip with media.")],
    focus: Annotated[str, Form(max_length=300, description="Another deal in the same chat to make the record for.")] = "",
) -> ExtractionResponse:
    """A WhatsApp chat export in, the same editable draft out, with who's who and any other deals."""
    settings = request.app.state.settings
    provider = request.app.state.provider
    if not provider.configured:
        status, code, message = _CHAT_ERRORS[ErrorKind.NOT_CONFIGURED]
        raise APIError(status, code, message)
    limit = settings.max_chat_export_bytes
    data = await file.read(limit + 1)
    name = file.filename or ""
    await file.close()
    if len(data) > limit:
        megabytes = limit // (1024 * 1024)
        raise APIError(413, "file_too_large", f"This export is larger than {megabytes} MB. Export the chat again without media, or send just the .txt file.")
    try:
        export = await run_in_threadpool(
            read_chat_export, data, name, max_text_bytes=settings.max_chat_text_bytes, max_file_bytes=settings.max_file_bytes
        )
    except UploadRejected as exc:
        log_event("upload.rejected", code=exc.code, source="chat_export")
        raise APIError(exc.status_code, exc.code, exc.message) from exc
    try:
        return await run_chat_extraction(provider, export, settings, focus=focus.strip())
    except ExtractionProviderError as exc:
        status, code, message = _CHAT_ERRORS[exc.kind]
        raise APIError(status, code, message) from exc
