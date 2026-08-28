"""freeweight.benchmarks.reliability.reliability — how much the same question moves between asks.

Benchmark catalog §3.13, as pure functions over one case's repeated attempts. The arithmetic is
:mod:`freeweight.domain.statistics`'; this module decides *what to compute it over* — which is the
whole of the difference between "a model scores 0.8" and "a model scores 0.8 with a coefficient of
variation of 0.4, and gave three different answers to the same question".

**Every repetition is kept.** The run engine stores all of them (catalog §3.13's "stores **all**
repetitions"), and nothing here reduces a case to its best attempt. ``pass@k`` is the unbiased
estimator over every attempt, not "did the best one pass".

**An outlier is flagged, and only excluded when a caller says so.** The default
:attr:`~freeweight.domain.statistics.OutlierPolicy.REPORT_ONLY` computes every figure over every
attempt and reports which of them sat far out, so a reader sees the contamination *and* the number
it affected. Where a caller chooses to exclude, :class:`CaseReliability` carries the report — the
policy, the threshold, the positions and the values — so the exclusion is explicit, reasoned and
preserved in the raw data rather than being a smaller number nobody can account for.

**Agreement is answer agreement, not score agreement.** Two attempts that both score ``1.0`` by
giving different correct answers are *reliable in score* and *unreliable in output*, and a suite
that reported only the first would miss the property a caching or routing decision depends on. The
labels are response hashes — the same hash the sample already stores — so no response text has to
be retained to compute it (spec §14).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from baseaicore import is_supported

from freeweight.domain.metrics import MetricResult, unavailable
from freeweight.domain.statistics import (
    OutlierPolicy,
    Statistic,
    Summary,
    agreement_rate,
    pass_at_k,
    summarize,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from baseaicore import Measurement

__all__ = [
    "DEFAULT_PASS_AT_K",
    "REASON_NO_CASES",
    "SUCCESS_THRESHOLD",
    "CaseAttempts",
    "CaseReliability",
    "SuiteReliability",
    "reliability_for_case",
    "summarize_suite",
]

SUCCESS_THRESHOLD = 0.0
"""A scored attempt counts as a success when its score is strictly above this.

The same rule as :func:`freeweight.domain.aggregation.is_success`, restated here rather than
imported so the two cannot drift apart silently: a completed attempt scored ``0.0`` is a real
measurement of a real failure, and counting it as a success would make ``pass@k`` rise as a model
gets worse."""

DEFAULT_PASS_AT_K = 3
"""The ``k`` this suite reports beside ``pass@1``.

Three, because it is the smallest ``k`` at which the unbiased estimator differs usefully from
``pass@1`` at the repetition counts a local benchmark run actually uses, and because a ``k`` larger
than the run's repetitions is refused rather than extrapolated."""

REASON_NO_CASES = "no_cases_with_attempts"
"""A suite-level reliability figure was asked for and no case recorded a scored attempt."""


@dataclass(frozen=True, slots=True)
class CaseAttempts:
    """One case's repeated attempts, as they were stored.

    Attributes:
        case_id: The case these attempts belong to.
        scores: One entry per attempt — the score, or :data:`~baseaicore.UNSUPPORTED` for an
            attempt that could not be scored. An unscoreable attempt is excluded from every figure
            and counted; it is neither a pass nor a fail.
        answer_labels: One entry per attempt — the response hash, or ``None`` where none was
            recorded. Used only for agreement.
    """

    case_id: str
    scores: tuple[Measurement, ...] = ()
    answer_labels: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True)
class CaseReliability:
    """Everything catalog §3.13 reports for one case.

    Attributes:
        case_id: The case.
        summary: Mean, median, min, max, standard deviation, coefficient of variation and
            p50/p95/p99 over the scored attempts, each with its own counts.
        pass_at_1: The unbiased ``pass@1`` — which for one draw is simply the success rate, and is
            computed through the same estimator so the two can never disagree.
        pass_at_k: The unbiased ``pass@k`` at :data:`DEFAULT_PASS_AT_K`, or the ``k`` the caller
            asked for. ``UNSUPPORTED`` where ``k`` exceeds the attempts actually made.
        answer_agreement: How often two attempts produced byte-identical answers.
        attempts: Attempts recorded for this case, scoreable or not.
        successes: Scored attempts above :data:`SUCCESS_THRESHOLD`.
        k: The ``k`` ``pass_at_k`` was computed at, so the figure can be read without guessing.
    """

    case_id: str
    summary: Summary
    pass_at_1: Statistic
    pass_at_k: Statistic
    answer_agreement: Statistic
    attempts: int
    successes: int
    k: int


