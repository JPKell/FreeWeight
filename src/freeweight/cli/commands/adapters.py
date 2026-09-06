"""freeweight.cli.commands.adapters — list and show the operator's LoRA adapters.

Both commands are **local** mode (CLI standards §6): they read
`[adapters] directory` and this installation's own `adapters`
table in-process, and need no server running.

The LoRA sense of "adapter", not the benchmark-harness sense `freeweight external` manages
(spec §7.5). Everything here reads; nothing writes — the directory is
the operator's, and rows are written only when a run measures under one.

Only `typer` and `json` are imported at module level, so registering this subgroup never pulls
in SQLAlchemy or ModelRack (CLI Standards §12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from freeweight.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Inspect the LoRA adapters this installation can measure under.")

_ConfigOption = Annotated[str | None, typer.Option("--config", help="Configuration file path.")]
_JsonOption = Annotated[bool, typer.Option("--json", help="Emit JSON instead of text.")]


@contextmanager
def _open(config: str | None) -> Iterator[tuple[Database, object]]:
    """Resolve configuration and open one database handle, or exit 3."""
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
        yield database, loaded.settings.adapters


@app.command("list")
def list_command(config: _ConfigOption = None, json_output: _JsonOption = False) -> None:
    """List every adapter the configured directory holds. Mode: local.

    An adapter that cannot be used is printed **with the reason**, never omitted: "the adapter I
    dropped in is not being used" is the confusion the whole directory design exists to prevent
    (ADR-0061). Manifests that could not be read at all, drafts nobody has reviewed, and artifacts
    with no manifest are listed for the same reason.

    Exits ``3`` when `[adapters] directory` is unset — adapters are off, deliberately — or
    names something that is not a directory.
    """
    from baseaicore import SuiteError

    from freeweight.services.adapters import adapter_overview

    with _open(config) as (database, adapters):
        try:
            overview = adapter_overview(database, adapters)  # type: ignore[arg-type]  # AdapterSettings
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(3) from exc

    if json_output:
        typer.echo(json.dumps(overview.as_json()))
        return

    typer.echo(f"Adapters in {overview.reading.directory}:")
    if not overview.reading.entries:
        typer.echo("  (no reviewed manifests)")
    for entry in overview.reading.entries:
        mark = "ok " if entry.available else "!! "
        base = entry.base_model_name
        if entry.base_confidence.value == "name_only":
            base += "  [NAME_ONLY — base named, not proven]"
        measured = " measured" if entry.artifact_sha256 in overview.measured else ""
        typer.echo(f"  {mark}{entry.name}  base={base}{measured}")
        typer.echo(f"      {entry.artifact_sha256}")
        if entry.declared_capabilities:
            typer.echo(f"      declares: {', '.join(entry.declared_capabilities)}")
        if not entry.available:
            typer.echo(f"      unavailable: {entry.unavailable_reason}", err=True)
    for path, problem in overview.reading.invalid:
        typer.echo(f"  ?? {path.name}: {problem}", err=True)
    for path in overview.reading.drafts:
        typer.echo(f"  -- {path.name}: a draft, never registered until a person keeps it")
    for path in overview.reading.unmanifested:
        typer.echo(f"  -- {path.name}: no manifest, so not registered")


@app.command("show")
def show(
    name: Annotated[str, typer.Argument(help="The adapter's name.")],
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Show one adapter in full: its identity, its base claim and what it declares. Mode: local.

    Exits ``2`` when no adapter of that name is in the directory, listing the names that are.
    """
    from baseaicore import SuiteError

    from freeweight.services.adapters import adapter_overview

    with _open(config) as (database, adapters):
        try:
            overview = adapter_overview(database, adapters)  # type: ignore[arg-type]  # AdapterSettings
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(3) from exc

    entry = overview.reading.by_name(name)
    if entry is None:
        known = sorted(item.name for item in overview.reading.entries)
        typer.echo(
            f"Error: no adapter named {name!r} in {overview.reading.directory}. "
            f"Registered: {known or ['(none)']} (VALIDATION_ERROR)",
            err=True,
        )
        raise typer.Exit(2)

    payload = {**entry.as_json(), "measured": entry.artifact_sha256 in overview.measured}
    if json_output:
        typer.echo(json.dumps(payload))
        return
    for key, value in payload.items():
        rendered = ", ".join(str(item) for item in value) if isinstance(value, list) else value
        typer.echo(f"{key}: {rendered}")
    if entry.base_confidence.value == "name_only":
        typer.echo(
            "note: this adapter's manifest names its base without proving a digest, so every "
            "subject it produces is flagged NAME_ONLY and its evidence carries reduced confidence."
        )
