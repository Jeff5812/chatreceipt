"""Shared route dependencies."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable

from fastapi import Request

from app.config import Settings
from app.errors import APIError
from app.rate_limit import RateLimiter, client_key


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def rate_limit(name: str) -> Callable[[Request], Awaitable[None]]:
    async def dependency(request: Request) -> None:
        settings: Settings = request.app.state.settings
        limiter: RateLimiter = request.app.state.limiters[name]
        retry_after = limiter.hit(
            client_key(request, trust_proxy_headers=settings.trust_proxy_headers, client_ip_header=settings.client_ip_header)
        )
        if retry_after is not None:
            raise APIError(
                429,
                "rate_limited",
                "Too many requests. Please wait a few minutes and try again.",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )

    return dependency
