"""freeweight.cli.commands.benchmarks — ``freeweight benchmarks list|show`` (spec §7.2, Phase 12).

The CLI form of ``GET /api/v1/benchmarks`` and ``GET /api/v1/benchmarks/{key}``. It reads the same
registry the run engine executes from — a suite listed here is a suite ``run start`` accepts — and
needs no server and no database: the registry is a pure value built from the shipped manifests and
the user's installed goal packs.

Every heavy import is inside a command body (CLI standards §12).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from freeweight.domain.benchmark import BenchmarkRegistry

__all__ = ["app"]

app = typer.Typer(help="List and inspect the benchmark suites this build can run.")

_ConfigOption = Annotated[str | None, typer.Option("--config", help="Path to a config.toml file.")]
_JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


def _registry(config: str | None) -> BenchmarkRegistry:
    """Build the registry the run engine uses, including the user's installed goal packs."""
    from freeweight.config import load_settings
    from freeweight.services.runs import build_registry_for

    settings = load_settings(config_path=config).settings
    return build_registry_for(settings)


@app.command("list")
def list_command(config: _ConfigOption = None, as_json: _JsonOption = False) -> None:
    """List every benchmark suite this build can run, in key order."""
    registry = _registry(config)
    suites = sorted(registry.all(), key=lambda benchmark: benchmark.manifest.key)
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "key": benchmark.manifest.key,
                        "name": benchmark.manifest.name,
                        "version": benchmark.manifest.version,
                        "category": benchmark.manifest.category,
                        "runner": benchmark.manifest.runner,
                        "capabilities": list(benchmark.manifest.capabilities),
                        "test_count": len(benchmark.tests),
                    }
                    for benchmark in suites
                ],
                indent=2,
            )
        )
        return
    for benchmark in suites:
        manifest = benchmark.manifest
        typer.echo(f"{manifest.key}  ({manifest.name})")
        typer.echo(
            f"    {manifest.runner} · {manifest.category} · {len(benchmark.tests)} test(s)"
            f" · v{manifest.version}"
        )


@app.command("show")
def show_command(
    key: Annotated[str, typer.Argument(help="The suite key, e.g. native.performance.")],
    config: _ConfigOption = None,
    as_json: _JsonOption = False,
) -> None:
    """Show one suite's manifest, its tests and their metrics."""
    from freeweight.domain.benchmark import BenchmarkNotFound

    registry = _registry(config)
    if key not in registry.keys():
        typer.echo(f"No benchmark suite {key!r} is installed.", err=True)
        raise typer.Exit(code=1) from BenchmarkNotFound(key)
    benchmark = registry.get(key)
    manifest = benchmark.manifest
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "key": manifest.key,
                    "name": manifest.name,
                    "version": manifest.version,
                    "category": manifest.category,
                    "runner": manifest.runner,
                    "capabilities": list(manifest.capabilities),
                    "manifest_hash": manifest.manifest_hash,
                    "tests": [
                        {
                            "key": test.key,
                            "name": test.name,
                            "metrics": [metric.metric_key for metric in test.metrics],
                        }
                        for test in benchmark.tests
                    ],
                },
                indent=2,
            )
        )
        return
    typer.echo(f"{manifest.key} — {manifest.name} (v{manifest.version})")
    typer.echo(f"  runner: {manifest.runner}   category: {manifest.category}")
    typer.echo(f"  capabilities: {', '.join(manifest.capabilities) or '—'}")
    typer.echo(f"  manifest hash: {manifest.manifest_hash}")
    for test in benchmark.tests:
        metrics = ", ".join(metric.metric_key for metric in test.metrics)
        typer.echo(f"  • {test.key} ({test.name}): {metrics or 'no metrics'}")
