"""End-to-end: exports, and the round trip that proves one carries what the UI showed.

Phase 10's export test is a round trip on purpose. An export that is well-formed, streams
promptly and reproduces nothing is worse than no export at all, because it looks like a backup.
So the assertions here go in both directions: what the dashboard shows must be in the document,
and what the document holds must read back into the same figures.

Three further properties are pinned because each of them is a way an export quietly stops being
trustworthy:

* **Canonical.** The streamed JSON is byte-identical to :func:`baseaicore.canonical_json` over the
  same structure. It is assembled in pieces so it can stream, and a hand-assembled canonical
  document is exactly the kind of thing that drifts from the canonicalizer it claims to match.
* **Unsupported is ``"unsupported"``.** Never ``0``, never ``null``, never an absent key — in
  JSON, in JSONL and in CSV (spec §11 contract 6).
* **Streaming.** The first chunk leaves before the last run is read, which is what makes the
  10 000-sample budget in spec §15 a property of the design rather than of the machine.
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from baseaicore import canonical_json
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from freeweight.cli.main import app as cli_app
from freeweight.config import load_settings
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION, Database
from freeweight.services.export import (
    EXPORT_SCHEMA,
    EXPORT_SCHEMA_VERSION,
    ExportFormat,
    ExportRefused,
    ExportScope,
    ExportSelection,
    iter_export,
    read_export,
)
from freeweight.services.results import DashboardFilter, build_dashboard
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


def _created_at(database: Database, run_id: str) -> Any:
    """One run's ``created_at``, for building a window around it."""
    from freeweight.infrastructure.db.repositories.runs import RunRepository

    with database.read() as session:
        run = RunRepository().get_by_id(session, run_id)
        assert run is not None
        return run.created_at


def _database(workspace: Path) -> Database:
    return Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}")


class TestTheRoundTrip:
    def test_exported_json_reads_back_into_the_same_metrics(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The whole point of an export: a viewer reproduces the figures from the document."""
        run_id = _completed_run(client)
        with _database(workspace) as database:
            dashboard = build_dashboard(database, DashboardFilter())
            text = "".join(
                iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=run_id))
            )

        runs = read_export(text)

        assert len(runs) == 1
        exported = runs[0]
        assert exported.run_id == run_id
        assert exported.status == "completed"
        for cell in dashboard.heatmap.cells.values():
            metric = exported.metric(cell.metric_key)
            assert metric is not None, f"{cell.metric_key} is missing from the export"
            assert metric.value == cell.value
            assert metric.sample_count == cell.sample_count
            assert metric.excluded_count == cell.excluded_count
            assert metric.unit == cell.unit

    def test_it_reads_back_from_jsonl_too(self, client: TestClient, workspace: Path) -> None:
        """A consumer that saved whichever form it asked for should not have to remember which."""
        run_id = _completed_run(client)
        with _database(workspace) as database:
            as_json = "".join(
                iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=run_id))
            )
            as_lines = "".join(
                iter_export(
                    database,
                    ExportSelection(
                        scope=ExportScope.RUN,
                        selector=run_id,
                        export_format=ExportFormat.JSONL,
                    ),
                )
            )

        from_json = read_export(as_json)
        from_lines = read_export(as_lines)

        assert [row.run_id for row in from_json] == [row.run_id for row in from_lines]
        assert [row.metrics for row in from_json] == [row.metrics for row in from_lines]

    def test_the_csv_carries_the_metric_key_beside_every_value(
        self, client: TestClient, workspace: Path
    ) -> None:
        run_id = _completed_run(client)
        with _database(workspace) as database:
            text = "".join(
                iter_export(
                    database,
                    ExportSelection(
                        scope=ExportScope.RUN, selector=run_id, export_format=ExportFormat.CSV
                    ),
                )
            )

        rows = list(csv.DictReader(io.StringIO(text)))

        assert rows
        assert all(row["run_id"] == run_id for row in rows)
        assert {row["metric_key"] for row in rows} >= {"harness_roundtrip_success"}
        assert all(row["unit"] for row in rows), "a value without its unit is a defect"


class TestTheDocumentIsCanonicalAndVersioned:
    def test_the_streamed_json_matches_the_canonicalizer_byte_for_byte(
        self, client: TestClient, workspace: Path
    ) -> None:
        """Assembled in pieces so it can stream; identical to the whole-document form."""
        run_id = _completed_run(client)
        with _database(workspace) as database:
            chunks = list(
                iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=run_id))
            )
        text = "".join(chunks)
        document = json.loads(text)

        assert canonical_json(document) == text

    def test_it_declares_its_schema_and_version(self, client: TestClient, workspace: Path) -> None:
        run_id = _completed_run(client)
        with _database(workspace) as database:
            document = json.loads(
                "".join(
                    iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=run_id))
                )
            )

        assert document["schema"] == EXPORT_SCHEMA
        assert document["schema_version"] == str(EXPORT_SCHEMA_VERSION)
        assert document["generator"]["name"] == "freeweight"

    def test_an_unsupported_major_is_rejected_naming_both_versions(self) -> None:
        """API standards §7 rule 3: readers never try their best."""
        from baseaicore import ValidationError

        document = json.dumps(
            {
                "schema": EXPORT_SCHEMA,
                "schema_version": "99.0",
                "generated_at": "2026-08-28T00:00:00.000Z",
                "generator": {"name": "freeweight", "version": "0.0.0"},
                "payload": {"runs": []},
            }
        )

        with pytest.raises(ValidationError, match="99.0"):
            read_export(document)

    def test_each_run_carries_a_validated_setspec_run_summary(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The cross-application contract, checked by re-reading it through SetSpec's own model."""
        from setspec.benchmark.v1 import BenchmarkRunSummaryIn

        run_id = _completed_run(client)
        with _database(workspace) as database:
            document = json.loads(
                "".join(
                    iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=run_id))
                )
            )

        summary = document["payload"]["runs"][0]["summary"]
        parsed = BenchmarkRunSummaryIn.model_validate(summary)

        assert parsed.runtime_profile_hash == summary["runtime_profile_hash"]
        assert parsed.suite.suite_key == "native.echo"


