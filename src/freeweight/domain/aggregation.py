"""freeweight.domain.aggregation — samples become metrics, without cold and warm ever mixing.

Pure domain: stdlib, :mod:`baseaicore`, and this package's own
:mod:`~freeweight.domain.metrics` and :mod:`~freeweight.domain.benchmark`. It is handed sample
*facts*, never a session — the run engine reads the ``samples`` table and passes what it read,
which is what keeps "aggregation cannot describe a sample that has not committed" true by
construction rather than by discipline.

**The three rules this module exists to enforce.**

1. *Cold and warm never combine into one headline metric* (benchmark catalog §3.1). Each test
   declares its :class:`~freeweight.domain.metrics.MeasurementClass`; a run-level metric whose
   contributing tests disagree about that class is emitted with **no value** and the reason
   ``cold_and_warm_not_comparable``, beside per-test rows that do have values. ``metric_values``
   has no measurement-class column (data model §2), so the only honest thing a combined row can
   contain is nothing.
2. *A sample that contributed nothing stays visible.* Every row carries ``sample_count`` (what was
   used) and ``excluded_count`` (what was not) — a failed sample, a timeout, or a completed sample
   whose provider reported none of the inputs this particular metric needs (spec §13).
3. *An aggregate with nothing to aggregate is ``UNSUPPORTED`` with a reason, never ``0``*
   (ADR-0016).

Dispersion travels with the value: ``stddev`` and ``coefficient_of_variation`` are computed
wherever there are at least two contributing samples, because a throughput number without its
spread cannot be compared against another one
([Machine Identity §6](../../../../docs/architecture/machine-identity-and-reproducibility.md)).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from baseaicore import UNSUPPORTED, Measurement, is_supported

from freeweight.domain.metrics import (
    AGGREGATE_METRIC_KEYS,
    SAMPLE_METRICS,
    MeasurementClass,
    MetricResult,
    SampleFacts,
    output_tokens_per_success,
    percentile,
    quality_per_1k_output_tokens,
    successes_per_million_output_tokens,
    total_tokens_per_success,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from freeweight.domain.benchmark import MetricDefinition

__all__ = [
    "AggregatedMetric",
    "SampleGroup",
    "aggregate_run",
    "aggregate_test",
    "is_success",
]

REASON_NO_SAMPLES = "no_usable_samples"
"""Every sample either failed or reported none of the inputs this metric needs."""

REASON_MIXED_CLASSES = "cold_and_warm_not_comparable"
"""The contributing tests were not all in one measurement state, so no combined value exists."""

REASON_NOT_SCORED = "not_scored"
"""A completed sample carries no score, so a score-derived metric has nothing to take from it."""

REASON_NOT_MEASURED = "not_measured_for_this_case"
"""This sample's scorer measured the metric for other cases in the test but not for this one.

