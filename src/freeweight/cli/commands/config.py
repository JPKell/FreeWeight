"""freeweight.cli.commands.config — show, validate, init, path.

Only ``typer`` and ``json`` load at module level; ``freeweight.config`` (which imports pydantic)
is imported lazily inside each command body, per the same startup-performance discipline as
:mod:`freeweight.cli.commands.system`.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

__all__ = ["app"]

app = typer.Typer(help="Configuration inspection and management.")


def _looks_secret(field_name: str) -> bool:
    lowered = field_name.lower()
    return any(marker in lowered for marker in ("token", "key", "secret", "password"))


@app.command("show")
def show(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """Print the effective configuration, with the source of every value.

    A runtime-changeable key whose stored row is in force is marked ``(database)`` and shows the
    stored value (configuration standards §7); a stored row an environment variable beats is
    marked as shadowed beside the variable that wins. With no readable database — absent,
    unmigrated or on another host — the output is exactly what it was before there was a settings
    table, and no database file is created.

    Example:
        freeweight config show --json
    """
    from freeweight.config import ConfigurationError, load_settings
    from freeweight.services.settings import database_overlay

    try:
        loaded = load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc

    dumped = loaded.settings.model_dump(mode="json")
    sources = dict(loaded.sources)
    for path, (value, source) in database_overlay(loaded.settings).items():
        sources[path] = source
        section, _, field_name = path.partition(".")
        if section in dumped and field_name in dumped[section]:
            dumped[section][field_name] = value
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "values": dumped,
                    "sources": sources,
                    "config_path": str(loaded.config_path),
                }
            )
        )
        return

    typer.echo(
        f"# {loaded.config_path}{'' if loaded.config_file_used else ' (not found; defaults apply)'}"
    )
    for section, fields in dumped.items():
        for field_name, value in fields.items():
            path = f"{section}.{field_name}"
            source = sources.get(path, "default")
            rendered = "********" if _looks_secret(field_name) else value
            typer.echo(f"{path:<32} {rendered!s:<24} ({source})")


@app.command("validate")
def validate(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    file: Annotated[
        str | None,
        typer.Option(
            "--file", help="Validate this candidate file instead of the installation's own."
        ),
    ] = None,
) -> None:
    """Validate configuration without starting the service. Exit 0 or 3.

    ``--file`` runs an arbitrary candidate through the same parse, the same validation and the
    same security refusals as startup (ADR-0127 rule 2) — the check WeightRoomGym runs before it
    writes a settings-form edit back to disk; the installation's own ``config.toml`` is never read
    or written for it. Without ``--file`` the verb keeps its present meaning: validate the resolved
    installation config (or ``--config``, if given).

    Example:
        freeweight config validate --file /tmp/candidate.toml
    """
    from freeweight.config import ConfigurationError, load_settings

    try:
        load_settings(config_path=file if file is not None else config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    typer.echo("Configuration is valid.")


@app.command("schema")
def schema(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the schema document as canonical JSON.")
    ] = False,
) -> None:
    """Print the settings-schema document (ADR-0127 rule 1).

    The JSON Schema of ``Settings``, the runtime-changeable registry, the security-relevant keys,
    every other key, and the source of each. Built for WeightRoomGym's settings form, which
    hardcodes none of FreeWeight's configuration surface and instead reads this document. Never
    prints a secret: the document carries key paths and layers, never a value (configuration
    standards §6).

    Example:
        freeweight config schema --json
    """
    from baseaicore import canonical_json

    from freeweight.config import ConfigurationError
    from freeweight.services.settings import config_schema_document

    try:
        document = config_schema_document(config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc

    if json_output:
        typer.echo(canonical_json(document))
        return

    typer.echo(
        f"schema_version {document['schema_version']}  application {document['application']}"
        f"  version {document['version']}"
    )
    typer.echo(f"config_path {document['config_path']}")
    typer.echo(f"runtime_changeable ({len(document['runtime_changeable'])}):")
    for entry in document["runtime_changeable"]:
        typer.echo(f"  {entry['key']} ({entry['kind']})")
    typer.echo(f"security_keys ({len(document['security_keys'])}):")
    for key in document["security_keys"]:
        typer.echo(f"  {key}")
    if document["problems"]:
        typer.echo("problems:")
        for problem in document["problems"]:
            typer.echo(f"  {problem}")


@app.command("path")
def path(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Print the resolved configuration file location.

    Example:
        freeweight config path
    """
    from freeweight.config import resolve_config_path

    typer.echo(str(resolve_config_path(config)))


@app.command("init")
def init(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to write the config file to.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a fully commented example configuration file.

    Example:
        freeweight config init --force
    """
    from freeweight.config import EXAMPLE_CONFIG_TOML, resolve_config_path

    target = resolve_config_path(config)
    if target.exists() and not force:
        typer.echo(f"Error: {target} already exists (use --force to overwrite).", err=True)
        raise typer.Exit(3)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG_TOML, encoding="utf-8")
    typer.echo(str(target))
