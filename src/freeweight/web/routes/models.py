"""freeweight.web.routes.models — the models list, detail and discovery action.

Every handler here is a plain ``def``, not ``async def``. ADR-0003 rule 1: "route handlers that
touch a database, a provider or the filesystem are ``def``; Starlette runs them in a bounded worker
threadpool, so they never block the event loop." :func:`models_page` touches only the database —
staleness is read from the last recorded discovery attempt, never probed live (module docstring of
:mod:`freeweight.services.models`) — but a database read is exactly what rule 1 names, and this
page now runs one query per model to join in its latest descriptor.

A route handler contains no business logic (coding standards): every handler here calls one or two
service functions and renders or redirects.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from baseaicore import ValidationError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from modelrack.errors import ModelNotFound, ProviderError

from freeweight.__about__ import __version__
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.database import Database
from freeweight.services.models import (
    discover_models,
    get_last_discovery,
    get_model_detail,
    list_models_with_latest_descriptor,
)
from freeweight.web.rendering import render

__all__ = ["router"]

router = APIRouter(include_in_schema=False)
logger = logging.getLogger(__name__)


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request) -> HTMLResponse:
    """Render every known model identity, or the error state if the database cannot be read.

    Includes the last discovery attempt (module docstring of
    :mod:`freeweight.services.models`), which is how this page says the data may be stale without
    calling the provider on every view.
    """
    database: Database = request.app.state.database
    try:
        models = list_models_with_latest_descriptor(database)
        last_discovery = get_last_discovery(database)
    except DatabaseError as exc:
        return HTMLResponse(
            render(
                "models/index.html",
                app_version=__version__,
                page="models",
                models=(),
                last_discovery=None,
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=503,
        )
    return HTMLResponse(
        render(
            "models/index.html",
            app_version=__version__,
            page="models",
            models=models,
            last_discovery=last_discovery,
            error=None,
        )
    )


@router.post("/models/discover")
def discover(request: Request) -> RedirectResponse:
    """Run discovery and return to the models page, which shows the outcome it just recorded.

    A plain HTML form action (ADR-0020: server-rendered, progressive enhancement, no SPA) rather
    than a JSON endpoint. Never itself fails the request: a provider error is recorded by
    :func:`~freeweight.services.models.discover_models` before it raises, so the redirect target
    already has the failure to show, exactly as if the request had succeeded — a live "it broke"
    banner would say nothing :func:`~freeweight.services.models.get_last_discovery` does not.
    """
    try:
        discover_models(
            request.app.state.database, request.app.state.provider, now=datetime.now(UTC)
        )
    except ProviderError as exc:
        logger.warning("models.discover.failed", extra={"code": exc.code})
    return RedirectResponse("/models", status_code=303)


@router.get("/models/{model_ref}", response_class=HTMLResponse)
def model_detail(request: Request, model_ref: str) -> HTMLResponse:
    """Render one model's identity, aliases and descriptor history.

    ``model_ref`` accepts a stored ULID or prefix, a canonical ID, or a bare provider name that
    falls back to a live :meth:`~modelrack.provider.Provider.resolve` call — see
    :func:`~freeweight.services.models.get_model_detail`.
    """
    database: Database = request.app.state.database
    try:
        detail = get_model_detail(
            database, request.app.state.provider, model_ref, now=datetime.now(UTC)
        )
    except ModelNotFound as exc:
        return HTMLResponse(
            render(
                "models/detail.html",
                app_version=__version__,
                page="models",
                model_ref=model_ref,
                detail=None,
                error=exc.message,
            ),
            status_code=404,
        )
    except (ValidationError, ProviderError, DatabaseError) as exc:
        return HTMLResponse(
            render(
                "models/detail.html",
                app_version=__version__,
                page="models",
                model_ref=model_ref,
                detail=None,
                error=exc.message,
            ),
            status_code=400 if isinstance(exc, ValidationError) else 503,
        )
    return HTMLResponse(
        render(
            "models/detail.html",
            app_version=__version__,
            page="models",
            model_ref=model_ref,
            detail=detail,
            error=None,
        )
    )
