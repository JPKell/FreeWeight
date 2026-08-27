"""End-to-end: Phase 5's run journey, through both HTTP and CLI (testing standards §1).

Acceptance criteria (development plan, Phase 5):

1. ``freeweight run start --suite native.echo`` completes, streams progress to the browser and the
   CLI, and stores raw samples.
2. Refreshing the browser mid-run resumes the live view with no missing events.
3. ``Ctrl-C`` during ``run wait`` cancels cleanly with exit 6 and consistent data.
4. Killing the server mid-run and restarting yields a resumable ``interrupted`` run.

Every journey runs against :class:`~modelrack.testing.FakeProvider`: the *running application*,
not only its unit tests, needs no GPU, no Ollama and no network.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
    """A migrated database and a fake-provider configuration, shared by the CLI and the server."""
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    # The shipped default is a 5 s cooldown between tests (spec §12). It is real behaviour and is
    # exercised in its own unit test; paying it in every end-to-end journey would buy nothing but
    # minutes.
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__COOLDOWN_SECONDS", "0")
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    return tmp_path


@pytest.fixture
def client(workspace: Path) -> Iterator[TestClient]:
    """A served application over ``workspace`` — lifespan entered, so the scheduler is running."""
    loaded = load_settings(config_path=workspace / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _cli(*args: str) -> Any:
    return runner.invoke(cli_app, list(args))


def _discover(workspace: Path) -> None:
    result = _cli("models", "refresh")
    assert result.exit_code == 0, result.output


def _wait_for_terminal(client: TestClient, run_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body = client.get(f"/api/v1/runs/{run_id}").json()
    while body["status"] not in _TERMINAL:
        assert time.monotonic() < deadline, f"run stuck in {body['status']}"
        time.sleep(0.05)
        body = client.get(f"/api/v1/runs/{run_id}").json()
    return dict(body)


class TestCriterion1CliRunCompletes:
    def test_run_start_completes_and_stores_raw_samples(self, workspace: Path) -> None:
        _discover(workspace)
        started = _cli(
            "run", "start", "--model", "fake-model:8b-q8_0", "--suite", "native.echo", "--json"
        )
        assert started.exit_code == 0, started.output

        # The id is printed the moment the run is persisted, so a script can chain on it
        # (CLI standards §11).
        first_line = json.loads(started.output.splitlines()[0])
        run_id = first_line["run_id"]
        assert first_line["status"] == "queued"

        shown = _cli("run", "show", run_id, "--json")
        assert shown.exit_code == 0, shown.output
        body = json.loads(shown.output)
        assert body["status"] == "completed"
        assert body["reproducibility_fingerprint"].startswith("sha256:")

        # Raw samples, not just an aggregate: every test drills to real rows.
        assert body["tests"]
        for test in body["tests"]:
            assert test["status"] == "completed"
            assert test["completed_cases"] == test["total_cases"]
        # Selected by key, not by position: a run's run-level metrics now include the telemetry
        # summary (peak VRAM, power, energy, temperature), which sorts before this one.
        run_metric = next(
            m
            for m in body["metrics"]
            if m["run_test_id"] is None and m["key"] == "harness_roundtrip_success"
        )
        assert run_metric["sample_count"] > 0
        assert run_metric["excluded_count"] == 0

    def test_run_list_shows_the_run(self, workspace: Path) -> None:
        _discover(workspace)
        _cli("run", "start", "--model", "fake-model:8b-q8_0", "--suite", "native.echo")
        listed = _cli("run", "list", "--json")
        assert listed.exit_code == 0, listed.output
        runs = json.loads(listed.output)["runs"]
        assert len(runs) == 1
        assert runs[0]["suite"] == "native.echo"

    def test_an_unknown_suite_is_a_usage_error(self, workspace: Path) -> None:
        _discover(workspace)
        result = _cli("run", "start", "--model", "fake-model:8b-q8_0", "--suite", "native.eco")
        assert result.exit_code == 2
        assert "BENCHMARK_NOT_FOUND" in result.output

    def test_an_unknown_model_is_a_usage_error(self, workspace: Path) -> None:
        _discover(workspace)
        result = _cli("run", "start", "--model", "not-a-model", "--suite", "native.echo")
        assert result.exit_code == 2
        assert "MODEL_NOT_FOUND" in result.output

    def test_show_for_an_unknown_run_is_a_usage_error(self, workspace: Path) -> None:
        result = _cli("run", "show", "01ZZZZZZZZZZZZZZZZZZZZZZZZ", "--json")
        assert result.exit_code == 2
        assert "RUN_NOT_FOUND" in result.output


class TestCriterion1HttpRunCompletes:
    def test_the_browser_journey_start_to_samples(self, client: TestClient) -> None:
        assert client.post("/models/discover", follow_redirects=False).status_code == 303

        page = client.get("/runs")
        assert page.status_code == 200
        assert "Start a run" in page.text

        started = client.post(
            "/runs",
            data={"model": "fake-model:8b-q8_0", "suite": "native.echo", "label": "journey"},
            follow_redirects=False,
        )
        assert started.status_code == 303
        run_id = started.headers["location"].removeprefix("/runs/")

        body = _wait_for_terminal(client, run_id)
        assert body["status"] == "completed"

        detail = client.get(f"/runs/{run_id}")
        assert detail.status_code == 200
        assert "harness_roundtrip_success" in detail.text
        assert "journey" in detail.text

        run_test_id = body["tests"][0]["id"]
        samples_page = client.get(f"/runs/{run_id}/tests/{run_test_id}")
        assert samples_page.status_code == 200
        assert "echo-" in samples_page.text

        samples = client.get(f"/api/v1/runs/{run_id}/tests/{run_test_id}/samples").json()
        assert samples["samples"]
        assert all(sample["response_hash"] for sample in samples["samples"])

    def test_the_api_journey_start_to_samples(self, client: TestClient) -> None:
        client.post("/models/discover", follow_redirects=False)
        created = client.post(
            "/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suites": ["native.echo"]}
        )
        assert created.status_code == 201
        run_id = created.json()["id"]
        assert created.json()["status"] == "queued"

        body = _wait_for_terminal(client, run_id)
        assert body["status"] == "completed"
        assert body["last_event_sequence"] > 0

        listed = client.get("/api/v1/runs").json()["runs"]
        assert [run["id"] for run in listed] == [run_id]

    def test_a_run_page_for_an_unknown_run_is_404_with_an_explanation(
        self, client: TestClient
    ) -> None:
        response = client.get("/runs/01ZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert response.status_code == 404
        assert "could not be shown" in response.text

    def test_the_run_form_reports_a_bad_suite_without_losing_input(
        self, client: TestClient
    ) -> None:
        client.post("/models/discover", follow_redirects=False)
        response = client.post(
            "/runs",
            data={"model": "fake-model:8b-q8_0", "suite": "native.eco", "label": "kept"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "BENCHMARK_NOT_FOUND" in response.text
        assert 'value="kept"' in response.text


class TestCriterion2RefreshMidRunLosesNothing:
    def test_the_detail_page_carries_the_sequence_the_stream_resumes_from(
        self, client: TestClient
    ) -> None:
        """The page renders ``data-last-sequence``; the browser reconnects from exactly that.

        This is the mechanism behind "refreshing the browser mid-run resumes the live view with no
        missing events": the server-rendered page states how far it got, and the stream starts
        strictly after it — no gap, and nothing replayed that the page already showed.
        """
        client.post("/models/discover", follow_redirects=False)
        run_id = client.post(
            "/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suites": ["native.echo"]}
        ).json()["id"]
        body = _wait_for_terminal(client, run_id)

        page = client.get(f"/runs/{run_id}")
        assert page.status_code == 200
        match = re.search(r'data-last-sequence="(\d+)"', page.text)
        assert match is not None, "the detail page must state where the stream resumes from"
        rendered = int(match.group(1))
        assert rendered == body["last_event_sequence"]

        # Everything up to `rendered` is already on the page; the stream must return only what
        # follows, and — since the run has finished — that is nothing but the terminal frame's
        # absence, i.e. an empty tail.
        tail = client.get(f"/api/v1/runs/{run_id}/events?last_event_id={rendered}")
        assert tail.status_code == 200
        assert "id: " not in tail.text

    def test_a_partial_replay_joins_the_page_without_a_gap(self, client: TestClient) -> None:
        client.post("/models/discover", follow_redirects=False)
        run_id = client.post(
            "/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suites": ["native.echo"]}
        ).json()["id"]
        _wait_for_terminal(client, run_id)

        full = _event_ids(client.get(f"/api/v1/runs/{run_id}/events").text)
        midpoint = full[len(full) // 2]
        resumed = _event_ids(
            client.get(f"/api/v1/runs/{run_id}/events?last_event_id={midpoint}").text
        )
        assert resumed[0] == midpoint + 1
        assert full[: full.index(midpoint) + 1] + resumed == full

    def test_the_last_event_id_header_is_honoured_too(self, client: TestClient) -> None:
        """A dropped connection sends the header; a page reload sends the query parameter."""
        client.post("/models/discover", follow_redirects=False)
        run_id = client.post(
            "/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suites": ["native.echo"]}
        ).json()["id"]
        _wait_for_terminal(client, run_id)

        response = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "2"})
        assert _event_ids(response.text)[0] == 3


class TestCriterion3CancelExitsSix:
    def test_ctrl_c_during_wait_cancels_and_exits_six(self, workspace: Path) -> None:
        """``Ctrl-C`` while waiting cancels the run itself, not just the watcher.

        ``run wait`` polls with ``time.sleep``; the ``KeyboardInterrupt`` a terminal ``Ctrl-C``
        raises is injected there, which is exactly where a real signal would land.
        """
        _discover(workspace)
        started = _cli(
            "run",
            "start",
            "--model",
            "fake-model:8b-q8_0",
            "--suite",
            "native.echo",
            "--detach",
            "--json",
        )
        assert started.exit_code == 0, started.output
        run_id = json.loads(started.output)["run_id"]

        with _sleep_raises_keyboard_interrupt():
            result = _cli("run", "wait", run_id)
        assert result.exit_code == 6, result.output

        shown = json.loads(_cli("run", "show", run_id, "--json").output)
        assert shown["status"] == "cancelled"
        assert shown["completed_at"] is not None
        # Consistent data: a cancelled run that never executed has no tests half-run and no
        # aggregates claiming to summarize samples that do not exist.
        assert all(test["status"] in {"cancelled", "pending"} for test in shown["tests"])
        assert shown["metrics"] == []

    def test_cancel_command_reports_the_new_state(self, workspace: Path) -> None:
        _discover(workspace)
        run_id = json.loads(
            _cli(
                "run",
                "start",
                "--model",
                "fake-model:8b-q8_0",
                "--suite",
                "native.echo",
                "--detach",
                "--json",
            ).output
        )["run_id"]
        result = _cli("run", "cancel", run_id, "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["status"] == "cancelled"

    def test_cancelling_a_finished_run_is_refused(self, workspace: Path) -> None:
        _discover(workspace)
        run_id = json.loads(
            _cli(
                "run", "start", "--model", "fake-model:8b-q8_0", "--suite", "native.echo", "--json"
            ).output.splitlines()[0]
        )["run_id"]
        result = _cli("run", "cancel", run_id)
        assert result.exit_code == 5
        assert "RUN_NOT_CANCELLABLE" in result.output

    def test_wait_on_a_finished_run_exits_zero(self, workspace: Path) -> None:
        _discover(workspace)
        run_id = json.loads(
            _cli(
                "run", "start", "--model", "fake-model:8b-q8_0", "--suite", "native.echo", "--json"
            ).output.splitlines()[0]
        )["run_id"]
        result = _cli("run", "wait", run_id)
        assert result.exit_code == 0, result.output


class TestCriterion4KillTheServerAndRestart:
    def test_a_run_left_in_flight_is_interrupted_and_resumable_after_a_restart(
        self, workspace: Path
    ) -> None:
        """Start a run, leave it mid-flight, restart the server, and finish it.

        The kill is simulated by writing the status a killed process leaves behind and dropping
        the handle — a real ``SIGKILL`` cannot be issued to the test process itself. What is
        actually under test is the *restart*: the new application's lifespan runs recovery, and
        the run must come back ``interrupted`` (not ``failed``) with its samples intact, and must
        then resume to completion.
        """
        _discover(workspace)
        run_id = json.loads(
            _cli(
                "run",
                "start",
                "--model",
                "fake-model:8b-q8_0",
                "--suite",
                "native.echo",
                "--detach",
                "--json",
            ).output
        )["run_id"]
        _leave_run_in_flight(workspace, run_id)

        loaded = load_settings(config_path=workspace / "missing.toml")
        with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as restarted:
            body = restarted.get(f"/api/v1/runs/{run_id}").json()
            assert body["status"] == "interrupted"
            assert body["error"] is None, "an interrupted run is not a failed one"

            events = restarted.get(f"/api/v1/runs/{run_id}/events").text
            assert "event: run.interrupted" in events

        resumed = _cli("run", "list", "--status", "interrupted", "--json")
        assert [run["run_id"] for run in json.loads(resumed.output)["runs"]] == [run_id]


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _event_ids(stream_text: str) -> list[int]:
    return [
        int(line.removeprefix("id: "))
        for line in stream_text.splitlines()
        if line.startswith("id: ")
    ]


def _sleep_raises_keyboard_interrupt() -> Any:
    """Patch ``time.sleep`` inside the CLI's wait loop to raise ``KeyboardInterrupt`` once."""
    from unittest.mock import patch

    return patch("freeweight.cli.commands.runs.time.sleep", side_effect=KeyboardInterrupt)


