"""freeweight.cli.main — the Typer root app.

Registers the top-level ``serve``/``health``/``version``/``doctor`` commands and the ``config`` and
``db`` subgroups. Only ``typer`` and the lightweight command modules load at import time; every
heavier dependency stays behind a lazy import inside the command bodies (CLI Standards §12), so
building ``--help`` never imports FastAPI, SQLAlchemy, httpx or Jinja2.
"""

from __future__ import annotations

from typing import Annotated

import typer

from freeweight.cli.commands import config as config_commands
from freeweight.cli.commands import db as db_commands
from freeweight.cli.commands import goals as goals_commands
from freeweight.cli.commands import judges as judges_commands
from freeweight.cli.commands import models as models_commands
from freeweight.cli.commands import prompts as prompts_commands
from freeweight.cli.commands import runs as runs_commands
from freeweight.cli.commands import system as system_commands

__all__ = ["app"]

app = typer.Typer(
    name="freeweight",
    help="Measure local open-weight models: capability, efficiency, reliability and resource use.",
    no_args_is_help=False,
    add_completion=True,
)


def _eager_version(show: bool) -> None:
    if not show:
        return
    system_commands.print_version(json_output=False)
    raise typer.Exit(0)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version", is_eager=True, callback=_eager_version, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """freeweight — local model benchmarking, measurement and evidence."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(system_commands.serve)


app.command(name="serve", help="Start the web server (also the default with no subcommand).")(
    system_commands.serve
)
app.command(name="health", help="Report component health.")(system_commands.health)
app.command(name="version", help="Print the application, API and schema versions.")(
    system_commands.version
)
app.command(name="doctor", help="Diagnose a broken installation.")(system_commands.doctor)
app.add_typer(config_commands.app, name="config", help="Configuration inspection and management.")
app.add_typer(db_commands.app, name="db", help="Database migration and maintenance.")
app.add_typer(models_commands.app, name="models", help="Model discovery and inspection.")
app.add_typer(
    goals_commands.app, name="goals", help="Author, inspect and move user-authored goals."
)
app.add_typer(
    judges_commands.app, name="judges", help="Inspect judge eligibility and dry-run a jury."
)
app.add_typer(runs_commands.app, name="run", help="Start, inspect and cancel benchmark runs.")
app.add_typer(prompts_commands.app, name="prompts", help="Inspect and rebuild the prompt pack.")
