"""freeweight.benchmarks.agent.benchmark — ``native.agent``.

Benchmark catalog §3.8. Four multi-step goals over the same deterministic tools: find a symbol then
read its definition, diagnose a failing test from its recorded output and the source, locate the
SKU with no stock, and combine two mock tables.

**Each goal has a shortest path, and it is declared.** ``cases.json`` names the calls a correct
trajectory makes and whether their order matters, so ``wrong_turns``, ``unnecessary_actions`` and
``ordering_accuracy`` are counted against a stated reference rather than against an idea of what
the model should have done.

**The finishing condition is in the prompt and in the expectation, once each.** The goal prompt's
``finish_by`` variable states what the answer must contain and
:class:`~freeweight.domain.scorers.exact.ExactMatchScorer` checks for exactly that, so "task
success" is a string comparison and no part of this suite needs a judge.

**A budget, not a hope.** Every interaction is bounded by
:data:`~freeweight.benchmarks.interaction.DEFAULT_MAX_STEPS`; a model still calling tools when the
budget runs out is recorded with ``hit_step_limit`` and scored as a failure to complete, which is
what it is.
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

PROMPT_ID = "benchmarks.agent.goal"
"""The one prompt record every goal in this suite renders."""


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
        KeyError: A goal offers a tool the fixture toolbox does not define.
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
