"""The anchor/holdout partition: deterministic, stratified, and reproducible from its seed.

Two of the phase's named cases live here — **deterministic under a fixed seed** and **stratified
across the grade range**. The third, *the holdout is provably never rendered into a judge prompt*,
needs a rendered prompt to scan and lives in ``tests/integration/test_calibration_flow.py``, where
it is asserted by hashing what the jury was actually shown rather than by reading the code.
"""

from __future__ import annotations

import pytest

from freeweight.domain.calibration import partition_samples


def _graded(counts: dict[int, int]) -> dict[str, int]:
    """Build ``{sample_id: grade}`` with ``counts[grade]`` samples at each grade."""
    return {
        f"s{grade}_{index}": grade
        for grade, count in sorted(counts.items())
        for index in range(count)
    }


class TestDeterminism:
    _GRADED = _graded({1: 3, 2: 3, 3: 2, 4: 2, 5: 2})

    def test_the_same_seed_gives_the_same_split(self) -> None:
        first = partition_samples(self._GRADED, holdout_fraction=0.4, seed=7)
        second = partition_samples(self._GRADED, holdout_fraction=0.4, seed=7)
        assert first.anchors == second.anchors
        assert first.holdout == second.holdout

    def test_a_different_seed_gives_a_different_split(self) -> None:
        first = partition_samples(self._GRADED, holdout_fraction=0.4, seed=7)
        second = partition_samples(self._GRADED, holdout_fraction=0.4, seed=8)
        assert (first.anchors, first.holdout) != (second.anchors, second.holdout)

    def test_insertion_order_does_not_change_the_split(self) -> None:
        reversed_input = dict(reversed(list(self._GRADED.items())))
        first = partition_samples(self._GRADED, holdout_fraction=0.4, seed=7)
        second = partition_samples(reversed_input, holdout_fraction=0.4, seed=7)
        assert first.anchors == second.anchors
        assert first.holdout == second.holdout

    def test_the_seed_is_recorded_on_the_partition(self) -> None:
        # So a reader can verify the holdout was not chosen to flatter the result.
        assert partition_samples(self._GRADED, holdout_fraction=0.4, seed=7).seed == 7  # noqa: PLR2004


class TestStratification:
    def test_both_halves_span_the_scale(self) -> None:
        graded = _graded({1: 4, 2: 4, 3: 4, 4: 4, 5: 4})
        partition = partition_samples(graded, holdout_fraction=0.4, seed=0)
        anchor_grades = {graded[sample] for sample in partition.anchors}
        holdout_grades = {graded[sample] for sample in partition.holdout}
        assert anchor_grades == {1, 2, 3, 4, 5}
        assert holdout_grades == {1, 2, 3, 4, 5}

    def test_a_stratum_of_two_contributes_one_to_each_half(self) -> None:
        graded = _graded({1: 2, 5: 2})
        partition = partition_samples(graded, holdout_fraction=0.4, seed=0)
        assert len(partition.holdout) == 2  # noqa: PLR2004 — one from each stratum
        assert len(partition.anchors) == 2  # noqa: PLR2004 — one from each stratum

    def test_a_stratum_of_one_stays_whole_rather_than_being_split(self) -> None:
        graded = _graded({3: 1})
        partition = partition_samples(graded, holdout_fraction=0.4, seed=0)
        assert partition.anchors == ("s3_0",)
        assert partition.holdout == ()

    def test_no_stratum_is_taken_entirely_into_the_holdout(self) -> None:
        # A holdout that swallowed a whole grade would leave the judge prompt with no example of
        # it, which is the anchor set's whole purpose.
        graded = _graded({1: 2, 2: 2, 3: 2})
        partition = partition_samples(graded, holdout_fraction=0.9, seed=0)
        anchor_grades = {graded[sample] for sample in partition.anchors}
        assert anchor_grades == {1, 2, 3}

    def test_the_strata_are_recorded_for_the_report(self) -> None:
        graded = _graded({1: 3, 5: 2})
        assert partition_samples(graded, holdout_fraction=0.4, seed=0).strata == {1: 3, 5: 2}


class TestShapeAndRefusals:
    def test_every_sample_lands_in_exactly_one_half(self) -> None:
        graded = _graded({1: 3, 2: 3, 3: 3, 4: 3})
        partition = partition_samples(graded, holdout_fraction=0.4, seed=0)
        assert set(partition.anchors) | set(partition.holdout) == set(graded)
        assert not set(partition.anchors) & set(partition.holdout)

    def test_the_label_lookup_answers_for_every_member(self) -> None:
        graded = _graded({1: 2, 5: 2})
        partition = partition_samples(graded, holdout_fraction=0.4, seed=0)
        for sample in partition.anchors:
            assert partition.label(sample) == "anchor"
        for sample in partition.holdout:
            assert partition.label(sample) == "holdout"

    def test_a_sample_outside_the_partition_is_refused_rather_than_labelled(self) -> None:
        partition = partition_samples(_graded({1: 2}), holdout_fraction=0.4, seed=0)
        with pytest.raises(KeyError, match="not part of this partition"):
            partition.label("somebody-elses-sample")

    def test_an_empty_set_produces_an_empty_partition(self) -> None:
        partition = partition_samples({}, holdout_fraction=0.4, seed=0)
        assert partition.anchors == ()
        assert partition.holdout == ()

    @pytest.mark.parametrize("fraction", [0.0, 1.0, -0.1, 1.5])
    def test_a_degenerate_fraction_is_refused(self, fraction: float) -> None:
        with pytest.raises(ValueError, match="above 0 and below 1"):
            partition_samples(_graded({1: 2}), holdout_fraction=fraction, seed=0)

    def test_roughly_the_requested_share_is_withheld(self) -> None:
        graded = _graded({1: 5, 2: 5, 3: 5, 4: 5})
        partition = partition_samples(graded, holdout_fraction=0.4, seed=0)
        assert len(partition.holdout) == 8  # noqa: PLR2004 — two from each of four strata
        assert len(partition.anchors) == 12  # noqa: PLR2004 — three from each of four strata
