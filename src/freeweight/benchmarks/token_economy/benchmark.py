"""freeweight.benchmarks.token_economy.benchmark — ``native.token_economy``, what an answer costs.

Benchmark catalog §3.3. The per-sample counts this suite reports — input, output, thinking and
tool tokens, characters, words and bytes — are collected on **every** benchmark in FreeWeight, not
only this one; they are columns on ``samples``, written by the run engine for every suite. What
this suite adds is a set of bounded tasks whose cost is worth comparing, and the four *derived*
figures the catalog names, which exist only over a set of samples:
``output_tokens_per_success``, ``total_tokens_per_success``, ``quality_per_1k_output_tokens`` and
``successes_per_million_output_tokens``.

**Tokenizers differ, and this suite is where that matters most.** A model with a denser tokenizer
reports fewer tokens for the same answer, so a token count compared across two models without the
character and byte counts beside it is a comparison of tokenizers wearing a measurement's clothes.
Every token metric here therefore ships with ``output_chars`` and ``output_bytes`` in the same
test, and the UI shows them together (benchmark catalog §3.3).

**What "success" means here, and its limit.** This suite's own scorer records that a non-empty
answer arrived — nothing about whether the answer is *right*, because judging that is what the
Phase 7 quality suites do. So ``quality_per_1k_output_tokens`` computed from *this* suite's scores
answers "how many tokens did it spend to say something at all"; the version of that figure worth
acting on is the one computed over a quality suite's scores, and it is the same formula
(:func:`freeweight.domain.metrics.quality_per_1k_output_tokens`) applied to a better numerator.
That limitation is stated here rather than left for a reader to infer from a suspiciously flat
number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.domain.benchmark import BenchmarkCase, BenchmarkManifest, MetricDefinition
from freeweight.domain.metrics import MeasurementClass
from freeweight.domain.scoring import ScoreMethod, ScoreResult
from freeweight.services.prompts import PromptLibrary, load_pack, prompt_subset_hash

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "PROMPT_ID",
    "AnswerArrivedScorer",
    "TokenEconomyBenchmark",
    "TokenEconomyTest",
    "build",
    "load_manifest",
]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"

PROMPT_ID = "benchmarks.token_economy.probe"
"""The one prompt record every case in this suite renders."""


@dataclass(frozen=True, slots=True)
class AnswerArrivedScorer:
    """Records that an answer arrived, and how big it was. It judges no content.

    ``1.0`` for any non-whitespace response, ``0.0`` for an empty one — see the module docstring
    for what that does and does not license the derived quality-per-token figure to mean.
    """

    key: str = "answer_arrived"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Score one response.

        Args:
            case: The case that produced ``response_text``.
            response_text: Exactly what the model returned.

        Returns:
            ``1.0`` when the model said something, ``0.0`` when it said nothing, with the three
            size counts in ``detail`` so a drill-down shows the cost beside the verdict.
        """
        stripped = response_text.strip()
        return ScoreResult(
            score=1.0 if stripped else 0.0,
            method=self.method,
            detail={
                "case": case.case_id,
                "response_chars": len(response_text),
                "response_words": len(response_text.split()),
                "response_bytes": len(response_text.encode("utf-8")),
            },
        )


_COUNT_METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        key="output_tokens",
        unit="count",
        higher_is_better=False,
        aggregation="mean",
        description="Generated tokens the provider reported, per sample.",
    ),
    MetricDefinition(
        key="prompt_tokens",
        unit="count",
        higher_is_better=False,
        aggregation="mean",
        description="Prompt tokens the provider reported, per sample.",
    ),
    MetricDefinition(
        key="thinking_tokens",
        unit="count",
        higher_is_better=False,
        aggregation="mean",
        description=(
            "Reasoning tokens, already counted inside output_tokens. Unsupported, never zero, on "
            "a provider that does not break them out."
        ),
    ),
    MetricDefinition(
        key="tool_tokens",
        unit="count",
        higher_is_better=False,
        aggregation="mean",
        description="Tokens spent on tool-call syntax, already counted inside output_tokens.",
    ),
    MetricDefinition(
        key="output_chars",
        unit="count",
        higher_is_better=False,
        aggregation="mean",
        description=(
            "Characters generated. Shown beside every token count: tokenizers differ, so a token "
            "comparison across models without this is a comparison of tokenizers."
        ),
    ),
    MetricDefinition(
        key="output_words",
        unit="count",
        higher_is_better=False,
        aggregation="mean",
        description="Whitespace-delimited words generated.",
    ),
    MetricDefinition(
        key="output_bytes",
        unit="count",
        higher_is_better=False,
        aggregation="mean",
        description="UTF-8 bytes generated — larger than the character count for non-ASCII text.",
    ),
)

_DERIVED_METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        key="output_tokens_per_success",
        unit="tokens",
        higher_is_better=False,
        aggregation="ratio",
        description=(
            "Output tokens spent per successful sample. Unsupported when nothing succeeded — a "
            "suite that failed every case did not achieve its results cheaply."
        ),
    ),
    MetricDefinition(
        key="total_tokens_per_success",
        unit="tokens",
        higher_is_better=False,
        aggregation="ratio",
        description="Prompt plus output tokens spent per successful sample.",
    ),
    MetricDefinition(
        key="quality_per_1k_output_tokens",
        unit="score/1k tokens",
        higher_is_better=True,
        aggregation="ratio",
        description=(
            "Mean score obtained per thousand output tokens. Within this suite the score records "
            "only that an answer arrived; the figure worth acting on applies the same formula to "
            "a quality suite's scores."
        ),
    ),
    MetricDefinition(
        key="successes_per_million_output_tokens",
        unit="count/1M tokens",
        higher_is_better=True,
        aggregation="ratio",
        description="Successful samples obtained per million output tokens.",
    ),
)


