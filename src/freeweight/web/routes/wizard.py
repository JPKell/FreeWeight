"""freeweight.web.routes.wizard — the seven-step goal authoring wizard, over HTTP.

Subjective Goals §7 as pages. Server-rendered with progressive enhancement, no SPA
([ADR-0020](../../../../docs/adr/0020-ui-rendering-strategy.md)): every step is a ``<form
method="post">`` that posts, saves and redirects to the next step, so a refresh, a back button and
a browser with JavaScript off all work identically.

**Nothing here holds a rule.** The wizard's state machine, its two questions, its rule proposals
and the pack it renders are all :mod:`freeweight.services.wizard`'s; the grading and the agreement
are :mod:`freeweight.services.calibration`'s. These handlers read a form, call one service
function, and redirect.

**Every mutation redirects (POST → 303 → GET).** That is what makes a refresh safe on the one
screen a user spends twenty unbroken minutes in: a re-POST after grading a sample would submit the
grade twice, and the redirect means the browser has nothing to re-submit.

**Grading is not draft state.** A grade goes straight into ``calibration_grades`` through the same
service the CLI uses, which is what makes it survive a refresh, a server restart, and an
out-of-order submission. The draft in the settings store holds only what is not yet a goal.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Annotated, Any

from baseaicore import SuiteError
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from freeweight.__about__ import __version__
from freeweight.services import wizard as wizard_service
from freeweight.services.wizard import WizardStep
from freeweight.web.rendering import render

if TYPE_CHECKING:
    from pathlib import Path

    from freeweight.config import Settings
    from freeweight.services.database import Database
    from freeweight.services.wizard import WizardDraft

__all__ = ["router"]

router = APIRouter(include_in_schema=False)

_TextForm = Annotated[str, Form()]


def _database(request: Request) -> Database:
    """The handle the server is serving from."""
    return request.app.state.database  # type: ignore[no-any-return]  # app state is untyped


def _settings(request: Request) -> Settings:
    """The resolved configuration."""
    return request.app.state.settings  # type: ignore[no-any-return]  # app state is untyped


def _goals_root(request: Request) -> Path:
    """Where the user's own goal packs live."""
    from pathlib import Path

    from freeweight.config import config_dir

    configured = _settings(request).goals.root
    return Path(configured) if configured else config_dir() / "goals"


def _page(
    template: str, *, draft: WizardDraft | None = None, status_code: int = 200, **context: Any
) -> HTMLResponse:
    """Render one wizard step with the shared shell context."""
    return HTMLResponse(
        render(
            template,
            app_version=__version__,
            page="goals",
            draft=draft,
            steps=wizard_service.sequence(),
            questions=wizard_service.SPLIT_QUESTIONS,
            **context,
        ),
        status_code=status_code,
    )


def _error(template: str, exc: SuiteError, **context: Any) -> HTMLResponse:
    """Render a step with its error state, keeping whatever the user typed."""
    return _page(template, error=f"{exc.message} ({exc.code})", status_code=400, **context)


@router.get("/goals", response_class=HTMLResponse)
def goals_page(request: Request) -> HTMLResponse:
    """The goals landing page: what is installed, and the two ways to add one.

    Two ways, and the page is honest about which is which: the wizard, and forking a starter.
    Neither is required — a pack written by hand in an editor is equally first-class — and the
    page says so, because a user who believes the wizard *is* the feature will not open the file
    it produced.
    """
    from freeweight.services.goals import list_goals, summarize

    goals = list_goals(_goals_root(request))
    return _page(
        "goals/index.html",
        step=None,
        goals=[(goal, summarize(goal)) for goal in goals],
        error=None,
    )


@router.get("/goals/starters", response_class=HTMLResponse)
def starters_page(request: Request) -> HTMLResponse:
    """The four shipped starters, in the order they are meant to be read.

    Read down the page and the deterministic share rises from 40 % to 90 %. The page states that
    outright rather than leaving it to be noticed, because it is the lesson the packs exist to
    teach (Subjective Goals §8).
    """
    del request
    return _starters_page()


