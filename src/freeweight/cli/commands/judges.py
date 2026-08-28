"""freeweight.cli.commands.judges — who may judge, and whether a jury can be assembled.

Spec §7.2's ``judges`` group: ``list`` and ``validate``. Both answer the same question from
different directions — *can this goal be judged on this machine, and by whom* — and both answer it
from :mod:`freeweight.domain.jury`, so neither can disagree with an actual run.

``list`` is **local** mode; ``validate`` is too. Neither needs a server, and neither generates
anything: assembling a jury is a decision about eligibility, not a call to a model.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

__all__ = ["app"]

app = typer.Typer(help="Inspect the models eligible to judge, and dry-run a jury.")

_JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]
_ConfigOption = Annotated[
    str | None, typer.Option("--config", help="Path to config.toml.", envvar="FREEWEIGHT_CONFIG")
]

_EXIT_CONFIGURATION = 3
_EXIT_DEPENDENCY = 4
_EXIT_OPERATION_FAILED = 5


def _settings(config: str | None) -> Any:  # noqa: ANN401 — freeweight.config.Settings
    """Resolve configuration, or exit 3."""
    from baseaicore import ConfigurationError

    from freeweight.config import load_settings

    try:
        return load_settings(config_path=config).settings
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(_EXIT_CONFIGURATION) from exc


def _installed(settings: Any) -> list[str]:  # noqa: ANN401 — freeweight.config.Settings
    """Return the canonical IDs this machine can serve, or exit 4 naming the provider."""
    from modelrack.errors import ProviderError

    from freeweight.infrastructure.providers.factory import build_provider

    provider = build_provider(settings.provider)
    try:
        return sorted(descriptor.identity.canonical_id for descriptor in provider.list_models())
    except ProviderError as exc:
        typer.echo(
            f"Error: the provider could not list models: {exc.message} ({exc.code})", err=True
        )
        raise typer.Exit(_EXIT_DEPENDENCY) from exc


@app.command("list")
def list_judges(
    candidate: Annotated[
        str | None,
        typer.Option("--candidate", help="A model being measured; it may not judge itself."),
    ] = None,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """List the models eligible to serve as jurors, with the reason for every refusal. Mode: local.

    Each entry links to that model's own ``native.judge`` results, which is how "how trustworthy is
    this instrument" is answered in one interaction (benchmark catalog §1).
    """
    from freeweight.domain.judging import JUDGE_SUITE_KEY, eligible_jurors

    settings = _settings(config)
    available = _installed(settings)
    verdicts = eligible_jurors(
        available,
        candidate=candidate,
        requested=list(settings.judge.models),
        allow_remote=settings.providers.allow_remote and settings.judge.allow_remote,
    )
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "items": [
                        {
                            "model": verdict.model_canonical_id,
                            "eligible": verdict.eligible,
                            "reasons": list(verdict.reasons),
                            "judge_benchmark_suite": JUDGE_SUITE_KEY,
                        }
                        for verdict in verdicts
                    ],
                    "jury_size": settings.judge.jury_size,
                }
            )
        )
        return
    if not verdicts:
        typer.echo("No models installed; run `freeweight models refresh`.")
        return
    for verdict in verdicts:
        mark = "eligible" if verdict.eligible else ", ".join(verdict.reasons)
        typer.echo(f"{verdict.model_canonical_id:<48} {mark}")
    typer.echo(f"\nJury size {settings.judge.jury_size}. Bias results: {JUDGE_SUITE_KEY}.")


@app.command("validate")
def validate(
    goal: Annotated[
        str | None,
        typer.Option("--goal", help="Validate the jury this goal's own configuration asks for."),
    ] = None,
    candidate: Annotated[
        str | None,
        typer.Option("--candidate", help="The model being measured; it may not judge itself."),
    ] = None,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Dry-run a jury configuration. Mode: local.

    Reports the jury that would be assembled, whether it is smaller than asked for, and every
    refusal with its reason — self-judging conflicts and remote permission included. Nothing is
    generated: this is a decision about eligibility.

    Exits ``5`` when no model is eligible at all, which is the state in which judged criteria
    would skip and only the rule criteria would score.
    """
    from freeweight.domain.jury import assemble_jury
    from freeweight.services.goals import get_goal

    settings = _settings(config)
    available = _installed(settings)
    jury_size = settings.judge.jury_size
    requested = list(settings.judge.models)
    goal_allows_remote = settings.judge.allow_remote
    slug = None
    if goal is not None:
        loaded = get_goal(settings.goals.root_path, goal)
        slug = loaded.pack.slug
        if loaded.pack.judge is not None:
            jury_size = loaded.pack.judge.jury_size
            requested = list(loaded.pack.judge.models) or requested
            goal_allows_remote = loaded.pack.judge.allow_remote
    assembly = assemble_jury(
        available,
        candidate=candidate if settings.judge.refuse_self_judging else None,
        requested=requested,
        jury_size=jury_size,
        allow_remote=settings.providers.allow_remote and goal_allows_remote,
    )
    payload = {"goal": slug, "candidate": candidate, **assembly.as_json()}
    if json_output:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(f"Jury of {len(assembly.jurors)} (asked for {assembly.requested_size}):")
        for juror in assembly.jurors:
            typer.echo(f"  {juror}")
        for refusal in assembly.refusals:
            typer.echo(f"  refused {refusal.model_canonical_id}: {', '.join(refusal.reasons)}")
        if assembly.reduced:
            typer.echo(
                "  jury_reduced: inter-juror agreement will be weaker or absent, and the result "
                "will say so."
            )
    if not assembly.available:
        typer.echo(
            "Error: no model is eligible to judge. Judged criteria would be skipped "
            "(judge_unavailable) and rule criteria would still score. (JUDGE_UNAVAILABLE)",
            err=True,
        )
        raise typer.Exit(_EXIT_OPERATION_FAILED)
