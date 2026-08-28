"""The goal and calibration HTTP surface, end to end through the real application.

Testing Standards §5 requires of every HTTP route: success, validation-error shape, not-found,
size limit, the error envelope and request-ID propagation. This file covers the Phase 8A/8B
endpoints against a real app with a real database, so the shapes the specification names are
asserted where a client would actually meet them.

The three shapes api.md decides, restated as tests:

* a **lint finding never blocks creation** — it comes back with the goal;
* ``PUT`` **says what it would separate before it commits**;
* ``DELETE`` **previews first**, and the preview names the grades it would destroy.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from freeweight.config import load_settings
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.services.goals import bundle_hash
from freeweight.web.app import create_app


def _goal_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": "house_voice",
        "name": "House voice",
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "intent": "Sounds like us.",
        "created_by": "tester",
        "criteria": [
            {
                "key": "tells",
                "name": "No LLM tells",
                "rung": "rule",
                "weight": 1.0,
                "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
            }
        ],
    }
    body.update(changes)
    return body


def _task_record(prompt_id: str = "goals.house_voice.t1") -> dict[str, Any]:
    return {
        "prompt_id": prompt_id,
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": "One task from the author's own work.",
        "task": "goal.house_voice",
        "capability": "creative_writing",
        "system": None,
        "template": "Write a short release note about a database migration.",
        "variables": {},
        "response": {"format": "text", "json_schema_ref": None, "expectations": []},
        "model_requirements": {
            "min_context_tokens": 2048,
            "requires_capabilities": [],
            "recommended_temperature": 0.7,
        },
        "metadata": {
            "author": "tester",
            "created_at": "2026-08-27T00:00:00Z",
            "changed_at": "2026-08-27T00:00:00Z",
            "change_reason": "First version.",
            "supersedes": None,
            "tags": ["goal"],
            "goal_task": {"key": "t1", "name": "Task one"},
        },
    }


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client against an app with a migrated database and an empty goal root."""
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    monkeypatch.setenv("FREEWEIGHT_GOALS__ROOT", str(tmp_path / "goals"))
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


