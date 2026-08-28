"""The calibration gate, and the distinction it exists to keep: insufficient is not uncalibrated.

The phase's own test list names the two things this file has to prove:

* **``CALIBRATION_INSUFFICIENT`` is distinguished from a failed gate in code**, which is what lets
  the API and the UI copy distinguish them too. One means the author has not done the work; the
  other means they did it and learned the rubric is not measurable. The remedies are opposite;
* **below threshold ⇒ no evidence at all** — not discounted evidence, none. The run still
  completes and every sample is inspectable, which is what makes the gate useful rather than
  merely obstructive.

The *absence* of an evidence row is asserted directly, in
``tests/integration/test_calibration_flow.py``, because "we emitted it quietly at the floor" is
precisely the failure the gate exists to prevent.
"""

from __future__ import annotations

import pytest

from freeweight.domain.agreement import concentrated_grades, has_variance
from freeweight.domain.calibration import CalibrationState, GateVerdict, verdict_for


def _verdict(  # noqa: PLR0913 — the gate's own inputs, one keyword each
    *,
    weights: dict[str, float] | None = None,
    judged_kappa: dict[str, float | None] | None = None,
    n_holdout: dict[str, int] | None = None,
    graded_samples: int = 12,
    min_samples: int = 8,
    min_agreement: float = 0.40,
) -> GateVerdict:
    """Build a verdict from the defaults, overriding one thing at a time."""
    return verdict_for(
        weights=weights if weights is not None else {"tells": 0.5, "wit": 0.5},
        judged_kappa=judged_kappa if judged_kappa is not None else {"wit": 0.8},
        n_holdout=n_holdout if n_holdout is not None else {"wit": 10},
        graded_samples=graded_samples,
        min_samples=min_samples,
        min_agreement=min_agreement,
    )


class TestTheFourStates:
    def test_a_goal_with_no_judged_criterion_needs_no_calibration(self) -> None:
        verdict = _verdict(judged_kappa={}, n_holdout={})
        assert verdict.state is CalibrationState.NOT_REQUIRED
        assert verdict.passed is True
        assert verdict.judge_validity_factor == 1.0

    def test_enough_grades_and_enough_agreement_is_calibrated(self) -> None:
        verdict = _verdict()
        assert verdict.state is CalibrationState.CALIBRATED
        assert verdict.passed is True

    def test_enough_grades_and_too_little_agreement_is_uncalibrated(self) -> None:
        verdict = _verdict(judged_kappa={"wit": 0.2})
        assert verdict.state is CalibrationState.UNCALIBRATED
        assert verdict.passed is False
        assert verdict.weighted_kappa_w == 0.2

    def test_too_few_grades_is_insufficient_and_not_uncalibrated(self) -> None:
        # The distinction the whole file exists for.
        verdict = _verdict(graded_samples=3)
        assert verdict.state is CalibrationState.INSUFFICIENT
        assert verdict.passed is False
        assert verdict.weighted_kappa_w is None

    def test_insufficient_reports_what_is_missing_rather_than_a_coefficient(self) -> None:
        body = _verdict(graded_samples=3).as_json()
        assert body["calibration_state"] == "insufficient"
        assert body["graded_samples"] == 3  # noqa: PLR2004 — the count is the assertion
        assert body["min_samples"] == 8  # noqa: PLR2004 — the minimum is the assertion
        assert body["weighted_kappa_w"] is None

    def test_the_two_failing_states_are_different_values(self) -> None:
        insufficient = _verdict(graded_samples=3).as_json()
        uncalibrated = _verdict(judged_kappa={"wit": 0.1}).as_json()
        assert insufficient["calibration_state"] != uncalibrated["calibration_state"]
        assert insufficient["passed_gate"] is uncalibrated["passed_gate"] is False


class TestTheThreshold:
    @pytest.mark.parametrize(
        ("kappa", "expected"),
        [
            (0.41, CalibrationState.CALIBRATED),
            (0.40, CalibrationState.CALIBRATED),
            (0.39, CalibrationState.UNCALIBRATED),
            (0.0, CalibrationState.UNCALIBRATED),
            (-0.2, CalibrationState.UNCALIBRATED),
        ],
    )
    def test_the_boundary_is_inclusive(self, kappa: float, expected: CalibrationState) -> None:
        assert _verdict(judged_kappa={"wit": kappa}).state is expected

    def test_the_threshold_is_recorded_because_it_is_configuration(self) -> None:
        # A reader must not assume the default.
        body = _verdict(min_agreement=0.6, judged_kappa={"wit": 0.5}).as_json()
        assert body["min_agreement"] == 0.6  # noqa: PLR2004 — the threshold is the assertion
        assert body["calibration_state"] == "uncalibrated"

    def test_the_policy_version_travels_with_the_verdict(self) -> None:
        assert _verdict().as_json()["policy_version"]

    def test_criteria_are_weighted_by_their_share_of_the_composite(self) -> None:
        # A weak criterion carrying 10% of the weight must not sink a strong one carrying 90%.
        verdict = verdict_for(
            weights={"big": 0.9, "small": 0.1},
            judged_kappa={"big": 0.8, "small": 0.0},
            n_holdout={"big": 10, "small": 10},
            graded_samples=12,
            min_samples=8,
            min_agreement=0.4,
        )
        assert verdict.weighted_kappa_w == pytest.approx(0.72)
        assert verdict.state is CalibrationState.CALIBRATED

    def test_an_unmeasurable_criterion_is_excluded_rather_than_counted_as_zero(self) -> None:
        # It has not been measured badly; it has not been measured.
        verdict = verdict_for(
            weights={"big": 0.5, "small": 0.5},
            judged_kappa={"big": 0.8, "small": None},
            n_holdout={"big": 10, "small": 0},
            graded_samples=12,
            min_samples=8,
            min_agreement=0.4,
        )
        assert verdict.weighted_kappa_w == pytest.approx(0.8)


class TestTheGradeDistributionChecks:
    """Two different refusals, and they mean different things."""

    def test_a_set_with_no_variance_has_nothing_to_agree_about(self) -> None:
        assert has_variance([4, 4, 4, 4]) is False
        assert has_variance([4, 4, 5, 4]) is True

    def test_a_single_grade_is_not_variance(self) -> None:
        assert has_variance([4]) is False

    def test_the_check_fires_on_an_all_four_and_five_set(self) -> None:
        # Subjective Goals §5.1's own example: "You graded eleven of twelve samples 4 or 5."
        grades = [4, 5, 4, 5, 5, 4, 5, 4, 5, 5, 4, 3]
        assert concentrated_grades(grades, scale_points=5) is True

    def test_it_fires_at_the_bottom_of_the_scale_too(self) -> None:
        assert concentrated_grades([1, 2, 1, 2, 1, 1, 2, 1, 1, 2], scale_points=5) is True

    def test_a_spread_set_passes(self) -> None:
        assert concentrated_grades([1, 2, 3, 4, 5, 1, 2, 3, 4, 5], scale_points=5) is False

    def test_the_threshold_is_configurable(self) -> None:
        grades = [4, 5, 4, 5, 3, 2]
        assert concentrated_grades(grades, scale_points=5, threshold=0.9) is False
        assert concentrated_grades(grades, scale_points=5, threshold=0.6) is True

    def test_an_empty_set_is_a_different_problem(self) -> None:
        assert concentrated_grades([], scale_points=5) is False

    def test_a_two_point_scale_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 3 points"):
            concentrated_grades([1, 2], scale_points=2)
