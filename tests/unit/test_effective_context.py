"""``effective_context_tokens``: measured against the configured threshold, and honest when absent.

The phase's own test list names two cases, and each is a class here:

* **effective context computed against the configured threshold** — the same accuracy curve gives
  different answers at 0.8 and at 0.5, and the threshold is recorded on every sample so a stored
  number can be read back with the line it was drawn at;
* **a model failing at depth is distinguished from one failing everywhere** — the first has a
  short-context baseline and loses it; the second never had one, and reporting the shortest length
  tested as its effective context would be a claim that it works there.

The rest is the metric-formula treatment testing standards §5 requires, plus the aggregation path
that turns per-sample coordinates into one figure.
"""

from __future__ import annotations

import pytest

from freeweight.benchmarks.long_context.haystack import (
    CHARACTERS_PER_TOKEN,
    approximate_tokens,
    assemble,
)
from freeweight.benchmarks.long_context.scoring import LongContextScorer
from freeweight.domain.aggregation import SampleGroup, aggregate_test
from freeweight.domain.benchmark import BenchmarkCase, MetricDefinition
from freeweight.domain.metrics import (
    DEFAULT_EFFECTIVE_CONTEXT_THRESHOLD,
    REASON_NO_CONTEXT_BASELINE,
    REASON_NO_CONTEXT_OBSERVATIONS,
    MeasurementClass,
    SampleFacts,
    effective_context_tokens,
)

_METRIC = MetricDefinition(
    key="effective_context_tokens",
    unit="tokens",
    higher_is_better=True,
    aggregation="max",
)


class TestAgainstTheConfiguredThreshold:
    """The same curve, two thresholds, two answers."""

    _CURVE = {2000: 1.0, 4000: 1.0, 8000: 0.9, 16000: 0.7, 32000: 0.3}

    def test_the_default_threshold_stops_at_the_last_length_above_eighty_percent(self) -> None:
        result = effective_context_tokens(self._CURVE, threshold_fraction=0.8)
        assert result.numeric_value == 8000.0  # noqa: PLR2004 — the answer is the assertion

    def test_a_looser_threshold_reaches_further(self) -> None:
        result = effective_context_tokens(self._CURVE, threshold_fraction=0.5)
        assert result.numeric_value == 16000.0  # noqa: PLR2004 — the answer is the assertion

    def test_a_threshold_of_one_admits_only_lengths_that_held_the_baseline(self) -> None:
        result = effective_context_tokens(self._CURVE, threshold_fraction=1.0)
        assert result.numeric_value == 4000.0  # noqa: PLR2004 — the answer is the assertion

    def test_the_baseline_is_the_shortest_length_not_the_best_one(self) -> None:
        # A model that peaks at 8K has still lost ground relative to where it started, and taking
        # the maximum as the baseline would hide that.
        curve = {2000: 0.5, 4000: 0.9, 8000: 0.45}
        assert effective_context_tokens(curve, threshold_fraction=0.8).numeric_value == 8000.0  # noqa: PLR2004

    def test_a_threshold_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="within 0.0..1.0"):
            effective_context_tokens(self._CURVE, threshold_fraction=1.5)
        with pytest.raises(ValueError, match="within 0.0..1.0"):
            effective_context_tokens(self._CURVE, threshold_fraction=-0.1)


class TestFailingAtDepthVersusFailingEverywhere:
    """The distinction the figure exists to draw."""

    def test_failing_at_depth_reports_where_it_stopped_working(self) -> None:
        curve = {2000: 1.0, 4000: 0.9, 8000: 0.2, 16000: 0.0}
        result = effective_context_tokens(
            curve, threshold_fraction=DEFAULT_EFFECTIVE_CONTEXT_THRESHOLD
        )
        assert result.numeric_value == 4000.0  # noqa: PLR2004 — the answer is the assertion
        assert result.unavailable_reason is None

    def test_failing_everywhere_reports_no_baseline_rather_than_a_small_context(self) -> None:
        curve = {2000: 0.0, 4000: 0.0, 8000: 0.0}
        result = effective_context_tokens(
            curve, threshold_fraction=DEFAULT_EFFECTIVE_CONTEXT_THRESHOLD
        )
        assert result.numeric_value is None
        assert result.unavailable_reason == REASON_NO_CONTEXT_BASELINE

    def test_an_empty_sweep_is_no_observation_rather_than_no_baseline(self) -> None:
        # Two different absences: "we measured nothing" and "we measured, and it was zero".
        result = effective_context_tokens({}, threshold_fraction=0.8)
        assert result.unavailable_reason == REASON_NO_CONTEXT_OBSERVATIONS

    def test_one_length_tested_is_its_own_effective_context(self) -> None:
        result = effective_context_tokens({4096: 0.6}, threshold_fraction=0.8)
        assert result.numeric_value == 4096.0  # noqa: PLR2004 — the answer is the assertion


