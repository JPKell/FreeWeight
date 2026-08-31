"""freeweight.web.routes.settings — the settings a running server may change.

``GET /api/v1/settings`` and ``PUT /api/v1/settings`` (API §8), plus the page behind them.

The page shows two lists and never blurs them. The first is editable: measurement parameters,
telemetry, retention, log level. The second is **config-only** and is rendered as text with its
environment variable beside it — the bind address, the exposure flag, auth tokens, the
remote-provider allowance, the database URL, the data roots. Attempting to change one over the API
is ``403 FORBIDDEN`` naming the key, because a security boundary that a browser session can move
is not a boundary (configuration standards §7).

The rules live in :mod:`freeweight.services.settings`, including which keys are which. These
handlers parse, call and render.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baseaicore import SuiteError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from weightsdb import DatabaseError

from freeweight.__about__ import __version__
from freeweight.services.settings import (
    config_only_keys,
    read_settings,
    update_settings,
)
from freeweight.web.rendering import render

if TYPE_CHECKING:
    from freeweight.config import Settings
    from freeweight.services.database import Database

__all__ = ["api_router", "router"]

api_router = APIRouter(tags=["settings"])
router = APIRouter(include_in_schema=False)


class SettingsBody(BaseModel):
    """A settings change: ``{"changes": {"telemetry.interval_ms": 500}}``."""

    model_config = ConfigDict(extra="forbid")

    changes: dict[str, Any] = Field(default_factory=dict)


def _database(request: Request) -> Database:
    """The handle the server is serving from."""
    return request.app.state.database  # type: ignore[no-any-return]  # app state is untyped


def _settings(request: Request) -> Settings:
    """The resolved configuration."""
    return request.app.state.settings  # type: ignore[no-any-return]  # app state is untyped


def _document(views: Any) -> dict[str, Any]:  # noqa: ANN401 — a Sequence[SettingView]
    """The response body both endpoints return."""
    return {
        "items": [view.as_json() for view in views],
        "config_only": list(config_only_keys()),
    }


@api_router.get("/settings", summary="Runtime-changeable settings")
def get_settings_endpoint(request: Request) -> dict[str, Any]:
    """Return every runtime-changeable setting with its effective value and its source.

    ``config_only`` lists the security-relevant keys this endpoint refuses, so a client can render
    them as read-only rather than discovering the refusal by attempting one.
    """
    return _document(read_settings(_database(request), _settings(request)))


@api_router.put("/settings", summary="Change runtime-changeable settings")
def put_settings_endpoint(request: Request, body: SettingsBody) -> dict[str, Any]:
    """Store new values for runtime-changeable settings.

    All-or-nothing: a request naming one permitted key and one forbidden key changes neither.

    Raises:
        SettingConfigOnly: A named key is security-relevant — ``403 FORBIDDEN``, naming it.
        SettingUnknown: A named key is not runtime-changeable.
        ValidationError: A value is the wrong type or out of range.
    """
    views = update_settings(_database(request), _settings(request), body.changes)
    return _document(views)


def _page(
    request: Request,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the settings page in whichever of its states applies."""
    views: Any = ()
    try:
        views = read_settings(_database(request), _settings(request))
    except DatabaseError as exc:
        error = error or f"{exc.message} ({exc.code})"
        status_code = 503
    return HTMLResponse(
        render(
            "settings/index.html",
            app_version=__version__,
            page="settings",
            views=views,
            config_only=config_only_keys(),
            notice=notice,
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    """Render the settings page."""
    return _page(request)


@router.post("/settings", response_class=HTMLResponse)
async def settings_form(request: Request) -> HTMLResponse:
    """Apply a settings form submission.

    ``async def`` unlike its siblings because it reads the request body itself — the form's field
    names are the setting keys, which no pydantic model can declare ahead of time — and awaiting
    the body is I/O on the event loop, not a database call (ADR-0003 rule 2). The database work it
    then does is a handful of upserts, which Starlette runs on this coroutine; the alternative,
    a threadpool hop for three writes, costs more than it saves.
    """
    form = await request.form()
    changes = {
        key: value
        for key, value in form.items()
        if key.startswith("setting.") and isinstance(value, str)
    }
    submitted = {key.removeprefix("setting."): value for key, value in changes.items()}
    # An unchecked checkbox submits nothing at all, so a boolean setting that is absent from the
    # form has been turned off rather than left alone. The hidden companion field names every
    # boolean the form rendered, which is how "absent" is told from "not on this form".
    for key in str(form.get("booleans", "")).split(","):
        name = key.strip()
        if name and name not in submitted:
            submitted[name] = "false"
    if not submitted:
        return _page(request, notice="Nothing to change.")
    try:
        update_settings(_database(request), _settings(request), submitted)
    except SuiteError as exc:
        return _page(
            request,
            error=f"{exc.message} ({exc.code})",
            status_code=403 if exc.code == "FORBIDDEN" else 400,
        )
    return _page(
        request,
        notice=(
            "Saved. New values apply to work started from now on; a run already executing keeps "
            "the conditions it was measured under."
        ),
    )
