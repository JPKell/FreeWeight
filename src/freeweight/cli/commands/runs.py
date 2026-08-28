"""freeweight.cli.commands.runs — start, list, show, cancel, wait.

All five are **local** mode (CLI standards §6): they open the configured database in-process and
need no server. ``start`` additionally runs the scheduler in this process, so a run started from a
terminal executes in that terminal — which is what makes ``freeweight run start --suite
native.echo`` a complete demonstration of the engine on a machine where nothing else is running.

CLI standards §6 says a command that mutates state a running server also owns is *client* mode
when a server is up. That rule matters and is not yet implementable: FreeWeight has no HTTP client
layer before Phase 10's tokens and no way to detect a peer. Until then ``start`` protects the
invariant the rule exists to protect — one GPU workload at a time — through the claim itself:
:meth:`~freeweight.infrastructure.db.repositories.runs.RunRepository.claim_next_queued` refuses
while any run is in flight, whichever process holds it, so this command queues the run and reports
that it could not execute it here (exit ``7``). Nothing is lost — the run is persisted, and the
other process's scheduler claims it.

Exit codes (CLI standards §4) are part of the contract and each is tested: ``0`` success, ``2`` a
usage error, ``3`` a configuration error, ``4`` the database is unavailable or a wait timed out,
``5`` the run executed and did not succeed, ``6`` cancelled — including ``Ctrl-C``, which cancels
the run cleanly and preserves its committed samples — and ``7`` the machine's one execution slot is
held by another process.

Only ``typer``, ``json`` and ``time`` load at import time, so registering this subgroup never pulls
in SQLAlchemy, ModelRack or FastAPI (CLI standards §12).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from modelrack.provider import Provider
    from sweatmeter import TelemetryCollector

    from freeweight.config import Settings
    from freeweight.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Start, inspect and cancel benchmark runs.")

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_SUCCESS_STATUSES = frozenset({"completed"})


@contextmanager
def _open_backend(config: str | None) -> Iterator[tuple[Settings, Database, Provider]]:
    """Resolve configuration, open the database and build the provider, or exit 3."""
    from freeweight.config import ConfigurationError, load_settings
    from freeweight.infrastructure.providers.factory import build_provider
    from freeweight.services.database import Database

    try:
        loaded = load_settings(config_path=config)
        provider = build_provider(loaded.settings.provider)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    storage = loaded.settings.storage
    if storage.database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(
        storage.database_url, statement_timeout_ms=storage.statement_timeout_ms
    ) as database:
        yield loaded.settings, database, provider


def _collector() -> TelemetryCollector:
    """Build the collector used to profile this machine when a run is created."""
    from freeweight.services.telemetry import build_collector

    return build_collector()


def _exit_code_for(status: str) -> int:
    """Map a terminal run status to this command's exit code (CLI standards §4)."""
    if status in _SUCCESS_STATUSES:
        return 0
    if status == "cancelled":
        return 6
    return 5


_ConfigOption = Annotated[str | None, typer.Option("--config", help="Path to a config.toml file.")]
_JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


def _run_json(summary: Any) -> dict[str, Any]:  # noqa: ANN401 — a RunSummary
    """Render one run summary as the ``--json`` object.

    ``run_id`` is the first key and is also the CLI's own name for it (CLI standards §11's worked
    example pipes ``.run_id`` into the next command), so both names are present rather than one of
    them being right only in the documentation.
    """
    from baseaicore.timeutil import to_rfc3339

    return {
        "run_id": summary.id,
        "id": summary.id,
        "status": summary.status,
        "suite": summary.suite_key,
        "suite_version": summary.suite_version,
        "model": summary.model_canonical_id,
        "label": summary.label,
        "created_at": to_rfc3339(summary.created_at),
        "started_at": to_rfc3339(summary.started_at) if summary.started_at else None,
        "completed_at": to_rfc3339(summary.completed_at) if summary.completed_at else None,
        "reproducibility_fingerprint": summary.reproducibility_fingerprint,
        "error_code": summary.error_code,
        "error_text": summary.error_text,
    }


