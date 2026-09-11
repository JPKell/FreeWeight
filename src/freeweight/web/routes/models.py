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
from typing import Annotated, Any

from baseaicore import ValidationError, to_rfc3339
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from modelrack.errors import ModelNotFound, ProviderError
from pydantic import BaseModel, ConfigDict
from weightsdb import DatabaseError

from freeweight.__about__ import __version__
from freeweight.services.database import Database
from freeweight.services.models import (
    discover_models,
    get_last_discovery,
    get_model_detail,
    list_models_with_latest_descriptor,
    set_model_enabled,
)
from freeweight.services.results import ResultsQuery, query_results
from freeweight.web.rendering import render

__all__ = ["api_router", "router"]

router = APIRouter(include_in_schema=False)
api_router = APIRouter(tags=["models"])
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


@router.post("/models/{model_ref}/enabled")
async def set_enabled(request: Request, model_ref: str) -> RedirectResponse:
    """Permit or refuse one model, from the models page (ADR-0118).

    A disabled model keeps its row and every result measured under it; what changes is that a new
    run naming it is refused rather than measured. Discovery never writes the flag, so a refresh
    does not undo the decision.
    """
    form = await request.form()
    set_model_enabled(
        request.app.state.database,
        model_ref=model_ref,
        enabled=str(form.get("enabled", "false")) == "true",
    )
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


def _identity_json(row: Any) -> dict[str, Any]:
    """The identity fields every model body carries (api.md §2)."""
    return {
        "id": row.id,
        "canonical_id": row.canonical_id,
        "provider_kind": row.provider_kind,
        "provider_model_name": row.provider_model_name,
        "artifact_digest": row.artifact_digest,
        "identity_confidence": row.identity_confidence,
        "first_seen_at": to_rfc3339(row.first_seen_at),
        "last_seen_at": to_rfc3339(row.last_seen_at),
    }


def _descriptor_json(descriptor: Any) -> dict[str, Any] | None:
    """One descriptor snapshot, or ``None`` where none has been recorded."""
    if descriptor is None:
        return None
    return {
        "observed_at": to_rfc3339(descriptor.observed_at),
        "family": descriptor.family,
        "architecture": descriptor.architecture,
        "parameter_count": descriptor.parameter_count,
        "active_parameter_count": descriptor.active_parameter_count,
        "expert_count": descriptor.expert_count,
        "quantization": descriptor.quantization,
        "weight_format": descriptor.weight_format,
        "size_bytes": descriptor.size_bytes,
        "max_context": descriptor.max_context,
        "embedding_dim": descriptor.embedding_dim,
        "layers": descriptor.layers,
        "attention_heads": descriptor.attention_heads,
    }


_SORT_KEYS = ("last_seen_at", "canonical_id")


def _sorted(rows: tuple[Any, ...], sort: str | None) -> tuple[Any, ...]:
    """``rows`` in the order ``sort`` names; unchanged — newest sighting first — for ``None``.

    Raises:
        ValidationError: ``sort`` is not one of the two keys, optionally prefixed with ``-``.
    """
    if sort is None:
        return rows
    key = sort.removeprefix("-")
    if key not in _SORT_KEYS:
        raise ValidationError(
            "sort must be last_seen_at or canonical_id, optionally with a leading '-'; "
            f"got {sort!r}.",
            details={"field": "sort", "value": sort},
        )
    return tuple(sorted(rows, key=lambda row: getattr(row, key), reverse=sort.startswith("-")))


class EnabledBody(BaseModel):
    """``POST /api/v1/models/{model_ref}/enabled``'s body (ADR-0118)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


@api_router.get("/models", summary="Every known model identity")
def list_models_endpoint(  # noqa: PLR0913 — every argument is a documented query parameter
    request: Request,
    provider_kind: Annotated[str | None, Query()] = None,
    family: Annotated[str | None, Query()] = None,
    quantization: Annotated[str | None, Query()] = None,
    canonical_id: Annotated[str | None, Query()] = None,
    has_results: Annotated[bool | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Return every stored model identity with its latest descriptor.

    A pure database read: staleness comes from the last recorded discovery attempt and the
    provider is never probed here, so listing models cannot hang on a provider that is down.

    ``canonical_id`` is the identity lookup (api.md §2). It is a **query parameter and never a
    path segment**, because a canonical ID contains ``/``, ``:`` and ``@``, and a percent-encoded
    ``/`` does not survive common reverse proxies ([ADR-0024](../../../../docs/adr/)).

    Args:
        request: The incoming request.
        provider_kind: Filter by provider kind.
        family: Filter by descriptor family.
        quantization: Filter by weight quantization.
        canonical_id: Return only the model with this exact canonical ID.
        has_results: Only models any of whose runs stored a metric, or only those none did.
        sort: ``last_seen_at`` or ``canonical_id``, ``-`` for descending.

    Returns:
        ``{"items": [...]}`` — newest sighting first unless ``sort`` says otherwise.

    Raises:
        ValidationError: ``sort`` names neither key.
    """
    rows = list_models_with_latest_descriptor(request.app.state.database)
    filters = {
        "provider_kind": provider_kind,
        "canonical_id": canonical_id,
        "quantization": quantization,
        "family": family,
        "has_results": has_results,
    }
    for field, wanted in filters.items():
        if wanted is not None:
            rows = tuple(row for row in rows if getattr(row, field) == wanted)
    return {
        "items": [
            {
                **_identity_json(row),
                "quantization": row.quantization,
                "parameter_count": row.parameter_count,
                "max_context": row.max_context,
                "family": row.family,
                "enabled": row.enabled,
                "has_results": row.has_results,
            }
            for row in _sorted(rows, sort)
        ]
    }


