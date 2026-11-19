"""Choose the extraction provider from settings. This is a switch, not a router: one provider per app."""

from __future__ import annotations

from app.config import Settings
from app.extraction.claude import ClaudeProvider
from app.extraction.gemini import GeminiProvider
from app.extraction.provider import ExtractionProvider


def create_provider(settings: Settings) -> ExtractionProvider:
    if settings.extraction_provider == "gemini":
        return GeminiProvider(settings)
    if settings.extraction_provider == "anthropic":
        return ClaudeProvider(settings)
    # "auto": whichever provider has a key, preferring Anthropic. With neither, the app reports that
    # reading screenshots is unavailable and manual entry still works.
    if not settings.extraction_configured and settings.gemini_configured:
        return GeminiProvider(settings)
    return ClaudeProvider(settings)
