"""freeweight.web.routes.goals — the goal-authoring surface, over HTTP.

[api.md §Goals](../../../../docs/apps/freeweight/api.md)'s Phase 8A endpoints: the CRUD, the lint,
the rule proposals, the task list, the export and the import. The calibration endpoints arrive with
the jury they describe, and the starter-pack endpoints with the wizard.

Three rules from the API standards decide the shapes here:

* **A lint finding never blocks creation.** ``POST`` and ``PUT`` return the findings alongside the
  goal; only an ``error`` finding refuses, and it refuses through the service layer's own
  ``GOAL_INVALID``. A warning is the lint's judgement, and the user owns the rubric.
* **``PUT`` says what it would separate before it commits.** The response carries the old and new
  ``goal_hash`` and, when they differ, the number of existing runs the change separates — the
  statement acceptance criterion 4 asks for.
* **``DELETE`` previews first.** A bare ``DELETE`` returns what would be lost, including how many
  of the user's own grades it would destroy; ``?dry_run=false`` performs it (database standards
  §8).

Every handler is a plain ``def``: they touch the filesystem and the database, which is exactly
what [ADR-0003](../../../../docs/adr/0003-sync-vs-async-strategy.md) rule 1 names. No handler
holds business logic — each calls one or two service functions and renders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from setspec.envelope import GeneratorInfo, SchemaVersion, dump_envelope

from freeweight.__about__ import __version__
from freeweight.config import Settings
from freeweight.services.goals import (
    delete_goal,
    get_goal,
    goal_hash_change,
    import_bundle,
    list_goals,
    replace_pack,
    suggest_rules_for_pack,
    summarize,
    write_pack,
)

if TYPE_CHECKING:
    from pathlib import Path

    from freeweight.services.goals import LoadedGoal

__all__ = ["api_router"]

api_router = APIRouter(tags=["goals"])

_GENERATOR = GeneratorInfo(name="freeweight", version=__version__)
_GOAL_PACK_SCHEMA_VERSION = SchemaVersion(1, 0)


class GoalPackBody(BaseModel):
    """A goal pack as a request body: its ``goal.json`` and its task prompt records.

    Two fields rather than one nested document, because they are two different kinds of
    thing: the goal is a rubric, and each task is an ADR-0012 prompt record validated by the
    prompt loader rather than by this model.
    """

    model_config = ConfigDict(extra="forbid")

    goal: dict[str, Any]
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class GoalBundleBody(BaseModel):
    """An imported bundle, plus the slug to import it under."""

    model_config = ConfigDict(extra="forbid")

    bundle: dict[str, Any]
    slug: str | None = None


def _root(request: Request) -> Path:
    """The configured goal-pack root."""
    settings: Settings = request.app.state.settings
    return settings.goals.root_path


def _goal_json(goal: LoadedGoal) -> dict[str, Any]:
    """Render one goal as the API returns it — the same field names the CLI's ``--json`` uses."""
    summary = summarize(goal)
    return {
        "slug": summary.slug,
        "name": summary.name,
        "intent": goal.pack.intent,
        "goal_hash": summary.goal_hash,
        "goal_pack_version": summary.goal_pack_version,
        "capability_id": summary.capability_id,
        "contributes_to": summary.contributes_to,
        "score_method_mix": dict(summary.score_method_mix),
        "unforked": summary.unforked,
        "calibration_state": _calibration_state(goal),
        "criteria": [
            {
                "key": criterion.key,
                "name": criterion.name,
                "rung": criterion.rung.value,
                "weight": criterion.weight,
                "gate": criterion.is_gate,
                "rule_type": criterion.rule_type,
                "scale_points": None if criterion.scale is None else criterion.scale.points,
                "has_scale_descriptors": (criterion.scale is not None and criterion.scale.anchored),
            }
            for criterion in goal.pack.criteria
        ],
        "tasks": [_task_json(goal, index) for index in range(len(goal.pack.tasks))],
        "findings": [finding.as_json() for finding in goal.findings],
    }


def _calibration_state(goal: LoadedGoal) -> str:
    """Whether this goal needs calibration at all, before any has happened.

    Phase 8A knows only two of api.md's three states: a goal with no judged criterion needs no
    calibration and is ``"calibrated"`` by construction — there is nothing to calibrate, so
    nothing failed to. One with judged criteria is ``"insufficient"`` until the grades exist,
    which is Phase 8B's business and is deliberately *not* reported as ``"uncalibrated"``: that
    word means "measured, and the agreement was too low", and saying it before any measurement
    would be a claim about a jury nobody has run.
    """
    return "insufficient" if goal.pack.judged_criteria else "calibrated"


