"""In-memory sliding-window rate limiting, per client.

Limits reset when the process restarts and aren't shared between processes, which is enough
for a single-instance deployment. A shared store is milestone 4 work.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request


class RateLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Record a request. Returns None if allowed, or seconds to wait if over the limit."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return hits[0] + self.window - now
            hits.append(now)
            if len(self._hits) > 10_000:
                self._prune(now)
        return None

    def _prune(self, now: float) -> None:
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= now - self.window]
        for key in stale:
            del self._hits[key]


def client_key(request: Request, *, trust_proxy_headers: bool, client_ip_header: str = "") -> str:
    """Who to count a request against.

    Behind a proxy, the address that connects to the app is the proxy's. ``client_ip_header`` names
    a header the proxy sets itself and a visitor can't (Cloudflare's CF-Connecting-IP, for Render).
    Without it, the last X-Forwarded-For entry is used: proxies append the address that connected to
    them, so earlier entries may have been written by the visitor to dodge the limits.
    """
    if trust_proxy_headers:
        if client_ip_header:
            stated = request.headers.get(client_ip_header, "").split(",")[0].strip()
            if stated:
                return stated
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if forwarded:
            return forwarded[-1]
    return request.client.host if request.client else "unknown"
