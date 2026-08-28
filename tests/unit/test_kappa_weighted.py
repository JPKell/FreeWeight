"""Quadratic-weighted Cohen's kappa, from both ends.

The phase's own test list is exact about what has to hold, and every clause of it is a class here:

* ``kappa_w`` against **hand-computed confusion matrices**;
* a **perfectly agreeing** synthetic grader yields ``1.0``;
* a **uniformly random** grader yields ``≈ 0``;
* a grader that is **consistently one point generous** yields high ``rho``, high ``kappa_w`` and
  non-zero ``bias`` — *the three statistics must be able to disagree with each other, or only one
  of them is real*.

The last one is the important one. A subtly wrong ``kappa_w`` would be invisible for months
because it would go on producing plausible numbers; the only defence is fixtures whose true
agreement is known by construction, and asserting that the three figures move independently.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence

import pytest

from freeweight.domain.agreement import (
    AgreementBand,
    agreement,
    band_for,
    cohens_kappa_weighted,
    mean_absolute_error,
    signed_bias,
    spearman_rho,
)


class TestHandComputedConfusionMatrices:
    """Small matrices whose arithmetic is written out in the comments."""

    def test_perfect_agreement_on_a_three_point_scale(self) -> None:
        # Observed weighted disagreement is 0, so kappa is 1 whatever the expected is.
        assert cohens_kappa_weighted([1, 2, 3, 1], [1, 2, 3, 1], scale_points=3) == 1.0

    def test_one_cell_off_by_one_on_a_five_point_scale(self) -> None:
        # Four pairs: (1,1), (3,3), (5,5), (4,5).
        # w_ij = (i-j)^2 / 16, so the only non-zero observed cell is (4,5): w = 1/16, O = 1/4.
        #   Do = (1/16)(1/4) = 0.015625
        # Marginals: author {1:1, 3:1, 4:1, 5:1}/4 ; jury {1:1, 3:1, 5:2}/4.
        #   De = sum over i,j of w_ij * p_i * q_j
        #      = (1/16)*Σ (i-j)^2 p_i q_j
        # Σ (i-j)^2 p_i q_j, with p = q' as above:
        #   author 1 (p=.25) vs jury 1(.25)=0, 3(.25)=4, 5(.5)=16   -> .25*(0*.25 + 4*.25 + 16*.5)
        #   author 3 (.25)   vs 1=4, 3=0, 5=4                        -> .25*(4*.25 + 0 + 4*.5)
        #   author 4 (.25)   vs 1=9, 3=1, 5=1                        -> .25*(9*.25 + 1*.25 + 1*.5)
        #   author 5 (.25)   vs 1=16, 3=4, 5=0                       -> .25*(16*.25 + 4*.25 + 0)
        #   = .25*(1+8) + .25*(1+2) + .25*(2.25+.25+.5) + .25*(4+1)
        #   = 2.25 + 0.75 + 0.75 + 1.25 = 5.0
        #   De = 5.0/16 = 0.3125
        # kappa = 1 - 0.015625/0.3125 = 0.95
        assert cohens_kappa_weighted([1, 3, 5, 4], [1, 3, 5, 5], scale_points=5) == pytest.approx(
            0.95
        )

    def test_a_four_versus_five_costs_far_less_than_a_one_versus_five(self) -> None:
        # Ordinal awareness, which is the whole reason the weights are quadratic.
        near = cohens_kappa_weighted([1, 3, 5, 4], [1, 3, 5, 5], scale_points=5)
        far = cohens_kappa_weighted([1, 3, 5, 1], [1, 3, 5, 5], scale_points=5)
        assert near is not None
        assert far is not None
        assert near > far

    def test_a_maximally_wrong_grader_is_negative(self) -> None:
        # Worse than chance, which kappa is entitled to say and a raw agreement rate is not.
        result = cohens_kappa_weighted([1, 1, 5, 5], [5, 5, 1, 1], scale_points=5)
        assert result is not None
        assert result < 0


class TestSyntheticGraders:
    """Graders whose true agreement is known by construction."""

    _SCALE = 5
    _AUTHOR = [1, 2, 3, 4, 5, 1, 3, 5, 2, 4, 3, 1]

    def test_a_perfect_grader_yields_one(self) -> None:
        assert cohens_kappa_weighted(self._AUTHOR, self._AUTHOR, scale_points=self._SCALE) == 1.0

    def test_a_uniformly_random_grader_yields_about_zero(self) -> None:
        # Seeded, so the assertion is a fact about the formula rather than about today's luck.
        generator = random.Random(20260827)  # noqa: S311 — a test fixture, not crypto
        author = [generator.randint(1, self._SCALE) for _ in range(4000)]
        jury = [generator.randint(1, self._SCALE) for _ in range(4000)]
        result = cohens_kappa_weighted(author, jury, scale_points=self._SCALE)
        assert result is not None
        assert abs(result) < 0.05  # noqa: PLR2004 — "approximately zero", stated as a number

    def test_a_consistently_generous_grader_separates_the_three_statistics(self) -> None:
        # The case the phase's own test list singles out: rho high, kappa_w high, bias non-zero.
        # If all three moved together, only one of them would be real.
        author = [1, 2, 3, 4, 1, 2, 3, 4, 1, 2, 3, 4]
        jury = [grade + 1 for grade in author]
        result = agreement(author, jury, scale_points=self._SCALE)
        assert result.rho == pytest.approx(1.0)
        assert result.kappa_w is not None
        assert result.kappa_w > 0.6  # noqa: PLR2004 — "high", stated as a number
        assert result.bias == pytest.approx(1.0)
        assert result.mae == pytest.approx(1.0)

    def test_a_grader_that_ranks_backwards_separates_them_the_other_way(self) -> None:
        # Perfectly *anti*-correlated: rho is -1, kappa is negative, and the bias is zero because
        # the errors cancel. A bias-only reader would call this jury unbiased.
        author = [1, 2, 3, 4, 5]
        jury = [5, 4, 3, 2, 1]
        result = agreement(author, jury, scale_points=self._SCALE)
        assert result.rho == pytest.approx(-1.0)
        assert result.kappa_w is not None
        assert result.kappa_w < 0
        assert result.bias == pytest.approx(0.0)
        assert result.mae > 0

    def test_a_noisy_but_unbiased_grader_has_a_middling_kappa_and_no_bias(self) -> None:
        author = [1, 2, 3, 4, 5, 1, 2, 3, 4, 5]
        jury = [2, 1, 3, 5, 4, 1, 3, 2, 5, 4]
        result = agreement(author, jury, scale_points=self._SCALE)
        assert result.kappa_w is not None
        assert 0.4 < result.kappa_w < 0.95  # noqa: PLR2004 — the middle band, as a number
        assert abs(result.bias) < 0.3  # noqa: PLR2004 — near zero, as a number


class TestNoVarianceAndBoundaries:
    """A chance-corrected statistic needs something to correct against."""

    def test_two_constant_graders_have_no_agreement_to_measure(self) -> None:
        # Expected disagreement is zero, so the ratio is a division nobody should perform.
        assert cohens_kappa_weighted([3, 3, 3], [3, 3, 3], scale_points=5) is None

    def test_one_constant_grader_still_has_a_coefficient(self) -> None:
        result = cohens_kappa_weighted([3, 3, 3, 3], [1, 3, 5, 3], scale_points=5)
        assert result is not None

    def test_a_single_pair_is_not_enough(self) -> None:
        assert cohens_kappa_weighted([3], [4], scale_points=5) is None

    def test_an_empty_pair_of_series_is_not_enough(self) -> None:
        assert cohens_kappa_weighted([], [], scale_points=5) is None

    def test_series_of_different_lengths_are_refused(self) -> None:
        with pytest.raises(ValueError, match="paired series"):
            cohens_kappa_weighted([1, 2], [1], scale_points=5)

    def test_a_grade_off_its_own_scale_is_refused(self) -> None:
        # It would silently reweight every kappa cell.
        with pytest.raises(ValueError, match="outside 1..5"):
            cohens_kappa_weighted([1, 6], [1, 5], scale_points=5)

    def test_a_two_point_scale_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 3 points"):
            cohens_kappa_weighted([1, 2], [1, 2], scale_points=2)


class TestTheOtherThreeStatistics:
    def test_mean_absolute_error_is_in_scale_points(self) -> None:
        assert mean_absolute_error([1, 2, 3], [2, 2, 5]) == pytest.approx(1.0)

    def test_bias_is_jury_minus_author(self) -> None:
        # Positive means the jury is more generous. The direction is fixed and stated everywhere.
        assert signed_bias([1, 2, 3], [2, 3, 4]) == pytest.approx(1.0)
        assert signed_bias([2, 3, 4], [1, 2, 3]) == pytest.approx(-1.0)

    def test_spearman_handles_ties_with_average_ranks(self) -> None:
        assert spearman_rho([1, 1, 2, 3], [1, 1, 2, 3]) == pytest.approx(1.0)

    def test_spearman_is_none_when_a_series_is_constant(self) -> None:
        # A constant series has no ranks to correlate; 0.0 would say "no relationship" where the
        # truth is "no measurement".
        assert spearman_rho([3, 3, 3], [1, 2, 3]) is None

    @pytest.mark.parametrize("statistic", [mean_absolute_error, signed_bias, spearman_rho])
    def test_every_statistic_refuses_unpaired_series(
        self, statistic: Callable[[Sequence[float], Sequence[float]], float | None]
    ) -> None:
        with pytest.raises(ValueError, match="paired series"):
            statistic([1, 2], [1])

    def test_empty_series_give_zero_error_and_zero_bias(self) -> None:
        assert mean_absolute_error([], []) == 0.0
        assert signed_bias([], []) == 0.0


class TestTheInterpretationBands:
    """A band, not a bare coefficient: "0.62" tells a person nothing (Subjective Goals §5.5)."""

    @pytest.mark.parametrize(
        ("kappa", "expected"),
        [
            (0.90, AgreementBand.STRONG),
            (0.75, AgreementBand.STRONG),
            (0.74, AgreementBand.GOOD),
            (0.60, AgreementBand.GOOD),
            (0.59, AgreementBand.FAIR),
            (0.40, AgreementBand.FAIR),
            (0.39, AgreementBand.NOT_MEASURABLE),
            (-0.5, AgreementBand.NOT_MEASURABLE),
            (None, AgreementBand.NOT_MEASURABLE),
        ],
    )
    def test_the_boundaries(self, kappa: float | None, expected: str) -> None:
        assert band_for(kappa) == expected

    def test_every_band_states_its_consequence(self) -> None:
        for band, description in AgreementBand.DESCRIPTIONS.items():
            assert description, band
            assert description[0].isupper()


class TestTheResultCarriesItsN:
    """``kappa_w`` without its ``n`` is a number pretending to be a fact."""

    def test_the_count_is_on_the_result(self) -> None:
        result = agreement([1, 2, 3, 4], [1, 2, 3, 5], scale_points=5)
        assert result.n == 4  # noqa: PLR2004 — the count is the assertion

    def test_and_in_its_json(self) -> None:
        body = agreement([1, 2, 3, 4], [1, 2, 3, 5], scale_points=5).as_json()
        assert body["n_holdout"] == 4  # noqa: PLR2004 — the count is the assertion
        assert "kappa_w" in body
        assert body["scale_points"] == 5  # noqa: PLR2004 — the scale is the assertion
