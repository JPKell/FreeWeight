"""Aggregation: what combines, what does not, and what stays visible when it cannot.

Development plan, Phase 6: "Cold and warm samples never combine into one headline metric (asserted
on the aggregation output)." That is :class:`TestColdAndWarmNeverCombine` below, and it asserts on
the *rows aggregation produced* rather than on a flag somewhere — a rule about output has to be
tested against output.

The rest covers the properties every other Phase 6 assertion leans on: a failed sample is excluded
and stays counted (spec §13), an aggregate with nothing to aggregate is ``UNSUPPORTED`` with a
reason rather than ``0`` (ADR-0016), and dispersion travels with the value.
"""

from __future__ import annotations

import pytest

from freeweight.domain.aggregation import (
    REASON_MIXED_CLASSES,
    REASON_NO_SAMPLES,
    REASON_NOT_SCORED,
    REASON_RAW,
    AggregatedMetric,
    SampleGroup,
    aggregate_run,
    aggregate_test,
    is_success,
)
from freeweight.domain.benchmark import MetricDefinition
from freeweight.domain.metrics import MeasurementClass, SampleFacts


def _metric(key: str, aggregation: str = "mean") -> MetricDefinition:
    return MetricDefinition(key=key, unit="ms", higher_is_better=False, aggregation=aggregation)


def _sample(**overrides: object) -> SampleFacts:
    return SampleFacts(**overrides)  # type: ignore[arg-type]


def _row(rows: tuple[AggregatedMetric, ...], key: str, *, run_level: bool) -> AggregatedMetric:
    return next(
        row for row in rows if row.metric_key == key and (row.run_test_id is None) == run_level
    )


class TestColdAndWarmNeverCombine:
    """A run-level metric whose tests disagree about model state has no value at all."""

    @staticmethod
    def _groups() -> list[SampleGroup]:
        metric = _metric("load_ms")
        return [
            SampleGroup(
                test_key="cold",
                run_test_id="rt-cold",
                measurement_class=MeasurementClass.COLD,
                metrics=(metric,),
                samples=[_sample(backend_load_ms=900.0), _sample(backend_load_ms=1100.0)],
            ),
            SampleGroup(
                test_key="warm",
                run_test_id="rt-warm",
                measurement_class=MeasurementClass.WARM,
                metrics=(metric,),
                samples=[_sample(backend_load_ms=4.0), _sample(backend_load_ms=6.0)],
            ),
        ]

    def test_the_run_level_row_carries_no_number(self) -> None:
        rows = aggregate_run(self._groups())
        combined = _row(rows, "load_ms", run_level=True)
        assert combined.numeric_value is None
        assert combined.unavailable_reason == REASON_MIXED_CLASSES

    def test_the_run_level_row_is_not_the_average_of_the_two(self) -> None:
        # The average of 900, 1100, 4 and 6 is 502.5 — a number describing neither a cold load nor
        # a warm one. Asserting its absence explicitly is the point of this test.
        rows = aggregate_run(self._groups())
        assert _row(rows, "load_ms", run_level=True).numeric_value != pytest.approx(502.5)

    def test_each_test_keeps_its_own_number(self) -> None:
        rows = aggregate_run(self._groups())
        by_test = {row.run_test_id: row for row in rows if row.run_test_id is not None}
        assert by_test["rt-cold"].numeric_value == pytest.approx(1000.0)
        assert by_test["rt-warm"].numeric_value == pytest.approx(5.0)
        assert by_test["rt-cold"].measurement_class is MeasurementClass.COLD

    def test_tests_that_agree_do_combine(self) -> None:
        metric = _metric("total_ms")
        groups = [
            SampleGroup(
                test_key="a",
                run_test_id="rt-a",
                measurement_class=MeasurementClass.WARM,
                metrics=(metric,),
                samples=[_sample(client_wall_ms=10.0)],
            ),
            SampleGroup(
                test_key="b",
                run_test_id="rt-b",
                measurement_class=MeasurementClass.WARM,
                metrics=(metric,),
                samples=[_sample(client_wall_ms=20.0)],
            ),
        ]
        combined = _row(aggregate_run(groups), "total_ms", run_level=True)
        assert combined.numeric_value == pytest.approx(15.0)
        assert combined.sample_count == 2


class TestExclusionsStayVisible:
    """A sample that contributed nothing is counted, not forgotten (spec §13)."""

    def test_a_failed_sample_is_excluded_and_counted(self) -> None:
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.WARM,
                metrics=(_metric("total_ms"),),
                samples=[
                    _sample(client_wall_ms=10.0),
                    _sample(status="failed"),
                    _sample(status="timeout"),
                ],
            )
        )
        assert rows[0].numeric_value == pytest.approx(10.0)
        assert rows[0].sample_count == 1
        assert rows[0].excluded_count == 2

    def test_a_completed_sample_missing_this_metric_is_excluded_too(self) -> None:
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.WARM,
                metrics=(_metric("decode_tokens_per_second"),),
                samples=[
                    _sample(output_tokens=100.0, backend_decode_ms=1000.0),
                    _sample(output_tokens=100.0),  # the provider reported no decode time
                ],
            )
        )
        assert rows[0].sample_count == 1
        assert rows[0].excluded_count == 1

    def test_nothing_usable_is_unsupported_with_a_reason_never_zero(self) -> None:
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.WARM,
                metrics=(_metric("total_ms"),),
                samples=[_sample(status="failed")],
            )
        )
        assert rows[0].numeric_value is None
        assert rows[0].unavailable_reason == REASON_NO_SAMPLES

    def test_a_metric_no_sample_produced_still_gets_a_row(self) -> None:
        # A missing row is indistinguishable from a metric nobody thought to compute.
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.WARM,
                metrics=(_metric("ttft_ms"), _metric("total_ms")),
                samples=[],
            )
        )
        assert {row.metric_key for row in rows} == {"ttft_ms", "total_ms"}


