"""Integration tests for the run event stream and its replay (development plan, Phase 5).

The phase's test-list item: "replay after disconnect has no gap and no duplicate". The store's own
sequence guarantees are in ``tests/unit/test_event_sequence.py``; what is tested here is the thing
built on top of them — :func:`~freeweight.web.routes.runs._event_stream`, the generator the SSE
endpoint serves.

Every test drives that generator directly through ``asyncio.run``. ``pytest-asyncio`` is not a
dependency of this project (see ``tests/integration/test_telemetry_sse.py``, which does the same),
and driving the generator is also what lets these tests assert the *exact* frame sequence rather
than whatever a client happened to receive before a timeout.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.conftest import RunEnvironment

from freeweight.config import ExecutionSettings, load_settings
from freeweight.services.runs import ExecutionConfig, create_run
from freeweight.services.scheduler import RunScheduler
from freeweight.web.app import create_app
from freeweight.web.routes.runs import _event_stream, _resolve_last_event_id


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


async def _drain(iterator: AsyncGenerator[str], *, timeout: float = 30.0) -> list[str]:
    """Collect every frame the generator yields until it closes."""
    frames: list[str] = []
    async with asyncio.timeout(timeout):
        async for frame in iterator:
            frames.append(frame)
    return frames


async def _take(iterator: AsyncGenerator[str], count: int, *, timeout: float = 30.0) -> list[str]:
    """Read exactly ``count`` frames and then abandon the stream, as a client that goes away does.

    Closing the generator part-way is what a dropped connection actually looks like from the
    server's side, and it is the only way to leave a subscriber holding a *prefix* of the stream —
    which is the situation replay exists for.
    """
    frames: list[str] = []
    async with asyncio.timeout(timeout):
        async for frame in iterator:
            frames.append(frame)
            if len(frames) >= count:
                break
    await iterator.aclose()
    return frames


def _ids(frames: list[str]) -> list[int]:
    """The ``id:`` line of every non-heartbeat frame, in order."""
    return [
        int(frame.split("\n", 1)[0].removeprefix("id: "))
        for frame in frames
        if frame.startswith("id: ")
    ]


def _payload(frame: str) -> dict[str, Any]:
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    return dict(json.loads(data_line.removeprefix("data: "))["payload"])


@pytest.fixture
def environment(run_environment: Callable[..., RunEnvironment]) -> RunEnvironment:
    return run_environment()


@pytest.fixture
def completed_run(environment: RunEnvironment) -> str:
    """One fully executed run, so its event stream is complete and terminal."""
    summary = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=ExecutionConfig.resolve(
            ExecutionSettings(warmup_repetitions=0, cooldown_seconds=0), measured_repetitions=1
        ),
    )
    RunScheduler(
        environment.database, environment.provider, registry=environment.registry
    ).run_once()
    return summary.id


class TestFullReplay:
    def test_a_fresh_subscriber_receives_every_event_from_one(
        self, environment: RunEnvironment, completed_run: str
    ) -> None:
        frames = asyncio.run(
            _drain(
                _event_stream(
                    _FakeRequest(),
                    environment.database,
                    completed_run,
                    after_sequence=0,
                    poll_interval_seconds=0.001,
                )
            )
        )
        ids = _ids(frames)
        assert ids == list(range(1, len(ids) + 1))
        assert _payload(frames[0])["type"] == "run.started"
        assert _payload(frames[-1])["type"] == "run.completed"

    def test_the_stream_closes_after_the_terminal_event(
        self, environment: RunEnvironment, completed_run: str
    ) -> None:
        """API standards §8: the terminal event is always sent before the server closes."""
        frames = asyncio.run(
            _drain(
                _event_stream(
                    _FakeRequest(),
                    environment.database,
                    completed_run,
                    after_sequence=0,
                    poll_interval_seconds=0.001,
                )
            )
        )
        assert _payload(frames[-1])["type"] == "run.completed"

    def test_a_disconnecting_client_ends_the_generator(
        self, environment: RunEnvironment, completed_run: str
    ) -> None:
        frames = asyncio.run(
            _drain(
                _event_stream(
                    _FakeRequest(disconnect_after=0),
                    environment.database,
                    completed_run,
                    after_sequence=0,
                    poll_interval_seconds=0.001,
                )
            )
        )
        assert frames == []


class TestResumeHasNoGapAndNoDuplicate:
    def test_a_reconnect_continues_exactly_where_it_stopped(
        self, environment: RunEnvironment, completed_run: str
    ) -> None:
        """Read a prefix, reconnect from its last id, and assert the two halves join cleanly."""
        first = asyncio.run(
            _take(
                _event_stream(
                    _FakeRequest(),
                    environment.database,
                    completed_run,
                    after_sequence=0,
                    poll_interval_seconds=0.001,
                ),
                3,
            )
        )
        assert len(first) == 3
        resume_from = _ids(first)[-1]

        second = asyncio.run(
            _drain(
                _event_stream(
                    _FakeRequest(),
                    environment.database,
                    completed_run,
                    after_sequence=resume_from,
                    poll_interval_seconds=0.001,
                )
            )
        )

        combined = _ids(first) + _ids(second)
        assert combined == sorted(combined), "ids must be monotonically increasing"
        assert len(combined) == len(set(combined)), "no id may be delivered twice"
        assert combined == list(range(1, len(combined) + 1)), "no id may be skipped"
        assert _payload(second[-1])["type"] == "run.completed"

    @pytest.mark.parametrize("resume_from", [0, 1, 3, 5, 8])
    def test_resuming_from_any_point_yields_the_exact_tail(
        self, environment: RunEnvironment, completed_run: str, resume_from: int
    ) -> None:
        frames = asyncio.run(
            _drain(
                _event_stream(
                    _FakeRequest(),
                    environment.database,
                    completed_run,
                    after_sequence=resume_from,
                    poll_interval_seconds=0.001,
                )
            )
        )
        ids = _ids(frames)
        assert ids[0] == resume_from + 1
        assert ids == list(range(resume_from + 1, resume_from + 1 + len(ids)))

    def test_resuming_past_the_end_yields_nothing_and_does_not_hang(
        self, environment: RunEnvironment, completed_run: str
    ) -> None:
        """A client ahead of the store gets no frames, and the generator still ends on disconnect.

        The "does not hang" half matters: a stream that blocks forever waiting for an event that
        will never arrive holds a connection and a worker for the life of the process.
        """
        frames = asyncio.run(
            _drain(
                _event_stream(
                    _FakeRequest(disconnect_after=2),
                    environment.database,
                    completed_run,
                    after_sequence=9_999,
                    poll_interval_seconds=0.001,
                )
            )
        )
        assert _ids(frames) == []


class TestLastEventIdResolution:
    def _request(self, header: str | None) -> Any:
        headers = {"last-event-id": header} if header is not None else {}
        return type("_Req", (), {"headers": headers})()

    def test_the_header_wins_when_present(self) -> None:
        assert _resolve_last_event_id(self._request("12"), None) == 12

    def test_the_query_parameter_is_used_when_the_header_is_absent(self) -> None:
        """A page *reload* starts a new ``EventSource``, which sends no ``Last-Event-ID``.

        This is the mechanism behind acceptance criterion 2 — refresh mid-run, no missing events —
        so it is tested as its own behaviour rather than only through a browser.
        """
        assert _resolve_last_event_id(self._request(None), "7") == 7

    def test_a_missing_position_replays_from_the_beginning(self) -> None:
        assert _resolve_last_event_id(self._request(None), None) == 0

    @pytest.mark.parametrize("raw", ["", "abc", "-4", "1.5"])
    def test_an_unusable_position_replays_from_the_beginning(self, raw: str) -> None:
        assert _resolve_last_event_id(self._request(raw), None) == 0


class TestHttpLevel:
    """The route wiring, checked through a served application.

    Only the non-streaming half is exercised over HTTP: ``TestClient`` reads a streaming response
    to completion before returning it, so driving a *live* stream through it would deadlock on a
    run that has not finished. The generator tests above cover streaming behaviour; these cover
    that the endpoint exists, refuses an unknown run, and declares the right media type.
    """

    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        from freeweight.infrastructure.db.engine import create_engine_for
        from freeweight.infrastructure.db.migration import MigrationRunner
        from freeweight.services.database import MIGRATIONS_LOCATION

        database = tmp_path / "freeweight.sqlite3"
        monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
        monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
        engine = create_engine_for(f"sqlite:///{database}")
        try:
            MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        finally:
            engine.dispose()
        loaded = load_settings(config_path=tmp_path / "missing.toml")
        with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
            yield test_client

    def test_events_for_an_unknown_run_is_404_not_an_empty_stream(self, client: Any) -> None:
        response = client.get("/api/v1/runs/01ZZZZZZZZZZZZZZZZZZZZZZZZ/events")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RUN_NOT_FOUND"

    def test_a_finished_run_streams_its_events_over_http(self, client: Any) -> None:
        assert client.post("/models/discover", follow_redirects=False).status_code == 303

        created = client.post(
            "/api/v1/runs",
            json={"model": "fake-model:8b-q8_0", "suites": ["native.echo"]},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]

        # The scheduler thread the lifespan started executes it; poll the API the same way a
        # script would rather than reaching into the scheduler.
        deadline = time.monotonic() + 30
        body = client.get(f"/api/v1/runs/{run_id}").json()
        while body["status"] not in {"completed", "failed", "cancelled", "interrupted"}:
            assert time.monotonic() < deadline, f"run stuck in {body['status']}"
            time.sleep(0.05)
            body = client.get(f"/api/v1/runs/{run_id}").json()
        assert body["status"] == "completed"

        response = client.get(f"/api/v1/runs/{run_id}/events")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        ids = [
            int(line.removeprefix("id: "))
            for line in response.text.splitlines()
            if line.startswith("id: ")
        ]
        assert ids == list(range(1, len(ids) + 1))
        assert "event: run.completed" in response.text
