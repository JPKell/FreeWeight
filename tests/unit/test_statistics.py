"""Statistics: what was used, what was excluded, and why an all-unsupported series is not zero.

Development plan, Phase 9: "Statistics: mean/median/stddev/CV/percentiles with ``UNSUPPORTED``
inputs excluded and counted; an all-unsupported series is ``unsupported``, not zero." Those are
:class:`TestUnsupportedInputsAreExcludedAndCounted` and
:class:`TestAnAllUnsupportedSeriesIsUnsupported`.

Acceptance criterion 3 — "every statistic reports the sample count it used and how many were
excluded" — is asserted on every figure rather than on one, because the criterion is about the
type, not about a single call.
"""

from __future__ import annotations

import pytest
from baseaicore import UNSUPPORTED

from freeweight.domain.statistics import (
    REASON_K_EXCEEDS_ATTEMPTS,
    REASON_NO_ATTEMPTS,
    REASON_NO_OBSERVATIONS,
    REASON_SINGLE_OBSERVATION,
    REASON_ZERO_MEAN,
    OutlierPolicy,
    Series,
    agreement_rate,
    coefficient_of_variation,
    flag_outliers,
    maximum,
    mean,
    median,
    minimum,
    pass_at_k,
    percentile_of,
    stddev,
    summarize,
)


class TestHandComputedValues:
    """The plain arithmetic, so a later refactor cannot quietly change what a mean is."""

    @staticmethod
    def _series() -> Series:
        return Series.of([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])

    def test_mean(self) -> None:
        # (2 + 4 + 4 + 4 + 5 + 5 + 7 + 9) / 8 = 5
        assert mean(self._series()).numeric_value == pytest.approx(5.0)

    def test_median_interpolates_between_the_middle_two(self) -> None:
        # Sorted: 2 4 4 4 | 5 5 7 9 — the middle two are 4 and 5.
        assert median(self._series()).numeric_value == pytest.approx(4.5)

    def test_min_and_max(self) -> None:
        assert minimum(self._series()).numeric_value == 2.0
        assert maximum(self._series()).numeric_value == 9.0

    def test_sample_standard_deviation_uses_n_minus_one(self) -> None:
        # Squared deviations from 5: 9 1 1 1 0 0 4 16 = 32; 32 / (8 - 1) = 4.571428…
        assert stddev(self._series()).numeric_value == pytest.approx((32.0 / 7.0) ** 0.5)

    def test_coefficient_of_variation_is_spread_over_mean(self) -> None:
        assert coefficient_of_variation(self._series()).numeric_value == pytest.approx(
            ((32.0 / 7.0) ** 0.5) / 5.0
        )

    def test_percentiles(self) -> None:
        series = Series.of([1.0, 2.0, 3.0, 4.0, 5.0])
        assert percentile_of(series, 0.0).numeric_value == 1.0
        assert percentile_of(series, 0.5).numeric_value == 3.0
        assert percentile_of(series, 1.0).numeric_value == 5.0
        # Halfway between the 4th and 5th of five values.
        assert percentile_of(series, 0.875).numeric_value == pytest.approx(4.5)

    def test_a_percentile_fraction_outside_the_range_is_a_caller_defect(self) -> None:
        with pytest.raises(ValueError, match="0.0..1.0"):
            percentile_of(Series.of([]), 1.5)


class TestUnsupportedInputsAreExcludedAndCounted:
    """An unsupported reading is dropped from the arithmetic and visible in the counts."""

    @staticmethod
    def _mixed() -> list[object]:
        return [10.0, UNSUPPORTED, 20.0, UNSUPPORTED, 30.0]

    def test_the_mean_is_over_the_supported_values_only(self) -> None:
        statistic = mean(Series.of(self._mixed()))  # type: ignore[arg-type]
        assert statistic.numeric_value == pytest.approx(20.0)
        assert statistic.sample_count == 3
        assert statistic.excluded_count == 2

    def test_an_unsupported_value_is_never_treated_as_zero(self) -> None:
        # A zero would drag the mean to 12; the point of the exclusion is that it does not.
        assert mean(Series.of(self._mixed())).numeric_value != pytest.approx(  # type: ignore[arg-type]
            60.0 / 5
        )

    @pytest.mark.parametrize(
        "figure",
        [
            "mean",
            "median",
            "minimum",
            "maximum",
            "stddev",
            "coefficient_of_variation",
            "p50",
            "p95",
            "p99",
        ],
    )
    def test_every_figure_reports_both_counts(self, figure: str) -> None:
        summary = summarize(self._mixed())  # type: ignore[arg-type]
        statistic = getattr(summary, figure)
        assert statistic.sample_count == 3
        assert statistic.excluded_count == 2

    def test_the_summary_reports_the_counts_once_as_well(self) -> None:
        summary = summarize(self._mixed())  # type: ignore[arg-type]
        assert summary.sample_count == 3
        assert summary.excluded_count == 2


