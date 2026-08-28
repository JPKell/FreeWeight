"""freeweight.domain.statistics — every summary statistic, with the counts that justify it.

Pure domain: stdlib and :mod:`baseaicore` only. Everything here is a function of values the caller
has already read, which is what lets a percentile, a coefficient of variation and a ``pass@k`` be
tested against hand-computed numbers without a database, a provider or a run.

**Three rules, and each of them is the reason a function here exists rather than a one-line
``statistics.mean`` call at the call site.**

1. *An :data:`~baseaicore.UNSUPPORTED` input is excluded and counted, never coerced.* Every
   statistic is returned as a :class:`Statistic`, which carries ``sample_count`` (what the number
   was computed from) beside ``excluded_count`` (what was dropped and why it had to be). A series
   in which *nothing* is supported is ``UNSUPPORTED`` with a reason — not ``0``, which would read
   as a measurement of zero (`ADR-0016 <../../../../docs/adr/0016-unavailable-is-not-zero.md>`_).
2. *Dispersion is not optional.* A throughput figure without its spread cannot be compared against
   another one, so :func:`summarize` computes the whole set — mean, median, min, max, standard
   deviation, coefficient of variation, p50/p95/p99 — in one pass over one series, and every one of
   them reports the same counts. A single observation has **no** spread: ``stddev`` is unavailable
   with :data:`REASON_SINGLE_OBSERVATION` rather than ``0.0``, which would claim perfect
   reproducibility from one reading.
3. *An outlier is never silently discarded* (benchmark catalog §3.13). :func:`flag_outliers`
   **labels**; it does not drop. Dropping happens only where a caller passes the resulting policy
   to :func:`summarize`, and the dropped values are still returned in the report, so the raw data
   keeps them and the exclusion is explicit and reasoned.

``pass@k`` uses the unbiased estimator rather than "did any of the first *k* pass", because the
latter depends on which *k* of *n* attempts you happened to look at and is not reproducible across
two runs of the same suite.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from baseaicore import Measurement, is_supported

from freeweight.domain.metrics import MetricResult, percentile, unavailable

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "DEFAULT_IQR_FACTOR",
    "DEFAULT_MODIFIED_Z_THRESHOLD",
    "REASON_K_EXCEEDS_ATTEMPTS",
    "REASON_NO_ATTEMPTS",
    "REASON_NO_OBSERVATIONS",
    "REASON_SINGLE_OBSERVATION",
    "REASON_ZERO_MEAN",
    "OutlierPolicy",
    "OutlierReport",
    "Series",
    "Statistic",
    "Summary",
    "agreement_rate",
    "coefficient_of_variation",
    "flag_outliers",
    "maximum",
    "mean",
    "median",
    "minimum",
    "pass_at_k",
    "percentile_of",
    "stddev",
    "summarize",
]

REASON_NO_OBSERVATIONS = "no_supported_observations"
"""Every value handed to this statistic was unsupported, so there is nothing to compute from.

The distinction this constant exists to keep: "nothing was measurable" and "the measurement was
zero" are different facts, and only one of them is a number."""

REASON_SINGLE_OBSERVATION = "single_observation"
"""A spread was asked for over one value. One reading has no dispersion, and ``0.0`` would claim
perfect reproducibility from a sample of one."""

REASON_ZERO_MEAN = "zero_mean"
"""A coefficient of variation was asked for where the mean is zero or negative.

CV is a *relative* spread; dividing by zero produces an infinity and dividing by a negative mean
produces a negative ratio that reads as "less variable than perfect"."""

REASON_NO_ATTEMPTS = "no_attempts"
"""A ``pass@k`` was asked for over a case nobody attempted."""

REASON_K_EXCEEDS_ATTEMPTS = "k_exceeds_attempts"
"""``pass@k`` was asked for a ``k`` larger than the number of attempts actually made.

Estimating it anyway would extrapolate past the evidence: ``pass@10`` from three attempts is a
claim about seven samples that were never taken."""

DEFAULT_MODIFIED_Z_THRESHOLD = 3.5
"""The modified z-score above which a value is flagged. Iglewicz and Hoaglin's conventional cut."""

DEFAULT_IQR_FACTOR = 1.5
"""Multiples of the interquartile range beyond the quartiles at which a value is flagged."""

