"""freeweight.web.routes.calibration — the grading surface and the agreement report, over HTTP.

[api.md §Goals](../../../../docs/apps/freeweight/api.md)'s calibration endpoints, plus ``/judges``.
Everything the wizard's steps 5 and 6 need, and everything a script needs to do the same work
without one.

Three shapes decided by the specification rather than by convenience:

* **A goal below the gate is a ``200``, not an error.** The report returns in full,
  ``calibration_state`` is ``"uncalibrated"``, and the diagnostics name the criteria and the
  samples where the jury diverged from the author. Refusing the request would withhold precisely
  the information the author needs to fix the rubric.
* **``CALIBRATION_INSUFFICIENT`` is a different thing and gets a different code.** Too few grades
  means the work is not done; a failed gate means it was done and the rubric turned out not to be
  measurable. The API keeps them apart because the UI copy has to.
* **Every coefficient carries its ``n_holdout``.** Not by convention — the payloads are built from
  :class:`~freeweight.domain.agreement.AgreementResult`, which holds both.

Every handler is a plain ``def``: they touch the database and, for ``calibration/run``, a provider
([ADR-0003](../../../../docs/adr/0003-sync-vs-async-strategy.md) rule 1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from freeweight.services.calibration import (
    GradeSubmission,
    add_samples,
    anchors_for,
    grading_progress,
    latest_outcome,
    record_grades,
    run_calibration,
)
from freeweight.services.goals import get_goal, sync_goals

if TYPE_CHECKING:
    from pathlib import Path

    from freeweight.config import Settings
    from freeweight.services.goals import LoadedGoal

__all__ = ["api_router"]

api_router = APIRouter(tags=["calibration"])


class SampleBody(BaseModel):
    """One candidate output to be graded."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    origin: str = "pasted"
    goal_task_key: str | None = None
    source_sample_id: str | None = None


class SamplesBody(BaseModel):
    """A batch of candidate outputs."""

    model_config = ConfigDict(extra="forbid")

    samples: list[SampleBody] = Field(default_factory=list)


class GradeBody(BaseModel):
    """One grade the author is recording."""

    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1)
    criterion: str = Field(min_length=1)
    grade: int = Field(ge=1, le=7)
    note: str = ""


class GradesBody(BaseModel):
    """A batch of grades. Partial submission is normal; progress survives interruption."""

    model_config = ConfigDict(extra="forbid")

    grades: list[GradeBody] = Field(default_factory=list)
    graded_by: str = Field(default="unknown", min_length=1)


class CalibrationRunBody(BaseModel):
    """A request to score the holdout with the configured jury."""

    model_config = ConfigDict(extra="forbid")

    graded_by: str = Field(default="unknown", min_length=1)


def _settings(request: Request) -> Settings:
    """The resolved configuration."""
    settings: Settings = request.app.state.settings
    return settings


def _root(request: Request) -> Path:
    """The configured goal-pack root."""
    return _settings(request).goals.root_path


def _goal(request: Request, slug: str) -> LoadedGoal:
    """Load one goal and make sure its database projection is current."""
    goal = get_goal(_root(request), slug)
    sync_goals(request.app.state.database, [goal])
    return goal


@api_router.get("/goals/{slug}/calibration", summary="The calibration set and grading progress")
def calibration_state(request: Request, slug: str) -> dict[str, Any]:
    """Return one goal's calibration samples, the partition, and what remains to be graded."""
    from freeweight.infrastructure.db.repositories.calibration import CalibrationSampleRepository
    from freeweight.infrastructure.db.repositories.goals import GoalRepository

    goal = _goal(request, slug)
    database = request.app.state.database
    progress = grading_progress(database, goal)
    with database.read() as session:
        row = GoalRepository().get_by_slug(session, goal.pack.slug)
        samples = (
            CalibrationSampleRepository().list_for_goal(session, row.id) if row is not None else []
        )
        items = [
            {
                "id": sample.id,
                "origin": sample.origin,
                "partition": sample.partition,
                "partition_seed": sample.partition_seed,
                "content_sha256": sample.content_sha256,
                "content": sample.content,
            }
            for sample in samples
        ]
    return {
        "slug": goal.pack.slug,
        "items": items,
        "page": {"limit": len(items), "next_cursor": None, "has_more": False},
        "total": len(items),
        "progress": progress.as_json(),
    }


@api_router.post(
    "/goals/{slug}/calibration/samples",
    status_code=status.HTTP_201_CREATED,
    summary="Add samples to grade",
)
def add_calibration_samples(request: Request, slug: str, body: SamplesBody) -> JSONResponse:
    """Add candidate outputs for the author to grade.

    A sample whose content is already present is skipped: two identical samples would be graded
    twice and counted twice in a figure that assumes independent observations.
    """
    goal = _goal(request, slug)
    added = add_samples(
        request.app.state.database,
        goal,
        contents=[entry.model_dump() for entry in body.samples],
    )
    return JSONResponse(
        {"slug": goal.pack.slug, "added": added, "count": len(added)},
        status_code=status.HTTP_201_CREATED,
    )


@api_router.post("/goals/{slug}/calibration/grades", summary="Submit grades")
def submit_grades(request: Request, slug: str, body: GradesBody) -> dict[str, Any]:
    """Record the author's grades. Idempotent per ``(sample, criterion)``."""
    goal = _goal(request, slug)
    database = request.app.state.database
    recorded = record_grades(
        database,
        goal,
        [
            GradeSubmission(
                sample_id=entry.sample_id,
                criterion_key=entry.criterion,
                grade=entry.grade,
                note=entry.note,
            )
            for entry in body.grades
        ],
        graded_by=body.graded_by,
    )
    return {
        "slug": goal.pack.slug,
        "recorded": recorded,
        "progress": grading_progress(database, goal).as_json(),
    }


