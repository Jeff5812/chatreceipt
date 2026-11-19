"""The interface every AI provider implements.

The rest of the app depends only on this module, so a provider can be replaced without touching
the transaction workflow.

A provider does one thing: given instructions, content (text and images) and a JSON schema, it
returns JSON that should follow the schema (``generate``). Every caller validates that JSON itself;
nothing a provider returns is trusted. ``extract`` is the original single-step screenshot
extraction used by ``POST /api/extractions``, expressed as one ``generate`` call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from app.uploads import PreparedImage


class ErrorKind(str, Enum):
    NOT_CONFIGURED = "not_configured"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"
    MALFORMED = "malformed"
    BAD_REQUEST = "bad_request"


class ExtractionProviderError(Exception):
    def __init__(self, kind: ErrorKind, detail: str = "") -> None:
        super().__init__(f"{kind.value}: {detail}" if detail else kind.value)
        self.kind = kind
        self.detail = detail


# The provider error is not specific to extraction; this name reads better in new code.
AIProviderError = ExtractionProviderError


@dataclass(frozen=True)
class ProviderUsage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    fallback_used: bool = False


@dataclass(frozen=True)
class ProviderResponse:
    # Parsed JSON from the model. Untrusted until validated against the schema.
    data: Any
    usage: ProviderUsage
    duration_ms: int
    request_id: str | None = None


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    media_type: str
    data: bytes

    @property
    def base64(self) -> str:
        import base64

        return base64.standard_b64encode(self.data).decode("ascii")


@dataclass(frozen=True)
class StructuredRequest:
    """One provider-neutral request for JSON output.

    ``task`` names the step for logs ("extraction", "transcription", "interpretation").
    ``max_output_tokens`` of None uses the provider's own default.
    ``light`` marks a short text task a smaller, faster model handles well, such as reading a
    typed correction; a provider may route it to one, keeping its main model's quota for images.
    """

    task: str
    system: str
    parts: Sequence[TextPart | ImagePart]
    schema: dict[str, Any]
    max_output_tokens: int | None = None
    light: bool = False


class StructuredModel(Protocol):
    """What the intelligence pipeline needs from a provider."""

    name: str

    @property
    def configured(self) -> bool: ...

    async def generate(self, request: StructuredRequest) -> ProviderResponse: ...

    async def aclose(self) -> None: ...


class ExtractionProvider(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    async def extract(self, images: Sequence[PreparedImage]) -> ProviderResponse: ...

    async def aclose(self) -> None: ...
