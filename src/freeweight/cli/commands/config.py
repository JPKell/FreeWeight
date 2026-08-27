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
    """Print the effective configuration, with the source of every value."""
    from freeweight.config import ConfigurationError, load_settings

    try:
        loaded = load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc

    dumped = loaded.settings.model_dump(mode="json")
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "values": dumped,
                    "sources": loaded.sources,
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
            source = loaded.sources.get(path, "default")
            rendered = "********" if _looks_secret(field_name) else value
            typer.echo(f"{path:<32} {rendered!s:<24} ({source})")


@app.command("validate")
def validate(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Validate configuration without starting the service. Exit 0 or 3."""
    from freeweight.config import ConfigurationError, load_settings

    try:
        load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    typer.echo("Configuration is valid.")


@app.command("path")
def path(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Print the resolved configuration file location."""
    from freeweight.config import resolve_config_path

    typer.echo(str(resolve_config_path(config)))


@app.command("init")
def init(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to write the config file to.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a fully commented example configuration file."""
    from freeweight.config import EXAMPLE_CONFIG_TOML, resolve_config_path

    target = resolve_config_path(config)
    if target.exists() and not force:
        typer.echo(f"Error: {target} already exists (use --force to overwrite).", err=True)
        raise typer.Exit(3)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG_TOML, encoding="utf-8")
    typer.echo(str(target))
