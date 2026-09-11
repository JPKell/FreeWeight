"""Row WM2: the MirrorWall 0.3 surfaces this application opted into (design brief §6).

Each is a diff a person can see in the page: the tab strip only when ``[console] url`` is set,
the status dot on the System page, dense list tables, inline meters in the telemetry bar, and
the run page's log pane fed by ``/runs/{id}/log`` — the same events as the API stream, rendered
as ``log`` frames and closed with ``log.closed``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from weightsdb import MigrationRunner, create_engine_for

from freeweight.config import load_settings
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.web.app import create_app

CONSOLE = "https://jordan-main.local:8769"


def _client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, console_url: str = ""
) -> TestClient:
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__IDLE_GPU_THRESHOLD_PERCENT", "0")
    if console_url:
        monkeypatch.setenv("FREEWEIGHT_CONSOLE__URL", console_url)
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    return TestClient(create_app(loaded.settings), base_url="http://127.0.0.1")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(tmp_path, monkeypatch) as test_client:
        yield test_client


def test_no_tab_strip_without_a_console_url(client: TestClient) -> None:
    page = client.get("/system").text
    assert 'class="app-tabs"' not in page and 'class="app-tab"' not in page


def test_the_tab_strip_links_the_console_and_the_peers_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch, console_url=CONSOLE + "/") as client:
        page = client.get("/system").text
    assert f'<a href="{CONSOLE}" class="app-tab">' in page  # WeightRoomGym itself
    assert '<a href="/" class="app-tab" aria-current="page">' in page  # this application
    for peer in ("loadcoach", "ideapress", "promptcadence"):
        assert f'<a href="{CONSOLE}/apps/{peer}" class="app-tab">' in page
    assert page.count("app-tab") >= 5


def test_the_system_page_shows_a_status_dot_per_health_component(client: TestClient) -> None:
    page = client.get("/system").text
    components = client.get("/api/v1/health").json()["components"]
    assert page.count('class="status-dot"') == len(components)
    assert 'data-status="ok"' in page


def test_the_list_tables_are_dense(client: TestClient) -> None:
    client.post("/models/discover", follow_redirects=False)
    client.post("/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suites": ["native.echo"]})
    for path in ("/runs", "/models", "/results"):
        page = client.get(path).text
        assert 'data-density="dense"' in page or "<table" not in page, path  # empty state has none
    assert 'data-density="dense"' in client.get("/models").text


def test_the_telemetry_bar_carries_meters(client: TestClient) -> None:
    page = client.get("/system").text
    for group in ("cpu", "ram", "gpu", "vram"):
        assert f'data-meter="{group}"' in page


def test_the_run_page_has_a_log_pane_fed_by_the_runs_own_events(client: TestClient) -> None:
    client.post("/models/discover", follow_redirects=False)
    run_id = client.post(
        "/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suites": ["native.echo"]}
    ).json()["id"]
    page = client.get(f"/runs/{run_id}").text
    assert f'sse-connect="/runs/{run_id}/log"' in page and 'sse-close="log.closed"' in page
    assert 'id="run-events"' not in page  # the old list is gone; runs.js keeps badge and bar
    assert "vendor/htmx/htmx.min.js" in page  # htmx on this page only
    assert "vendor/htmx" not in client.get("/runs").text

    with client.stream("GET", f"/runs/{run_id}/log") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = ""
        for chunk in response.iter_text():
            body += chunk
            if "event: log.closed" in body or body.count("event: log") >= 3:
                break
    assert 'event: log\ndata: <div class="log-pane-line" data-level=' in body
    assert "run.started" in body or "run.progress" in body or "run.completed" in body
