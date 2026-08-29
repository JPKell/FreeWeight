"""freeweight.cli.commands.goals — author, inspect, validate and move a user's goal packs.

Spec §7.2's ``goals`` group: ``list``, ``show``, ``init``, ``edit``, ``validate``,
``suggest-rules``, ``export`` and ``import`` for the pack itself, and ``calibrate``, ``grade``,
``calibration show`` and ``report`` for the instrument that scores its judged criteria.

Every command here is **local** mode (CLI standards §6): it reads the pack directory and, where it
needs one, the configured database. None of them needs a server.

**Nothing here rewrites the user's rubric.** ``validate`` names problems, ``suggest-rules`` names
rules that could carry part of a judged criterion, and both stop there. A model that reworded a
criterion until it became measurable would be optimizing the target into the instrument
([ADR-0031 §3](../../../../docs/adr/0031-user-defined-goal-benchmarks.md)).

Only ``typer`` and ``json`` are imported at module level, so registering this subgroup — which
:mod:`freeweight.cli.main` does eagerly, to build ``--help`` — never pulls in SQLAlchemy or
Jinja2 (CLI standards §12).
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from pathlib import Path

    from freeweight.services.goals import LoadedGoal

__all__ = ["app"]

app = typer.Typer(help="Author, inspect and move user-authored goal packs.")

_JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]
_ConfigOption = Annotated[
    str | None, typer.Option("--config", help="Path to config.toml.", envvar="FREEWEIGHT_CONFIG")
]

_EXIT_CONFIGURATION = 3
_EXIT_OPERATION_FAILED = 5
_EXIT_USAGE = 2

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _goals_root(config: str | None) -> Path:
    """Resolve ``goals.root`` from the configuration, or exit 3."""
    from baseaicore import ConfigurationError

    from freeweight.config import load_settings

    try:
        return load_settings(config_path=config).settings.goals.root_path
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(_EXIT_CONFIGURATION) from exc


def _load(root: Path, slug: str) -> LoadedGoal:
    """Load one goal, or exit 5 with its refusal."""
    from baseaicore import SuiteError

    from freeweight.services.goals import get_goal

    try:
        return get_goal(root, slug)
    except SuiteError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(_EXIT_OPERATION_FAILED) from exc


def _goal_json(goal: LoadedGoal) -> dict[str, Any]:
    """Render one goal as the object ``--json`` prints, matching the HTTP field names."""
    from freeweight.services.goals import summarize

    summary = summarize(goal)
    return {
        "slug": summary.slug,
        "name": summary.name,
        "goal_hash": summary.goal_hash,
        "goal_pack_version": summary.goal_pack_version,
        "capability_id": summary.capability_id,
        "contributes_to": summary.contributes_to,
        "score_method_mix": dict(summary.score_method_mix),
        "unforked": summary.unforked,
        "criteria": [
            {
                "key": criterion.key,
                "name": criterion.name,
                "rung": criterion.rung.value,
                "weight": criterion.weight,
                "gate": criterion.is_gate,
                "rule_type": criterion.rule_type,
                "scale_points": None if criterion.scale is None else criterion.scale.points,
            }
            for criterion in goal.pack.criteria
        ],
        "tasks": [
            {
                "key": task.key,
                "name": task.name,
                "prompt_id": task.prompt_id,
                "prompt_version": task.prompt_version,
                "is_starter": task.is_starter,
            }
            for task in goal.pack.tasks
        ],
        "findings": [finding.as_json() for finding in goal.findings],
        "pack_path": str(goal.pack_path),
    }


@app.command("list")
def list_goals_command(config: _ConfigOption = None, json_output: _JsonOption = False) -> None:
    """List every installed goal pack. Mode: local.

    A pack that cannot be parsed at all is omitted rather than hiding the ones that can:
    ``freeweight goals validate`` is where a broken pack is explained.

    Example:
        freeweight goals list --json
    """
    from freeweight.services.goals import list_goals, summarize

    root = _goals_root(config)
    goals = list_goals(root)
    if json_output:
        typer.echo(json.dumps({"items": [_goal_json(goal) for goal in goals]}))
        return
    if not goals:
        typer.echo(f"No goal packs installed under {root}.")
        return
    for goal in goals:
        summary = summarize(goal)
        deterministic = summary.score_method_mix["rule"] + summary.score_method_mix["reference"]
        marker = " (unforked starter)" if summary.unforked else ""
        typer.echo(
            f"{summary.slug:<28} {summary.goal_pack_version:<8} "
            f"{summary.criteria_count} criteria, {summary.task_count} tasks, "
            f"{deterministic:.0%} deterministic{marker}"
        )
        typer.echo(f"{'':<28} {summary.goal_hash}")


@app.command("show")
def show(
    slug: Annotated[str, typer.Argument(help="The goal's slug.")],
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Show one goal's criteria, tasks and lint findings. Mode: local.

    Example:
        freeweight goals show creative_voice
    """
    goal = _load(_goals_root(config), slug)
    if json_output:
        typer.echo(json.dumps(_goal_json(goal)))
        return
    typer.echo(f"{goal.pack.slug}  {goal.pack.name}")
    typer.echo(f"  capability   {goal.pack.capability_id}")
    typer.echo(f"  goal_hash    {goal.goal_hash}")
    typer.echo(f"  version      {goal.pack.goal_pack_version}")
    if goal.pack.intent:
        typer.echo(f"  intent       {goal.pack.intent}")
    typer.echo("  criteria")
    for criterion in goal.pack.criteria:
        gate = " [gate]" if criterion.is_gate else ""
        detail = criterion.rule_type or (
            f"{criterion.scale.points}-point scale" if criterion.scale else "no scale"
        )
        typer.echo(
            f"    {criterion.key:<24} {criterion.rung.value:<10} "
            f"{criterion.weight:>5.2f}  {detail}{gate}"
        )
    typer.echo("  tasks")
    for task in goal.pack.tasks:
        typer.echo(f"    {task.key:<24} {task.prompt_id} v{task.prompt_version}")
    _print_findings(goal.findings)


