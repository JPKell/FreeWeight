"""freeweight.web.routes.models — the models page.

A route handler contains no business logic (coding standards): it calls one service function and
renders. The table it renders is real and, until Phase 3's discovery through ModelRack populates
it, legitimately empty — which is a state UI standards §6 requires the page to design for, not a
reason to leave the page out.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from freeweight.__about__ import __version__
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.database import Database
from freeweight.services.inventory import list_models
from freeweight.web.rendering import render

__all__ = ["router"]

router = APIRouter(include_in_schema=False)


@router.get("/models", response_class=HTMLResponse)
async def models_page(request: Request) -> HTMLResponse:
    """Render every known model identity, or the error state if the database cannot be read."""
    database: Database = request.app.state.database
    try:
        models = list_models(database)
    except DatabaseError as exc:
        return HTMLResponse(
            render(
                "models/index.html",
                app_version=__version__,
                page="models",
                models=(),
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=503,
        )
    return HTMLResponse(
        render(
            "models/index.html", app_version=__version__, page="models", models=models, error=None
        )
    )