def _task_json(goal: LoadedGoal, index: int) -> dict[str, Any]:
    """Render one task."""
    task = goal.pack.tasks[index]
    return {
        "key": task.key,
        "name": task.name,
        "prompt_id": task.prompt_id,
        "prompt_version": task.prompt_version,
        "prompt_sha256": task.prompt_sha256,
        "rendered_prompt_hash": task.rendered_prompt_hash,
        "is_starter": task.is_starter,
        "has_source": task.source is not None,
    }


@api_router.get("/goals", summary="Every installed goal")
def list_goals_endpoint(request: Request) -> dict[str, Any]:
    """List the goal packs this installation can run.

    A pack that cannot be parsed at all is omitted rather than failing the listing: nine working
    goals must not be hidden by a tenth with a typo, and ``POST /goals/{slug}/validate`` is where
    the tenth is explained.
    """
    goals = list_goals(_root(request))
    return {
        "items": [_goal_json(goal) for goal in goals],
        "page": {"limit": len(goals), "next_cursor": None, "has_more": False},
        "total": len(goals),
    }


@api_router.post("/goals", status_code=status.HTTP_201_CREATED, summary="Create a goal")
def create_goal_endpoint(request: Request, body: GoalPackBody) -> JSONResponse:
    """Write a new goal pack and return it with its lint findings.

    Findings never block creation; an ``error`` finding refuses through ``GOAL_INVALID`` because
    such a pack could not be run at all.
    """
    goal = write_pack(_root(request), goal=body.goal, tasks=body.tasks)
    return JSONResponse(_goal_json(goal), status_code=status.HTTP_201_CREATED)


@api_router.get("/goals/{slug}", summary="One goal")
def get_goal_endpoint(request: Request, slug: str) -> dict[str, Any]:
    """Return one goal as loaded, with its lint findings."""
    return _goal_json(get_goal(_root(request), slug))


@api_router.put("/goals/{slug}", summary="Replace a goal")
def replace_goal_endpoint(
    request: Request, slug: str, body: GoalPackBody, dry_run: bool = False
) -> dict[str, Any]:
    """Replace one goal, reporting what the change separates.

    The response carries ``hash_change``: the previous and current ``goal_hash``, whether they
    differ, which parts of the measurement-defining document moved, and how many existing runs
    the previous hash holds — the runs a separating change leaves behind.

    ``?dry_run=true`` builds and validates the replacement and then discards it, so the same
    report can be shown **before** the change is applied. That is the order acceptance criterion 4
    asks for: a user is told what an edit would separate while they can still decide not to make
    it.
    """
    previous, current = replace_pack(
        _root(request), slug=slug, goal=body.goal, tasks=body.tasks, dry_run=dry_run
    )
    change = goal_hash_change(
        request.app.state.database, slug=slug, existing=previous, replacement=current
    )
    return {
        **_goal_json(current),
        "dry_run": dry_run,
        "hash_change": {
            "previous_goal_hash": change.previous,
            "goal_hash": change.current,
            "separates": change.separates,
            "changed_fields": list(change.changed_fields),
            "separated_runs": change.separated_runs,
        },
    }


@api_router.delete("/goals/{slug}", summary="Delete a goal, preview first")
def delete_goal_endpoint(request: Request, slug: str, dry_run: bool = True) -> dict[str, Any]:
    """Preview or perform a goal's deletion.

    A bare ``DELETE`` previews. The preview names the two things that are expensive to lose: the
    runs it orphans, and the grades the user produced by hand.
    """
    return delete_goal(request.app.state.database, _root(request), slug, dry_run=dry_run)


@api_router.post("/goals/{slug}/validate", summary="Every problem this goal has")
def validate_goal_endpoint(request: Request, slug: str) -> dict[str, Any]:
    """Return every lint finding for one goal, with a severity each."""
    from freeweight.domain.goals.lint import has_errors

    goal = get_goal(_root(request), slug)
    return {
        "slug": goal.pack.slug,
        "goal_hash": goal.goal_hash,
        "valid": not has_errors(goal.findings),
        "findings": [finding.as_json() for finding in goal.findings],
    }


