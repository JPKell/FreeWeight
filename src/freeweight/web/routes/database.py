"""freeweight.web.routes.database — data management, previewed and confirmed.

``GET /api/v1/database/stats``, ``POST /api/v1/database/delete-preview``,
``DELETE /api/v1/database/results``, ``POST /api/v1/database/backup`` and
``POST /api/v1/database/vacuum`` (API §7), plus the HTML page that drives all five.

**Nothing here deletes anything without having first said exactly what it would delete.** The
preview returns a token computed over the selection *and* the counts; the deletion recomputes both
and refuses if either moved. A user who previewed "412 samples across 3 runs" and confirmed a
minute later either removes exactly that, or is shown a fresh preview — never something else.

That token is also this page's cross-site defence. FreeWeight has no CSRF token framework yet
(see ``PHASE10_ISSUES.md``), and the destructive route is the one place where waiting for one
would be negligent: the deletion form additionally requires a token that only a same-origin
preview response carries and a typed confirmation, so a cross-origin form post cannot assemble a
valid request even though it can reach the endpoint.

Models and machines are never removed here. The page proves it rather than promising it: the
preserved-table counts are shown before and after, and the deletion outcome carries both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from baseaicore import to_rfc3339
from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from freeweight.__about__ import __version__
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.database_admin import (
    DeletionScope,
    DeletionSelection,
    backup_database,
    database_stats,
    delete_results,
    preview_deletion,
    vacuum_database,
)
from freeweight.web.rendering import render

if TYPE_CHECKING:
    from pathlib import Path

    from freeweight.config import Settings
    from freeweight.services.database import Database

__all__ = ["api_router", "router"]

api_router = APIRouter(tags=["database"])
router = APIRouter(include_in_schema=False)

CONFIRMATION_PHRASE = "delete"
"""What the user types to confirm a deletion in the UI (database standards §8)."""


def _database(request: Request) -> Database:
    """The handle the server is serving from."""
    return request.app.state.database  # type: ignore[no-any-return]  # app state is untyped


def _settings(request: Request) -> Settings:
    """The resolved configuration."""
    return request.app.state.settings  # type: ignore[no-any-return]  # app state is untyped


def _artifact_dir(request: Request) -> Path | None:
    """Where run artifacts live, when configured."""
    from pathlib import Path

    configured = _settings(request).storage.artifact_dir
    return Path(configured) if configured else None


class DeletePreviewBody(BaseModel):
    """The selection to preview."""

    model_config = ConfigDict(extra="forbid")

    scope: DeletionScope
    selector: str | None = None


class DeleteBody(BaseModel):
    """The selection to delete, plus the token that authorizes it."""

    model_config = ConfigDict(extra="forbid")

    scope: DeletionScope
    selector: str | None = None
    token: str = Field(min_length=1)


@api_router.get("/database/stats", summary="Row counts, size, revision, backups, integrity")
def stats_endpoint(request: Request) -> dict[str, Any]:
    """Return the database snapshot (API §7).

    Raises:
        DatabaseUnavailable: The database could not be reached.
    """
    return database_stats(_database(request), artifact_dir=_artifact_dir(request)).as_json()


@api_router.post("/database/delete-preview", summary="Exactly what a deletion would remove")
def delete_preview_endpoint(request: Request, body: DeletePreviewBody) -> dict[str, Any]:
    """Report what deleting this selection would remove, and return the token to confirm it.

    Raises:
        ValidationError: The scope/selector pairing is unanswerable, or a reference is ambiguous.
    """
    selection = DeletionSelection(scope=body.scope, selector=body.selector)
    return preview_deletion(_database(request), selection).as_json()


@api_router.delete("/database/results", summary="Delete stored results, previewed and confirmed")
def delete_results_endpoint(request: Request, body: DeleteBody) -> dict[str, Any]:
    """Delete stored results. Requires the token from a matching preview.

    Raises:
        DatabaseError: The token does not match a fresh preview of this selection.
    """
    selection = DeletionSelection(scope=body.scope, selector=body.selector)
    outcome = delete_results(
        _database(request),
        selection,
        token=body.token,
        keep_backups=_settings(request).storage.backup_retention,
    )
    return outcome.as_json()


@api_router.post(
    "/database/backup",
    status_code=status.HTTP_201_CREATED,
    summary="Take a backup",
)
def backup_endpoint(request: Request) -> JSONResponse:
    """Take a backup of the configured database and return where it went."""
    result = backup_database(_database(request), keep=_settings(request).storage.backup_retention)
    return JSONResponse(
        {
            "path": str(result.path),
            "size_bytes": result.size_bytes,
            "created_at": to_rfc3339(result.created_at),
            "dialect": result.dialect,
            "pruned": [str(path) for path in result.pruned],
        },
        status_code=status.HTTP_201_CREATED,
    )


@api_router.post("/database/vacuum", summary="Reclaim free space")
def vacuum_endpoint(request: Request) -> dict[str, Any]:
    """Reclaim free space and report what it predicted against what it measured."""
    outcome = vacuum_database(_database(request))
    return {
        "estimated_reclaimable_bytes": outcome.estimated_reclaimable_bytes,
        "size_before_bytes": outcome.size_before_bytes,
        "size_after_bytes": outcome.size_after_bytes,
        "reclaimed_bytes": outcome.reclaimed_bytes,
    }


def _page(  # noqa: PLR0913 — the page has exactly these five states to distinguish
    request: Request,
    *,
    preview: Any = None,  # noqa: ANN401 — a DeletionPreview
    outcome: Any = None,  # noqa: ANN401 — a DeletionOutcome
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    form: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render the database page with whatever just happened attached to it."""
    stats = None
    stats_error = error
    try:
        stats = database_stats(_database(request), artifact_dir=_artifact_dir(request))
    except DatabaseError as exc:
        stats_error = stats_error or f"{exc.message} ({exc.code})"
        status_code = 503
    return HTMLResponse(
        render(
            "database/index.html",
            app_version=__version__,
            page="database",
            stats=stats,
            preview=preview,
            outcome=outcome,
            notice=notice,
            error=stats_error,
            form=form or {"scope": "run", "selector": ""},
            confirmation_phrase=CONFIRMATION_PHRASE,
            backup_retention=_settings(request).storage.backup_retention,
        ),
        status_code=status_code,
    )