def _facts(
    *, context_tokens: int | None, score: float | None, threshold: float = 0.8
) -> SampleFacts:
    detail: dict[str, object] = {"effective_context_threshold": threshold}
    if context_tokens is not None:
        detail["context_tokens"] = context_tokens
    return SampleFacts.from_row(
        {
            "status": "completed" if score is not None else "failed",
            "score": score,
            "result_json": detail,
        }
    )


class TestThroughAggregation:
    """The path a stored sample actually takes to the figure."""

    def _group(self, samples: list[SampleFacts]) -> SampleGroup:
        return SampleGroup(
            test_key="depth_sweep",
            run_test_id="rt",
            measurement_class=MeasurementClass.WARM,
            metrics=(_METRIC,),
            samples=samples,
        )

    def test_repetitions_at_one_length_are_averaged_before_the_threshold_is_applied(self) -> None:
        rows = aggregate_test(
            self._group(
                [
                    _facts(context_tokens=2000, score=1.0),
                    _facts(context_tokens=4000, score=1.0),
                    _facts(context_tokens=4000, score=0.0),
                    _facts(context_tokens=8000, score=0.0),
                ]
            )
        )
        # 4000 averages to 0.5, which is below 0.8 x 1.0, so the effective context is 2000.
        assert rows[0].numeric_value == 2000.0  # noqa: PLR2004 — the answer is the assertion
        assert rows[0].sample_count == 4  # noqa: PLR2004 — the count is the assertion

    def test_the_threshold_comes_from_the_samples_not_from_todays_configuration(self) -> None:
        rows = aggregate_test(
            self._group(
                [
                    _facts(context_tokens=2000, score=1.0, threshold=0.5),
                    _facts(context_tokens=4000, score=0.6, threshold=0.5),
                ]
            )
        )
        assert rows[0].numeric_value == 4000.0  # noqa: PLR2004 — the answer is the assertion

    def test_a_sample_with_no_context_length_is_excluded_rather_than_assigned_one(self) -> None:
        rows = aggregate_test(
            self._group(
                [_facts(context_tokens=2000, score=1.0), _facts(context_tokens=None, score=1.0)]
            )
        )
        assert rows[0].sample_count == 1
        assert rows[0].excluded_count == 1

    def test_a_failed_sample_is_excluded_and_stays_visible_in_the_counts(self) -> None:
        rows = aggregate_test(
            self._group(
                [_facts(context_tokens=2000, score=1.0), _facts(context_tokens=4000, score=None)]
            )
        )
        assert rows[0].numeric_value == 2000.0  # noqa: PLR2004 — the answer is the assertion
        assert rows[0].excluded_count == 1

    def test_no_usable_sample_yields_no_observation(self) -> None:
        rows = aggregate_test(self._group([_facts(context_tokens=None, score=1.0)]))
        assert rows[0].numeric_value is None
        assert rows[0].unavailable_reason == REASON_NO_CONTEXT_OBSERVATIONS


class TestTheScorerRecordsTheCoordinates:
    """A long-context sample is meaningless without the shape of the document behind it."""

    def _case(self, *, context_tokens: int, position: int, distractors: int) -> BenchmarkCase:
        return BenchmarkCase(
            case_id="c",
            ordinal=0,
            prompt="p",
            expectation={
                "exact": {"any_of": ["4718"], "contains": True},
                "long_context": {
                    "context_tokens": context_tokens,
                    "position_percent": position,
                    "distractor_count": distractors,
                },
            },
        )

    def test_the_sweep_ceiling_travels_beside_the_effective_context(self) -> None:
        # A model that did not fail anywhere the sweep looked has an effective context equal to
        # the longest length tested. Without the ceiling beside it, a reader would take that for
        # observed degradation rather than for the edge of the measurement.
        verdict = LongContextScorer().score(
            self._case(context_tokens=32000, position=50, distractors=0), "4718"
        )
        assert verdict.detail["longest_tested_context_tokens"] == 32000  # noqa: PLR2004

    def test_they_travel_with_the_verdict(self) -> None:
        verdict = LongContextScorer().score(
            self._case(context_tokens=8000, position=50, distractors=3), "4718"
        )
        assert verdict.score == 1.0
        assert verdict.detail["context_tokens"] == 8000  # noqa: PLR2004 — the value is the assertion
        assert verdict.detail["position_percent"] == 50  # noqa: PLR2004 — the value is the assertion
        assert verdict.detail["distractor_count"] == 3  # noqa: PLR2004 — the value is the assertion
        assert verdict.detail["effective_context_threshold"] == DEFAULT_EFFECTIVE_CONTEXT_THRESHOLD

    def test_they_travel_with_a_wrong_answer_too(self) -> None:
        verdict = LongContextScorer().score(
            self._case(context_tokens=8000, position=90, distractors=0), "no idea"
        )
        assert verdict.score == 0.0
        assert verdict.detail["context_tokens"] == 8000  # noqa: PLR2004 — the value is the assertion

    def test_an_unscoreable_case_still_records_which_depth_went_unmeasured(self) -> None:
        case = BenchmarkCase(
            case_id="c",
            ordinal=0,
            prompt="p",
            expectation={"long_context": {"context_tokens": 16000}},
        )
        verdict = LongContextScorer().score(case, "anything")
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"
        assert verdict.detail["context_tokens"] == 16000  # noqa: PLR2004 — the value is the assertion