class TestAnAllUnsupportedSeriesIsUnsupported:
    """Nothing measurable is not a measurement of zero (ADR-0016)."""

    @pytest.mark.parametrize(
        "figure",
        [
            "mean",
            "median",
            "minimum",
            "maximum",
            "stddev",
            "coefficient_of_variation",
            "p50",
            "p95",
            "p99",
        ],
    )
    def test_every_figure_refuses_with_a_reason(self, figure: str) -> None:
        summary = summarize([UNSUPPORTED, UNSUPPORTED, UNSUPPORTED])
        statistic = getattr(summary, figure)
        assert statistic.numeric_value is None
        assert statistic.unavailable_reason == REASON_NO_OBSERVATIONS
        assert statistic.sample_count == 0
        assert statistic.excluded_count == 3

    def test_an_empty_series_is_also_unsupported(self) -> None:
        assert mean(Series.of([])).unavailable_reason == REASON_NO_OBSERVATIONS


class TestDispersionOfOneObservation:
    """One reading has no spread, and ``0.0`` would claim perfect reproducibility from it."""

    def test_stddev_of_one_value(self) -> None:
        statistic = stddev(Series.of([42.0]))
        assert statistic.numeric_value is None
        assert statistic.unavailable_reason == REASON_SINGLE_OBSERVATION
        assert statistic.sample_count == 1

    def test_coefficient_of_variation_of_one_value(self) -> None:
        assert coefficient_of_variation(Series.of([42.0])).unavailable_reason == (
            REASON_SINGLE_OBSERVATION
        )

    def test_a_zero_mean_has_no_relative_spread(self) -> None:
        assert coefficient_of_variation(Series.of([-1.0, 1.0])).unavailable_reason == (
            REASON_ZERO_MEAN
        )


class TestOutliersAreNeverSilentlyDiscarded:
    """Benchmark catalog §3.13: an exclusion is explicit, reasoned and kept in the raw data."""

    @staticmethod
    def _contaminated() -> list[float]:
        return [100.0, 101.0, 99.0, 100.5, 100.2, 5000.0]

    def test_the_default_policy_flags_nothing_and_removes_nothing(self) -> None:
        summary = summarize(self._contaminated())
        assert summary.outliers.flagged_count == 0
        assert summary.outliers.excluded is False
        assert summary.sample_count == 6

    def test_a_named_policy_flags_the_contaminated_reading(self) -> None:
        report = flag_outliers(
            Series.of(self._contaminated()), policy=OutlierPolicy.MODIFIED_Z_SCORE
        )
        assert report.flagged_values == (5000.0,)
        # flag_outliers labels; it never drops.
        assert report.excluded is False

    def test_excluding_keeps_the_values_it_removed(self) -> None:
        summary = summarize(self._contaminated(), policy=OutlierPolicy.MODIFIED_Z_SCORE)
        assert summary.outliers.excluded is True
        assert summary.outliers.flagged_values == (5000.0,)
        assert summary.sample_count == 5
        assert summary.excluded_count == 1
        assert summary.mean.numeric_value == pytest.approx(100.14)

    def test_a_clean_series_flags_nothing_under_a_policy(self) -> None:
        report = flag_outliers(Series.of([10.0, 10.1, 9.9, 10.05]), policy=OutlierPolicy.IQR)
        assert report.flagged_count == 0


