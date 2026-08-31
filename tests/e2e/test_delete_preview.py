"""End-to-end: data management, and the property that makes it safe to use.

Database standards §8 and spec §20 criterion 11 make three promises about deleting results:
the preview says exactly what will go, the deletion removes exactly that, and models and machines
survive it. This file checks all three against a real run's rows rather than against a mock, plus
the confirmation and backup behaviour that surrounds them.

The preview token is what binds the first promise to the second. It is a hash of the selection
*and* of the counts, recomputed at deletion time, so a database that changed between the two is a
refusal rather than a surprise. That refusal is tested here too: it is the difference between
"this will delete 412 rows" being a statement and being a hope.
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
from freeweight.services.database import MIGRATIONS_LOCATION, Database, get_status
from freeweight.services.database_admin import (
    DeletionScope,
    DeletionSelection,
    database_stats,
    delete_results,
    preview_deletion,
)
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


def _database(workspace: Path) -> Database:
    return Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}")


class TestThePreviewMatchesTheDeletion:
    def test_deletion_removes_exactly_what_the_preview_counted(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The property, checked table by table rather than in total.

        A total that matches while two tables are wrong in opposite directions would satisfy a
        looser assertion and would still be a defect.
        """
        run_id = _completed_run(client)
        with _database(workspace) as database:
            selection = DeletionSelection(scope=DeletionScope.RUN, selector=run_id)
            preview = preview_deletion(database, selection)
            assert preview.total_rows > 0

            outcome = delete_results(database, selection, token=preview.token)

        assert dict(outcome.deleted_counts) == dict(preview.removed_counts)

    def test_the_preview_counts_every_table_the_cascade_reaches(
        self, client: TestClient, workspace: Path
    ) -> None:
        """A cascade that removed more than the preview stated is a defect (database standards §8).

        Checked against the whole database rather than against the preview's own arithmetic:
        every table's row count after the deletion has to have fallen by exactly what the preview
        said it would, and by nothing more.
        """
        run_id = _completed_run(client)
        with _database(workspace) as database:
            before = dict(get_status(database).table_row_counts)
            selection = DeletionSelection(scope=DeletionScope.RUN, selector=run_id)
            preview = preview_deletion(database, selection)
            delete_results(database, selection, token=preview.token)
            after = dict(get_status(database).table_row_counts)

        for table, count in before.items():
            expected = count - preview.removed_counts.get(table, 0)
            assert after[table] == expected, f"{table} lost more or fewer rows than the preview"

    def test_models_and_machines_survive_a_result_deletion(
        self, client: TestClient, workspace: Path
    ) -> None:
        """Spec §20 criterion 11: history of what exists outlives history of what was measured."""
        run_id = _completed_run(client)
        with _database(workspace) as database:
            selection = DeletionSelection(scope=DeletionScope.RUN, selector=run_id)
            preview = preview_deletion(database, selection)
            assert preview.preserved_counts["models"] >= 1
            assert preview.preserved_counts["machines"] >= 1

            outcome = delete_results(database, selection, token=preview.token)

        assert outcome.preserved_counts_after == preview.preserved_counts
        assert "models" not in outcome.deleted_counts
        assert "machines" not in outcome.deleted_counts

    def test_a_stale_token_is_refused_rather_than_applied(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The database changed after the preview, so what the user was shown is no longer true."""
        first_run = _completed_run(client)
        with _database(workspace) as database:
            selection = DeletionSelection(scope=DeletionScope.ALL)
            stale = preview_deletion(database, selection)

        second_run = _completed_run(client)
        assert second_run != first_run

        with _database(workspace) as database:
            from weightsdb import DatabaseError

            with pytest.raises(DatabaseError, match="Preview it again"):
                delete_results(database, selection, token=stale.token)

            # And nothing was removed by the attempt.
            assert preview_deletion(database, selection).run_ids

    def test_a_token_from_a_different_selection_is_refused(
        self, client: TestClient, workspace: Path
    ) -> None:
        run_id = _completed_run(client)
        with _database(workspace) as database:
            from weightsdb import DatabaseError

            one = preview_deletion(
                database, DeletionSelection(scope=DeletionScope.RUN, selector=run_id)
            )
            with pytest.raises(DatabaseError):
                delete_results(
                    database, DeletionSelection(scope=DeletionScope.ALL), token=one.token
                )


class TestTheHttpFlow:
    def test_the_page_previews_before_it_offers_to_delete(self, client: TestClient) -> None:
        run_id = _completed_run(client)

        page = client.post("/database/preview", data={"scope": "run", "selector": run_id})

        assert page.status_code == 200
        assert "nothing has been deleted yet" in page.text
        assert "This will delete" in page.text
        assert "Models and machines are not touched." in page.text
        assert 'name="token"' in page.text

    def test_a_mistyped_confirmation_deletes_nothing(
        self, client: TestClient, workspace: Path
    ) -> None:
        run_id = _completed_run(client)
        preview = client.post(
            "/api/v1/database/delete-preview", json={"scope": "run", "selector": run_id}
        ).json()

        response = client.post(
            "/database/delete",
            data={
                "scope": "run",
                "selector": run_id,
                "token": preview["token"],
                "confirm": "yes",
            },
        )

        assert response.status_code == 400
        assert "Nothing was deleted" in response.text
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 200

    def test_a_typed_confirmation_deletes_and_shows_what_survived(self, client: TestClient) -> None:
        run_id = _completed_run(client)
        preview = client.post(
            "/api/v1/database/delete-preview", json={"scope": "run", "selector": run_id}
        ).json()

        response = client.post(
            "/database/delete",
            data={
                "scope": "run",
                "selector": run_id,
                "token": preview["token"],
                "confirm": "delete",
            },
        )

        assert response.status_code == 200
        assert "Deleted" in response.text
        assert "Untouched, counted after the deletion committed." in response.text
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 404

    def test_the_api_delete_requires_the_token(self, client: TestClient) -> None:
        run_id = _completed_run(client)

        refused = client.request(
            "DELETE",
            "/api/v1/database/results",
            json={"scope": "run", "selector": run_id, "token": "made-up"},
        )

        assert refused.status_code >= 400
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 200

    def test_stats_backup_and_vacuum_all_report_an_outcome(self, client: TestClient) -> None:
        _completed_run(client)

        stats = client.get("/api/v1/database/stats").json()
        backup = client.post("/api/v1/database/backup").json()
        vacuum = client.post("/api/v1/database/vacuum").json()

        assert stats["integrity_ok"] is True
        assert stats["table_row_counts"]["runs"] == 1
        assert backup["size_bytes"] > 0
        assert vacuum["size_after_bytes"] > 0


class TestBackupAndDeletionScope:
    def test_a_large_deletion_takes_a_backup_first(
        self, client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Database standards §8: automatic backup above the row threshold.

        The threshold is lowered here rather than a thousand rows being generated: the behaviour
        under test is "the threshold is honoured", and a test that spent a minute manufacturing
        rows to reach the shipped number would be testing the run engine instead.
        """
        import freeweight.services.database_admin as admin

        run_id = _completed_run(client)
        monkeypatch.setattr(admin, "AUTO_BACKUP_ROW_THRESHOLD", 1)
        with _database(workspace) as database:
            selection = DeletionSelection(scope=DeletionScope.RUN, selector=run_id)
            preview = preview_deletion(database, selection)
            assert preview.will_backup is True

            outcome = delete_results(database, selection, token=preview.token)

        assert outcome.backup_path is not None
        assert outcome.backup_path.exists()

    def test_a_small_deletion_does_not(self, client: TestClient, workspace: Path) -> None:
        run_id = _completed_run(client)
        with _database(workspace) as database:
            selection = DeletionSelection(scope=DeletionScope.RUN, selector=run_id)
            preview = preview_deletion(database, selection)
            assert preview.will_backup is False

            outcome = delete_results(database, selection, token=preview.token)

        assert outcome.backup_path is None

    def test_deleting_a_models_results_is_previewed_like_any_other_deletion(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The deletion that replaces time-based retention.

        A measurement does not expire — the thing that invalidates one is the model leaving the
        machine, or the hardware changing, and neither is something a clock can detect. So
        ``scope=model`` is the shape a user actually needs, and it previews and confirms like
        every other destructive operation.
        """
        _completed_run(client)
        with _database(workspace) as database:
            preview = preview_deletion(
                database,
                DeletionSelection(scope=DeletionScope.MODEL, selector="fake-model:8b-q8_0"),
            )

        assert preview.run_ids, "the model's own runs were not selected"
        assert preview.total_rows > 0

    def test_there_is_no_time_based_retention_setting(self) -> None:
        """A setting that silently deleted measurements on a timer is not offered at all.

        Asserted rather than assumed: it existed once, applied to nothing, and read as a promise
        about disk usage the application was not keeping.
        """
        from freeweight.config import StorageSettings
        from freeweight.services.settings import RUNTIME_SETTINGS

        assert "retention_days" not in StorageSettings.model_fields
        assert not [item for item in RUNTIME_SETTINGS if item.key.endswith("retention_days")]

    def test_stats_report_the_backup_that_was_just_taken(
        self, client: TestClient, workspace: Path
    ) -> None:
        _completed_run(client)
        client.post("/api/v1/database/backup")

        with _database(workspace) as database:
            stats = database_stats(database)

        assert stats.backup_count >= 1
        assert stats.last_backup_at is not None


class TestNothingMatches:
    def test_a_selection_that_matches_nothing_previews_as_nothing(
        self, client: TestClient, workspace: Path
    ) -> None:
        with _database(workspace) as database:
            preview = preview_deletion(
                database, DeletionSelection(scope=DeletionScope.SUITE, selector="native.nothing")
            )

            assert preview.run_ids == ()
            assert preview.total_rows == 0
            assert "nothing would be deleted" in preview.summary_line()

            outcome = delete_results(
                database,
                DeletionSelection(scope=DeletionScope.SUITE, selector="native.nothing"),
                token=preview.token,
            )
        assert outcome.deleted_counts == {}

    def test_scope_all_refuses_a_selector(self) -> None:
        from baseaicore import ValidationError

        with pytest.raises(ValidationError, match="takes no selector"):
            DeletionSelection(scope=DeletionScope.ALL, selector="something")