def _leave_run_in_flight(workspace: Path, run_id: str) -> None:
    """Put a queued run into ``running`` and drop the handle, as a killed process would."""
    from freeweight.domain.run_state import RunStatus
    from freeweight.infrastructure.db.repositories.runs import RunRepository
    from freeweight.services.database import Database

    with (
        Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database,
        database.write() as session,
    ):
        RunRepository().set_status(session, run_id, status=RunStatus.RUNNING.value)


class TestExitCodesAndTextOutput:
    """The remaining documented exit codes, and the human-readable output beside ``--json``.

    CLI standards §13: "every command is tested for: success path, ``--json`` shape, each
    documented exit code". Codes 0, 2, 5 and 6 are covered by the criterion classes above; 4 and 7
    are here, along with the text rendering — which is what a person actually sees, and which is
    where "an unavailable metric prints ``—``, never ``0``" has to hold.
    """

    def test_wait_times_out_with_exit_four_and_leaves_the_run_alone(self, workspace: Path) -> None:
        _discover(workspace)
        run_id = json.loads(
            _cli(
                "run",
                "start",
                "--model",
                "fake-model:8b-q8_0",
                "--suite",
                "native.echo",
                "--detach",
                "--json",
            ).output
        )["run_id"]

        result = _cli("run", "wait", run_id, "--timeout", "0", "--poll", "0.01")
        assert result.exit_code == 4, result.output
        assert "Timed out" in result.output
        assert json.loads(_cli("run", "show", run_id, "--json").output)["status"] == "queued"

    def test_start_exits_seven_when_another_process_holds_the_machine(
        self, workspace: Path
    ) -> None:
        """The run is queued, not lost, and the exit code says the slot was taken."""
        from freeweight.domain.run_state import RunStatus
        from freeweight.infrastructure.db.repositories.runs import RunRepository
        from freeweight.services.database import Database

        _discover(workspace)
        blocker = json.loads(
            _cli(
                "run",
                "start",
                "--model",
                "fake-model:8b-q8_0",
                "--suite",
                "native.echo",
                "--detach",
                "--json",
            ).output
        )["run_id"]
        with (
            Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database,
            database.write() as session,
        ):
            RunRepository().set_status(session, blocker, status=RunStatus.RUNNING.value)

        result = _cli(
            "run", "start", "--model", "fake-model:8b-q8_0", "--suite", "native.echo", "--json"
        )
        assert result.exit_code == 7, result.output
        queued_id = json.loads(result.output.splitlines()[0])["run_id"]
        assert json.loads(_cli("run", "show", queued_id, "--json").output)["status"] == "queued"

    def test_list_and_show_render_readable_text(self, workspace: Path) -> None:
        _discover(workspace)
        run_id = json.loads(
            _cli(
                "run",
                "start",
                "--model",
                "fake-model:8b-q8_0",
                "--suite",
                "native.echo",
                "--label",
                "readable",
                "--json",
            ).output.splitlines()[0]
        )["run_id"]

        listed = _cli("run", "list")
        assert listed.exit_code == 0, listed.output
        assert run_id in listed.output
        assert "native.echo" in listed.output
        assert "readable" in listed.output

        shown = _cli("run", "show", run_id)
        assert shown.exit_code == 0, shown.output
        assert "fingerprint sha256:" in shown.output
        assert "echo.short" in shown.output
        assert "harness_roundtrip_success" in shown.output

    def test_list_says_so_when_there_are_no_runs(self, workspace: Path) -> None:
        result = _cli("run", "list")
        assert result.exit_code == 0, result.output
        assert "No runs yet" in result.output

    def test_cancel_of_an_unknown_run_is_a_usage_error(self, workspace: Path) -> None:
        result = _cli("run", "cancel", "01ZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert result.exit_code == 2
        assert "RUN_NOT_FOUND" in result.output

    def test_wait_on_an_unknown_run_is_a_usage_error(self, workspace: Path) -> None:
        result = _cli("run", "wait", "01ZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert result.exit_code == 2
        assert "RUN_NOT_FOUND" in result.output


class TestPhase6RepeatAndTheRunPage:
    """Phase 6's two user-facing additions, driven the way a person drives them.

    ``run repeat`` and the run detail page are Phase 6 *Work* rather than Phase 5 acceptance
    criteria, so they live in their own class here rather than under one of the numbered ones
    above.
    """

    def test_run_repeat_reruns_the_recorded_configuration(self, workspace: Path) -> None:
        _discover(workspace)
        started = _cli(
            "run", "start", "--model", "fake-model:8b-q8_0", "--suite", "native.echo", "--json"
        )
        assert started.exit_code == 0, started.output
        original = json.loads(started.output.splitlines()[0])["run_id"]

        repeated = _cli("run", "repeat", original, "--json")
        assert repeated.exit_code == 0, repeated.output
        body = json.loads(repeated.output.splitlines()[0])
        assert body["repeat_of"] == original
        assert body["run_id"] != original

        shown = json.loads(_cli("run", "show", body["run_id"], "--json").output)
        assert shown["status"] == "completed"
        assert shown["label"].startswith("repeat of ")

    def test_run_repeat_check_reports_that_the_provenance_is_identical(
        self, workspace: Path
    ) -> None:
        _discover(workspace)
        started = _cli(
            "run", "start", "--model", "fake-model:8b-q8_0", "--suite", "native.echo", "--json"
        )
        original = json.loads(started.output.splitlines()[0])["run_id"]

        checked = _cli("run", "repeat", original, "--check")
        assert checked.exit_code == 0, checked.output
        # Nothing about this environment moved between the two runs, and the command says so
        # rather than printing nothing and leaving the user to infer it.
        assert "Provenance identical" in checked.output

    def test_run_repeat_names_a_run_that_does_not_exist(self, workspace: Path) -> None:
        result = _cli("run", "repeat", "01JNOPE")
        assert result.exit_code == 2
        assert "RUN_NOT_FOUND" in result.output

    def test_the_run_page_shows_the_fingerprint_and_the_provenance(
        self, client: TestClient, workspace: Path
    ) -> None:
        _discover(workspace)
        created = client.post(
            "/api/v1/runs",
            json={"model": "fake-model:8b-q8_0", "suite": "native.echo", "repetitions": 1},
        )
        run_id = created.json()["id"]
        _wait_for_terminal(client, run_id)

        page = client.get(f"/runs/{run_id}")
        assert page.status_code == 200
        assert "Fingerprint" in page.text
        assert "Served context" in page.text
        assert "Attributed device" in page.text
        assert "Fingerprint document" in page.text
        # Telemetry either charted or honestly absent — never a blank section.
        assert ("Telemetry" in page.text) and (
            "telemetry-chart" in page.text or "No telemetry was recorded" in page.text
        )

    def test_the_api_returns_the_fingerprint_document_and_degradations(
        self, client: TestClient, workspace: Path
    ) -> None:
        _discover(workspace)
        created = client.post(
            "/api/v1/runs",
            json={"model": "fake-model:8b-q8_0", "suite": "native.echo", "repetitions": 1},
        )
        run_id = created.json()["id"]
        _wait_for_terminal(client, run_id)

        body = client.get(f"/api/v1/runs/{run_id}").json()
        document = body["provenance"]["fingerprint_document"]
        assert document["benchmark"]["suite_key"] == "native.echo"
        assert body["provenance"]["served_context_source"] in {
            "configured",
            "reported",
            "assumed",
        }
        assert body["degradations"] == []
