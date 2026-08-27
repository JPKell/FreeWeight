"""freeweight.web.middleware — request IDs, Host validation and body-size limits.

Written as plain ASGI middleware (a callable class wrapping ``app``), not
``starlette.middleware.base.BaseHTTPMiddleware``: the latter runs the downstream app in a separate
task internally in some Starlette versions, which can detach the response from the
``contextvars`` context the request-ID middleware sets. A plain ASGI class runs everything in the
same coroutine, so :func:`freeweight.observability.logging.bind_context` reliably covers every log
record produced while handling the request.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from baseaicore import new_id
from baseaicore.timeutil import to_rfc3339, utc_now
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from freeweight.observability.logging import bind_context
from freeweight.web.errors import ErrorDetail, ErrorEnvelope

__all__ = ["BodySizeLimitMiddleware", "HostValidationMiddleware", "RequestIdMiddleware"]

logger = logging.getLogger(__name__)

_REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _valid_request_id(candidate: str) -> bool:
    """A client-supplied ``X-Request-ID`` is honoured only if it is a safe, bounded token."""
    return bool(_REQUEST_ID_PATTERN.match(candidate))


def _split_host(header: str) -> str:
    """Extract the hostname from a ``Host`` header, handling bracketed IPv6 literals."""
    header = header.strip()
    if header.startswith("["):
        end = header.find("]")
        if end != -1:
            return header[1:end].lower()
    return header.split(":", 1)[0].lower()


def _error_body(
    *, code: str, message: str, request_id: str, details: dict[str, str]
) -> dict[str, object]:
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details,
            request_id=request_id,
            timestamp=to_rfc3339(utc_now()),
        )
    )
    return envelope.model_dump(mode="json")


class RequestIdMiddleware:
    """Assigns or echoes a request ID; binds it into every log record for the request's duration."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Assign or echo the request ID, bind it into the log context, and add response headers."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        supplied = headers.get("x-request-id")
        request_id = supplied if supplied and _valid_request_id(supplied) else new_id()
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                mutable_headers = MutableHeaders(raw=list(message["headers"]))
                mutable_headers["X-Request-ID"] = request_id
                mutable_headers["X-Api-Version"] = "v1"
                if "cache-control" not in mutable_headers:
                    mutable_headers["Cache-Control"] = "no-store"
                message["headers"] = mutable_headers.raw
            await send(message)

        with bind_context(request_id=request_id):
            await self.app(scope, receive, send_wrapper)


class HostValidationMiddleware:
    """Rejects any request whose ``Host`` header is not on the allowlist (ADR-0026 §1)."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str]) -> None:
        """Wrap ``app``, accepting only requests whose ``Host`` header is in ``allowed_hosts``."""
        self.app = app
        self._allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject a mismatched ``Host`` header with 421 before the request reaches routing."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host_header = headers.get("host", "")
        host = _split_host(host_header)
        if host not in self._allowed_hosts:
            logger.warning("request.host_rejected", extra={"host": host_header})
            request_id = scope.get("state", {}).get("request_id") or new_id()
            response = JSONResponse(
                status_code=421,
                content=_error_body(
                    code="MISDIRECTED_REQUEST",
                    message="The Host header does not match an allowed hostname for this server.",
                    request_id=request_id,
                    details={"host": host_header},
                ),
                headers={"X-Request-ID": request_id},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Rejects a request whose declared ``Content-Length`` exceeds the configured limit."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        """Wrap ``app``, rejecting a request whose declared body exceeds ``max_body_bytes``."""
        self.app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject an oversized request with 413 before the request reaches routing."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                length = None
            if length is not None and length > self._max_body_bytes:
                request_id = scope.get("state", {}).get("request_id") or new_id()
                response = JSONResponse(
                    status_code=413,
                    content=_error_body(
                        code="PAYLOAD_TOO_LARGE",
                        message="Request body exceeds the configured size limit.",
                        request_id=request_id,
                        details={"limit_bytes": str(self._max_body_bytes)},
                    ),
                    headers={"X-Request-ID": request_id},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
