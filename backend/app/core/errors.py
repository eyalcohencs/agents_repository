from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

_HTTP_STATUS_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


class ErrorResponse(BaseModel):
    """Standard API error envelope: {code, message, details?}."""

    code: str
    message: str
    details: dict[str, Any] | list[Any] | None = None


class AppError(Exception):
    """Domain or infrastructure exception mapped directly to the standard error envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _build_error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | list[Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(code=code, message=message, details=details).model_dump(
        exclude_none=True
    )
    return JSONResponse(status_code=status_code, content=payload, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    """Attach global exception handlers returning the standard error envelope."""

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return _build_error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        sanitized_errors = [
            {
                "loc": list(err.get("loc", ())),
                "msg": str(err.get("msg", "Invalid value")),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        return _build_error_response(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Request validation failed.",
            details=sanitized_errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _HTTP_STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
        if exc.status_code >= 500:
            message = (
                "Service temporarily unavailable."
                if exc.status_code == 503
                else "An unexpected internal error occurred."
            )
        else:
            message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return _build_error_response(
            status_code=exc.status_code,
            code=code,
            message=message,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unhandled_exception(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled exception during request processing", exc_info=exc)
        return _build_error_response(
            status_code=500,
            code="INTERNAL_ERROR",
            message="An unexpected internal error occurred.",
        )
