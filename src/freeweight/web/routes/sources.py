"""freeweight.web.routes.sources — the benchmark-source page (Phase 13).

Credits every external benchmark FreeWeight can drive: the upstream project, its pinned version
and commit, its licence, the datasets it pins, and whether it needs a sandbox — the honest
acknowledgement that these benchmarks are other people's work, run under their licences, with
their datasets installed by the user rather than redistributed here (ADR-0018).

A thin route: it calls one service function and renders. No business logic (architecture §4).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from freeweight.services.external import list_benchmarks
from freeweight.web.rendering import render

__all__ = ["router"]

router = APIRouter(include_in_schema=False)


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request) -> HTMLResponse:
    """The benchmark-source page: every external benchmark, credited and licensed."""
    settings = request.app.state.settings
    benchmarks = list_benchmarks(settings)
    return HTMLResponse(
        render(
            "sources/index.html",
            page="sources",
            benchmarks=benchmarks,
        )
    )
