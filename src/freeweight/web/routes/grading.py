"""freeweight.web.routes.grading — the blinded grading screen for a run's ``human`` criteria.

Subjective Goals §3.3's second entry point. The calibration wizard already grades *calibration*
samples through ``/goals/{slug}/grade``; this screen grades a completed goal run's own samples on
its rung-4 criteria, and it lands in Phase 11 because this is where a human grade first has
somewhere to go — evidence.

Three things matter more here than anywhere else, and each is enforced in the service rather than
the template: the model that produced a sample is never fetched, the order is not the order the
samples were produced in, and every grade is saved the moment it is submitted.

Every handler is a plain ``def`` (ADR-0003 rule 1). No business logic lives here: the view and the
recording are :mod:`freeweight.services.calibration`'s.
"""

from __future__ import annotations

from typing import Annotated, Any

from baseaicore import SuiteError
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from freeweight.__about__ import __version__
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.calibration import (
    RunGradeSubmission,
    record_run_grades,
    run_grading_view,
)
from freeweight.web.rendering import render

__all__ = ["router"]

router = APIRouter(include_in_schema=False)

_TextForm = Annotated[str, Form()]


def _page(view: Any, *, run_id: str, error: str | None, status_code: int = 200) -> HTMLResponse:  # noqa: ANN401 — a RunGradingView or None
    """Render the grading screen in whichever of its states applies."""
    return HTMLResponse(
        render(
            "grading/run.html",
            app_version=__version__,
            page="runs",
            run_id=run_id,
            view=view,
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/runs/{run_id}/grade", response_class=HTMLResponse)
def run_grade_page(request: Request, run_id: str) -> HTMLResponse:
    """Grade a completed goal run's samples on its human criteria, blinded and shuffled."""
    database = request.app.state.database
    try:
        view = run_grading_view(database, run_id)
    except SuiteError as exc:
        status_code = 503 if isinstance(exc, DatabaseError) else 404
        return _page(
            None, run_id=run_id, error=f"{exc.message} ({exc.code})", status_code=status_code
        )
    return _page(view, run_id=run_id, error=None)


@router.post("/runs/{run_id}/grade")
def run_grade_submit(  # noqa: PLR0913 — one grade is exactly these fields
    request: Request,
    run_id: str,
    sample_id: _TextForm = "",
    criterion: _TextForm = "",
    grade: _TextForm = "",
    note: _TextForm = "",
) -> Any:  # noqa: ANN401 — a redirect or the screen's own error state
    """Record one grade and return to the grading screen.

    One grade per submission, upserted onto the sample's criterion row, so an out-of-order
    submission replaces one row rather than appending a second and a refresh loses nothing. The
    sample's composite, the run's aggregates and the subject's evidence are all refreshed before
    the redirect, so the evidence page reflects the grade the moment the screen reloads.
    """
    database = request.app.state.database
    settings = request.app.state.settings
    try:
        record_run_grades(
            database,
            run_id,
            [
                RunGradeSubmission(
                    sample_id=sample_id,
                    criterion_key=criterion,
                    grade=int(grade),
                    note=note,
                )
            ],
            graded_by="ui",
            registry=request.app.state.registry,
            evidence_settings=settings.evidence,
        )
    except (SuiteError, ValueError) as exc:
        # The screen shows the stored state, which is the truth; a rejected grade did not land,
        # and the page says so beside the sample that is still ungraded.
        try:
            view = run_grading_view(database, run_id)
        except SuiteError as inner:
            return _page(
                None, run_id=run_id, error=f"{inner.message} ({inner.code})", status_code=404
            )
        message = f"{exc.message} ({exc.code})" if isinstance(exc, SuiteError) else str(exc)
        return _page(view, run_id=run_id, error=message, status_code=400)
    return RedirectResponse(f"/runs/{run_id}/grade", status_code=303)
