"""freeweight.benchmarks.reliability.benchmark — ``native.reliability``, how much a model moves.

Benchmark catalog §3.13. Two tests whose questions have exactly one short correct answer, run at
whatever repetition count the run was configured with, and one derivation that reads every stored
repetition back.

**The questions are trivial on purpose.** This suite does not measure whether a model can answer;
it measures whether it answers *the same way twice*. A hard question conflates the two — a model
that gets it right a third of the time looks unreliable when it is merely wrong — so every case
here is something any usable model answers correctly, and the interesting number is the spread
around that.

**Nothing is reduced to its best attempt.** ``pass@k`` is the unbiased estimator over all
repetitions (:func:`freeweight.domain.statistics.pass_at_k`), not "did any of the first *k* pass",
because the latter gives two different answers for the same stored samples depending on the order
you read them in.

**Agreement is measured on the answer, not on the score.** Two repetitions that both score ``1.0``
by giving different correct answers are reliable in score and unreliable in output, and a routing
or caching decision depends on the second. The label is the response hash the sample already
stores, so nothing here requires response text to be retained (spec §14).

**Outliers are flagged, never quietly dropped.** The default policy reports and keeps; a caller
that excludes gets the policy, the threshold and the removed values back in the report, which is
catalog §3.13's "any exclusion is explicit, reasoned and preserved in the raw data".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baseaicore import canonical_json, sha256_of

from freeweight.benchmarks.loading import load_cases
from freeweight.benchmarks.reliability.reliability import (
    DEFAULT_PASS_AT_K,
    CaseAttempts,
    summarize_suite,
)
from freeweight.domain.aggregation import AggregatedMetric
from freeweight.domain.benchmark import BenchmarkCase, BenchmarkManifest, MetricDefinition
from freeweight.domain.metrics import MeasurementClass, MetricResult
from freeweight.domain.scoring import ScoreMethod, ScoreResult
from freeweight.domain.statistics import OutlierPolicy, Statistic, summarize
from freeweight.services.prompts import PromptLibrary, load_pack, prompt_subset_hash

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "PROMPT_ID",
    "ExactShortAnswerScorer",
    "ReliabilityBenchmark",
    "ReliabilityTest",
    "build",
    "derive",
    "load_manifest",
]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"

PROMPT_ID = "benchmarks.reliability.probe"
"""The one prompt record every case in this suite renders."""

_CASES_PATH = Path(__file__).parent / "cases.json"
"""The suite's questions.

A **file**, not a tuple in this module, and hashed into ``dataset_hashes`` like every other suite
whose content can drift. Editing a question changes what this suite measures; when the questions
lived in Python, only the suite *version* separated those results — which depended on whoever
edited the tuple remembering to bump it. Now the separation is structural: the hash moves, the
manifest check fails, and the person editing is told what they changed."""

CASES_DATASET_KEY = "reliability_cases"
"""``dataset_hashes`` key for the case file."""


def cases_hash() -> str:
    """Return the ``sha256:``-prefixed hash of the installed case file.

    Over canonical JSON rather than the file's bytes, exactly as a corpus hash is: re-indenting the
    file must not separate this suite's results from the ones it produced yesterday.

    Returns:
        ``"sha256:"`` followed by 64 lowercase hex characters.
    """
    body = json.loads(_CASES_PATH.read_text(encoding="utf-8"))
    return f"sha256:{sha256_of(canonical_json(body))}"


def _case_spec(test_key: str) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Read one test's ``(case_id, question, accepted answers)`` triples from the case file.

    The accepted set is written out in the file rather than normalized at scoring time, so that
    "nine" and "9" are a *declared* equivalence rather than a tokenizer accident.

    Raises:
        ValueError: The file declares no test with that key — a packaging defect, refused at build
            time rather than producing a suite with an empty test in it.
    """
    body = load_cases(_CASES_PATH)
    for test in body["tests"]:
        if str(test["key"]) == test_key:
            return tuple(
                (str(case["case_id"]), str(case["question"]), tuple(case["accepted"]))
                for case in test["cases"]
            )
    raise ValueError(f"Reliability case file declares no test {test_key!r}.")


