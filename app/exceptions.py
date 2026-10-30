"""Safe, consistent API error responses and exception handlers."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """A safe error intended for an API client."""

    def __init__(self, status_code: int, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or [],
                "request_id": _request_id(request),
            }
        },
        headers=headers,
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404 and request.url.path.startswith("/api/"):
        return _error_response(
            request,
            status_code=404,
            code="api_endpoint_not_found",
            message="This API endpoint is not available in the running app. Restart TalentLens from the project folder and try again.",
        )
    try:
        message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        message = "Request failed"
    return _error_response(request, status_code=exc.status_code, code="http_error", message=message)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Do not echo submitted values: they may contain personal or confidential data.
    details = [
        {
            "location": [str(part) for part in error.get("loc", ())],
            "message": "Invalid value",
            "type": str(error.get("type", "value_error")),
        }
        for error in exc.errors()
    ]
    return _error_response(
        request,
        status_code=422,
        code="validation_error",
        message="Request validation failed",
        details=details,
    )


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return _error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        headers={"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled request exception",
        extra={"request_id": _request_id(request), "exception_type": type(exc).__name__},
    )
    return _error_response(
        request,
        status_code=500,
        code="internal_error",
        message="An unexpected error occurred",
    )
