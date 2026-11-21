"""Shared test doubles and sample data."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image

from app.config import Settings
from app.extraction.provider import ProviderResponse, ProviderUsage
from app.uploads import PreparedImage


class FakeProvider:
    name = "fake"

    def __init__(self, data: Any = None, error: Exception | None = None, configured: bool = True) -> None:
        self.data = data
        self.error = error
        self._configured = configured
        self.calls: list[list[PreparedImage]] = []
        self.requests: list[Any] = []  # requests made through generate(), such as chat exports

    @property
    def configured(self) -> bool:
        return self._configured

    async def extract(self, images: Any) -> ProviderResponse:
        self.calls.append(list(images))
        if self.error is not None:
            raise self.error
        return ProviderResponse(
            data=self.data,
            usage=ProviderUsage(model="fake-model", input_tokens=1200, output_tokens=800),
            duration_ms=3,
        )

    async def generate(self, request: Any) -> ProviderResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ProviderResponse(data=self.data, usage=ProviderUsage(model="fake-model", input_tokens=900, output_tokens=700), duration_ms=3)

    async def aclose(self) -> None:
        return None


class FakeRenderer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def render(self, html: str, fmt: str) -> bytes:
        self.calls.append((fmt, html))
        if self.error is not None:
            raise self.error
        return b"%PDF-1.7\n%fake" if fmt == "pdf" else b"\x89PNG\r\n\x1a\nfake"

    def close(self) -> None:
        return None


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "anthropic_api_key": "test-key",
        "extraction_rate_limit": 50,
        "document_rate_limit": 50,
        "calculate_rate_limit": 500,
        "browser_channel": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (400, 800), *, noise: bool = False, mode: str = "RGB", **save: Any) -> bytes:
    if noise:
        image = Image.effect_noise(size, 64).convert(mode)
    elif mode == "RGBA":
        image = Image.new("RGBA", size, (200, 120, 40, 128))
    else:
        image = Image.new(mode, size, "#e8e2d6" if mode == "RGB" else 0)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **save)
    return buffer.getvalue()


def prepared_image(screenshot: int = 1, part: int = 1, parts: int = 1) -> PreparedImage:
    return PreparedImage(
        screenshot=screenshot, part=part, parts=parts, media_type="image/jpeg", data=b"jpeg", width=10, height=10
    )


def field(
    value: str = "",
    *,
    confidence: str = "high",
    quote: str | None = None,
    screenshot: int = 1,
    note: str = "",
    found: bool | None = None,
) -> dict[str, Any]:
    is_found = bool(value) if found is None else found
    evidence = [{"screenshot": screenshot, "quote": quote}] if is_found and quote else []
    return {"found": is_found, "value": value, "confidence": confidence, "evidence": evidence, "note": note}


def line_item(
    description: str,
    quantity: str,
    unit_price: str,
    *,
    confidence: str = "high",
    quote: str | None = None,
    screenshot: int = 1,
    note: str = "",
) -> dict[str, Any]:
    return {
        "description": description,
        "quantity": quantity,
        "unit_price": unit_price,
        "confidence": confidence,
        "evidence": [{"screenshot": screenshot, "quote": quote}] if quote else [],
        "note": note,
    }


def adjustment(label: str, value: str, kind: str = "amount", *, confidence: str = "high") -> dict[str, Any]:
    return {"label": label, "kind": kind, "value": value, "confidence": confidence, "evidence": [], "note": ""}


def bag_conversation_output(**overrides: Any) -> dict[str, Any]:
    """The example in docs/ai-extraction-spec.md, as a correct extraction reports it.

    Seller: "The bag is ₦18,000."   Customer: "Can you do 15k?"   Seller: "17k final."
    Customer: "Okay, I will take it."   Seller: "Delivery is 2k."   Customer: "I will transfer tomorrow."
    """
    data: dict[str, Any] = {
        "contains_transaction": True,
        "summary": "The customer agreed to buy a bag for ₦17,000 plus ₦2,000 delivery and will transfer tomorrow.",
        "currency": field("NGN", quote="The bag is ₦18,000."),
        "seller": field(""),
        "buyer": field(""),
        "transaction_date": field(""),
        "due_date": field(""),
        "line_items": [line_item("Bag", "1", "17000", quote="17k final.")],
        "discounts": [],
        "additional_fees": [],
        "delivery_fee": field("2000", quote="Delivery is 2k."),
        "agreed_total": field(""),
        "amount_paid": field(""),
        "outstanding_amount": field(""),
        "payment_status": field("unpaid", quote="I will transfer tomorrow."),
        "payment_terms": field("Customer says they will transfer tomorrow.", quote="I will transfer tomorrow."),
        "delivery_terms": field(""),
        "notes": field(""),
        "ambiguities": [],
        "review_required": False,
    }
    data.update(overrides)
    return data


def valid_draft(**overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "currency": "NGN",
        "seller": "Tola Bags",
        "buyer": "Ada",
        "transaction_date": "2026-11-01",
        "line_items": [{"id": "item-1", "description": "Brown leather bag", "quantity": "1", "unit_price": "17000"}],
        "delivery_fee": "2000",
        "payment_status": "unpaid",
        "payment_terms": "Buyer says they will transfer tomorrow.",
    }
    draft.update(overrides)
    return draft
