"""KV-cache arithmetic: hand-computed values, refused guesses, and a slope fitted to a known line.

Development plan, Phase 9: "KV theory against hand-computed values; missing architecture fields ⇒
``unsupported``, not a wrong number; hybrid architectures flagged and excluded from the formula"
and "VRAM slope fit on synthetic data; OOM path recorded as a measurement (max context), not a
failure."

Every expected number below is written as the arithmetic that produced it rather than as a
literal, so a reader can check the formula against benchmark catalog §3.2 without running
anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from baseaicore import UNSUPPORTED, is_supported

from freeweight.benchmarks.memory_kv.benchmark import SampleWindow, derive, stabilized_vram
from freeweight.benchmarks.memory_kv.kv import (
    REASON_HYBRID_ARCHITECTURE,
    REASON_MISSING_ARCHITECTURE,
    REASON_NO_COLD_PREFILL,
    REASON_NO_THEORETICAL_BASELINE,
    REASON_SINGLE_CONTEXT_POINT,
    REASON_UNKNOWN_KV_PRECISION,
    REASON_VRAM_DID_NOT_GROW,
    ContextObservation,
    KvArchitecture,
    bytes_per_element_for,
    cache_reuse_speedup,
    fit_context_slope,
    is_hybrid,
    kv_overhead_ratio,
    max_context_capped_by_configuration,
    max_successful_context_tokens,
    observed_mb_per_1k_context,
    theoretical_kv_bytes_per_token,
)
from freeweight.domain.metrics import REASON_MULTI_GPU_PLACEMENT_UNKNOWN


def _architecture(**overrides: object) -> KvArchitecture:
    base = {
        "layers": 32.0,
        "kv_heads": 8.0,
        "head_dim": 128.0,
        "architecture": "transformer",
        "kv_cache_precision": "f16",
    }
    base.update(overrides)
    return KvArchitecture(**base)  # type: ignore[arg-type]


class TestTheoreticalAgainstHandComputedValues:
    """``2 × layers × kv_heads × head_dim × bytes_per_element``, checked by hand."""

    def test_grouped_query_model_at_f16(self) -> None:
        # 2 × 32 layers × 8 kv heads × 128 head dim × 2 bytes = 131 072 bytes per token.
        result = theoretical_kv_bytes_per_token(_architecture())
        assert result.numeric_value == 2 * 32 * 8 * 128 * 2

    def test_halving_the_precision_halves_the_cost(self) -> None:
        f16 = theoretical_kv_bytes_per_token(_architecture(kv_cache_precision="f16"))
        q8 = theoretical_kv_bytes_per_token(_architecture(kv_cache_precision="q8_0"))
        assert f16.numeric_value is not None
        assert q8.numeric_value == pytest.approx(f16.numeric_value / 2)

    def test_multi_head_attention_costs_the_head_count(self) -> None:
        # Without grouped-query attention kv_heads equals attention_heads; a 64-head model at the
        # same layer count and head dim costs eight times the 8-kv-head model above.
        result = theoretical_kv_bytes_per_token(_architecture(kv_heads=64.0))
        assert result.numeric_value == 2 * 32 * 64 * 128 * 2

    @pytest.mark.parametrize(
        ("precision", "expected"),
        [("f32", 4.0), ("f16", 2.0), ("bf16", 2.0), ("q8_0", 1.0), ("q4_0", 0.5)],
    )
    def test_element_sizes(self, precision: str, expected: float) -> None:
        assert bytes_per_element_for(precision).numeric_value == expected


class TestMissingFieldsRefuseRatherThanGuess:
    """A field the provider did not report produces no number at all (ADR-0016)."""

    @pytest.mark.parametrize("field_name", ["layers", "kv_heads", "head_dim"])
    def test_each_missing_field_is_unsupported(self, field_name: str) -> None:
        result = theoretical_kv_bytes_per_token(_architecture(**{field_name: UNSUPPORTED}))
        assert not is_supported(result.value)
        assert result.unavailable_reason == REASON_MISSING_ARCHITECTURE

    def test_a_zero_field_is_also_refused(self) -> None:
        # Zero layers is not a model; it is a normalizer that wrote a default.
        result = theoretical_kv_bytes_per_token(_architecture(layers=0.0))
        assert result.unavailable_reason == REASON_MISSING_ARCHITECTURE

    def test_unknown_precision_is_not_assumed_to_be_f16(self) -> None:
        result = theoretical_kv_bytes_per_token(_architecture(kv_cache_precision="q6_k_xl"))
        assert result.unavailable_reason == REASON_UNKNOWN_KV_PRECISION

    def test_unset_precision_is_not_assumed_either(self) -> None:
        result = theoretical_kv_bytes_per_token(_architecture(kv_cache_precision=None))
        assert result.unavailable_reason == REASON_UNKNOWN_KV_PRECISION


class TestHybridArchitecturesAreFlaggedAndExcluded:
    """A state-space model is refused, not forced through the transformer formula."""

    @pytest.mark.parametrize(
        "architecture", ["mamba", "Mamba2", "jamba-1.5", "rwkv", "hybrid", "state_space", "SSM"]
    )
    def test_recognized_as_hybrid(self, architecture: str) -> None:
        assert is_hybrid(architecture)

    @pytest.mark.parametrize("architecture", ["transformer", "llama", "qwen3", None])
    def test_not_hybrid(self, architecture: str | None) -> None:
        assert not is_hybrid(architecture)

    def test_hybrid_is_excluded_even_with_every_field_present(self) -> None:
        # The fields are all there; the formula still does not describe this architecture, and a
        # number produced from them would be confidently wrong rather than approximately right.
        result = theoretical_kv_bytes_per_token(_architecture(architecture="mamba2"))
        assert not is_supported(result.value)
        assert result.unavailable_reason == REASON_HYBRID_ARCHITECTURE


class TestSlopeFitOnSyntheticData:
    """A line with a known gradient is recovered exactly; noise shows up in the fit quality."""

    def test_exact_line_recovers_its_gradient(self) -> None:
        # 4 GiB of weights plus 100 bytes per token of context.
        base, per_token = 4 * 1024**3, 100.0
        observations = [
            ContextObservation(context_tokens=size, vram_used_bytes=base + per_token * size)
            for size in (1024, 2048, 4096, 8192, 16384)
        ]
        fit = fit_context_slope(observations)
        assert fit.slope_bytes_per_token.numeric_value == pytest.approx(per_token)
        assert fit.intercept_bytes.numeric_value == pytest.approx(base)
        assert fit.r_squared.numeric_value == pytest.approx(1.0)
        assert fit.sample_count == len(observations)

    def test_noise_lowers_the_reported_fit_quality(self) -> None:
        # Phase 9's named risk is slope noise from other processes. It must be visible in
        # r_squared rather than averaged into the slope and forgotten.
        clean = [
            ContextObservation(context_tokens=size, vram_used_bytes=1000.0 + 10.0 * size)
            for size in (1000, 2000, 3000, 4000)
        ]
        noisy = [
            ContextObservation(
                context_tokens=item.context_tokens,
                vram_used_bytes=item.vram_used_bytes + offset,
            )
            for item, offset in zip(clean, (0.0, 9000.0, -9000.0, 0.0), strict=True)
        ]
        assert fit_context_slope(clean).r_squared.numeric_value == pytest.approx(1.0)
        noisy_fit = fit_context_slope(noisy)
        assert noisy_fit.r_squared.numeric_value is not None
        assert noisy_fit.r_squared.numeric_value < 1.0
        assert noisy_fit.residual_stddev_bytes.numeric_value is not None

    def test_one_observation_is_not_a_slope(self) -> None:
        fit = fit_context_slope([ContextObservation(context_tokens=1024, vram_used_bytes=1.0)])
        assert fit.slope_bytes_per_token.unavailable_reason == REASON_SINGLE_CONTEXT_POINT
        assert fit.slope_bytes_per_token.numeric_value is None

    def test_no_spread_on_the_context_axis_is_not_a_zero_slope(self) -> None:
        observations = [
            ContextObservation(context_tokens=4096, vram_used_bytes=value)
            for value in (1000.0, 1100.0, 1200.0)
        ]
        fit = fit_context_slope(observations)
        assert fit.slope_bytes_per_token.unavailable_reason == REASON_VRAM_DID_NOT_GROW

    def test_mb_per_1k_context_restates_the_same_slope(self) -> None:
        # 1 MiB per 1 024 tokens is 1024 bytes per token.
        restated = observed_mb_per_1k_context(
            fit_context_slope(
                [
                    ContextObservation(context_tokens=0, vram_used_bytes=0.0),
                    ContextObservation(context_tokens=1024, vram_used_bytes=1024.0 * 1024.0),
                ]
            ).slope_bytes_per_token
        )
        assert restated.numeric_value == pytest.approx(1.0)

    def test_an_unavailable_slope_carries_its_reason_through(self) -> None:
        fit = fit_context_slope(())
        assert observed_mb_per_1k_context(fit.slope_bytes_per_token).unavailable_reason == (
            REASON_SINGLE_CONTEXT_POINT
        )


class TestOverheadRatio:
    """Observed over theoretical, and an honest refusal on either side."""

    def test_ratio_of_hand_computed_inputs(self) -> None:
        observed = fit_context_slope(
            [
                ContextObservation(context_tokens=0, vram_used_bytes=0.0),
                ContextObservation(context_tokens=1000, vram_used_bytes=160_000.0),
            ]
        ).slope_bytes_per_token
        theoretical = theoretical_kv_bytes_per_token(
            _architecture(layers=1.0, kv_heads=32.0, head_dim=64.0, kv_cache_precision="q8_0")
        )
        # 2 × 1 × 32 × 64 × 1 = 4 096 theoretical; observed 160 bytes per token.
        assert theoretical.numeric_value == 4096
        assert kv_overhead_ratio(observed, theoretical).numeric_value == pytest.approx(160 / 4096)

    def test_missing_theory_carries_its_own_reason(self) -> None:
        observed = fit_context_slope(
            [
                ContextObservation(context_tokens=0, vram_used_bytes=0.0),
                ContextObservation(context_tokens=100, vram_used_bytes=100.0),
            ]
        ).slope_bytes_per_token
        theoretical = theoretical_kv_bytes_per_token(_architecture(architecture="mamba"))
        assert kv_overhead_ratio(observed, theoretical).unavailable_reason == (
            REASON_HYBRID_ARCHITECTURE
        )

    def test_zero_theory_is_refused_rather_than_divided_by(self) -> None:
        from freeweight.domain.metrics import MetricResult

        assert (
            kv_overhead_ratio(MetricResult(100.0), MetricResult(0.0)).unavailable_reason
            == REASON_NO_THEORETICAL_BASELINE
        )


class TestMaximumContextFitRecordsTheOom:
    """An out-of-memory rejection is the measurement, never a failed run."""

    def test_the_largest_success_is_the_answer(self) -> None:
        attempts = [(8192, True), (16384, True), (32768, True), (65536, False), (131072, False)]
        assert max_successful_context_tokens(attempts).numeric_value == 32768

    def test_refusals_alone_produce_no_maximum(self) -> None:
        # A model that failed at every length has no maximum context; reporting the smallest
        # length tried would claim it worked there.
        result = max_successful_context_tokens([(8192, False), (16384, False)])
        assert result.numeric_value is None
        assert result.unavailable_reason == REASON_SINGLE_CONTEXT_POINT

    def test_a_configured_ceiling_caps_the_reported_maximum(self) -> None:
        attempts = [(8192, True), (16384, True)]
        assert max_successful_context_tokens(attempts, configured_limit=8192).numeric_value == 8192


class TestCacheReuse:
    """Cold prefill over warm prefill, refusing a division that would fabricate a speed-up."""

    def test_speedup_is_the_ratio(self) -> None:
        assert cache_reuse_speedup(800.0, 50.0).numeric_value == pytest.approx(16.0)

    @pytest.mark.parametrize(
        ("cold", "warm"), [(UNSUPPORTED, 50.0), (800.0, UNSUPPORTED), (800.0, 0.0)]
    )
    def test_missing_or_zero_input_is_refused(self, cold: object, warm: object) -> None:
        result = cache_reuse_speedup(cold, warm)  # type: ignore[arg-type]
        assert result.unavailable_reason == REASON_NO_COLD_PREFILL


class TestStabilizedVram:
    """A reading is attributed to a sample only when it fell inside that sample's window."""

    @staticmethod
    def _window(start: int, end: int) -> SampleWindow:
        origin = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
        return SampleWindow(
            context_tokens=4096,
            started_at=origin + timedelta(seconds=start),
            ended_at=origin + timedelta(seconds=end),
            succeeded=True,
        )

    @staticmethod
    def _timestamps(count: int) -> list[datetime]:
        origin = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
        return [origin + timedelta(seconds=index) for index in range(count)]

    def test_takes_the_peak_inside_the_window(self) -> None:
        # The KV cache is largest at the end of a generation; a mean would under-report what the
        # allocator was actually asked for.
        value = stabilized_vram(
            self._window(1, 3), self._timestamps(6), [10.0, 20.0, 50.0, 30.0, 90.0, 10.0]
        )
        assert value == 50.0

    def test_no_reading_inside_the_window_is_unsupported_not_zero(self) -> None:
        value = stabilized_vram(self._window(10, 12), self._timestamps(3), [1.0, 2.0, 3.0])
        assert not is_supported(value)

    def test_unreadable_samples_inside_the_window_are_skipped(self) -> None:
        value = stabilized_vram(self._window(0, 2), self._timestamps(3), [None, 7.0, None])
        assert value == 7.0


