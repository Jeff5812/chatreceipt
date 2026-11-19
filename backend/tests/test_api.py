from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.documents.browser import DocumentRenderError
from app.extraction.provider import ErrorKind, ExtractionProviderError
from app.main import create_app
from tests.samples import FakeProvider, FakeRenderer, bag_conversation_output, image_bytes, make_settings, valid_draft


def build_client(provider: FakeProvider | None = None, renderer: FakeRenderer | None = None, **settings: Any) -> TestClient:
    app = create_app(
        settings=make_settings(**settings),
        provider=provider or FakeProvider(data=bag_conversation_output()),
        renderer=renderer or FakeRenderer(),
    )
    return TestClient(app)


def upload(*images: bytes) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", (f"shot{i}.png", data, "image/png")) for i, data in enumerate(images, start=1)]


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


def test_health() -> None:
    with build_client() as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_config() -> None:
    with build_client() as client:
        body = client.get("/api/config").json()
    currencies = {currency["code"]: currency for currency in body["currencies"]}
    assert currencies["NGN"]["symbol"] == "₦" and currencies["NGN"]["minor_units"] == 2
    assert currencies["JPY"]["minor_units"] == 0
    assert body["default_currency"] == "NGN"
    assert body["extraction_available"] is True
    assert body["limits"]["max_files"] == 10


def test_config_reports_when_extraction_is_unavailable() -> None:
    with build_client(provider=FakeProvider(configured=False)) as client:
        assert client.get("/api/config").json()["extraction_available"] is False


def test_calculate() -> None:
    with build_client() as client:
        response = client.post("/api/transactions/calculate", json=valid_draft())
    assert response.status_code == 200
    assert response.json()["total"] == "19000.00"


def test_malformed_requests_get_a_readable_error_without_echoing_values() -> None:
    with build_client() as client:
        response = client.post("/api/transactions/calculate", json={**valid_draft(), "secret_field": "do-not-echo"})
    assert response.status_code == 422
    assert error_code(response) == "invalid_request"
    assert "do-not-echo" not in response.text


def test_extraction_returns_a_draft_and_keeps_screenshot_order() -> None:
    provider = FakeProvider(data=bag_conversation_output())
    with build_client(provider=provider) as client:
        response = client.post("/api/extractions", files=upload(image_bytes(size=(400, 600)), image_bytes(size=(400, 900))))
    assert response.status_code == 200
    body = response.json()
    assert body["calculation"]["total"] == "19000.00"
    assert body["draft"]["line_items"][0]["unit_price"] == "17000"
    assert [image.height for image in provider.calls[0]] == [600, 900]
    assert [image.screenshot for image in provider.calls[0]] == [1, 2]


def test_extraction_unavailable_without_credentials() -> None:
    provider = FakeProvider(configured=False)
    with build_client(provider=provider) as client:
        response = client.post("/api/extractions", files=upload(image_bytes()))
    assert response.status_code == 503
    assert error_code(response) == "extraction_unavailable"
    assert provider.calls == []


def test_extraction_rejects_files_that_are_not_images() -> None:
    with build_client() as client:
        response = client.post("/api/extractions", files=upload(b"%PDF-1.4 not an image"))
    assert response.status_code == 415
    assert error_code(response) == "unsupported_type"
    assert response.json()["error"]["details"] == {"file_index": 1}


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        (ErrorKind.TIMEOUT, 504, "extraction_timeout"),
        (ErrorKind.RATE_LIMITED, 503, "extraction_busy"),
        (ErrorKind.UNAVAILABLE, 502, "extraction_failed"),
        (ErrorKind.REFUSED, 422, "extraction_declined"),
        (ErrorKind.MALFORMED, 502, "extraction_unreadable"),
    ],
)
def test_extraction_failures_are_explained(kind: ErrorKind, status: int, code: str) -> None:
    with build_client(provider=FakeProvider(error=ExtractionProviderError(kind))) as client:
        response = client.post("/api/extractions", files=upload(image_bytes()))
    assert response.status_code == status
    assert error_code(response) == code


def test_malformed_model_output_is_an_error() -> None:
    with build_client(provider=FakeProvider(data={"nope": 1})) as client:
        response = client.post("/api/extractions", files=upload(image_bytes()))
    assert response.status_code == 502
    assert error_code(response) == "extraction_unreadable"


def test_extraction_is_rate_limited() -> None:
    with build_client(extraction_rate_limit=2) as client:
        statuses = [client.post("/api/extractions", files=upload(image_bytes())).status_code for _ in range(3)]
        limited = client.post("/api/extractions", files=upload(image_bytes()))
    assert statuses == [200, 200, 429]
    assert int(limited.headers["retry-after"]) >= 1


def test_oversized_requests_are_rejected() -> None:
    with build_client(max_total_bytes=1000) as client:
        response = client.post("/api/extractions", files=upload(b"x" * (1024 * 1024 + 5000)))
    assert response.status_code == 413
    assert error_code(response) == "request_too_large"


def test_preview_shows_backend_numbers_and_the_verification_disclaimer() -> None:
    with build_client() as client:
        response = client.post("/api/documents/preview", json={"draft": valid_draft()})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "₦19,000.00" in html
    assert "Unpaid" in html
    assert "ChatReceipt doesn't verify payments" in html


def test_preview_escapes_user_text() -> None:
    with build_client() as client:
        html = client.post("/api/documents/preview", json={"draft": valid_draft(seller="<script>alert(1)</script>")}).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert" not in html


def test_documents_refuse_incomplete_drafts() -> None:
    renderer = FakeRenderer()
    draft = valid_draft(line_items=[{"id": "item-1", "description": "Bag", "quantity": "1", "unit_price": ""}])
    with build_client(renderer=renderer) as client:
        response = client.post("/api/documents/pdf", json={"draft": draft})
    assert response.status_code == 422
    assert error_code(response) == "draft_incomplete"
    assert response.json()["error"]["details"]["issues"][0]["field"] == "line_items.item-1.unit_price"
    assert renderer.calls == []


@pytest.mark.parametrize(("fmt", "media_type", "magic"), [("pdf", "application/pdf", b"%PDF"), ("png", "image/png", b"\x89PNG")])
def test_exports(fmt: str, media_type: str, magic: bytes) -> None:
    renderer = FakeRenderer()
    with build_client(renderer=renderer) as client:
        response = client.post(f"/api/documents/{fmt}", json={"draft": valid_draft(), "stats": {"from_extraction": True, "edited_fields": 2}})
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.headers["content-disposition"] == f'attachment; filename="chatreceipt-tola-bags-2026-11-01.{fmt}"'
    assert response.content.startswith(magic)
    assert renderer.calls[0][0] == fmt


def test_export_failures_are_reported() -> None:
    with build_client(renderer=FakeRenderer(error=DocumentRenderError("unavailable", "no browser"))) as client:
        response = client.post("/api/documents/pdf", json={"draft": valid_draft()})
    assert response.status_code == 503
    assert error_code(response) == "export_unavailable"


def test_cors_allows_only_the_configured_frontend() -> None:
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    with build_client() as client:
        allowed = client.options("/api/transactions/calculate", headers={"Origin": "http://localhost:3000", **preflight})
        denied = client.options("/api/transactions/calculate", headers={"Origin": "https://evil.example", **preflight})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in denied.headers


def test_unexpected_errors_return_json_with_cors(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(_: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.api.transactions.calculate", explode)
    with build_client() as client:
        response = client.post("/api/transactions/calculate", json=valid_draft(), headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 500
    assert error_code(response) == "internal_error"
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
