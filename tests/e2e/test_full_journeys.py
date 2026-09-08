"""End-to-end: the whole clean-machine journey, Phase 14 acceptance criterion 1.

Development plan, Phase 14: "Clean-machine install: `pip install freeweight` → `freeweight serve`
→ run a benchmark → export." `install-check` (CI) proves the install and that the server starts;
this file proves the rest — that a freshly served application with **zero configuration** (no
config file, ``load_settings`` pointed at a path that does not exist, exactly `G3`) can run a
benchmark to completion and export a document that actually names the run it just produced. Every
other e2e file exercises one page or one endpoint; this one is the single, unbroken path across
all of them, which is the property Phase 14 names and none of the others assert together.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from weightsdb import MigrationRunner, create_engine_for

from freeweight.config import load_settings
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.web.app import create_app

_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A **fresh** database and a fake-provider configuration — no file, no prior state."""
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__IDLE_GPU_THRESHOLD_PERCENT", "0")
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    return tmp_path


@pytest.fixture
def client(workspace: Path) -> Iterator[TestClient]:
    """``freeweight serve`` over ``workspace``, with no config file on disk (G3)."""
    loaded = load_settings(config_path=workspace / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_serve_run_a_benchmark_and_export_is_one_unbroken_path(client: TestClient) -> None:
    """Development plan Phase 14, acceptance criterion 1, end to end and in order."""
    # `freeweight serve` reaches a healthy state with only the model source configured — no
    # config file, nothing hand-edited (G3).
    health = client.get("/api/v1/health")
    assert health.status_code == 200

    # Discover the one model the fake provider offers, then run a benchmark against it.
    assert client.post("/models/discover", follow_redirects=False).status_code == 303
    started = client.post(
        "/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suite": "native.echo"}
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    deadline = time.monotonic() + 60.0
    body = client.get(f"/api/v1/runs/{run_id}").json()
    while body["status"] not in _TERMINAL:
        assert time.monotonic() < deadline, f"run stuck in {body['status']}"
        time.sleep(0.05)
        body = client.get(f"/api/v1/runs/{run_id}").json()
    assert body["status"] == "completed", body

    # Export, and prove the document names the run this journey just produced — an export that
    # cannot be traced back to the run it followed would satisfy the letter of "export" and
    # none of its purpose.
    exported = client.get("/api/v1/results/export", params={"scope": "all", "format": "jsonl"})
    assert exported.status_code == 200
    lines = [json.loads(line) for line in exported.text.splitlines() if line]
    assert any(run["run_id"] == run_id for line in lines for run in line["payload"]["runs"])