class TestDeriveRefusesOnUnknownPlacement:
    """ADR-0027 §3: every figure, not some of them."""

    def test_multi_gpu_without_placement_produces_no_numbers(self) -> None:
        rows = derive(
            architecture=_architecture(),
            observations=[
                ContextObservation(context_tokens=1024, vram_used_bytes=1.0),
                ContextObservation(context_tokens=2048, vram_used_bytes=2.0),
            ],
            attempts=[(8192, True)],
            cold_prefill_ms=800.0,
            warm_prefill_ms=50.0,
            gpu_index=0,
            multi_gpu_visible=True,
            placement_known=False,
        )
        assert rows
        assert all(row.numeric_value is None for row in rows)
        assert {row.unavailable_reason for row in rows} == {REASON_MULTI_GPU_PLACEMENT_UNKNOWN}

    def test_single_gpu_produces_the_whole_set_with_counts(self) -> None:
        rows = derive(
            architecture=_architecture(),
            observations=[
                ContextObservation(context_tokens=size, vram_used_bytes=1000.0 + 100.0 * size)
                for size in (1024, 2048, 4096)
            ],
            attempts=[(8192, True), (16384, False)],
            cold_prefill_ms=800.0,
            warm_prefill_ms=50.0,
            gpu_index=1,
        )
        by_key = {row.metric_key: row for row in rows}
        assert by_key["observed_kv_bytes_per_token"].numeric_value == pytest.approx(100.0)
        assert by_key["max_successful_context_tokens"].numeric_value == 8192
        assert by_key["max_successful_context_tokens"].excluded_count == 1
        assert by_key["reuse_speedup"].numeric_value == pytest.approx(16.0)
        assert all(row.gpu_index == 1 for row in rows)


