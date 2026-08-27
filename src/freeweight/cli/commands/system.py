"""freeweight.cli.commands.system — serve, health, version, doctor.

Only ``typer`` and ``json`` are imported at module level, so registering these commands (which
``freeweight.cli.main`` does eagerly, to build ``--help``) never pulls in FastAPI, SQLAlchemy,
httpx or Jinja2 (CLI Standards §12). Every heavier dependency is imported inside a function body,
where it is only reached once that command actually runs.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

__all__ = ["doctor", "health", "print_version", "serve", "version"]


def serve(
    host: Annotated[
        str | None, typer.Option(help="Bind host. Overrides configuration for this run.")
    ] = None,
    port: Annotated[
        int | None, typer.Option(help="Bind port. Overrides configuration for this run.")
    ] = None,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Start the web server. Mode: local.

    This is also what runs when ``freeweight`` (or ``python -m freeweight``) is invoked with no
    subcommand at all.
    """
    import os

    import uvicorn

    from freeweight.config import ConfigurationError, load_settings

    if config is not None:
        os.environ["FREEWEIGHT_CONFIG"] = config
    if host is not None:
        os.environ["FREEWEIGHT_SERVER__HOST"] = host
    if port is not None:
        os.environ["FREEWEIGHT_SERVER__PORT"] = str(port)

    try:
        loaded = load_settings()
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc

    uvicorn.run(
        "freeweight.bootstrap:create_app_from_environment",
        factory=True,
        host=loaded.settings.server.host,
        port=loaded.settings.server.port,
        log_config=None,
    )


def health(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """Report component health. Mode: local. Exit 0 (ok/degraded) or 4 (unavailable)."""
    from freeweight.services.health import get_health_report

    report = get_health_report()
    if json_output:
        typer.echo(json.dumps(report.model_dump(mode="json")))
    else:
        typer.echo(f"status: {report.status}")
        for component in report.components:
            typer.echo(f"  {component.name}: {component.status} — {component.detail}")
    if report.status == "unavailable":
        raise typer.Exit(4)


def _version_payload() -> dict[str, object]:
    from freeweight.__about__ import __version__

    return {
        "application": {"name": "freeweight", "version": __version__, "git_commit": None},
        "api": {"current": "v1", "supported": ["v1"], "deprecated": []},
        "schemas": {},
    }


def print_version(*, json_output: bool) -> None:
    """Print the version, as text or as the same JSON the API returns."""
    if json_output:
        typer.echo(json.dumps(_version_payload()))
    else:
        from freeweight.__about__ import __version__

        typer.echo(f"freeweight {__version__} (api v1)")


def version(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """Print the application, API and schema versions."""
    print_version(json_output=json_output)


def doctor() -> None:
    """Diagnose a broken installation. Mode: local."""
    from freeweight.services.health import get_health_report

    report = get_health_report()
    typer.echo(f"freeweight doctor — status: {report.status}")
    if not report.components:
        typer.echo(
            "  ✓ no dependent components yet (telemetry and sandbox checks arrive in later phases)"
        )
    for component in report.components:
        symbol = "✓" if component.status == "ok" else "!" if component.status == "degraded" else "✗"
        typer.echo(f"  {symbol} {component.name}: {component.detail}")