_MAD_TO_SIGMA = 0.6745
"""Scales a median absolute deviation onto the standard-deviation scale for a normal sample."""


class OutlierPolicy(StrEnum):
    """How a series treats values that sit far from the rest of it.

    :attr:`REPORT_ONLY` is the default everywhere, and it is the policy that matches benchmark
    catalog §3.13: outliers are *identified* so a reader can see them, and they still contribute
    to every statistic. The two excluding policies exist because a run contaminated by another
    process's GPU work produces a real outlier that a mean should not carry — but choosing one is
    a deliberate act by a caller, it is recorded in the :class:`OutlierReport`, and the excluded
    values are returned rather than dropped on the floor.
    """

    REPORT_ONLY = "report_only"
    MODIFIED_Z_SCORE = "modified_z_score"
    IQR = "iqr"


@dataclass(frozen=True, slots=True)
class Statistic:
    """One computed statistic, inseparable from the counts that produced it.

    Attributes:
        result: The value, or :data:`~baseaicore.UNSUPPORTED` with the reason there is none.
        sample_count: How many observations the value was computed from.
        excluded_count: How many observations were not — unsupported inputs, and any the caller's
            :class:`OutlierPolicy` removed.

    A statistic that reported a value without its ``sample_count`` would be a number pretending to
    be a fact, which is why the two are one object rather than two returns a caller may forget to
    carry together (Phase 9 acceptance criterion 3).
    """

    result: MetricResult
    sample_count: int
    excluded_count: int

    @property
    def value(self) -> Measurement:
        """The number, or :data:`~baseaicore.UNSUPPORTED`."""
        return self.result.value

    @property
    def numeric_value(self) -> float | None:
        """The value as the plain ``float`` storage takes, or ``None`` when unavailable."""
        return self.result.numeric_value

    @property
    def unavailable_reason(self) -> str | None:
        """Why there is no number, or ``None`` when there is one."""
        return self.result.unavailable_reason


@dataclass(frozen=True, slots=True)
class OutlierReport:
    """Which observations sat far from the rest, under which policy, and at what threshold.

    Attributes:
        policy: The policy that was applied.
        threshold: The cut it applied — the modified z-score, or the IQR factor.
        flagged_positions: Positions in the *supported* values, ascending. Positions rather than
            values so a caller can point at the raw sample that produced each one.
        flagged_values: The values at those positions, in the same order. Preserved deliberately:
            catalog §3.13 requires an exclusion to be explicit, reasoned and kept in the raw data,
            and a report that named a count without the numbers would satisfy none of the three.
        excluded: Whether the flagged values were removed from the statistics or only labelled.
    """

    policy: OutlierPolicy
    threshold: float
    flagged_positions: tuple[int, ...] = ()
    flagged_values: tuple[float, ...] = ()
    excluded: bool = False

    @property
    def flagged_count(self) -> int:
        """How many observations the policy flagged."""
        return len(self.flagged_positions)


@dataclass(frozen=True, slots=True)
class Series:
    """A set of observations split into what can be computed from and what cannot.

    Attributes:
        values: The supported observations, in input order.
        excluded_count: How many inputs were :data:`~baseaicore.UNSUPPORTED`.

    Built through :meth:`of`, never by hand from a filtered list — the split and the count have to
    happen in the same place, or the count drifts from the thing it describes.
    """

    values: tuple[float, ...]
    excluded_count: int

    @classmethod
    def of(cls, measurements: Iterable[Measurement]) -> Series:
        """Split ``measurements`` into supported values and a count of the rest.

        Args:
            measurements: Observations in any order; order is preserved among the supported ones.

        Returns:
            The series. An input of nothing at all yields an empty series with
            ``excluded_count = 0`` — "no observations" and "observations that were all
            unsupported" are both unavailable, and the counts are what distinguish them.
        """
        values: list[float] = []
        excluded = 0
        for measurement in measurements:
            if is_supported(measurement):
                values.append(float(measurement))
            else:
                excluded += 1
        return cls(values=tuple(values), excluded_count=excluded)

    @property
    def sample_count(self) -> int:
        """How many observations survived into the statistics."""
        return len(self.values)

    def without(self, positions: Sequence[int]) -> Series:
        """Return this series with ``positions`` removed and counted as exclusions."""
        dropped = set(positions)
        return Series(
            values=tuple(value for index, value in enumerate(self.values) if index not in dropped),
            excluded_count=self.excluded_count + len(dropped),
        )


