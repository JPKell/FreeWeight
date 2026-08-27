"""freeweight.web.errors — the one error envelope shape, per API and Contract Standards §4.

Every non-2xx response from ``/api/v1`` uses this shape, unwrapped by any SetSpec envelope: an
error describes one request, not a document that outlives it.
"""

from __future__ import annotations

import logging
from typing import Any

from baseaicore import SuiteError, new_id
from baseaicore.timeutil import to_rfc3339, utc_now
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "ErrorDetail",
    "ErrorEnvelope",
    "build_error_response",
    "register_exception_handlers",
]

logger = logging.getLogger(__name__)

_STATUS_BY_CODE: dict[str, int] = {
    "VALIDATION_ERROR": status.HTTP_400_BAD_REQUEST,
    "SCHEMA_VERSION_UNSUPPORTED": status.HTTP_400_BAD_REQUEST,
    "UNAUTHENTICATED": status.HTTP_401_UNAUTHORIZED,
    "FORBIDDEN": status.HTTP_403_FORBIDDEN,
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "MODEL_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "RUN_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "BENCHMARK_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "CONFLICT": status.HTTP_409_CONFLICT,
    "RUN_NOT_CANCELLABLE": status.HTTP_409_CONFLICT,
    "RUN_ALREADY_RUNNING": status.HTTP_409_CONFLICT,
    "PAYLOAD_TOO_LARGE": status.HTTP_413_CONTENT_TOO_LARGE,
    "MISDIRECTED_REQUEST": 421,
    "DEPENDENCY_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "PROVIDER_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "PROVIDER_TIMEOUT": status.HTTP_504_GATEWAY_TIMEOUT,
    "PROVIDER_PROTOCOL_ERROR": status.HTTP_502_BAD_GATEWAY,
    "CONFIGURATION_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "INTERNAL_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
}

_CODE_BY_HTTP_STATUS: dict[int, str] = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    421: "MISDIRECTED_REQUEST",
}


class ErrorDetail(BaseModel):
    """The inner ``error`` object, identical across every application in the suite."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str
    timestamp: str


class ErrorEnvelope(BaseModel):
    """The complete error response body: ``{"error": {...}}``, never further wrapped."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


def _request_id(request: Request) -> str:
    """Return this request's ID, generating one if the request-ID middleware did not run."""
    state_id = getattr(request.state, "request_id", None)
    return state_id if isinstance(state_id, str) and state_id else new_id()


def build_error_response(
    request: Request,
    *,
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build the standard error envelope as a response, with the request ID in body and header."""
    request_id = _request_id(request)
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details or {},
            request_id=request_id,
            timestamp=to_rfc3339(utc_now()),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that translate every exception type into the standard envelope."""

    @app.exception_handler(SuiteError)
    async def _suite_error_handler(request: Request, exc: SuiteError) -> JSONResponse:
        status_code = _STATUS_BY_CODE.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error("request.failed", extra={"code": exc.code}, exc_info=exc)
        else:
            logger.warning("request.rejected", extra={"code": exc.code})
        return build_error_response(
            request,
            code=exc.code,
            message=exc.message,
            status_code=status_code,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {
                "path": ".".join(str(part) for part in error["loc"] if part != "body"),
                "problem": error["msg"],
            }
            for error in exc.errors()
        ]
        return build_error_response(
            request,
            code="VALIDATION_ERROR",
            message="Request body failed validation.",
            status_code=status.HTTP_400_BAD_REQUEST,
            details={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _CODE_BY_HTTP_STATUS.get(exc.status_code, "HTTP_ERROR")
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else "Request failed."
        return build_error_response(
            request, code=code, message=message, status_code=exc.status_code
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("request.unhandled_error", exc_info=exc)
        return build_error_response(
            request,
            code="INTERNAL_ERROR",
            message="An unexpected error occurred.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
