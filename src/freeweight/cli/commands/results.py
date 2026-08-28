"""freeweight.cli.commands.results — ``freeweight results compare``.

**Local** mode (CLI standards §6): it opens the configured database in-process and needs no
server. Comparing stored results mutates nothing, so there is no writer to race with.

Exit codes (CLI standards §4), and each is tested: ``0`` the comparison was produced — *including*
one in which every column is separated, because "these cannot be merged, here is why" is a result
and not a failure; ``2`` a usage error (fewer than two runs, a run named twice, an ambiguous
prefix, or a run that does not exist); ``3`` a configuration error; ``4`` the database is
unavailable.

Only ``typer`` and ``json`` load at import time, so registering this subgroup never pulls in
SQLAlchemy or the comparison engine (CLI standards §12).

``list``, ``show`` and ``export`` land here in Phase 10, completing spec §7.2's
``results list|show|compare|export``. All four are local: they read stored results and mutate
nothing, so there is no writer to race with and no server to require.

``export`` streams. It writes each chunk as it is produced rather than building the document and
printing it, so ``freeweight results export --scope all > everything.json`` costs one run of
memory rather than the whole history — the same property the HTTP endpoint has, for the same
reason (spec §15).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from freeweight.services.comparison import Comparison
    from freeweight.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Inspect and compare stored results.")

_ConfigOption = Annotated[str | None, typer.Option("--config", help="Path to a config.toml file.")]

_MINIMUM_SUBJECTS = 2
_ID_PREFIX_CHARS = 8


@contextmanager
def _open_database(config: str | None) -> Iterator[Database]:
    """Resolve configuration and open the database, or exit 3."""
    from freeweight.config import ConfigurationError, load_settings
    from freeweight.services.database import Database

    try:
        loaded = load_settings(config_path=config)
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
        yield database


def _short(run_id: str) -> str:
    """Shorten a ULID for a column header, keeping enough to be unambiguous by eye."""
    return run_id[:_ID_PREFIX_CHARS]


def _format_value(value: float | None, reason: str | None) -> str:
    """Render one cell: the number, or an em dash that carries its reason.

    Never ``0`` for an unavailable figure (UI standards §5 and ADR-0016), and never a bare blank —
    a blank cell and a refused measurement look identical in a terminal, which is exactly the
    ambiguity the em dash and the reason exist to remove.
    """
    if value is None:
        return f"— ({reason})" if reason else "—"
    return f"{value:.4g}"


def _print_text(comparison: Comparison) -> None:
    """Print the comparison as aligned columns, separations first."""
    typer.echo(f"Study: {comparison.study.value.replace('_', ' ')}")
    typer.echo("")
    typer.echo("Subjects")
    for column in comparison.columns:
        typer.echo(
            f"  {_short(column.run_id)}  {column.label}  "
            f"{column.suite_key} v{column.suite_version}  "
            f"quant={column.quantization or '—'}  kv={column.kv_cache_precision or '—'}  "
            f"profile={column.runtime_profile_hash}  "
            f"machine={column.machine_hostname or column.machine_fingerprint[:12]}"
        )

    if comparison.separations:
        typer.echo("")
        typer.echo("Separations")
        for verdict in comparison.separations:
            typer.echo(
                f"  {_short(verdict.left)} vs {_short(verdict.right)}: "
                f"{verdict.comparability.value} ({verdict.study.value})"
            )
            typer.echo(f"    {verdict.reason}")
            for entry in verdict.diff:
                typer.echo(f"    {entry.path}: {entry.left!r} -> {entry.right!r}")

    typer.echo("")
    header = "  ".join(f"{_short(column.run_id):>22}" for column in comparison.columns)
    typer.echo(f"{'metric':<38}  {header}  comparable")
    for row in comparison.rows:
        cells = "  ".join(
            f"{_format_value(cell.numeric_value, cell.unavailable_reason):>22}"
            for cell in row.cells
        )
        typer.echo(f"{row.metric_key:<38}  {cells}  {'yes' if row.mergeable else 'NO'}")
        counts = "  ".join(
            f"{f'n={cell.sample_count or 0} excl={cell.excluded_count or 0}':>22}"
            for cell in row.cells
        )
        suffix = "" if row.mergeable else f"  {len(row.groups)} groups"
        typer.echo(f"{'':<38}  {counts}{suffix}")

    _print_context_sweep(comparison)


def _print_context_sweep(comparison: Comparison) -> None:
    """Print the fitted KV cost function, when these runs happen to be a context sweep.

    Silent otherwise, which is the usual case: this is derived from the comparison rather than
    asked for, so a user who has run one model at several contexts gets the allocation function
    without having known to request it.
    """
    sweep = comparison.context_sweep
    if sweep is None:
        return
    gib = 1024**3
    kib = 1024
    typer.echo("")
    typer.echo(f"Context sweep — {sweep.model_canonical_id}")
    for context, value in sweep.points:
        typer.echo(f"  {context:>9,} tokens  {value / gib:>8.3f} GiB")
    typer.echo(
        f"  fit: {sweep.weights_bytes / gib:.3f} GiB + "
        f"{sweep.bytes_per_token / kib:.1f} KiB/token   "
        f"r^2={sweep.r_squared:.4f}  spread=±{sweep.residual_stddev_bytes / gib:.4f} GiB"
    )
    typer.echo(
        "  A study across runs, not a benchmark result: each point is one run's own reported "
        "residency."
    )


@app.command("compare")
def compare(
    runs: Annotated[
        list[str],
        typer.Argument(
            help=(
                "Two or more subjects: run ULIDs or prefixes, or model references. A model "
                "resolves to its latest completed run of --suite."
            )
        ),
    ],
    suite: Annotated[
        str | None,
        typer.Option(
            "--suite",
            help=(
                "The suite a model subject is compared at, and the guard every run subject must "
                "satisfy."
            ),
        ),
    ] = None,
    config: _ConfigOption = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """Compare two or more runs, aligned by metric key. Mode: local.

    Prints one row per metric and one column per run, with the sample and exclusion counts under
    every figure — Phase 9 acceptance criterion 3, on the surface a script reads.

    **It never averages across a boundary marked separate.** Where two runs measured different
    things — a different suite version, a different runtime profile, a different machine for a
    hardware metric — the row is marked ``NO`` in the comparable column, the runs are reported in
    separate groups, and the field-level fingerprint diff that separates them is printed above the
    table. That is a successful comparison, and it exits ``0``: the answer to "can I compare
    these?" is sometimes no, and a non-zero exit would make a script treat an honest answer as a
    breakage.

    A subject may be a run **or a model**. A reference that resolves to a run is that run; one
    that names a model resolves to that model's latest completed run of ``--suite``, which is why
    naming a model without a suite is refused — "compare these two models" has no answer until
    somebody says at what.

    ``--json`` prints the same document the HTTP API returns, with the same field names
    (CLI standards §3), so a script can pipe it straight into ``jq``.

    Example:
        freeweight results compare ollama/qwen3.5:9b ollama/llama3:8b --suite native.performance
    """
    from baseaicore import SuiteError

    from freeweight.infrastructure.db.errors import DatabaseError
    from freeweight.services.comparison import compare_runs, comparison_json, enforce_suite
    from freeweight.services.results import resolve_subject_runs

    if len(runs) < _MINIMUM_SUBJECTS:
        typer.echo(
            f"Error: a comparison needs at least {_MINIMUM_SUBJECTS} runs; got {len(runs)}. "
            "Usage: freeweight results compare RUN_A RUN_B [RUN_C …]",
            err=True,
        )
        raise typer.Exit(2)

    with _open_database(config) as database:
        try:
            comparison = compare_runs(database, resolve_subject_runs(database, runs, suite=suite))
            enforce_suite(comparison, suite)
        except DatabaseError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            for candidate in exc.details.get("candidates", []):
                typer.echo(f"  candidate: {candidate}", err=True)
            for offender in exc.details.get("offenders", []):
                typer.echo(f"  {offender['run_id']} ran {offender['suite']}", err=True)
            raise typer.Exit(2) from exc

    if json_output:
        payload: dict[str, Any] = comparison_json(comparison)
        typer.echo(json.dumps(payload))
        return
    _print_text(comparison)


@app.command("list")
def list_results(  # noqa: PLR0913 — the documented filter set, one option each
    model: Annotated[
        str | None, typer.Option("--model", help="Model canonical ID, ULID or prefix.")
    ] = None,
    suite: Annotated[str | None, typer.Option("--suite", help="Benchmark suite key.")] = None,
    metric: Annotated[str | None, typer.Option("--metric", help="Exact metric key.")] = None,
    machine: Annotated[str | None, typer.Option("--machine", help="Machine fingerprint.")] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="RFC 3339; runs created at or after.")
    ] = None,
    status: Annotated[
        str, typer.Option("--status", help="Run status, or 'any'. Default: completed.")
    ] = "completed",
    limit: Annotated[int, typer.Option("--limit", min=1, max=500, help="Rows to print.")] = 50,
    config: _ConfigOption = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """List stored metrics, newest run first. Mode: local.

    Completed runs only unless ``--status any``: a metric from a run that stopped halfway measured
    a different set of cases than the row beside it, and printing both under one heading puts the
    difference nowhere a reader can see it.

    Unavailable measurements print as ``—`` with their reason, never as ``0`` (ADR-0016).

    ``--json`` prints the same document ``GET /api/v1/results`` returns, field for field
    (CLI standards §3).

    Example:
        freeweight results list --suite native.performance --metric decode_tokens_per_second

    Exit codes: ``0`` printed; ``2`` a usage error (an unknown model, a malformed timestamp);
    ``3`` a configuration error; ``4`` the database is unavailable.
    """
    from baseaicore import SuiteError, from_rfc3339

    from freeweight.infrastructure.db.errors import DatabaseError
    from freeweight.services.results import ResultsQuery, query_results

    try:
        parsed_since = from_rfc3339(since) if since else None
    except (SuiteError, ValueError) as exc:
        typer.echo(
            f"Error: --since must be an RFC 3339 instant, such as 2026-08-28T00:00:00Z; "
            f"got {since!r}. ({exc})",
            err=True,
        )
        raise typer.Exit(2) from exc

    query = ResultsQuery(
        model=model,
        suite=suite,
        metric_key=metric,
        machine=machine,
        since=parsed_since,
        status=None if status == "any" else status,
        limit=limit,
    )
    with _open_database(config) as database:
        try:
            page = query_results(database, query)
        except DatabaseError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2) from exc

    if json_output:
        typer.echo(json.dumps(page.as_json()))
        return
    if not page.rows:
        typer.echo("No stored results match those filters.")
        return
    typer.echo(
        f"{'metric':<34}  {'value':>14}  {'unit':<14}  {'n':>5}  {'excl':>5}  "
        f"{'suite':<26}  {'run':<10}  model"
    )
    for row in page.rows:
        typer.echo(
            f"{row.metric_key:<34}  "
            f"{_format_value(row.numeric_value, row.unavailable_reason):>14}  "
            f"{row.unit:<14}  {row.sample_count:>5}  {row.excluded_count:>5}  "
            f"{row.suite_key:<26}  {_short(row.run_id):<10}  {row.model_canonical_id}"
        )
    typer.echo("")
    typer.echo(f"{len(page.rows)} rows{' (more available)' if page.has_more else ''}.")


@app.command("show")
def show(
    run: Annotated[str, typer.Argument(help="A run ULID, or an unambiguous prefix of one.")],
    config: _ConfigOption = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """Show one run: its provenance, its tests and every metric it produced. Mode: local.

    The terminal equivalent of the run detail page, and the first of the two steps to a raw
    sample: this prints the run test IDs, and ``freeweight run show`` drills into one.

    Example:
        freeweight results show 01J9K2M --json

    Exit codes: ``0`` printed; ``2`` no such run, or an ambiguous prefix; ``3`` a configuration
    error; ``4`` the database is unavailable.
    """
    from baseaicore import SuiteError

    from freeweight.infrastructure.db.errors import DatabaseError
    from freeweight.services.runs import get_run

    with _open_database(config) as database:
        try:
            detail = get_run(database, run)
        except DatabaseError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "run_id": detail.run.id,
                    "status": detail.run.status,
                    "suite": detail.run.suite_key,
                    "suite_version": detail.run.suite_version,
                    "model": detail.run.model_canonical_id,
                    "reproducibility_fingerprint": detail.run.reproducibility_fingerprint,
                    "served_context": detail.run.served_context,
                    "served_context_source": detail.run.served_context_source,
                    "tests": [
                        {
                            "run_test_id": test.id,
                            "test": test.test_key,
                            "status": test.status,
                            "skip_reason": test.skip_reason,
                            "completed_cases": test.completed_cases,
                            "total_cases": test.total_cases,
                        }
                        for test in detail.tests
                    ],
                    "metrics": [
                        {
                            "metric_key": metric.metric_key,
                            "run_test_id": metric.run_test_id,
                            "value": (
                                "unsupported"
                                if metric.numeric_value is None
                                else metric.numeric_value
                            ),
                            "unavailable_reason": metric.unavailable_reason,
                            "unit": metric.unit,
                            "aggregation": metric.aggregation,
                            "higher_is_better": metric.higher_is_better,
                            "sample_count": metric.sample_count,
                            "excluded_count": metric.excluded_count,
                        }
                        for metric in detail.metrics
                    ],
                }
            )
        )
        return

    summary = detail.run
    typer.echo(f"Run       {summary.id}")
    typer.echo(f"Status    {summary.status}")
    typer.echo(f"Suite     {summary.suite_key} v{summary.suite_version}")
    typer.echo(f"Model     {summary.model_canonical_id}")
    typer.echo(
        f"Context   {summary.served_context or '—'} ({summary.served_context_source or '—'})"
    )
    typer.echo(f"Fingerprint {summary.reproducibility_fingerprint}")
    typer.echo("")
    typer.echo("Tests")
    for test in detail.tests:
        skip = f"  ({test.skip_reason})" if test.skip_reason else ""
        typer.echo(
            f"  {_short(test.id)}  {test.test_key:<34}  {test.status:<12}  "
            f"{test.completed_cases}/{test.total_cases}{skip}"
        )
    typer.echo("")
    typer.echo(f"{'metric':<34}  {'value':>14}  {'unit':<14}  {'n':>5}  {'excl':>5}")
    for metric in detail.metrics:
        typer.echo(
            f"{metric.metric_key:<34}  "
            f"{_format_value(metric.numeric_value, metric.unavailable_reason):>14}  "
            f"{metric.unit:<14}  {metric.sample_count or 0:>5}  {metric.excluded_count or 0:>5}"
        )


@app.command("export")
def export(  # noqa: PLR0913 — the documented parameter set, one option each
    scope: Annotated[
        str, typer.Option("--scope", help="run, model, suite, comparison or all.")
    ] = "all",
    selector: Annotated[
        str | None,
        typer.Option(
            "--selector",
            help="The scope's argument: a run, a model, a suite key, or a comma-separated list.",
        ),
    ] = None,
    export_format: Annotated[str, typer.Option("--format", help="json, jsonl or csv.")] = "json",
    include_samples: Annotated[
        bool, typer.Option("--include-samples", help="Include the raw samples behind each metric.")
    ] = False,
    include_prompts: Annotated[
        bool, typer.Option("--include-prompts", help="Include each sample's prompt identity.")
    ] = False,
    include_prompt_text: Annotated[
        bool,
        typer.Option(
            "--include-prompt-text",
            help="Add an appendix of each distinct rendered prompt, so the export is auditable "
            "on a machine that does not have the prompt pack.",
        ),
    ] = False,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only runs created at or after this RFC 3339 instant."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Only runs created strictly before this RFC 3339 instant."),
    ] = None,
    output: Annotated[
        str | None, typer.Option("--output", "-o", help="Write here instead of stdout.")
    ] = None,
    config: _ConfigOption = None,
) -> None:
    """Export stored results as JSON, JSONL or CSV. Mode: local.

    Streams: each chunk is written as it is produced, so exporting a decade of measurements costs
    one run of memory rather than all of it. Writes to stdout by default, so it pipes.

    JSON and JSONL are SetSpec-wrapped; CSV is the flattened spreadsheet form (spec §7.3).
    Unavailable measurements appear as ``"unsupported"``, never as ``0`` or an empty cell.

    ``--since`` / ``--until`` bound the export by run creation time. The window is **half-open**,
    ``[since, until)``, so consecutive windows tile: a history too large for one document exports
    as several that neither overlap nor drop a run between them.

    ``--include-prompts`` gives each sample's prompt *identity*; ``--include-prompt-text`` adds the
    text itself, once per distinct prompt. Identity is enough to re-render here and not enough
    anywhere else, which is the difference between an export that is auditable and one that is
    merely referential.

    Example:
        freeweight results export --scope suite --selector native.performance --format csv

    Example:
        freeweight results export --scope all --since 2026-01-01T00:00:00Z \
            --until 2026-07-01T00:00:00Z --include-samples --include-prompt-text

    Exit codes: ``0`` written; ``2`` the scope/selector pairing is unanswerable or matched
    nothing; ``3`` a configuration error; ``4`` the database is unavailable.
    """
    import sys
    from datetime import datetime
    from pathlib import Path

    from baseaicore import SuiteError

    from freeweight.infrastructure.db.errors import DatabaseError
    from freeweight.services.export import (
        ExportFormat,
        ExportScope,
        ExportSelection,
        iter_export,
    )

    def _instant(value: str | None, flag: str) -> datetime | None:
        """Parse an RFC 3339 bound, or refuse it by name."""
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            typer.echo(
                f"Error: {flag} must be an RFC 3339 instant, e.g. 2026-01-01T00:00:00Z. ({exc})",
                err=True,
            )
            raise typer.Exit(2) from exc

    try:
        selection = ExportSelection(
            scope=ExportScope(scope),
            selector=selector,
            export_format=ExportFormat(export_format),
            include_samples=include_samples,
            include_prompts=include_prompts,
            include_prompt_text=include_prompt_text,
            since=_instant(since, "--since"),
            until=_instant(until, "--until"),
        )
    except ValueError as exc:
        typer.echo(
            f"Error: --scope must be one of {', '.join(item.value for item in ExportScope)} and "
            f"--format one of {', '.join(item.value for item in ExportFormat)}. ({exc})",
            err=True,
        )
        raise typer.Exit(2) from exc
    except SuiteError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(2) from exc

    destination = Path(output) if output else None
    with _open_database(config) as database:
        try:
            stream = iter_export(database, selection)
            if destination is None:
                for chunk in stream:
                    sys.stdout.write(chunk)
                sys.stdout.flush()
            else:
                with destination.open("w", encoding="utf-8", newline="") as handle:
                    for chunk in stream:
                        handle.write(chunk)
        except DatabaseError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2) from exc

    if destination is not None:
        typer.echo(f"Wrote {destination}.", err=True)
