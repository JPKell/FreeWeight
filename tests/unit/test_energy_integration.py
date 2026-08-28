"""Energy integration: real timestamps, irregular intervals, and refusals that are not zero.

Development plan, Phase 9: "Energy integrated over irregular intervals using real timestamps."
That is :class:`TestIrregularIntervals`, and it is the test the whole module exists for — a
sampler configured for 250 ms does not deliver samples 250 ms apart, and it drifts most while the
GPU is busiest, so multiplying by the nominal interval biases the total in the direction of the
thing being measured.

The rest covers what the catalog's per-request figures must refuse: a window with no power
readings, a window with readings but no interval to apply them over, and a denominator of zero
successes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from baseaicore import UNSUPPORTED

from freeweight.benchmarks.energy.benchmark import derive
from freeweight.benchmarks.energy.energy import (
    ENERGY_METHOD,
    JOULES_PER_KWH,
    REASON_NO_INTERVALS,
    REASON_NO_POWER_SAMPLES,
    REASON_NO_REQUESTS,
    REASON_NO_SUCCESSFUL_TASKS,
    EnergyEstimate,
    PowerSample,
    energy_per_output_token,
    energy_per_request,
    energy_per_successful_task,
    integrate_energy_joules,
    output_tokens_per_joule,
    peak_power_watts,
    successful_tasks_per_kwh,
)
from freeweight.domain.metrics import REASON_MULTI_GPU_PLACEMENT_UNKNOWN

_ORIGIN = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


def _series(*pairs: tuple[float, object]) -> list[PowerSample]:
    """Build a series from ``(seconds_from_origin, watts)`` pairs."""
    return [
        PowerSample(timestamp=_ORIGIN + timedelta(seconds=offset), power_watts=watts)  # type: ignore[arg-type]
        for offset, watts in pairs
    ]


class TestIrregularIntervals:
    """Each reading applies until the next real observation, never for a nominal period."""

    def test_hand_computed_total_over_uneven_gaps(self) -> None:
        # 100 W for 0.25 s, 200 W for 1.75 s, 50 W for 0.5 s; the last sample has no interval.
        #   100 × 0.25 = 25
        #   200 × 1.75 = 350
        #    50 × 0.50 = 25
        #   total      = 400 J
        estimate = integrate_energy_joules(
            _series((0.0, 100.0), (0.25, 200.0), (2.0, 50.0), (2.5, 10.0))
        )
        assert estimate.joules.numeric_value == pytest.approx(400.0)
        assert estimate.interval_count == 3
        assert estimate.window_seconds == pytest.approx(2.5)

    def test_the_nominal_interval_would_have_given_a_different_answer(self) -> None:
        # The same series at a nominal 250 ms would be (100 + 200 + 50 + 10) × 0.25 = 90 J, which
        # is not close to the 400 J the real timestamps give. This asserts the two differ, so a
        # regression to the nominal interval cannot pass.
        estimate = integrate_energy_joules(
            _series((0.0, 100.0), (0.25, 200.0), (2.0, 50.0), (2.5, 10.0))
        )
        nominal = (100.0 + 200.0 + 50.0 + 10.0) * 0.25
        assert estimate.joules.numeric_value != pytest.approx(nominal)

    def test_a_gap_in_the_series_is_integrated_over_the_gap_it_had(self) -> None:
        # A sampler that stalled for ten seconds really did leave the device drawing 300 W for
        # ten seconds as far as anything here knows, and shortening it would under-report.
        estimate = integrate_energy_joules(_series((0.0, 300.0), (10.0, 300.0), (11.0, 300.0)))
        assert estimate.joules.numeric_value == pytest.approx(300.0 * 10.0 + 300.0 * 1.0)

    def test_out_of_order_samples_are_sorted_before_integration(self) -> None:
        forward = integrate_energy_joules(_series((0.0, 100.0), (1.0, 200.0), (2.0, 50.0)))
        shuffled = integrate_energy_joules(_series((2.0, 50.0), (0.0, 100.0), (1.0, 200.0)))
        assert shuffled.joules.numeric_value == forward.joules.numeric_value

    def test_a_reading_with_no_power_contributes_no_interval_and_is_counted(self) -> None:
        # An unreadable sensor is not zero watts. The interval it would have covered is skipped,
        # and the sample shows up in excluded_count rather than dragging the total down.
        estimate = integrate_energy_joules(
            _series((0.0, 100.0), (1.0, UNSUPPORTED), (2.0, 100.0), (3.0, 100.0))
        )
        assert estimate.joules.numeric_value == pytest.approx(100.0 + 100.0)
        assert estimate.interval_count == 2
        assert estimate.excluded_count == 2

    def test_a_measured_zero_watt_window_is_a_measurement(self) -> None:
        estimate = integrate_energy_joules(_series((0.0, 0.0), (1.0, 0.0)))
        assert estimate.joules.numeric_value == 0.0


class TestRefusalsAreNotZero:
    """Every unavailable figure carries the reason, and none of them is ``0``."""

    def test_no_power_readings_at_all(self) -> None:
        estimate = integrate_energy_joules(_series((0.0, UNSUPPORTED), (1.0, UNSUPPORTED)))
        assert estimate.joules.numeric_value is None
        assert estimate.joules.unavailable_reason == REASON_NO_POWER_SAMPLES

    def test_one_reading_has_no_interval_after_it(self) -> None:
        estimate = integrate_energy_joules(_series((0.0, 250.0)))
        assert estimate.joules.numeric_value is None
        assert estimate.joules.unavailable_reason == REASON_NO_INTERVALS

    def test_an_empty_window(self) -> None:
        estimate = integrate_energy_joules([])
        assert estimate.joules.unavailable_reason == REASON_NO_POWER_SAMPLES

    def test_the_estimate_labels_itself(self) -> None:
        estimate = integrate_energy_joules(_series((0.0, 100.0), (1.0, 100.0)))
        assert estimate.is_estimate is True
        assert estimate.method == ENERGY_METHOD


class TestDerivedFigures:
    """The catalog's per-request, per-token and per-task figures, and their denominators."""

    @staticmethod
    def _estimate() -> EnergyEstimate:
        # 100 W for one second, then 100 W for one more: 200 J.
        return integrate_energy_joules(_series((0.0, 100.0), (1.0, 100.0), (2.0, 100.0)))

    def test_joules_per_request(self) -> None:
        assert energy_per_request(
            self._estimate(),
            requests=4,
        ).numeric_value == pytest.approx(50.0)

    def test_joules_per_output_token(self) -> None:
        assert energy_per_output_token(
            self._estimate(),
            output_tokens=1000.0,
        ).numeric_value == pytest.approx(0.2)

    def test_tokens_per_joule_is_the_reciprocal_read(self) -> None:
        assert output_tokens_per_joule(
            self._estimate(),
            output_tokens=1000.0,
        ).numeric_value == pytest.approx(5.0)

    def test_successful_tasks_per_kwh(self) -> None:
        assert successful_tasks_per_kwh(
            self._estimate(),
            successes=2,
        ).numeric_value == pytest.approx(2 / (200.0 / JOULES_PER_KWH))

    def test_zero_successes_is_not_zero_joules_per_success(self) -> None:
        # A suite that failed every task did not do so efficiently.
        result = energy_per_successful_task(self._estimate(), successes=0)
        assert result.numeric_value is None
        assert result.unavailable_reason == REASON_NO_SUCCESSFUL_TASKS

    def test_unreported_tokens_refuse_rather_than_divide(self) -> None:
        result = energy_per_output_token(
            self._estimate(),
            output_tokens=UNSUPPORTED,
        )
        assert result.unavailable_reason == REASON_NO_REQUESTS

    def test_peak_power(self) -> None:
        assert peak_power_watts(_series((0.0, 100.0), (1.0, 310.0), (2.0, 5.0))).numeric_value == (
            310.0
        )

    def test_peak_power_without_readings(self) -> None:
        assert peak_power_watts(_series((0.0, UNSUPPORTED))).unavailable_reason == (
            REASON_NO_POWER_SAMPLES
        )


