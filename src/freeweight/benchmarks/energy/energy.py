"""freeweight.benchmarks.energy.energy — energy from power samples, and what it buys per token.

Benchmark catalog §3.14, as pure functions over readings the caller has already loaded. No
collector, no database, no clock: the samples arrive as ``(timestamp, watts)`` pairs, which is what
lets integration over deliberately irregular intervals be asserted against a hand-computed answer.

**It is an estimate, and it says so in the type.** :class:`EnergyEstimate` carries
``method`` and ``is_estimate`` on every instance, and there is no constructor here that produces
one without them. The catalog's phrasing — "always labelled a telemetry-derived estimate, never
instrumentation" — is a property of the value rather than a caption a UI may forget to render.

**Real timestamps, never the nominal interval.** A sampler configured for 250 ms does not deliver
samples 250 ms apart: it drifts under load, and it drifts *most* exactly while the GPU is busy,
which is when the power is highest. Multiplying every reading by the nominal interval therefore
biases the total in a direction that correlates with the thing being measured.
:func:`integrate_energy_joules` uses each sample's own timestamp and the next one's, so an
irregular series integrates correctly and a series with a gap in it integrates over the gap it
actually had.

**A sample with no power reading is skipped, not zeroed.** A missing sensor reading contributes no
interval and is counted in ``excluded_count``; treating it as zero watts would say the device was
idle for that interval, which is the fabricated measurement ADR-0016 forbids.

**One device, never a machine total.** Every function here takes one device's series. There is no
machine-wide GPU energy figure in this system, and none can be assembled from these parts
(ADR-0027 §5).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baseaicore import UNSUPPORTED, Measurement, is_supported

from freeweight.domain.metrics import MetricResult, unavailable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

__all__ = [
    "ENERGY_METHOD",
    "JOULES_PER_KWH",
    "REASON_NEGATIVE_ENERGY",
    "REASON_NO_INTERVALS",
    "REASON_NO_POWER_SAMPLES",
    "REASON_NO_REQUESTS",
    "REASON_NO_REQUEST_OVERLAP",
    "REASON_NO_SUCCESSFUL_TASKS",
    "EnergyEstimate",
    "PowerSample",
    "energy_per_output_token",
    "energy_per_request",
    "energy_per_successful_task",
    "integrate_energy_joules",
    "merge_windows",
    "output_tokens_per_joule",
    "peak_power_watts",
    "successful_tasks_per_kwh",
]

ENERGY_METHOD = "telemetry_left_rectangle_sum"
"""How every joule figure in this application was arrived at.

Stated as a value and carried on the estimate, because "estimate" is a claim about *method* and a
consumer that cannot see the method cannot judge the claim. Left-rectangle rather than trapezoid,
matching :meth:`sweatmeter.TelemetryWindow.energy_joules` exactly: two integrations of one series
that disagreed by a rule nobody chose would be worse than either."""

JOULES_PER_KWH = 3_600_000.0
"""Joules in one kilowatt-hour — 1 000 W × 3 600 s."""

REASON_NO_POWER_SAMPLES = "no_power_samples"
"""No observation in the window reported this device's power draw."""

REASON_NO_INTERVALS = "no_integrable_intervals"
"""Power was reported, but never with a following sample to give it a duration.

A single reading has no interval after it, and inventing one from the configured sampler period
would be exactly the nominal-interval bias this module exists to avoid."""

REASON_NEGATIVE_ENERGY = "non_monotonic_timestamps"
"""The series ran backwards. A negative duration is a storage or clock defect, never a reading."""

REASON_NO_REQUESTS = "no_requests"
"""A per-request figure was asked for over a window in which nothing was requested."""

REASON_NO_REQUEST_OVERLAP = "no_power_samples_inside_a_request"
"""Power was reported and requests were made, but no interval overlapped one.

The usual cause is a sampler interval longer than the requests: a 1 s sampler over 200 ms requests
can miss every one of them. It is a real refusal rather than a zero — nothing was measured *while
work was happening*, which is a different fact from "the device drew no power"."""

REASON_NO_SUCCESSFUL_TASKS = "no_successful_tasks"
"""A per-success figure was asked for and nothing succeeded.

A suite that failed every task did not do so efficiently, and dividing by zero successes to reach
"0 J per success" would say exactly that (the same rule as ``output_tokens_per_success``)."""


@dataclass(frozen=True, slots=True)
class PowerSample:
    """One device power reading at one instant.

    Attributes:
        timestamp: When the reading was taken. Timezone-aware, UTC — the persisted telemetry
            column's own type, so nothing here has to normalize anything.
        power_watts: The device's draw, or :data:`~baseaicore.UNSUPPORTED` where the sensor was
            unreadable at that instant.
    """

    timestamp: datetime
    power_watts: Measurement = UNSUPPORTED