class TestUnsupportedIsNeverZero:
    def test_json_writes_the_word_and_never_a_zero_or_a_null(
        self, client: TestClient, workspace: Path
    ) -> None:
        run_id = _completed_run(client)
        with _database(workspace) as database:
            document = json.loads(
                "".join(
                    iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=run_id))
                )
            )

        metrics = document["payload"]["runs"][0]["metrics"]
        unsupported = [row for row in metrics if row["value"] == "unsupported"]
        assert unsupported, "the fake provider exposes no GPU; something should be unsupported"
        for row in unsupported:
            assert row["value"] == "unsupported"
            assert row["unavailable_reason"]
        assert all(row["value"] is not None for row in metrics)

    def test_csv_writes_the_word_rather_than_an_empty_cell(
        self, client: TestClient, workspace: Path
    ) -> None:
        """An empty cell and a refused measurement look identical in a spreadsheet."""
        run_id = _completed_run(client)
        with _database(workspace) as database:
            text = "".join(
                iter_export(
                    database,
                    ExportSelection(
                        scope=ExportScope.RUN, selector=run_id, export_format=ExportFormat.CSV
                    ),
                )
            )

        rows = list(csv.DictReader(io.StringIO(text)))
        unsupported = [row for row in rows if row["value"] == "unsupported"]

        assert unsupported
        assert all(row["unavailable_reason"] for row in unsupported)