class TestPassAtK:
    """The unbiased estimator, and the two things it refuses to extrapolate."""

    def test_all_attempts_passing_is_one(self) -> None:
        assert pass_at_k(successes=5, attempts=5, k=3).numeric_value == 1.0

    def test_no_attempt_passing_is_zero(self) -> None:
        # Zero here is a measurement: five attempts were made and none passed.
        assert pass_at_k(successes=0, attempts=5, k=3).numeric_value == 0.0

    def test_pass_at_one_is_the_success_rate(self) -> None:
        assert pass_at_k(successes=2, attempts=5, k=1).numeric_value == pytest.approx(0.4)

    def test_hand_computed_estimate(self) -> None:
        # n = 5, c = 2, k = 2. 1 - C(3,2)/C(5,2) = 1 - 3/10 = 0.7
        assert pass_at_k(successes=2, attempts=5, k=2).numeric_value == pytest.approx(0.7)

    def test_it_is_not_did_any_of_the_first_k_pass(self) -> None:
        # The naive reading of "2 of 5 passed, draw 2" is 1.0 if the two you look at happen to be
        # the passing ones. The estimator must not depend on which two you looked at.
        assert pass_at_k(successes=2, attempts=5, k=2).numeric_value != 1.0

    def test_no_attempts_is_unsupported_not_zero(self) -> None:
        statistic = pass_at_k(successes=0, attempts=0, k=1)
        assert statistic.numeric_value is None
        assert statistic.unavailable_reason == REASON_NO_ATTEMPTS

    def test_k_beyond_the_attempts_is_refused_rather_than_extrapolated(self) -> None:
        statistic = pass_at_k(successes=1, attempts=3, k=10)
        assert statistic.numeric_value is None
        assert statistic.unavailable_reason == REASON_K_EXCEEDS_ATTEMPTS
        assert statistic.sample_count == 3

    @pytest.mark.parametrize(("successes", "attempts", "k"), [(0, 5, 0), (-1, 5, 1), (6, 5, 1)])
    def test_impossible_arguments_are_caller_defects(
        self, successes: int, attempts: int, k: int
    ) -> None:
        with pytest.raises(ValueError, match="pass_at_k"):
            pass_at_k(successes=successes, attempts=attempts, k=k)


class TestAgreementRate:
    """Pairwise concordance over repeated answers, with its counts."""

    def test_identical_answers_agree_completely(self) -> None:
        assert agreement_rate(["a", "a", "a"]).numeric_value == 1.0

    def test_all_different_answers_agree_not_at_all(self) -> None:
        assert agreement_rate(["a", "b", "c"]).numeric_value == 0.0

    def test_hand_computed_partial_agreement(self) -> None:
        # Four attempts, three pairs agree of six: (a,a), (a,a), (a,a) among the three a's.
        assert agreement_rate(["a", "a", "a", "b"]).numeric_value == pytest.approx(3 / 6)

    def test_unlabelled_attempts_are_excluded_and_counted(self) -> None:
        statistic = agreement_rate(["a", None, "a"])
        assert statistic.numeric_value == 1.0
        assert statistic.sample_count == 2
        assert statistic.excluded_count == 1

    def test_one_answer_agrees_with_nothing(self) -> None:
        statistic = agreement_rate(["a"])
        assert statistic.numeric_value is None
        assert statistic.unavailable_reason == REASON_SINGLE_OBSERVATION

    def test_no_labels_at_all(self) -> None:
        assert agreement_rate([None, None]).unavailable_reason == REASON_NO_OBSERVATIONS


class TestReliabilityComposition:
    """The reliability report is these statistics applied per case, with the same guarantees."""

    def test_per_case_report_carries_every_count(self) -> None:
        from freeweight.benchmarks.reliability.reliability import (
            CaseAttempts,
            reliability_for_case,
        )

        report = reliability_for_case(
            CaseAttempts(
                case_id="arith-sum",
                scores=(1.0, 1.0, 0.0, UNSUPPORTED),
                answer_labels=("h1", "h1", "h2", None),
            )
        )
        assert report.attempts == 4
        assert report.successes == 2
        assert report.summary.sample_count == 3
        assert report.summary.excluded_count == 1
        # n = 3, c = 2, k = 3 -> every draw of three contains a success.
        assert report.pass_at_k.numeric_value == 1.0
        assert report.pass_at_1.numeric_value == pytest.approx(2 / 3)
        assert report.answer_agreement.numeric_value == pytest.approx(1 / 3)

    def test_a_suite_with_nothing_scored_is_unsupported_not_zero(self) -> None:
        from freeweight.benchmarks.reliability.reliability import CaseAttempts, summarize_suite

        report = summarize_suite(
            [CaseAttempts(case_id="c", scores=(UNSUPPORTED,), answer_labels=(None,))]
        )
        assert report.mean_pass_at_1.numeric_value is None
        assert report.mean_score_cv.numeric_value is None
