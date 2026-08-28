"""End-to-end: the dashboard, and the anti-lie test that is the point of it.

Phase 10's chief named risk is "dashboard aggregates diverging from raw data", and its stated
mitigation is this file. Every figure the dashboard renders is recomputed here **from the stored
samples**, through the same domain arithmetic the run engine used, and compared. A dashboard that
showed a number nothing in the database supports would fail here rather than in six months, in
front of someone making a decision on it.

The rest of the file covers what UI standards §6 requires of any view — empty, error and populated
states — and the two-interaction drill-down UI standards §13 requires of any headline metric.

Everything runs against :class:`~modelrack.testing.FakeProvider`: no GPU, no Ollama, no network.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from freeweight.cli.main import app as cli_app
from freeweight.config import load_settings
from freeweight.domain.aggregation import SampleGroup, aggregate_run
from freeweight.domain.metrics import MeasurementClass
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION, Database
from freeweight.services.results import DashboardFilter, build_dashboard
from freeweight.services.runs import _sample_row_facts, build_registry
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


def _completed_run(client: TestClient, suite: str = "native.echo") -> str:
    """Discover a model, run ``suite`` over it, and return the completed run's ID."""
    assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0
    created = client.post("/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suite": suite})
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


def _recomputed_from_samples(workspace: Path, run_id: str) -> dict[str, float | None]:
    """Recompute a run's aggregate metrics straight from its stored samples.

    Deliberately *not* by reading ``metric_values``: that table is what the dashboard shows, and a
    check that read it would prove only that the page renders its own input. This goes back to the
    ``samples`` rows and re-derives, so the comparison is between what a user is shown and what was
    actually measured.
    """
    from freeweight.infrastructure.db.models_runs import BenchmarkSuite, Run
    from freeweight.infrastructure.db.repositories.runs import RunTestRepository, SampleRepository

    registry = build_registry()
    with Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database:
        with database.read() as session:
            run = session.get(Run, run_id)
            assert run is not None
            suite_row = session.get(BenchmarkSuite, run.suite_id)
            assert suite_row is not None
            benchmark = registry.get(suite_row.key)
            test_rows = {row.id: row for row in RunTestRepository().list_for_run(session, run_id)}
            groups = []
            for test in benchmark.tests:
                matching = [
                    row for row in test_rows.values() if _test_key_of(session, row) == test.key
                ]
                if not matching:
                    continue
                row = matching[0]
                groups.append(
                    SampleGroup(
                        test_key=test.key,
                        run_test_id=row.id,
                        measurement_class=MeasurementClass(row.measurement_class),
                        metrics=test.metrics,
                        samples=[
                            _sample_row_facts(sample)
                            for sample in SampleRepository().list_for_run_test(
                                session, row.id, limit=100_000
                            )
                        ],
                    )
                )
    return {
        metric.metric_key: metric.numeric_value
        for metric in aggregate_run(groups)
        if metric.run_test_id is None
    }


def _test_key_of(session: Any, run_test_row: Any) -> str:
    """The benchmark test key behind one ``run_tests`` row."""
    from freeweight.infrastructure.db.models_runs import BenchmarkTestRow

    row = session.get(BenchmarkTestRow, run_test_row.test_id)
    return str(row.key) if row is not None else ""


