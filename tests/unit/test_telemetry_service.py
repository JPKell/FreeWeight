"""Unit tests for freeweight.services.telemetry.

Phase 4's own test list (development plan): the sampler stops cleanly and leaks no thread across
repeated starts; a machine with no GPU renders every GPU-derived value with a reason and degrades
health without crashing. Every test runs against SweatMeter's scripted readers, never the real
platform.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from baseaicore import UNSUPPORTED, UnsupportedPlatformError
from sweatmeter import GpuSample, TelemetryCollector, TelemetrySnapshot
from sweatmeter.testing import (
    HostReading,
    NullGpuReader,
    NullHostReader,
    ScriptedGpuReader,
    ScriptedHostReader,
)
from sweatmeter.types import HostFacts

from freeweight.services.health import get_health_report
from freeweight.services.telemetry import (
    TelemetryService,
    build_collector,
    format_heartbeat,
    format_sample_event,
    snapshot_to_json,
)
from freeweight.web.rendering import render

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_SAMPLER_THREAD_NAME = "sweatmeter-sampler"


def _sampler_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == _SAMPLER_THREAD_NAME]


def _repeating_collector() -> TelemetryCollector:
    """A collector the sampler can call any number of times without exhausting a script.

    ``NullHostReader``/``NullGpuReader`` degrade every field to ``UNSUPPORTED`` on every call
    (never raising and never exhausting), which is all the lifecycle tests below need — they
    assert on thread behaviour, not on sampled content.
    """
    return TelemetryCollector(host=NullHostReader(), gpu=NullGpuReader())


class TestTelemetryServiceLifecycle:
    def test_latest_is_none_before_start(self) -> None:
        service = TelemetryService(_repeating_collector(), interval_seconds=0.01)

        assert service.latest() is None

    def test_start_then_stop_leaves_no_sampler_thread_running(self) -> None:
        service = TelemetryService(_repeating_collector(), interval_seconds=0.01)

        service.start()
        deadline = time.monotonic() + 2.0
        while service.latest() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.latest() is not None

        service.stop()

        assert _sampler_threads() == []

    def test_repeated_start_stop_cycles_leave_no_thread_leak(self) -> None:
        service = TelemetryService(_repeating_collector(), interval_seconds=0.01)

        for _ in range(5):
            service.start()
            service.stop()

        assert _sampler_threads() == []

    def test_stop_before_start_is_safe(self) -> None:
        service = TelemetryService(_repeating_collector(), interval_seconds=0.01)

        service.stop()

        assert _sampler_threads() == []


class TestBuildCollector:
    def test_degrades_to_null_host_reader_when_platform_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise() -> object:
            raise UnsupportedPlatformError("no reader for this platform")

        monkeypatch.setattr("freeweight.services.telemetry.create_host_reader", _raise)

        collector = build_collector()

        snapshot = collector.snapshot()
        assert snapshot.cpu_percent is UNSUPPORTED
        assert snapshot.unavailable_reasons().get("cpu_percent") == "platform_unsupported"


class TestSnapshotToJson:
    def test_unsupported_measurement_renders_as_the_fixed_string(self) -> None:
        snapshot = TelemetrySnapshot(timestamp=NOW)

        rendered = snapshot_to_json(snapshot)

        assert rendered["cpu_percent"] == "unsupported"
        assert rendered["gpus"] == []

    def test_gpu_list_and_reasons_are_included(self) -> None:
        snapshot = TelemetrySnapshot(
            timestamp=NOW,
            gpus=(GpuSample(index=0, uuid="GPU-1", utilization_percent=42),),
            _reasons=(("gpu", "no_gpus"),),
        )

        rendered = snapshot_to_json(snapshot)

        assert rendered["gpus"][0]["uuid"] == "GPU-1"
        assert rendered["gpus"][0]["utilization_percent"] == 42
        assert rendered["gpus"][0]["vram_used_bytes"] == "unsupported"
        assert rendered["unavailable_reasons"]["gpu"] == "no_gpus"


class TestSseFrameFormatting:
    def test_sample_event_is_a_conformant_envelope_frame(self) -> None:
        snapshot = TelemetrySnapshot(timestamp=NOW, cpu_percent=10)

        frame = format_sample_event(1, snapshot, clock=lambda: NOW)

        lines = frame.split("\n")
        assert lines[0] == "id: 1"
        assert lines[1] == "event: telemetry.sampled"
        assert lines[2].startswith("data: ")
        assert lines[3:] == ["", ""]
        envelope = json.loads(lines[2][len("data: ") :])
        assert envelope["schema"] == "event.envelope"
        assert envelope["schema_version"] == "1.0"
        assert envelope["payload"]["type"] == "telemetry.sampled"
        assert envelope["payload"]["sequence"] == 1
        assert envelope["payload"]["data"]["cpu_percent"] == 10

    def test_heartbeat_is_an_sse_comment(self) -> None:
        heartbeat = format_heartbeat(clock=lambda: NOW)

        assert heartbeat.startswith(": heartbeat ")
        assert heartbeat.endswith("\n\n")


class TestHealthIntegration:
    def test_gpu_telemetry_component_is_ok_when_a_gpu_reports(self) -> None:
        collector = TelemetryCollector(
            host=ScriptedHostReader([HostReading(static_facts=HostFacts(hostname="bench-01"))]),
            gpu=ScriptedGpuReader([(GpuSample(index=0, uuid="GPU-1"),)]),
        )

        report = get_health_report(telemetry=collector)

        gpu_component = next(c for c in report.components if c.name == "gpu_telemetry")
        assert gpu_component.status == "ok"

    def test_gpu_telemetry_component_is_degraded_with_no_gpu_and_overall_health_degrades(
        self,
    ) -> None:
        collector = TelemetryCollector(
            host=ScriptedHostReader([HostReading(static_facts=HostFacts(hostname="bench-01"))]),
            gpu=NullGpuReader(),
        )

        report = get_health_report(telemetry=collector)

        gpu_component = next(c for c in report.components if c.name == "gpu_telemetry")
        assert gpu_component.status == "degraded"
        assert gpu_component.detail != ""
        assert report.status == "degraded"

    def test_machine_component_is_ok_when_host_platform_is_identified(self) -> None:
        collector = TelemetryCollector(
            host=ScriptedHostReader(
                [HostReading(static_facts=HostFacts(hostname="bench-01", os_name="Linux"))]
            ),
            gpu=NullGpuReader(),
        )

        report = get_health_report(telemetry=collector)

        machine_component = next(c for c in report.components if c.name == "machine")
        assert machine_component.status == "ok"


class TestTelemetryBarLayout:
    def test_every_updated_field_has_a_fixed_width_so_a_new_value_cannot_shift_layout(self) -> None:
        html = render("partials/telemetry_bar.html")

        # telemetry.js only ever replaces a field's textContent (never restructures the DOM), and
        # every field it targets carries the fixed-width rule — together, that is what keeps a
        # value updating every second from moving anything around it (UI Standards §3). Since
        # Phase 12 the rule lives in MirrorWall's layout.css rather than an inline style block,
        # so that stylesheet is where the width is asserted.
        import mirrorwall

        layout_css = (Path(mirrorwall.__file__).parent / "static" / "css" / "layout.css").read_text(
            encoding="utf-8"
        )
        assert re.search(r"\.telemetry-value\s*\{[^}]*min-width", layout_css)
        for field in (
            "cpu_percent",
            "cpu_temperature_c",
            "ram_used_bytes",
            "ram_total_bytes",
            "gpu_utilization_percent",
            "gpu_temperature_c",
            "gpu_power_watts",
            "gpu_vram_used_bytes",
            "gpu_vram_total_bytes",
        ):
            marker = f'data-field="{field}"'
            assert marker in html
            span_start = html.index(marker)
            span_open_tag = html.rfind("<span", 0, span_start)
            assert 'class="telemetry-value' in html[span_open_tag:span_start]
