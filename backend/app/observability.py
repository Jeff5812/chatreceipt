"""Structured event logs and metrics for running the product.

Events carry IDs, counts, durations, token usage and outcomes. They never carry screenshot content,
names, amounts, phone numbers or anything else from a conversation.

Each log line automatically includes what is being worked on (request ID, job ID, stage) through
``log_context``.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_events = logging.getLogger("chatreceipt.events")
_context: ContextVar[dict[str, Any] | None] = ContextVar("chatreceipt_log_context", default=None)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add these fields to every event logged inside the block."""
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def log_event(event: str, **fields: Any) -> None:
    _events.info("%s", json.dumps({"event": event, **(_context.get() or {}), **fields}, default=str))


class PathOnlyAccessLog(logging.Filter):
    """Keep query strings out of access logs: Meta's webhook handshake puts the verify token in one."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (args[0], args[1], args[2].partition("?")[0], args[3], args[4])
        return True


def configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP client libraries log every request at INFO; keep them quiet.
    for name in ("httpx", "httpx2", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, PathOnlyAccessLog) for existing in access_log.filters):
        access_log.addFilter(PathOnlyAccessLog())


_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)


def _label_text(labels: tuple[tuple[str, str], ...], extra: str = "") -> str:
    parts = [f'{key}="{value}"' for key, value in labels]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class Metrics:
    """Counters and duration histograms in the Prometheus text format, kept in this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}

    @staticmethod
    def _key(name: str, labels: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((key, str(value)) for key, value in labels.items()))

    def inc(self, name: str, amount: float = 1, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def observe(self, name: str, seconds: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            # Bucket counts, then the running sum and count.
            slot = self._histograms.setdefault(key, [0.0] * (len(_DURATION_BUCKETS) + 2))
            for index, limit in enumerate(_DURATION_BUCKETS):
                if seconds <= limit:
                    slot[index] += 1
            slot[-2] += seconds
            slot[-1] += 1

    def value(self, name: str, **labels: Any) -> float:
        with self._lock:
            return self._counters.get(self._key(name, labels), 0)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{_label_text(labels)} {value:g}")
            for (name, labels), slot in sorted(self._histograms.items()):
                for index, limit in enumerate(_DURATION_BUCKETS):
                    lines.append(f"{name}_bucket{_label_text(labels, f'le="{limit:g}"')} {slot[index]:g}")
                lines.append(f"{name}_bucket{_label_text(labels, 'le="+Inf"')} {slot[-1]:g}")
                lines.append(f"{name}_sum{_label_text(labels)} {slot[-2]:g}")
                lines.append(f"{name}_count{_label_text(labels)} {slot[-1]:g}")
        return "\n".join(lines) + "\n"


metrics = Metrics()