@app.command("start")
def start(  # noqa: PLR0913 — every parameter is a documented run option, not incidental state
    model: Annotated[str, typer.Option("--model", help="Model ULID, canonical ID or name.")],
    suite: Annotated[str, typer.Option("--suite", help="Benchmark suite key, e.g. native.echo.")],
    label: Annotated[str | None, typer.Option("--label", help="A label for this run.")] = None,
    repetitions: Annotated[
        int | None,
        typer.Option("--repetitions", help="Measured repetitions per case; overrides config."),
    ] = None,
    context_size: Annotated[
        int | None,
        typer.Option(
            "--context-size",
            min=1,
            help=(
                "Tokens of context to serve for this run; overrides [runtime].context_size. "
                "Two runs at two contexts are two subjects and are never merged."
            ),
        ),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option(
            "--detach/--no-detach",
            help="Queue the run and exit instead of executing it in this process.",
        ),
    ] = False,
    allow_prompt_override: Annotated[
        bool,
        typer.Option(
            "--allow-prompt-override/--no-allow-prompt-override",
            help=(
                "Proceed when this suite renders a prompt your override directory replaces. "
                "The override is recorded in the run's reproducibility fingerprint, which "
                "separates its results from runs of the shipped prompt."
            ),
        ),
    ] = False,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Queue a run and, unless ``--detach``, execute it here. Mode: local.

    Prints the run id as soon as the run is persisted — before execution — so a script can capture
    it even if the run is later cancelled or fails (CLI standards §11).

    ``Ctrl-C`` cancels the run cleanly and exits ``6``: the write in flight is rolled back by
    :func:`~freeweight.infrastructure.db.session.session_scope` (which catches ``BaseException``
    for this reason), every sample already committed is kept, and the run is finished as
    ``cancelled`` rather than being left in ``cancelling``.

    Exits ``7`` without executing when another process already holds the machine's one execution
    slot. The run stays queued and that process's scheduler will claim it; ``freeweight run wait``
    is the command that then follows it to completion.

    ``--context-size`` sets the context the model is served at (ADR-0023 §3). Without it the
    provider decides and the run records its served context as ``assumed``; with it the run is
    ``configured`` and the number in the fingerprint is a fact. It also changes the
    ``runtime_profile_hash``, which is what makes measuring one model at 8K and again at 64K two
    comparable-in-their-own-right subjects rather than two runs FreeWeight would merge.

    Example:
        freeweight run start --model ollama/qwen3.5:9b --suite native.performance

    Example:
        freeweight run start --model ollama/qwen3.5:9b --suite native.memory_kv --context-size 8192
    """
    from baseaicore import SuiteError

    from freeweight.services.runs import ExecutionConfig, build_registry_for, create_run
    from freeweight.services.scheduler import RunScheduler

    with _open_backend(config) as (settings, database, provider):
        registry = build_registry_for(settings)
        # One collector for the whole command: the run is created against the machine it profiles
        # and executed with telemetry sampled from the same instrument.
        collector = _collector()
        try:
            summary = create_run(
                database,
                provider,
                collector,
                registry,
                model_ref=model,
                suite_key=suite,
                execution=ExecutionConfig.resolve(
                    settings.execution, measured_repetitions=repetitions
                ),
                runtime_profile=(
                    settings.runtime.model_copy(update={"context_size": context_size}).to_profile()
                    if context_size is not None
                    else settings.runtime.to_profile()
                ),
                label=label,
                allow_prompt_override=allow_prompt_override,
            )
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(_start_failure_code(exc.code)) from exc

        if json_output:
            typer.echo(json.dumps({"run_id": summary.id, "status": summary.status}))
        else:
            typer.echo(f"Queued run {summary.id} ({suite} against {summary.model_canonical_id}).")

        if detach:
            return

        # Deliberately not `scheduler.start()`: that runs startup recovery, which would mark a
        # run another process is executing `interrupted`. This command drives the loop body
        # directly instead, so it can only ever take a run nothing else has claimed.
        scheduler = RunScheduler(
            database,
            provider,
            registry=registry,
            collector=collector,
            telemetry=settings.telemetry,
            settings=settings,
        )
        try:
            while True:
                current = _reload(database, summary.id)
                if current.status in _TERMINAL_STATUSES:
                    break
                if scheduler.run_once() is None:
                    typer.echo(
                        f"Another run holds this machine; {summary.id} stays queued. "
                        "Follow it with `freeweight run wait`.",
                        err=True,
                    )
                    raise typer.Exit(7)
        except KeyboardInterrupt:
            final = _reload(database, summary.id)
            typer.echo(f"Cancelled run {summary.id}.", err=True)
            _print_final(final, json_output=json_output)
            raise typer.Exit(6) from None

        final = _reload(database, summary.id)
        _print_final(final, json_output=json_output)
        raise typer.Exit(_exit_code_for(final.status))


def _start_failure_code(code: str) -> int:
    """Map a run-creation failure to its exit code.

    A missing model or suite is a usage error (``2``) — the user named something that does not
    exist — while anything else at this point is the database or the provider being unavailable
    (``4``). Both are stable and both are tested.
    """
    return 2 if code in {"MODEL_NOT_FOUND", "BENCHMARK_NOT_FOUND", "VALIDATION_ERROR"} else 4


def _reload(database: Database, run_id: str) -> Any:  # noqa: ANN401 — a RunSummary
    """Re-read a run after execution, so the printed status is the stored one."""
    from freeweight.services.runs import get_run

    return get_run(database, run_id).run


def _print_final(summary: Any, *, json_output: bool) -> None:  # noqa: ANN401 — a RunSummary
    """Print the run's terminal state as text or JSON."""
    if json_output:
        typer.echo(json.dumps(_run_json(summary)))
        return
    typer.echo(f"Run {summary.id} {summary.status}.")
    if summary.error_code:
        typer.echo(f"  {summary.error_code}: {summary.error_text}", err=True)


@app.command("list")
def list_command(
    status_filter: Annotated[
        str | None, typer.Option("--status", help="Only runs in this status.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum runs to show.")] = 50,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """List runs, newest first. Mode: local.

    Example:
        freeweight run list --status completed --limit 20
    """
    from freeweight.infrastructure.db.errors import DatabaseError
    from freeweight.services.runs import list_runs

    with _open_backend(config) as (_settings, database, _provider):
        try:
            runs = list_runs(database, status=status_filter, limit=limit)
        except DatabaseError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc

    if json_output:
        typer.echo(json.dumps({"runs": [_run_json(run) for run in runs]}))
        return
    if not runs:
        typer.echo("No runs yet. Start one with `freeweight run start --suite native.echo`.")
        return
    for run in runs:
        typer.echo(
            f"{run.id}  {run.status:<11}  {run.suite_key:<16}  {run.model_canonical_id}"
            f"{'  ' + run.label if run.label else ''}"
        )


@app.command("show")
def show(
    run_id: Annotated[str, typer.Argument(help="Run ULID or an unambiguous prefix.")],
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Show one run with its tests and aggregate metrics. Mode: local.

    Example:
        freeweight run show 01J9K2M --json
    """
    from baseaicore import SuiteError

    from freeweight.services.runs import get_run

    with _open_backend(config) as (_settings, database, _provider):
        try:
            detail = get_run(database, run_id)
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2 if exc.code == "RUN_NOT_FOUND" else 4) from exc

    if json_output:
        body = _run_json(detail.run)
        body["effective_config"] = detail.effective_config.to_json()
        body["tests"] = [
            {
                "id": test.id,
                "key": test.test_key,
                "status": test.status,
                "skip_reason": test.skip_reason,
                "completed_cases": test.completed_cases,
                "total_cases": test.total_cases,
                "repetitions": test.repetitions,
            }
            for test in detail.tests
        ]
        body["metrics"] = [
            {
                "key": metric.metric_key,
                "run_test_id": metric.run_test_id,
                "value": (
                    "unsupported" if metric.unavailable_reason is not None else metric.numeric_value
                ),
                "unit": metric.unit,
                "sample_count": metric.sample_count,
                "excluded_count": metric.excluded_count,
            }
            for metric in detail.metrics
        ]
        typer.echo(json.dumps(body))
        return

    run = detail.run
    typer.echo(f"Run {run.id}  {run.status}")
    typer.echo(f"  suite       {run.suite_key} {run.suite_version}")
    typer.echo(f"  model       {run.model_canonical_id}")
    typer.echo(f"  fingerprint {run.reproducibility_fingerprint}")
    if run.error_code:
        typer.echo(f"  error       {run.error_code}: {run.error_text}")
    typer.echo("  tests")
    for test in detail.tests:
        skip = f"  ({test.skip_reason})" if test.skip_reason else ""
        typer.echo(
            f"    {test.test_key:<16} {test.status:<10} "
            f"{test.completed_cases}/{test.total_cases} cases{skip}"
        )
    typer.echo("  metrics")
    for metric in detail.metrics:
        scope = "run" if metric.run_test_id is None else "test"
        # An unavailable metric prints "—", never 0 (ADR-0016, UI standards §5).
        value = "—" if metric.unavailable_reason is not None else f"{metric.numeric_value:.4f}"
        typer.echo(
            f"    {metric.metric_key:<28} {scope:<5} {value:>10} {metric.unit:<8} "
            f"n={metric.sample_count} excluded={metric.excluded_count}"
        )


@app.command("repeat")
def repeat(  # noqa: PLR0913 — every parameter is a documented option, not incidental state
    run_id: Annotated[str, typer.Argument(help="Run ULID or an unambiguous prefix.")],
    check: Annotated[
        bool,
        typer.Option("--check", help="Diff the repeat's provenance against the original after it."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Repeat anyway, recording every divergence on the new run."),
    ] = False,
    detach: Annotated[
        bool,
        typer.Option("--detach/--no-detach", help="Queue the repeat and exit without running it."),
    ] = False,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Re-run a recorded run with its identical effective configuration. Mode: local.

    The reproduction workflow of Machine Identity §7. The original run's frozen execution
    parameters are reused verbatim — never re-resolved from configuration, which would silently
    repeat a *different* run whenever a default had changed since.

    **Refuses, with reasons, when the environment has moved.** A changed model digest, a different
    machine, an upgraded provider or a moved dataset each make the repeat a measurement of
    something else; the command exits ``5`` and prints what changed, what was recorded and what is
    here now. ``--force`` proceeds and records the divergence on the new run's degradations rather
    than pretending the two runs match.

    ``--check`` prints a field-level diff of the two runs' fingerprint documents after the repeat
    finishes, so "is that other result the same thing?" is answered by naming the fields that
    differ rather than by two hex strings.

    Exit codes are ``run start``'s: ``0`` completed, ``5`` failed (including a refused repeat),
    ``6`` cancelled, ``7`` another process holds the machine's execution slot.

    Example:
        freeweight run repeat 01J9K2M --check
    """
    from baseaicore import SuiteError

    from freeweight.domain.provenance import diff_documents
    from freeweight.services.runs import build_registry_for, get_run, repeat_run
    from freeweight.services.scheduler import RunScheduler

    with _open_backend(config) as (settings, database, provider):
        registry = build_registry_for(settings)
        collector = _collector()
        try:
            original = get_run(database, run_id).run
            summary = repeat_run(
                database,
                provider,
                collector,
                registry,
                run_ref=run_id,
                force=force,
            )
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            for blocker in exc.details.get("blockers", []):
                typer.echo(
                    f"  {blocker['field']}: recorded {blocker['recorded']!r}, "
                    f"now {blocker['observed']!r}",
                    err=True,
                )
            raise typer.Exit(2 if exc.code == "RUN_NOT_FOUND" else 5) from exc

        if json_output:
            typer.echo(
                json.dumps(
                    {"run_id": summary.id, "status": summary.status, "repeat_of": original.id}
                )
            )
        else:
            typer.echo(f"Queued run {summary.id}, repeating {original.id}.")
        if detach:
            return

        scheduler = RunScheduler(
            database,
            provider,
            registry=registry,
            collector=collector,
            telemetry=settings.telemetry,
            settings=settings,
        )
        try:
            while True:
                current = _reload(database, summary.id)
                if current.status in _TERMINAL_STATUSES:
                    break
                if scheduler.run_once() is None:
                    typer.echo(
                        f"Another run holds this machine; {summary.id} stays queued.", err=True
                    )
                    raise typer.Exit(7)
        except KeyboardInterrupt:
            typer.echo(f"Cancelled run {summary.id}.", err=True)
            raise typer.Exit(6) from None

        final = _reload(database, summary.id)
        if check:
            _print_fingerprint_diff(database, original.id, summary.id, diff=diff_documents)
        _print_final(final, json_output=json_output)
        raise typer.Exit(_exit_code_for(final.status))


def _document(row: Any) -> dict[str, Any]:  # noqa: ANN401 — a runs row
    """Return one run's stored fingerprint document, or an empty one when it has none."""
    body = getattr(row, "fingerprint_document_json", None)
    return dict(body) if isinstance(body, dict) else {}


def _print_fingerprint_diff(
    database: Database, original_id: str, repeat_id: str, *, diff: Any
) -> None:
    """Print the field-level diff between two runs' fingerprint documents.

    Machine Identity §4 rule 3: two runs with different fingerprints are never silently merged,
    and what separates them is shown field by field. "No difference" is printed explicitly rather
    than left as silence, because silence is also what a diff nobody computed looks like.
    """
    from freeweight.infrastructure.db.repositories.runs import RunRepository

    with database.read() as session:
        repository = RunRepository()
        left = repository.get_by_id(session, original_id)
        right = repository.get_by_id(session, repeat_id)
        before = _document(left)
        after = _document(right)
    differences = diff(before, after)
    if not differences:
        typer.echo("Provenance identical: every fingerprint input matched the original run.")
        return
    typer.echo(f"Provenance differs on {len(differences)} field(s):")
    for entry in differences:
        typer.echo(f"  {entry.path}: {entry.left!r} -> {entry.right!r}")


@app.command("cancel")
def cancel(
    run_id: Annotated[str, typer.Argument(help="Run ULID or an unambiguous prefix.")],
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Cancel a run. Mode: local.

    A queued run is cancelled immediately. A running run enters ``cancelling`` and stops at the
    executing process's next boundary — this command reports that honestly rather than claiming a
    completion it cannot observe.

    Example:
        freeweight run cancel 01J9K2M
    """
    from baseaicore import SuiteError

    from freeweight.services.events import RunEventPublisher
    from freeweight.services.runs import cancel_run

    with _open_backend(config) as (_settings, database, _provider):
        try:
            summary = cancel_run(database, RunEventPublisher(database), run_id)
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2 if exc.code == "RUN_NOT_FOUND" else 5) from exc

    if json_output:
        typer.echo(json.dumps(_run_json(summary)))
    else:
        typer.echo(f"Run {summary.id} is now {summary.status}.")


@app.command("wait")
def wait(
    run_id: Annotated[str, typer.Argument(help="Run ULID or an unambiguous prefix.")],
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait before giving up.")
    ] = 3600.0,
    poll: Annotated[float, typer.Option("--poll", help="Seconds between status checks.")] = 0.5,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Wait for a run to reach a terminal state, then exit with a code that reflects it.

    Mode: local. Exit ``0`` completed, ``5`` failed or interrupted, ``6`` cancelled, ``4`` the
    timeout elapsed with the run still going (the run is untouched — a timeout on *watching*
    something is not a reason to stop it).

    ``Ctrl-C`` while waiting cancels the run and exits ``6``, which is what acceptance criterion 3
    asks for: the signal reaches the run, not just the watcher.

    Example:
        freeweight run wait 01J9K2M --timeout 3600
    """
    from baseaicore import SuiteError

    from freeweight.services.events import RunEventPublisher
    from freeweight.services.runs import cancel_run, get_run

    with _open_backend(config) as (_settings, database, _provider):
        try:
            summary = get_run(database, run_id).run
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2 if exc.code == "RUN_NOT_FOUND" else 4) from exc

        publisher = RunEventPublisher(database)
        deadline = time.monotonic() + timeout
        while summary.status not in _TERMINAL_STATUSES:
            if time.monotonic() >= deadline:
                typer.echo(
                    f"Timed out after {timeout:g}s; run {summary.id} is still {summary.status}.",
                    err=True,
                )
                raise typer.Exit(4)
            try:
                time.sleep(poll)
            except KeyboardInterrupt:
                cancel_run(database, publisher, summary.id)
                typer.echo(f"Cancelled run {summary.id}.", err=True)
                raise typer.Exit(6) from None
            summary = get_run(database, summary.id).run

    _print_final(summary, json_output=json_output)
    raise typer.Exit(_exit_code_for(summary.status))
