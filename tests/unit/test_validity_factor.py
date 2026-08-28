"""``judge_validity_factor``: 1.0 for deterministic work, shrunk at small n, clamped at both ends.

The phase's own test list, clause by clause:

* **1.0 when every criterion is rungs 1–4** — no existing measurement changes value, which is the
  property ADR-0032 §2 chose the factor for;
* **shrunk at small ``n_holdout``** — six samples at ``kappa_w`` 0.71 yields 0.55, hand-computed
  as ``0.71 × sqrt(6/10) = 0.71 × 0.7746``;
* **clamped at both ends** — never above 1.0, never below 0.05.

And the third property ADR-0032 names, which is the one with teeth: **mechanizing a criterion
raises the factor arithmetically**. The formula pays the author for climbing the ladder.
"""

from __future__ import annotations

import math

import pytest

from freeweight.domain.calibration import (
    DEFAULT_N_HOLDOUT_TARGET,
    criterion_validity,
    judge_validity_factor,
)


class TestOneForEveryDeterministicMeasurement:
    def test_a_goal_scored_entirely_by_rules(self) -> None:
        assert (
            judge_validity_factor(weights={"tells": 0.6, "rhythm": 0.4}, judged={}, n_holdout={})
            == 1.0
        )

    def test_a_goal_with_reference_and_human_criteria_too(self) -> None:
        # Rungs 1-4 are all v = 1.0; only rung 5 is shrunk.
        assert (
            judge_validity_factor(
                weights={"entities": 0.5, "graded_by_hand": 0.5}, judged={}, n_holdout={}
            )
            == 1.0
        )


class TestTheShrinkage:
    def test_six_holdout_samples_at_zero_point_seven_one_yields_zero_point_five_five(self) -> None:
        # Hand-computed: 0.71 * sqrt(6/10) = 0.71 * 0.774596... = 0.5499...
        assert criterion_validity(0.71, n_holdout=6) == pytest.approx(0.71 * math.sqrt(0.6))
        assert criterion_validity(0.71, n_holdout=6) == pytest.approx(0.55, abs=0.005)

    def test_at_the_target_there_is_no_shrinkage(self) -> None:
        assert criterion_validity(0.71, n_holdout=DEFAULT_N_HOLDOUT_TARGET) == pytest.approx(0.71)

    def test_beyond_the_target_the_shrinkage_does_not_become_a_bonus(self) -> None:
        assert criterion_validity(0.71, n_holdout=1000) == pytest.approx(0.71)

    def test_a_larger_holdout_is_worth_more_than_a_smaller_one(self) -> None:
        assert criterion_validity(0.8, n_holdout=9) > criterion_validity(0.8, n_holdout=4)

    def test_the_target_is_configuration(self) -> None:
        assert criterion_validity(0.8, n_holdout=5, n_holdout_target=5) == pytest.approx(0.8)


class TestClampedAtBothEnds:
    def test_a_negative_coefficient_is_worth_nothing(self) -> None:
        # A judge that agrees with the author worse than chance has established nothing.
        assert criterion_validity(-0.4, n_holdout=20) == 0.0

    def test_an_unmeasured_coefficient_is_worth_nothing(self) -> None:
        assert criterion_validity(None, n_holdout=20) == 0.0

    def test_the_goal_level_factor_never_exceeds_one(self) -> None:
        assert (
            judge_validity_factor(weights={"wit": 1.0}, judged={"wit": 1.0}, n_holdout={"wit": 50})
            == 1.0
        )

    def test_the_goal_level_factor_never_falls_below_the_floor(self) -> None:
        # ADR-0017's own floor, applied to the sixth factor as well.
        assert (
            judge_validity_factor(weights={"wit": 1.0}, judged={"wit": -1.0}, n_holdout={"wit": 1})
            == 0.05  # noqa: PLR2004 — the floor, stated as a number
        )


class TestMechanizingACriterionRaisesTheFactor:
    """The arithmetic incentive the ladder lacked (ADR-0032 §2)."""

    @staticmethod
    def _judged_only() -> float:
        return judge_validity_factor(
            weights={"tells": 0.5, "wit": 0.5},
            judged={"tells": 0.5, "wit": 0.5},
            n_holdout={"tells": 10, "wit": 10},
        )

    def test_moving_one_criterion_from_judge_to_rule(self) -> None:
        before = self._judged_only()
        after = judge_validity_factor(
            weights={"tells": 0.5, "wit": 0.5},
            judged={"wit": 0.5},
            n_holdout={"wit": 10},
        )
        assert after > before
        # Half the weight now scores at 1.0 and half at 0.5.
        assert after == pytest.approx(0.75)

    def test_moving_weight_onto_a_rule_without_removing_the_criterion(self) -> None:
        before = self._judged_only()
        after = judge_validity_factor(
            weights={"tells": 0.8, "wit": 0.2},
            judged={"wit": 0.5},
            n_holdout={"wit": 10},
        )
        assert after > before

    def test_a_goal_that_mechanizes_everything_reaches_one(self) -> None:
        assert (
            judge_validity_factor(weights={"tells": 0.5, "wit": 0.5}, judged={}, n_holdout={})
            == 1.0
        )


class TestRefusals:
    def test_no_weights_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one weighted criterion"):
            judge_validity_factor(weights={}, judged={}, n_holdout={})

    def test_a_judged_criterion_with_no_weight_is_refused(self) -> None:
        # The factor is a weighted mean; a criterion whose share is unknown cannot be in one.
        with pytest.raises(ValueError, match="have no weight"):
            judge_validity_factor(
                weights={"tells": 1.0}, judged={"wit": 0.5}, n_holdout={"wit": 10}
            )

    def test_zero_total_weight_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive total weight"):
            judge_validity_factor(weights={"tells": 0.0}, judged={}, n_holdout={})

    def test_a_negative_holdout_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            criterion_validity(0.7, n_holdout=-1)

    def test_a_non_positive_target_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            criterion_validity(0.7, n_holdout=6, n_holdout_target=0)
