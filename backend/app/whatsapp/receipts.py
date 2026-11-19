"""A saved record as the receipt a customer receives on WhatsApp: an image, and a PDF on request.

Rendering and uploading take seconds and use the network, so confirming a record only queues a
request. The sender turns it into a real message when its turn comes, which keeps replies in order.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.calculations import compute
from app.documents.browser import DocumentFormat
from app.documents.render import render_document_html
from app.models import TransactionDraft
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.messages import document_message, image_message
from app.whatsapp.store import Row

RECEIPT_FAILED = "Your record is saved, but I couldn't create the receipt file just now. Please try again in a few minutes."
RECORD_GONE = "That record is no longer available."

_MIME_TYPES: dict[str, str] = {"png": "image/png", "pdf": "application/pdf"}


class DocumentRenderer(Protocol):
    async def render(self, html: str, fmt: DocumentFormat) -> bytes: ...


async def receipt_message(
    record: Row, request: dict[str, Any], *, client: WhatsAppClient, renderer: DocumentRenderer
) -> dict[str, Any]:
    """Render the saved record, upload the file to WhatsApp and return the message that delivers it."""
    file_format = request["format"]
    draft = TransactionDraft.model_validate(record["draft"])
    html = render_document_html(
        draft,
        compute(draft),
        prepared_on=record["created_at"].date(),
        receipt={"reference": record["reference"], "version": None},
    )
    data = await renderer.render(html, file_format)
    filename = f"ChatReceipt-{record['reference']}.{file_format}"
    media_id = await client.upload_media(data, mime_type=_MIME_TYPES[file_format], filename=filename)
    if file_format == "pdf":
        return document_message(media_id, filename, request["caption"])
    return image_message(media_id, request["caption"])