def _statistic(series: Series, result: MetricResult) -> Statistic:
    """Attach ``series``' counts to a computed result."""
    return Statistic(
        result=result, sample_count=series.sample_count, excluded_count=series.excluded_count
    )


def _empty(series: Series) -> Statistic:
    """The honest answer for a statistic over a series with nothing supported in it."""
    return Statistic(
        result=unavailable(REASON_NO_OBSERVATIONS),
        sample_count=0,
        excluded_count=series.excluded_count,
    )


def mean(series: Series) -> Statistic:
    """Arithmetic mean of the supported observations."""
    if not series.values:
        return _empty(series)
    return _statistic(series, MetricResult(math.fsum(series.values) / len(series.values)))


def median(series: Series) -> Statistic:
    """Median of the supported observations, interpolating between the middle two."""
    if not series.values:
        return _empty(series)
    return _statistic(series, MetricResult(percentile(series.values, 0.5)))


def minimum(series: Series) -> Statistic:
    """Smallest supported observation."""
    if not series.values:
        return _empty(series)
    return _statistic(series, MetricResult(min(series.values)))


def maximum(series: Series) -> Statistic:
    """Largest supported observation."""
    if not series.values:
        return _empty(series)
    return _statistic(series, MetricResult(max(series.values)))


def percentile_of(series: Series, fraction: float) -> Statistic:
    """Linearly interpolated percentile of the supported observations.

    Args:
        series: The observations.
        fraction: ``0.0`` through ``1.0`` — ``0.5`` for p50, ``0.95`` for p95.

    Returns:
        The percentile with its counts, or an unavailable statistic when nothing is supported.

    Raises:
        ValueError: ``fraction`` is outside ``0.0..1.0``, which is a caller defect rather than a
            missing measurement.
    """
    if not series.values:
        if not 0.0 <= fraction <= 1.0:
            raise ValueError(f"percentile fraction must be within 0.0..1.0; got {fraction!r}.")
        return _empty(series)
    return _statistic(series, MetricResult(percentile(series.values, fraction)))


def stddev(series: Series) -> Statistic:
    """Sample standard deviation (``n - 1``) of the supported observations.

    Sample rather than population, because a suite's repetitions are a sample of the machine's
    behaviour and not the whole of it.

    Returns:
        The spread, ``UNSUPPORTED`` with :data:`REASON_SINGLE_OBSERVATION` for one observation,
        and ``UNSUPPORTED`` with :data:`REASON_NO_OBSERVATIONS` for none.
    """
    if not series.values:
        return _empty(series)
    if len(series.values) < 2:
        return _statistic(series, unavailable(REASON_SINGLE_OBSERVATION))
    average = math.fsum(series.values) / len(series.values)
    variance = math.fsum((value - average) ** 2 for value in series.values) / (
        len(series.values) - 1
    )
    return _statistic(series, MetricResult(math.sqrt(variance)))


def coefficient_of_variation(series: Series) -> Statistic:
    """Standard deviation as a fraction of the mean — spread on a scale-free axis.

    Returns:
        ``stddev / mean``. ``UNSUPPORTED`` with :data:`REASON_ZERO_MEAN` where the mean is zero or
        negative, and with whatever reason :func:`stddev` gave where there is no spread to divide.
    """
    spread = stddev(series)
    if spread.numeric_value is None:
        return spread
    average = math.fsum(series.values) / len(series.values)
    if average <= 0:
        return _statistic(series, unavailable(REASON_ZERO_MEAN))
    return _statistic(series, MetricResult(spread.numeric_value / average))


@dataclass(frozen=True, slots=True)
class Summary:
    """Every dispersion figure benchmark catalog §3.13 asks for, over one series.

    Attributes:
        sample_count: Observations the figures were computed from.
        excluded_count: Observations that were not — unsupported inputs plus any the outlier
            policy removed.
        mean: Arithmetic mean.
        median: Median.
        minimum: Smallest observation.
        maximum: Largest observation.
        stddev: Sample standard deviation.
        coefficient_of_variation: Spread relative to the mean.
        p50: Median, restated under the name the metric catalog uses.
        p95: 95th percentile.
        p99: 99th percentile.
        outliers: What the policy flagged, and whether it removed it.

    Every field is a :class:`Statistic`, so a figure that could not be computed says why rather
    than being absent — a missing key and a refused computation are indistinguishable otherwise.
    """

    sample_count: int
    excluded_count: int
    mean: Statistic
    median: Statistic
    minimum: Statistic
    maximum: Statistic
    stddev: Statistic
    coefficient_of_variation: Statistic
    p50: Statistic
    p95: Statistic
    p99: Statistic
    outliers: OutlierReport


