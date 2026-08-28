"""The four statistics together, and the judged-criterion scorer that consumes them.

``test_kappa_weighted.py`` and ``test_krippendorff.py`` take the two coefficients apart; this file
asserts what happens when they are *combined* — the median rule, the retained verdicts, the
refusals that are not low grades — and the two properties that hold the whole design together:

* **jurors are combined by median, never by mean**, so one juror misreading a rubric cannot drag
  the score, and the dispersion the median is reported against survives;
* **a refusal is not a grade of one**. A juror that refused contributes nothing and is recorded
  with its reason; a criterion no juror could grade is *skipped*, never scored zero.
"""

from __future__ import annotations

import pytest

from freeweight.domain.agreement import agreement, weighted_mean
from freeweight.domain.goals.criteria import CriterionStatus, SkipReason
from freeweight.domain.goals.pack import Criterion, Rung, ScaleSpec
from freeweight.domain.scorers.judged import (
    JurorVerdict,
    combine_verdicts,
    inter_juror_agreement,
    normalize_grade,
    pairwise_win_rate,
)

_SCALE = ScaleSpec(
    points=5, descriptors={"5": "Wry and understated.", "3": "Flat.", "1": "Earnest."}
)


def _criterion(*, mode: str | None = None, weight: float = 1.0) -> Criterion:
    return Criterion(
        key="wit", name="Dry wit", rung=Rung.JUDGE, weight=weight, scale=_SCALE, mode=mode
    )


def _verdicts(*grades: int | None, refused: str | None = None) -> list[JurorVerdict]:
    return [
        JurorVerdict(
            juror_canonical_id=f"m{index}",
            juror_ordinal=index,
            repetition=1,
            grade=grade,
            refused_reason=refused if grade is None else None,
        )
        for index, grade in enumerate(grades)
    ]


class TestJurorsAreCombinedByMedian:
    def test_one_outlier_does_not_drag_the_score(self) -> None:
        # Mean would be 3.0; median is 4, which is what four of five jurors said.
        result = combine_verdicts(_criterion(), _verdicts(4, 4, 4, 4, 1))
        assert result.median_grade == 4.0  # noqa: PLR2004 — the median is the assertion
        assert result.outcome.raw_score == pytest.approx(0.75)

    def test_an_even_jury_medians_between_the_two_middle_grades(self) -> None:
        result = combine_verdicts(_criterion(), _verdicts(3, 4))
        assert result.median_grade == 3.5  # noqa: PLR2004 — the median is the assertion

    def test_the_grades_and_their_spread_travel_in_the_detail(self) -> None:
        # The dispersion *is* the error bar; folding it into the number would destroy it.
        result = combine_verdicts(_criterion(), _verdicts(2, 4, 5))
        assert result.outcome.detail["grades"] == [2.0, 4.0, 5.0]
        assert result.outcome.detail["grade_spread"] == 3.0  # noqa: PLR2004 — the spread is the point

    def test_every_verdict_is_retained_for_the_judge_verdicts_table(self) -> None:
        verdicts = _verdicts(4, 4, 5)
        assert len(combine_verdicts(_criterion(), verdicts).verdicts) == 3  # noqa: PLR2004


class TestNormalization:
    @pytest.mark.parametrize(("grade", "expected"), [(1, 0.0), (3, 0.5), (5, 1.0), (4.5, 0.875)])
    def test_the_scale_maps_onto_zero_to_one(self, grade: float, expected: float) -> None:
        assert normalize_grade(grade, points=5) == pytest.approx(expected)

    def test_the_bottom_of_the_scale_is_zero_not_a_fifth(self) -> None:
        # A grader who gave the lowest grade meant the lowest score.
        assert normalize_grade(1, points=5) == 0.0

    @pytest.mark.parametrize("grade", [0, 6, -1])
    def test_a_grade_off_the_scale_is_refused(self, grade: float) -> None:
        with pytest.raises(ValueError, match="outside 1..5"):
            normalize_grade(grade, points=5)

    def test_a_one_point_scale_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 2 points"):
            normalize_grade(1, points=1)


class TestARefusalIsNotALowGrade:
    def test_a_refused_juror_contributes_nothing(self) -> None:
        verdicts = [
            JurorVerdict("m0", 0, 1, grade=5),
            JurorVerdict("m1", 1, 1, refused_reason="self_judging"),
        ]
        result = combine_verdicts(_criterion(), verdicts)
        assert result.median_grade == 5.0  # noqa: PLR2004 — the surviving grade
        assert result.outcome.detail["refusals"] == ["self_judging"]
        assert result.outcome.detail["usable_verdicts"] == 1

    def test_a_criterion_no_juror_could_grade_is_skipped_not_zeroed(self) -> None:
        verdicts = [JurorVerdict("m0", 0, 1, refused_reason="provider_error")]
        result = combine_verdicts(_criterion(), verdicts)
        assert result.outcome.status is CriterionStatus.SKIPPED
        assert result.outcome.raw_score is None
        assert result.outcome.skip_reason == SkipReason.JUDGE_UNAVAILABLE.value
        assert result.outcome.detail["error_code"] == "JUDGE_UNAVAILABLE"

    def test_an_empty_jury_is_skipped(self) -> None:
        result = combine_verdicts(_criterion(), ())
        assert result.outcome.status is CriterionStatus.SKIPPED
        assert result.outcome.raw_score is None

    def test_the_refusals_are_named_rather_than_summarized(self) -> None:
        verdicts = [
            JurorVerdict("m0", 0, 1, refused_reason="self_judging"),
            JurorVerdict("m1", 1, 1, refused_reason="protocol_error"),
        ]
        detail = combine_verdicts(_criterion(), verdicts).outcome.detail
        assert detail["refusals"] == ["protocol_error", "self_judging"]


