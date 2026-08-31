"""freeweight.web.middleware — the request body-size limit.

Request-ID assignment and ``Host`` validation moved to MirrorWall at Phase 12 (ADR-0026 §1: one
implementation of a security control across the suite, not three subtly different ones). The body
limit stays here because MirrorWall deliberately ships none — the right cap is an application
decision. Written as plain ASGI middleware (a callable class wrapping ``app``), not
``BaseHTTPMiddleware``, so everything runs in the same coroutine as the request.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from baseaicore import new_id
from baseaicore.timeutil import to_rfc3339, utc_now
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from freeweight.web.errors import ErrorDetail, ErrorEnvelope

__all__ = ["BodySizeLimitMiddleware"]

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