def _modified_z_outliers(values: Sequence[float], threshold: float) -> list[int]:
    """Positions whose modified z-score exceeds ``threshold``.

    Median-based rather than mean-based, because the mean and the standard deviation are dragged
    by the very values this is trying to find; a single contaminated reading can raise the
    standard deviation enough to hide itself.
    """
    if len(values) < 3:
        return []
    centre = percentile(values, 0.5)
    deviations = [abs(value - centre) for value in values]
    mad = percentile(deviations, 0.5)
    if mad <= 0:
        return []
    return [
        index
        for index, value in enumerate(values)
        if abs(_MAD_TO_SIGMA * (value - centre) / mad) > threshold
    ]


def _iqr_outliers(values: Sequence[float], factor: float) -> list[int]:
    """Positions beyond ``factor`` interquartile ranges outside the quartiles."""
    if len(values) < 4:
        return []
    q1 = percentile(values, 0.25)
    q3 = percentile(values, 0.75)
    spread = q3 - q1
    if spread <= 0:
        return []
    low, high = q1 - factor * spread, q3 + factor * spread
    return [index for index, value in enumerate(values) if value < low or value > high]


def flag_outliers(
    series: Series,
    *,
    policy: OutlierPolicy = OutlierPolicy.REPORT_ONLY,
    threshold: float | None = None,
) -> OutlierReport:
    """Identify observations that sit far from the rest of ``series``.

    Args:
        series: The observations.
        policy: Which rule to apply. :attr:`OutlierPolicy.REPORT_ONLY` flags nothing and is the
            default, so a caller gets outlier *reporting* only when it asks for a rule.
        threshold: The cut — a modified z-score, or a multiple of the interquartile range.
            ``None`` takes the policy's documented default.

    Returns:
        The report. ``excluded`` is always ``False`` here: this function labels and never drops.
        :func:`summarize` is the one place a flagged value leaves a statistic, and it records the
        removal in the report it returns.
    """
    if policy is OutlierPolicy.MODIFIED_Z_SCORE:
        cut = DEFAULT_MODIFIED_Z_THRESHOLD if threshold is None else threshold
        positions = _modified_z_outliers(series.values, cut)
    elif policy is OutlierPolicy.IQR:
        cut = DEFAULT_IQR_FACTOR if threshold is None else threshold
        positions = _iqr_outliers(series.values, cut)
    else:
        return OutlierReport(policy=policy, threshold=0.0)
    return OutlierReport(
        policy=policy,
        threshold=cut,
        flagged_positions=tuple(positions),
        flagged_values=tuple(series.values[index] for index in positions),
    )


def summarize(
    measurements: Iterable[Measurement],
    *,
    policy: OutlierPolicy = OutlierPolicy.REPORT_ONLY,
    threshold: float | None = None,
) -> Summary:
    """Compute every dispersion figure for one set of observations, with its counts.

    Args:
        measurements: The observations, :data:`~baseaicore.UNSUPPORTED` included. Unsupported
            values are excluded from every figure and counted in ``excluded_count``.
        policy: The outlier rule. The default flags nothing and removes nothing.
        threshold: The policy's cut, or ``None`` for its default.

    Returns:
        The summary. Where nothing was supported, every figure is ``UNSUPPORTED`` with
        :data:`REASON_NO_OBSERVATIONS` and ``sample_count`` is ``0`` — the whole of ADR-0016's
        "an all-unsupported series is unsupported, not zero", stated once here so no caller has to
        remember it.
    """
    series = Series.of(measurements)
    report = flag_outliers(series, policy=policy, threshold=threshold)
    if report.flagged_positions:
        series = series.without(report.flagged_positions)
        report = OutlierReport(
            policy=report.policy,
            threshold=report.threshold,
            flagged_positions=report.flagged_positions,
            flagged_values=report.flagged_values,
            excluded=True,
        )
    return Summary(
        sample_count=series.sample_count,
        excluded_count=series.excluded_count,
        mean=mean(series),
        median=median(series),
        minimum=minimum(series),
        maximum=maximum(series),
        stddev=stddev(series),
        coefficient_of_variation=coefficient_of_variation(series),
        p50=percentile_of(series, 0.5),
        p95=percentile_of(series, 0.95),
        p99=percentile_of(series, 0.99),
        outliers=report,
    )


