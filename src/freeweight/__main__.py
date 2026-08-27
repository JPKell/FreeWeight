"""freeweight.__main__ — ``python -m freeweight``.

Delegates to the Typer app, whose root callback starts ``serve`` when invoked with no subcommand
(CLI Standards §1).
"""

from __future__ import annotations

from freeweight.cli.main import app

if __name__ == "__main__":
    app()
