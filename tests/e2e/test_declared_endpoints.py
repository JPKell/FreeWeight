"""The nine endpoints spec §7.1 declared and no phase built, exercised over HTTP.

They were found by ``tests/contract/test_declared_surface.py``, which reads §7.1 and asserts every
path in it is routable. Six were already known from a live journey that got a 404; the test found
three more — the whole machines API, and ``POST /runs/{id}/repeat``, which had a service function
and a CLI command and no route.

Routable is not the same as correct, so this is the other half: each endpoint answers, with the
shape [api.md](../../docs/apps/freeweight/api.md) documents.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from freeweight.cli.main import app as cli_app
from freeweight.config import load_settings
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.web.app import create_app

runner = CliRunner()
_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    loaded = load_settings(config_path=workspace / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _completed_run(client: TestClient) -> str:
    assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0
    created = client.post(
        "/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suite": "native.echo"}
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["id"]
    deadline = time.monotonic() + 60.0
    while True:
        body = client.get(f"/api/v1/runs/{run_id}").json()
        if body["status"] in _TERMINAL:
            assert body["status"] == "completed", body
            return str(run_id)
        assert time.monotonic() < deadline, f"run stuck in {body['status']}"
        time.sleep(0.05)


class TestTheModelsApi:
    def test_it_lists_identities_with_their_latest_descriptor(self, client: TestClient) -> None:
        assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0
        body = client.get("/api/v1/models").json()

        assert body["items"], "discovery stored models the API does not list"
        first = body["items"][0]
        assert first["canonical_id"] and first["identity_confidence"] in {"digest", "name_only"}
        assert "quantization" in first and "max_context" in first

    def test_a_canonical_id_is_a_query_parameter_and_never_a_path_segment(
        self, client: TestClient
    ) -> None:
        """ADR-0024: a canonical ID contains ``/``, ``:`` and ``@``, and a percent-encoded ``/``
        does not survive common reverse proxies."""
        assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0
        canonical = client.get("/api/v1/models").json()["items"][0]["canonical_id"]

        found = client.get("/api/v1/models", params={"canonical_id": canonical}).json()

        assert [row["canonical_id"] for row in found["items"]] == [canonical]

    def test_it_serves_one_model_with_its_descriptor_history(self, client: TestClient) -> None:
        assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0
        model_id = client.get("/api/v1/models").json()["items"][0]["id"]

        body = client.get(f"/api/v1/models/{model_id}").json()

        assert body["id"] == model_id
        assert "descriptor_history" in body and "aliases" in body

    def test_discovery_reports_what_changed_rather_than_the_models(
        self, client: TestClient
    ) -> None:
        first = client.post("/api/v1/models/discover")
        assert first.status_code == 200, first.text
        again = client.post("/api/v1/models/discover").json()

        assert set(again) == {"added", "updated", "unchanged", "total"}
        assert again["added"] == 0, "re-discovery must be idempotent"

    def test_one_models_results_are_scoped_to_it(self, client: TestClient) -> None:
        _completed_run(client)
        model_id = client.get("/api/v1/models").json()["items"][0]["id"]

        body = client.get(f"/api/v1/models/{model_id}/results").json()

        assert body["items"], "a completed run produced no results for its own model"
        assert "next_cursor" in body

    def test_an_unknown_model_is_a_404_not_an_empty_list(self, client: TestClient) -> None:
        response = client.get("/api/v1/models/01ZZZZZZZZZZZZZZZZZZZZZZZZ")

        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"


class TestTheMachinesApi:
    def test_the_list_is_empty_before_anything_has_been_measured(self, client: TestClient) -> None:
        """Machines are recorded when a run is created, not when this endpoint is polled: a GET
        that wrote would let a monitoring poll make a machine look freshly used."""
        assert client.get("/api/v1/machines").json()["items"] == []

    def test_it_lists_machines_and_flags_the_current_one(self, client: TestClient) -> None:
        """A fingerprint is not a name a person recognizes, so the list says which one is here."""
        _completed_run(client)
        body = client.get("/api/v1/machines").json()

        assert body["items"], "no machine was profiled"
        assert any(row["is_current"] for row in body["items"])
        assert all(row["machine_fingerprint"] for row in body["items"])

    def test_it_serves_one_machine_by_id(self, client: TestClient) -> None:
        _completed_run(client)
        machine_id = client.get("/api/v1/machines").json()["items"][0]["id"]

        body = client.get(f"/api/v1/machines/{machine_id}").json()

        assert body["id"] == machine_id
        assert "first_seen_at" in body and "last_seen_at" in body

    def test_an_unknown_machine_is_a_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/machines/01ZZZZZZZZZZZZZZZZZZZZZZZZ")

        assert response.status_code == 404, response.text


class TestTheBenchmarksApi:
    def test_it_lists_every_suite_the_run_engine_can_execute(self, client: TestClient) -> None:
        """Read from the registry the executor uses, so a listed suite is a runnable one."""
        body = client.get("/api/v1/benchmarks").json()

        keys = {row["key"] for row in body["items"]}
        assert "native.echo" in keys and "native.performance" in keys
        assert all(row["manifest_hash"].startswith("sha256:") for row in body["items"])

    def test_each_suite_declares_its_headline_metric(self, client: TestClient) -> None:
        body = client.get("/api/v1/benchmarks").json()
        echo = next(row for row in body["items"] if row["key"] == "native.echo")

        assert echo["headline_metric"] == "harness_roundtrip_success"

    def test_one_suite_carries_its_tests_and_their_metrics(self, client: TestClient) -> None:
        body = client.get("/api/v1/benchmarks/native.performance").json()

        assert body["key"] == "native.performance"
        assert body["tests"], "a suite with no tests would run instantly and report nothing"
        assert all("metrics" in test and "case_count" in test for test in body["tests"])

    def test_an_unknown_suite_is_a_404_that_names_what_is_installed(
        self, client: TestClient
    ) -> None:
        response = client.get("/api/v1/benchmarks/native.nonexistent")

        assert response.status_code == 404, response.text
        assert response.json()["error"]["details"]["installed"]


class TestRepeatOverHttp:
    def test_it_queues_a_new_run_with_the_originals_configuration(self, client: TestClient) -> None:
        """The service and the CLI had this for five phases; the API declared it and did not."""
        original = _completed_run(client)

        response = client.post(f"/api/v1/runs/{original}/repeat")

        assert response.status_code == 201, response.text
        repeated = response.json()
        assert repeated["id"] != original

        first = client.get(f"/api/v1/runs/{original}").json()
        second = client.get(f"/api/v1/runs/{repeated['id']}").json()
        assert second["effective_config"] == first["effective_config"]

    def test_repeating_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        response = client.post("/api/v1/runs/01ZZZZZZZZZZZZZZZZZZZZZZZZ/repeat")

        assert response.status_code == 404, response.text