class TestTheHaystack:
    """The document a sweep point is measured on."""

    _FILLER = ("Alpha beta gamma delta.", "Epsilon zeta eta theta.")

    def test_it_reaches_about_the_requested_size(self) -> None:
        built = assemble(
            filler=self._FILLER,
            facts=("The code is 4718.",),
            distractors=(),
            context_tokens=2000,
            position_percent=50,
        )
        assert 1900 <= built.context_tokens <= 2100  # noqa: PLR2004 — the tolerance is the assertion
        assert approximate_tokens(built.text) == built.context_tokens

    def test_the_needle_lands_where_the_case_asked(self) -> None:
        early = assemble(
            filler=self._FILLER,
            facts=("NEEDLE",),
            distractors=(),
            context_tokens=1000,
            position_percent=10,
        )
        late = assemble(
            filler=self._FILLER,
            facts=("NEEDLE",),
            distractors=(),
            context_tokens=1000,
            position_percent=90,
        )
        assert early.text.index("NEEDLE") < len(early.text) // 4
        assert late.text.index("NEEDLE") > 3 * len(late.text) // 4

    def test_two_facts_land_far_apart(self) -> None:
        built = assemble(
            filler=self._FILLER,
            facts=("FIRST_FACT", "SECOND_FACT"),
            distractors=(),
            context_tokens=1000,
            position_percent=10,
            second_position_percent=90,
        )
        assert (
            built.text.index("SECOND_FACT") - built.text.index("FIRST_FACT") > len(built.text) // 2
        )

    def test_expansion_is_deterministic(self) -> None:
        first = assemble(
            filler=self._FILLER,
            facts=("NEEDLE",),
            distractors=(),
            context_tokens=1000,
            position_percent=50,
        )
        second = assemble(
            filler=self._FILLER,
            facts=("NEEDLE",),
            distractors=(),
            context_tokens=1000,
            position_percent=50,
        )
        assert first.text == second.text

    def test_distractors_are_spread_rather_than_clustered(self) -> None:
        built = assemble(
            filler=self._FILLER,
            facts=("NEEDLE",),
            distractors=("DISTRACTOR_ONE", "DISTRACTOR_TWO", "DISTRACTOR_THREE"),
            context_tokens=2000,
            position_percent=50,
        )
        assert built.distractor_count == 3  # noqa: PLR2004 — the count is the assertion
        offsets = [
            built.text.index(name)
            for name in ("DISTRACTOR_ONE", "DISTRACTOR_TWO", "DISTRACTOR_THREE")
        ]
        assert offsets == sorted(offsets)
        assert offsets[-1] - offsets[0] > len(built.text) // 4

    def test_the_required_context_leaves_headroom_for_the_answer(self) -> None:
        built = assemble(
            filler=self._FILLER,
            facts=("NEEDLE",),
            distractors=(),
            context_tokens=1000,
            position_percent=50,
        )
        assert built.required_context_tokens > built.context_tokens

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"filler": ()}, "non-empty filler"),
            ({"facts": ()}, "at least one fact"),
            ({"context_tokens": 0}, "positive context_tokens"),
            ({"position_percent": 101}, "within 0..100"),
        ],
    )
    def test_a_document_that_would_measure_nothing_is_refused(
        self, kwargs: dict[str, object], message: str
    ) -> None:
        base: dict[str, object] = {
            "filler": self._FILLER,
            "facts": ("NEEDLE",),
            "distractors": (),
            "context_tokens": 1000,
            "position_percent": 50,
        }
        with pytest.raises(ValueError, match=message):
            assemble(**{**base, **kwargs})

    def test_the_token_approximation_is_declared_rather_than_measured(self) -> None:
        assert approximate_tokens("x" * (CHARACTERS_PER_TOKEN * 7)) == 7  # noqa: PLR2004
