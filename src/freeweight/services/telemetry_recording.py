"""freeweight.services.telemetry_recording — telemetry for the duration of one run.

Three jobs, all of them about making a measurement honest about the conditions it was taken in.

**Persistence.** :class:`TelemetryRecorder` runs one :class:`~sweatmeter.TelemetrySampler` for as
long as a run executes and writes every observation as one host row plus one row per visible GPU
(ADR-0027 §4). It starts when the run starts and stops when the run stops — telemetry outside a
run belongs to nothing that could be read back against a measurement (spec §10), so there is no
way to ask this module to record without a ``run_id``.

**Calibration.** :func:`calibrate_sampling_overhead` measures what the sampler itself costs and
stores the result on the run (``runs.telemetry_overhead_percent``). Spec §15 budgets the sampling
effect on measured throughput at ≤ 1 %, "measured and recorded per run" — an assumption that is
never checked is not a budget, and a run that paid more than that should be able to say so years
later.

**Idle detection.** :func:`wait_for_idle` implements the wait spec §13 defines, and returns what it
observed rather than a bare boolean, because the caller has to record the numbers whichever way it
decides. This module never decides ``warn`` versus ``refuse``; that is the run engine's, because
the outcome of ``refuse`` is a failed run.

Every device figure names its device. There is no function here that returns a machine-wide GPU
number, and :func:`summarize_gpu_telemetry` refuses to produce one when more than one GPU is
visible and the provider cannot say which holds the model (ADR-0027 §3).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baseaicore import UNSUPPORTED, Measurement, is_supported, monotonic_ns
from sweatmeter import TelemetrySampler, TelemetryWindow
from sweatmeter.types import GpuSample, TelemetrySnapshot

from freeweight.domain.metrics import REASON_MULTI_GPU_PLACEMENT_UNKNOWN, MetricResult, unavailable
from freeweight.infrastructure.db.repositories.telemetry import TelemetryRepository

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from types import TracebackType

    from sweatmeter import TelemetryCollector

    from freeweight.services.database import Database

__all__ = [
    "GpuSeries",
    "IdleOutcome",
    "OverheadCalibration",
    "TelemetryRecorder",
    "TelemetrySeries",
    "TelemetrySummary",
    "calibrate_sampling_overhead",
    "load_series",
    "load_window",
    "summarize_gpu_telemetry",
    "wait_for_idle",
]

logger = logging.getLogger(__name__)

_MS_PER_SECOND = 1000.0


def _number(value: Measurement) -> float | None:
    """Collapse a measurement to the plain nullable column the telemetry tables keep.

    ``NULL`` here means "this platform did not report it", which is exactly what
    :data:`~baseaicore.UNSUPPORTED` means; unlike a metric, a telemetry row needs no per-field
    reason, because the whole row is a snapshot of what the machine would say at that instant.
    """
    return float(value) if is_supported(value) else None


def _integer(value: Measurement) -> int | None:
    """Collapse a measurement to a plain nullable integer column."""
    return int(value) if is_supported(value) else None


def _host_columns(snapshot: TelemetrySnapshot) -> dict[str, Any]:
    """Render one snapshot's host fields as ``telemetry_samples`` column values."""
    return {
        "cpu_percent": _number(snapshot.cpu_percent),
        "load_average_1m": _number(snapshot.load_average_1m),
        "ram_used_bytes": _integer(snapshot.ram_used_bytes),
        "ram_available_bytes": _integer(snapshot.ram_available_bytes),
        "ram_total_bytes": _integer(snapshot.ram_total_bytes),
        "cpu_temperature_c": _number(snapshot.cpu_temperature_c),
        "disk_read_bytes_per_sec": _number(snapshot.disk_read_bytes_per_sec),
        "disk_write_bytes_per_sec": _number(snapshot.disk_write_bytes_per_sec),
        "process_rss_bytes": _integer(snapshot.process_rss_bytes),
    }


