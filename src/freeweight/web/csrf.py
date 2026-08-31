"""freeweight.web.csrf — the page side of MirrorWall's double-submit CSRF (ADR-0026 §2, Phase 14).

MirrorWall's ``CsrfMiddleware`` rejects a form post whose ``csrf_token`` field does not equal the
``__Host-mw-csrf`` cookie. This module is the other half, and it is **central rather than
per-route**: :class:`CsrfCookieMiddleware` issues (or reuses) the token once per request and sets
the cookie, and :func:`current_csrf_token` makes that same token available to
:func:`freeweight.web.rendering.render`, which injects it into every page. A form only has to
``{% include "partials/_csrf.html" %}`` — no route has to remember to pass the token, which is the
exact kind of "every entry point must apply the same policy" gap the M5 verification flagged.

The ``__Host-`` prefix requires ``Secure``; browsers treat ``http://localhost`` and
``http://127.0.0.1`` as secure contexts, which is the loopback bind FreeWeight defaults to (the M5
lesson: ``__Host-`` cookies do not exist on plain HTTP off loopback — the LAN deployment
terminates TLS in front per ADR-0026 §1, so the prefix holds there too).
"""

from __future__ import annotations

import contextvars
import http.cookies
from typing import TYPE_CHECKING

from mirrorwall import CSRF_COOKIE_NAME, issue_csrf_token
from starlette.datastructures import Headers, MutableHeaders

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["CsrfCookieMiddleware", "current_csrf_token"]

_csrf_token_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "freeweight_csrf_token", default=""
)


def current_csrf_token() -> str:
    """The CSRF token bound to the current request, or ``""`` outside a request.

    :func:`freeweight.web.rendering.render` injects this into every template as ``csrf_token``,
    so a form's ``_csrf`` partial always carries the token the cookie was set to.
    """
    return _csrf_token_var.get()


class CsrfCookieMiddleware:
    """Issue or reuse the double-submit CSRF token once per request, and set its cookie.

    Reads the ``__Host-mw-csrf`` cookie; if present, binds it for the render to echo, so two tabs
    keep working. If absent, mints one, binds it, and sets the cookie on the response — so the
    next form post from this page carries a token the cookie can be compared against. Pairs with
    MirrorWall's ``CsrfMiddleware``, which does the *validation* half.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the request's token and, when freshly minted, set the cookie on the response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cookies = http.cookies.SimpleCookie()
        cookies.load(Headers(scope=scope).get("cookie", ""))
        morsel = cookies.get(CSRF_COOKIE_NAME)
        existing = morsel.value if morsel is not None else ""
        token = existing or issue_csrf_token()
        fresh = not existing
        reset_token = _csrf_token_var.set(token)

        async def send_wrapper(message: Message) -> None:
            if fresh and message["type"] == "http.response.start":
                headers = MutableHeaders(raw=list(message["headers"]))
                headers.append(
                    "set-cookie",
                    f"{CSRF_COOKIE_NAME}={token}; Path=/; Secure; HttpOnly; SameSite=Strict",
                )
                message["headers"] = headers.raw
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _csrf_token_var.reset(reset_token)
