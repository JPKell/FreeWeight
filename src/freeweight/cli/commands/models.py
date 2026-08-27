"""freeweight.cli.commands.models — list, show, refresh.

Every command here is **local** mode (CLI standards §6): it opens the configured database — and,
for ``show`` and ``refresh``, constructs the configured provider — in-process, and needs no server
running. They call exactly the functions ``freeweight.web.routes.models`` calls, so the two surfaces
never disagree about what discovery found.

Only ``typer`` and ``json`` are imported at module level, so registering this subgroup (which
``freeweight.cli.main`` does eagerly, to build ``--help``) never pulls in SQLAlchemy, ModelRack or
httpx (CLI Standards §12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from modelrack.provider import Provider

    from freeweight.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Model discovery and inspection.")


@contextmanager
def _open_database(config: str | None) -> Iterator[Database]:
    """Resolve configuration and open one database handle for this command, or exit 3."""
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


@contextmanager
def _open_backend(config: str | None) -> Iterator[tuple[Database, Provider]]:
    """Resolve configuration, open the database and build the provider, or exit 3.

    For the two commands that reach the provider (``show``'s fallback resolve, and ``refresh``).
    ``list`` uses :func:`_open_database` alone — it never touches the provider, matching
    :mod:`freeweight.services.models`'s own no-live-probe design for reading the model list.
    """
    from freeweight.config import ConfigurationError, load_settings
    from freeweight.infrastructure.providers.factory import build_provider
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
    try:
        provider = build_provider(loaded.settings.provider)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    with Database.from_url(
        storage.database_url, statement_timeout_ms=storage.statement_timeout_ms
    ) as database:
        yield database, provider


_ConfigOption = Annotated[str | None, typer.Option("--config", help="Path to a config.toml file.")]
_JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


@app.command("list")
def list_models(config: _ConfigOption = None, json_output: _JsonOption = False) -> None:
    """List every discovered model identity. Mode: local."""
    from baseaicore.timeutil import to_rfc3339

    from freeweight.infrastructure.db.errors import DatabaseError
    from freeweight.services.models import get_last_discovery, list_models_with_latest_descriptor

    with _open_database(config) as database:
        try:
            models = list_models_with_latest_descriptor(database)
            last_discovery = get_last_discovery(database)
        except DatabaseError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "models": [
                        {
                            "id": model.id,
                            "canonical_id": model.canonical_id,
                            "provider_kind": model.provider_kind,
                            "provider_model_name": model.provider_model_name,
                            "artifact_digest": model.artifact_digest,
                            "identity_confidence": model.identity_confidence,
                            "quantization": model.quantization,
                            "parameter_count": model.parameter_count,
                            "max_context": model.max_context,
                            "first_seen_at": to_rfc3339(model.first_seen_at),
                            "last_seen_at": to_rfc3339(model.last_seen_at),
                        }
                        for model in models
                    ],
                    "last_discovery": (
                        {
                            "ok": last_discovery.ok,
                            "checked_at": to_rfc3339(last_discovery.checked_at),
                            "detail": last_discovery.detail,
                        }
                        if last_discovery is not None
                        else None
                    ),
                }
            )
        )
        return

    if last_discovery is not None and not last_discovery.ok:
        typer.echo(
            f"Warning: the last refresh attempt ({to_rfc3339(last_discovery.checked_at)}) failed: "
            f"{last_discovery.detail}. The models below are what was last discovered successfully.",
            err=True,
        )
    if not models:
        typer.echo("No models yet. Run `freeweight models refresh`.")
        return
    for model in models:
        quantization = model.quantization or "—"
        parameters = model.parameter_count if model.parameter_count is not None else "—"
        context = model.max_context if model.max_context is not None else "—"
        typer.echo(
            f"{model.id}  {model.canonical_id}  ({model.identity_confidence})  "
            f"{quantization}  params={parameters}  context={context}"
        )


@app.command("show")
def show(
    reference: Annotated[
        str, typer.Argument(help="A stored model ID/prefix, canonical ID, or provider name.")
    ],
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Show one model's identity, aliases and descriptor history. Mode: local.

    Falls back to the provider when ``reference`` matches nothing stored yet, and records the
    resolution as an alias if it changes what was typed (canonical model identity §2.3).
    """
    from baseaicore import ValidationError
    from baseaicore.timeutil import to_rfc3339
    from modelrack.errors import ModelNotFound, ProviderError

    from freeweight.services.models import get_model_detail

    with _open_backend(config) as (database, provider):
        try:
            detail = get_model_detail(database, provider, reference, now=datetime.now(UTC))
        except (ModelNotFound, ValidationError) as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2) from exc
        except ProviderError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "id": detail.id,
                    "canonical_id": detail.canonical_id,
                    "provider_kind": detail.provider_kind,
                    "provider_model_name": detail.provider_model_name,
                    "artifact_digest": detail.artifact_digest,
                    "identity_confidence": detail.identity_confidence,
                    "first_seen_at": to_rfc3339(detail.first_seen_at),
                    "last_seen_at": to_rfc3339(detail.last_seen_at),
                    "aliases": list(detail.aliases),
                    "resolved_alias": detail.resolved_alias,
                    "descriptor_count": len(detail.descriptor_history),
                    "latest_descriptor": (
                        {
                            "family": detail.latest_descriptor.family,
                            "architecture": detail.latest_descriptor.architecture,
                            "parameter_count": detail.latest_descriptor.parameter_count,
                            "quantization": detail.latest_descriptor.quantization,
                            "max_context": detail.latest_descriptor.max_context,
                            "observed_at": to_rfc3339(detail.latest_descriptor.observed_at),
                        }
                        if detail.latest_descriptor is not None
                        else None
                    ),
                }
            )
        )
        return

    typer.echo(f"{detail.canonical_id}")
    if detail.resolved_alias:
        typer.echo(f"  resolved from: {detail.resolved_alias} (now recorded as an alias)")
    typer.echo(f"  identity confidence: {detail.identity_confidence}")
    typer.echo(f"  first seen:          {to_rfc3339(detail.first_seen_at)}")
    typer.echo(f"  last seen:           {to_rfc3339(detail.last_seen_at)}")
    if detail.latest_descriptor is None:
        typer.echo("  no descriptor recorded yet")
        return
    described = detail.latest_descriptor
    parameter_count = described.parameter_count if described.parameter_count is not None else "—"
    max_context = described.max_context if described.max_context is not None else "—"
    typer.echo(f"  family:              {described.family or '—'}")
    typer.echo(f"  quantization:        {described.quantization or '—'}")
    typer.echo(f"  parameters:          {parameter_count}")
    typer.echo(f"  max context:         {max_context}")
    typer.echo(f"  descriptor snapshots: {len(detail.descriptor_history)}")


@app.command("refresh")
def refresh(config: _ConfigOption = None, json_output: _JsonOption = False) -> None:
    """Discover every model the configured provider is serving. Mode: local."""
    from modelrack.errors import ProviderError

    from freeweight.services.models import discover_models

    with _open_backend(config) as (database, provider):
        try:
            outcome = discover_models(database, provider, now=datetime.now(UTC))
        except ProviderError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "added": outcome.added,
                    "updated": outcome.updated,
                    "unchanged": outcome.unchanged,
                    "total": outcome.total,
                }
            )
        )
        return
    typer.echo(
        f"discovered {outcome.total} model(s): {outcome.added} added, {outcome.updated} updated, "
        f"{outcome.unchanged} unchanged"
    )
