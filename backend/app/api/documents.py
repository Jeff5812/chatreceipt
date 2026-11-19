"""Transaction record preview (HTML) and exports (PDF, PNG).

Every request recalculates from the submitted draft, so a document can only ever show the
backend's numbers, and a draft with problems is refused.
"""

from __future__ import annotations

import time
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.api.deps import rate_limit
from app.calculations import Computation, compute
from app.documents.browser import DocumentFormat, DocumentRenderError
from app.documents.render import DOCUMENT_CSP, document_filename, render_document_html
from app.errors import APIError
from app.models import DocumentRequest
from app.observability import log_event

router = APIRouter(dependencies=[Depends(rate_limit("documents"))])

_MEDIA_TYPES: dict[DocumentFormat, str] = {"pdf": "application/pdf", "png": "image/png"}


def _exportable(body: DocumentRequest) -> Computation:
    computation = compute(body.draft)
    if not computation.can_export:
        problems = [issue.model_dump() for issue in computation.issues if issue.severity == "error"]
        raise APIError(
            422,
            "draft_incomplete",
            "Fix the highlighted details before creating the record.",
            details={"issues": problems},
        )
    return computation


@router.post("/api/documents/preview", response_class=HTMLResponse)
async def preview_document(body: DocumentRequest) -> HTMLResponse:
    computation = _exportable(body)
    html = render_document_html(body.draft, computation, prepared_on=date.today())
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": DOCUMENT_CSP})


async def _export(request: Request, body: DocumentRequest, fmt: DocumentFormat) -> Response:
    computation = _exportable(body)
    html = render_document_html(body.draft, computation, prepared_on=date.today())
    started = time.perf_counter()
    try:
        data = await request.app.state.renderer.render(html, fmt)
    except DocumentRenderError as exc:
        log_event("document.failed", format=fmt, kind=exc.kind, detail=exc.detail)
        raise APIError(
            503,
            "export_unavailable",
            f"The {fmt.upper()} couldn't be created right now. Please try again in a moment.",
        ) from exc
    stats = body.stats
    log_event(
        "document.exported",
        format=fmt,
        duration_ms=round((time.perf_counter() - started) * 1000),
        bytes=len(data),
        line_items=len(body.draft.line_items),
        from_extraction=stats.from_extraction if stats else None,
        edited_fields=stats.edited_fields if stats else None,
        added_fields=stats.added_fields if stats else None,
    )
    filename = document_filename(body.draft, computation, fmt)
    return Response(
        data,
        media_type=_MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.post("/api/documents/pdf")
async def export_pdf(request: Request, body: DocumentRequest) -> Response:
    return await _export(request, body, "pdf")


@router.post("/api/documents/png")
async def export_png(request: Request, body: DocumentRequest) -> Response:
    return await _export(request, body, "png")
