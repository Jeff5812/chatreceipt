"""Gemini implementation of the AI provider (Gemini API over HTTPS).

Uses the REST API directly, so no Google SDK is needed. The answer is validated by the caller
(the extraction service or the intelligence pipeline) either way.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import httpx

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

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Thinking tokens count toward this limit.
MAX_OUTPUT_TOKENS = 32_768
# Finish reasons that mean the model declined to answer.
_DECLINED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}
# Brief overloads are common; one retry after a short pause usually succeeds.
_RETRY_STATUSES = {500, 502, 503, 504}
_RETRY_DELAY_SECONDS = 1.5


def build_generate_request(request: StructuredRequest, settings: Settings) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for part in request.parts:
        if isinstance(part, TextPart):
            parts.append({"text": part.text})
        else:
            parts.append({"inline_data": {"mime_type": part.media_type, "data": part.base64}})
    generation_config: dict[str, Any] = {
        "responseMimeType": "application/json",
        "responseJsonSchema": request.schema,
        "maxOutputTokens": request.max_output_tokens or MAX_OUTPUT_TOKENS,
    }
    if settings.gemini_thinking_level:
        generation_config["thinkingConfig"] = {"thinkingLevel": settings.gemini_thinking_level}
    return {
        "systemInstruction": {"parts": [{"text": request.system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation_config,
    }


def build_request(images: Sequence[PreparedImage], settings: Settings) -> dict[str, Any]:
    """The single-step screenshot extraction request."""
    return build_generate_request(extraction_request(images), settings)


def parse_response(body: dict[str, Any], duration_ms: int) -> ProviderResponse:
    """Read the JSON answer from a generateContent response, or raise ExtractionProviderError."""
    feedback = body.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise ExtractionProviderError(ErrorKind.REFUSED, f"declined (block reason: {feedback['blockReason']})")
    candidates = body.get("candidates") or []
    if not candidates:
        raise ExtractionProviderError(ErrorKind.MALFORMED, "the answer had no candidates")
    candidate = candidates[0]
    finish = candidate.get("finishReason")
    if finish in _DECLINED:
        raise ExtractionProviderError(ErrorKind.REFUSED, f"declined (finish reason: {finish})")
    if finish == "MAX_TOKENS":
        raise ExtractionProviderError(ErrorKind.MALFORMED, "the answer was cut off at the token limit")

    parts = (candidate.get("content") or {}).get("parts") or []
    # Thought summaries, if any, are not part of the answer.
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    if not text.strip():
        raise ExtractionProviderError(ErrorKind.MALFORMED, "the answer had no text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionProviderError(ErrorKind.MALFORMED, "the answer wasn't valid JSON") from exc

    usage = body.get("usageMetadata") or {}
    cached = int(usage.get("cachedContentTokenCount") or 0)
    return ProviderResponse(
        data=data,
        usage=ProviderUsage(
            model=str(body.get("modelVersion") or ""),
            # promptTokenCount includes cached tokens; report them separately, like Anthropic does.
            input_tokens=max(0, int(usage.get("promptTokenCount") or 0) - cached),
            # Thinking is billed as output.
            output_tokens=int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0),
            cache_read_tokens=cached,
        ),
        duration_ms=duration_ms,
        request_id=body.get("responseId"),
    )


def map_http_error(status: int, body: Any) -> ExtractionProviderError:
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    reason = str(error.get("status") or "")
    message = str(error.get("message") or "")
    if status in (401, 403) or reason in ("UNAUTHENTICATED", "PERMISSION_DENIED") or "API key not valid" in message:
        return ExtractionProviderError(ErrorKind.NOT_CONFIGURED, "the API key was rejected")
    if reason == "FAILED_PRECONDITION":
        # For example, the API isn't available for this account or region.
        return ExtractionProviderError(ErrorKind.NOT_CONFIGURED, f"the provider refused the account: {message[:160]}")
    if status == 429 or reason == "RESOURCE_EXHAUSTED":
        return ExtractionProviderError(ErrorKind.RATE_LIMITED, "rate limited by the AI provider")
    if status in (400, 404):
        # These messages describe the request's structure (schema, model name), not its content.
        return ExtractionProviderError(ErrorKind.BAD_REQUEST, f"request rejected ({reason or status}): {message[:200]}")
    return ExtractionProviderError(ErrorKind.UNAVAILABLE, f"provider returned {status}")


class GeminiProvider:
    name = "gemini"

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return self._settings.gemini_configured

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=httpx.Timeout(self._settings.extraction_timeout_seconds, connect=10.0),
                transport=self._transport,
            )
        return self._client

    async def _post(self, model: str, request: dict[str, Any], *, timeout_seconds: float | None = None) -> httpx.Response:
        try:
            return await self._get_client().post(
                f"/models/{model}:generateContent",
                json=request,
                headers={"x-goog-api-key": self._settings.gemini_api_key or ""},
                timeout=httpx.Timeout(timeout_seconds, connect=10.0) if timeout_seconds else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TimeoutException as exc:
            raise ExtractionProviderError(ErrorKind.TIMEOUT, "the AI provider timed out") from exc
        except httpx.HTTPError as exc:
            raise ExtractionProviderError(ErrorKind.UNAVAILABLE, "couldn't connect to the AI provider") from exc

    async def generate(self, request: StructuredRequest) -> ProviderResponse:
        if not self.configured:
            raise ExtractionProviderError(ErrorKind.NOT_CONFIGURED, "no Gemini API key set")
        body_out = build_generate_request(request, self._settings)
        model, fallback = self._settings.gemini_model, self._settings.gemini_fallback_model
        if request.light and self._settings.gemini_light_model:
            # A short text task: the light model answers, and the main model stands in if it can't.
            model, fallback = self._settings.gemini_light_model, self._settings.gemini_model
        fallback = fallback if fallback != model else ""
        started = time.perf_counter()
        fallback_used = False
        if fallback:
            # With a second model available, don't wait out a stalled or overloaded one: the
            # configured model gets a shorter time limit and the fallback answers the same request.
            response: httpx.Response | None
            try:
                response = await self._post(model, body_out, timeout_seconds=self._settings.gemini_primary_timeout_seconds)
            except ExtractionProviderError as exc:
                if exc.kind not in (ErrorKind.TIMEOUT, ErrorKind.UNAVAILABLE):
                    raise
                response = None
            # Each model has its own quota, so a rate limit on one is also a reason to try the other.
            if response is None or response.status_code in _RETRY_STATUSES or response.status_code == 429:
                response = await self._post(fallback, body_out)
                fallback_used = True
        else:
            response = await self._post(model, body_out)
            if response.status_code in _RETRY_STATUSES:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                response = await self._post(model, body_out)
        duration_ms = round((time.perf_counter() - started) * 1000)
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200:
            raise map_http_error(response.status_code, body)
        if not isinstance(body, dict):
            raise ExtractionProviderError(ErrorKind.MALFORMED, "the provider's response wasn't JSON")
        parsed = parse_response(body, duration_ms)
        if fallback_used:
            parsed = replace(parsed, usage=replace(parsed.usage, fallback_used=True))
        return parsed

    async def extract(self, images: Sequence[PreparedImage]) -> ProviderResponse:
        return await self.generate(extraction_request(images))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