class TestCappedByConfiguration:
    """``PHASE9_ISSUES.md`` §7: the ceiling and the model's limit report the same number.

    Without a companion saying which is which, a reader comparing two models sees two identical
    maxima and cannot tell that one of them was never actually established.
    """

    def test_stopping_at_the_ceiling_is_flagged(self) -> None:
        result = max_context_capped_by_configuration(
            [(8192, True), (16384, True), (32768, True)], configured_limit=32768
        )

        assert result.value == 1.0

    def test_a_refusal_below_the_ceiling_is_the_models_own_limit(self) -> None:
        result = max_context_capped_by_configuration(
            [(8192, True), (16384, True), (32768, False)], configured_limit=131072
        )

        assert result.value == 0.0

    def test_no_ceiling_at_all_is_never_capped(self) -> None:
        assert max_context_capped_by_configuration([(8192, True)]).value == 0.0

    def test_nothing_succeeded_has_no_maximum_to_qualify(self) -> None:
        """``UNSUPPORTED``, not ``0.0``: there is no maximum for the question to be about."""
        result = max_context_capped_by_configuration([(8192, False)], configured_limit=32768)

        assert result.value is UNSUPPORTED
        assert result.unavailable_reason == REASON_SINGLE_CONTEXT_POINT

    def test_the_two_metrics_agree_about_the_same_sweep(self) -> None:
        attempts = [(8192, True), (16384, True), (32768, True)]

        largest = max_successful_context_tokens(attempts, configured_limit=32768)
        capped = max_context_capped_by_configuration(attempts, configured_limit=32768)

        assert largest.value == 32768
        assert capped.value == 1.0, "the number is the ceiling and must say so"