@dataclass(frozen=True, slots=True)
class TokenEconomyTest:
    """One test of ``native.token_economy``: a set of bounded tasks and what they cost.

    Attributes:
        key: Stable test key within the suite.
        name: Human-readable name.
        category: The catalog category this suite contributes to.
        tasks: ``(case_id, task)`` pairs rendered through the pack's one record.
        library: The loaded prompt pack.
    """

    key: str
    name: str
    category: str
    tasks: tuple[tuple[str, str], ...]
    library: PromptLibrary

    @property
    def scorer(self) -> AnswerArrivedScorer:
        """The one scorer every case in this suite uses."""
        return AnswerArrivedScorer()

    @property
    def measurement_class(self) -> str:
        """``warm``. Every case runs after the engine's warm-up, and none loads a cold model."""
        return MeasurementClass.WARM.value

    @property
    def streaming(self) -> bool:
        """``False``. Token *counts* need no stream; only latency figures do."""
        return False

    @property
    def metrics(self) -> Sequence[MetricDefinition]:
        """The seven per-sample counts and the four derived per-set figures."""
        return (*_COUNT_METRICS, *_DERIVED_METRICS)

    @property
    def requires(self) -> Mapping[str, Any]:
        """``token_counts``: without them there is nothing here to measure at all."""
        return {
            "provider_capabilities": ["token_counts"],
            "sandbox": False,
            "network": False,
        }

    def cases(self) -> Iterator[BenchmarkCase]:
        """Yield this test's cases in declaration order, rendered from the prompt record."""
        record = self.library.get(PROMPT_ID)
        for ordinal, (case_id, task) in enumerate(self.tasks):
            rendered = record.render({"task": task})
            yield BenchmarkCase(
                case_id=case_id,
                ordinal=ordinal,
                prompt=rendered.user,
                system_prompt=rendered.system,
                prompt_id=rendered.prompt_id,
                prompt_version=rendered.version,
                required_context_tokens=1024,
                expectation={},
                metadata={"suite": "native.token_economy", "test": self.key},
            )


_SHORT_TASKS: tuple[tuple[str, str], ...] = (
    ("short-capital", "Name the capital of France."),
    ("short-arithmetic", "What is 17 multiplied by 23?"),
    ("short-definition", "Define the term 'KV cache' in one sentence."),
    ("short-list", "List three common file compression formats."),
)

_OPEN_TASKS: tuple[tuple[str, str], ...] = (
    ("open-explain", "Explain why quantizing a model's weights reduces its memory footprint."),
    ("open-compare", "Compare streaming and non-streaming inference from a user's point of view."),
    ("open-tradeoff", "Describe one trade-off involved in increasing a model's context length."),
)


def load_manifest() -> BenchmarkManifest:
    """Load ``manifest.json`` from beside this module.

    Returns:
        The parsed manifest.

    Raises:
        ValueError: The shipped manifest is missing a required field — a packaging defect.
    """
    body = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return BenchmarkManifest.from_json(body)


@dataclass(frozen=True, slots=True)
class TokenEconomyBenchmark:
    """The ``native.token_economy`` suite: its manifest, its pack and its two tests."""

    manifest: BenchmarkManifest
    library: PromptLibrary

    @property
    def tests(self) -> Sequence[TokenEconomyTest]:
        """Two tests: tasks with one right answer, and tasks with no natural length.

        The split is the measurement. A closed question separates a terse model from a verbose one
        with the *same* correct answer; an open one shows what a model spends when nothing bounds
        it. Averaging the two would hide both.
        """
        return (
            TokenEconomyTest(
                key="token_economy.short_answer",
                name="Short answers",
                category="token_efficiency",
                tasks=_SHORT_TASKS,
                library=self.library,
            ),
            TokenEconomyTest(
                key="token_economy.open_ended",
                name="Open-ended answers",
                category="token_efficiency",
                tasks=_OPEN_TASKS,
                library=self.library,
            ),
        )


def build(library: PromptLibrary | None = None) -> TokenEconomyBenchmark:
    """Build the suite, verifying that the manifest describes the installed prompts.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one.

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` does not match the installed pack. See
            :func:`freeweight.benchmarks.performance.benchmark.build` for why this is a refusal
            rather than a recomputation.
        PromptNotFound: The manifest declares a prompt the installed pack does not have.
    """
    pack = library if library is not None else load_pack()
    manifest = load_manifest()
    references = pack.references(
        (entry["prompt_id"], entry.get("version")) for entry in manifest.prompt_ids
    )
    actual = prompt_subset_hash(references)
    if manifest.prompt_subset_hash != actual:
        raise ValueError(
            f"Benchmark {manifest.key!r} declares prompt_subset_hash "
            f"{manifest.prompt_subset_hash!r}; the installed pack's declared prompts hash to "
            f"{actual!r}. Rebuild the manifest and bump the suite version."
        )
    return TokenEconomyBenchmark(manifest=manifest, library=pack)