@api_router.post("/goals/{slug}/suggest-rules", summary="Rules that could carry a criterion")
def suggest_rules_endpoint(request: Request, slug: str) -> dict[str, Any]:
    """Propose rung-2 rules for this goal's criteria. **Proposals only** — never applied."""
    goal = get_goal(_root(request), slug)
    return {"slug": goal.pack.slug, "proposals": suggest_rules_for_pack(goal)}


@api_router.get("/goals/{slug}/tasks", summary="One goal's tasks")
def goal_tasks_endpoint(request: Request, slug: str) -> dict[str, Any]:
    """List one goal's tasks, each flagged whether it is unedited starter content."""
    goal = get_goal(_root(request), slug)
    items = [_task_json(goal, index) for index in range(len(goal.pack.tasks))]
    return {
        "items": items,
        "page": {"limit": len(items), "next_cursor": None, "has_more": False},
        "total": len(items),
    }


@api_router.get("/goals/{slug}/export", summary="Export as benchmark.goal_pack")
def export_goal_endpoint(request: Request, slug: str) -> Response:
    """Return one goal as a ``benchmark.goal_pack`` SetSpec envelope.

    The cross-application contract, returned as the envelope alone with no collection wrapper
    (API standards §3): a pack is one document. It carries the goal's *definition* — criteria,
    weights, rungs, task prompt identities and hashes — which is what a consumer needs to decide
    comparability. The portable *bundle*, which carries the files an importer would need, is
    ``freeweight goals export`` on the CLI.
    """
    from setspec.goal.v1 import GoalPackOut

    goal = get_goal(_root(request), slug)
    payload = GoalPackOut.model_validate(_setspec_payload(goal))
    body = dump_envelope(
        payload,
        schema="benchmark.goal_pack",
        version=_GOAL_PACK_SCHEMA_VERSION,
        generator=_GENERATOR,
    )
    return Response(content=body, media_type="application/json; charset=utf-8")


def _setspec_payload(goal: LoadedGoal) -> dict[str, Any]:
    """Build the ``benchmark.goal_pack`` payload from a loaded goal."""
    from baseaicore import utc_now

    pack = goal.pack
    judge_set = None
    if pack.judge is not None:
        judge_set = {
            "jurors": list(pack.judge.models),
            "prompt_id": pack.judge.prompt_id,
            "prompt_version": pack.judge.prompt_version,
            "prompt_sha256": (
                goal.judge_prompt.sha256 if goal.judge_prompt is not None else "sha256:unresolved"
            ),
            "remote": pack.judge.allow_remote,
        }
    return {
        "slug": pack.slug,
        "name": pack.name,
        "intent": pack.intent,
        "goal_pack_version": pack.goal_pack_version,
        "goal_hash": goal.goal_hash,
        "contributes_to": pack.contributes_to,
        "criteria": [
            {
                "key": criterion.key,
                "name": criterion.name,
                "rung": criterion.rung.value,
                "weight": criterion.weight,
                "is_gate": criterion.is_gate,
                "rule_type": criterion.rule_type,
                "scale_points": None if criterion.scale is None else criterion.scale.points,
                "has_scale_descriptors": (criterion.scale is not None and criterion.scale.anchored),
            }
            for criterion in pack.criteria
        ],
        "tasks": [
            {
                "key": task.key,
                "prompt_id": task.prompt_id,
                "prompt_version": task.prompt_version,
                "prompt_sha256": task.prompt_sha256,
                "is_starter": task.is_starter,
            }
            for task in pack.tasks
        ],
        "judge_set": judge_set,
        "unforked": pack.unforked,
        "created_by": pack.created_by,
        "created_at": pack.created_at or utc_now(),
    }


@api_router.post("/goals/import", status_code=status.HTTP_201_CREATED, summary="Import a bundle")
def import_goal_endpoint(request: Request, body: GoalBundleBody) -> JSONResponse:
    """Import a portable goal bundle.

    Everything is validated against the bundle in memory — size, member names, hash, slug
    availability — before a single file is written, and an import never overwrites an existing
    goal in place (spec §14).
    """
    settings = request.app.state.settings
    goal = import_bundle(
        body.bundle,
        root=settings.goals.root_path,
        max_bytes=settings.goals.max_pack_bytes,
        slug=body.slug,
    )
    return JSONResponse(_goal_json(goal), status_code=status.HTTP_201_CREATED)
