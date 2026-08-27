"""freeweight.bootstrap — the composition root: settings, logging and the ASGI app, wired once.

This module sits outside the ``web``/``cli``/``services``/``domain`` layer ordering that
``.importlinter`` enforces, precisely so it can import both configuration and the web layer.
``freeweight.cli`` never imports it directly — the ``web-cli-independence`` contract forbids any
import chain from ``cli`` into ``web``, and this module imports ``web``. Instead, the CLI's
``serve`` command hands uvicorn the dotted string
``"freeweight.bootstrap:create_app_from_environment"`` and lets uvicorn perform that import
itself; a string literal is invisible to import-linter's static analysis, so the two surfaces stay
decoupled at the source level while still running the same application in one process.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI

from freeweight.config import LoadedSettings, load_settings
from freeweight.observability.logging import configure_logging
from freeweight.services.database import Database, ensure_ready
from freeweight.services.machine import profile_machine
from freeweight.services.runs import shipped_prompt_library
from freeweight.services.telemetry import build_collector
from freeweight.web.app import create_app

__all__ = ["Application", "bootstrap", "create_app_from_environment"]


@dataclass(frozen=True, slots=True)
class Application:
    """A fully wired application: the settings it was built from and its ASGI app."""

    loaded_settings: LoadedSettings
    app: FastAPI


def bootstrap() -> Application:
    """Load configuration, configure logging, ready the database, profile this host, build the app.

    Reads configuration through the standard precedence chain (defaults, file, environment) with
    no CLI-argument layer of its own: a caller that needs CLI overrides applies them as
    environment variables before calling this function, which is what
    ``freeweight.cli.commands.system.serve`` does.

    The startup revision check (database standards §5.1) and the one-time machine profile (Phase 4)
    both run here, in the composition root, and deliberately not inside
    :func:`~freeweight.web.app.create_app` — that function is documented as a pure function of
    :class:`~freeweight.config.Settings` precisely so tests can build an app without touching the
    filesystem, and opening a database or reading the host's hardware is neither pure nor free.

    Returns:
        The wired :class:`Application`.

    The prompt pack is loaded and validated here too, before anything can run: prompt standards
    §5 makes a malformed prompt a *startup* failure, because a pack that fails halfway through a
    run has already produced measurements whose provenance nobody can reconstruct.

    Raises:
        ConfigurationError: Configuration is invalid, or an unsafe bind/auth combination is
            configured.
        PromptPackInvalid: A prompt record is malformed, or the pack manifest does not describe
            the records on disk.
        MigrationRequired: The database is behind head and ``storage.auto_migrate`` is false.
        SchemaAhead: The database was written by a newer application version.
        DatabaseUnavailable: The configured database could not be reached at all.
    """
    loaded = load_settings()
    configure_logging(
        level=loaded.settings.logging.level, log_format=loaded.settings.logging.format
    )
    # Before the database, deliberately: a build whose prompts do not validate cannot produce a
    # correct measurement, and finding that out after migrating a database is worse than finding
    # it out first. Cached, so nothing loads the pack a second time.
    shipped_prompt_library()
    database_url = loaded.settings.storage.database_url
    if database_url is not None:
        # Always true by the time Settings has validated (StorageSettings fills it in), but the
        # type is Optional and a startup path is not the place to trust that silently.
        #
        # This handle is for the migration check and the one-time machine profile alone, and is
        # closed again immediately: the long-lived one belongs to the running application and is
        # created by the web lifespan, which has not started yet. Migration also disposes pools of
        # its own (a failed migration restores a backup, which cannot happen underneath live
        # handles), so it is the wrong thing to run through the handle the server will serve from.
        with Database.from_url(
            database_url, statement_timeout_ms=loaded.settings.storage.statement_timeout_ms
        ) as database:
            ensure_ready(
                database,
                auto_migrate=loaded.settings.storage.auto_migrate,
                backup_retention=loaded.settings.storage.backup_retention,
            )
            # Profile → fingerprint → upsert → last_seen_at, once per server startup. A second,
            # short-lived collector: the long-lived one the telemetry bar samples from belongs to
            # the web lifespan, for the same reason the database handle above does.
            profile_machine(database, build_collector())
    return Application(loaded_settings=loaded, app=create_app(loaded.settings))


def create_app_from_environment() -> FastAPI:
    """Zero-argument ASGI factory: the target uvicorn imports by dotted name.

    See the module docstring for why this is referenced by string rather than imported directly
    by ``freeweight.cli``.
    """
    return bootstrap().app