@dataclass(frozen=True, slots=True)
class EnergyEstimate:
    """Energy over one window for one device, inseparable from how it was arrived at.

    Attributes:
        joules: The integrated total, or ``UNSUPPORTED`` with the reason there is none.
        gpu_index: The device this describes. Always one device (ADR-0027 §5).
        interval_count: How many ``(sample, next sample)`` intervals contributed.
        excluded_count: How many samples contributed nothing — an unreadable sensor, or the final
            sample, which has no interval after it.
        window_seconds: Elapsed time the integrated intervals covered. A total over 4 s of a 60 s
            run is a very different claim from one over 59 s of it, and this is the field that
            says which.
        method: :data:`ENERGY_METHOD`.
        is_estimate: Always ``True``. Present so that a serializer, a template or an export cannot
            emit this number without the label the catalog requires beside it.
    """

    joules: MetricResult
    gpu_index: int
    interval_count: int
    excluded_count: int
    window_seconds: float
    method: str = ENERGY_METHOD
    is_estimate: bool = True


def merge_windows(
    windows: Sequence[tuple[datetime, datetime]],
) -> tuple[tuple[datetime, datetime], ...]:
    """Return ``windows`` as a disjoint, ordered union.

    Overlapping requests would otherwise have their shared seconds counted twice, which on a
    concurrent run would inflate energy by however much the requests overlapped. Zero-length and
    backwards windows are dropped: a sample that was never sent has no window, and that is not the
    same as one whose window is an instant.

    Args:
        windows: ``(start, end)`` pairs, in any order.

    Returns:
        Non-overlapping windows in ascending order, possibly empty.
    """
    ordered = sorted((begin, end) for begin, end in windows if end > begin)
    merged: list[tuple[datetime, datetime]] = []
    for begin, end in ordered:
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((begin, end))
    return tuple(merged)


def _overlap_seconds(
    begin: datetime, end: datetime, windows: Sequence[tuple[datetime, datetime]]
) -> float:
    """Seconds of the interval ``[begin, end]`` that fall inside a disjoint ``windows``."""
    total = 0.0
    for start, stop in windows:
        if stop <= begin:
            continue
        if start >= end:
            break
        total += (min(end, stop) - max(begin, start)).total_seconds()
    return total


def integrate_energy_joules(
    samples: Sequence[PowerSample],
    *,
    gpu_index: int = 0,
    windows: Sequence[tuple[datetime, datetime]] | None = None,
) -> EnergyEstimate:
    """Integrate one device's power series into joules using the samples' own timestamps.

    ``Σ(power_watts × dt_seconds)`` where each ``dt`` is the *real* elapsed time to the next
    observation. Samples are sorted by timestamp first, so telemetry persisted out of order cannot
    produce a negative interval; a series whose timestamps still run backwards after sorting is
    impossible, and a duplicate timestamp contributes a zero-length interval rather than an error.

    **With ``windows``, each interval is clipped to the union of them** and only the overlapping
    seconds are integrated, so the result is energy spent *on requests* rather than over the run's
    whole wall-clock span — which includes the idle settle wait, the warm-up generations and the
    inter-test cooldowns. Clipping rather than filtering is the point: a reading taken inside a
    request whose next reading falls after it would otherwise carry the whole idle gap at the
    request's power level, which is the largest error the naive version makes.

    Args:
        samples: The device's readings, in any order.
        gpu_index: The device the readings came from, carried onto the estimate.
        windows: Request windows to restrict the integration to, in any order and possibly
            overlapping — they are merged. ``None`` integrates the whole series.

    Returns:
        The estimate. ``UNSUPPORTED`` with :data:`REASON_NO_POWER_SAMPLES` when no reading carried
        a wattage, with :data:`REASON_NO_INTERVALS` when readings existed but none had a following
        sample to give it a duration, and with :data:`REASON_NO_REQUEST_OVERLAP` when ``windows``
        was given and no interval fell inside one. A genuinely measured zero-watt window integrates
        to ``0.0``, which is a measurement and is returned as one.
    """
    bounds = None if windows is None else merge_windows(windows)
    ordered = sorted(samples, key=lambda sample: sample.timestamp)
    reported = [sample for sample in ordered if is_supported(sample.power_watts)]
    if not reported:
        return EnergyEstimate(
            joules=unavailable(REASON_NO_POWER_SAMPLES),
            gpu_index=gpu_index,
            interval_count=0,
            excluded_count=len(ordered),
            window_seconds=0.0,
        )
    terms: list[float] = []
    covered = 0.0
    for position, sample in enumerate(ordered[:-1]):
        if not is_supported(sample.power_watts):
            continue
        following = ordered[position + 1].timestamp
        seconds = (following - sample.timestamp).total_seconds()
        if seconds < 0:  # pragma: no cover — sorting above makes this unreachable
            return EnergyEstimate(
                joules=unavailable(REASON_NEGATIVE_ENERGY),
                gpu_index=gpu_index,
                interval_count=0,
                excluded_count=len(ordered),
                window_seconds=0.0,
            )
        if bounds is not None:
            seconds = _overlap_seconds(sample.timestamp, following, bounds)
            if seconds <= 0.0:
                continue
        terms.append(float(sample.power_watts) * seconds)
        covered += seconds
    if not terms:
        return EnergyEstimate(
            joules=unavailable(REASON_NO_REQUEST_OVERLAP if bounds else REASON_NO_INTERVALS),
            gpu_index=gpu_index,
            interval_count=0,
            excluded_count=len(ordered),
            window_seconds=0.0,
        )
    return EnergyEstimate(
        joules=MetricResult(math.fsum(terms)),
        gpu_index=gpu_index,
        interval_count=len(terms),
        excluded_count=len(ordered) - len(terms),
        window_seconds=covered,
    )


