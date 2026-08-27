"""freeweight.benchmarks.echo.benchmark — ``native.echo``, the harness self-test.

A trivial, deterministic suite whose only purpose is to exercise the whole machine: queue →
claim → prepare → warm → execute → aggregate → complete, with samples stored, events streamed and
a run page that renders. It ships in the product and stays there, because "does the run engine
work on this machine, right now, with this provider" is a question a user asks on a fresh install
and after every upgrade, and answering it with a real benchmark conflates a broken harness with a
weak model.

**It measures FreeWeight, not the model.** :class:`HarnessRoundTripScorer` scores whether a
response came back at all — nothing about its content. That is deliberate and it is the only
honest thing this suite can score: a self-test must pass on every provider including
:class:`~modelrack.testing.FakeProvider`, and any content assertion strong enough to be
interesting would fail on one of them for reasons that have nothing to do with the harness. The
manifest declares no capabilities, so ``native.echo`` contributes to no
``capability.evidence`` and cannot be mistaken for a quality measurement.

The suite's two tests differ only in prompt size, which exercises the engine on a short and a
longer request without pretending to measure anything about either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.domain.benchmark import BenchmarkCase, BenchmarkManifest, MetricDefinition
from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "EchoBenchmark",
    "EchoTest",
    "HarnessRoundTripScorer",
    "build",
    "load_manifest",
]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"

_LONG_PROMPT_PREFIX = (
    "Below is a short passage. Read it and then reply with a single sentence summarising it.\n\n"
    "A benchmark harness is not a benchmark. The harness schedules work, calls a provider, stores "
    "what came back, aggregates it and shows it to a person. Each of those can fail on its own, "
    "and each fails in a way that looks, from the outside, exactly like a model performing badly. "
    "A self-test exists so that the two can be told apart.\n\n"
)


@dataclass(frozen=True, slots=True)
class HarnessRoundTripScorer:
    """Scores whether the harness completed a round trip for one case.

    ``1.0`` for a non-empty response, ``0.0`` for an empty one. A sample that never got a response
    at all does not reach a scorer — the run engine records it as a failed sample with the
    provider's error, and a failed sample is excluded from the aggregate rather than scored zero
    (ADR-0016). So ``0.0`` here means precisely one thing: the provider answered, and answered
    with nothing.

    ``detail`` records the response's length and whether the case's marker appeared in it. The
    marker is **not** scored: a model is free to summarise rather than echo, and scoring an echo
    would make this self-test fail on well-behaved models for reasons unrelated to the harness.
    It is recorded because it is free, and because a person reading a sample wants to see it.
    """

    key: str = "harness_roundtrip"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Score one response.

        Args:
            case: The case that produced ``response_text``; its ``expectation`` may carry a
                ``marker`` string to record the presence of.
            response_text: Exactly what the model returned.

        Returns:
            ``1.0`` when ``response_text`` has any non-whitespace content, ``0.0`` otherwise.
            Never ``None``: a response that arrived is always scoreable by this rule, even when
            the answer is "it was empty".
        """
        stripped = response_text.strip()
        marker = case.expectation.get("marker")
        return ScoreResult(
            score=1.0 if stripped else 0.0,
            method=self.method,
            detail={
                "response_chars": len(response_text),
                "marker": marker,
                "marker_present": bool(marker) and str(marker) in response_text,
            },
        )


@dataclass(frozen=True, slots=True)
class EchoTest:
    """One of ``native.echo``'s two tests: a fixed list of cases and one scorer.

    Attributes:
        key: Stable test key within the suite.
        name: Human-readable name.
        category: The catalog category the suite belongs to.
        prompts: ``(case_id, prompt)`` pairs in declaration order.
    """

    key: str
    name: str
    category: str
    prompts: tuple[tuple[str, str], ...]

    @property
    def scorer(self) -> HarnessRoundTripScorer:
        """The one scorer every case in this test uses."""
        return HarnessRoundTripScorer()

    @property
    def measurement_class(self) -> str:
        """``n/a``. A self-test measures the harness, for which cold and warm mean nothing."""
        return "n/a"

    @property
    def streaming(self) -> bool:
        """``False``. A round trip is a round trip; nothing here needs a first-token moment."""
        return False

    @property
    def metrics(self) -> Sequence[MetricDefinition]:
        """The single metric this test produces."""
        return (
            MetricDefinition(
                key="harness_roundtrip_success",
                unit="ratio",
                higher_is_better=True,
                aggregation="mean",
                description=(
                    "Share of cases for which the harness sent a prompt and stored a non-empty "
                    "response. A property of FreeWeight and the provider, not of the model."
                ),
            ),
        )

    @property
    def requires(self) -> Mapping[str, Any]:
        """Nothing. A self-test that could be skipped would not be a self-test."""
        return {"provider_capabilities": [], "sandbox": False, "network": False}

    def cases(self) -> Iterator[BenchmarkCase]:
        """Yield this test's cases in declaration order, one per prompt."""
        for ordinal, (case_id, prompt) in enumerate(self.prompts):
            yield BenchmarkCase(
                case_id=case_id,
                ordinal=ordinal,
                prompt=prompt,
                expectation={"marker": case_id},
                metadata={"suite": "native.echo", "test": self.key},
            )


def load_manifest() -> BenchmarkManifest:
    """Load and parse ``manifest.json`` from beside this module.

    Read from the file rather than built in Python so that the manifest is the artefact the hash
    is taken over and the thing a user can read, exactly as benchmark catalog §5 describes for
    every other suite. A self-test suite that special-cased its own manifest format would stop
    exercising the manifest machinery, which is half of what it is for.

    Returns:
        The parsed manifest.

    Raises:
        ValueError: The shipped manifest is missing a required field — a packaging defect, not a
            user error.
    """
    body = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return BenchmarkManifest.from_json(body)


@dataclass(frozen=True, slots=True)
class EchoBenchmark:
    """The ``native.echo`` suite: its manifest and its two tests."""

    manifest: BenchmarkManifest

    @property
    def tests(self) -> Sequence[EchoTest]:
        """The two tests, short prompts first."""
        return (
            EchoTest(
                key="echo.short",
                name="Short round trip",
                category="reliability",
                prompts=(
                    ("echo-short-1", "Reply with exactly this word: echo-short-1"),
                    ("echo-short-2", "Reply with exactly this word: echo-short-2"),
                    ("echo-short-3", "Reply with exactly this word: echo-short-3"),
                ),
            ),
            EchoTest(
                key="echo.long",
                name="Longer round trip",
                category="reliability",
                prompts=(
                    (
                        "echo-long-1",
                        f"{_LONG_PROMPT_PREFIX}End your reply with the word echo-long-1.",
                    ),
                    (
                        "echo-long-2",
                        f"{_LONG_PROMPT_PREFIX}End your reply with the word echo-long-2.",
                    ),
                ),
            ),
        )


def build() -> EchoBenchmark:
    """Build the ``native.echo`` benchmark.

    The entry point :func:`freeweight.services.runs.build_registry` calls. A function rather than
    a module-level instance so that loading this module reads no file until something actually
    wants the benchmark.
    """
    return EchoBenchmark(manifest=load_manifest())