@api_router.post("/goals/{slug}/calibration/run", summary="Score the holdout and measure agreement")
def run_calibration_endpoint(
    request: Request, slug: str, body: CalibrationRunBody
) -> dict[str, Any]:
    """Score the held-out samples with the configured jury and store the report.

    A rubric that does not clear the gate returns ``200`` with ``calibration_state`` set to
    ``"uncalibrated"`` and the diagnostics attached: that is a real and useful answer, and
    refusing the request would withhold exactly what the author needs. Too *few* grades is
    ``CALIBRATION_INSUFFICIENT`` — a different state with a different remedy.
    """
    from freeweight.infrastructure.providers.factory import build_provider
    from freeweight.services.jury import build_jury
    from freeweight.services.runs import active_prompt_library

    goal = _goal(request, slug)
    settings = _settings(request)
    database = request.app.state.database
    provider = getattr(request.app.state, "provider", None) or build_provider(settings.provider)
    available = sorted(descriptor.identity.canonical_id for descriptor in provider.list_models())
    jury = build_jury(
        provider,
        pack=goal.pack,
        library=active_prompt_library(),
        settings=settings.judge,
        candidate_canonical_id="",
        available=available,
        allow_remote_provider=settings.providers.allow_remote,
        anchors=anchors_for(database, goal),
        seed=settings.calibration.partition_seed,
        # Served under `[runtime]`, not under whatever the provider picks. A juror left to its
        # advertised context is the exact path that allocated 21.9 GiB of KV cache on a 30 GiB
        # machine and took the display driver down with it (PHASE10_ISSUES.md).
        runtime_profile=settings.runtime.to_profile(),
    )
    outcome = run_calibration(
        database,
        goal,
        jury=jury,
        n_holdout_target=settings.calibration.n_holdout_target,
        graded_by=body.graded_by,
    )
    return outcome.as_json()


@api_router.get("/goals/{slug}/calibration/report", summary="The stored agreement report")
def calibration_report(request: Request, slug: str) -> dict[str, Any]:
    """Return the stored report: every coefficient with its ``n_holdout``, and the gate verdict."""
    goal = _goal(request, slug)
    outcome = latest_outcome(request.app.state.database, goal)
    if outcome is None:
        return {
            "slug": goal.pack.slug,
            "goal_hash": goal.goal_hash,
            "calibration_state": (
                "not_required" if not goal.pack.judged_criteria else "insufficient"
            ),
            "criteria": [],
        }
    return outcome.as_json()


@api_router.get("/judges", summary="Models eligible to judge")
def list_judges(request: Request, candidate: str | None = None) -> dict[str, Any]:
    """List every model that may serve as a juror, with the reason for each refusal."""
    from freeweight.domain.judging import JUDGE_SUITE_KEY, eligible_jurors
    from freeweight.infrastructure.providers.factory import build_provider

    settings = _settings(request)
    provider = getattr(request.app.state, "provider", None) or build_provider(settings.provider)
    available = sorted(descriptor.identity.canonical_id for descriptor in provider.list_models())
    verdicts = eligible_jurors(
        available,
        candidate=candidate,
        requested=list(settings.judge.models),
        allow_remote=settings.providers.allow_remote and settings.judge.allow_remote,
    )
    items = [
        {
            "model": verdict.model_canonical_id,
            "eligible": verdict.eligible,
            "reasons": list(verdict.reasons),
            "judge_benchmark_suite": JUDGE_SUITE_KEY,
        }
        for verdict in verdicts
    ]
    return {
        "items": items,
        "page": {"limit": len(items), "next_cursor": None, "has_more": False},
        "total": len(items),
        "jury_size": settings.judge.jury_size,
    }


class JuryValidationBody(BaseModel):
    """A jury configuration to dry-run."""

    model_config = ConfigDict(extra="forbid")

    goal: str | None = None
    candidate: str | None = None


@api_router.post("/judges/validate", summary="Dry-run a jury configuration")
def validate_jury(request: Request, body: JuryValidationBody) -> dict[str, Any]:
    """Report the jury that would be assembled, and every refusal with its reason.

    Nothing is generated: assembling a jury is a decision about eligibility. Self-judging
    conflicts and remote permission are both visible in the response.
    """
    from freeweight.domain.jury import assemble_jury
    from freeweight.infrastructure.providers.factory import build_provider

    settings = _settings(request)
    provider = getattr(request.app.state, "provider", None) or build_provider(settings.provider)
    available = sorted(descriptor.identity.canonical_id for descriptor in provider.list_models())
    jury_size = settings.judge.jury_size
    requested = list(settings.judge.models)
    allows_remote = settings.judge.allow_remote
    slug = None
    if body.goal is not None:
        goal = get_goal(_root(request), body.goal)
        slug = goal.pack.slug
        if goal.pack.judge is not None:
            jury_size = goal.pack.judge.jury_size
            requested = list(goal.pack.judge.models) or requested
            allows_remote = goal.pack.judge.allow_remote
    assembly = assemble_jury(
        available,
        candidate=body.candidate if settings.judge.refuse_self_judging else None,
        requested=requested,
        jury_size=jury_size,
        allow_remote=settings.providers.allow_remote and allows_remote,
    )
    return {"goal": slug, "candidate": body.candidate, **assembly.as_json()}