@dataclass(frozen=True, slots=True)
class ExactShortAnswerScorer:
    """Scores a short answer against a declared set of accepted ones.

    Case-insensitive, whitespace- and terminal-punctuation-tolerant, and nothing else: a model
    that wrote a paragraph did not answer with the shortest correct answer, and scoring it as
    correct would hide the very instability this suite measures.

    ``1.0`` when the normalized response is one of the accepted answers, ``0.0`` when it is not.
    Never ``None``: an answer that arrived is always scoreable here — there is no case in which
    this scorer cannot tell — so an unscoreable sample in this suite is always a provider failure,
    which the engine records without ever reaching a scorer.
    """

    key: str = "exact_short_answer"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Score one response against its case's accepted answers.

        Args:
            case: The case, whose ``expectation`` carries ``accepted``.
            response_text: Exactly what the model returned.

        Returns:
            ``1.0`` for a match and ``0.0`` otherwise, with the normalized answer in ``detail`` so
            a drill-down shows what was compared rather than only the verdict.
        """
        accepted = tuple(str(item).lower() for item in case.expectation.get("accepted", ()))
        normalized = response_text.strip().lower().rstrip(".!?").strip()
        return ScoreResult(
            score=1.0 if normalized in accepted else 0.0,
            method=self.method,
            detail={
                "case": case.case_id,
                "normalized_answer": normalized[:200],
                "accepted": list(accepted),
            },
        )


_SCORE_METRIC = MetricDefinition(
    key="answer_correct",
    unit="ratio",
    higher_is_better=True,
    aggregation="mean",
    description=(
        "Whether the repetition gave one of the accepted short answers. The headline score; the "
        "reliability figures are about how much it moved, not about how high it is."
    ),
    source="score",
)
"""The one per-sample metric. Declared ``source = "score"`` rather than left to the resolution
order, because every reliability figure is *derived from these scores* and a key that silently
became something else would corrupt all of them at once."""


@dataclass(frozen=True, slots=True)
class ReliabilityTest:
    """One test of ``native.reliability``: questions with one right answer, asked repeatedly.

    Attributes:
        key: Stable test key within the suite.
        name: Human-readable name.
        category: The catalog category the suite contributes to.
        measurement_class: ``warm`` — a cold first ask has a different failure profile, and this
            suite is about steady-state variability.
        metrics: The per-sample metric; the reliability figures come from :func:`derive`.
        cases_spec: ``(case_id, question, accepted answers)`` per case.
        library: The loaded prompt pack the cases render from.
    """

    key: str
    name: str
    category: str
    measurement_class: str
    metrics: Sequence[MetricDefinition]
    cases_spec: tuple[tuple[str, str, tuple[str, ...]], ...]
    library: PromptLibrary

    @property
    def scorer(self) -> ExactShortAnswerScorer:
        """The one scorer every case in this suite uses."""
        return ExactShortAnswerScorer()

    @property
    def streaming(self) -> bool:
        """Never: a one-word answer has nothing to stream, and streaming would add a variable."""
        return False

    @property
    def requires(self) -> Mapping[str, Any]:
        """Nothing. Every provider can answer a question, which is why this suite is the one that
        can measure reliability on a provider that reports no token counts at all."""
        return {"provider_capabilities": [], "sandbox": False, "network": False}

    def cases(self) -> Iterator[BenchmarkCase]:
        """Yield this test's cases in declaration order, each rendered from the prompt record."""
        record = self.library.get(PROMPT_ID)
        for ordinal, (case_id, question, accepted) in enumerate(self.cases_spec):
            rendered = record.render({"question": question})
            yield BenchmarkCase(
                case_id=case_id,
                ordinal=ordinal,
                prompt=rendered.user,
                system_prompt=rendered.system,
                prompt_id=rendered.prompt_id,
                prompt_version=rendered.version,
                required_context_tokens=512,
                expectation={"accepted": list(accepted)},
                metadata={"suite": "native.reliability", "test": self.key},
            )


def _answer_stability(library: PromptLibrary) -> ReliabilityTest:
    """Same question, same answer? The content half of the suite."""
    return ReliabilityTest(
        key="reliability.answer_stability",
        name="Answer stability",
        category="reliability",
        measurement_class=MeasurementClass.WARM.value,
        metrics=(_SCORE_METRIC,),
        cases_spec=_case_spec("reliability.answer_stability"),
        library=library,
    )


def _format_stability(library: PromptLibrary) -> ReliabilityTest:
    """Same question, same *shape* of answer? The half a parser cares about."""
    return ReliabilityTest(
        key="reliability.format_stability",
        name="Format stability",
        category="reliability",
        measurement_class=MeasurementClass.WARM.value,
        metrics=(_SCORE_METRIC,),
        cases_spec=_case_spec("reliability.format_stability"),
        library=library,
    )