def _print_findings(findings: Any) -> None:  # noqa: ANN401 — a Sequence[Finding]
    """Print lint findings, most severe first."""
    if not findings:
        return
    typer.echo("  findings")
    for finding in sorted(findings, key=lambda item: _SEVERITY_ORDER[item.severity.value]):
        where = f" [{finding.criterion_key}]" if finding.criterion_key else ""
        typer.echo(f"    {finding.severity.value:<8}{where} {finding.code}: {finding.message}")


@app.command("validate")
def validate(
    slug: Annotated[str, typer.Argument(help="The goal's slug.")],
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Name every problem one pack has, with a severity each. Mode: local.

    Exits ``5`` when any finding is an error, ``0`` otherwise. Warnings do not fail: the lint's
    judgement about a mechanizable criterion is a suggestion, and the user owns the rubric.

    Example:
        freeweight goals validate creative_voice
    """
    from freeweight.domain.goals.lint import has_errors

    goal = _load(_goals_root(config), slug)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "slug": goal.pack.slug,
                    "goal_hash": goal.goal_hash,
                    "valid": not has_errors(goal.findings),
                    "findings": [finding.as_json() for finding in goal.findings],
                }
            )
        )
    else:
        _print_findings(goal.findings)
        if not goal.findings:
            typer.echo(f"{goal.pack.slug}: no findings.")
    if has_errors(goal.findings):
        raise typer.Exit(_EXIT_OPERATION_FAILED)


@app.command("suggest-rules")
def suggest_rules_command(
    slug: Annotated[str, typer.Argument(help="The goal's slug.")],
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Propose rung-2 rules that could carry part of a judged criterion. Mode: local.

    Proposals only; nothing is applied. Accepting one moves weight off the judge and onto a rule,
    which raises the goal's ``judge_validity_factor`` arithmetically (ADR-0032 §2) — and rules are
    free, exact, and never disagree with you.

    Example:
        freeweight goals suggest-rules creative_voice
    """
    from freeweight.services.goals import suggest_rules_for_pack

    goal = _load(_goals_root(config), slug)
    proposals = suggest_rules_for_pack(goal)
    if json_output:
        typer.echo(json.dumps({"slug": goal.pack.slug, "proposals": proposals}))
        return
    if not proposals:
        typer.echo(f"{goal.pack.slug}: no rule proposals. Nothing here looks mechanizable.")
        return
    for key, rules in proposals.items():
        criterion = goal.pack.criterion(key)
        typer.echo(f"{key} ({criterion.name if criterion else key})")
        for rule_type in rules:
            typer.echo(f"  -> {rule_type}")


@app.command("export")
def export(
    slug: Annotated[str, typer.Argument(help="The goal's slug.")],
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Write the bundle here instead of to stdout."),
    ] = None,
    config: _ConfigOption = None,
) -> None:
    """Write one goal as a portable, hash-pinned bundle. Mode: local.

    The bundle is every file of the pack in one JSON document, which is what ``goals import``
    reads. The ``benchmark.goal_pack`` SetSpec envelope — the cross-application contract — is
    served by ``GET /api/v1/goals/{slug}/export`` instead: an envelope names a task's prompt by id
    and hash, which is what a consumer needs and not what an importer could rebuild a pack from.

    Example:
        freeweight goals export creative_voice --output ./creative_voice.json
    """
    from pathlib import Path as _Path

    from freeweight.services.goals import export_bundle

    goal = _load(_goals_root(config), slug)
    document = json.dumps(export_bundle(goal), indent=2, ensure_ascii=False) + "\n"
    if output is None:
        typer.echo(document, nl=False)
        return
    destination = _Path(output).expanduser()
    destination.write_text(document, encoding="utf-8")
    destination.chmod(0o600)
    typer.echo(f"Wrote {destination} ({goal.goal_hash}).")