@router.get("/database", response_class=HTMLResponse)
def database_page(request: Request) -> HTMLResponse:
    """Render the database page: stats, the deletion form, backup and vacuum."""
    return _page(request)


_ScopeForm = Annotated[str, Form()]
_SelectorForm = Annotated[str, Form()]


@router.post("/database/preview", response_class=HTMLResponse)
def preview_form(
    request: Request, scope: _ScopeForm = "run", selector: _SelectorForm = ""
) -> HTMLResponse:
    """Preview a deletion from the page, and render the confirmation form beneath it."""
    from baseaicore import SuiteError

    form = {"scope": scope, "selector": selector}
    try:
        selection = DeletionSelection(scope=DeletionScope(scope), selector=selector.strip() or None)
        preview = preview_deletion(_database(request), selection)
    except (SuiteError, ValueError) as exc:
        message = getattr(exc, "message", str(exc))
        code = getattr(exc, "code", "VALIDATION_ERROR")
        return _page(request, error=f"{message} ({code})", status_code=400, form=form)
    return _page(request, preview=preview, form=form)


@router.post("/database/delete", response_class=HTMLResponse)
def delete_form(  # noqa: PLR0913 — the confirmation is exactly these four fields
    request: Request,
    scope: _ScopeForm = "run",
    selector: _SelectorForm = "",
    token: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Delete, having been previewed and confirmed.

    Refuses on a typed confirmation that does not match, before touching the database at all: the
    check is free, and doing it first means a mistyped confirmation cannot race a concurrent write
    into a different outcome.
    """
    from baseaicore import SuiteError

    form = {"scope": scope, "selector": selector}
    if confirm.strip().lower() != CONFIRMATION_PHRASE:
        return _page(
            request,
            error=(f"Type {CONFIRMATION_PHRASE!r} to confirm. Nothing was deleted."),
            status_code=400,
            form=form,
        )
    try:
        selection = DeletionSelection(scope=DeletionScope(scope), selector=selector.strip() or None)
        outcome = delete_results(
            _database(request),
            selection,
            token=token,
            keep_backups=_settings(request).storage.backup_retention,
        )
    except (SuiteError, ValueError) as exc:
        message = getattr(exc, "message", str(exc))
        code = getattr(exc, "code", "VALIDATION_ERROR")
        return _page(request, error=f"{message} ({code})", status_code=400, form=form)
    return _page(request, outcome=outcome, form=form)


@router.post("/database/backup", response_class=HTMLResponse)
def backup_form(request: Request) -> HTMLResponse:
    """Take a backup from the page and report where it went."""
    try:
        result = backup_database(
            _database(request), keep=_settings(request).storage.backup_retention
        )
    except DatabaseError as exc:
        return _page(request, error=f"{exc.message} ({exc.code})", status_code=503)
    return _page(request, notice=f"Backup written to {result.path} ({result.size_bytes} bytes).")


@router.post("/database/vacuum", response_class=HTMLResponse)
def vacuum_form(request: Request) -> HTMLResponse:
    """Vacuum from the page and report the predicted against the measured reclaim."""
    try:
        outcome = vacuum_database(_database(request))
    except DatabaseError as exc:
        return _page(request, error=f"{exc.message} ({exc.code})", status_code=503)
    return _page(
        request,
        notice=(
            f"Vacuum reclaimed {outcome.reclaimed_bytes} bytes "
            f"(predicted {outcome.estimated_reclaimable_bytes})."
        ),
    )
