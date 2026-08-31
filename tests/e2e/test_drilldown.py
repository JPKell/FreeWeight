"""End-to-end: the results experience, from a metric row to the request that produced it.

Phase 10 acceptance criterion 2 — "no headline number is more than two interactions from its raw
source" — is checked from the dashboard in ``test_dashboard.py``. This file checks the other
entrance to the same chain: the results table, which is where someone arrives who knows what
metric they care about and does not know which run produced it.

The case inspector is the end of the chain, and it has to carry everything at once: the prompt's
identity, the response, the tool calls, the per-criterion scoring and the telemetry observed while
it ran. A page that made the reader fetch any of those separately would put a loading state
between a person and the evidence.
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
    """A migrated database and a fake-provider configuration."""
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__COOLDOWN_SECONDS", "0")
    # The shipped default waits for three consecutive quiet telemetry observations at one-second
    # intervals before the first provider call (spec §13). That is ~2.2 s of every run here, it is
    # the same wait on every one of them, and it is exercised in its own right by
    # tests/integration/test_performance_benchmark.py::TestIdleDetection, which covers all three
    # of its outcomes. Paying it again in every end-to-end journey buys nothing but minutes —
    # the same argument the cooldown line above makes. `0` is the documented way to disable it.
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


class TestTheResultsTable:
    def test_the_empty_state_says_what_would_fill_it(self, client: TestClient) -> None:
        response = client.get("/results")

        assert response.status_code == 200
        assert "No results match" in response.text
        assert 'href="/runs"' in response.text

    def test_a_completed_run_appears_with_its_counts_and_its_source(
        self, client: TestClient
    ) -> None:
        run_id = _completed_run(client)

        response = client.get("/results")

        assert response.status_code == 200
        assert "harness_roundtrip_success" in response.text
        assert f'href="/runs/{run_id}' in response.text

    def test_filters_narrow_the_table_and_survive_the_round_trip(self, client: TestClient) -> None:
        _completed_run(client)

        matching = client.get("/results", params={"metric_key": "harness_roundtrip_success"})
        missing = client.get("/results", params={"metric_key": "no_such_metric"})

        assert "harness_roundtrip_success" in matching.text
        assert "No results match" in missing.text
        # The filter bar keeps what was typed, so a refined search starts from the last one.
        assert 'value="no_such_metric"' in missing.text

    def test_an_ambiguous_model_reference_is_refused_rather_than_guessed(
        self, client: TestClient
    ) -> None:
        _completed_run(client)

        response = client.get("/results", params={"model": "definitely-not-a-model"})

        assert response.status_code == 404
        assert "MODEL_NOT_FOUND" in response.text

    def test_the_api_returns_the_collection_envelope(self, client: TestClient) -> None:
        _completed_run(client)

        body = client.get("/api/v1/results").json()

        assert "items" in body
        assert "page" in body
        assert body["page"]["has_more"] is False
        assert all("sample_count" in item for item in body["items"])

    def test_pagination_is_a_total_order_with_no_gap_and_no_repeat(
        self, client: TestClient
    ) -> None:
        """API standards §6: sort order is total, so a cursor can neither skip nor repeat."""
        _completed_run(client)

        first = client.get("/api/v1/results", params={"limit": 2}).json()
        assert first["page"]["has_more"] is True
        seen = [item["metric_value_id"] for item in first["items"]]

        cursor = first["page"]["next_cursor"]
        while cursor:
            page = client.get("/api/v1/results", params={"limit": 2, "cursor": cursor}).json()
            for item in page["items"]:
                key = item["metric_value_id"]
                assert key not in seen, "a cursor repeated a row"
                seen.append(key)
            cursor = page["page"]["next_cursor"]

        everything = client.get("/api/v1/results", params={"limit": 500}).json()
        assert len(seen) == len(everything["items"]), "a cursor skipped a row"

    def test_a_forged_cursor_is_refused_rather_than_decoded(self, client: TestClient) -> None:
        response = client.get("/api/v1/results", params={"cursor": "not-ours"})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestTheCaseInspector:
    def test_it_shows_the_prompt_identity_the_response_and_the_scoring(
        self, client: TestClient
    ) -> None:
        run_id = _completed_run(client)
        tests = client.get(f"/api/v1/runs/{run_id}/tests").json()["tests"]
        samples = client.get(f"/api/v1/runs/{run_id}/tests/{tests[0]['id']}/samples").json()[
            "samples"
        ]
        sample_id = samples[0]["id"]

        page = client.get(f"/results/samples/{sample_id}")

        assert page.status_code == 200
        assert sample_id in page.text
        assert samples[0]["case_id"] in page.text
        assert "<h3>Scoring</h3>" in page.text
        assert f'href="/runs/{run_id}"' in page.text

    def test_a_missing_sample_is_a_404_that_says_so(self, client: TestClient) -> None:
        response = client.get("/results/samples/01AAAAAAAAAAAAAAAAAAAAAAAA")

        assert response.status_code == 404
        assert "NOT_FOUND" in response.text

    def test_it_says_no_reading_rather_than_zero_when_telemetry_is_absent(
        self, client: TestClient
    ) -> None:
        """ADR-0016 again, on the page where a reader is closest to the raw numbers."""
        run_id = _completed_run(client)
        tests = client.get(f"/api/v1/runs/{run_id}/tests").json()["tests"]
        samples = client.get(f"/api/v1/runs/{run_id}/tests/{tests[0]['id']}/samples").json()[
            "samples"
        ]

        page = client.get(f"/results/samples/{samples[0]['id']}")

        assert "Not zero usage: no reading." in page.text


class TestTheCliDrillDown:
    def test_results_show_lists_the_tests_and_every_metric(self, client: TestClient) -> None:
        run_id = _completed_run(client)

        result = runner.invoke(cli_app, ["results", "show", run_id])

        assert result.exit_code == 0, result.output
        assert "harness_roundtrip_success" in result.output
        assert "echo.short" in result.output

    def test_results_list_prints_the_same_field_names_as_the_api(self, client: TestClient) -> None:
        """CLI standards §3: ``--json`` prints the document the HTTP API returns."""
        import json

        _completed_run(client)

        result = runner.invoke(cli_app, ["results", "list", "--json"])
        assert result.exit_code == 0, result.output
        from_cli = json.loads(result.output)
        from_api = client.get("/api/v1/results", params={"limit": 50}).json()

        assert sorted(from_cli["items"][0]) == sorted(from_api["items"][0])
        assert from_cli["page"].keys() == from_api["page"].keys()

    def test_an_unsupported_metric_prints_an_em_dash_and_its_reason(
        self, client: TestClient
    ) -> None:
        _completed_run(client)

        result = runner.invoke(cli_app, ["results", "list", "--limit", "500"])

        assert result.exit_code == 0, result.output
        assert "—" in result.output
        assert " 0.0000 " not in result.output.replace("—", "")

    def test_a_malformed_since_is_a_usage_error_not_a_traceback(self, client: TestClient) -> None:
        result = runner.invoke(cli_app, ["results", "list", "--since", "yesterday"])

        assert result.exit_code == 2
        assert "RFC 3339" in result.output


class TestComparingModelsRatherThanRuns:
    """``PHASE9_ISSUES.md`` §9, closed: ``subjects`` accepts models when ``suite`` says at what."""

    def test_a_model_subject_resolves_to_its_latest_completed_run(self, client: TestClient) -> None:
        first = _completed_run(client)
        second = _completed_run(client)
        assert first != second

        compared = client.get(
            "/api/v1/results/compare",
            params={"subjects": f"fake-model:8b-q8_0,{first}", "suite": "native.echo"},
        )

        assert compared.status_code == 200, compared.text
        subjects = [subject["run_id"] for subject in compared.json()["subjects"]]
        assert subjects == [second, first], "the model did not resolve to the newest run"

    def test_a_model_subject_without_a_suite_is_refused_and_says_why(
        self, client: TestClient
    ) -> None:
        run_id = _completed_run(client)

        refused = client.get(
            "/api/v1/results/compare",
            params={"subjects": f"fake-model:8b-q8_0,{run_id}"},
        )

        assert refused.status_code == 400
        assert "needs a suite" in refused.json()["error"]["message"]

    def test_a_model_with_no_completed_run_of_that_suite_is_refused_by_name(
        self, client: TestClient
    ) -> None:
        run_id = _completed_run(client)

        refused = client.get(
            "/api/v1/results/compare",
            params={
                "subjects": f"fake-model:8b-q8_0,{run_id}",
                "suite": "native.performance",
            },
        )

        assert refused.status_code in (400, 404)
        assert "native.performance" in refused.text

    def test_the_cli_reads_subjects_the_same_way(self, client: TestClient) -> None:
        """CLI standards §3: one document, two surfaces, one reading of the arguments."""
        import json as json_module

        first = _completed_run(client)
        _completed_run(client)

        result = runner.invoke(
            cli_app,
            [
                "results",
                "compare",
                "fake-model:8b-q8_0",
                first,
                "--suite",
                "native.echo",
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        from_cli = json_module.loads(result.output)
        from_api = client.get(
            "/api/v1/results/compare",
            params={"subjects": f"fake-model:8b-q8_0,{first}", "suite": "native.echo"},
        ).json()
        assert [subject["run_id"] for subject in from_cli["subjects"]] == [
            subject["run_id"] for subject in from_api["subjects"]
        ]