def _gpu_columns(gpu: GpuSample) -> dict[str, Any]:
    """Render one device's fields as ``telemetry_gpu_samples`` column values."""
    return {
        "gpu_index": gpu.index,
        "gpu_uuid": gpu.uuid,
        "gpu_utilization_percent": _number(gpu.utilization_percent),
        "gpu_memory_utilization_percent": _number(gpu.memory_utilization_percent),
        "vram_used_bytes": _integer(gpu.vram_used_bytes),
        "vram_total_bytes": _integer(gpu.vram_total_bytes),
        "gpu_temperature_c": _number(gpu.temperature_c),
        "gpu_memory_temperature_c": _number(gpu.memory_temperature_c),
        "gpu_power_watts": _number(gpu.power_watts),
        "gpu_power_limit_watts": _number(gpu.power_limit_watts),
        "gpu_fan_percent": _number(gpu.fan_percent),
        "gpu_core_clock_mhz": _number(gpu.core_clock_mhz),
        "gpu_memory_clock_mhz": _number(gpu.memory_clock_mhz),
        "throttle_reasons_json": list(gpu.throttle_reasons),
        "throttle_reasons_available": gpu.throttle_reasons_available,
    }


class TelemetryRecorder:
    """Persists telemetry for exactly as long as one run is executing.

    Used as a context manager by the run engine. Entering starts a sampler; leaving stops it and
    waits for the worker thread, so no row can be written after the run has been marked terminal.

    A write failure is logged and dropped, never raised: telemetry is provenance *about* a
    measurement, and losing an observation must not fail the run that observation was describing.
    The count of rows actually written is available from :attr:`written`, so a run that recorded
    nothing is visible rather than assumed.

    Args:
        database: The application's database handle.
        run_id: The run being observed.
        collector: The telemetry collector to sample from.
        interval_seconds: Delay between observations.
        enabled: ``False`` makes every method a no-op, which is what
            ``telemetry.persist_during_runs = false`` means. Constructed either way so the run
            engine has no conditional around its ``with`` statement.
    """

    __slots__ = ("_database", "_enabled", "_run_id", "_sampler", "_written")

    def __init__(
        self,
        database: Database,
        run_id: str,
        collector: TelemetryCollector,
        *,
        interval_seconds: float,
        enabled: bool = True,
    ) -> None:
        """Configure the sampler without starting its thread."""
        self._database = database
        self._run_id = run_id
        self._enabled = enabled
        self._written = 0
        self._sampler = TelemetrySampler(
            collector, interval_seconds=interval_seconds, on_sample=self._record
        )

    @property
    def written(self) -> int:
        """How many host observations this recorder has committed."""
        return self._written

    def _record(self, snapshot: TelemetrySnapshot) -> None:
        """Write one observation. Never raises — see the class docstring."""
        try:
            with self._database.write() as session:
                TelemetryRepository().insert_sample(
                    session,
                    run_id=self._run_id,
                    timestamp=snapshot.timestamp,
                    host=_host_columns(snapshot),
                    gpus=[_gpu_columns(gpu) for gpu in snapshot.gpus],
                )
            self._written += 1
        except Exception:  # noqa: BLE001 — a lost observation must not fail the run it describes
            logger.warning("telemetry.record_failed", extra={"run_id": self._run_id})

    def start(self) -> None:
        """Begin sampling, unless persistence is disabled."""
        if self._enabled:
            self._sampler.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop sampling and wait for the worker thread. Safe whether or not it was started."""
        self._sampler.stop(timeout=timeout)

    def __enter__(self) -> TelemetryRecorder:
        """Start sampling and return this recorder."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop sampling, whatever the run did."""
        self.stop()


@dataclass(frozen=True, slots=True)
class OverheadCalibration:
    """What the telemetry sampler costs, as a share of its own interval.

    Attributes:
        mean_collect_ms: Mean wall time one collection took, measured with
            :func:`~baseaicore.monotonic_ns`.
        interval_ms: The configured sampling interval.
        overhead_percent: ``mean_collect_ms / interval_ms × 100`` — the share of elapsed time the
            sampler occupies, which is the quantity spec §15 budgets at ≤ 1 %.
        observations: How many collections the mean was taken over.
    """

    mean_collect_ms: float
    interval_ms: float
    overhead_percent: float
    observations: int


