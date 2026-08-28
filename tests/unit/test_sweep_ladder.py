"""The long-context sweep's ceiling is configuration, and it separates results.

Two things are asserted here, and the second matters more than the first:

* **the ladder fits the machine** — truncated on a card that cannot serve 32 000 tokens, extended
  by doubling on one that can serve far more, and never empty;
* **two ceilings are two measurements.** A sweep that stopped at 32 000 reports a smaller
  ``effective_context_tokens`` than one that reached 128 000 for reasons that have nothing to do
  with the model. The effective ladder is therefore hashed into the built suite's
  ``dataset_hashes``, where every other content-identity fact already lives, so the separation is
  structural rather than something a reader has to notice.
"""

from __future__ import annotations

import pytest

from freeweight.benchmarks.long_context.benchmark import (
    LADDER_DATASET_KEY,
    ladder_hash,
    sweep_ladder,
)

SHIPPED = (2_000, 4_000, 8_000, 16_000, 32_000)


class TestTheLadderFitsTheMachine:
    def test_the_shipped_ceiling_leaves_the_shipped_ladder_alone(self) -> None:
        """The default must change nothing, or every existing result is separated for no reason."""
        assert sweep_ladder(SHIPPED, ceiling=32_000) == SHIPPED

    def test_a_lower_ceiling_truncates(self) -> None:
        assert sweep_ladder(SHIPPED, ceiling=8_000) == (2_000, 4_000, 8_000)

    def test_a_higher_ceiling_keeps_doubling(self) -> None:
        assert sweep_ladder(SHIPPED, ceiling=128_000) == (*SHIPPED, 64_000, 128_000)

    def test_a_ceiling_off_the_doubling_lands_on_itself(self) -> None:
        """The ceiling is a rung in its own right — a sweep that stopped short of what the machine
        can serve would report a floor as though it were a limit."""
        assert sweep_ladder(SHIPPED, ceiling=100_000)[-1] == 100_000

    def test_a_ceiling_below_the_first_rung_still_sweeps_once(self) -> None:
        """An empty sweep measures nothing and would report the absence as a model property."""
        assert sweep_ladder(SHIPPED, ceiling=500) == (2_000,)

    def test_the_ladder_is_ascending_and_has_no_duplicates(self) -> None:
        for ceiling in (1_000, 8_000, 32_000, 33_000, 64_000, 262_144):
            ladder = sweep_ladder(SHIPPED, ceiling=ceiling)
            assert list(ladder) == sorted(ladder), ceiling
            assert len(set(ladder)) == len(ladder), ceiling


class TestTwoCeilingsAreTwoMeasurements:
    @staticmethod
    def _document(lengths: tuple[int, ...]) -> dict[str, object]:
        return {"tests": [{"key": "depth_sweep", "context_lengths": list(lengths)}]}

    def test_the_same_ladder_hashes_the_same(self) -> None:
        assert ladder_hash(self._document(SHIPPED)) == ladder_hash(self._document(SHIPPED))

    def test_a_different_ceiling_hashes_differently(self) -> None:
        assert ladder_hash(self._document(SHIPPED)) != ladder_hash(
            self._document((*SHIPPED, 64_000))
        )

    def test_the_hash_reaches_the_built_manifest(self) -> None:
        """Which is what makes the separation happen without a second mechanism: the fingerprint
        already covers ``dataset_hashes``."""
        from freeweight.benchmarks.long_context import benchmark as long_context

        narrow = long_context.build(max_context_tokens=8_000)
        wide = long_context.build(max_context_tokens=64_000)

        assert (
            narrow.manifest.dataset_hashes[LADDER_DATASET_KEY]
            != (wide.manifest.dataset_hashes[LADDER_DATASET_KEY])
        )

    def test_only_the_depth_sweep_stretches(self) -> None:
        """The other three hold context constant on purpose — they vary position, distractors and
        fact separation instead, and stretching them would change what they measure."""
        from freeweight.benchmarks.long_context import benchmark as long_context

        wide = long_context.build(max_context_tokens=64_000)
        by_key = {test.key: test for test in wide.tests}

        assert len(list(by_key["depth_sweep"].cases())) > len(SHIPPED)
        assert len(list(by_key["position_sweep"].cases())) == 5

    @pytest.mark.parametrize("ceiling", [8_000, 32_000, 64_000])
    def test_every_ceiling_builds_a_runnable_suite(self, ceiling: int) -> None:
        from freeweight.benchmarks.long_context import benchmark as long_context

        suite = long_context.build(max_context_tokens=ceiling)
        assert suite.tests
        for test in suite.tests:
            assert list(test.cases()), f"{test.key} built no cases at ceiling {ceiling}"