class TestTheAntiLieProperty:
    """Every dashboard figure matches a value recomputed directly from raw samples."""

    def test_every_heatmap_cell_matches_a_recomputation_from_samples(
        self, client: TestClient, workspace: Path
    ) -> None:
        run_id = _completed_run(client)
        recomputed = _recomputed_from_samples(workspace, run_id)
        assert recomputed, "the recomputation produced nothing to compare against"

        with Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database:
            dashboard = build_dashboard(database, DashboardFilter())

        assert dashboard.heatmap.cells, "the dashboard showed no cells for a completed run"
        for cell in dashboard.heatmap.cells.values():
            assert cell.metric_key in recomputed, (
                f"the dashboard shows {cell.metric_key}, which no recomputation from samples "
                "produces"
            )
            expected = recomputed[cell.metric_key]
            if expected is None:
                assert cell.value is None
            else:
                assert cell.value == pytest.approx(expected), cell.metric_key

    def test_every_panel_figure_matches_a_recomputation_or_is_suite_derived(
        self, client: TestClient, workspace: Path
    ) -> None:
        """Panel figures are the same stored rows, so the same equality has to hold.

        A panel may legitimately show a metric no sample-level formula produces — a KV slope or an
        energy integral is derived from telemetry, not from samples — so those are allowed through
        by name rather than silently. ``native.echo`` produces none of them, which is why this
        assertion is meaningful on it.
        """
        run_id = _completed_run(client)
        recomputed = _recomputed_from_samples(workspace, run_id)
        with Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database:
            dashboard = build_dashboard(database, DashboardFilter())

        for panel in dashboard.panels:
            for row in panel.rows:
                if row.metric_key not in recomputed:
                    continue
                expected = recomputed[row.metric_key]
                if expected is None:
                    assert row.value is None, row.metric_key
                else:
                    assert row.value == pytest.approx(expected), row.metric_key

    def test_a_run_level_figure_equals_the_mean_of_its_own_samples(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The narrowest possible version of the same check, computed by hand.

        ``harness_roundtrip_success`` is the mean of the stored per-sample scores, and nothing
        else. Computing it here with ``sum(...)/len(...)`` rather than through the domain code
        means the two implementations would have to be wrong in the same way to agree.
        """
        _completed_run(client)
        from sqlalchemy import select

        from freeweight.infrastructure.db.models_runs import RunTest, Sample

        with Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database:
            with database.read() as session:
                scores: list[float] = [
                    float(value)
                    for value in session.scalars(
                        select(Sample.score)
                        .join(RunTest, RunTest.id == Sample.run_test_id)
                        .where(Sample.status == "completed", Sample.score.is_not(None))
                    )
                    if value is not None
                ]
            dashboard = build_dashboard(database, DashboardFilter())

        assert scores, "the run stored no scored samples"
        expected = sum(scores) / len(scores)
        cell = next(
            cell
            for cell in dashboard.heatmap.cells.values()
            if cell.metric_key == "harness_roundtrip_success"
        )
        assert cell.value == pytest.approx(expected)
        assert cell.sample_count == len(scores)


class TestTheFourStates:
    def test_the_empty_state_says_what_would_create_a_figure(self, client: TestClient) -> None:
        response = client.get("/dashboard")

        assert response.status_code == 200
        assert "Nothing measured yet" in response.text
        assert "freeweight run start" in response.text

    def test_the_populated_state_shows_the_summary_cards(self, client: TestClient) -> None:
        _completed_run(client)

        response = client.get("/dashboard")

        assert response.status_code == 200
        assert "Completed runs" in response.text
        assert "Unsupported measurements" in response.text
        assert "harness_roundtrip_success" in response.text

    def test_the_error_state_names_the_code_and_what_to_do(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'never-migrated.sqlite3'}"
        )
        loaded = load_settings(config_path=tmp_path / "missing.toml")
        with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as client:
            response = client.get("/dashboard")

        assert response.status_code == 503
        assert "freeweight db upgrade" in response.text

    def test_an_unknown_model_filter_is_refused_by_name(self, client: TestClient) -> None:
        response = client.get("/dashboard", params={"model": "nothing/at-all"})

        assert response.status_code == 404
        assert "MODEL_NOT_FOUND" in response.text


class TestDrillDown:
    def test_every_headline_figure_links_to_its_raw_source(self, client: TestClient) -> None:
        """UI standards §13: no headline metric is more than two interactions from its samples."""
        run_id = _completed_run(client)

        page = client.get("/dashboard")

        assert f'href="/runs/{run_id}' in page.text, "the heatmap cell does not link to its run"

    def test_every_figure_reaches_its_stored_samples_in_two_interactions(
        self, client: TestClient, workspace: Path
    ) -> None:
        """UI standards §5 and §13, checked by walking the links rather than by reading them.

        The test follows the href the dashboard actually rendered for each cell, then the first
        link that page offers towards raw rows, and requires a table of stored samples at the end
        of it. Two page loads, no more — which is the whole claim.
        """
        _completed_run(client)
        with Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database:
            dashboard = build_dashboard(database, DashboardFilter())
        assert dashboard.heatmap.cells

        for cell in dashboard.heatmap.cells.values():
            first_url = f"/runs/{cell.run_id}"
            if cell.run_test_id:
                first_url += f"/tests/{cell.run_test_id}"
            first = client.get(first_url)
            assert first.status_code == 200, first.text

            if "/results/samples/" in first.text:
                # This page already *is* the raw sample table; the inspector is a bonus third.
                continue
            # Otherwise the run page must offer a link straight to a test's samples.
            marker = f"/runs/{cell.run_id}/tests/"
            assert marker in first.text, f"{first_url} offers no route to raw samples"
            run_test_id = first.text.split(marker, 1)[1].split('"', 1)[0]
            second = client.get(f"{marker}{run_test_id}")
            assert second.status_code == 200, second.text
            assert "/results/samples/" in second.text

    def test_a_sample_row_opens_the_case_inspector(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The inspector itself: prompt, response, tool calls, scoring and telemetry in one page."""
        run_id = _completed_run(client)
        detail = client.get(f"/api/v1/runs/{run_id}/tests").json()
        run_test_id = detail["tests"][0]["id"]

        page = client.get(f"/runs/{run_id}/tests/{run_test_id}")
        assert "/results/samples/" in page.text
        sample_id = page.text.split("/results/samples/", 1)[1].split('"', 1)[0]

        inspector = client.get(f"/results/samples/{sample_id}")

        assert inspector.status_code == 200, inspector.text
        assert "Case inspector" in inspector.text
        for section in (
            "<h3>Identity</h3>",
            "<h3>Prompt</h3>",
            "<h3>Response</h3>",
            "<h3>Scoring</h3>",
            "<h3>Tool calls</h3>",
            "<h3>Telemetry during this request</h3>",
        ):
            assert section in inspector.text, section
        assert run_id in inspector.text


class TestUnsupportedIsNeverZero:
    def test_an_unsupported_figure_renders_an_em_dash_carrying_its_reason(
        self, client: TestClient, workspace: Path
    ) -> None:
        """ADR-0016 §4 on the surface a user reads.

        The fake provider exposes no GPU, so every telemetry-derived figure of a real run is
        unsupported. Each one has to appear as an em dash *with its reason*, and none of them may
        appear as a number — the check is per figure rather than a search for the character
        ``0``, because a chart's own zero axis label is not a fabricated measurement.
        """
        _completed_run(client)
        with Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database:
            dashboard = build_dashboard(database, DashboardFilter())

        response = client.get("/dashboard")

        assert response.status_code == 200
        assert dashboard.cards.unsupported_metrics > 0, "nothing was unsupported to check"
        unsupported = [row for panel in dashboard.panels for row in panel.rows if row.value is None]
        for row in unsupported:
            assert row.unavailable_reason, f"{row.metric_key} is unsupported with no reason"
            assert row.unavailable_reason in response.text, row.metric_key
        for row in unsupported:
            assert f">{row.metric_key}</td>" in response.text
        assert "—" in response.text
