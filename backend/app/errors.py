"""The API's error type and handlers.

Every error response has the same shape, with a message written for the person using the app:
``{"error": {"code": "...", "message": "...", "details": {...} | null}}``
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


_HTTP_MESSAGES = {
    400: ("bad_request", "The request couldn't be read."),
    404: ("not_found", "That page or resource doesn't exist."),
    405: ("method_not_allowed", "That action isn't supported here."),
    413: ("request_too_large", "That upload is too large."),
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Report where each problem is, never the submitted values themselves.
        fields = [
            {
                "location": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
                "problem": error.get("msg", ""),
            }
            for error in exc.errors()[:20]
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_request",
                "Some of the details sent couldn't be read. Check them and try again.",
                {"fields": fields},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _HTTP_MESSAGES.get(exc.status_code, ("http_error", "The request failed."))
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message),
            headers=getattr(exc, "headers", None),
        )