Phase 7's rates are deliberately *absent* where their denominator is empty — ordering accuracy for
a case that requires one tool call, calls-per-success for a case that failed. The sample is
excluded from that metric and counted in ``excluded_count`` rather than contributing a zero, which
would be a claim about something the run never observed (ADR-0016)."""

REASON_RAW = "raw_metric_not_aggregated"
"""The metric declares ``aggregation = "raw"``: it lives per sample and has no run-level value."""

_COMBINERS = frozenset(
    {"mean", "median", "p50", "p95", "p99", "min", "max", "sum", "count", "ratio", "raw"}
)


@dataclass(frozen=True, slots=True)
class SampleGroup:
    """One benchmark test's stored samples, with what that test declares about them.

    Named for the samples rather than for the test — ``TestSamples`` would be collected as a test
    class by pytest wherever it is imported, and a warning in every run that touches aggregation
    is a warning nobody reads.

    Attributes:
        test_key: The test's key within its suite.
        run_test_id: The ``run_tests`` row these samples belong to, or ``None`` when aggregating
            outside a run (unit tests, and the comparison view at a later phase).
        measurement_class: Whether this test measured a cold model, a warm one, a reused cache, or
            something to which the distinction does not apply.
        metrics: The metric definitions the test declares.
        samples: The stored samples, as facts.
    """

    test_key: str
    run_test_id: str | None
    measurement_class: MeasurementClass
    metrics: Sequence[MetricDefinition]
    samples: Sequence[SampleFacts] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AggregatedMetric:
    """One aggregate ready to be written to ``metric_values``.

    ``numeric_value`` and ``unavailable_reason`` are mutually exclusive, exactly as
    :class:`~freeweight.domain.metrics.MetricResult` requires of the values that produced them.
    """

    metric_key: str
    run_test_id: str | None
    numeric_value: float | None
    unavailable_reason: str | None
    unit: str
    aggregation: str
    higher_is_better: bool
    sample_count: int
    excluded_count: int
    stddev: float | None
    coefficient_of_variation: float | None
    measurement_class: MeasurementClass
    gpu_index: int | None = None


def is_success(facts: SampleFacts) -> bool:
    """Whether one sample counts as a success for the per-success token-economy metrics.

    A success is a sample that *completed* and was scored above zero. A completed sample scored
    ``0.0`` is a real measurement of a real failure to do the task, and counting it as a success
    would make "tokens per success" cheaper the worse a model gets. A sample with no score at all
    — a provider error, a timeout — is neither a success nor a cheap one; it is excluded.
    """
    return facts.status == "completed" and is_supported(facts.score) and float(facts.score) > 0.0


def _combine(values: Sequence[float], aggregation: str) -> MetricResult:
    """Reduce contributing values by the metric's declared aggregation.

    Args:
        values: At least one value. The caller has already decided there is something to combine.
        aggregation: One of the vocabulary ``metric_values.aggregation`` accepts.

    Returns:
        The combined value, or ``UNSUPPORTED`` with :data:`REASON_RAW` for a ``raw`` metric, which
        by definition has no run-level value.

    Raises:
        ValueError: ``aggregation`` is not in the data model's vocabulary. A benchmark that
            declares an aggregation nobody implements must fail loudly at its first run, not
            silently average.
    """
    if aggregation not in _COMBINERS:
        raise ValueError(
            f"Unknown aggregation {aggregation!r}; the data model's vocabulary is "
            f"{sorted(_COMBINERS)}."
        )
    match aggregation:
        case "raw":
            return MetricResult(UNSUPPORTED, REASON_RAW)
        case "mean" | "ratio":
            return MetricResult(sum(values) / len(values))
        case "median" | "p50":
            return MetricResult(percentile(values, 0.5))
        case "p95":
            return MetricResult(percentile(values, 0.95))
        case "p99":
            return MetricResult(percentile(values, 0.99))
        case "min":
            return MetricResult(min(values))
        case "max":
            return MetricResult(max(values))
        case "sum":
            return MetricResult(math.fsum(values))
        case _:  # "count"
            return MetricResult(float(len(values)))


def _dispersion(values: Sequence[float]) -> tuple[float | None, float | None]:
    """Return ``(stddev, coefficient_of_variation)`` for the contributing values.

    Sample standard deviation (``n - 1``), because a run's repetitions are a sample of the
    machine's behaviour rather than its whole population. Both are ``None`` for a single value:
    one measurement has no spread, and reporting ``0.0`` would claim perfect reproducibility from
    one observation.
    """
    if len(values) < 2:
        return None, None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    stddev = math.sqrt(variance)
    return stddev, (stddev / mean if mean > 0 else None)


def _values_for(
    metric: MetricDefinition, samples: Sequence[SampleFacts]
) -> tuple[list[float], int]:
    """Extract this metric's contributing values from ``samples``.

    Three sources, in a fixed order of preference.

    1. A metric key registered in :data:`~freeweight.domain.metrics.SAMPLE_METRICS` is derived
       from each sample's own facts — the counts and timings the provider reported.
    2. Otherwise, if **any** completed sample in the group carries a number under that key in its
       scorer detail, the metric is *detail-derived*: each sample contributes the number its
       scorer measured, and a sample that carries none is excluded with
       :data:`REASON_NOT_MEASURED`. This is how a suite whose scorer measures several things at
       once — Phase 7's tool and instruction-following suites — reports each of them as its own
       metric instead of reporting the headline score under a dozen different names.
    3. Otherwise the metric is score-derived, which is what keeps a suite whose one metric *is*
       the score (``native.echo``) working through the same path.

    The group decides whether a key is detail-derived, not the individual sample: a sample missing
    the key would otherwise silently fall through to the headline score, which is a different
    number wearing this metric's name.

    Returns:
        ``(values, excluded)`` — what contributed, and how many samples did not.
    """
    derive = SAMPLE_METRICS.get(metric.key)
    from_detail = derive is None and any(
        _detail_number(facts, metric.key) is not None
        for facts in samples
        if facts.status == "completed"
    )
    values: list[float] = []
    excluded = 0
    for facts in samples:
        if facts.status != "completed":
            excluded += 1
            continue
        if derive is not None:
            result = derive(facts)
        elif from_detail:
            measured = _detail_number(facts, metric.key)
            result = (
                MetricResult(measured)
                if measured is not None
                else MetricResult(UNSUPPORTED, REASON_NOT_MEASURED)
            )
        elif is_supported(facts.score):
            result = MetricResult(facts.score)
        else:
            result = MetricResult(UNSUPPORTED, REASON_NOT_SCORED)
        numeric = result.numeric_value
        if numeric is None:
            excluded += 1
        else:
            values.append(numeric)
    return values, excluded


def _detail_number(facts: SampleFacts, key: str) -> float | None:
    """Return the number one sample's scorer recorded under ``key``, or ``None``.

    ``bool`` is refused explicitly: Python makes ``True`` a ``float``-compatible ``int``, and a
    scorer that recorded a flag would otherwise be averaged as though it were a rate.
    """
    value = facts.detail.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _aggregate_only(
    metric: MetricDefinition, samples: Sequence[SampleFacts]
) -> tuple[MetricResult, int, int]:
    """Compute one of benchmark catalog §3.3's per-*set* metrics over ``samples``.

    Returns:
        ``(result, sample_count, excluded_count)``. ``sample_count`` is the number of completed
        samples the totals were taken over, not the number of successes — the successes are the
        denominator and are visible in the value itself.
    """
    completed = [facts for facts in samples if facts.status == "completed"]
    excluded = len(samples) - len(completed)
    successes = sum(1 for facts in completed if is_success(facts))
    output_total = _total(completed, "output_tokens")
    input_total = _total(completed, "input_tokens")
    scores = [float(facts.score) for facts in completed if is_supported(facts.score)]
    mean_score = sum(scores) / len(scores) if scores else UNSUPPORTED
    match metric.key:
        case "output_tokens_per_success":
            result = output_tokens_per_success(output_total, successes)
        case "total_tokens_per_success":
            result = total_tokens_per_success(input_total, output_total, successes)
        case "quality_per_1k_output_tokens":
            result = quality_per_1k_output_tokens(mean_score, output_total)
        case _:  # "successes_per_million_output_tokens"
            result = successes_per_million_output_tokens(successes, output_total)
    return result, len(completed), excluded


def _total(samples: Sequence[SampleFacts], field_name: str) -> Measurement:
    """Sum one reported quantity across samples, or ``UNSUPPORTED`` if none reported it.

    A partial report sums what was reported: a provider that counted nine of ten calls has told
    the truth about nine, and treating the tenth as zero is the fabrication ADR-0016 forbids — so
    the excluded sample is counted in ``excluded_count`` instead.
    """
    reported = [
        float(getattr(facts, field_name))
        for facts in samples
        if is_supported(getattr(facts, field_name))
    ]
    return math.fsum(reported) if reported else UNSUPPORTED


def _row(
    metric: MetricDefinition,
    result: MetricResult,
    *,
    run_test_id: str | None,
    measurement_class: MeasurementClass,
    sample_count: int,
    excluded_count: int,
    values: Sequence[float] = (),
) -> AggregatedMetric:
    """Assemble one output row from a computed result and its counts."""
    stddev, cov = _dispersion(values)
    return AggregatedMetric(
        metric_key=metric.key,
        run_test_id=run_test_id,
        numeric_value=result.numeric_value,
        unavailable_reason=result.unavailable_reason,
        unit=metric.unit,
        aggregation=metric.aggregation,
        higher_is_better=metric.higher_is_better,
        sample_count=sample_count,
        excluded_count=excluded_count,
        stddev=stddev,
        coefficient_of_variation=cov,
        measurement_class=measurement_class,
    )


def aggregate_test(group: SampleGroup) -> tuple[AggregatedMetric, ...]:
    """Aggregate one test's samples into one row per metric it declares.

    Args:
        group: The test, its declared metrics and its stored samples.

    Returns:
        One row per declared metric, in declaration order. A metric no sample could produce is
        present with ``numeric_value = None`` and a reason — never absent, because a missing row
        is indistinguishable from a metric nobody thought to compute.
    """
    rows: list[AggregatedMetric] = []
    for metric in group.metrics:
        if metric.key in AGGREGATE_METRIC_KEYS:
            result, sample_count, excluded = _aggregate_only(metric, group.samples)
            rows.append(
                _row(
                    metric,
                    result,
                    run_test_id=group.run_test_id,
                    measurement_class=group.measurement_class,
                    sample_count=sample_count,
                    excluded_count=excluded,
                )
            )
            continue
        values, excluded = _values_for(metric, group.samples)
        result = (
            _combine(values, metric.aggregation)
            if values
            else MetricResult(UNSUPPORTED, REASON_NO_SAMPLES)
        )
        rows.append(
            _row(
                metric,
                result,
                run_test_id=group.run_test_id,
                measurement_class=group.measurement_class,
                sample_count=len(values),
                excluded_count=excluded,
                values=values,
            )
        )
    return tuple(rows)


def aggregate_run(groups: Sequence[SampleGroup]) -> tuple[AggregatedMetric, ...]:
    """Aggregate a whole run: every test's rows, then one run-level row per distinct metric key.

    The run-level row combines every test that declares that key — **unless those tests were not
    all in the same measurement state**, in which case the row is emitted with no value and the
    reason ``cold_and_warm_not_comparable``. That is the rule benchmark catalog §3.1 states as
    "cold and warm results never mixed", made structural: there is no code path that averages a
    cold ``load_ms`` into a warm one, because the classes are compared before the values are
    touched.

    Args:
        groups: One entry per test in the run, in declaration order.

    Returns:
        Every per-test row followed by every run-level row (``run_test_id`` is ``None`` on the
        latter), ordered by metric key so two runs' rows line up.
    """
    rows: list[AggregatedMetric] = []
    for group in groups:
        rows.extend(aggregate_test(group))

    by_key: dict[str, list[SampleGroup]] = {}
    definitions: dict[str, MetricDefinition] = {}
    for group in groups:
        for metric in group.metrics:
            by_key.setdefault(metric.key, []).append(group)
            definitions.setdefault(metric.key, metric)

    for key in sorted(by_key):
        metric = definitions[key]
        contributors = by_key[key]
        classes = {group.measurement_class for group in contributors}
        samples = [facts for group in contributors for facts in group.samples]
        if len(classes) > 1:
            excluded = sum(1 for facts in samples if facts.status != "completed")
            rows.append(
                _row(
                    metric,
                    MetricResult(UNSUPPORTED, REASON_MIXED_CLASSES),
                    run_test_id=None,
                    measurement_class=MeasurementClass.NOT_APPLICABLE,
                    sample_count=len(samples) - excluded,
                    excluded_count=excluded,
                )
            )
            continue
        merged = SampleGroup(
            test_key="",
            run_test_id=None,
            measurement_class=next(iter(classes)),
            metrics=(metric,),
            samples=samples,
        )
        rows.extend(aggregate_test(merged))
    return tuple(rows)
