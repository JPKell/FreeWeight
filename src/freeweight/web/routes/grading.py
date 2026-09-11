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

from typing import TYPE_CHECKING, Annotated, Any

from baseaicore import SuiteError
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from weightsdb import DatabaseError

from freeweight.__about__ import __version__
from freeweight.services.calibration import (
    RunGradeSubmission,
    record_run_grades,
    run_grading_view,
)
from freeweight.web.rendering import render

if TYPE_CHECKING:
    from freeweight.services.calibration import RunGradingView

__all__ = ["api_router", "router"]

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


# ---------------------------------------------------------------------------------------------
# The same screen over the API (api.md §4)
#
# The view and the recording above, for a client that grades away from this page. The view carries
# exactly what the screen shows — no more — so the blinding holds for a client too.
# ---------------------------------------------------------------------------------------------

api_router = APIRouter(tags=["runs"])


class RunGradeBody(BaseModel):
    """One grade for one of the run's samples on one human criterion."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1)
    criterion: str = Field(min_length=1)
    grade: int = Field(ge=1, le=7)
    note: str = ""


class RunGradesBody(BaseModel):
    """A batch of grades. Partial submission is normal; each is upserted on its own row."""

    model_config = ConfigDict(extra="forbid")

    grades: list[RunGradeBody] = Field(default_factory=list)
    graded_by: str = Field(default="unknown", min_length=1)


def _view_json(view: RunGradingView) -> dict[str, Any]:
    """The grading screen's view as JSON: the samples' text and grades, never the model."""
    return {
        "run_id": view.run_id,
        "goal_slug": view.goal_slug,
        "goal_name": view.goal_name,
        "criteria": [
            {
                "key": criterion.key,
                "name": criterion.name,
                "weight": criterion.weight,
                "scale_points": criterion.scale_points,
                "descriptors": dict(criterion.descriptors),
            }
            for criterion in view.criteria
        ],
        "samples": [
            {
                "sample_id": sample.sample_id,
                "case_id": sample.case_id,
                "response_text": sample.response_text,
                "grades": {key: dict(value) for key, value in sample.grades.items()},
            }
            for sample in view.samples
        ],
        "expected_grades": view.expected,
        "recorded_grades": view.recorded,
        "complete": view.complete,
    }


@api_router.get("/runs/{run_id}/grading", summary="A goal run's blinded grading view")
def run_grading_endpoint(request: Request, run_id: str) -> dict[str, Any]:
    """The samples a person grades on a completed goal run's human criteria, blinded and shuffled.

    Raises:
        RunNotFound: No run has this id.
        RunNotGradeable: ``409``: not a completed goal run with a human criterion, or its goal's
            rubric has changed since the run.
    """
    return _view_json(run_grading_view(request.app.state.database, run_id))


@api_router.post("/runs/{run_id}/grades", summary="Grade a goal run's samples")
def run_grades_endpoint(request: Request, run_id: str, body: RunGradesBody) -> dict[str, Any]:
    """Record grades on the run's samples; composites, aggregates and evidence follow.

    Upserted per ``(sample, criterion)``, so a batch sent twice — a client retrying after a dropped
    connection — replaces rather than duplicates.

    Raises:
        RunNotFound: No run has this id.
        RunNotGradeable: See :func:`run_grading_endpoint`.
        ValidationError: A sample outside the run, a criterion that is not a human one, or a grade
            outside the criterion's scale — and then no grade of the batch lands.
    """
    database = request.app.state.database
    recorded = record_run_grades(
        database,
        run_id,
        [
            RunGradeSubmission(
                sample_id=grade.sample_id,
                criterion_key=grade.criterion,
                grade=grade.grade,
                note=grade.note,
            )
            for grade in body.grades
        ],
        graded_by=body.graded_by,
        registry=request.app.state.registry,
        evidence_settings=request.app.state.settings.evidence,
    )
    view = run_grading_view(database, run_id)
    return {
        "run_id": run_id,
        "recorded": recorded,
        "expected_grades": view.expected,
        "recorded_grades": view.recorded,
        "complete": view.complete,
    }
