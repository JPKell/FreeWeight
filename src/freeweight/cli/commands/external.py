"""freeweight.cli.commands.external — ``freeweight external list|install|verify`` (Phase 13).

The CLI front end of :mod:`freeweight.services.external`. ``list`` shows every external benchmark
adapter with its upstream credit, licence and install state; ``install`` creates a benchmark's
isolated environment; ``verify`` re-checks an installed benchmark's datasets against their pins.

Every heavy import is inside a command body (CLI standards §12).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from freeweight.config import Settings

__all__ = ["app"]

app = typer.Typer(help="Manage external benchmark adapters (Phase 13).")

_ConfigOption = Annotated[str | None, typer.Option("--config", help="Path to a config.toml file.")]
_JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


def _load(config: str | None) -> Settings:
    from freeweight.config import load_settings

    return load_settings(config_path=config).settings


@app.command("list")
def list_command(config: _ConfigOption = None, as_json: _JsonOption = False) -> None:
    """List every external benchmark adapter, with its credit and install state."""
    from freeweight.services.external import list_benchmarks

    infos = list_benchmarks(_load(config))
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "key": info.key,
                        "name": info.name,
                        "category": info.category,
                        "capabilities": list(info.capabilities),
                        "source_repository": info.source_repository,
                        "release_tag": info.release_tag,
                        "commit": info.commit,
                        "license": info.license,
                        "requires_sandbox": info.requires_sandbox,
                        "installed": info.installed,
                        "datasets": list(info.dataset_names),
                    }
                    for info in infos
                ],
                indent=2,
            )
        )
        return
    for info in infos:
        state = "installed" if info.installed else "not installed"
        sandbox = " [sandbox]" if info.requires_sandbox else ""
        typer.echo(f"{info.key}  ({info.name}){sandbox}")
        typer.echo(f"    {info.category} → {', '.join(info.capabilities) or '—'}   {state}")
        typer.echo(f"    {info.source_repository} @ {info.release_tag} ({info.license})")


@app.command("install")
def install_command(
    key: Annotated[str, typer.Argument(help="The benchmark key, e.g. external.ifeval.")],
    config: _ConfigOption = None,
    as_json: _JsonOption = False,
) -> None:
    """Create one benchmark's isolated environment and record its install state."""
    from baseaicore import SuiteError

    from freeweight.services.external import install_benchmark

    try:
        state = install_benchmark(_load(config), key)
    except SuiteError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(json.dumps(state.to_json(), indent=2))
        return
    typer.echo(f"Installed {state.key} ({state.release_tag}, commit {state.commit}).")
    if state.datasets:
        typer.echo(f"    datasets: {', '.join(state.datasets)}")


@app.command("verify")
def verify_command(
    key: Annotated[str, typer.Argument(help="The benchmark key, e.g. external.ifeval.")],
    config: _ConfigOption = None,
) -> None:
    """Verify an installed benchmark's datasets against their pinned hashes."""
    from baseaicore import SuiteError

    from freeweight.services.external import verify_benchmark

    try:
        verify_benchmark(_load(config), key)
    except SuiteError as exc:
        typer.echo(f"{exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{key}: installed and every dataset matches its pin.")
