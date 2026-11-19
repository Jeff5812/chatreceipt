from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.extraction import gemini
from app.extraction.claude import ClaudeProvider
from app.extraction.gemini import GeminiProvider, build_request, map_http_error, parse_response
from app.extraction.prompt import SYSTEM_PROMPT
from app.extraction.provider import ErrorKind, ExtractionProviderError, StructuredRequest, TextPart
from app.extraction.providers import create_provider
from app.extraction.schema import EXTRACTION_SCHEMA
from tests.samples import bag_conversation_output, make_settings, prepared_image


def gemini_settings(**overrides: Any):
    values: dict[str, Any] = {"anthropic_api_key": None, "gemini_api_key": "gemini-test-key", "extraction_provider": "gemini"}
    values.update(overrides)
    return make_settings(**values)


def answer(data: Any, *, finish: str = "STOP", thought: bool = False, usage: dict[str, int] | None = None) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if thought:
        parts.append({"text": "thinking about prices", "thought": True})
    parts.append({"text": data if isinstance(data, str) else json.dumps(data)})
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "usageMetadata": usage or {"promptTokenCount": 1500, "candidatesTokenCount": 400, "thoughtsTokenCount": 300},
        "modelVersion": "gemini-3.6-flash",
        "responseId": "resp-1",
    }


def test_request_shape() -> None:
    images = [prepared_image(1), prepared_image(2, part=1, parts=2), prepared_image(2, part=2, parts=2)]
    request = build_request(images, gemini_settings())
    assert request["systemInstruction"]["parts"][0]["text"] == SYSTEM_PROMPT
    config = request["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == EXTRACTION_SCHEMA
    assert "thinkingConfig" not in config
    parts = request["contents"][0]["parts"]
    labels = [part["text"] for part in parts if "text" in part]
    assert labels[1:4] == ["Screenshot 1", "Screenshot 2, part 1 of 2", "Screenshot 2, part 2 of 2"]
    images_sent = [part["inline_data"] for part in parts if "inline_data" in part]
    assert len(images_sent) == 3
    assert all(image["mime_type"] == "image/jpeg" and image["data"] for image in images_sent)


def test_thinking_level_is_sent_when_set() -> None:
    request = build_request([prepared_image(1)], gemini_settings(gemini_thinking_level="high"))
    assert request["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}


def test_reads_json_and_ignores_thought_parts() -> None:
    data = bag_conversation_output()
    response = parse_response(answer(data, thought=True), 25)
    assert response.data == data
    assert response.usage.model == "gemini-3.6-flash"
    assert response.request_id == "resp-1"
    assert response.duration_ms == 25


def test_usage_counts_thinking_as_output_and_cached_tokens_separately() -> None:
    usage = {"promptTokenCount": 2000, "cachedContentTokenCount": 500, "candidatesTokenCount": 400, "thoughtsTokenCount": 100}
    response = parse_response(answer(bag_conversation_output(), usage=usage), 1)
    assert (response.usage.input_tokens, response.usage.cache_read_tokens, response.usage.output_tokens) == (1500, 500, 500)


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ({"promptFeedback": {"blockReason": "SAFETY"}}, ErrorKind.REFUSED),
        (answer({}, finish="PROHIBITED_CONTENT"), ErrorKind.REFUSED),
        (answer({}, finish="MAX_TOKENS"), ErrorKind.MALFORMED),
        ({"candidates": []}, ErrorKind.MALFORMED),
        (answer("   "), ErrorKind.MALFORMED),
        (answer('{"contains_transaction": tr'), ErrorKind.MALFORMED),
    ],
)
def test_unusable_answers_raise(body: dict[str, Any], kind: ErrorKind) -> None:
    with pytest.raises(ExtractionProviderError) as raised:
        parse_response(body, 1)
    assert raised.value.kind is kind


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (400, {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key."}}, ErrorKind.NOT_CONFIGURED),
        (403, {"error": {"status": "PERMISSION_DENIED", "message": "denied"}}, ErrorKind.NOT_CONFIGURED),
        (400, {"error": {"status": "FAILED_PRECONDITION", "message": "not available"}}, ErrorKind.NOT_CONFIGURED),
        (429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}}, ErrorKind.RATE_LIMITED),
        (400, {"error": {"status": "INVALID_ARGUMENT", "message": "bad schema"}}, ErrorKind.BAD_REQUEST),
        (404, {"error": {"status": "NOT_FOUND", "message": "model not found"}}, ErrorKind.BAD_REQUEST),
        (500, None, ErrorKind.UNAVAILABLE),
    ],
)
def test_http_errors_map_to_kinds(status: int, body: Any, kind: ErrorKind) -> None:
    assert map_http_error(status, body).kind is kind


def run(provider: GeminiProvider, images: Any) -> Any:
    async def go() -> Any:
        try:
            return await provider.extract(images)
        finally:
            await provider.aclose()

    return asyncio.run(go())


def test_extract_sends_the_key_in_a_header_and_parses_the_answer() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["query"] = str(request.url.query)
        return httpx.Response(200, json=answer(bag_conversation_output()))

    provider = GeminiProvider(gemini_settings(gemini_model="gemini-3.5-flash"), transport=httpx.MockTransport(handler))
    response = run(provider, [prepared_image(1)])
    assert response.data == bag_conversation_output()
    assert seen["path"] == "/v1beta/models/gemini-3.5-flash:generateContent"
    assert seen["key"] == "gemini-test-key"
    assert "key=" not in seen["query"]  # never in the URL, where it could end up in logs


