"""freeweight.benchmarks.tool_use.benchmark — ``native.tool_use``.

Benchmark catalog §3.6. Four tests covering the catalog's eleven scenarios, over the mock toolbox
in :mod:`freeweight.benchmarks.fixtures.tools` — no shell, no real filesystem, no network, no real
database (spec §14).

**A scenario is built from what the model is offered, not from what it is told.** "Tool
unavailable" is a case that offers a toolbox without that tool; "several similar tools" is a case
that offers four plausible ones; "tool failure" is a case with a scheduled failure on its first
call. None of them is a sentence in the prompt asking the model to pretend, which is why each
scenario measures behaviour rather than compliance with a stage direction.

**Selection and arguments are scored by different instruments.** ``tool_use.selection`` uses
:class:`~freeweight.domain.scorers.tools.ToolSelectionScorer`; ``tool_use.arguments`` uses
:class:`~freeweight.domain.scorers.tools.ToolArgumentScorer`. Picking the wrong tool and passing
the wrong path to the right one are different defects, and one headline number for both would make
them impossible to tell apart.

**Capability-gated.** The suite requires ``tool_calling``. A provider that has not declared it
produces a skipped test with ``unsupported_capability``, contributing no score — never a zero
(benchmark catalog §3.5's rule, applied here for the same reason).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.benchmarks.fixtures.tools import MockToolbox
from freeweight.benchmarks.interaction import ToolSession
from freeweight.benchmarks.loading import (
    SuiteBenchmark,
    build_tests,
    load_cases,
    load_manifest,
    verify_prompts,
)
from freeweight.domain.scorers.tools import ToolArgumentScorer, ToolSelectionScorer
from freeweight.services.prompts import load_pack

if TYPE_CHECKING:
    from freeweight.domain.benchmark import BenchmarkCase
    from freeweight.services.prompts import PromptLibrary

__all__ = ["PROMPT_ID", "build", "load_suite_manifest", "toolbox_for"]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_CASES_PATH = Path(__file__).parent / "cases.json"

PROMPT_ID = "benchmarks.tool_use.task"
"""The one prompt record every case in this suite renders."""

_ARGUMENT_TESTS = frozenset({"tool_use.arguments"})
"""Tests whose headline is argument correctness rather than tool choice."""


def toolbox_for(case: BenchmarkCase, sandbox_root: Path | None = None) -> MockToolbox:
    """Build the toolbox one case offers, from that case's own metadata.

    A fresh instance per case: the injected-failure counters are per-case state, and a shared
    toolbox would let one case's scheduled timeout land on the next case's first call.

    Args:
        case: The case, whose ``metadata`` names ``offered_tools`` and any ``injected_failures``.
        sandbox_root: Where ``write_sandbox_file`` may write, or ``None`` for a directory under
            the system temporary root. Injected so a run can scope it and a test can inspect it;
            no case in this suite writes, and the directory is created only on first use.

    Returns:
        The toolbox.
    """
    import tempfile

    root = (
        sandbox_root
        if sandbox_root is not None
        else Path(tempfile.gettempdir()) / "freeweight-tools"
    )
    return MockToolbox(
        sandbox_root=root,
        offered=tuple(case.metadata.get("offered_tools", ())),
        injected_failures=dict(case.metadata.get("injected_failures", {})),
    )


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
        PromptNotFound: The manifest declares a prompt the pack does not have.
        KeyError: A case offers a tool the fixture toolbox does not define — caught here, at
            startup, rather than as a hallucinated-tool score in the middle of a run.
    """
    pack = library if library is not None else load_pack()
    manifest = load_suite_manifest()
    verify_prompts(manifest, pack)
    session = ToolSession(toolbox=toolbox_for)
    tests = build_tests(
        manifest=manifest,
        cases=load_cases(_CASES_PATH),
        pack=pack,
        prompt_id=PROMPT_ID,
        scorer_for=lambda body: (
            ToolArgumentScorer() if body["key"] in _ARGUMENT_TESTS else ToolSelectionScorer()
        ),
        interaction_for=lambda _body: session,
    )
    for test in tests:
        for case in test.declared_cases:
            toolbox_for(case).definitions()
    return SuiteBenchmark(manifest=manifest, tests=tests)