def peak_power_watts(samples: Sequence[PowerSample]) -> MetricResult:
    """The highest reported draw in one device's series.

    Returns:
        The maximum, or ``UNSUPPORTED`` with :data:`REASON_NO_POWER_SAMPLES` when no sample
        reported one. The mean lives on :class:`~sweatmeter.TelemetryWindow`; the peak is here
        because the catalog asks for both and a peak is what a power-limit question is about.
    """
    reported = [float(sample.power_watts) for sample in samples if is_supported(sample.power_watts)]
    if not reported:
        return unavailable(REASON_NO_POWER_SAMPLES)
    return MetricResult(max(reported))


def _per_unit(estimate: EnergyEstimate, count: float, reason: str) -> MetricResult:
    """Divide an energy estimate by a count, refusing a zero or negative denominator."""
    if estimate.joules.numeric_value is None:
        return estimate.joules
    if count <= 0:
        return unavailable(reason)
    return MetricResult(estimate.joules.numeric_value / count)


def energy_per_request(estimate: EnergyEstimate, *, requests: int) -> MetricResult:
    """Joules per provider request made inside the measured window."""
    return _per_unit(estimate, float(requests), REASON_NO_REQUESTS)


def energy_per_output_token(
    estimate: EnergyEstimate, *, output_tokens: Measurement
) -> MetricResult:
    """Joules per generated token.

    Args:
        estimate: The window's energy.
        output_tokens: Tokens the provider reported generating in that window, or
            :data:`~baseaicore.UNSUPPORTED` where it reported none.

    Returns:
        The figure, or ``UNSUPPORTED`` — with the estimate's own reason where there is no energy,
        and with :data:`REASON_NO_REQUESTS` where no tokens were produced or counted.
    """
    if not is_supported(output_tokens):
        return unavailable(REASON_NO_REQUESTS)
    return _per_unit(estimate, float(output_tokens), REASON_NO_REQUESTS)


def energy_per_successful_task(estimate: EnergyEstimate, *, successes: int) -> MetricResult:
    """Joules per successfully completed task."""
    return _per_unit(estimate, float(successes), REASON_NO_SUCCESSFUL_TASKS)


def output_tokens_per_joule(
    estimate: EnergyEstimate, *, output_tokens: Measurement
) -> MetricResult:
    """Generated tokens per joule — the catalog's "tokens per joule", the reciprocal read.

    Returns:
        The figure, or ``UNSUPPORTED`` when there is no energy estimate, when the estimate is zero
        or negative joules (a reciprocal of zero is not "infinitely efficient"), or when no tokens
        were reported.
    """
    if estimate.joules.numeric_value is None:
        return estimate.joules
    if estimate.joules.numeric_value <= 0:
        return unavailable(REASON_NO_INTERVALS)
    if not is_supported(output_tokens):
        return unavailable(REASON_NO_REQUESTS)
    return MetricResult(float(output_tokens) / estimate.joules.numeric_value)


def successful_tasks_per_kwh(estimate: EnergyEstimate, *, successes: int) -> MetricResult:
    """Successfully completed tasks per kilowatt-hour of device energy."""
    if estimate.joules.numeric_value is None:
        return estimate.joules
    if estimate.joules.numeric_value <= 0:
        return unavailable(REASON_NO_INTERVALS)
    return MetricResult(successes / (estimate.joules.numeric_value / JOULES_PER_KWH))