def _starters_page(*, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    """Render the starters page, with an error attached when a fork was refused."""
    from freeweight.goals.starters import list_starters, load_starter_calibration

    packs = list_starters()
    return _page(
        "goals/starters.html",
        step=None,
        starters=[(pack, load_starter_calibration(pack.key)) for pack in packs],
        error=error,
        status_code=status_code,
    )


@router.post("/goals/starters/{key}/fork")
def fork_starter_form(request: Request, key: str, slug: _TextForm = "") -> Any:  # noqa: ANN401
    """Fork one starter into the user's goals, unedited.

    The pack lands on disk immediately: a real, editable, diffable directory the user owns from
    that moment. It is badged ``unforked`` until they change something, and the goals page says so
    on the row rather than in a footnote.
    """
    from freeweight.goals.starters import fork_starter

    try:
        fork_starter(_goals_root(request), key, slug=slug.strip() or None)
    except SuiteError as exc:
        return _starters_page(error=f"{exc.message} ({exc.code})", status_code=400)
    return RedirectResponse("/goals", status_code=303)


@router.post("/goals/starters/{key}/customise")
def customise_starter_form(request: Request, key: str) -> Any:  # noqa: ANN401
    """Open the wizard on a copy of one starter, writing nothing yet.

    The other half of "starters, not defaults": rather than forking a pack and hoping the user
    edits it, this puts its criteria and its tasks into step 2 as *drafts*, where the two
    questions get asked of each of them before anything is written.
    """
    try:
        draft = wizard_service.starter_draft(_database(request), key)
    except SuiteError as exc:
        return _starters_page(error=f"{exc.message} ({exc.code})", status_code=400)
    return RedirectResponse(f"/goals/new/{draft.draft_id}/criteria", status_code=303)


@router.get("/goals/new", response_class=HTMLResponse)
def wizard_start(request: Request) -> HTMLResponse:
    """Step 1: what are you trying to get?"""
    del request
    # `intent` must be passed explicitly: the template renders `{{ intent or '' }}` as the
    # sticky form value, and StrictUndefined turns the omission into a 500 on the fresh GET —
    # the M6-3 defect class, which the POST error path below never hits because it echoes the
    # submitted text back.
    return _page("goals/wizard_intent.html", step=WizardStep.INTENT, error=None, intent="")


@router.post("/goals/new")
def wizard_start_submit(request: Request, intent: _TextForm = "", name: _TextForm = "") -> Any:  # noqa: ANN401 — a redirect or the step's own error state
    """Record step 1 and move to step 2."""
    try:
        draft = wizard_service.start_draft(_database(request), intent=intent, name=name)
    except SuiteError as exc:
        return _error("goals/wizard_intent.html", exc, step=WizardStep.INTENT, intent=intent)
    return RedirectResponse(f"/goals/new/{draft.draft_id}/criteria", status_code=303)


@router.get("/goals/new/{draft_id}/criteria", response_class=HTMLResponse)
def wizard_criteria(request: Request, draft_id: str) -> HTMLResponse:
    """Step 2: break it into criteria, and ask the two questions of each."""
    draft = wizard_service.load_draft(_database(request), draft_id)
    return _page("goals/wizard_criteria.html", draft=draft, step=WizardStep.CRITERIA, error=None)


@router.post("/goals/new/{draft_id}/criteria")
def wizard_criteria_submit(  # noqa: PLR0913 — one handler, one form, its fields
    request: Request,
    draft_id: str,
    action: _TextForm = "add",
    name: _TextForm = "",
    intent: _TextForm = "",
    criterion: _TextForm = "",
    graded_alike: _TextForm = "",
    one_quality: _TextForm = "",
    first: _TextForm = "",
    second: _TextForm = "",
    points: _TextForm = "5",
    top: _TextForm = "",
    middle: _TextForm = "",
    bottom: _TextForm = "",
) -> Any:  # noqa: ANN401 — a redirect or the step's own error state
    """Add a criterion, answer its two questions, describe its scale, or split it in two.

    Four actions on one form because they are four answers to the same screen, and a user who has
    just been asked "is this one quality, or two?" should be able to say "two" and split it
    without a page in between.
    """
    database = _database(request)
    draft = wizard_service.load_draft(database, draft_id)
    try:
        if action == "add":
            draft = wizard_service.add_criterion(draft, name=name, intent=intent)
        elif action == "answer":
            draft = wizard_service.answer_questions(
                draft,
                criterion,
                graded_alike=_tristate(graded_alike),
                one_quality=_tristate(one_quality),
            )
        elif action == "describe":
            scale = int(points or "5")
            draft = wizard_service.set_scale(
                draft,
                criterion,
                points=scale,
                descriptors={
                    str(scale): top,
                    str((scale + 1) // 2): middle,
                    "1": bottom,
                },
            )
        elif action == "split":
            draft = wizard_service.split_criterion(draft, criterion, first=first, second=second)
        elif action == "next":
            return RedirectResponse(f"/goals/new/{draft_id}/rules", status_code=303)
    except SuiteError as exc:
        return _error("goals/wizard_criteria.html", exc, draft=draft, step=WizardStep.CRITERIA)
    wizard_service.save_draft(database, draft)
    return RedirectResponse(f"/goals/new/{draft_id}/criteria", status_code=303)


def _tristate(value: str) -> bool | None:
    """Read a yes/no/unanswered radio group.

    Unanswered is ``None`` and is not the same as "no": the wizard offers a split when the user
    says a criterion is two qualities, and offering one because they have not answered yet would
    be the wizard performing the move rather than making it visible.
    """
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


@router.get("/goals/new/{draft_id}/rules", response_class=HTMLResponse)
def wizard_rules(request: Request, draft_id: str) -> HTMLResponse:
    """Step 3: the application proposes rules; the user accepts, edits or skips each."""
    draft = wizard_service.load_draft(_database(request), draft_id)
    return _page(
        "goals/wizard_rules.html",
        draft=draft,
        step=WizardStep.RULES,
        proposals=wizard_service.propose_rules(draft),
        shift=wizard_service.weight_shift(draft),
        error=None,
    )


@router.post("/goals/new/{draft_id}/rules")
def wizard_rules_submit(
    request: Request,
    draft_id: str,
    action: _TextForm = "accept",
    criterion: _TextForm = "",
    rule_type: _TextForm = "",
    parameters: _TextForm = "",
) -> Any:  # noqa: ANN401 — a redirect or the step's own error state
    """Accept one proposed rule, or move on.

    ``parameters`` arrives as JSON the user may have edited in the textarea. A malformed edit is a
    validation error naming the field, never a silently discarded edit — a rule quietly applied
    with the *proposed* parameters after the user changed them would be the worst outcome here.
    """
    import json

    database = _database(request)
    draft = wizard_service.load_draft(database, draft_id)
    if action == "next":
        return RedirectResponse(f"/goals/new/{draft_id}/tasks", status_code=303)
    try:
        edited = json.loads(parameters) if parameters.strip() else None
        if edited is not None and not isinstance(edited, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        from baseaicore import ValidationError

        return _error(
            "goals/wizard_rules.html",
            ValidationError(
                f"Those rule parameters are not a JSON object: {exc}. Nothing was applied.",
                details={"field": "parameters"},
            ),
            draft=draft,
            step=WizardStep.RULES,
            proposals=wizard_service.propose_rules(draft),
            shift=wizard_service.weight_shift(draft),
        )
    try:
        draft = wizard_service.accept_rule(draft, criterion, rule_type=rule_type, parameters=edited)
    except SuiteError as exc:
        return _error(
            "goals/wizard_rules.html",
            exc,
            draft=draft,
            step=WizardStep.RULES,
            proposals=wizard_service.propose_rules(draft),
            shift=wizard_service.weight_shift(draft),
        )
    wizard_service.save_draft(database, draft)
    return RedirectResponse(f"/goals/new/{draft_id}/rules", status_code=303)


@router.get("/goals/new/{draft_id}/tasks", response_class=HTMLResponse)
def wizard_tasks(request: Request, draft_id: str) -> HTMLResponse:
    """Step 4: the user's own prompts."""
    draft = wizard_service.load_draft(_database(request), draft_id)
    return _page(
        "goals/wizard_tasks.html",
        draft=draft,
        step=WizardStep.TASKS,
        cost=wizard_service.grading_cost_sentence(draft),
        error=None,
    )


@router.post("/goals/new/{draft_id}/tasks")
def wizard_tasks_submit(
    request: Request,
    draft_id: str,
    action: _TextForm = "add",
    name: _TextForm = "",
    prompt_text: _TextForm = "",
) -> Any:  # noqa: ANN401 — a redirect or the step's own error state
    """Add a task, or move on to grading."""
    database = _database(request)
    draft = wizard_service.load_draft(database, draft_id)
    if action == "next":
        return RedirectResponse(f"/goals/new/{draft_id}/save", status_code=303)
    try:
        draft = wizard_service.add_task(draft, name=name, prompt_text=prompt_text)
    except SuiteError as exc:
        return _error(
            "goals/wizard_tasks.html",
            exc,
            draft=draft,
            step=WizardStep.TASKS,
            cost=wizard_service.grading_cost_sentence(draft),
        )
    wizard_service.save_draft(database, draft)
    return RedirectResponse(f"/goals/new/{draft_id}/tasks", status_code=303)


@router.get("/goals/new/{draft_id}/save", response_class=HTMLResponse)
def wizard_save(request: Request, draft_id: str) -> HTMLResponse:
    """Step 7: name the goal, write the pack, and say plainly what the user now owns.

    Reached before grading rather than after it, and deliberately: the pack has to exist on disk
    before its calibration samples can be attached to it, and a user who stops here owns a
    complete, runnable, uncalibrated goal rather than nothing. Steps 5 and 6 continue from here.
    """
    database = _database(request)
    draft = wizard_service.load_draft(database, draft_id)
    goal = None
    if draft.saved_slug:
        from freeweight.services.goals import get_goal

        goal = get_goal(_goals_root(request), draft.saved_slug)
    return _page(
        "goals/wizard_save.html",
        draft=draft,
        step=WizardStep.SAVE,
        goal=goal,
        cost=wizard_service.grading_cost_sentence(draft),
        error=None,
    )


@router.post("/goals/new/{draft_id}/save", response_class=HTMLResponse)
def wizard_save_submit(
    request: Request, draft_id: str, slug: _TextForm = "", name: _TextForm = ""
) -> HTMLResponse:
    """Write the pack under the name the user chose.

    The slug is theirs to pick because it becomes a directory name, a URL segment and the
    capability the goal's evidence is emitted under (``user.<slug>``) — three things they will
    read again later. A collision is an error they can fix here, not a generated suffix they
    would have to discover.
    """
    from dataclasses import replace

    database = _database(request)
    draft = wizard_service.load_draft(database, draft_id)
    if slug.strip() or name.strip():
        draft = wizard_service.save_draft(
            database,
            replace(
                draft,
                slug=slug.strip() or draft.slug,
                name=name.strip() or draft.name,
            ),
        )
    try:
        draft, goal = wizard_service.save_pack(database, _goals_root(request), draft)
    except SuiteError as exc:
        return _error(
            "goals/wizard_save.html",
            exc,
            draft=draft,
            step=WizardStep.SAVE,
            goal=None,
            cost=wizard_service.grading_cost_sentence(draft),
        )
    return _page(
        "goals/wizard_save.html",
        draft=draft,
        step=WizardStep.SAVE,
        goal=goal,
        cost=wizard_service.grading_cost_sentence(draft),
        error=None,
    )


def _blinded_order(slug: str, sample_ids: list[str]) -> list[str]:
    """A stable, per-goal shuffle of the calibration samples.

    Stable so that a refresh shows the same order — an order that changed under the user would
    make "the third one" meaningless mid-sitting — and derived from the goal's slug rather than
    from a clock so it is reproducible. The point is only that the order is not the order the
    models were run in, so it carries no signal about which model produced what.
    """
    return sorted(
        sample_ids,
        key=lambda sample_id: hashlib.sha256(f"{slug}:{sample_id}".encode()).hexdigest(),
    )


@router.get("/goals/{slug}/grade", response_class=HTMLResponse)
def grade_page(request: Request, slug: str) -> HTMLResponse:
    """Step 5: grade the calibration samples, blinded and shuffled.

    The one screen a user spends twenty unbroken minutes in, so three things matter more here than
    anywhere else: the model that produced a sample is not shown, the order is not the order they
    were generated in, and every grade is saved the moment it is submitted rather than at the end.
    """
    from freeweight.services.calibration import grading_progress
    from freeweight.services.goals import get_goal

    database = _database(request)
    goal = get_goal(_goals_root(request), slug)
    progress = grading_progress(database, goal)
    samples = _stored_samples(database, goal)
    order = _blinded_order(slug, [sample["id"] for sample in samples])
    by_id = {sample["id"]: sample for sample in samples}
    return _page(
        "goals/grade.html",
        step=WizardStep.GRADE,
        goal=goal,
        progress=progress,
        samples=[by_id[sample_id] for sample_id in order],
        criteria=goal.pack.judged_criteria,
        error=None,
    )


def _stored_samples(database: Database, goal: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """This goal's calibration samples, with the grades already recorded against them.

    The model that produced a sample is deliberately **not** read: blinding is enforced by not
    fetching the identity rather than by not rendering it, so a template change cannot leak it.
    """
    from freeweight.infrastructure.db.repositories.calibration import (
        CalibrationGradeRepository,
        CalibrationSampleRepository,
    )
    from freeweight.infrastructure.db.repositories.goals import GoalRepository

    with database.read() as session:
        row = GoalRepository().get_by_slug(session, goal.pack.slug)
        if row is None:
            return []
        criterion_keys = {
            value: key for key, value in GoalRepository().criterion_ids(session, row.id).items()
        }
        grades: dict[str, dict[str, Any]] = {}
        for grade in CalibrationGradeRepository().list_for_goal(session, row.id):
            grades.setdefault(grade.calibration_sample_id, {})[
                criterion_keys.get(grade.goal_criterion_id, "")
            ] = {"grade": grade.grade, "note": grade.note or ""}
        return [
            {"id": sample.id, "content": sample.content, "grades": grades.get(sample.id, {})}
            for sample in CalibrationSampleRepository().list_for_goal(session, row.id)
        ]


@router.post("/goals/{slug}/grade")
def grade_submit(  # noqa: PLR0913 — one grade is exactly these fields
    request: Request,
    slug: str,
    sample_id: _TextForm = "",
    criterion: _TextForm = "",
    grade: _TextForm = "",
    note: _TextForm = "",
) -> Any:  # noqa: ANN401 — a redirect or the step's own error state
    """Record one grade and return to the grading screen.

    One grade per submission, upserted by ``(sample, criterion)``. That is what makes an
    out-of-order submission harmless — a user who goes back and regrades sample three after
    sample seven replaces one row rather than appending a second — and what makes a refresh
    lose nothing.
    """
    from freeweight.services.calibration import GradeSubmission, record_grades
    from freeweight.services.goals import get_goal

    database = _database(request)
    goal = get_goal(_goals_root(request), slug)
    try:
        record_grades(
            database,
            goal,
            [
                GradeSubmission(
                    sample_id=sample_id,
                    criterion_key=criterion,
                    grade=int(grade),
                    note=note,
                )
            ],
            graded_by="ui",
        )
    except (SuiteError, ValueError):
        # The grading screen shows the stored state, which is the truth; a rejected grade simply
        # did not land, and the page will show that the sample is still ungraded.
        return RedirectResponse(f"/goals/{slug}/grade", status_code=303)
    return RedirectResponse(f"/goals/{slug}/grade", status_code=303)


@router.get("/goals/{slug}/report", response_class=HTMLResponse)
def report_page(request: Request, slug: str) -> HTMLResponse:
    """Step 6: the agreement, in words, with the samples that disagreed.

    The band and the consequence are stated in sentences; the coefficient and ``n_holdout`` sit
    beside them. A screen that showed ``kappa_w = 0.62`` and nothing else would be telling a
    number to someone who has no way to act on it (Subjective Goals §5.5).
    """
    from freeweight.services.calibration import latest_outcome
    from freeweight.services.goals import get_goal

    database = _database(request)
    goal = get_goal(_goals_root(request), slug)
    return _page(
        "goals/report.html",
        step=WizardStep.AGREEMENT,
        goal=goal,
        outcome=latest_outcome(database, goal),
        bands=BAND_TABLE,
        error=None,
    )


BAND_TABLE: tuple[tuple[str, str, str], ...] = (
    ("Strong", "0.75 and above", "The judge tracks your grading closely."),
    ("Good", "0.60 to 0.75", "Usable; expect the occasional sample you would score differently."),
    ("Fair", "0.40 to 0.60", "Evidence is emitted, but confidence is reduced substantially."),
    (
        "Not measurable yet",
        "below 0.40",
        "Results run and are fully inspectable; no evidence is emitted.",
    ),
)
"""Subjective Goals §5.5's bands, as the report screen states them.

Words first, coefficient second. The consequence is part of the band because a user reading
"0.41" needs to know that it is the difference between evidence and no evidence."""
