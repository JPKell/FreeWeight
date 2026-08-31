"""freeweight.cli.commands.evidence — ``freeweight evidence show|export``.

The CLI front end of :mod:`freeweight.services.evidence`. ``show`` prints the same records
``GET /api/v1/evidence`` returns and, with ``--json``, the identical collection envelope;
``export`` writes the same ``benchmark.evidence_bundle`` ``GET /api/v1/evidence/export`` does,
so a file produced either way is one contract. Both are **local**: they open the configured
database and need no server.

Every heavy import is inside a command body (CLI standards §12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from freeweight.config import Settings
    from freeweight.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Inspect and export capability evidence.")

_ConfigOption = Annotated[str | None, typer.Option("--config", help="Path to a config.toml file.")]
_CapabilityOption = Annotated[
    str | None, typer.Option("--capability", help="Exact capability ID, e.g. tool_use.")
]
_ModelOption = Annotated[
    str | None, typer.Option("--model", help="Model canonical ID, ULID or unambiguous prefix.")
]
_MachineOption = Annotated[str | None, typer.Option("--machine", help="Machine fingerprint.")]
_ProfileOption = Annotated[
    str | None, typer.Option("--runtime-profile", help="Runtime profile hash.")
]
_MinConfidenceOption = Annotated[
    float | None,
    typer.Option("--min-confidence", min=0.0, max=1.0, help="Only records at or above this."),
]
_JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]

_ID_PREFIX_CHARS = 8


@contextmanager
def _open(config: str | None) -> Iterator[tuple[Settings, Database]]:
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
        yield loaded.settings, database


def _exit_for(exc: Exception) -> typer.Exit:
    """Map a service failure onto the documented exit codes (CLI standards §4)."""
    from baseaicore import SuiteError
    from weightsdb import DatabaseError

    if isinstance(exc, DatabaseError):
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        return typer.Exit(4)
    if isinstance(exc, SuiteError):
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        return typer.Exit(2)
    typer.echo(f"Error: {exc}", err=True)
    return typer.Exit(1)


def _print_report(report: Any) -> None:  # noqa: ANN401 — an AggregationReport
    """Print what a recomputation emitted and withheld, on stderr, because it is not data."""
    typer.echo(
        f"Recomputed evidence for {report.subjects} subject(s) under policy "
        f"{report.policy_version}: {len(report.emitted)} record(s) emitted, "
        f"{len(report.withheld)} withheld.",
        err=True,
    )
    for item in report.withheld:
        typer.echo(f"  withheld {item.capability_id} for {item.model}: {item.reason}", err=True)
    for note in report.separated:
        typer.echo(f"  separated: {note}", err=True)
    for note in report.notes:
        typer.echo(f"  note: {note}", err=True)


@app.command("show")
def show(  # noqa: PLR0913 — the documented filter set, one option each
    capability: _CapabilityOption = None,
    model: _ModelOption = None,
    machine: _MachineOption = None,
    runtime_profile: _ProfileOption = None,
    min_confidence: _MinConfidenceOption = None,
    recompute: Annotated[
        bool,
        typer.Option(
            "--recompute",
            help="Rewrite the evidence from the stored runs first, and report what was withheld.",
        ),
    ] = False,
    json_output: _JsonOption = False,
    config: _ConfigOption = None,
) -> None:
    """Show capability evidence, with its confidence explained. Mode: local.

    Prints the records LoadCoach consumes: capability, model, score, confidence, samples, when
    they were measured, and whether they are stale. ``--json`` prints the same collection envelope
    ``GET /api/v1/evidence`` returns. ``--recompute`` rewrites the evidence from the stored runs
    first — after changing the capability weights or the confidence policy — and lists on stderr
    every capability that was withheld and why, because a goal below its gate emits nothing and
    silence would be indistinguishable from an error.

    Example:
        freeweight evidence show --capability tool_use

    Example:
        freeweight evidence show --recompute --json | jq '.items[].payload.confidence'

    Exit codes: ``0`` printed; ``2`` a filter is invalid or matches no model; ``3`` a
    configuration error; ``4`` the database is unavailable.
    """
    from baseaicore import utc_now

    from freeweight.services.evidence import (
        MAX_EVIDENCE_LIMIT,
        EvidenceQuery,
        policy_for,
        query_evidence,
        recompute_evidence,
        staleness_of,
    )

    with _open(config) as (settings, database):
        try:
            if recompute:
                _print_report(recompute_evidence(database, settings=settings.evidence))
            page = query_evidence(
                database,
                EvidenceQuery(
                    capability=capability,
                    model=model,
                    machine=machine,
                    runtime_profile=runtime_profile,
                    min_confidence=min_confidence,
                    limit=MAX_EVIDENCE_LIMIT,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — mapped onto the documented exit codes
            raise _exit_for(exc) from exc
        policy = policy_for(settings.evidence)

    if json_output:
        typer.echo(json.dumps(page.as_json()))
        return
    if not page.records:
        typer.echo("No capability evidence yet. Complete a run to produce some.")
        return
    now = utc_now()
    typer.echo(
        f"{'capability':<28} {'model':<36} {'score':>6} {'conf':>5} {'n':>5} {'age':>6}  status"
    )
    for record in page.records:
        staleness = staleness_of(record, now=now, policy=policy)
        status = "stale" if staleness.stale else "fresh"
        if record.identity_confidence == "name_only":
            status += ", name only"
        if record.is_goal_sourced:
            status += ", goal"
        typer.echo(
            f"{record.capability_id:<28} {record.model_canonical_id[:36]:<36} "
            f"{record.score:>6.3f} {record.confidence:>5.2f} {record.sample_count:>5} "
            f"{staleness.age_days:>5.0f}d  {status}"
        )
        typer.echo(
            "    factors: "
            + " ".join(
                f"{name.split('_factor')[0]}={float(record.factors.get(name, 0.0)):.2f}"
                for name in (
                    "sample_factor",
                    "consistency_factor",
                    "freshness_factor",
                    "environment_factor",
                    "identity_factor",
                )
            )
            + f" judge_validity={record.judge_validity_factor:.2f}"
        )
        typer.echo(
            "    from: "
            + ", ".join(
                f"{metric.metric_key} (w={metric.weight:g}, n={metric.sample_count})"
                for metric in record.contributing_metrics
            )
        )
        if record.calibration is not None:
            typer.echo(
                f"    judge agreement: kappa_w={float(record.calibration['kappa_w']):.2f} over "
                f"{record.calibration['n_holdout']} held-out samples graded by "
                f"{record.calibration['graded_by']}"
            )


@app.command("export")
def export(  # noqa: PLR0913 — the documented parameter set, one option each
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help=(
                "RFC 3339; only evidence computed after this instant, on this machine's clock. "
                "Pass the generated_at of the bundle you received last time."
            ),
        ),
    ] = None,
    capability: _CapabilityOption = None,
    model: _ModelOption = None,
    machine: _MachineOption = None,
    runtime_profile: _ProfileOption = None,
    min_confidence: _MinConfidenceOption = None,
    output: Annotated[
        str | None, typer.Option("--output", "-o", help="Write here instead of stdout.")
    ] = None,
    config: _ConfigOption = None,
) -> None:
    """Export the evidence bundle LoadCoach imports. Mode: local.

    Writes one ``benchmark.evidence_bundle`` SetSpec envelope — the same bytes
    ``GET /api/v1/evidence/export`` returns — to stdout, or to ``--output``. A bundle with no
    ``--since`` and no filter is ``complete``; anything narrower is incremental and says so, so a
    consumer never infers a removal from a bundle that could not show one (ADR-0022 §5).

    Example:
        freeweight evidence export --output evidence.json

    Example:
        freeweight evidence export --since 2026-08-01T00:00:00Z --capability tool_use

    Exit codes: ``0`` written; ``2`` a filter is invalid or matches no model; ``3`` a
    configuration error; ``4`` the database is unavailable.
    """
    import sys
    from pathlib import Path

    from baseaicore import from_rfc3339

    from freeweight.services.evidence import EvidenceQuery, iter_evidence_export

    instant = None
    if since is not None:
        try:
            instant = from_rfc3339(since)
        except Exception as exc:  # noqa: BLE001 — every parse failure is one usage error
            typer.echo(
                f"Error: --since must be an RFC 3339 instant, e.g. 2026-08-01T00:00:00Z. ({exc})",
                err=True,
            )
            raise typer.Exit(2) from exc
    query = EvidenceQuery(
        capability=capability,
        model=model,
        machine=machine,
        runtime_profile=runtime_profile,
        min_confidence=min_confidence,
        since=instant,
    )
    destination = Path(output) if output else None
    with _open(config) as (_settings, database):
        try:
            stream = iter_evidence_export(database, query)
            if destination is None:
                for chunk in stream:
                    sys.stdout.write(chunk)
                sys.stdout.flush()
            else:
                with destination.open("w", encoding="utf-8", newline="") as handle:
                    for chunk in stream:
                        handle.write(chunk)
        except Exception as exc:  # noqa: BLE001 — mapped onto the documented exit codes
            raise _exit_for(exc) from exc
    if destination is not None:
        typer.echo(f"Wrote {destination}.", err=True)