@app.command("import")
def import_command(
    file: Annotated[
        str, typer.Option("--file", "-f", help="The bundle to import, or - for stdin.")
    ],
    slug: Annotated[
        str | None,
        typer.Option("--slug", help="Import under this slug instead of the bundle's own."),
    ] = None,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Import a goal bundle. Mode: local.

    Size, path containment, schema, hash and slug availability are all checked against the bundle
    in memory; only then is anything written, and it is written to a fresh directory. An import
    never overwrites an existing goal in place — a colliding slug is refused with the existing
    hash named, and ``--slug`` is how you import it alongside.

    Example:
        freeweight goals import ./creative_voice.json --slug my_voice
    """
    from baseaicore import SuiteError

    from freeweight.config import load_settings
    from freeweight.services.goals import import_bundle

    settings = load_settings(config_path=config).settings
    text = sys.stdin.read() if file == "-" else _read_file(file)
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: the bundle is not valid JSON: {exc} (GOAL_INVALID)", err=True)
        raise typer.Exit(_EXIT_OPERATION_FAILED) from exc
    if not isinstance(body, dict):
        typer.echo("Error: the bundle is not a JSON object. (GOAL_INVALID)", err=True)
        raise typer.Exit(_EXIT_OPERATION_FAILED)
    try:
        goal = import_bundle(
            body,
            root=settings.goals.root_path,
            max_bytes=settings.goals.max_pack_bytes,
            slug=slug,
        )
    except SuiteError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(_EXIT_OPERATION_FAILED) from exc
    if json_output:
        typer.echo(json.dumps(_goal_json(goal)))
        return
    typer.echo(f"Imported {goal.pack.slug} ({goal.goal_hash}) into {goal.pack_path}.")
    _print_findings(goal.findings)


def _read_file(path: str) -> str:
    """Read one file, or exit 2 naming it."""
    from pathlib import Path as _Path

    try:
        return _Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Error: could not read {path}: {exc}", err=True)
        raise typer.Exit(_EXIT_USAGE) from exc


@app.command("edit")
def edit(
    slug: Annotated[str, typer.Argument(help="The goal's slug.")],
    config: _ConfigOption = None,
) -> None:
    """Open the pack's ``goal.json`` in ``$EDITOR``, then validate it. Mode: local.

    A goal pack is hand-editable JSON and this command exists to say so out loud. It edits the
    file and re-validates; it does not parse the user's intent or write anything itself.

    Exits ``2`` when there is no ``$EDITOR`` and stdin is not a terminal — a command that would
    have prompted names the variable that would have answered it (CLI standards §5).

    Example:
        freeweight goals edit my_voice --file ./goal.json
    """
    import os
    import subprocess  # noqa: S404 — launching the user's own configured editor

    root = _goals_root(config)
    goal = _load(root, slug)
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        typer.echo(
            "Error: no $EDITOR set. The pack is plain JSON — edit "
            f"{goal.pack_path / 'goal.json'} in any editor and run "
            f"`freeweight goals validate {slug}`.",
            err=True,
        )
        raise typer.Exit(_EXIT_USAGE)
    subprocess.run([editor, str(goal.pack_path / "goal.json")], check=False)  # noqa: S603 — the user's editor, argv list, no shell
    reloaded = _load(root, slug)
    _print_findings(reloaded.findings)
    typer.echo(f"{slug}: goal_hash is now {reloaded.goal_hash}.")


@app.command("init")
def init(  # noqa: PLR0913 — a goal is authored from exactly these facts
    slug: Annotated[str, typer.Option("--slug", help="The goal's stable identifier.")],
    name: Annotated[str | None, typer.Option("--name", help="Display name.")] = None,
    intent: Annotated[
        str | None,
        typer.Option("--intent", help="What you are trying to get, in your own words."),
    ] = None,
    task: Annotated[
        list[str] | None,
        typer.Option("--task", help="A task prompt. Repeat for several; at least one is needed."),
    ] = None,
    created_by: Annotated[
        str | None, typer.Option("--created-by", help="Who is authoring this goal.")
    ] = None,
    contributes_to: Annotated[
        str | None,
        typer.Option("--contributes-to", help="A shipped capability this goal also feeds."),
    ] = None,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Write a new goal pack, interviewing for anything not supplied. Mode: local.

    The terminal half of the authoring surface (ADR-0031 §7). It writes a pack with **one starting
    criterion** — a ``forbidden_phrases`` rule with an empty list, which is the shape of the
    cheapest useful criterion — and then tells you where the file is. It deliberately does not
    invent criteria: a rubric the application wrote is a rubric the application is measuring.

    Non-interactive when every option is supplied. When stdin is not a terminal and something is
    missing, it exits ``2`` naming the option that would have answered it (CLI standards §5).

    Example:
        freeweight goals init --slug my_voice --name 'My essay voice'
    """
    from baseaicore import SuiteError
    from baseaicore.timeutil import to_rfc3339, utc_now

    from freeweight.config import load_settings
    from freeweight.services.goals import write_pack

    interactive = sys.stdin.isatty()
    resolved_name = name or _ask("Name", slug.replace("_", " ").title(), interactive, "--name")
    resolved_intent = intent or _ask(
        "What are you trying to get?", "", interactive, "--intent", required=False
    )
    resolved_by = created_by or _ask("Who is grading?", "", interactive, "--created-by")
    tasks = list(task or ())
    if not tasks:
        first = _ask(
            "One task prompt from your real work", "", interactive, "--task", required=True
        )
        tasks = [first]

    now = to_rfc3339(utc_now())
    goal_body: dict[str, Any] = {
        "slug": slug,
        "name": resolved_name,
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "intent": resolved_intent,
        "created_by": resolved_by,
        "created_at": now,
        "contributes_to": contributes_to,
        "criteria": [
            {
                "key": "no_unwanted_phrases",
                "name": "No unwanted phrases",
                "rung": "rule",
                "weight": 1.0,
                "gate": False,
                "intent": "Replace this with what you actually mean, and add more criteria.",
                "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
            }
        ],
        "calibration": {},
    }
    records: list[dict[str, Any]] = [
        {
            "prompt_id": f"goals.{slug}.task_{ordinal:03d}",
            "version": "1.0.0",
            "schema_version": "1.0",
            "purpose": f"Task {ordinal} of goal {slug!r}, supplied by its author.",
            "task": f"goal.{slug}",
            "capability": contributes_to or "creative_writing",
            "system": None,
            "template": text,
            "variables": {},
            "response": {"format": "text", "json_schema_ref": None, "expectations": []},
            "model_requirements": {
                "min_context_tokens": 2048,
                "requires_capabilities": [],
                "recommended_temperature": 0.7,
            },
            "metadata": {
                "author": resolved_by,
                "created_at": now,
                "changed_at": now,
                "change_reason": "First version, written by `freeweight goals init`.",
                "supersedes": None,
                "tags": ["goal", slug],
                "goal_task": {"key": f"task_{ordinal:03d}", "name": f"Task {ordinal}"},
            },
        }
        for ordinal, text in enumerate(tasks, start=1)
    ]
    settings = load_settings(config_path=config).settings
    try:
        goal = write_pack(settings.goals.root_path, goal=goal_body, tasks=records)
    except SuiteError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(_EXIT_OPERATION_FAILED) from exc
    if json_output:
        typer.echo(json.dumps(_goal_json(goal)))
        return
    typer.echo(f"Wrote {goal.pack_path}")
    typer.echo(f"  goal_hash {goal.goal_hash}")
    typer.echo(
        "  The pack is plain JSON: editable, diffable and portable. Edit it, then run "
        f"`freeweight goals validate {slug}`."
    )
    _print_findings(goal.findings)


def _ask(
    question: str, default: str, interactive: bool, option: str, *, required: bool = True
) -> str:
    """Prompt for one answer, or exit 2 naming the option that would have supplied it."""
    if interactive:
        answer = typer.prompt(question, default=default, show_default=bool(default))
        return str(answer)
    if default or not required:
        return default
    typer.echo(
        f"Error: {question.lower()} was not supplied and stdin is not a terminal. Pass {option}.",
        err=True,
    )
    raise typer.Exit(_EXIT_USAGE)


calibration_app = typer.Typer(help="Inspect a goal's calibration set and its agreement report.")
app.add_typer(calibration_app, name="calibration")


def _open_backend(config: str | None) -> Any:  # noqa: ANN401 — a Database context manager
    """Open the configured database, or exit 4."""
    from baseaicore import SuiteError

    from freeweight.config import load_settings
    from freeweight.services.database import Database

    settings = load_settings(config_path=config).settings
    url = settings.storage.database_url
    try:
        return settings, Database.from_url(
            str(url), statement_timeout_ms=settings.storage.statement_timeout_ms
        )
    except SuiteError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(4) from exc


def _synced(settings: Any, database: Any, slug: str) -> Any:  # noqa: ANN401 — a LoadedGoal
    """Load one goal and make sure its projection is current, or exit 5."""
    from baseaicore import SuiteError

    from freeweight.services.goals import get_goal, sync_goals

    try:
        goal = get_goal(settings.goals.root_path, slug)
        sync_goals(database, [goal])
    except SuiteError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(_EXIT_OPERATION_FAILED) from exc
    return goal


@app.command("calibrate")
def calibrate(
    slug: Annotated[str, typer.Argument(help="The goal's slug.")],
    graded_by: Annotated[
        str, typer.Option("--graded-by", help="Who graded these samples.")
    ] = "unknown",
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Score the holdout with the configured jury and report the agreement. Mode: local.

    The jury never sees the holdout before this: the anchors are the only samples that reach a
    judge prompt, and the split that decides which is which is seeded and recorded.

    Exits ``5`` when there are too few grades (``CALIBRATION_INSUFFICIENT``) — which is work still
    to do, not a rubric that failed to measure. A rubric that *did* fail to measure exits ``0``
    with an ``uncalibrated`` verdict, because that is a real and useful answer.

    Example:
        freeweight goals calibrate my_voice
    """
    from baseaicore import SuiteError

    from freeweight.services.calibration import anchors_for, run_calibration
    from freeweight.services.jury import build_jury

    settings, database = _open_backend(config)
    with database:
        goal = _synced(settings, database, slug)
        from freeweight.infrastructure.providers.factory import build_provider
        from freeweight.services.runs import active_prompt_library

        provider = build_provider(settings.provider)
        try:
            available = sorted(
                descriptor.identity.canonical_id for descriptor in provider.list_models()
            )
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc
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
            # Served under `[runtime]` for the same reason the HTTP path is.
            runtime_profile=settings.runtime.to_profile(),
        )
        try:
            outcome = run_calibration(
                database,
                goal,
                jury=jury,
                n_holdout_target=settings.calibration.n_holdout_target,
                graded_by=graded_by,
            )
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(_EXIT_OPERATION_FAILED) from exc
    if json_output:
        typer.echo(json.dumps(outcome.as_json()))
        return
    _print_outcome(outcome)


@app.command("grade")
def grade(
    slug: Annotated[str, typer.Argument(help="The goal's slug.")],
    grade_file: Annotated[
        str,
        typer.Option(
            "--file",
            "-f",
            help="A JSON list of {sample_id, criterion, grade, note}, or - for stdin.",
        ),
    ],
    graded_by: Annotated[str, typer.Option("--graded-by", help="Who is grading.")] = "unknown",
    run: Annotated[
        str | None,
        typer.Option(
            "--run",
            help=(
                "Grade a completed run's own samples on this goal's human (rung-4) criteria, "
                "instead of the calibration set. The CLI form of /runs/{id}/grade."
            ),
        ),
    ] = None,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Record grades for calibration samples, or for a run's samples with ``--run``. Mode: local.

    Idempotent per ``(sample, criterion)``: partial submission is normal, and re-grading a sample
    you changed your mind about replaces the grade rather than adding a second observation of it.

    With ``--run``, the grades land on that run's samples' ``human`` criteria: each sample's
    composite is recomputed, the run's aggregate metrics are rewritten and the subject's capability
    evidence is refreshed, exactly as the blinded grading screen does. The sample IDs come from
    ``freeweight run show <id>``; which model produced the run is deliberately not printed here.

    Example:
        freeweight goals grade my_voice --file grades.json

    Example:
        freeweight goals grade my_voice --run 01J9K2M --file grades.json --graded-by me
    """
    from baseaicore import SuiteError

    from freeweight.services.calibration import GradeSubmission, grading_progress, record_grades

    settings, database = _open_backend(config)
    text = sys.stdin.read() if grade_file == "-" else _read_file(grade_file)
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: the grade file is not valid JSON: {exc}", err=True)
        raise typer.Exit(_EXIT_USAGE) from exc
    if not isinstance(body, list):
        typer.echo("Error: the grade file must be a JSON list.", err=True)
        raise typer.Exit(_EXIT_USAGE)
    if run is not None:
        _grade_run(settings, database, run, body, graded_by=graded_by, json_output=json_output)
        return
    with database:
        goal = _synced(settings, database, slug)
        try:
            recorded = record_grades(
                database,
                goal,
                [
                    GradeSubmission(
                        sample_id=str(entry["sample_id"]),
                        criterion_key=str(entry["criterion"]),
                        grade=int(entry["grade"]),
                        note=str(entry.get("note", "")),
                    )
                    for entry in body
                ],
                graded_by=graded_by,
            )
        except (SuiteError, KeyError, TypeError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(_EXIT_OPERATION_FAILED) from exc
        progress = grading_progress(database, goal)
    if json_output:
        typer.echo(json.dumps({"recorded": recorded, **progress.as_json()}))
        return
    typer.echo(f"Recorded {recorded} grade(s).")
    typer.echo(
        f"  {progress.recorded}/{progress.expected} graded across {progress.samples} samples "
        f"and {progress.judged_criteria} criteria."
    )


def _grade_run(  # noqa: PLR0913 — the run form of `grade` takes exactly what `grade` does
    settings: Any,  # noqa: ANN401 — the resolved Settings
    database: Any,  # noqa: ANN401 — a Database context manager
    run: str,
    body: list[Any],
    *,
    graded_by: str,
    json_output: bool,
) -> None:
    """Record ``goals grade --run`` grades on a completed run's samples, or exit 5."""
    from baseaicore import SuiteError

    from freeweight.services.calibration import (
        RunGradeSubmission,
        record_run_grades,
        run_grading_view,
    )
    from freeweight.services.runs import build_registry_for

    with database:
        try:
            recorded = record_run_grades(
                database,
                run,
                [
                    RunGradeSubmission(
                        sample_id=str(entry["sample_id"]),
                        criterion_key=str(entry["criterion"]),
                        grade=int(entry["grade"]),
                        note=str(entry.get("note", "")),
                    )
                    for entry in body
                ],
                graded_by=graded_by,
                registry=build_registry_for(settings),
                evidence_settings=settings.evidence,
            )
            view = run_grading_view(database, run)
        except (SuiteError, KeyError, TypeError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(_EXIT_OPERATION_FAILED) from exc
    if json_output:
        typer.echo(json.dumps({"recorded": recorded, **view.as_json()}))
        return
    typer.echo(f"Recorded {recorded} grade(s) on run {run}.")
    typer.echo(
        f"  {view.recorded}/{view.expected} graded across {len(view.samples)} samples and "
        f"{len(view.criteria)} human criteria."
    )


@calibration_app.command("show")
def calibration_show(
    slug: Annotated[str, typer.Argument(help="The goal's slug.")],
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Show the calibration set and what remains to be graded. Mode: local.

    Example:
        freeweight goals calibration show my_voice
    """
    from freeweight.services.calibration import grading_progress

    settings, database = _open_backend(config)
    with database:
        goal = _synced(settings, database, slug)
        progress = grading_progress(database, goal)
    if json_output:
        typer.echo(json.dumps({"slug": goal.pack.slug, **progress.as_json()}))
        return
    typer.echo(f"{goal.pack.slug}: {progress.samples} calibration samples")
    typer.echo(
        f"  {progress.recorded}/{progress.expected} grades recorded "
        f"(minimum {progress.min_samples} samples, target {progress.target_samples})"
    )
    for sample_id, criterion in progress.remaining[:20]:
        typer.echo(f"  to grade: {sample_id}  {criterion}")


@app.command("report")
def report(
    slug: Annotated[str, typer.Argument(help="The goal's slug.")],
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Show the stored calibration report. Mode: local.

    Every coefficient arrives with the ``n_holdout`` it was computed over, because a coefficient
    without its sample count is a number pretending to be a fact.

    Example:
        freeweight goals report my_voice --json
    """
    from freeweight.services.calibration import latest_outcome

    settings, database = _open_backend(config)
    with database:
        goal = _synced(settings, database, slug)
        outcome = latest_outcome(database, goal)
    if outcome is None:
        if json_output:
            typer.echo(json.dumps({"slug": goal.pack.slug, "calibration_state": "insufficient"}))
            return
        typer.echo(f"{goal.pack.slug}: never calibrated.")
        return
    if json_output:
        typer.echo(json.dumps(outcome.as_json()))
        return
    _print_outcome(outcome)


def _print_outcome(outcome: Any) -> None:  # noqa: ANN401 — a CalibrationOutcome
    """Render a calibration outcome, coefficients never separated from their n."""
    from freeweight.domain.agreement import AgreementBand, band_for

    verdict = outcome.verdict
    typer.echo(f"{outcome.goal_slug}: {verdict.state.value}")
    if verdict.weighted_kappa_w is not None:
        band = band_for(verdict.weighted_kappa_w)
        typer.echo(
            f"  weighted kappa_w {verdict.weighted_kappa_w:.2f} over {verdict.n_holdout} "
            f"held-out samples (gate {verdict.min_agreement:.2f})"
        )
        typer.echo(f"  {AgreementBand.DESCRIPTIONS[band]}")
    typer.echo(f"  judge_validity_factor {verdict.judge_validity_factor:.2f}")
    for item in outcome.criteria:
        kappa = "unmeasured" if item.result.kappa_w is None else f"{item.result.kappa_w:.2f}"
        rho = "n/a" if item.result.rho is None else f"{item.result.rho:.2f}"
        typer.echo(
            f"  {item.criterion_key:<24} kappa_w {kappa} (n={item.result.n})  rho {rho}  "
            f"mae {item.result.mae:.2f}  bias {item.result.bias:+.2f}"
        )
        if item.lint:
            typer.echo(f"      {item.lint}")
        for divergence in item.disagreements:
            typer.echo(
                f"      diverged on {divergence.sample_id}: you {divergence.author_grade}, "
                f"jury {divergence.jury_grade:g}"
            )
            if divergence.author_note:
                typer.echo(f"        your note: {divergence.author_note}")
            if divergence.jury_rationale:
                typer.echo(f"        the jury:  {divergence.jury_rationale}")
    for warning in outcome.warnings:
        typer.echo(f"  ! {warning}")


@app.command("starters")
def starters(
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """List the starter packs that ship with FreeWeight. Mode: local.

    Printed in the order they are meant to be read. Down the list, the share of weight scored
    deterministically rises from 40 % to 90 % — which is the lesson: the better you understand
    what you want, the less of it needs a judge.

    They are starters, not defaults. Forking one and running it unedited is badged ``unforked``
    wherever its results appear.

    Example:
        freeweight goals starters --json

    Exit codes: ``0`` printed; ``3`` a configuration error.
    """
    import json

    from freeweight.goals.starters import list_starters

    del config
    packs = list_starters()
    if json_output:
        typer.echo(json.dumps({"items": [pack.as_json() for pack in packs]}))
        return
    for pack in packs:
        typer.echo(
            f"{pack.reading_position}. {pack.key:24} "
            f"{pack.deterministic_weight:.0%} deterministic / "
            f"{pack.judged_weight:.0%} judged  "
            f"({pack.criteria_count} criteria, {pack.task_count} tasks)"
        )
        typer.echo(f"     {pack.name}")
        typer.echo(f"     {pack.carries}")
        typer.echo("")
    typer.echo("Fork one with: freeweight goals fork-starter <key>")


@app.command("fork-starter")
def fork_starter_command(
    key: Annotated[str, typer.Argument(help="Which starter to fork.")],
    slug: Annotated[
        str | None, typer.Option("--slug", help="Name the new goal. Defaults to the starter's key.")
    ] = None,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Copy a starter pack into your own goals directory. Mode: local.

    What you get is an ordinary directory of JSON: open it in an editor, diff it in git, carry it
    to another machine. Nothing about it points back at the installed package.

    It is badged ``unforked`` until you edit its criteria or its tasks, and the badge travels into
    the UI, the results and the exports. That is deliberate: a voice measured on somebody else's
    prompts is not your voice.

    Example:
        freeweight goals fork-starter creative_voice --slug my_essay_voice

    Exit codes: ``0`` forked; ``2`` no such starter, or that slug is taken; ``3`` a configuration
    error.
    """
    import json

    from baseaicore import SuiteError

    from freeweight.goals.starters import fork_starter

    root = _goals_root(config)
    try:
        goal = fork_starter(root, key, slug=slug)
    except SuiteError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(2) from exc

    if json_output:
        typer.echo(json.dumps(_goal_json(goal)))
        return
    typer.echo(f"Forked {key} to {goal.pack_path}")
    typer.echo(f"  slug       {goal.slug}")
    typer.echo(f"  goal_hash  {goal.goal_hash}")
    typer.echo("")
    typer.echo(
        "It is badged 'unforked' until you edit its criteria or its tasks. Open "
        f"{goal.pack_path / 'goal.json'} and make it yours."
    )