class TestScoreDerivedMetrics:
    """A metric key nobody derives from sample facts falls back to the sample's score."""

    def test_score_metrics_aggregate_by_their_declared_aggregation(self) -> None:
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.NOT_APPLICABLE,
                metrics=(_metric("harness_roundtrip_success", "mean"),),
                samples=[_sample(score=1.0), _sample(score=0.0)],
            )
        )
        assert rows[0].numeric_value == pytest.approx(0.5)

    def test_a_completed_but_unscored_sample_is_excluded_with_its_reason(self) -> None:
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.NOT_APPLICABLE,
                metrics=(_metric("some_score", "mean"),),
                samples=[_sample()],
            )
        )
        assert rows[0].sample_count == 0
        assert rows[0].excluded_count == 1
        assert rows[0].unavailable_reason == REASON_NO_SAMPLES

    def test_not_scored_is_the_reason_a_sample_was_dropped(self) -> None:
        # The constant is part of the module's vocabulary and is asserted so a rename cannot
        # silently change what storage records.
        assert REASON_NOT_SCORED == "not_scored"


class TestAggregations:
    """The data model's aggregation vocabulary, including the one that produces no value."""

    @staticmethod
    def _values(aggregation: str) -> float | None:
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.WARM,
                metrics=(_metric("total_ms", aggregation),),
                samples=[_sample(client_wall_ms=value) for value in (10.0, 20.0, 60.0)],
            )
        )
        return rows[0].numeric_value

    @pytest.mark.parametrize(
        ("aggregation", "expected"),
        [
            ("mean", 30.0),
            ("median", 20.0),
            ("p50", 20.0),
            ("min", 10.0),
            ("max", 60.0),
            ("sum", 90.0),
            ("count", 3.0),
            ("ratio", 30.0),
        ],
    )
    def test_known_values(self, aggregation: str, expected: float) -> None:
        assert self._values(aggregation) == pytest.approx(expected)

    def test_raw_has_no_run_level_value(self) -> None:
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.WARM,
                metrics=(_metric("total_ms", "raw"),),
                samples=[_sample(client_wall_ms=10.0)],
            )
        )
        assert rows[0].numeric_value is None
        assert rows[0].unavailable_reason == REASON_RAW

    def test_an_unknown_aggregation_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="Unknown aggregation"):
            aggregate_test(
                SampleGroup(
                    test_key="t",
                    run_test_id="rt",
                    measurement_class=MeasurementClass.WARM,
                    metrics=(_metric("total_ms", "geometric_mean"),),
                    samples=[_sample(client_wall_ms=10.0)],
                )
            )


class TestDispersion:
    """A throughput number without its spread cannot be compared against another one."""

    def test_stddev_and_cov_over_several_samples(self) -> None:
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.WARM,
                metrics=(_metric("total_ms"),),
                samples=[_sample(client_wall_ms=value) for value in (10.0, 12.0, 14.0)],
            )
        )
        assert rows[0].stddev == pytest.approx(2.0)
        assert rows[0].coefficient_of_variation == pytest.approx(2.0 / 12.0)

    def test_one_sample_has_no_spread_rather_than_a_spread_of_zero(self) -> None:
        rows = aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.WARM,
                metrics=(_metric("total_ms"),),
                samples=[_sample(client_wall_ms=10.0)],
            )
        )
        assert rows[0].stddev is None
        assert rows[0].coefficient_of_variation is None


class TestPerSetMetrics:
    """The token-economy figures that exist only over a set of samples."""

    @staticmethod
    def _rows() -> tuple[AggregatedMetric, ...]:
        metrics = (
            MetricDefinition(
                key="output_tokens_per_success",
                unit="tokens",
                higher_is_better=False,
                aggregation="ratio",
            ),
            MetricDefinition(
                key="successes_per_million_output_tokens",
                unit="count/1M tokens",
                higher_is_better=True,
                aggregation="ratio",
            ),
        )
        return aggregate_test(
            SampleGroup(
                test_key="t",
                run_test_id="rt",
                measurement_class=MeasurementClass.WARM,
                metrics=metrics,
                samples=[
                    _sample(output_tokens=100.0, score=1.0),
                    _sample(output_tokens=300.0, score=1.0),
                    _sample(status="failed"),
                ],
            )
        )

    def test_tokens_per_success_divides_by_successes_not_by_samples(self) -> None:
        assert self._rows()[0].numeric_value == pytest.approx(200.0)

    def test_the_failed_sample_stays_in_the_excluded_count(self) -> None:
        assert self._rows()[0].excluded_count == 1

    def test_successes_per_million(self) -> None:
        assert self._rows()[1].numeric_value == pytest.approx(2 / 0.0004)


class TestIsSuccess:
    """What counts as a success, and the two things that deliberately do not."""

    def test_a_completed_scored_sample_is_a_success(self) -> None:
        assert is_success(_sample(status="completed", score=1.0))

    def test_a_completed_sample_scored_zero_is_a_measured_failure(self) -> None:
        assert not is_success(_sample(status="completed", score=0.0))

    def test_an_unscored_sample_is_not_a_cheap_success(self) -> None:
        assert not is_success(_sample(status="failed"))
