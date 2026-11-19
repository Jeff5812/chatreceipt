"""Run the intelligence stages for one conversation.

    screenshots ─ transcribe (AI) ─ merge ─┐
    text export / labelled text ─ parse ───┼─ Conversation ─ interpret (AI) ─ rules ─ proposals
    unstructured text ─ segment (AI) ──────┘

Each AI stage returns its raw answer and a ``StageRecord`` (provider, model, tokens, duration) so
the caller can store them for traceability. The deterministic steps are plain functions that can
be re-run on stored inputs.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.extraction.provider import ErrorKind, ExtractionProviderError, ProviderResponse, StructuredModel
from app.intelligence.conversation import Conversation
from app.intelligence.interpretation import Interpretation, interpretation_request, parse_interpretation
from app.intelligence.proposal import Proposal, build_proposals
from app.intelligence.review_policy import DEFAULT_POLICY, ReviewPolicy
from app.intelligence.screenshots import ScreenshotTranscript, merge_screenshots
from app.intelligence.text_import import parse_text_conversation
from app.intelligence.transcription import (
    parse_transcription,
    segmented_conversation,
    text_segmentation_request,
    transcription_request,
)
from app.uploads import PreparedImage


class EmptyConversationError(Exception):
    """Nothing readable to analyse. Permanent: retrying won't help."""


@dataclass(frozen=True)
class StageRecord:
    task: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    fallback_used: bool = False
    request_id: str | None = None

    @classmethod
    def of(cls, task: str, provider: str, response: ProviderResponse) -> StageRecord:
        usage = response.usage
        return cls(
            task=task,
            provider=provider,
            model=usage.model,
            input_tokens=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
            duration_ms=response.duration_ms,
            fallback_used=usage.fallback_used,
            request_id=response.request_id,
        )


@dataclass(frozen=True)
class Transcription:
    transcripts: list[ScreenshotTranscript]
    raw: Any
    stage: StageRecord


@dataclass(frozen=True)
class Analysis:
    interpretation: Interpretation
    raw: Any
    stage: StageRecord


async def transcribe_screenshots(model: StructuredModel, images: Sequence[PreparedImage]) -> Transcription:
    response = await model.generate(transcription_request(images))
    numbers = sorted({image.screenshot for image in images})
    transcripts = parse_transcription(response.data, numbers)
    return Transcription(transcripts=transcripts, raw=response.data, stage=StageRecord.of("transcription", model.name, response))


def conversation_from_screenshots(transcripts: list[ScreenshotTranscript]) -> Conversation:
    conversation = merge_screenshots(transcripts)
    if not any(message.participant for message in conversation.messages):
        raise EmptyConversationError("No messages could be read in these screenshots.")
    return conversation


async def conversation_from_text(
    model: StructuredModel | None, text: str, *, day_first_default: bool = True
) -> tuple[Conversation, Any, StageRecord | None]:
    """Parse text deterministically when it has a known structure; otherwise ask the AI to split it."""
    parsed = parse_text_conversation(text, day_first_default=day_first_default)
    if parsed is not None and parsed.messages:
        return parsed, None, None
    if not text.strip():
        raise EmptyConversationError("The conversation is empty.")
    if model is None or not model.configured:
        raise ExtractionProviderError(ErrorKind.NOT_CONFIGURED, "unstructured text needs the AI provider")
    response = await model.generate(text_segmentation_request(text))
    transcripts = parse_transcription(response.data, [1])
    conversation = segmented_conversation(transcripts[0])
    if not conversation.messages:
        raise EmptyConversationError("No messages could be found in this text.")
    return conversation, response.data, StageRecord.of("segmentation", model.name, response)


async def interpret(model: StructuredModel, conversation: Conversation) -> Analysis:
    started = time.perf_counter()
    response = await model.generate(interpretation_request(conversation))
    interpretation = parse_interpretation(response.data, conversation)
    stage = StageRecord.of("interpretation", model.name, response)
    if not stage.duration_ms:
        stage = StageRecord(**{**stage.__dict__, "duration_ms": round((time.perf_counter() - started) * 1000)})
    return Analysis(interpretation=interpretation, raw=response.data, stage=stage)


def propose(
    conversation: Conversation, interpretation: Interpretation, *, policy: ReviewPolicy = DEFAULT_POLICY
) -> list[Proposal]:
    return build_proposals(conversation, interpretation, policy=policy)
