"""freeweight.services.telemetry — the one sampler the application owns, and its wire shapes.

A single :class:`TelemetryService` is built once at server startup (the web lifespan) and shared by
every request: the telemetry bar's SSE stream, ``GET /api/v1/system/status`` and the
``gpu_telemetry`` health component all read its ``latest()`` cache rather than each triggering
their own live collection, which is what keeps a page load from paying ``nvidia-smi``'s cost (up
to 120 ms per SweatMeter's own performance budget) on every request.

Telemetry is never persisted here — SweatMeter itself owns no storage (spec §10), and persisting
samples outside a run is explicitly deferred to Phase 6. The SSE stream is therefore live-only: a
reconnecting client gets the current snapshot going forward, not a gap-free replay of what it
missed, which is the one respect in which this stream does not need everything API and Contract
Standards §8 asks of a *persisted* event stream (run events, Phase 5, replay from ``Last-Event-ID``
against the event store).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from baseaicore import UnsupportedPlatformError, canonical_json, new_id, utc_now
from baseaicore.timeutil import to_rfc3339
from setspec.envelope import GeneratorInfo, SchemaVersion, dump_envelope
from sweatmeter import TelemetryCollector, TelemetrySampler
from sweatmeter.platform import NullHostReader, create_host_reader

from freeweight.__about__ import __version__

if TYPE_CHECKING:
    from baseaicore import MachineProfile
    from baseaicore.timeutil import Clock
    from sweatmeter import GpuSample, TelemetrySnapshot

__all__ = [
    "TelemetryService",
    "build_collector",
    "format_heartbeat",
    "format_sample_event",
    "snapshot_to_json",
]

_GENERATOR = GeneratorInfo(name="freeweight", version=__version__)
_EVENT_SCHEMA_VERSION = SchemaVersion(1, 0)


def build_collector() -> TelemetryCollector:
    """Build a :class:`~sweatmeter.TelemetryCollector` for this platform.

    Degrades to :class:`~sweatmeter.platform.NullHostReader` rather than raising when the host
    platform has no reader — the public degrade path SweatMeter spec §13 documents for exactly this
    case ("consumers construct a ``NullHostReader`` and degrade"), which keeps a build on a tier-3
    platform serving with every host field honestly ``UNSUPPORTED`` instead of failing to start.

    Returns:
        A collector for the current process. GPU telemetry is read through the collector's own
        default backend selection (NVML when available, else ``nvidia-smi``, else none).
    """
    try:
        host = create_host_reader()
    except UnsupportedPlatformError:
        host = NullHostReader()
    return TelemetryCollector(host=host)


def _canonical_dict(value: object) -> dict[str, Any]:
    """Round-trip a structure through canonical JSON into a plain, JSON-safe ``dict``.

    ``json.loads`` is typed to return ``Any``; the cast is safe because :func:`canonical_json`
    only ever produces a JSON object at the top level for the mapping inputs this module passes it.
    """
    return cast("dict[str, Any]", json.loads(canonical_json(value)))


def _gpu_json(gpu: GpuSample) -> dict[str, Any]:
    """Render one live GPU sample as JSON, ``UNSUPPORTED`` fields included honestly."""
    return _canonical_dict(
        {
            "index": gpu.index,
            "uuid": gpu.uuid,
            "utilization_percent": gpu.utilization_percent,
            "memory_utilization_percent": gpu.memory_utilization_percent,
            "vram_used_bytes": gpu.vram_used_bytes,
            "vram_total_bytes": gpu.vram_total_bytes,
            "temperature_c": gpu.temperature_c,
            "memory_temperature_c": gpu.memory_temperature_c,
            "power_watts": gpu.power_watts,
            "power_limit_watts": gpu.power_limit_watts,
            "fan_percent": gpu.fan_percent,
            "core_clock_mhz": gpu.core_clock_mhz,
            "memory_clock_mhz": gpu.memory_clock_mhz,
            "throttle_reasons": list(gpu.throttle_reasons),
            "throttle_reasons_available": gpu.throttle_reasons_available,
        }
    )


def snapshot_to_json(snapshot: TelemetrySnapshot) -> dict[str, Any]:
    """Render a live snapshot as the JSON shape the telemetry bar and SSE stream consume.

    Every :data:`~baseaicore.measurement.Measurement` field renders as a number or the fixed string
    ``"unsupported"`` (ADR-0016 §4) — never ``null``, never ``0`` — so the client can tell "not
    measurable, here is why" from "genuinely zero" without its own sentinel logic.

    Args:
        snapshot: The snapshot to render.

    Returns:
        A JSON-safe mapping: every host field, the ``gpus`` list, and ``unavailable_reasons`` for
        the fields that could not be read this sample.
    """
    return _canonical_dict(
        {
            "timestamp": snapshot.timestamp,
            "cpu_percent": snapshot.cpu_percent,
            "load_average_1m": snapshot.load_average_1m,
            "ram_used_bytes": snapshot.ram_used_bytes,
            "ram_available_bytes": snapshot.ram_available_bytes,
            "ram_total_bytes": snapshot.ram_total_bytes,
            "cpu_temperature_c": snapshot.cpu_temperature_c,
            "disk_read_bytes_per_sec": snapshot.disk_read_bytes_per_sec,
            "disk_write_bytes_per_sec": snapshot.disk_write_bytes_per_sec,
            "process_rss_bytes": snapshot.process_rss_bytes,
            "gpus": [_gpu_json(gpu) for gpu in snapshot.gpus],
            "unavailable_reasons": dict(snapshot.unavailable_reasons()),
        }
    )


def format_sample_event(
    sequence: int, snapshot: TelemetrySnapshot, *, clock: Clock = utc_now
) -> str:
    """Format one ``telemetry.sampled`` SSE frame, per API and Contract Standards §8.

    Args:
        sequence: This connection's 1-based, gap-free frame counter — a fresh stream for a
            reconnecting client, per this module's docstring on why telemetry does not replay.
        snapshot: The sample to send.
        clock: Returns the current instant the envelope is stamped with; injected for
            deterministic tests.

    Returns:
        The complete frame, ``id``/``event``/``data`` lines and the trailing blank line.
    """
    payload = {
        "event_id": new_id(),
        "sequence": sequence,
        "type": "telemetry.sampled",
        "timestamp": to_rfc3339(snapshot.timestamp),
        "data": snapshot_to_json(snapshot),
    }
    data = dump_envelope(
        payload,
        schema="event.envelope",
        version=_EVENT_SCHEMA_VERSION,
        generator=_GENERATOR,
        clock=clock,
    )
    return f"id: {sequence}\nevent: telemetry.sampled\ndata: {data}\n\n"


def format_heartbeat(*, clock: Clock = utc_now) -> str:
    """Format one SSE heartbeat comment, sent every 15 s per API and Contract Standards §8."""
    return f": heartbeat {to_rfc3339(clock())}\n\n"


class TelemetryService:
    """Owns one :class:`~sweatmeter.TelemetryCollector` and its background sampler.

    Built once by the web lifespan and stored on ``app.state.telemetry``; every request reads
    through the same instance rather than building its own collector or sampler. Not itself
    thread-unsafe to share: :class:`~sweatmeter.TelemetrySampler` documents its reads and its
    start/stop transitions as synchronized.

    Args:
        collector: The collector to sample from. Injected so tests can pass a collector built over
            :mod:`sweatmeter.testing`'s scripted readers instead of the real platform.
        interval_seconds: Positive delay between samples.
    """

    def __init__(self, collector: TelemetryCollector, *, interval_seconds: float) -> None:
        """Configure the sampler without starting its thread."""
        self._collector = collector
        self._sampler = TelemetrySampler(collector, interval_seconds=interval_seconds)

    def start(self) -> None:
        """Start background sampling, or do nothing if already running."""
        self._sampler.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop background sampling and wait for the worker thread to exit.

        Safe to call whether or not :meth:`start` was ever called — mirrors
        :meth:`~sweatmeter.TelemetrySampler.stop`.
        """
        self._sampler.stop(timeout=timeout)

    def latest(self) -> TelemetrySnapshot | None:
        """Return the newest sample, or ``None`` before the first successful collection."""
        return self._sampler.latest()

    def machine_profile(self) -> MachineProfile:
        """Return this host's static profile, collected fresh from the shared collector."""
        return self._collector.machine_profile()

    @property
    def collector(self) -> TelemetryCollector:
        """The underlying collector, for callers (health checks) that need a live reading."""
        return self._collector