class TestPairwiseMode:
    def test_the_win_rate_counts_ties_as_half(self) -> None:
        verdicts = [
            JurorVerdict("m0", 0, 1, pairwise_choice="candidate"),
            JurorVerdict("m1", 1, 1, pairwise_choice="reference"),
            JurorVerdict("m2", 2, 1, pairwise_choice="tie"),
        ]
        assert pairwise_win_rate(verdicts) == pytest.approx(0.5)

    def test_it_scores_the_criterion_directly(self) -> None:
        verdicts = [
            JurorVerdict("m0", 0, 1, pairwise_choice="candidate"),
            JurorVerdict("m1", 1, 1, pairwise_choice="candidate"),
        ]
        result = combine_verdicts(_criterion(mode="pairwise"), verdicts)
        assert result.outcome.raw_score == 1.0
        assert result.outcome.detail["mode"] == "pairwise"

    def test_no_usable_verdict_is_no_measurement(self) -> None:
        assert pairwise_win_rate([JurorVerdict("m0", 0, 1, refused_reason="timeout")]) is None

    def test_a_pairwise_criterion_with_no_verdicts_is_skipped(self) -> None:
        result = combine_verdicts(_criterion(mode="pairwise"), ())
        assert result.outcome.status is CriterionStatus.SKIPPED


class TestInterJurorAgreement:
    def test_a_single_juror_has_none_rather_than_perfect(self) -> None:
        # With one juror there is no inter-juror agreement, only self-consistency, and a biased
        # juror exhibits that perfectly.
        first = combine_verdicts(_criterion(), _verdicts(4))
        second = combine_verdicts(_criterion(), _verdicts(2))
        assert inter_juror_agreement([first, second]) is None

    def test_jurors_that_agree_everywhere_reach_one(self) -> None:
        results = [
            combine_verdicts(_criterion(), _verdicts(5, 5, 5)),
            combine_verdicts(_criterion(), _verdicts(1, 1, 1)),
            combine_verdicts(_criterion(), _verdicts(3, 3, 3)),
        ]
        assert inter_juror_agreement(results) == pytest.approx(1.0)

    def test_jurors_that_disagree_score_lower(self) -> None:
        agreeing = [
            combine_verdicts(_criterion(), _verdicts(5, 5, 5)),
            combine_verdicts(_criterion(), _verdicts(1, 1, 1)),
        ]
        disagreeing = [
            combine_verdicts(_criterion(), _verdicts(5, 1, 3)),
            combine_verdicts(_criterion(), _verdicts(1, 5, 3)),
        ]
        high = inter_juror_agreement(agreeing)
        low = inter_juror_agreement(disagreeing)
        assert high is not None
        assert low is not None
        assert high > low

    def test_one_sample_is_not_enough_units(self) -> None:
        result = combine_verdicts(_criterion(), _verdicts(4, 4, 5))
        assert result.inter_juror_alpha is None
        assert inter_juror_agreement([result]) is None


class TestRefusalsOfMisuse:
    def test_a_rule_criterion_is_not_this_function_s_business(self) -> None:
        criterion = Criterion(
            key="tells", name="Tells", rung=Rung.RULE, weight=1.0, rule={"type": "x"}
        )
        with pytest.raises(ValueError, match="combine_verdicts is for rung 5"):
            combine_verdicts(criterion, ())

    def test_an_absolute_criterion_with_no_scale_is_refused(self) -> None:
        criterion = Criterion(key="wit", name="Wit", rung=Rung.JUDGE, weight=1.0)
        with pytest.raises(ValueError, match="declares none"):
            combine_verdicts(criterion, _verdicts(4))


class TestWeightingAcrossCriteria:
    def test_an_unmeasured_criterion_is_excluded_rather_than_zeroed(self) -> None:
        assert weighted_mean({"a": 0.8, "b": None}, {"a": 0.5, "b": 0.5}) == pytest.approx(0.8)

    def test_weights_apply(self) -> None:
        assert weighted_mean({"a": 1.0, "b": 0.0}, {"a": 0.75, "b": 0.25}) == pytest.approx(0.75)

    def test_nothing_measurable_is_none(self) -> None:
        assert weighted_mean({"a": None}, {"a": 1.0}) is None

    def test_a_criterion_with_no_weight_is_excluded(self) -> None:
        assert weighted_mean({"a": 0.9, "b": 0.1}, {"a": 1.0, "b": 0.0}) == pytest.approx(0.9)


class TestTheFourStatisticsTogether:
    def test_a_result_carries_all_four_and_the_count(self) -> None:
        result = agreement([1, 2, 3, 4], [2, 3, 4, 5], scale_points=5)
        body = result.as_json()
        assert set(body) == {"kappa_w", "rho", "mae", "bias", "n_holdout", "scale_points"}
        assert body["n_holdout"] == 4  # noqa: PLR2004 — the count is the assertion
