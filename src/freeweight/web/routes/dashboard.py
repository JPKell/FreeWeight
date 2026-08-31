"""freeweight.web.routes.dashboard — the page that answers the four questions.

*Which model is best at what? What does it cost me? Will it fit? Can I trust the number?* — the
dashboard exists to answer those four without the user opening anything, and to be one click from
the evidence for every answer it gives.

Server-rendered in one pass. The heatmap is an ordinary ``<table>``, the two scatter charts are
inline SVG with a table of the same figures beside them, and the filter bar is a
``<form method="get">``. All of it works with JavaScript disabled (ADR-0020, UI standards §13);
``dashboard.js`` and ``charts.js`` add hover detail and re-theming on top of markup that is already
complete.

The handler holds no business logic: :func:`~freeweight.services.results.build_dashboard` decides
what every figure is, and this function turns a query string into a filter and renders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from baseaicore import NotFoundError, ValidationError, from_rfc3339
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from weightsdb import DatabaseError

from freeweight.__about__ import __version__
from freeweight.services.results import DashboardFilter, build_dashboard
from freeweight.web.rendering import render

if TYPE_CHECKING:
    from freeweight.services.database import Database

__all__ = ["router"]

router = APIRouter(include_in_schema=False)

_SuiteQuery = Annotated[str | None, Query(description="Restrict to one benchmark suite key.")]
_ModelQuery = Annotated[str | None, Query(description="Restrict to one model.")]
_MachineQuery = Annotated[str | None, Query(description="Restrict to one machine fingerprint.")]
_SinceQuery = Annotated[str | None, Query(description="RFC 3339; runs created at or after.")]


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    suite: _SuiteQuery = None,
    model: _ModelQuery = None,
    machine: _MachineQuery = None,
    since: _SinceQuery = None,
) -> HTMLResponse:
    """Render the dashboard, or the state that explains why there is nothing on it.

    Four states, as UI standards §6 requires: **empty** on a database with no completed run, and
    it says what would create one; **error** when the database cannot be read, with the code and
    the command that fixes it; **populated** otherwise. There is no loading state because there is
    nothing to wait for — the page is rendered before it is sent.
    """
    database: Database = request.app.state.database
    parsed_since = None
    error: str | None = None
    status_code = 200
    dashboard = None
    try:
        if since:
            parsed_since = from_rfc3339(since)
        dashboard = build_dashboard(
            database,
            DashboardFilter(suite=suite, model=model, machine=machine, since=parsed_since),
        )
    except ValueError as exc:
        error = f"since must be an RFC 3339 instant, such as 2026-08-28T00:00:00Z. ({exc})"
        status_code = 400
    except (NotFoundError, ValidationError) as exc:
        error = f"{exc.message} ({exc.code})"
        status_code = 404 if isinstance(exc, NotFoundError) else 400
    except DatabaseError as exc:
        error = f"{exc.message} ({exc.code})"
        status_code = 503
    return HTMLResponse(
        render(
            "dashboard/index.html",
            app_version=__version__,
            page="dashboard",
            dashboard=dashboard,
            filters={
                "suite": suite or "",
                "model": model or "",
                "machine": machine or "",
                "since": since or "",
            },
            error=error,
        ),
        status_code=status_code,
    )
