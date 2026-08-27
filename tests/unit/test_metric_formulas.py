"""Every metric formula, against known values, boundaries, division guards and ``UNSUPPORTED``.

Development plan, Phase 6: "Every metric formula with known values, boundaries, zero-division
guards and ``UNSUPPORTED`` inputs." Spec §18 says the same of the unit layer.

The formulas are pure functions over one sample's facts, so every case here is a literal in and a
literal out. What is being defended is not the arithmetic — it is that *no* input combination
produces a number that was not measured: a missing token count, a zero duration and a suite with
no successes must each yield ``UNSUPPORTED`` with a reason, never ``0``
([ADR-0016](../../../docs/adr/0016-unavailable-is-not-zero.md)).
"""

from __future__ import annotations

import pytest
from baseaicore import UNSUPPORTED, is_supported

from freeweight.domain.metrics import (
    REASON_CHUNKS_ARE_NOT_TOKENS,
    REASON_NO_OUTPUT_TOKENS,
    REASON_NO_SUCCESSES,
    REASON_NOT_REPORTED,
    REASON_NOT_STREAMED,
    REASON_ZERO_DURATION,
    MetricResult,
    SampleFacts,
    decode_tokens_per_second,
    inter_chunk_ms_mean,
    inter_chunk_ms_p50,
    inter_chunk_ms_p95,
    inter_token_ms_mean,
    output_tokens_per_success,
    percentile,
    prompt_tokens_per_second,
    quality_per_1k_output_tokens,
    rate_per_second,
    successes_per_million_output_tokens,
    total_tokens_per_success,
)


class TestMetricResultRefusesToBeAmbiguous:
    """A result carries a value or a reason. It is never allowed to carry both or neither."""

    def test_a_value_with_a_reason_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not carry an unavailable reason"):
            MetricResult(12.0, "not_reported")

    def test_an_unavailable_value_without_a_reason_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must say why"):
            MetricResult(UNSUPPORTED)

    def test_numeric_value_is_none_when_unavailable(self) -> None:
        assert MetricResult(UNSUPPORTED, REASON_NOT_REPORTED).numeric_value is None


class TestRatePerSecond:
    """The one division guard every throughput formula routes through."""

    def test_known_value(self) -> None:
        # 100 tokens in 500 ms is 200 tokens per second, exactly.
        assert rate_per_second(100, 500).numeric_value == pytest.approx(200.0)

    def test_a_zero_count_is_a_real_zero_rate(self) -> None:
        # Zero tokens in a measured duration is a measurement: the model produced nothing, and
        # that is not the same as "we could not tell".
        result = rate_per_second(0, 500)
        assert result.numeric_value == 0.0
        assert result.unavailable_reason is None

    @pytest.mark.parametrize("duration", [0, 0.0, -1.0])
    def test_a_non_positive_duration_never_becomes_a_rate(self, duration: float) -> None:
        result = rate_per_second(100, duration)
        assert not is_supported(result.value)
        assert result.unavailable_reason == REASON_ZERO_DURATION

    @pytest.mark.parametrize(
        ("count", "duration"),
        [(UNSUPPORTED, 500), (100, UNSUPPORTED), (UNSUPPORTED, UNSUPPORTED)],
    )
    def test_an_unreported_input_yields_no_rate(self, count: object, duration: object) -> None:
        result = rate_per_second(count, duration)  # type: ignore[arg-type]
        assert result.numeric_value is None
        assert result.unavailable_reason == REASON_NOT_REPORTED


class TestThroughputFormulas:
    """Prompt and decode throughput divide the provider's counts by the provider's own durations."""

    def test_prompt_throughput_uses_the_backend_prompt_time(self) -> None:
        facts = SampleFacts(input_tokens=2048, backend_prompt_eval_ms=1024, client_wall_ms=99_999)
        # The client's 99 999 ms is deliberately absurd: if it leaked into this formula the
        # assertion below would fail, which is the point of writing it that way.
        assert prompt_tokens_per_second(facts).numeric_value == pytest.approx(2000.0)

    def test_decode_throughput_uses_the_backend_decode_time(self) -> None:
        facts = SampleFacts(output_tokens=64, backend_decode_ms=2000)
        assert decode_tokens_per_second(facts).numeric_value == pytest.approx(32.0)

    def test_a_provider_that_reports_no_counts_yields_no_throughput(self) -> None:
        facts = SampleFacts(backend_decode_ms=1000)
        assert decode_tokens_per_second(facts).unavailable_reason == REASON_NOT_REPORTED


class TestPercentile:
    """Interpolated percentiles, and the two inputs that have no answer at all."""

    def test_single_value_is_its_own_every_percentile(self) -> None:
        assert percentile([7.0], 0.0) == 7.0
        assert percentile([7.0], 0.95) == 7.0

    def test_interpolates_between_neighbours(self) -> None:
        assert percentile([0.0, 10.0], 0.5) == pytest.approx(5.0)
        assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)

    def test_boundaries_are_the_extremes(self) -> None:
        values = [5.0, 1.0, 9.0]
        assert percentile(values, 0.0) == 1.0
        assert percentile(values, 1.0) == 9.0

    def test_no_percentile_of_nothing(self) -> None:
        with pytest.raises(ValueError, match="at least one value"):
            percentile([], 0.5)

    @pytest.mark.parametrize("fraction", [-0.01, 1.01])
    def test_a_fraction_outside_the_range_is_refused(self, fraction: float) -> None:
        with pytest.raises(ValueError, match="within 0.0..1.0"):
            percentile([1.0], fraction)


