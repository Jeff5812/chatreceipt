"""Request-size limit and a JSON response for unexpected errors.

Added inside the CORS middleware, so these responses still carry CORS headers and the browser can
read them instead of reporting an opaque network error.
"""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import log_context, metrics

logger = logging.getLogger(__name__)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestContextMiddleware:
    """Give every request an ID (returned as X-Request-ID, and in its logs) and count it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers", [])).get(b"x-request-id", b"").decode("latin-1")
        request_id = supplied if _REQUEST_ID.match(supplied) else uuid.uuid4().hex
        status = 500
        started = time.perf_counter()

        async def with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            with log_context(request_id=request_id):
                await self.app(scope, receive, with_request_id)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            metrics.inc("chatreceipt_http_requests_total", method=scope.get("method", ""), route=route, status=status)
            metrics.observe("chatreceipt_http_request_seconds", time.perf_counter() - started, route=route)


class _BodyTooLarge(Exception):
    pass


async def _send_json(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message, "details": None}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class GuardMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await _send_json(send, 400, "bad_request", "The request couldn't be read.")
                    return
                if declared > self.max_body_bytes:
                    await _send_json(
                        send,
                        413,
                        "request_too_large",
                        "That upload is too large. Upload fewer or smaller screenshots.",
                    )
                    return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await _send_json(send, 413, "request_too_large", "That upload is too large.")
        except Exception as exc:
            # Where it failed, without the error's text: database errors can quote the values involved.
            frames = "".join(traceback.format_tb(exc.__traceback__)[-6:])
            logger.error("Unhandled %s on %s %s\n%s", type(exc).__name__, scope.get("method"), scope.get("path"), frames)
            if response_started:
                raise
            await _send_json(
                send, 500, "internal_error", "Something went wrong on our side. Please try again."
            )