@api_router.post("/models/{model_ref}/enabled", summary="Permit or refuse one model")
def set_enabled_endpoint(request: Request, model_ref: str, body: EnabledBody) -> dict[str, Any]:
    """Disable or enable one model for measurement, as the Models page's button does (ADR-0118).

    JSON, not the page's form field: a form post to ``/api/v1`` carries no CSRF token and the
    CSRF middleware refuses it, where a JSON body cannot be sent cross-origin at all.

    Raises:
        ModelNotFound: No stored model matches ``model_ref``.
        ValidationError: ``model_ref`` is an ambiguous prefix.
    """
    canonical_id = set_model_enabled(
        request.app.state.database, model_ref=model_ref, enabled=body.enabled
    )
    return {"canonical_id": canonical_id, "enabled": body.enabled}


@api_router.post("/models/discover", summary="Re-discover models through ModelRack")
def discover_models_endpoint(request: Request) -> dict[str, Any]:
    """Ask the provider what it has, and record what changed.

    Returns the counts rather than the models: a client that wants the list asks for it, and a
    discovery that reported every model would make the *outcome* — what changed — hard to see.

    Returns:
        ``{"added", "updated", "unchanged", "total"}``.

    Raises:
        ProviderUnavailable: The provider could not be reached, answered as ``503``.
    """
    outcome = discover_models(
        request.app.state.database, request.app.state.provider, now=datetime.now(UTC)
    )
    return {
        "added": outcome.added,
        "updated": outcome.updated,
        "unchanged": outcome.unchanged,
        "total": outcome.total,
    }


@api_router.get("/models/{model_ref}", summary="One model's identity and descriptor history")
def get_model_endpoint(request: Request, model_ref: str) -> dict[str, Any]:
    """Return one model, its aliases and every descriptor snapshot recorded for it.

    ``model_ref`` accepts a stored ULID or an unambiguous prefix, or a bare provider name. A
    canonical ID is accepted in the body and the query string but **never as a path segment**
    (ADR-0024); use ``GET /models?canonical_id=…``.

    Raises:
        ModelNotFound: Nothing matches locally or at the provider.
        ValidationError: An ambiguous prefix; the message names the candidates.
    """
    detail = get_model_detail(
        request.app.state.database,
        request.app.state.provider,
        model_ref,
        now=datetime.now(UTC),
    )
    return {
        **_identity_json(detail),
        "enabled": detail.enabled,
        "aliases": [dict(alias) for alias in detail.aliases],
        "resolved_alias": detail.resolved_alias,
        "latest_descriptor": _descriptor_json(detail.latest_descriptor),
        "descriptor_history": [_descriptor_json(item) for item in detail.descriptor_history],
    }


@api_router.get("/models/{model_ref}/results", summary="One model's stored results")
def model_results_endpoint(
    request: Request,
    model_ref: str,
    suite: Annotated[str | None, Query()] = None,
    runtime_profile_hash: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Return this model's metric rows, filterable by suite and runtime profile.

    The same query the results surface serves, scoped to one model — so a client holding a model
    reference does not have to know how to spell a filter for it.

    Raises:
        ModelNotFound: Nothing matches ``model_ref``.
    """
    detail = get_model_detail(
        request.app.state.database,
        request.app.state.provider,
        model_ref,
        now=datetime.now(UTC),
    )
    page = query_results(
        request.app.state.database,
        ResultsQuery(
            model=detail.canonical_id,
            suite=suite,
            runtime_profile=runtime_profile_hash,
            limit=limit,
            cursor=cursor,
        ),
    )
    return {
        "model": detail.canonical_id,
        "items": [row.as_json() for row in page.rows],
        "next_cursor": page.next_cursor,
    }