@dataclass(frozen=True, slots=True)
class SuiteReliability:
    """The suite-level view: one figure per property, over every case's per-case figure.

    Attributes:
        cases: The per-case reports, in the order they were given.
        mean_pass_at_1: Mean ``pass@1`` across cases that produced one.
        mean_pass_at_k: Mean ``pass@k`` across cases that produced one.
        mean_score_cv: Mean coefficient of variation across cases — the headline "how much does
            this model move" figure.
        mean_answer_agreement: Mean answer agreement across cases.
        k: The ``k`` every per-case ``pass@k`` used.

    Averaging *per-case* figures rather than pooling every attempt is deliberate: pooling would let
    a case with twenty repetitions outvote nineteen cases with one each, and the resulting number
    would describe the repetition schedule as much as the model.
    """

    cases: tuple[CaseReliability, ...] = ()
    mean_pass_at_1: Statistic = field(
        default_factory=lambda: Statistic(unavailable(REASON_NO_CASES), 0, 0)
    )
    mean_pass_at_k: Statistic = field(
        default_factory=lambda: Statistic(unavailable(REASON_NO_CASES), 0, 0)
    )
    mean_score_cv: Statistic = field(
        default_factory=lambda: Statistic(unavailable(REASON_NO_CASES), 0, 0)
    )
    mean_answer_agreement: Statistic = field(
        default_factory=lambda: Statistic(unavailable(REASON_NO_CASES), 0, 0)
    )
    k: int = DEFAULT_PASS_AT_K


def reliability_for_case(
    attempts: CaseAttempts,
    *,
    k: int = DEFAULT_PASS_AT_K,
    policy: OutlierPolicy = OutlierPolicy.REPORT_ONLY,
    threshold: float | None = None,
) -> CaseReliability:
    """Compute one case's dispersion, ``pass@k`` and answer agreement.

    Args:
        attempts: The case's stored repetitions.
        k: The draw size for ``pass@k``.
        policy: The outlier rule applied to the score dispersion. The default flags and keeps.
        threshold: The policy's cut, or ``None`` for its default.

    Returns:
        The report. Every figure carries the counts it was computed from, and an attempt that
        could not be scored is excluded from all of them and visible in ``excluded_count``.
    """
    summary = summarize(attempts.scores, policy=policy, threshold=threshold)
    scored = [score for score in attempts.scores if is_supported(score)]
    successes = sum(1 for score in scored if float(score) > SUCCESS_THRESHOLD)
    return CaseReliability(
        case_id=attempts.case_id,
        summary=summary,
        pass_at_1=pass_at_k(successes=successes, attempts=len(scored), k=1),
        pass_at_k=pass_at_k(successes=successes, attempts=len(scored), k=k),
        answer_agreement=agreement_rate(attempts.answer_labels),
        attempts=len(attempts.scores),
        successes=successes,
        k=k,
    )


def _mean_of(values: Sequence[Statistic], *, excluded: int) -> Statistic:
    """Average a set of per-case statistics, excluding and counting the unavailable ones."""
    present = [value.numeric_value for value in values if value.numeric_value is not None]
    missing = excluded + len(values) - len(present)
    if not present:
        return Statistic(unavailable(REASON_NO_CASES), sample_count=0, excluded_count=missing)
    return Statistic(
        MetricResult(sum(present) / len(present)),
        sample_count=len(present),
        excluded_count=missing,
    )


def summarize_suite(
    cases: Sequence[CaseAttempts],
    *,
    k: int = DEFAULT_PASS_AT_K,
    policy: OutlierPolicy = OutlierPolicy.REPORT_ONLY,
    threshold: float | None = None,
) -> SuiteReliability:
    """Compute the reliability report for a whole suite of repeated cases.

    Args:
        cases: One entry per case, each with its own repetitions.
        k: The draw size for ``pass@k``.
        policy: The outlier rule.
        threshold: The policy's cut, or ``None`` for its default.

    Returns:
        The report. Every suite-level figure is the mean of the per-case figures that exist, with
        the cases that produced none counted as exclusions rather than as zeroes — a case nobody
        could score did not score zero.
    """
    reports = tuple(
        reliability_for_case(case, k=k, policy=policy, threshold=threshold) for case in cases
    )
    return SuiteReliability(
        cases=reports,
        mean_pass_at_1=_mean_of([report.pass_at_1 for report in reports], excluded=0),
        mean_pass_at_k=_mean_of([report.pass_at_k for report in reports], excluded=0),
        mean_score_cv=_mean_of(
            [report.summary.coefficient_of_variation for report in reports], excluded=0
        ),
        mean_answer_agreement=_mean_of([report.answer_agreement for report in reports], excluded=0),
        k=k,
    )
