"""freeweight.cli._backend — how a CLI command resolves configuration and opens its database.

Every local-mode command (CLI standards §6) does the same three things before its own work:
resolve configuration, open one database handle for the life of the command, and — for the
commands that reach a model — build the provider. Each is here once, and each exits ``3`` on a
configuration error (CLI standards §4) with the same one-line message.

Configuration is read exactly once per command. Reading it twice is how the URL used to open the
database and the retention used to rotate its backups end up disagreeing after an operator edits
the file mid-run. The service layer is imported inside the functions, never at module level, so
that ``--help`` stays cheap (CLI standards §12).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from modelrack.provider import Provider

    from freeweight.config import Settings
    from freeweight.services.database import Database

__all__ = ["build_provider_or_exit", "load_settings_or_exit", "open_database"]


def load_settings_or_exit(config: str | None) -> Settings:
    """Resolve configuration, or exit ``3`` with the refusal on stderr.

    Args:
        config: The ``--config`` path, or ``None`` for the default search.
    """
    from freeweight.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config).settings
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


@contextmanager
def open_database(config: str | None) -> Iterator[tuple[Settings, Database]]:
    """Resolve configuration and open one database handle for this command, or exit ``3``.

    One handle per command, closed on the way out — the CLI is one-shot, so it neither needs nor
    wants the server's application-lifetime engine. Opening it here rather than inside each
    service function is what lets the same service code serve both: the caller decides how long
    the connection lives, and a one-shot command decides "this command".

    Yields:
        The resolved settings and the open database.
    """
    from freeweight.services.database import Database

    settings = load_settings_or_exit(config)
    storage = settings.storage
    if storage.database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(
        storage.database_url, statement_timeout_ms=storage.statement_timeout_ms
    ) as database:
        yield settings, database


def build_provider_or_exit(settings: Settings) -> Provider:
    """Build the configured provider, or exit ``3`` when its configuration is refused.

    Args:
        settings: The resolved configuration; ``provider`` and ``adapters`` decide the build.
    """
    from freeweight.config import ConfigurationError
    from freeweight.infrastructure.providers.factory import build_provider

    try:
        return build_provider(settings.provider, adapters=settings.adapters)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