def calibrate_sampling_overhead(
    collector: TelemetryCollector, *, interval_ms: float, observations: int = 5
) -> OverheadCalibration:
    """Measure what this machine's telemetry collection costs, before a run starts.

    **What is measured, and why this and not a before/after throughput comparison.** The sampler
    is a thread that wakes every ``interval_ms`` and spends ``mean_collect_ms`` collecting; the
    share of wall time it occupies is the ratio of the two, and it is a property of the machine
    that can be measured directly and repeatably. Timing a workload twice — once with sampling and
    once without — would measure the machine's noise at least as much as the sampler, and would
    have to run the workload twice to produce a number whose whole purpose is to say the first run
    was not distorted.

    Runs before the first provider call, so the calibration itself is outside the window it
    describes.

    Args:
        collector: The collector a run's recorder will use. Called for real: an estimate from a
            different collector would not describe this machine.
        interval_ms: The configured sampling interval, in milliseconds.
        observations: How many collections to time. At least one.

    Returns:
        The calibration, ready to store on the run.

    Raises:
        ValueError: ``interval_ms`` is not positive, or ``observations`` is below 1. A zero
            interval has no share to compute and would divide by zero.
    """
    if interval_ms <= 0:
        raise ValueError(f"interval_ms must be positive; got {interval_ms!r}.")
    if observations < 1:
        raise ValueError(f"observations must be at least 1; got {observations!r}.")
    durations: list[float] = []
    for _ in range(observations):
        started = monotonic_ns()
        collector.snapshot()
        durations.append((monotonic_ns() - started) / 1_000_000.0)
    mean = sum(durations) / len(durations)
    return OverheadCalibration(
        mean_collect_ms=mean,
        interval_ms=interval_ms,
        overhead_percent=mean / interval_ms * 100.0,
        observations=len(durations),
    )


@dataclass(frozen=True, slots=True)
class IdleOutcome:
    """What the idle check observed, whatever the caller decides to do about it.

    Attributes:
        idle: Whether the machine settled below the threshold for the required run of samples.
        disabled: Whether the check was switched off (``idle_gpu_threshold_percent = 0``), in
            which case ``idle`` is ``True`` and means "not checked", not "verified quiet".
        threshold_percent: The threshold that was applied.
        observed_gpu_percent: The busiest GPU utilization seen while waiting, or ``UNSUPPORTED``
            on a machine with no GPU telemetry.
        observed_cpu_percent: The busiest host CPU utilization seen while waiting.
        consecutive_idle_samples: The longest run of consecutive quiet samples achieved.
        samples_taken: How many observations the wait made.
        waited_seconds: How long it waited.
    """

    idle: bool
    disabled: bool
    threshold_percent: float
    observed_gpu_percent: Measurement = UNSUPPORTED
    observed_cpu_percent: Measurement = UNSUPPORTED
    consecutive_idle_samples: int = 0
    samples_taken: int = 0
    waited_seconds: float = 0.0

    def as_detail(self) -> dict[str, Any]:
        """Render the observed numbers for a degradation record or an error's ``details``."""
        return {
            "threshold_percent": self.threshold_percent,
            "gpu_utilization_percent": _number(self.observed_gpu_percent),
            "cpu_percent": _number(self.observed_cpu_percent),
            "consecutive_idle_samples": self.consecutive_idle_samples,
            "samples_taken": self.samples_taken,
            "waited_seconds": round(self.waited_seconds, 3),
        }


def _busiest_gpu_percent(snapshot: TelemetrySnapshot) -> Measurement:
    """Return the highest utilization across visible devices, or ``UNSUPPORTED``.

    A *maximum*, not a mean, and this is the one place a figure crosses devices — deliberately, and
    it is not a measurement: "is anything on this machine busy?" is answered by the busiest device,
    and averaging two GPUs would let one saturated device hide behind an idle one. No number
    derived here is ever stored as a measurement of the machine (ADR-0027).
    """
    reported = [
        float(gpu.utilization_percent)
        for gpu in snapshot.gpus
        if is_supported(gpu.utilization_percent)
    ]
    return max(reported) if reported else UNSUPPORTED


