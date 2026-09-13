"""Row WX7's additive endpoints over HTTP, against the fake provider.

Five surfaces, each added because the console (and, for context-fit, LoadCoach through it) had no
way to ask a question FreeWeight already knew the answer to: what to *call* a model, which models
are of a given size, what an operator calls a machine, which tests of a run actually ran, and how
much context fit. Routable is not the same as correct, so each is exercised for its shape here,
as `test_declared_endpoints.py` does for §7.1's list.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner
from weightsdb import MigrationRunner, create_engine_for

from freeweight.cli.main import app as cli_app
from freeweight.config import load_settings
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.web.app import create_app

runner = CliRunner()
_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A migrated database, a fake provider, and an adapter directory with one bare artifact."""
    database = tmp_path / "freeweight.sqlite3"
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    (adapters / "terse.gguf").write_bytes(b"lora-weights")
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    monkeypatch.setenv("FREEWEIGHT_ADAPTERS__DIRECTORY", str(adapters))
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
    """A served application over ``workspace``, lifespan entered."""
    loaded = load_settings(config_path=workspace / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _completed_run(client: TestClient, suite: str = "native.echo") -> str:
    """Discover models, run ``suite``, and return the completed run's ID."""
    assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0
    created = client.post("/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suite": suite})
    assert created.status_code == 201, created.text
    run_id = str(created.json()["id"])
    deadline = time.monotonic() + 120.0
    while True:
        body = client.get(f"/api/v1/runs/{run_id}").json()
        if body["status"] in _TERMINAL:
            assert body["status"] == "completed", body
            return run_id
        assert time.monotonic() < deadline, f"run stuck in {body['status']}"
        time.sleep(0.05)


class TestModels:
    """``display_name`` and the parameter filters on ``GET /models``."""

    def test_every_item_carries_a_display_name(self, client: TestClient) -> None:
        """The list names each model the way a page should, so no template has to decide."""
        assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0

        items = client.get("/api/v1/models").json()["items"]

        assert items
        for item in items:
            assert item["display_name"] in (item["provider_model_name"], item["canonical_id"])

    def test_one_model_carries_the_same_display_name(self, client: TestClient) -> None:
        """``GET /models/{ref}`` is the list's own answer, not a second rule."""
        assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0
        listed = client.get("/api/v1/models").json()["items"][0]

        one = client.get(f"/api/v1/models/{listed['id']}").json()

        assert one["display_name"] == listed["display_name"]

    def test_a_parameter_range_filters_the_list(self, client: TestClient) -> None:
        """Two bounds beside the existing kind, family and quantization filters."""
        assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0
        items = client.get("/api/v1/models").json()["items"]
        counted = [one for one in items if isinstance(one["parameter_count"], int)]
        assert counted, "the fake provider reports a parameter count"
        floor = max(one["parameter_count"] for one in counted)

        above = client.get("/api/v1/models", params={"min_parameters": floor}).json()["items"]
        below = client.get("/api/v1/models", params={"max_parameters": floor - 1}).json()["items"]

        assert [one["canonical_id"] for one in above] == [
            one["canonical_id"] for one in counted if one["parameter_count"] >= floor
        ]
        assert all(one["parameter_count"] < floor for one in below)

    def test_an_unreported_parameter_count_is_outside_every_bound(self, client: TestClient) -> None:
        """ADR-0016: an unsupported measurement is not a number, and never compares as zero."""
        assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0

        items = client.get("/api/v1/models", params={"min_parameters": 0}).json()["items"]

        assert all(one["parameter_count"] is not None for one in items)


class TestMachines:
    """The nickname a machine can be given, and the display name that follows from it."""

    def test_a_machine_can_be_named_and_unnamed(self, client: TestClient) -> None:
        """The operator's label, written and cleared through ``PATCH``."""
        _completed_run(client)
        machine = client.get("/api/v1/machines").json()["items"][0]
        assert machine["nickname"] is None
        assert machine["display_name"] == (machine["hostname"] or machine["id"])

        named = client.patch(
            f"/api/v1/machines/{machine['id']}", json={"nickname": "the workstation"}
        )

        assert named.status_code == 200, named.text
        assert named.json()["nickname"] == "the workstation"
        after = client.get(f"/api/v1/machines/{machine['id']}").json()
        assert after["display_name"] == "the workstation"
        assert after["machine_fingerprint"] == machine["machine_fingerprint"]

        cleared = client.patch(f"/api/v1/machines/{machine['id']}", json={"nickname": "  "})

        assert cleared.json()["nickname"] is None
        assert client.get(f"/api/v1/machines/{machine['id']}").json()["nickname"] is None

    def test_an_unknown_machine_is_refused(self, client: TestClient) -> None:
        """An exact ULID or nothing: a write must not rename a machine by near miss."""
        answer = client.patch("/api/v1/machines/01JZZZZZZZZZZZZZZZZZZZZZZZ", json={"nickname": "x"})

        assert answer.status_code == 404, answer.text


