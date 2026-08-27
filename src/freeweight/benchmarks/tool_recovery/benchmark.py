"""freeweight.benchmarks.tool_recovery.benchmark — ``native.tool_recovery``.

Benchmark catalog §3.7. Six cases, one per declared failure — file not found, invalid argument,
empty search, permission denied, tool timeout, ambiguous result — each injected into a tool that
otherwise works perfectly.

**The failure is scheduled, not simulated in the prompt.** The toolbox takes a per-case schedule
(:class:`~freeweight.benchmarks.fixtures.tools.MockToolbox`), the first call to the named tool
returns that error, and every later call works. A model is never told a failure is coming, so what
is measured is what it does when one arrives rather than how well it follows a stage direction.

**Recovery is defined, not inferred.** A failed call counts as recovered when a later call
succeeded; retrying the identical call is *not* excluded from that, because retrying is the right
answer to a timeout — how much of the trajectory was mere repetition is reported separately as
``retry_count`` and ``repeated_error_count``
(:func:`~freeweight.domain.scorers.agent.trajectory_metrics`).

Scored by :class:`~freeweight.domain.scorers.agent.AgentTrajectoryScorer`: recovery is a property
of a path, and the agent scorer is this application's instrument for paths. One instrument rather
than two near-identical ones, so ``wrong_turns`` means the same thing in both suites.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.benchmarks.interaction import ToolSession
from freeweight.benchmarks.loading import (
    SuiteBenchmark,
    build_tests,
    load_cases,
    load_manifest,
    verify_prompts,
)
from freeweight.benchmarks.tool_use.benchmark import toolbox_for
from freeweight.domain.scorers.agent import AgentTrajectoryScorer
from freeweight.services.prompts import load_pack

if TYPE_CHECKING:
    from freeweight.services.prompts import PromptLibrary

__all__ = ["PROMPT_ID", "build", "load_suite_manifest"]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_CASES_PATH = Path(__file__).parent / "cases.json"

PROMPT_ID = "benchmarks.tool_recovery.task"
"""The one prompt record every case in this suite renders."""


def load_suite_manifest() -> Any:  # noqa: ANN401 — BenchmarkManifest, imported lazily by callers
    """Load ``manifest.json`` from beside this module."""
    return load_manifest(_MANIFEST_PATH)


def build(library: PromptLibrary | None = None) -> SuiteBenchmark:
    """Build the suite, verifying that the manifest describes the installed prompts.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one.

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` does not match the installed pack.
        KeyError: A case offers a tool the fixture toolbox does not define, or schedules a failure
            on a tool it does not offer.
    """
    pack = library if library is not None else load_pack()
    manifest = load_suite_manifest()
    verify_prompts(manifest, pack)
    tests = build_tests(
        manifest=manifest,
        cases=load_cases(_CASES_PATH),
        pack=pack,
        prompt_id=PROMPT_ID,
        scorer_for=lambda _body: AgentTrajectoryScorer(),
        interaction_for=lambda _body: ToolSession(toolbox=toolbox_for),
    )
    for test in tests:
        for case in test.declared_cases:
            toolbox_for(case).definitions()
    return SuiteBenchmark(manifest=manifest, tests=tests)
