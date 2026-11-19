"""Claude implementation of the extraction provider (Anthropic Python SDK)."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any

import anthropic

from app.config import Settings
from app.extraction.prompt import TextPart, extraction_request
from app.extraction.provider import (
    ErrorKind,
    ExtractionProviderError,
    ProviderResponse,
    ProviderUsage,
    StructuredRequest,
)
from app.uploads import PreparedImage

# Server-side fallback: if the model declines a request, the API re-runs it on Anthropic's
# recommended fallback model within the same call. Disable with EXTRACTION_REFUSAL_FALLBACK=false.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Thinking is always on for Claude Opus 5.5 and counts toward this limit; streaming keeps a
# large limit safe from HTTP timeouts.
MAX_OUTPUT_TOKENS = 32_000


def build_user_content(request: StructuredRequest) -> list[dict[str, Any]]:
    """The request's text and images as Messages API content blocks."""
    content: list[dict[str, Any]] = []
    for part in request.parts:
        if isinstance(part, TextPart):
            content.append({"type": "text", "text": part.text})
        else:
            content.append(
                {"type": "image", "source": {"type": "base64", "media_type": part.media_type, "data": part.base64}}
            )
    return content


def build_generate_request(request: StructuredRequest, settings: Settings) -> dict[str, Any]:
    return {
        "model": settings.extraction_model,
        "max_tokens": request.max_output_tokens or MAX_OUTPUT_TOKENS,
        # The instructions never change between requests of the same task, so cache them.
        "system": [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": build_user_content(request)}],
        "output_config": {
            "effort": settings.extraction_effort,
            "format": {"type": "json_schema", "schema": request.schema},
        },
    }


def build_request(images: Sequence[PreparedImage], settings: Settings) -> dict[str, Any]:
    """The single-step screenshot extraction request."""
    return build_generate_request(extraction_request(images), settings)


def parse_message(message: Any, duration_ms: int) -> ProviderResponse:
    """Read the JSON answer from a finished message, or raise ExtractionProviderError."""
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise ExtractionProviderError(ErrorKind.REFUSED, f"declined (category: {category})")
    if message.stop_reason == "max_tokens":
        raise ExtractionProviderError(ErrorKind.MALFORMED, "the answer was cut off at the token limit")

    blocks = list(message.content)
    # After a mid-answer fallback, text before the last fallback block is a discarded partial.
    last_fallback = max((i for i, block in enumerate(blocks) if block.type == "fallback"), default=-1)
    text = "".join(block.text for block in blocks[last_fallback + 1 :] if block.type == "text")
    if not text.strip():
        raise ExtractionProviderError(ErrorKind.MALFORMED, "the answer had no text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionProviderError(ErrorKind.MALFORMED, "the answer wasn't valid JSON") from exc

    usage = message.usage
    iterations = getattr(usage, "iterations", None) or []
    fallback_used = last_fallback >= 0 or any(
        getattr(entry, "type", None) == "fallback_message" for entry in iterations
    )
    return ProviderResponse(
        data=data,
        usage=ProviderUsage(
            model=str(getattr(message, "model", "")),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            fallback_used=fallback_used,
        ),
        duration_ms=duration_ms,
        request_id=getattr(message, "_request_id", None),
    )


def map_api_error(exc: anthropic.AnthropicError) -> ExtractionProviderError:
    # Most specific first: the status-code errors subclass APIStatusError, and
    # APITimeoutError subclasses APIConnectionError.
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return ExtractionProviderError(ErrorKind.NOT_CONFIGURED, "the API key was rejected")
    if isinstance(exc, anthropic.RateLimitError):
        return ExtractionProviderError(ErrorKind.RATE_LIMITED, "rate limited by the AI provider")
    if isinstance(exc, anthropic.BadRequestError):
        return ExtractionProviderError(ErrorKind.BAD_REQUEST, f"request rejected: {exc.message}")
    if isinstance(exc, anthropic.APIStatusError):
        return ExtractionProviderError(ErrorKind.UNAVAILABLE, f"provider returned {exc.status_code}")
    if isinstance(exc, anthropic.APITimeoutError):
        return ExtractionProviderError(ErrorKind.TIMEOUT, "the AI provider timed out")
    if isinstance(exc, anthropic.APIConnectionError):
        return ExtractionProviderError(ErrorKind.UNAVAILABLE, "couldn't connect to the AI provider")
    return ExtractionProviderError(ErrorKind.UNAVAILABLE, type(exc).__name__)


class ClaudeProvider:
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def configured(self) -> bool:
        return self._settings.extraction_configured

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(
                # None lets the SDK resolve credentials from the environment.
                api_key=self._settings.anthropic_api_key or None,
                timeout=anthropic.Timeout(self._settings.extraction_timeout_seconds, connect=10.0),
                max_retries=1,
            )
        return self._client

    async def extract(self, images: Sequence[PreparedImage]) -> ProviderResponse:
        return await self.generate(extraction_request(images))

    async def generate(self, structured: StructuredRequest) -> ProviderResponse:
        if not self.configured:
            raise ExtractionProviderError(ErrorKind.NOT_CONFIGURED, "no Anthropic credentials set")
        client = self._get_client()
        request = build_generate_request(structured, self._settings)
        started = time.perf_counter()
        try:
            if self._settings.extraction_refusal_fallback:
                async with client.beta.messages.stream(
                    **request, betas=[FALLBACK_BETA], fallbacks="default"
                ) as stream:
                    message = await stream.get_final_message()
            else:
                async with client.messages.stream(**request) as stream:
                    message = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise map_api_error(exc) from exc
        return parse_message(message, round((time.perf_counter() - started) * 1000))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
