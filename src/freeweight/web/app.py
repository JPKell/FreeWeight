"""freeweight.web.app — the FastAPI application factory.

``create_app`` is a pure function of :class:`~freeweight.config.Settings`, so tests can build an
app without touching environment variables or the filesystem. The zero-argument
``create_app_from_environment`` some callers need (uvicorn's own dynamic import, see
:mod:`freeweight.bootstrap`) wraps it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from mirrorwall import (
    CsrfMiddleware,
    HostValidationMiddleware,
    RequestIdMiddleware,
    loopback_allowlist,
    mount_static,
)

from freeweight.__about__ import __version__
from freeweight.config import LOOPBACK_HOSTS, Settings
from freeweight.infrastructure.providers.factory import build_provider
from freeweight.services.database import Database
from freeweight.services.goals import LoadedGoal
from freeweight.services.runs import build_registry, build_registry_for
from freeweight.services.scheduler import RunScheduler
from freeweight.services.settings import apply_stored
from freeweight.services.telemetry import TelemetryService, build_collector
from freeweight.web.csrf import CsrfCookieMiddleware
from freeweight.web.errors import register_exception_handlers
from freeweight.web.middleware import BodySizeLimitMiddleware
from freeweight.web.rendering import render, templates
from freeweight.web.routes import benchmarks as benchmarks_routes
from freeweight.web.routes import calibration as calibration_routes
from freeweight.web.routes import compare as compare_routes
from freeweight.web.routes import dashboard as dashboard_routes
from freeweight.web.routes import database as database_routes
from freeweight.web.routes import evidence as evidence_routes
from freeweight.web.routes import goals as goals_routes
from freeweight.web.routes import grading as grading_routes
from freeweight.web.routes import machines as machines_routes
from freeweight.web.routes import models as models_routes
from freeweight.web.routes import results as results_routes
from freeweight.web.routes import runs as runs_routes
from freeweight.web.routes import settings as settings_routes
from freeweight.web.routes import sources as sources_routes
from freeweight.web.routes import system as system_routes
from freeweight.web.routes import wizard as wizard_routes

__all__ = ["create_app"]

_STATIC_DIR = Path(__file__).parent / "static"
_MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024


def _resolve_allowed_hosts(settings: Settings) -> frozenset[str]:
    """The Host-header allowlist for this bind (ADR-0026 §1)."""
    host = settings.server.host.lower()
    if host in LOOPBACK_HOSTS:
        return loopback_allowlist(host)
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

    The telemetry sampler is started and stopped here for the identical reason: one background
    thread for as long as the server serves, reachable as ``request.app.state.telemetry``, and
    guaranteed to stop cleanly (Phase 4's own risk: "sampler lifecycle bugs under reload") no
    matter how the lifespan exits.

    The run scheduler is started here for a third reason on top of those two: **there must be
    exactly one**, and one per served application is the only count that keeps "one GPU workload
    at a time" true. Starting it here also means its startup recovery
    (:meth:`~freeweight.services.scheduler.RunScheduler.recover`) runs before the first request is
    served, so a run orphaned by a kill is already ``interrupted`` — and resumable — by the time
    anyone looks at it.
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
    # Database-backed settings sit *between* the file and the environment
    # (configuration standards §7), and this is the one place that precedence is applied. It
    # happens here rather than in `create_app` because it is a read: folding stored values in
    # requires a database handle, and `create_app` opens nothing. Everything built below —
    # the telemetry sampler's interval, the scheduler's execution defaults — is therefore built
    # from what the settings page actually saved.
    settings = apply_stored(database, settings)
    app.state.settings = settings
    # Built once for the same reason the database handle is: OllamaProvider owns a pooled
    # httpx.Client, and rebuilding one per request would throw the pool away every time. Nothing
    # here opens a connection — construction only validates the configured URL.
    app.state.provider = build_provider(settings.provider)
    telemetry = TelemetryService(
        build_collector(), interval_seconds=settings.telemetry.interval_ms / 1000
    )
    app.state.telemetry = telemetry
    # Rebuilt here, not reused from ``create_app``: this is the first point in the lifecycle
    # that may touch the filesystem, and the user's goal packs live there. A goal installed
    # after the process started still needs a restart; a goal installed before it does not.
    app.state.registry = build_registry_for(settings)
    scheduler = RunScheduler(
        database,
        app.state.provider,
        registry=app.state.registry,
        # The same collector the telemetry bar samples from, so a run's persisted telemetry and
        # the live view are two readings of one instrument rather than two instruments competing
        # for ``nvidia-smi``.
        collector=telemetry.collector,
        telemetry=settings.telemetry,
        # The whole configuration, because a goal run with judged criteria assembles a jury from
        # ``[judge]``, ``[calibration]`` and ``providers.allow_remote``.
        settings=settings,
    )
    app.state.scheduler = scheduler
    # Both background threads are started *inside* the try, so that a failure in the second one
    # still stops the first. Started before it, a raise between the two would skip the `finally`
    # entirely — an ``asynccontextmanager``'s cleanup only runs for what happens after the
    # ``yield`` — and leak the sampler thread for the life of the process.
    try:
        telemetry.start()
        scheduler.start()
        yield
    finally:
        scheduler.stop()
        telemetry.stop()
        database.close()
        app.state.database = None
        app.state.provider = None
        app.state.telemetry = None
        app.state.scheduler = None


def create_app(settings: Settings, *, goals: Sequence[LoadedGoal] = ()) -> FastAPI:
    """Build the FastAPI application for the given settings.

    Registers, from outermost to innermost: the request-ID middleware, Host-header validation,
    the request body size limit, the standard error envelope handlers, the ``/api/v1`` system,
    run, comparison, results, evidence, database and settings routes, static assets, and the HTML
    pages (the shell, dashboard, machines, models, runs, results, compare, evidence, grading,
    database and settings).

    Still a pure function of its arguments — it opens nothing. The database handle is created by
    the lifespan, which runs only when the application is actually served (or when a test enters
    ``TestClient`` as a context manager); the user's goal packs are read by
    :func:`freeweight.bootstrap.bootstrap` and handed in, for the same reason.

    Args:
        settings: The resolved configuration.
        goals: The user's goal packs, already loaded and validated. Empty is a normal state and is
            what a test that builds an app directly gets.
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
    app.state.provider = None
    app.state.telemetry = None
    app.state.scheduler = None
    # The benchmark registry is a pure, cheap value with no I/O, so unlike the handles above it is
    # built here rather than in the lifespan: a route that renders the list of runnable suites
    # then works in a test that never entered the lifespan. The lifespan rebuilds it so that the
    # scheduler and the routes share one instance.
    app.state.goals = tuple(goals)
    app.state.registry = build_registry(goals=goals, rule_timeout_ms=settings.goals.rule_timeout_ms)

    # Starlette wraps in reverse order of these calls, so the last added is outermost. From the
    # outside in the stack is therefore: Host validation, body-size limit, CSRF validation, the
    # CSRF cookie issuer, then request-ID innermost. Host validation is outermost so a DNS-rebinding
    # attempt is 421 before it reaches CSRF or a route — the check "runs before authentication"
    # (ADR-0026 §1) and, here, before every other control. The cookie issuer sits just outside the
    # request-ID middleware so it sees the final response and can set the cookie on it, and CSRF
    # validation sits outside the issuer so a forged post is rejected before a fresh cookie is
    # minted for it (ADR-0026 §2, Security Standards §14).
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(CsrfCookieMiddleware)
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=_MAX_REQUEST_BODY_BYTES)
    app.add_middleware(HostValidationMiddleware, allowed_hosts=_resolve_allowed_hosts(settings))

    register_exception_handlers(app)

    app.include_router(system_routes.router, prefix="/api/v1")
    app.include_router(runs_routes.api_router, prefix="/api/v1")
    app.include_router(goals_routes.api_router, prefix="/api/v1")
    app.include_router(calibration_routes.api_router, prefix="/api/v1")
    app.include_router(compare_routes.api_router, prefix="/api/v1")
    app.include_router(results_routes.api_router, prefix="/api/v1")
    app.include_router(database_routes.api_router, prefix="/api/v1")
    app.include_router(settings_routes.api_router, prefix="/api/v1")
    app.include_router(machines_routes.api_router, prefix="/api/v1")
    app.include_router(models_routes.api_router, prefix="/api/v1")
    app.include_router(benchmarks_routes.api_router, prefix="/api/v1")
    app.include_router(evidence_routes.api_router, prefix="/api/v1")
    app.include_router(machines_routes.router)
    app.include_router(models_routes.router)
    app.include_router(runs_routes.router)
    app.include_router(compare_routes.router)
    app.include_router(dashboard_routes.router)
    app.include_router(results_routes.router)
    app.include_router(database_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(wizard_routes.router)
    app.include_router(evidence_routes.router)
    app.include_router(grading_routes.router)
    app.include_router(sources_routes.router)

    # MirrorWall's own assets (tokens, layout and component CSS, theme/table/SSE/telemetry JS),
    # served from the installed package: no CDN, no network request at page load. Passing the
    # environment swaps the plain `asset_url` filter for the hashing one, so every template emits
    # cacheable URLs without a template change. Mounted before the application's own `/static`
    # because MirrorWall serves under `/static/mirrorwall` and Starlette matches mounts in
    # registration order — the broader prefix would otherwise swallow the package's.
    mount_static(app, environment=templates())
    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def shell() -> HTMLResponse:
        """Render the application shell: the overview every later phase builds into."""
        return HTMLResponse(render("base.html", app_version=__version__, page="home"))

    return app