def pass_at_k(*, successes: int, attempts: int, k: int) -> Statistic:
    """The unbiased ``pass@k`` estimator over one case's repeated attempts.

    ``pass@k = 1 - C(n - c, k) / C(n, k)`` — the probability that a random draw of ``k`` of the
    ``n`` attempts contains at least one success. The naive alternative ("did any of the first
    ``k`` pass") depends on which attempts you happened to look at first and gives two different
    answers for the same stored samples, which is not a benchmark result.

    Args:
        successes: Attempts that succeeded, ``0 <= successes <= attempts``.
        attempts: Attempts that were actually made and scored.
        k: The draw size.

    Returns:
        The estimate in ``0.0..1.0`` with ``sample_count = attempts``. ``UNSUPPORTED`` with
        :data:`REASON_NO_ATTEMPTS` when nothing was attempted — a case nobody ran did not fail —
        and with :data:`REASON_K_EXCEEDS_ATTEMPTS` when ``k > attempts``, because estimating from
        samples that were never taken is extrapolation, not measurement.

    Raises:
        ValueError: ``k`` is below 1, ``successes`` is negative, or ``successes > attempts``. Each
            is a caller defect that would otherwise produce a plausible number.
    """
    if k < 1:
        raise ValueError(f"pass_at_k needs k >= 1; got {k!r}.")
    if successes < 0 or successes > attempts:
        raise ValueError(
            f"pass_at_k needs 0 <= successes <= attempts; got successes={successes!r}, "
            f"attempts={attempts!r}."
        )
    if attempts <= 0:
        return Statistic(unavailable(REASON_NO_ATTEMPTS), sample_count=0, excluded_count=0)
    if k > attempts:
        return Statistic(
            unavailable(REASON_K_EXCEEDS_ATTEMPTS), sample_count=attempts, excluded_count=0
        )
    failures = attempts - successes
    if failures < k:
        return Statistic(MetricResult(1.0), sample_count=attempts, excluded_count=0)
    probability_none = math.comb(failures, k) / math.comb(attempts, k)
    return Statistic(MetricResult(1.0 - probability_none), sample_count=attempts, excluded_count=0)


def agreement_rate(labels: Sequence[str | None]) -> Statistic:
    """How often two repetitions of the same stochastic case produced the same answer.

    The fraction of unordered pairs of attempts that agree — a plain, unweighted concordance, and
    deliberately *not* a chance-corrected coefficient: the categories here are free-form answers,
    not a fixed ordinal scale, so there is no chance-agreement distribution to correct against.
    Where an ordinal scale exists, :func:`~freeweight.domain.agreement.krippendorff_alpha` is the
    right instrument and this one is not.

    Args:
        labels: One label per attempt — a response hash, a tool-call signature, a judge verdict.
            ``None`` is an attempt that produced no label and is excluded and counted.

    Returns:
        The agreement in ``0.0..1.0`` with ``sample_count`` set to the labelled attempts.
        ``UNSUPPORTED`` with :data:`REASON_SINGLE_OBSERVATION` for fewer than two labelled
        attempts: one answer agrees with nothing, and ``1.0`` would claim perfect determinism from
        a single observation.
    """
    present = [label for label in labels if label is not None]
    excluded = len(labels) - len(present)
    if len(present) < 2:
        reason = REASON_NO_OBSERVATIONS if not present else REASON_SINGLE_OBSERVATION
        return Statistic(unavailable(reason), sample_count=len(present), excluded_count=excluded)
    pairs = 0
    agreeing = 0
    for first in range(len(present)):
        for second in range(first + 1, len(present)):
            pairs += 1
            if present[first] == present[second]:
                agreeing += 1
    return Statistic(
        MetricResult(agreeing / pairs), sample_count=len(present), excluded_count=excluded
    )