class TestDashboard:
    """The tests matrix beside the heatmap."""

    def test_the_matrix_names_every_test_of_the_runs_the_heatmap_draws_from(
        self, client: TestClient
    ) -> None:
        """The heatmap says how good; this says what actually ran."""
        run_id = _completed_run(client)

        body = client.get("/api/v1/dashboard").json()

        matrix = body["tests_matrix"]
        assert matrix["models"] and matrix["tests"]
        assert matrix["cells"]
        for cell in matrix["cells"]:
            assert cell["status"] in {"completed", "failed", "skipped", "cancelled"}
            assert cell["model"] in matrix["models"]
            assert cell["test"] in matrix["tests"]
        assert {cell["run_id"] for cell in matrix["cells"]} == {run_id}

    def test_an_empty_database_has_an_empty_matrix(self, client: TestClient) -> None:
        """Nothing measured is an empty matrix, not a grid of zeroes."""
        body = client.get("/api/v1/dashboard").json()

        assert body["tests_matrix"] == {"models": [], "tests": [], "cells": []}


class TestContextFit:
    """``GET /results/context-fit``, the pipe row WX9 reads."""

    def test_nothing_measured_is_an_empty_list(self, client: TestClient) -> None:
        """An installation that never ran ``native.memory_kv`` has no reading, and says so."""
        answer = client.get("/api/v1/results/context-fit")

        assert answer.status_code == 200, answer.text
        assert answer.json() == {"items": []}

    def test_a_memory_kv_run_produces_one_reading(self, client: TestClient) -> None:
        """One reading per (model, runtime profile, machine), with the profile it was measured
        under — a context number without its profile is a claim about the configuration."""
        _completed_run(client, suite="native.memory_kv")

        items = client.get("/api/v1/results/context-fit").json()["items"]

        assert len(items) == 1
        reading = items[0]
        assert set(reading) == {
            "model",
            "runtime_profile_hash",
            "machine_fingerprint",
            "max_successful_context_tokens",
            "capped_by_configuration",
            "observed_mb_per_1k_context",
            "run_id",
            "measured_at",
        }
        assert reading["runtime_profile_hash"]
        assert reading["max_successful_context_tokens"] != 0


class TestAdapterDraft:
    """``POST /adapters/{name}/draft``: a proposal, and never a registration."""

    def test_a_draft_is_written_and_registers_nothing(self, client: TestClient) -> None:
        """The draft appears under ``drafts``; ``adapters`` stays empty (ADR-0061 rule 4)."""
        answer = client.post(
            "/api/v1/adapters/terse/draft", json={"base_model_name": "fake-model:8b-q8_0"}
        )

        assert answer.status_code == 200, answer.text
        assert answer.json()["payload"]["name"] == "terse"
        catalog = client.get("/api/v1/adapters").json()
        assert not catalog["adapters"]
        assert [Path(one).name for one in catalog["drafts"]] == ["terse.manifest.draft.json"]

    def test_a_second_draft_is_refused(self, client: TestClient) -> None:
        """What it would overwrite is a person's review."""
        client.post("/api/v1/adapters/terse/draft", json={"base_model_name": "x"})

        answer = client.post("/api/v1/adapters/terse/draft", json={"base_model_name": "x"})

        assert answer.status_code == 409, answer.text
        assert answer.json()["error"]["code"] == "DRAFT_REFUSED"

    def test_a_base_model_name_is_required(self, client: TestClient) -> None:
        """The one field nobody can read off a GGUF is the one the caller must supply."""
        answer = client.post("/api/v1/adapters/terse/draft", json={})

        assert answer.status_code == 400, answer.text