def _create(client: TestClient, **changes: Any) -> dict[str, Any]:
    response = client.post(
        "/api/v1/goals", json={"goal": _goal_body(**changes), "tasks": [_task_record()]}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


class TestCreateAndRead:
    def test_creating_a_goal_returns_it_with_its_hash(self, client: TestClient) -> None:
        body = _create(client)
        assert body["slug"] == "house_voice"
        assert body["capability_id"] == "user.house_voice"
        assert body["goal_hash"].startswith("sha256:")
        assert body["score_method_mix"]["rule"] == 1.0

    def test_a_lint_finding_never_blocks_creation(self, client: TestClient) -> None:
        # The deterministic-share note is informational and comes back with the goal.
        body = _create(client)
        assert any(finding["code"] == "DETERMINISTIC_WEIGHT_SHARE" for finding in body["findings"])

    def test_a_pack_that_could_not_run_is_refused_with_its_own_code(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/api/v1/goals",
            json={"goal": _goal_body(criteria=[]), "tasks": [_task_record()]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "GOAL_INVALID"

    def test_the_listing_is_a_collection_envelope(self, client: TestClient) -> None:
        _create(client)
        body = client.get("/api/v1/goals").json()
        assert set(body) >= {"items", "page", "total"}
        assert body["total"] == 1
        assert body["page"]["has_more"] is False

    def test_an_empty_installation_lists_nothing(self, client: TestClient) -> None:
        body = client.get("/api/v1/goals").json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_one_goal_reads_back(self, client: TestClient) -> None:
        _create(client)
        body = client.get("/api/v1/goals/house_voice").json()
        assert body["slug"] == "house_voice"
        assert body["criteria"][0]["key"] == "tells"
        assert body["tasks"][0]["prompt_id"] == "goals.house_voice.t1"

    def test_an_unknown_goal_is_a_404_in_the_error_envelope(self, client: TestClient) -> None:
        response = client.get("/api/v1/goals/nothing_here")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "GOAL_NOT_FOUND"
        assert error["message"]
        assert error["request_id"]

    def test_the_request_id_is_propagated(self, client: TestClient) -> None:
        response = client.get("/api/v1/goals/nothing_here", headers={"X-Request-ID": "abc-123"})
        assert response.json()["error"]["request_id"] == "abc-123"

    def test_an_unknown_field_in_the_body_is_refused(self, client: TestClient) -> None:
        # extra="forbid": a typo surfaces immediately rather than being silently ignored.
        response = client.post(
            "/api/v1/goals",
            json={"goal": _goal_body(), "tasks": [], "extra": True},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_a_colliding_slug_is_a_conflict(self, client: TestClient) -> None:
        _create(client)
        response = client.post(
            "/api/v1/goals", json={"goal": _goal_body(), "tasks": [_task_record()]}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"


class TestValidateAndSuggest:
    def test_validate_returns_every_finding_with_a_severity(self, client: TestClient) -> None:
        _create(client)
        body = client.post("/api/v1/goals/house_voice/validate").json()
        assert body["valid"] is True
        assert all(
            finding["severity"] in {"error", "warning", "info"} for finding in body["findings"]
        )

    def test_suggest_rules_proposes_and_never_applies(self, client: TestClient) -> None:
        _create(
            client,
            criteria=[
                {
                    "key": "not_linkedin",
                    "name": "No corporate hedging",
                    "rung": "judge",
                    "weight": 1.0,
                    "scale": {
                        "points": 5,
                        "descriptors": {"5": "Plain.", "3": "Mixed.", "1": "Hedged."},
                    },
                }
            ],
            judge={"jury_size": 3},
        )
        body = client.post("/api/v1/goals/house_voice/suggest-rules").json()
        assert "forbidden_phrases" in body["proposals"]["not_linkedin"]
        # And the goal is unchanged: proposals are proposals.
        stored = client.get("/api/v1/goals/house_voice").json()
        assert stored["criteria"][0]["rung"] == "judge"

    def test_the_task_list_flags_starter_content(self, client: TestClient) -> None:
        _create(client)
        body = client.get("/api/v1/goals/house_voice/tasks").json()
        assert body["total"] == 1
        assert body["items"][0]["is_starter"] is False


class TestReplaceSaysWhatItSeparates:
    def test_a_rename_separates_nothing(self, client: TestClient) -> None:
        _create(client)
        renamed = _goal_body()
        renamed["criteria"][0]["name"] = "No giveaway phrases"
        body = client.put(
            "/api/v1/goals/house_voice",
            json={"goal": renamed, "tasks": [_task_record()]},
        ).json()
        assert body["hash_change"]["separates"] is False
        assert body["hash_change"]["separated_runs"] == 0

    def test_changing_the_phrase_list_separates(self, client: TestClient) -> None:
        created = _create(client)
        changed = _goal_body()
        changed["criteria"][0]["rule"]["phrases"].append("leverage")
        body = client.put(
            "/api/v1/goals/house_voice", json={"goal": changed, "tasks": [_task_record()]}
        ).json()
        assert body["hash_change"]["separates"] is True
        assert body["hash_change"]["previous_goal_hash"] == created["goal_hash"]
        assert "criteria" in body["hash_change"]["changed_fields"]

    def test_a_dry_run_reports_the_separation_without_applying_it(self, client: TestClient) -> None:
        # Acceptance criterion 4: the user is told what an edit would separate while they can
        # still decide not to make it.
        created = _create(client)
        changed = _goal_body()
        changed["criteria"][0]["rule"]["phrases"].append("leverage")
        body = client.put(
            "/api/v1/goals/house_voice?dry_run=true",
            json={"goal": changed, "tasks": [_task_record()]},
        ).json()
        assert body["dry_run"] is True
        assert body["hash_change"]["separates"] is True
        # And the stored goal is untouched.
        assert client.get("/api/v1/goals/house_voice").json()["goal_hash"] == created["goal_hash"]

    def test_a_rename_of_the_goal_itself_is_refused(self, client: TestClient) -> None:
        _create(client)
        response = client.put(
            "/api/v1/goals/house_voice",
            json={"goal": _goal_body(slug="other_voice"), "tasks": [_task_record()]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "GOAL_INVALID"


class TestDeletePreviewsFirst:
    def test_a_bare_delete_previews(self, client: TestClient) -> None:
        _create(client)
        body = client.delete("/api/v1/goals/house_voice").json()
        assert body["dry_run"] is True
        assert body["orphaned_runs"] == 0
        assert body["destroyed_grades"] == 0
        # And nothing was removed.
        assert client.get("/api/v1/goals/house_voice").status_code == 200

    def test_dry_run_false_performs_it(self, client: TestClient) -> None:
        _create(client)
        body = client.delete("/api/v1/goals/house_voice?dry_run=false").json()
        assert body["dry_run"] is False
        assert client.get("/api/v1/goals/house_voice").status_code == 404

    def test_deleting_an_unknown_goal_is_a_404(self, client: TestClient) -> None:
        assert client.delete("/api/v1/goals/nothing_here").status_code == 404


class TestExportAndImport:
    def test_export_returns_a_setspec_envelope(self, client: TestClient) -> None:
        _create(client)
        body = client.get("/api/v1/goals/house_voice/export").json()
        assert body["schema"] == "benchmark.goal_pack"
        assert body["payload"]["slug"] == "house_voice"
        assert body["payload"]["criteria"][0]["rule_type"] == "forbidden_phrases"
        assert body["generator"]["name"] == "freeweight"

    def test_import_accepts_a_bundle(self, client: TestClient) -> None:
        files = {
            "goal.json": json.dumps(_goal_body(slug="their_voice")),
            "tasks/001.json": json.dumps(_task_record("goals.their_voice.t1")),
        }
        response = client.post(
            "/api/v1/goals/import",
            json={
                "bundle": {
                    "bundle_version": "1.0",
                    "slug": "their_voice",
                    "goal_hash": "sha256:" + "00" * 32,
                    "files": files,
                    "bundle_sha256": bundle_hash(files),
                }
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["slug"] == "their_voice"

    def test_a_traversing_member_is_refused_with_its_own_code(self, client: TestClient) -> None:
        files = {"goal.json": json.dumps(_goal_body()), "../escape.json": "{}"}
        response = client.post(
            "/api/v1/goals/import",
            json={
                "bundle": {
                    "files": files,
                    "slug": "house_voice",
                    "bundle_sha256": bundle_hash(files),
                }
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "GOAL_PATH_UNSAFE"

    def test_a_bad_hash_is_refused_with_its_own_code(self, client: TestClient) -> None:
        files = {
            "goal.json": json.dumps(_goal_body()),
            "tasks/001.json": json.dumps(_task_record()),
        }
        response = client.post(
            "/api/v1/goals/import",
            json={
                "bundle": {
                    "files": files,
                    "slug": "house_voice",
                    "bundle_sha256": "sha256:" + "ff" * 32,
                }
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "GOAL_HASH_MISMATCH"


class TestCalibrationEndpoints:
    def _judged(self, client: TestClient) -> dict[str, Any]:
        return _create(
            client,
            criteria=[
                {
                    "key": "tells",
                    "name": "No LLM tells",
                    "rung": "rule",
                    "weight": 0.5,
                    "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
                },
                {
                    "key": "wit",
                    "name": "Dry wit",
                    "rung": "judge",
                    "weight": 0.5,
                    "scale": {
                        "points": 5,
                        "descriptors": {"5": "Wry.", "3": "Flat.", "1": "Earnest."},
                    },
                },
            ],
            judge={"jury_size": 2, "repetitions": 1},
        )

    def test_a_rules_only_goal_needs_no_calibration(self, client: TestClient) -> None:
        _create(client)
        body = client.get("/api/v1/goals/house_voice/calibration/report").json()
        assert body["calibration_state"] == "not_required"

    def test_a_judged_goal_starts_insufficient_not_uncalibrated(self, client: TestClient) -> None:
        # "Uncalibrated" means measured and found wanting; before any measurement it would be a
        # claim about a jury nobody has run.
        self._judged(client)
        body = client.get("/api/v1/goals/house_voice/calibration/report").json()
        assert body["calibration_state"] == "insufficient"

    def test_samples_and_grades_round_trip(self, client: TestClient) -> None:
        self._judged(client)
        added = client.post(
            "/api/v1/goals/house_voice/calibration/samples",
            json={"samples": [{"content": f"sample {index}"} for index in range(4)]},
        )
        assert added.status_code == 201
        ids = added.json()["added"]
        assert len(ids) == 4
        graded = client.post(
            "/api/v1/goals/house_voice/calibration/grades",
            json={
                "grades": [{"sample_id": ids[0], "criterion": "wit", "grade": 4, "note": "wry"}],
                "graded_by": "tester",
            },
        ).json()
        assert graded["recorded"] == 1
        assert graded["progress"]["remaining"]

    def test_the_calibration_view_shows_the_partition_and_the_progress(
        self, client: TestClient
    ) -> None:
        self._judged(client)
        client.post(
            "/api/v1/goals/house_voice/calibration/samples",
            json={"samples": [{"content": "one sample"}]},
        )
        body = client.get("/api/v1/goals/house_voice/calibration").json()
        assert body["total"] == 1
        assert body["items"][0]["partition"] in {"anchor", "holdout"}
        assert body["progress"]["expected_grades"] == 1

    def test_too_few_grades_is_its_own_conflict_code(self, client: TestClient) -> None:
        self._judged(client)
        added = client.post(
            "/api/v1/goals/house_voice/calibration/samples",
            json={"samples": [{"content": f"sample {index}"} for index in range(2)]},
        ).json()["added"]
        client.post(
            "/api/v1/goals/house_voice/calibration/grades",
            json={
                "grades": [
                    {"sample_id": added[0], "criterion": "wit", "grade": 2},
                    {"sample_id": added[1], "criterion": "wit", "grade": 5},
                ],
                "graded_by": "tester",
            },
        )
        response = client.post(
            "/api/v1/goals/house_voice/calibration/run", json={"graded_by": "tester"}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CALIBRATION_INSUFFICIENT"

    def test_a_grade_off_the_scale_is_a_validation_error(self, client: TestClient) -> None:
        self._judged(client)
        added = client.post(
            "/api/v1/goals/house_voice/calibration/samples",
            json={"samples": [{"content": "one sample"}]},
        ).json()["added"]
        response = client.post(
            "/api/v1/goals/house_voice/calibration/grades",
            json={
                "grades": [{"sample_id": added[0], "criterion": "wit", "grade": 9}],
                "graded_by": "tester",
            },
        )
        assert response.status_code == 400


class TestJudgeEndpoints:
    def test_the_judge_listing_names_every_refusal(self, client: TestClient) -> None:
        body = client.get("/api/v1/judges").json()
        assert set(body) >= {"items", "page", "total", "jury_size"}
        for item in body["items"]:
            assert "eligible" in item
            assert item["judge_benchmark_suite"] == "native.judge"

    def test_a_candidate_is_refused_from_its_own_jury(self, client: TestClient) -> None:
        listed = client.get("/api/v1/judges").json()["items"]
        assert listed, "the fake provider serves at least one model"
        candidate = listed[0]["model"]
        body = client.get(f"/api/v1/judges?candidate={candidate}").json()
        refused = {item["model"]: item["reasons"] for item in body["items"]}
        assert "self_judging" in refused[candidate]

    def test_validating_a_jury_reports_what_would_be_assembled(self, client: TestClient) -> None:
        body = client.post("/api/v1/judges/validate", json={}).json()
        assert "jurors" in body
        assert "jury_reduced" in body
        assert body["requested_size"] >= 1

    def test_validating_against_a_goal_uses_the_goal_s_own_jury_size(
        self, client: TestClient
    ) -> None:
        _create(
            client,
            judge={"jury_size": 1},
            criteria=[
                {
                    "key": "wit",
                    "name": "Dry wit",
                    "rung": "judge",
                    "weight": 1.0,
                    "scale": {
                        "points": 5,
                        "descriptors": {"5": "Wry.", "3": "Flat.", "1": "Earnest."},
                    },
                }
            ],
        )
        body = client.post("/api/v1/judges/validate", json={"goal": "house_voice"}).json()
        assert body["goal"] == "house_voice"
        assert body["requested_size"] == 1

    def test_an_unknown_field_is_refused(self, client: TestClient) -> None:
        response = client.post("/api/v1/judges/validate", json={"goal": "x", "nope": 1})
        assert response.status_code == 400