def test_retries_once_when_the_provider_is_overloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini, "_RETRY_DELAY_SECONDS", 0)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"status": "UNAVAILABLE", "message": "overloaded"}})
        return httpx.Response(200, json=answer(bag_conversation_output()))

    run(GeminiProvider(gemini_settings(), transport=httpx.MockTransport(handler)), [prepared_image(1)])
    assert len(calls) == 2


def test_errors_after_the_retry_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini, "_RETRY_DELAY_SECONDS", 0)
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="overloaded"))
    with pytest.raises(ExtractionProviderError) as raised:
        run(GeminiProvider(gemini_settings(), transport=transport), [prepared_image(1)])
    assert raised.value.kind is ErrorKind.UNAVAILABLE


def test_a_stalled_model_hands_over_to_the_fallback() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "gemini-3.5-flash" in request.url.path:
            raise httpx.ReadTimeout("stalled", request=request)
        return httpx.Response(200, json=answer(bag_conversation_output()))

    settings = gemini_settings(gemini_model="gemini-3.5-flash", gemini_fallback_model="gemini-2.5-flash", gemini_primary_timeout_seconds=5)
    provider = GeminiProvider(settings, transport=httpx.MockTransport(handler))
    response = run(provider, [prepared_image(1)])
    assert response.usage.fallback_used is True
    assert paths == ["/v1beta/models/gemini-3.5-flash:generateContent", "/v1beta/models/gemini-2.5-flash:generateContent"]


def test_with_a_fallback_an_overloaded_model_is_not_retried_first() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "gemini-3.5-flash" in request.url.path:
            return httpx.Response(503, json={"error": {"status": "UNAVAILABLE", "message": "overloaded"}})
        return httpx.Response(200, json=answer(bag_conversation_output()))

    settings = gemini_settings(gemini_model="gemini-3.5-flash", gemini_fallback_model="gemini-2.5-flash")
    run(GeminiProvider(settings, transport=httpx.MockTransport(handler)), [prepared_image(1)])
    assert [path.split("/")[-1] for path in paths] == ["gemini-3.5-flash:generateContent", "gemini-2.5-flash:generateContent"]


def test_a_rate_limited_model_hands_over_to_the_fallback() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "gemini-2.5-flash" in request.url.path:
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}})
        return httpx.Response(200, json=answer(bag_conversation_output()))

    settings = gemini_settings(gemini_model="gemini-2.5-flash", gemini_fallback_model="gemini-3.5-flash")
    response = run(GeminiProvider(settings, transport=httpx.MockTransport(handler)), [prepared_image(1)])
    assert response.usage.fallback_used is True and len(paths) == 2


def test_a_light_text_task_goes_to_the_light_model_first() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path.split("/")[-1])
        if "lite" in request.url.path:
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}})
        return httpx.Response(200, json=answer({"intent": "help"}))

    settings = gemini_settings(gemini_model="gemini-2.5-flash", gemini_light_model="gemini-2.5-flash-lite")
    request = StructuredRequest(task="whatsapp_correction", system="s", parts=[TextPart("hi")], schema={"type": "object"}, light=True)

    async def go() -> Any:
        provider = GeminiProvider(settings, transport=httpx.MockTransport(handler))
        try:
            return await provider.generate(request)
        finally:
            await provider.aclose()

    response = asyncio.run(go())
    assert paths == ["gemini-2.5-flash-lite:generateContent", "gemini-2.5-flash:generateContent"]
    assert response.data == {"intent": "help"}


def test_timeouts_and_connection_failures() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    for handler, kind in ((timeout, ErrorKind.TIMEOUT), (refused, ErrorKind.UNAVAILABLE)):
        with pytest.raises(ExtractionProviderError) as raised:
            run(GeminiProvider(gemini_settings(), transport=httpx.MockTransport(handler)), [prepared_image(1)])
        assert raised.value.kind is kind


def test_unconfigured_provider_never_calls_the_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the API must not be called without a key")

    provider = GeminiProvider(gemini_settings(gemini_api_key=None), transport=httpx.MockTransport(handler))
    assert provider.configured is False
    with pytest.raises(ExtractionProviderError) as raised:
        run(provider, [prepared_image(1)])
    assert raised.value.kind is ErrorKind.NOT_CONFIGURED


def test_provider_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    auto = {"extraction_provider": "auto"}
    assert isinstance(create_provider(make_settings(**auto)), ClaudeProvider)  # Anthropic key set
    assert isinstance(create_provider(make_settings(**auto, gemini_api_key="g")), ClaudeProvider)  # Anthropic preferred
    assert isinstance(create_provider(make_settings(**auto, anthropic_api_key=None, gemini_api_key="g")), GeminiProvider)
    neither = create_provider(make_settings(**auto, anthropic_api_key=None))
    assert isinstance(neither, ClaudeProvider) and neither.configured is False
    assert isinstance(create_provider(make_settings(extraction_provider="gemini", gemini_api_key="g")), GeminiProvider)
    assert isinstance(create_provider(make_settings(extraction_provider="anthropic", anthropic_api_key=None, gemini_api_key="g")), ClaudeProvider)


def test_unknown_provider_setting_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_settings(extraction_provider="openai")


def test_cost_estimate_uses_the_active_providers_prices() -> None:
    from app.extraction.provider import ProviderUsage
    from app.extraction.service import estimate_cost_usd

    usage = ProviderUsage(model="m", input_tokens=1_000_000, output_tokens=1_000_000)
    settings = make_settings(gemini_input_price_per_mtok=0.5, gemini_output_price_per_mtok=3.0)
    assert estimate_cost_usd(usage, settings, "gemini") == pytest.approx(3.5)
    assert estimate_cost_usd(usage, settings, "anthropic") == pytest.approx(24.0)
