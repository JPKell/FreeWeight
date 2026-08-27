"""freeweight.benchmarks.loading — one loader for the declarative quality suites.

Phase 7 adds five suites that differ in what they measure and agree on everything else: each is a
``manifest.json`` naming the suite and its metrics, a ``cases.json`` holding its tests and their
cases, and a small module that says which scorer reads them and how the conversation is driven.
That shape is written here once rather than five times, because five copies of "load the manifest,
verify the prompt subset hash, render the cases" is five places for a suite to drift from the
others in how it attributes a prompt.

**Case text is data.** Task statements, constraints, schemas and expected answers live in
``cases.json`` beside the suite, never as string literals in Python: they are prompt content and
expectation content, and prompt standards §1 and §7 keep both out of source where they cannot be
versioned, diffed or attached to a result.

**The manifest is verified, never recomputed.** A suite whose declared ``prompt_subset_hash`` does
not match the installed pack refuses to build, exactly as ``native.performance`` does — the subset
hash is a fingerprint input, and silently correcting it here would let a stale manifest produce
runs whose provenance describes prompts that were never rendered (ADR-0028 §1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from freeweight.domain.benchmark import BenchmarkCase, BenchmarkManifest, MetricDefinition
from freeweight.services.prompts import PromptLibrary, prompt_subset_hash

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path


__all__ = [
    "SuiteBenchmark",
    "SuiteTest",
    "build_tests",
    "load_cases",
    "load_manifest",
    "metric_definitions",
    "verify_prompts",
]


@dataclass(frozen=True, slots=True)
class SuiteTest:
    """One test of a declarative suite: its cases, its scorer and how it is executed.

    Satisfies :class:`~freeweight.domain.benchmark.BenchmarkTest` structurally, and adds one
    attribute the protocol does not know about: :attr:`interaction`. A test that declares one is
    executed through it — a tool loop, or a call plus a corrective retry — instead of through the
    run engine's default single call.

    Attributes:
        key: Stable test key within the suite.
        name: Human-readable name.
        category: The benchmark catalog §2 category the suite contributes to.
        measurement_class: ``cold``, ``warm``, ``cache_reused`` or ``n/a``. Declared, never
            defaulted: it decides what aggregation may combine.
        streaming: Whether cases run through :meth:`~modelrack.Provider.stream`. ``False``
            throughout Phase 7 — a quality suite measures what was said, not when it arrived, and
            streaming a tool loop would add timing noise to a correctness figure.
        metrics: What this test produces.
        requires: Preconditions, checked before the test runs.
        scorer: The scorer every case in this test is scored by.
        interaction: The conversation driver, or ``None`` for a single-call test.
        declared_cases: The cases, already rendered.
    """

    key: str
    name: str
    category: str
    measurement_class: str
    streaming: bool
    metrics: tuple[MetricDefinition, ...]
    requires: Mapping[str, Any]
    scorer: Any
    interaction: Any = None
    declared_cases: tuple[BenchmarkCase, ...] = ()

    def cases(self) -> Iterator[BenchmarkCase]:
        """Yield this test's cases in declaration order, from a fresh iterator each call."""
        return iter(self.declared_cases)


@dataclass(frozen=True, slots=True)
class SuiteBenchmark:
    """A manifest plus its tests — everything the run engine needs to execute a suite."""

    manifest: BenchmarkManifest
    tests: tuple[SuiteTest, ...] = ()


def load_manifest(path: Path) -> BenchmarkManifest:
    """Load one suite's ``manifest.json``.

    Args:
        path: The manifest file.

    Returns:
        The parsed manifest.

    Raises:
        ValueError: The shipped manifest is missing a required field — a packaging defect.
    """
    return BenchmarkManifest.from_json(json.loads(path.read_text(encoding="utf-8")))


def load_cases(path: Path) -> Mapping[str, Any]:
    """Load one suite's ``cases.json``.

    Args:
        path: The case file.

    Returns:
        The parsed body, with a ``tests`` list.

    Raises:
        ValueError: The file declares no ``tests`` list. A suite with no tests would install
            cleanly, run instantly and report nothing, which is worse than refusing to build.
    """
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or not isinstance(body.get("tests"), list):
        raise ValueError(f"Benchmark case file {path} declares no 'tests' list.")
    return body


