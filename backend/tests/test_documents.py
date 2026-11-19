from __future__ import annotations

import asyncio
import os
from datetime import date
from html import unescape
from typing import Any

import pytest

from app.calculations import compute
from app.documents.browser import BrowserRenderer, DocumentRenderError
from app.documents.render import build_view, document_filename, render_document_html
from app.models import TransactionDraft
from tests.samples import valid_draft


def render(data: dict[str, Any]) -> str:
    draft = TransactionDraft.model_validate(data)
    return render_document_html(draft, compute(draft), prepared_on=date(2026, 11, 8))


def test_record_shows_backend_totals_terms_and_disclaimer() -> None:
    html = render(valid_draft())
    for expected in (
        "Transaction record",
        "Tola Bags",
        "Ada",
        "1 November 2026",
        "Prepared 8 November 2026",
        "₦17,000.00",
        "₦2,000.00",
        "₦19,000.00",
        "Unpaid",
        "Recorded from the conversation. ChatReceipt doesn't verify payments.",
        "Buyer says they will transfer tomorrow.",
    ):
        assert expected in html


def test_missing_details_say_not_specified() -> None:
    html = render(valid_draft(seller="", buyer=""))
    assert html.count("Not specified") >= 2


def test_adjustments_are_labelled() -> None:
    html = render(
        valid_draft(
            discounts=[{"id": "d1", "label": "Promo", "kind": "percent", "value": "10"}],
            additional_fees=[{"id": "f1", "label": "", "kind": "amount", "value": "300"}],
        )
    )
    assert "Promo (10%)" in html
    assert "−₦1,700.00" in html
    assert "Additional fee" in html


def test_document_is_self_contained() -> None:
    html = render(valid_draft())
    assert "font-family:'Space Grotesk'" in html
    assert "font-family:'Tiny5'" in html
    assert "font-family:'IBM Plex Mono'" in html
    assert "default-src 'none'" in unescape(html)  # the CSP meta tag (quotes are escaped in the attribute)
    assert "http://" not in html and "https://" not in html


def test_incomplete_calculations_cannot_be_rendered() -> None:
    draft = TransactionDraft.model_validate(
        valid_draft(line_items=[{"id": "item-1", "description": "Bag", "quantity": "1", "unit_price": ""}])
    )
    with pytest.raises(ValueError):
        build_view(draft, compute(draft), date(2026, 11, 8))


def test_filename_is_safe_ascii() -> None:
    draft = TransactionDraft.model_validate(valid_draft(seller="Tọlá's Bags & Co."))
    assert document_filename(draft, compute(draft), "pdf") == "chatreceipt-tola-s-bags-co-2026-11-01.pdf"


def test_real_browser_prints_pdf_and_png() -> None:
    """Uses the installed browser (BROWSER_CHANNEL, default msedge); skipped if none is available."""
    renderer = BrowserRenderer(channel=os.environ.get("BROWSER_CHANNEL", "msedge"), timeout_seconds=90)
    html = render(valid_draft())
    try:
        pdf = asyncio.run(renderer.render(html, "pdf"))
        png = asyncio.run(renderer.render(html, "png"))
    except DocumentRenderError as exc:
        if exc.kind == "unavailable":
            pytest.skip(f"no browser available: {exc}")
        raise
    finally:
        renderer.close()
    assert pdf.startswith(b"%PDF")
    assert png.startswith(b"\x89PNG")