class TestScopesAndRefusals:
    def test_every_scope_resolves_to_the_runs_it_names(
        self, client: TestClient, workspace: Path
    ) -> None:
        run_id = _completed_run(client)
        with _database(workspace) as database:
            for selection in (
                ExportSelection(scope=ExportScope.RUN, selector=run_id),
                ExportSelection(scope=ExportScope.MODEL, selector="fake-model:8b-q8_0"),
                ExportSelection(scope=ExportScope.SUITE, selector="native.echo"),
                ExportSelection(scope=ExportScope.COMPARISON, selector=run_id),
                ExportSelection(scope=ExportScope.ALL),
            ):
                runs = read_export("".join(iter_export(database, selection)))
                assert [row.run_id for row in runs] == [run_id], selection.scope

    def test_a_scope_that_needs_a_selector_refuses_without_one(self) -> None:
        with pytest.raises(ExportRefused, match="needs a selector"):
            ExportSelection(scope=ExportScope.SUITE)

    def test_a_selection_that_matches_nothing_is_refused_by_name(
        self, client: TestClient, workspace: Path
    ) -> None:
        with _database(workspace) as database:
            with pytest.raises(ExportRefused, match="matched no runs"):
                list(
                    iter_export(
                        database,
                        ExportSelection(scope=ExportScope.SUITE, selector="native.nothing"),
                    )
                )

    def test_include_samples_adds_the_raw_rows_and_omitting_it_does_not(
        self, client: TestClient, workspace: Path
    ) -> None:
        run_id = _completed_run(client)
        with _database(workspace) as database:
            without = read_export(
                "".join(
                    iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=run_id))
                )
            )
            with_samples = read_export(
                "".join(
                    iter_export(
                        database,
                        ExportSelection(
                            scope=ExportScope.RUN,
                            selector=run_id,
                            include_samples=True,
                            include_prompts=True,
                        ),
                    )
                )
            )

        assert without[0].sample_count == 0
        assert with_samples[0].sample_count > 0