def verify_prompts(manifest: BenchmarkManifest, pack: PromptLibrary) -> None:
    """Refuse a suite whose manifest does not describe the prompts the pack holds.

    Args:
        manifest: The suite's manifest.
        pack: The installed prompt pack.

    Raises:
        ValueError: The declared ``prompt_subset_hash`` does not match what the pack's declared
            prompts hash to. Refused rather than recomputed — see the module docstring.
        PromptNotFound: The manifest declares a prompt the installed pack does not have.
    """
    references = pack.references(
        (entry["prompt_id"], entry.get("version")) for entry in manifest.prompt_ids
    )
    actual = prompt_subset_hash(references)
    if manifest.prompt_subset_hash != actual:
        raise ValueError(
            f"Benchmark {manifest.key!r} declares prompt_subset_hash "
            f"{manifest.prompt_subset_hash!r}; the installed pack's declared prompts hash to "
            f"{actual!r}. Rebuild the manifest and bump the suite version — a prompt this suite "
            "uses has changed, which separates its results."
        )


def metric_definitions(manifest: BenchmarkManifest) -> dict[str, MetricDefinition]:
    """Index a manifest's declared metrics by key.

    Args:
        manifest: The suite's manifest.

    Returns:
        The definitions, by key.
    """
    return {
        str(entry["key"]): MetricDefinition(
            key=str(entry["key"]),
            unit=str(entry["unit"]),
            higher_is_better=bool(entry["higher_is_better"]),
            aggregation=str(entry["aggregation"]),
            description=str(entry.get("description", "")),
        )
        for entry in manifest.body.get("metrics", ())
    }


def build_tests(  # noqa: PLR0913 — a suite is assembled from exactly these six things
    *,
    manifest: BenchmarkManifest,
    cases: Mapping[str, Any],
    pack: PromptLibrary,
    prompt_id: str,
    scorer_for: Any,
    interaction_for: Any = None,
    variables_for: Any = None,
) -> tuple[SuiteTest, ...]:
    """Assemble a suite's tests from its manifest and its case file.

    Args:
        manifest: The suite's manifest, supplying the metric definitions.
        cases: The parsed ``cases.json``.
        pack: The installed prompt pack the cases render from.
        prompt_id: The record every case in this suite renders.
        scorer_for: ``(test_body) -> Scorer``. A callable rather than a fixed scorer because a
            suite's tests do not all measure the same thing — ``native.tool_use`` scores selection
            in one test and arguments in another, from one case file.
        interaction_for: ``(test_body) -> Interaction | None``, or ``None`` for single-call tests.
        variables_for: ``(case_body) -> Mapping[str, Any]`` producing the prompt variables, or
            ``None`` to use the case's own ``variables`` unchanged. This is the hook that lets
            ``native.instruction_following`` render the constraint text from the same declaration
            its scorer checks, so the two cannot drift apart.

    Returns:
        The tests, in declaration order.

    Raises:
        KeyError: A test names a metric the manifest does not define — caught at build time,
            which is startup, rather than as a missing column after a run.
    """
    definitions = metric_definitions(manifest)
    record = pack.get(prompt_id)
    built: list[SuiteTest] = []
    for body in cases["tests"]:
        metrics = tuple(definitions[str(key)] for key in body["metric_keys"])
        declared: list[BenchmarkCase] = []
        for ordinal, case_body in enumerate(body["cases"]):
            variables = (
                dict(case_body.get("variables", {}))
                if variables_for is None
                else dict(variables_for(case_body))
            )
            rendered = record.render(variables)
            declared.append(
                BenchmarkCase(
                    case_id=str(case_body["case_id"]),
                    ordinal=ordinal,
                    prompt=rendered.user,
                    system_prompt=rendered.system,
                    prompt_id=rendered.prompt_id,
                    prompt_version=rendered.version,
                    required_context_tokens=case_body.get("required_context_tokens"),
                    expectation=dict(case_body.get("expectation", {})),
                    metadata={
                        "suite": manifest.key,
                        "test": str(body["key"]),
                        "scenario": str(case_body.get("scenario", "")),
                        **dict(case_body.get("metadata", {})),
                    },
                )
            )
        built.append(
            SuiteTest(
                key=str(body["key"]),
                name=str(body["name"]),
                category=str(body.get("category", manifest.category)),
                measurement_class=str(body.get("measurement_class", "warm")),
                streaming=bool(body.get("streaming", False)),
                metrics=metrics,
                requires=dict(body.get("requires", manifest.requires)),
                scorer=scorer_for(body),
                interaction=None if interaction_for is None else interaction_for(body),
                declared_cases=tuple(declared),
            )
        )
    return tuple(built)
