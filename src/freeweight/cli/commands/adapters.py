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
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from freeweight.infrastructure.adapters import AdapterEntry
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
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Compose this adapter's panel against a stored base and show what has been "
                "measured on the resulting subject."
            ),
        ),
    ] = None,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Show one adapter in full: its identity, its base claim and what it declares. Mode: local.

    With ``--model``, also composes the subject's panel (ADR-0059) and prints what has actually
    been measured **on that subject**. An unmeasured capability prints ``—``, never a number and
    never the base's: an adapter subject inherits nothing from its base, at any weight
    (ADR-0016, ADR-0059).

    Exits ``2`` when no adapter of that name is in the directory, listing the names that are, or
    when ``--model`` names no stored model.
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

    payload: dict[str, object] = {
        **entry.as_json(),
        "measured": entry.artifact_sha256 in overview.measured,
    }
    panel = None
    if model is not None:
        with _open(config) as (database, _adapters):
            panel = _panel_for(database, model, entry)
        payload["subject"] = panel.as_json()

    if json_output:
        typer.echo(json.dumps(payload))
        return
    for key, value in payload.items():
        if key == "subject":
            continue
        rendered = ", ".join(str(item) for item in value) if isinstance(value, list) else value
        typer.echo(f"{key}: {rendered}")
    if entry.base_confidence.value == "name_only":
        typer.echo(
            "note: this adapter's manifest names its base without proving a digest, so every "
            "subject it produces is flagged NAME_ONLY and its evidence carries reduced confidence."
        )
    if panel is not None:
        typer.echo(f"\nsubject: {panel.subject.canonical_id}")
        typer.echo(f"panel (v{panel.panel.panel_version}):")
        for part in panel.panel.parts:
            typer.echo(f"  {part.name}: {', '.join(part.suites) or '(none)'}")
            typer.echo(f"      {part.reason}")
        typer.echo("measured on this subject:")
        if not panel.measured:
            typer.echo("  — nothing yet. An adapter subject inherits no evidence from its base.")
        for capability, score in sorted(panel.measured.items()):
            typer.echo(f"  {capability}: {score:.3f}")


def _panel_for(database: Database, model_ref: str, entry: AdapterEntry) -> Any:
    """Compose one subject's panel, or exit 2 when the model is not stored."""
    import typer as _typer
    from baseaicore import SuiteError

    from freeweight.infrastructure.db.repositories.models import ModelRepository
    from freeweight.services.adapters import panel_for, resolve_subject
    from freeweight.services.evidence import load_capability_mapping
    from freeweight.services.runs import build_registry

    with database.read() as session:
        row = ModelRepository().get_by_canonical_id(
            session, model_ref
        ) or ModelRepository().get_by_provider_model_name(session, model_ref)
        if row is None:
            typer.echo(
                f"Error: no stored model matches {model_ref!r}; run `freeweight models refresh` "
                "first (MODEL_NOT_FOUND)",
                err=True,
            )
            raise _typer.Exit(2)
        detached = _Detached(row)
    try:
        subject = resolve_subject(detached, (entry,), entry.name)
    except SuiteError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise _typer.Exit(2) from exc
    registry = build_registry()
    return panel_for(
        database,
        subject,
        mapping=load_capability_mapping(),
        available_suites=[benchmark.manifest.key for benchmark in registry.all()],
    )


class _Detached:
    """The three identity fields a subject needs, copied out of a session-bound row.

    SQLAlchemy models never leave the repository layer (coding standards §4), and this command
    reads one outside its session; copying the three fields is cheaper than a repository method
    nothing else would call.
    """

    __slots__ = ("artifact_digest", "provider_kind", "provider_model_name")

    def __init__(self, row: Any) -> None:
        """Copy the identity triple off ``row``."""
        self.provider_kind = row.provider_kind
        self.provider_model_name = row.provider_model_name
        self.artifact_digest = row.artifact_digest
