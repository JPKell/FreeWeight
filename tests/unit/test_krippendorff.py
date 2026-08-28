"""Krippendorff's alpha: against a published worked example, and from both ends.

The phase's own test list names two cases: **against a published worked example**, and **total
juror agreement ⇒ 1.0**. The published example is the reliability-data matrix distributed with the
``krippendorff`` reference implementation, whose interval alpha is documented as ``0.811``; this
module reproduces it to six decimal places.

The rest is what makes the coefficient trustworthy rather than merely present: a random rater
lands near zero, missing ratings are handled without imputation, and a degenerate set returns
``None`` rather than a number produced by a division nobody should have done.
"""

from __future__ import annotations

import random

import pytest

from freeweight.domain.agreement import krippendorff_alpha

_N = None

# The reliability data published with the reference implementation of Krippendorff's alpha.
# Documented results: nominal 0.691, interval 0.811.
_PUBLISHED: list[list[float | None]] = [
    [_N, _N, _N, _N, _N, 3, 4, 1, 2, 1, 1, 3, 3, _N, 3],
    [1, _N, 2, 1, 3, 3, 4, 3, _N, _N, _N, _N, _N, _N, _N],
    [_N, _N, 2, 1, 3, 4, 4, _N, 2, 1, 1, 3, 3, _N, 4],
]


def _units(observers: list[list[float | None]]) -> list[list[float | None]]:
    """Transpose an observer-by-unit matrix into the unit-by-observer form alpha takes."""
    return [list(column) for column in zip(*observers, strict=True)]


class TestThePublishedWorkedExample:
    def test_the_interval_alpha_matches_the_documented_value(self) -> None:
        alpha = krippendorff_alpha(_units(_PUBLISHED))
        assert alpha is not None
        assert alpha == pytest.approx(0.811, abs=0.001)

    def test_units_rated_by_one_observer_are_dropped_rather_than_imputed(self) -> None:
        # Unit 14 is rated by nobody and unit 2 by one observer; both contribute nothing, and
        # removing them from the input must not move the coefficient.
        pruned = [
            [row[index] for index in range(len(row)) if index not in {1, 13}] for row in _PUBLISHED
        ]
        assert krippendorff_alpha(_units(pruned)) == pytest.approx(
            krippendorff_alpha(_units(_PUBLISHED))
        )


class TestTotalAgreementAndItsOpposite:
    def test_every_juror_agreeing_everywhere_is_one(self) -> None:
        assert krippendorff_alpha([[1, 1, 1], [3, 3, 3], [5, 5, 5]]) == 1.0

    def test_two_jurors_agreeing_everywhere_is_one(self) -> None:
        assert krippendorff_alpha([[1, 1], [3, 3], [5, 5]]) == 1.0

    def test_a_random_rater_lands_near_zero(self) -> None:
        generator = random.Random(20260827)  # noqa: S311 — a test fixture, not crypto
        units = [[generator.randint(1, 5) for _ in range(3)] for _ in range(3000)]
        alpha = krippendorff_alpha(units)
        assert alpha is not None
        assert abs(alpha) < 0.05  # noqa: PLR2004 — "approximately zero", stated as a number

    def test_systematic_disagreement_is_negative(self) -> None:
        units = [[1, 5], [5, 1], [1, 5], [5, 1]]
        alpha = krippendorff_alpha(units)
        assert alpha is not None
        assert alpha < 0

    def test_near_agreement_beats_far_agreement(self) -> None:
        # The interval metric is ordinal-aware: a one-point disagreement costs far less than four.
        near = krippendorff_alpha([[1, 2], [3, 3], [5, 4]])
        far = krippendorff_alpha([[1, 5], [3, 3], [5, 1]])
        assert near is not None
        assert far is not None
        assert near > far


class TestDegenerateInput:
    def test_every_rating_identical_is_no_measurement_rather_than_perfect(self) -> None:
        # Expected disagreement is zero; there is nothing to agree *about*.
        assert krippendorff_alpha([[3, 3], [3, 3]]) is None

    def test_a_single_unit_gives_no_usable_coefficient(self) -> None:
        # Alpha over one unit is degenerate: it evaluates to 0 however well the jurors agreed.
        assert krippendorff_alpha([[4, 4, 5]]) == 0.0

    def test_units_with_one_rating_contribute_nothing(self) -> None:
        assert krippendorff_alpha([[1, None], [2, None]]) is None

    def test_an_empty_matrix_is_none(self) -> None:
        assert krippendorff_alpha([]) is None

    def test_a_nominal_metric_is_refused_rather_than_silently_substituted(self) -> None:
        # A different metric gives a different number, and returning one under the other's name
        # would be the quietest possible error.
        with pytest.raises(ValueError, match="interval difference metric"):
            krippendorff_alpha([[1, 1], [2, 2]], interval=False)
