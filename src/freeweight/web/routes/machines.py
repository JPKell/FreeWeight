"""freeweight.web.routes.machines — the machines page.

A route handler contains no business logic (coding standards): it calls one service function and
renders. The table it renders is real and, until Phase 4 writes the first machine profile,
legitimately empty — which is a state UI standards §6 requires the page to design for, not a
reason to leave the page out.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from freeweight.__about__ import __version__
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.database import Database
from freeweight.services.inventory import list_machines
from freeweight.web.rendering import render

__all__ = ["router"]

router = APIRouter(include_in_schema=False)


@router.get("/machines", response_class=HTMLResponse)
async def machines_page(request: Request) -> HTMLResponse:
    """Render every known machine, or the error state if the database cannot be read."""
    database: Database = request.app.state.database
    try:
        machines = list_machines(database)
    except DatabaseError as exc:
        return HTMLResponse(
            render(
                "machines/index.html",
                app_version=__version__,
                page="machines",
                machines=(),
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=503,
        )
    return HTMLResponse(
        render(
            "machines/index.html",
            app_version=__version__,
            page="machines",
            machines=machines,
            error=None,
        )
    )
