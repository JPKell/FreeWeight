"""freeweight.cli.commands.config — show, validate, init, path.

Only ``typer`` and ``json`` load at module level; ``freeweight.config`` (which imports pydantic)
is imported lazily inside each command body, per the same startup-performance discipline as
:mod:`freeweight.cli.commands.system`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:  # imported for typing only: `freeweight.config` loads pydantic, and the
    from freeweight.config import Settings  # CLI keeps that out of module import time

__all__ = ["app"]

app = typer.Typer(help="Configuration inspection and management.")


def _looks_secret(field_name: str) -> bool:
    lowered = field_name.lower()
    return any(marker in lowered for marker in ("token", "key", "secret", "password"))


def _database_overlay(settings: Settings) -> dict[str, tuple[object, str]]:
    """The runtime-changeable values the ``settings`` table decides, and how to label them.

    Configuration standards §7 asks ``config show`` to mark database-sourced values
    ``(database)``. This opens the configured database read-only to find them, and **never
    raises**: an absent, unmigrated or unreadable database is not a failure of ``config show`` —
    printing the configured values is exactly the right answer when there is no database to
    consult, and a command that needed one would be unusable on a fresh install.

    Args:
        settings: The loaded :class:`~freeweight.config.Settings` — the file/environment layers as
            resolved, before :func:`~freeweight.services.settings.apply_stored` has touched them,
            which is why the stored value rather than the effective one is what a
            database-sourced row prints here.

    Returns:
        ``path -> (value, source)`` for the keys the database decides, plus the keys whose stored
        row is beaten by an environment variable — those keep their configured value and say that
        a row exists and does nothing. Empty when no database can be read.
    """
    from baseaicore import SuiteError
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    from freeweight.services.database import Database
    from freeweight.services.settings import read_settings

    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        return {}
    url = make_url(database_url)
    if url.drivername.startswith("sqlite") and url.database not in (None, ":memory:"):
        # Connecting would create the file. An inspection command must not leave a database
        # behind that `db status` would then report as unmigrated.
        if not Path(str(url.database)).is_file():
            return {}
    try:
        with Database.from_url(database_url) as database:
            views = read_settings(database, settings)
    except (SQLAlchemyError, SuiteError, OSError):
        return {}
    overlay: dict[str, tuple[object, str]] = {}
    for view in views:
        if view.source == "database":
            overlay[view.setting.key] = (view.stored_value, "database")
        elif view.overridden_by_env:
            overlay[view.setting.key] = (
                view.effective_value,
                f"env {view.setting.env_var}; database row {view.stored_value} shadowed",
            )
    return overlay


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

    try:
        loaded = load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc

    dumped = loaded.settings.model_dump(mode="json")
    sources = dict(loaded.sources)
    for path, (value, source) in _database_overlay(loaded.settings).items():
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
) -> None:
    """Validate configuration without starting the service. Exit 0 or 3.

    Example:
        freeweight config validate --config ./config.toml
    """
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
