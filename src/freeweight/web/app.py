"""freeweight.web.app — the FastAPI application factory.

``create_app`` is a pure function of :class:`~freeweight.config.Settings`, so tests can build an
app without touching environment variables or the filesystem. The zero-argument
``create_app_from_environment`` some callers need (uvicorn's own dynamic import, see
:mod:`freeweight.bootstrap`) wraps it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from freeweight.__about__ import __version__
from freeweight.config import LOOPBACK_HOSTS, Settings
from freeweight.services.database import Database
from freeweight.web.errors import register_exception_handlers
from freeweight.web.middleware import (
    BodySizeLimitMiddleware,
    HostValidationMiddleware,
    RequestIdMiddleware,
)
from freeweight.web.rendering import render
from freeweight.web.routes import machines as machines_routes
from freeweight.web.routes import models as models_routes
from freeweight.web.routes import system as system_routes

__all__ = ["create_app"]

_STATIC_DIR = Path(__file__).parent / "static"
_MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024


def _resolve_allowed_hosts(settings: Settings) -> frozenset[str]:
    """The Host-header allowlist for this bind (ADR-0026 §1)."""
    host = settings.server.host.lower()
    if host in LOOPBACK_HOSTS:
        return frozenset({"localhost", "127.0.0.1", "::1", host})
    return frozenset(name.lower() for name in settings.server.allowed_hosts) | {host}


def _docs_allowed(settings: Settings) -> bool:
    """Interactive API docs are loopback-only by default (API Standards §11)."""
    return settings.server.host in LOOPBACK_HOSTS


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own one database handle for as long as the server serves, and dispose it on shutdown.

    An engine is a connection pool plus SQLAlchemy's compiled-statement cache. Building one per
    request throws both away every time — measured on this schema, 0.95 ms against 0.12 ms for the
    same SQLite query, and 7.41 ms against 0.38 ms on PostgreSQL, where every request was also
    opening a fresh backend connection and making the configured ``pool_size`` meaningless.

    The handle is created here rather than in :func:`create_app` so that it is disposed when the
    server stops, and so that building an app object (which tests do freely) opens nothing. It is
    reachable from a route as ``request.app.state.database``.
    """
    settings: Settings = app.state.settings
    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        message = "no database_url configured"
        raise RuntimeError(message)
    database = Database.from_url(
        database_url, statement_timeout_ms=settings.storage.statement_timeout_ms
    )
    app.state.database = database
    try:
        yield
    finally:
        database.close()
        app.state.database = None


def create_app(settings: Settings) -> FastAPI:
    """Build the FastAPI application for the given settings.

    Registers, from outermost to innermost: the request-ID middleware, Host-header validation,
    the request body size limit, the standard error envelope handlers, the ``/api/v1`` system
    routes, static assets, and the HTML pages (the shell, machines and models).

    Still a pure function of ``Settings`` — it opens nothing. The database handle is created by
    the lifespan, which runs only when the application is actually served (or when a test enters
    ``TestClient`` as a context manager).
    """
    app = FastAPI(
        title="FreeWeight",
        version=__version__,
        docs_url="/api/v1/docs" if _docs_allowed(settings) else None,
        openapi_url="/api/v1/openapi.json" if _docs_allowed(settings) else None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.database = None

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(HostValidationMiddleware, allowed_hosts=_resolve_allowed_hosts(settings))
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=_MAX_REQUEST_BODY_BYTES)

    register_exception_handlers(app)

    app.include_router(system_routes.router, prefix="/api/v1")
    app.include_router(machines_routes.router)
    app.include_router(models_routes.router)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def shell() -> HTMLResponse:
        """Render the application shell: the overview every later phase builds into."""
        return HTMLResponse(render("base.html", app_version=__version__, page="home"))

    return app