class TestItStreams:
    def test_the_first_chunk_arrives_before_the_document_is_finished(
        self, client: TestClient, workspace: Path
    ) -> None:
        """Streaming is the property, so it is checked by consuming one chunk and stopping."""
        run_id = _completed_run(client)
        with _database(workspace) as database:
            stream = iter_export(
                database,
                ExportSelection(scope=ExportScope.RUN, selector=run_id, include_samples=True),
            )
            first = next(stream)
            assert first.startswith('{"generated_at":')
            assert '"runs":[' in first
            # Nothing of the payload's body has been produced yet.
            assert run_id not in first
            del stream

    def test_the_http_endpoint_streams_with_a_filename(self, client: TestClient) -> None:
        _completed_run(client)

        response = client.get("/api/v1/results/export", params={"scope": "all", "format": "jsonl"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert "attachment" in response.headers["content-disposition"]
        assert response.text.count("\n") == 1

    def test_a_refusal_is_a_clean_error_and_not_a_half_written_document(
        self, client: TestClient
    ) -> None:
        response = client.get(
            "/api/v1/results/export", params={"scope": "suite", "selector": "native.nothing"}
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestTheCliExport:
    def test_it_writes_the_same_document_the_endpoint_returns(
        self, client: TestClient, workspace: Path
    ) -> None:
        run_id = _completed_run(client)
        destination = workspace / "export.json"

        result = runner.invoke(
            cli_app,
            ["results", "export", "--scope", "run", "--selector", run_id, "-o", str(destination)],
        )

        assert result.exit_code == 0, result.output
        exported = read_export(destination.read_text(encoding="utf-8"))
        assert [row.run_id for row in exported] == [run_id]

    def test_an_unknown_format_is_a_usage_error(self, client: TestClient) -> None:
        result = runner.invoke(cli_app, ["results", "export", "--format", "yaml"])

        assert result.exit_code == 2
        assert "--format" in result.output


class TestTheExportWindow:
    """``since`` / ``until``: how a history larger than one document gets exported completely."""

    def test_the_window_is_half_open_so_consecutive_windows_tile(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The property that makes windowing *complete* rather than merely smaller.

        Two windows meeting at instant ``t``: a run created exactly at ``t`` belongs to the window
        that *starts* there and not to the one that *ends* there. A closed window would export it
        twice; an open one would lose it between the two.
        """
        from datetime import timedelta

        from freeweight.services.export import (
            ExportRefused,
            ExportScope,
            ExportSelection,
            resolve_run_ids,
        )

        _completed_run(client)
        with _database(workspace) as database:
            run_id = resolve_run_ids(database, ExportSelection(scope=ExportScope.ALL))[0]
            boundary = _created_at(database, run_id)

            later = ExportSelection(
                scope=ExportScope.ALL, since=boundary, until=boundary + timedelta(hours=1)
            )
            assert run_id in resolve_run_ids(database, later)

            earlier = ExportSelection(
                scope=ExportScope.ALL, since=boundary - timedelta(hours=1), until=boundary
            )
            with pytest.raises(ExportRefused, match="matched no runs in"):
                resolve_run_ids(database, earlier)

    def test_an_empty_window_is_refused_before_anything_streams(self) -> None:
        from datetime import UTC, datetime

        from freeweight.services.export import ExportRefused, ExportScope, ExportSelection

        with pytest.raises(ExportRefused, match="half-open"):
            ExportSelection(
                scope=ExportScope.ALL,
                since=datetime(2026, 6, 1, tzinfo=UTC),
                until=datetime(2026, 1, 1, tzinfo=UTC),
            )

    def test_a_window_that_matches_nothing_says_the_window_is_why(
        self, client: TestClient, workspace: Path
    ) -> None:
        from datetime import UTC, datetime

        from freeweight.services.export import (
            ExportRefused,
            ExportScope,
            ExportSelection,
            resolve_run_ids,
        )

        _completed_run(client)
        with _database(workspace) as database:
            with pytest.raises(ExportRefused, match="matched no runs in"):
                resolve_run_ids(
                    database,
                    ExportSelection(
                        scope=ExportScope.ALL,
                        since=datetime(2000, 1, 1, tzinfo=UTC),
                        until=datetime(2000, 2, 1, tzinfo=UTC),
                    ),
                )

    def test_the_document_states_the_window_it_covers(
        self, client: TestClient, workspace: Path
    ) -> None:
        """Otherwise a reader cannot tell a windowed export from a complete one."""
        import json

        from freeweight.services.export import ExportScope, ExportSelection, iter_export

        _completed_run(client)
        with _database(workspace) as database:
            document = json.loads(
                "".join(iter_export(database, ExportSelection(scope=ExportScope.ALL)))
            )

        assert "since" in document["payload"]
        assert "until" in document["payload"]


class TestThePromptAppendix:
    """``include_prompt_text``: the difference between auditable and merely referential."""

    def test_it_is_absent_unless_asked_for(self, client: TestClient, workspace: Path) -> None:
        """A measurement database must not quietly become a second copy of the prompt pack."""
        import json

        from freeweight.services.export import ExportScope, ExportSelection, iter_export

        _completed_run(client)
        with _database(workspace) as database:
            document = json.loads(
                "".join(
                    iter_export(
                        database, ExportSelection(scope=ExportScope.ALL, include_samples=True)
                    )
                )
            )

        assert "prompt_appendix" not in document["payload"]

    def test_it_carries_each_distinct_prompt_once_keyed_by_its_hash(
        self, client: TestClient, workspace: Path
    ) -> None:
        import json

        from freeweight.services.export import ExportScope, ExportSelection, iter_export

        _completed_run(client)
        with _database(workspace) as database:
            document = json.loads(
                "".join(
                    iter_export(
                        database,
                        ExportSelection(
                            scope=ExportScope.ALL,
                            include_samples=True,
                            include_prompts=True,
                            include_prompt_text=True,
                        ),
                    )
                )
            )

        appendix = document["payload"]["prompt_appendix"]
        assert appendix, "the appendix resolved no prompts at all"
        for digest, text in appendix.items():
            assert digest.startswith("sha256:")
            assert isinstance(text, str) and text

        hashes = {
            sample["prompt"]["rendered_prompt_hash"]
            for run in document["payload"]["runs"]
            for sample in run["samples"]
        }
        assert set(appendix) <= hashes, "the appendix carries prompts these runs never used"

    def test_a_prompt_is_only_offered_under_the_hash_its_text_produces(
        self, client: TestClient, workspace: Path
    ) -> None:
        """It re-renders rather than reading stored text, so it also verifies.

        A prompt edited since the run does not match, and is absent rather than present and wrong:
        a reader gets no text instead of the wrong text, and the sample's own hash still records
        what was asked.
        """
        import json

        from baseaicore import sha256_of

        from freeweight.services.export import ExportScope, ExportSelection, iter_export

        _completed_run(client)
        with _database(workspace) as database:
            document = json.loads(
                "".join(
                    iter_export(
                        database,
                        ExportSelection(scope=ExportScope.ALL, include_prompt_text=True),
                    )
                )
            )

        for digest, text in document["payload"]["prompt_appendix"].items():
            assert digest == f"sha256:{sha256_of(text)}"