class TestDeriveEmitsTheWholeSetOrNone:
    """The suite's run-level rows, and ADR-0027 §3's all-or-nothing refusal."""

    def test_single_gpu_rows_carry_the_device_and_the_counts(self) -> None:
        rows = derive(
            _series((0.0, 100.0), (1.0, 100.0), (2.0, 100.0)),
            requests=4,
            successes=3,
            output_tokens=1000.0,
            max_cpu_temperature_c=61.5,
            throttling_suspected=False,
            gpu_index=2,
        )
        by_key = {row.metric_key: row for row in rows}
        assert by_key["gpu_energy_joules"].numeric_value == pytest.approx(200.0)
        assert by_key["joules_per_request"].numeric_value == pytest.approx(50.0)
        assert by_key["max_cpu_temperature_c"].numeric_value == pytest.approx(61.5)
        assert by_key["throttling_suspected"].numeric_value == 0.0
        assert all(row.gpu_index == 2 for row in rows)
        assert by_key["gpu_energy_joules"].sample_count == 2

    def test_unknown_throttle_state_is_not_false(self) -> None:
        rows = derive(
            _series((0.0, 100.0), (1.0, 100.0)),
            requests=1,
            successes=1,
            throttling_suspected=None,
        )
        row = next(row for row in rows if row.metric_key == "throttling_suspected")
        assert row.numeric_value is None
        assert row.unavailable_reason == "throttle_state_unknown"

    def test_multi_gpu_without_placement_produces_no_numbers(self) -> None:
        rows = derive(
            _series((0.0, 100.0), (1.0, 100.0)),
            requests=1,
            successes=1,
            output_tokens=100.0,
            multi_gpu_visible=True,
            placement_known=False,
        )
        assert rows
        assert all(row.numeric_value is None for row in rows)
        assert {row.unavailable_reason for row in rows} == {REASON_MULTI_GPU_PLACEMENT_UNKNOWN}
