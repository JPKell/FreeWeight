"""End-to-end: ``GET /api/v1/health`` answers quickly while the provider's own check is slow.

Row WY6. WeightRoomGym's ``app_down`` probe gives the health check 5 s. On the reference machine
the llama.cpp provider's ``health()`` re-parses every GGUF header once its metadata cache expires
(about 5.5 s for 27 files), and the route ran that on the event loop, so every tenth probe timed out
and the console called FreeWeight down. A slow provider must make the report ``degraded``, never
late, and must not stall any other request while it runs (Graceful Degradation §3).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from modelrack.provider import ProviderHealth
from modelrack.providers.fake import FakeProvider
from weightsdb import MigrationRunner, create_engine_for

from freeweight.config import load_settings
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.web.app import create_app

_SLOW_HEALTH_SECONDS = 3.0


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
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


@pytest.fixture
def slow_provider_health(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """Make the fake provider's ``health()`` take 3 s, released early at teardown."""
    release = threading.Event()
    original = FakeProvider.health

    def slow(self: FakeProvider) -> ProviderHealth:
        release.wait(_SLOW_HEALTH_SECONDS)
        return original(self)

    monkeypatch.setattr(FakeProvider, "health", slow)
    yield release
    release.set()


def test_health_answers_degraded_inside_a_second_while_the_provider_check_is_slow(
    client: TestClient, slow_provider_health: threading.Event
) -> None:
    started = time.monotonic()
    response = client.get("/api/v1/health")
    elapsed_seconds = time.monotonic() - started

    assert elapsed_seconds < 1.0
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    provider = next(c for c in body["components"] if c["name"] == "provider")
    assert provider["status"] == "degraded"
    assert "did not answer" in provider["detail"]


def test_a_slow_health_check_does_not_stall_other_requests(
    client: TestClient, slow_provider_health: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unbound the provider check so the health request really is in progress for 3 s; what is
    # under test is that it runs off the event loop, not the bound.
    monkeypatch.setattr("freeweight.web.routes.system.PROVIDER_HEALTH_TIMEOUT_SECONDS", None)
    in_flight = threading.Thread(target=client.get, args=("/api/v1/health",))
    in_flight.start()
    try:
        time.sleep(0.2)
        started = time.monotonic()
        response = client.get("/api/v1/version")
        elapsed_seconds = time.monotonic() - started
    finally:
        slow_provider_health.set()
        in_flight.join()

    assert response.status_code == 200
    assert elapsed_seconds < 1.0
