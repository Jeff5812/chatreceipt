from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from app.extraction.claude import ClaudeProvider, build_request, map_api_error, parse_message
from app.extraction.provider import ErrorKind, ExtractionProviderError
from tests.samples import bag_conversation_output, make_settings, prepared_image


def message(*blocks: Any, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=None,
        content=list(blocks),
        model="claude-opus-5-5",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0, iterations=None
        ),
    )


def text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def test_request_shape() -> None:
    images = [prepared_image(1), prepared_image(2, part=1, parts=2), prepared_image(2, part=2, parts=2)]
    request = build_request(images, make_settings(extraction_effort="high"))
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["effort"] == "high"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    content = request["messages"][0]["content"]
    labels = [block["text"] for block in content if block["type"] == "text"]
    assert labels[1:4] == ["Screenshot 1", "Screenshot 2, part 1 of 2", "Screenshot 2, part 2 of 2"]
    assert sum(block["type"] == "image" for block in content) == 3
    # Thinking can't be disabled on this model and sampling parameters are rejected.
    assert not {"thinking", "temperature", "top_p", "top_k"} & set(request)


def test_reads_json_after_thinking() -> None:
    data = bag_conversation_output()
    response = parse_message(message(SimpleNamespace(type="thinking", thinking=""), text(json.dumps(data))), 10)
    assert response.data == data
    assert response.usage.fallback_used is False


def test_uses_only_text_after_the_last_fallback_block() -> None:
    data = bag_conversation_output()
    response = parse_message(message(text('{"partial": '), SimpleNamespace(type="fallback"), text(json.dumps(data))), 10)
    assert response.data == data
    assert response.usage.fallback_used is True


@pytest.mark.parametrize(
    ("msg", "kind"),
    [
        (message(stop_reason="refusal"), ErrorKind.REFUSED),
        (message(text('{"cut off'), stop_reason="max_tokens"), ErrorKind.MALFORMED),
        (message(text("not json")), ErrorKind.MALFORMED),
        (message(), ErrorKind.MALFORMED),
    ],
)
def test_unusable_messages_raise(msg: SimpleNamespace, kind: ErrorKind) -> None:
    with pytest.raises(ExtractionProviderError) as caught:
        parse_message(msg, 10)
    assert caught.value.kind is kind


def test_api_errors_map_to_kinds() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
        return cls("error", response=httpx2.Response(code, request=request), body=None)

    assert map_api_error(status_error(anthropic.AuthenticationError, 401)).kind is ErrorKind.NOT_CONFIGURED
    assert map_api_error(status_error(anthropic.RateLimitError, 429)).kind is ErrorKind.RATE_LIMITED
    assert map_api_error(status_error(anthropic.BadRequestError, 400)).kind is ErrorKind.BAD_REQUEST
    assert map_api_error(status_error(anthropic.InternalServerError, 500)).kind is ErrorKind.UNAVAILABLE
    assert map_api_error(anthropic.APITimeoutError(request=request)).kind is ErrorKind.TIMEOUT
    assert map_api_error(anthropic.APIConnectionError(request=request)).kind is ErrorKind.UNAVAILABLE


def test_unconfigured_provider_never_calls_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    provider = ClaudeProvider(make_settings(anthropic_api_key=None))
    assert provider.configured is False
    with pytest.raises(ExtractionProviderError) as caught:
        asyncio.run(provider.extract([prepared_image()]))
    assert caught.value.kind is ErrorKind.NOT_CONFIGURED
