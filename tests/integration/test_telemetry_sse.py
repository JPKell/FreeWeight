"""Integration tests for the telemetry SSE endpoint.

Phase 4's own test list (development plan): events flow, a heartbeat is present, disconnect and
reconnect both work, and 50 concurrent subscribers stay within budget. The generator-level tests
below exercise :func:`freeweight.web.routes.system._telemetry_events` directly against a real,
running :class:`~freeweight.services.telemetry.TelemetryService` (a genuine background sampler
thread) with fast, overridden poll and heartbeat intervals — the same function the route serves,
without waiting on the real 15 s heartbeat cadence. ``TestHttpLevel`` proves the route wiring
through a served application (see its class docstring for why the streaming endpoint is checked
without driving a live SSE round trip).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient
from sweatmeter import TelemetryCollector
from sweatmeter.testing import NullGpuReader, NullHostReader

from freeweight.config import load_settings
from freeweight.services.telemetry import TelemetryService
from freeweight.web.app import create_app
from freeweight.web.routes.system import _telemetry_events, telemetry_stream

if TYPE_CHECKING:
    from fastapi import Request


class _FakeRequest:
    """A minimal stand-in for ``fastapi.Request``: only ``is_disconnected`` is called."""

    def __init__(self, *, disconnect_after: int | None = None) -> None:
        self._remaining = disconnect_after

    async def is_disconnected(self) -> bool:
        if self._remaining is None:
            return False
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


async def _collect(iterator: AsyncIterator[str], count: int, *, timeout: float = 5.0) -> list[str]:
    frames: list[str] = []
    async with asyncio.timeout(timeout):
        async for frame in iterator:
            frames.append(frame)
            if len(frames) >= count:
                break
    return frames


@pytest.fixture
def service() -> Iterator[TelemetryService]:
    """A real telemetry service, sampling fast, over a GPU-less scripted platform."""
    collector = TelemetryCollector(host=NullHostReader(), gpu=NullGpuReader())
    instance = TelemetryService(collector, interval_seconds=0.01)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


class TestGeneratorLevel:
    """Runs each async body through ``asyncio.run``: ``pytest-asyncio`` is not a dependency of
    this project, so a plain ``def test_...`` driving ``asyncio.run`` is how ``async def`` code is
    exercised from pytest here.
    """

    def test_events_flow(self, service: TelemetryService) -> None:
        async def body() -> list[str]:
            return await _collect(
                _telemetry_events(
                    _FakeRequest(),
                    service,
                    poll_interval_seconds=0.01,
                    heartbeat_interval_seconds=60.0,
                ),
                count=2,
            )

        frames = asyncio.run(body())

        assert len(frames) == 2
        assert frames[0].startswith("id: 1\nevent: telemetry.sampled\ndata: ")
        assert frames[1].startswith("id: 2\nevent: telemetry.sampled\ndata: ")

    def test_heartbeat_present_absent_a_new_sample(self) -> None:
        # A sampler that is never started: latest() stays None forever, so every frame the
        # generator emits must be a heartbeat, never a sample.
        idle_service = TelemetryService(
            TelemetryCollector(host=NullHostReader(), gpu=NullGpuReader()), interval_seconds=1.0
        )

        async def body() -> list[str]:
            return await _collect(
                _telemetry_events(
                    _FakeRequest(),
                    idle_service,
                    poll_interval_seconds=0.01,
                    heartbeat_interval_seconds=0.02,
                ),
                count=1,
            )

        frames = asyncio.run(body())

        assert frames[0].startswith(": heartbeat ")

    def test_disconnect_stops_the_generator_cleanly(self, service: TelemetryService) -> None:
        async def body() -> list[str]:
            return [
                frame
                async for frame in _telemetry_events(
                    _FakeRequest(disconnect_after=0), service, poll_interval_seconds=0.01
                )
            ]

        assert asyncio.run(body()) == []

    def test_reconnect_starts_a_fresh_sequence(self, service: TelemetryService) -> None:
        async def body() -> tuple[list[str], list[str]]:
            first_connection = await _collect(
                _telemetry_events(_FakeRequest(), service, poll_interval_seconds=0.01), count=1
            )
            second_connection = await _collect(
                _telemetry_events(_FakeRequest(), service, poll_interval_seconds=0.01), count=1
            )
            return first_connection, second_connection

        first_connection, second_connection = asyncio.run(body())

        assert first_connection[0].startswith("id: 1\n")
        assert second_connection[0].startswith("id: 1\n")

    def test_fifty_concurrent_subscribers_stay_within_budget(
        self, service: TelemetryService
    ) -> None:
        async def body() -> list[list[str]]:
            return await asyncio.gather(
                *(
                    _collect(
                        _telemetry_events(_FakeRequest(), service, poll_interval_seconds=0.01),
                        count=2,
                    )
                    for _ in range(50)
                )
            )

        started = time.monotonic()
        results = asyncio.run(body())
        elapsed = time.monotonic() - started

        assert all(len(frames) == 2 for frames in results)
        assert elapsed < 5.0


@contextmanager
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A lifespan-started client with a fast telemetry interval, per ``test_server_boot.py``."""
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    monkeypatch.setenv("FREEWEIGHT_TELEMETRY__INTERVAL_MS", "10")
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    app = create_app(loaded.settings)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


class TestHttpLevel:
    def test_stream_route_returns_a_conformant_streaming_response(
        self, service: TelemetryService
    ) -> None:
        # Deliberately not a live round trip through TestClient: breaking out of an SSE read
        # early does not reliably deliver an ASGI disconnect back through TestClient's transport,
        # which leaves the server-side generator polling forever and the test hanging — a known
        # rough edge of testing raw streaming responses this way, not something this endpoint's
        # own logic can fix. The generator itself (events flow, heartbeat, disconnect, reconnect,
        # concurrency) is exercised directly and safely by ``TestGeneratorLevel`` above; what is
        # left to prove here is that the route wires it up correctly, which does not require
        # driving the stream at all — constructing a ``StreamingResponse`` does not consume its
        # generator.
        request = cast(
            "Request",
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(telemetry=service))),
        )

        response = asyncio.run(telemetry_stream(request))

        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"

    def test_status_endpoint_reports_a_telemetry_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _client(tmp_path, monkeypatch) as client:
            deadline = time.monotonic() + 2.0
            body: dict[str, object] = {}
            while time.monotonic() < deadline:
                body = client.get("/api/v1/system/status").json()
                if body["telemetry"] is not None:
                    break
                time.sleep(0.02)

        telemetry = body["telemetry"]
        assert isinstance(telemetry, dict)
        assert "cpu_percent" in telemetry
        # This client's database is deliberately not migrated, so the run queue cannot be read at
        # all. `None`, not `0`: "no runs are waiting" and "I cannot see the queue" are different
        # facts, and only one of them is reassuring. `/health` reports the reason. A migrated
        # instance's real queue depth is asserted in tests/unit/test_scheduler.py.
        assert body["queue_depth"] is None
        assert body["active_run"] is None