def wait_for_idle(  # noqa: PLR0913 — every argument is a documented configuration value
    collector: TelemetryCollector,
    *,
    threshold_percent: float,
    required_samples: int,
    timeout_seconds: float,
    poll_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> IdleOutcome:
    """Wait for GPU and CPU to fall below ``threshold_percent``, and report what was seen.

    Spec §13: the run waits up to ``timeout_seconds`` for both to sit below the threshold for
    ``required_samples`` consecutive observations. This function does the waiting and the
    observing; it never decides what happens when the wait fails, because ``warn`` records a
    degradation and ``refuse`` fails the run, and only the run engine can do either.

    A machine that reports **no** utilization at all — no GPU telemetry, no host reader — is
    treated as quiet: an unreadable sensor is not evidence of contention, and refusing to run on
    every machine without ``nvidia-smi`` would make the check a platform restriction rather than a
    hygiene measure. The absence is visible in the outcome's ``UNSUPPORTED`` fields.

    Args:
        collector: The collector to observe through.
        threshold_percent: The utilization below which the machine counts as quiet. ``0`` disables
            the check entirely and returns immediately with ``disabled=True``.
        required_samples: How many consecutive quiet observations are needed.
        timeout_seconds: How long to wait before giving up.
        poll_seconds: Delay between observations.
        sleep: The sleep to use; injected so a test does not wait in real time.
        monotonic: The duration clock; injected for the same reason.

    Returns:
        The outcome, with the busiest utilization observed during the wait.
    """
    if threshold_percent <= 0:
        return IdleOutcome(idle=True, disabled=True, threshold_percent=threshold_percent)
    started = monotonic()
    consecutive = 0
    best_run = 0
    taken = 0
    worst_gpu: Measurement = UNSUPPORTED
    worst_cpu: Measurement = UNSUPPORTED
    while True:
        snapshot = collector.snapshot()
        taken += 1
        gpu = _busiest_gpu_percent(snapshot)
        cpu = snapshot.cpu_percent
        if is_supported(gpu) and (not is_supported(worst_gpu) or gpu > worst_gpu):
            worst_gpu = gpu
        if is_supported(cpu) and (not is_supported(worst_cpu) or cpu > worst_cpu):
            worst_cpu = cpu
        busy = (is_supported(gpu) and gpu >= threshold_percent) or (
            is_supported(cpu) and cpu >= threshold_percent
        )
        consecutive = 0 if busy else consecutive + 1
        best_run = max(best_run, consecutive)
        elapsed = monotonic() - started
        if consecutive >= required_samples:
            return IdleOutcome(
                idle=True,
                disabled=False,
                threshold_percent=threshold_percent,
                observed_gpu_percent=worst_gpu,
                observed_cpu_percent=worst_cpu,
                consecutive_idle_samples=best_run,
                samples_taken=taken,
                waited_seconds=elapsed,
            )
        if elapsed >= timeout_seconds:
            return IdleOutcome(
                idle=False,
                disabled=False,
                threshold_percent=threshold_percent,
                observed_gpu_percent=worst_gpu,
                observed_cpu_percent=worst_cpu,
                consecutive_idle_samples=best_run,
                samples_taken=taken,
                waited_seconds=elapsed,
            )
        sleep(poll_seconds)


@dataclass(frozen=True, slots=True)
class TelemetrySummary:
    """The derived figures one run's telemetry supports, each naming its device.

    Every field is a :class:`~freeweight.domain.metrics.MetricResult`, so a figure this machine
    could not produce carries the reason rather than a zero. ``gpu_index`` is on the summary and
    not on each field because a summary describes one device by construction — there is no
    machine-wide GPU figure in this system (ADR-0027 §5).
    """

    gpu_index: int
    sample_count: int
    peak_vram_bytes: MetricResult
    mean_power_watts: MetricResult
    energy_joules: MetricResult
    max_temperature_c: MetricResult
    throttling_suspected: bool | None = None

    def metric_results(self) -> dict[str, MetricResult]:
        """The four numeric figures by metric key, ready for ``metric_values`` rows."""
        return {
            "peak_vram_bytes": self.peak_vram_bytes,
            "mean_gpu_power_watts": self.mean_power_watts,
            "gpu_energy_joules": self.energy_joules,
            "max_gpu_temperature_c": self.max_temperature_c,
        }


def _snapshot_from_rows(host: Any, gpus: Sequence[Any]) -> TelemetrySnapshot:  # noqa: ANN401 — ORM
    """Rebuild one SweatMeter snapshot from its stored rows.

    A ``NULL`` column becomes :data:`~baseaicore.UNSUPPORTED` again, which is what lets
    :class:`~sweatmeter.TelemetryWindow` apply exactly the same "how many samples actually
    supported this metric" logic to stored telemetry as it does to live telemetry. Reading from
    storage rather than from an in-memory buffer is also what makes a *resumed* run summarizable:
    the process that recorded the first half may no longer exist.
    """

    def measured(value: object) -> Measurement:
        return UNSUPPORTED if value is None or isinstance(value, bool) else float(value)  # type: ignore[arg-type]  # a nullable numeric column is a number or None

    return TelemetrySnapshot(
        timestamp=host.timestamp,
        cpu_percent=measured(host.cpu_percent),
        load_average_1m=measured(host.load_average_1m),
        ram_used_bytes=measured(host.ram_used_bytes),
        ram_available_bytes=measured(host.ram_available_bytes),
        ram_total_bytes=measured(host.ram_total_bytes),
        cpu_temperature_c=measured(host.cpu_temperature_c),
        disk_read_bytes_per_sec=measured(host.disk_read_bytes_per_sec),
        disk_write_bytes_per_sec=measured(host.disk_write_bytes_per_sec),
        process_rss_bytes=measured(host.process_rss_bytes),
        gpus=tuple(
            GpuSample(
                index=row.gpu_index,
                uuid=row.gpu_uuid,
                utilization_percent=measured(row.gpu_utilization_percent),
                memory_utilization_percent=measured(row.gpu_memory_utilization_percent),
                vram_used_bytes=measured(row.vram_used_bytes),
                vram_total_bytes=measured(row.vram_total_bytes),
                temperature_c=measured(row.gpu_temperature_c),
                memory_temperature_c=measured(row.gpu_memory_temperature_c),
                power_watts=measured(row.gpu_power_watts),
                power_limit_watts=measured(row.gpu_power_limit_watts),
                fan_percent=measured(row.gpu_fan_percent),
                core_clock_mhz=measured(row.gpu_core_clock_mhz),
                memory_clock_mhz=measured(row.gpu_memory_clock_mhz),
                throttle_reasons=tuple(str(item) for item in (row.throttle_reasons_json or ())),
                throttle_reasons_available=bool(row.throttle_reasons_available),
            )
            for row in gpus
        ),
    )


def load_window(database: Database, run_id: str) -> TelemetryWindow:
    """Rebuild a :class:`~sweatmeter.TelemetryWindow` over one run's persisted telemetry."""
    with database.read() as session:
        repository = TelemetryRepository()
        hosts = repository.list_for_run(session, run_id)
        by_sample: dict[str, list[Any]] = {}
        for row in repository.list_gpu_for_run(session, run_id):
            by_sample.setdefault(row.telemetry_sample_id, []).append(row)
        return TelemetryWindow(
            [_snapshot_from_rows(host, by_sample.get(host.id, [])) for host in hosts]
        )


def summarize_gpu_telemetry(
    window: TelemetryWindow, *, gpu_index: int, multi_gpu_visible: bool, placement_known: bool
) -> TelemetrySummary:
    """Derive one device's figures from a run's telemetry window.

    Args:
        window: The run's observations.
        gpu_index: The device the run's metrics are attributed to (``execution.gpu_index``).
        multi_gpu_visible: Whether more than one GPU was visible during the run.
        placement_known: Whether the provider reported which device holds the model.

    Returns:
        The summary. When more than one GPU is visible and placement is unknown, every memory and
        energy figure is unavailable with ``multi_gpu_placement_unknown`` rather than attributed to
        a guess — a VRAM figure read from the wrong device is a wrong number, not an approximate
        one (ADR-0027 §3). Temperature and power are refused on the same grounds and for the same
        reason: they describe whichever device was doing the work, and that is exactly what is in
        doubt.
    """
    if multi_gpu_visible and not placement_known:
        refusal = unavailable(REASON_MULTI_GPU_PLACEMENT_UNKNOWN)
        return TelemetrySummary(
            gpu_index=gpu_index,
            sample_count=window.sample_count(),
            peak_vram_bytes=refusal,
            mean_power_watts=refusal,
            energy_joules=refusal,
            max_temperature_c=refusal,
            throttling_suspected=None,
        )
    verdict = window.suspected_throttling(gpu_index)
    return TelemetrySummary(
        gpu_index=gpu_index,
        sample_count=window.sample_count(),
        peak_vram_bytes=_result(window.peak_vram_bytes(gpu_index)),
        mean_power_watts=_result(window.mean_power_watts(gpu_index)),
        energy_joules=_result(window.energy_joules(gpu_index)),
        max_temperature_c=_result(window.max_temperature_c(gpu_index)),
        throttling_suspected=verdict.suspected,
    )


def _result(value: Measurement) -> MetricResult:
    """Wrap one window figure, giving an unavailable one the reason storage requires."""
    return MetricResult(value) if is_supported(value) else unavailable("no_gpu_telemetry")


@dataclass(frozen=True, slots=True)
class GpuSeries:
    """One device's series through a run, ready to plot.

    Attributes:
        gpu_index: The device. Every series names one; there is no combined series (ADR-0027 §5).
        gpu_uuid: The device's stable identifier, where the collector read one.
        utilization_percent: Utilization per observation; ``None`` where it was not readable.
        vram_used_bytes: Used device memory per observation.
        power_watts: Device power draw per observation.
        temperature_c: Device temperature per observation.
    """

    gpu_index: int
    gpu_uuid: str | None
    utilization_percent: tuple[float | None, ...]
    vram_used_bytes: tuple[float | None, ...]
    power_watts: tuple[float | None, ...]
    temperature_c: tuple[float | None, ...]


@dataclass(frozen=True, slots=True)
class TelemetrySeries:
    """A run's persisted telemetry as parallel series, for the run page's charts.

    ``timestamps`` and every series share one index, so a chart can plot any of them against the
    same axis without joining anything. A ``None`` in a series is a reading this platform could not
    take at that instant, and a chart draws a gap there — never a zero, which would read as an idle
    machine (ADR-0016, UI standards §5).
    """

    timestamps: tuple[datetime, ...]
    cpu_percent: tuple[float | None, ...]
    ram_used_bytes: tuple[float | None, ...]
    gpus: tuple[GpuSeries, ...]

    @property
    def sample_count(self) -> int:
        """How many observations the run recorded."""
        return len(self.timestamps)


def load_series(database: Database, run_id: str) -> TelemetrySeries:
    """Load one run's telemetry as plottable series.

    Reads the host rows once and the device rows once, then indexes the device rows by their
    parent — rather than one query per device, which on a machine that adds a GPU next month would
    quietly become two.

    Args:
        database: The application's database handle.
        run_id: The run to read.

    Returns:
        The series. Empty — and honestly so — for a run that recorded no telemetry.
    """
    with database.read() as session:
        repository = TelemetryRepository()
        hosts = repository.list_for_run(session, run_id)
        positions = {host.id: index for index, host in enumerate(hosts)}
        per_device: dict[int, list[Any | None]] = {}
        uuids: dict[int, str | None] = {}
        for row in repository.list_gpu_for_run(session, run_id):
            column = per_device.setdefault(row.gpu_index, [None] * len(hosts))
            uuids.setdefault(row.gpu_index, row.gpu_uuid)
            index = positions.get(row.telemetry_sample_id)
            if index is not None:
                column[index] = row
        return TelemetrySeries(
            timestamps=tuple(host.timestamp for host in hosts),
            cpu_percent=tuple(host.cpu_percent for host in hosts),
            ram_used_bytes=tuple(
                None if host.ram_used_bytes is None else float(host.ram_used_bytes)
                for host in hosts
            ),
            gpus=tuple(
                GpuSeries(
                    gpu_index=index,
                    gpu_uuid=uuids.get(index),
                    utilization_percent=tuple(
                        None if row is None else row.gpu_utilization_percent
                        for row in per_device[index]
                    ),
                    vram_used_bytes=tuple(
                        None
                        if row is None or row.vram_used_bytes is None
                        else float(row.vram_used_bytes)
                        for row in per_device[index]
                    ),
                    power_watts=tuple(
                        None if row is None else row.gpu_power_watts for row in per_device[index]
                    ),
                    temperature_c=tuple(
                        None if row is None else row.gpu_temperature_c for row in per_device[index]
                    ),
                )
                for index in sorted(per_device)
            ),
        )