def _row(
    key: str,
    statistic: Statistic,
    *,
    unit: str,
    higher_is_better: bool,
    aggregation: str,
) -> AggregatedMetric:
    """Assemble one run-level derived row, carrying the statistic's own counts.

    ``sample_count`` and ``excluded_count`` come straight off the :class:`Statistic`, which is
    Phase 9 acceptance criterion 3 made structural: there is no path here that writes a figure
    without the counts that produced it, because the two arrive together.
    """
    return AggregatedMetric(
        metric_key=key,
        run_test_id=None,
        numeric_value=statistic.numeric_value,
        unavailable_reason=statistic.unavailable_reason,
        unit=unit,
        aggregation=aggregation,
        higher_is_better=higher_is_better,
        sample_count=statistic.sample_count,
        excluded_count=statistic.excluded_count,
        stddev=None,
        coefficient_of_variation=None,
        measurement_class=MeasurementClass.WARM,
    )


def derive(
    cases: Sequence[CaseAttempts],
    *,
    k: int = DEFAULT_PASS_AT_K,
    policy: OutlierPolicy = OutlierPolicy.REPORT_ONLY,
    threshold: float | None = None,
) -> tuple[AggregatedMetric, ...]:
    """Turn one run's stored repetitions into ``native.reliability``'s run-level metrics.

    Args:
        cases: One entry per case, carrying every repetition's score and response hash.
        k: The draw size for ``pass@k``.
        policy: The outlier rule. The default flags and keeps.
        threshold: The policy's cut, or ``None`` for its default.

    Returns:
        The dispersion set, ``pass@1``, ``pass@k``, answer agreement and the count of flagged
        outliers, each with the sample and exclusion counts behind it. A suite with no scored
        attempt yields rows that are ``UNSUPPORTED`` with a reason rather than absent — a missing
        row and a refused computation are indistinguishable otherwise.
    """
    report = summarize_suite(cases, k=k, policy=policy, threshold=threshold)
    pooled = [score for case in cases for score in case.scores]
    spread = summarize(pooled, policy=policy, threshold=threshold)
    flagged = sum(case.summary.outliers.flagged_count for case in report.cases)
    rows: list[AggregatedMetric] = [
        _row("score_mean", spread.mean, unit="ratio", higher_is_better=True, aggregation="mean"),
        _row(
            "score_median", spread.median, unit="ratio", higher_is_better=True, aggregation="median"
        ),
        _row("score_min", spread.minimum, unit="ratio", higher_is_better=True, aggregation="min"),
        _row("score_max", spread.maximum, unit="ratio", higher_is_better=True, aggregation="max"),
        _row(
            "score_stddev", spread.stddev, unit="ratio", higher_is_better=False, aggregation="mean"
        ),
        _row(
            "score_coefficient_of_variation",
            spread.coefficient_of_variation,
            unit="ratio",
            higher_is_better=False,
            aggregation="mean",
        ),
        _row("score_p95", spread.p95, unit="ratio", higher_is_better=True, aggregation="p95"),
        _row("score_p99", spread.p99, unit="ratio", higher_is_better=True, aggregation="p99"),
        _row(
            "pass_at_1",
            report.mean_pass_at_1,
            unit="ratio",
            higher_is_better=True,
            aggregation="mean",
        ),
        _row(
            "pass_at_k",
            report.mean_pass_at_k,
            unit="ratio",
            higher_is_better=True,
            aggregation="mean",
        ),
        _row(
            "answer_agreement",
            report.mean_answer_agreement,
            unit="ratio",
            higher_is_better=True,
            aggregation="mean",
        ),
        _row(
            "outliers_flagged",
            Statistic(
                MetricResult(float(flagged)),
                sample_count=spread.sample_count,
                excluded_count=spread.excluded_count,
            ),
            unit="count",
            higher_is_better=False,
            aggregation="sum",
        ),
    ]
    return tuple(rows)


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
class ReliabilityBenchmark:
    """The ``native.reliability`` suite: its manifest, its prompt pack and its two tests."""

    manifest: BenchmarkManifest
    library: PromptLibrary

    @property
    def tests(self) -> Sequence[ReliabilityTest]:
        """The two tests: answer stability, then format stability."""
        return (_answer_stability(self.library), _format_stability(self.library))


def build(library: PromptLibrary | None = None) -> ReliabilityBenchmark:
    """Build the suite, verifying that the manifest describes the installed prompts.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one.

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` does not match the installed pack.
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
            f"{actual!r}. Rebuild the manifest and bump the suite version — a prompt this suite "
            "uses has changed, which separates its results."
        )
    declared_cases = manifest.dataset_hashes.get(CASES_DATASET_KEY)
    installed_cases = cases_hash()
    if declared_cases != installed_cases:
        raise ValueError(
            f"Benchmark {manifest.key!r} declares "
            f"dataset_hashes[{CASES_DATASET_KEY!r}] = {declared_cases!r}; the installed case file "
            f"hashes to {installed_cases!r}. Rebuild the manifest and bump the suite version — "
            "the questions this suite asks have changed, which separates its results."
        )
    return ReliabilityBenchmark(manifest=manifest, library=pack)