class TestStreamingLatency:
    """Chunk latency exists whenever a stream had two deltas. Token latency needs a declaration."""

    @staticmethod
    def _streamed(*gaps: float, token_level: bool = False) -> SampleFacts:
        return SampleFacts(detail={"inter_chunk_ms": list(gaps), "token_level_chunks": token_level})

    def test_mean_median_and_p95_over_known_gaps(self) -> None:
        facts = self._streamed(10.0, 20.0, 30.0, 40.0)
        assert inter_chunk_ms_mean(facts).numeric_value == pytest.approx(25.0)
        assert inter_chunk_ms_p50(facts).numeric_value == pytest.approx(25.0)
        assert inter_chunk_ms_p95(facts).numeric_value == pytest.approx(38.5)

    def test_a_non_streamed_sample_has_no_chunk_latency(self) -> None:
        result = inter_chunk_ms_mean(SampleFacts())
        assert result.numeric_value is None
        assert result.unavailable_reason == REASON_NOT_STREAMED

    def test_a_single_delta_has_no_gap_after_it(self) -> None:
        # One delta means zero gaps. Reporting 0 ms would claim an instantaneous second token
        # that never arrived.
        assert inter_chunk_ms_mean(self._streamed()).unavailable_reason == REASON_NOT_STREAMED

    def test_token_latency_is_refused_when_a_chunk_is_not_a_token(self) -> None:
        result = inter_token_ms_mean(self._streamed(10.0, 20.0, token_level=False))
        assert result.numeric_value is None
        assert result.unavailable_reason == REASON_CHUNKS_ARE_NOT_TOKENS

    def test_token_latency_is_the_chunk_figure_when_a_chunk_is_a_token(self) -> None:
        facts = self._streamed(10.0, 20.0, token_level=True)
        assert inter_token_ms_mean(facts).numeric_value == pytest.approx(15.0)
        assert inter_chunk_ms_mean(facts).numeric_value == pytest.approx(15.0)


class TestPerSuccessFormulas:
    """Benchmark catalog §3.3's derived figures, including the case where nothing succeeded."""

    def test_output_tokens_per_success(self) -> None:
        assert output_tokens_per_success(900, 3).numeric_value == pytest.approx(300.0)

    def test_total_tokens_per_success_counts_both_directions(self) -> None:
        assert total_tokens_per_success(300, 900, 3).numeric_value == pytest.approx(400.0)

    @pytest.mark.parametrize("successes", [0, -1])
    def test_no_successes_is_never_zero_cost(self, successes: int) -> None:
        result = output_tokens_per_success(900, successes)
        assert result.numeric_value is None
        assert result.unavailable_reason == REASON_NO_SUCCESSES

    def test_quality_per_1k_output_tokens(self) -> None:
        assert quality_per_1k_output_tokens(0.8, 2000).numeric_value == pytest.approx(0.4)

    def test_successes_per_million_output_tokens(self) -> None:
        result = successes_per_million_output_tokens(4, 500_000)
        assert result.numeric_value == pytest.approx(8.0)

    @pytest.mark.parametrize("tokens", [0, -5])
    def test_a_per_token_figure_needs_tokens(self, tokens: int) -> None:
        assert quality_per_1k_output_tokens(1.0, tokens).unavailable_reason == (
            REASON_NO_OUTPUT_TOKENS
        )
        assert successes_per_million_output_tokens(1, tokens).unavailable_reason == (
            REASON_NO_OUTPUT_TOKENS
        )

    def test_unreported_totals_yield_no_figure(self) -> None:
        assert output_tokens_per_success(UNSUPPORTED, 3).unavailable_reason == REASON_NOT_REPORTED
        assert (
            total_tokens_per_success(UNSUPPORTED, 900, 3).unavailable_reason == REASON_NOT_REPORTED
        )
        assert (
            quality_per_1k_output_tokens(UNSUPPORTED, 100).unavailable_reason == REASON_NOT_REPORTED
        )
        assert (
            successes_per_million_output_tokens(1, UNSUPPORTED).unavailable_reason
            == REASON_NOT_REPORTED
        )


class TestSampleFactsFromRow:
    """A ``NULL`` column becomes ``UNSUPPORTED``, which is the sentinel arithmetic refuses."""

    def test_null_columns_become_unsupported(self) -> None:
        facts = SampleFacts.from_row({"status": "failed", "output_tokens": None})
        assert facts.status == "failed"
        assert not is_supported(facts.output_tokens)

    def test_reported_columns_become_numbers(self) -> None:
        facts = SampleFacts.from_row({"output_tokens": 12, "result_json": {"a": 1}})
        assert facts.output_tokens == 12.0
        assert facts.detail == {"a": 1}

    def test_a_non_mapping_result_json_is_ignored_rather_than_crashing_a_metric(self) -> None:
        assert SampleFacts.from_row({"result_json": "not-an-object"}).detail == {}
