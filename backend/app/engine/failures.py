"""Which failures are worth retrying, and what to tell people about them."""

from __future__ import annotations

from dataclasses import dataclass

from app.db.pool import DatabaseUnavailable
from app.documents.browser import DocumentRenderError
from app.extraction.provider import ErrorKind, ExtractionProviderError
from app.intelligence.pipeline import EmptyConversationError
from app.storage import StorageError
from app.uploads import UploadRejected

_BUSY = "The service that reads conversations is busy right now. ChatReceipt will keep trying."
_AI_FAILURES: dict[ErrorKind, tuple[str, bool]] = {
    ErrorKind.TIMEOUT: (_BUSY, True),
    ErrorKind.RATE_LIMITED: (_BUSY, True),
    ErrorKind.UNAVAILABLE: (_BUSY, True),
    ErrorKind.MALFORMED: ("The conversation couldn't be analysed.", True),
    ErrorKind.REFUSED: ("This conversation couldn't be processed. Try different screenshots, or enter the details yourself.", False),
    ErrorKind.NOT_CONFIGURED: ("Reading conversations isn't available right now. You can enter the details yourself.", False),
    ErrorKind.BAD_REQUEST: ("This conversation couldn't be processed. You can enter the details yourself.", False),
}


@dataclass(frozen=True)
class Failure:
    code: str
    message: str  # safe to show to the customer; never contains conversation content or secrets
    retryable: bool


def classify(exc: BaseException) -> Failure:
    if isinstance(exc, ExtractionProviderError):
        message, retryable = _AI_FAILURES[exc.kind]
        return Failure(f"ai_{exc.kind.value}", message, retryable)
    if isinstance(exc, UploadRejected):
        return Failure(exc.code, exc.message, False)
    if isinstance(exc, EmptyConversationError):
        return Failure("no_messages", str(exc), False)
    if isinstance(exc, StorageError):
        return Failure("storage_unavailable", "The uploaded file couldn't be read right now.", True)
    if isinstance(exc, DatabaseUnavailable):
        return Failure("database_unavailable", "ChatReceipt is temporarily unavailable.", True)
    if isinstance(exc, DocumentRenderError):
        return Failure(f"render_{exc.kind}", "The document couldn't be created right now.", True)
    return Failure("unexpected_error", "Something went wrong while processing this.", True)
