"""End-to-end: grading survives everything that can interrupt a twenty-minute sitting.

Grading twelve samples across five criteria is a real sitting, and Subjective Goals §7 step 5 is
explicit that it "must survive being interrupted". Three interruptions are tested here because
they fail in three different ways:

* **A browser refresh** loses whatever the page was holding. So the page holds nothing: every
  grade is a form POST that is stored and redirected away from, and the screen is rendered from
  the database each time.
* **A server restart** loses whatever the process was holding. So the process holds nothing
  either: grades are ``calibration_grades`` rows, written through the same service the CLI uses.
* **An out-of-order submission** — the user goes back and regrades sample three after sample
  seven — must replace one row rather than appending a second. Otherwise the agreement figure is
  computed over grades the user has already superseded, which is worse than losing them.

The fourth thing tested is the one that makes the screen honest rather than merely durable: the
model that produced a sample is never fetched, so no template change can leak it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from weightsdb import MigrationRunner, create_engine_for

from freeweight.config import load_settings
from freeweight.services.database import MIGRATIONS_LOCATION, Database
from freeweight.web.app import create_app

_GOAL_SLUG = "sitting"


def _goal_body() -> dict[str, Any]:
    return {
        "slug": _GOAL_SLUG,
        "name": "A real sitting",
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "intent": "Twelve samples across two judged criteria.",
        "created_by": "tester",
        "criteria": [
            {
                "key": "wit",
                "name": "Dry wit",
                "rung": "judge",
                "weight": 0.5,
                "scale": {
                    "points": 5,
                    "descriptors": {
                        "5": "Wry and understated.",
                        "3": "Occasional flashes.",
                        "1": "Earnest throughout.",
                    },
                },
            },
            {
                "key": "concrete",
                "name": "Concrete over abstract",
                "rung": "judge",
                "weight": 0.5,
                "scale": {
                    "points": 5,
                    "descriptors": {
                        "5": "Anchored to specifics.",
                        "3": "Mixed.",
                        "1": "Abstraction throughout.",
                    },
                },
            },
        ],
        "judge": {"jury_size": 2, "repetitions": 1},
        "calibration": {"min_samples": 8, "target_samples": 12, "holdout_fraction": 0.4},
    }


def _task_record() -> dict[str, Any]:
    return {
        "prompt_id": "goals.sitting.one",
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": "One task.",
        "task": "goal.sitting",
        "capability": "creative_writing",
        "system": None,
        "template": "Write three paragraphs about a warehouse at night.",
        "variables": {},
        "response": {"format": "text", "json_schema_ref": None, "expectations": []},
        "model_requirements": {
            "min_context_tokens": 2048,
            "requires_capabilities": [],
            "recommended_temperature": 0.8,
        },
        "metadata": {
            "author": "tester",
            "created_at": "2026-08-28T00:00:00Z",
            "changed_at": "2026-08-28T00:00:00Z",
            "change_reason": "First version.",
            "supersedes": None,
            "tags": ["goal"],
            "goal_task": {"key": "one", "name": "Warehouse night"},
        },
    }


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A migrated database and one goal with twelve ungraded calibration samples."""
    from freeweight.services.calibration import add_samples
    from freeweight.services.goals import sync_goals, write_pack

    database_path = tmp_path / "freeweight.sqlite3"
    goals_root = tmp_path / "goals"
    goals_root.mkdir()
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    monkeypatch.setenv("FREEWEIGHT_GOALS__ROOT", str(goals_root))
    engine = create_engine_for(f"sqlite:///{database_path}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    goal = write_pack(goals_root, goal=_goal_body(), tasks=[_task_record()])
    with Database.from_url(f"sqlite:///{database_path}") as database:
        sync_goals(database, [goal])
        add_samples(
            database,
            goal,
            contents=[
                {"content": f"Candidate essay {index}, about a warehouse."} for index in range(12)
            ],
        )
    return tmp_path


def _client(workspace: Path) -> TestClient:
    """A fresh served application over ``workspace`` — a new process, as far as state goes."""
    loaded = load_settings(config_path=workspace / "missing.toml")
    return TestClient(create_app(loaded.settings), base_url="http://127.0.0.1")


@pytest.fixture
def client(workspace: Path) -> Iterator[TestClient]:
    with _client(workspace) as test_client:
        yield test_client


def _sample_ids(client: TestClient) -> list[str]:
    state = client.get(f"/api/v1/goals/{_GOAL_SLUG}/calibration").json()
    return [item["id"] for item in state["items"]]


def _progress(client: TestClient) -> dict[str, Any]:
    return dict(client.get(f"/api/v1/goals/{_GOAL_SLUG}/calibration").json()["progress"])


def _grade(client: TestClient, sample_id: str, criterion: str, grade: int, note: str) -> Any:
    return client.post(
        f"/goals/{_GOAL_SLUG}/grade",
        data={
            "sample_id": sample_id,
            "criterion": criterion,
            "grade": str(grade),
            "note": note,
        },
    )


class TestABrowserRefresh:
    def test_a_grade_survives_reloading_the_page(self, client: TestClient) -> None:
        sample_id = _sample_ids(client)[0]

        _grade(client, sample_id, "wit", 4, "understated")
        reloaded = client.get(f"/goals/{_GOAL_SLUG}/grade")

        assert reloaded.status_code == 200
        assert "graded 4" in reloaded.text
        assert "understated" in reloaded.text
        assert _progress(client)["recorded_grades"] == 1

    def test_the_page_holds_nothing_the_database_does_not(self, client: TestClient) -> None:
        """Every grade is a POST that redirects, so a refresh has nothing to re-submit."""
        sample_id = _sample_ids(client)[0]

        posted = _grade(client, sample_id, "wit", 3, "flat")

        # The client followed a redirect rather than rendering the POST's own response.
        assert posted.status_code == 200
        assert posted.history, "the grade POST did not redirect"
        assert posted.history[0].status_code == 303  # noqa: PLR2004 — See Other

    def test_progress_is_reported_so_a_sitting_can_be_resumed(self, client: TestClient) -> None:
        ids = _sample_ids(client)
        for sample_id in ids[:5]:
            _grade(client, sample_id, "wit", 3, "")

        page = client.get(f"/goals/{_GOAL_SLUG}/grade")

        assert "5 / 24" in page.text, "the page does not say how much is left"
        assert _progress(client)["complete"] is False


class TestAServerRestart:
    def test_grades_survive_a_restart_mid_sitting(self, workspace: Path) -> None:
        with _client(workspace) as first:
            ids = _sample_ids(first)
            for sample_id in ids[:6]:
                _grade(first, sample_id, "wit", 5, "before the restart")

        # A genuinely new application object over the same database, exactly as a restart is.
        with _client(workspace) as second:
            progress = _progress(second)
            page = second.get(f"/goals/{_GOAL_SLUG}/grade")

            assert progress["recorded_grades"] == 6  # noqa: PLR2004
            assert "before the restart" in page.text

            for sample_id in ids[6:]:
                _grade(second, sample_id, "wit", 2, "after the restart")
            assert _progress(second)["recorded_grades"] == 12  # noqa: PLR2004

    def test_the_calibration_can_be_completed_across_two_processes(self, workspace: Path) -> None:
        with _client(workspace) as first:
            ids = _sample_ids(first)
            for sample_id in ids:
                _grade(first, sample_id, "wit", (ids.index(sample_id) % 5) + 1, "wit note")

        with _client(workspace) as second:
            for index, sample_id in enumerate(ids):
                _grade(second, sample_id, "concrete", (index % 5) + 1, "concrete note")

            progress = _progress(second)
            assert progress["complete"] is True
            assert progress["recorded_grades"] == 24  # noqa: PLR2004


class TestOutOfOrderSubmission:
    def test_regrading_replaces_rather_than_appends(self, client: TestClient) -> None:
        ids = _sample_ids(client)

        _grade(client, ids[2], "wit", 2, "first thoughts")
        _grade(client, ids[6], "wit", 5, "much better")
        # Back to sample three, after seven.
        _grade(client, ids[2], "wit", 4, "changed my mind")

        assert _progress(client)["recorded_grades"] == 2  # noqa: PLR2004 — two samples, not three
        page = client.get(f"/goals/{_GOAL_SLUG}/grade")
        assert "changed my mind" in page.text
        assert "first thoughts" not in page.text

    def test_the_stored_grade_is_the_last_one_submitted(
        self, client: TestClient, workspace: Path
    ) -> None:
        ids = _sample_ids(client)
        _grade(client, ids[0], "wit", 1, "harsh")
        _grade(client, ids[0], "wit", 5, "generous")

        from sqlalchemy import select

        from freeweight.infrastructure.db.models_goals import CalibrationGrade

        with Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database:
            with database.read() as session:
                rows = list(
                    session.scalars(
                        select(CalibrationGrade).where(
                            CalibrationGrade.calibration_sample_id == ids[0]
                        )
                    )
                )

        assert len(rows) == 1, "a regrade appended a second row"
        assert rows[0].grade == 5  # noqa: PLR2004
        assert rows[0].note == "generous"

    def test_a_rejected_grade_lands_nothing_and_says_nothing_false(
        self, client: TestClient
    ) -> None:
        ids = _sample_ids(client)

        _grade(client, ids[0], "wit", 99, "off the scale")

        assert _progress(client)["recorded_grades"] == 0
        page = client.get(f"/goals/{_GOAL_SLUG}/grade")
        assert "off the scale" not in page.text


class TestTheGradingScreenIsBlinded:
    def test_the_model_that_produced_a_sample_is_never_shown(self, client: TestClient) -> None:
        page = client.get(f"/goals/{_GOAL_SLUG}/grade")

        assert page.status_code == 200
        assert "Blinded and shuffled" in page.text
        for marker in ("model_id", "canonical_id", "fake-model"):
            assert marker not in page.text, marker

    def test_the_order_is_stable_across_reloads(self, client: TestClient) -> None:
        """A shuffle that changed under the user would make "the third one" meaningless."""
        first = client.get(f"/goals/{_GOAL_SLUG}/grade").text
        second = client.get(f"/goals/{_GOAL_SLUG}/grade").text

        def order(page: str) -> list[str]:
            return [
                fragment.split('"', 1)[0] for fragment in page.split('name="sample_id" value="')[1:]
            ]

        assert order(first) == order(second)

    def test_the_order_is_not_the_order_they_were_stored_in(self, client: TestClient) -> None:
        stored = _sample_ids(client)
        page = client.get(f"/goals/{_GOAL_SLUG}/grade").text
        shown = []
        for fragment in page.split('name="sample_id" value="')[1:]:
            candidate = fragment.split('"', 1)[0]
            if candidate not in shown:
                shown.append(candidate)

        assert sorted(shown) == sorted(stored)
        assert shown != stored, "the samples are presented in the order they were generated in"

    def test_the_scale_descriptors_are_on_the_screen_being_graded(self, client: TestClient) -> None:
        """A grader who has to remember what 4 means is a grader producing noise."""
        page = client.get(f"/goals/{_GOAL_SLUG}/grade")

        assert "what the points mean" in page.text
        assert "Wry and understated." in page.text
        assert "Abstraction throughout." in page.text


class TestTheApiAgrees:
    def test_the_ui_and_the_api_record_the_same_grades(self, client: TestClient) -> None:
        """CLI standards §3's sibling rule: two surfaces, one stored fact."""
        ids = _sample_ids(client)
        _grade(client, ids[0], "wit", 4, "from the ui")

        posted = client.post(
            f"/api/v1/goals/{_GOAL_SLUG}/calibration/grades",
            json={
                "grades": [
                    {
                        "sample_id": ids[1],
                        "criterion": "wit",
                        "grade": 2,
                        "note": "from the api",
                    }
                ]
            },
        )

        assert posted.status_code in (200, 201), posted.text
        progress = _progress(client)
        assert progress["recorded_grades"] == 2  # noqa: PLR2004
        page = client.get(f"/goals/{_GOAL_SLUG}/grade")
        assert "from the ui" in page.text
        assert "from the api" in page.text

    def test_the_progress_document_is_the_same_on_both(self, client: TestClient) -> None:
        ids = _sample_ids(client)
        _grade(client, ids[0], "wit", 4, "")

        from_api = client.get(f"/api/v1/goals/{_GOAL_SLUG}/calibration").json()["progress"]

        assert json.loads(json.dumps(from_api))["recorded_grades"] == 1
        assert from_api["expected_grades"] == 24  # noqa: PLR2004
