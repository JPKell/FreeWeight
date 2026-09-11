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

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from weightsdb import MigrationRunner, create_engine_for

from freeweight.config import load_settings
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
        offered = [item for item in body["items"] if item["rule_type"] == "forbidden_phrases"]
        assert offered[0]["criterion"] == "not_linkedin"
        assert offered[0]["parameters"]["phrases"], "a proposal arrives with its parameters"
        assert "No corporate hedging" in offered[0]["explanation"]
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

    def test_the_grading_view_is_blinded_and_holds_what_was_graded(
        self, client: TestClient
    ) -> None:
        self._judged(client)
        ids = client.post(
            "/api/v1/goals/house_voice/calibration/samples",
            json={"samples": [{"content": f"sample {index}"} for index in range(8)]},
        ).json()["added"]
        client.post(
            "/api/v1/goals/house_voice/calibration/grades",
            json={
                "grades": [{"sample_id": ids[2], "criterion": "wit", "grade": 4, "note": "wry"}],
                "graded_by": "tester",
            },
        )
        body = client.get("/api/v1/goals/house_voice/calibration/grading").json()
        assert [criterion["key"] for criterion in body["criteria"]] == ["wit"]
        assert body["criteria"][0]["descriptors"] == {"5": "Wry.", "3": "Flat.", "1": "Earnest."}
        order = [sample["sample_id"] for sample in body["samples"]]
        assert sorted(order) == sorted(ids)
        assert order != ids, "the grading order is not the order the samples were added in"
        assert all(set(sample) == {"sample_id", "content", "grades"} for sample in body["samples"])
        graded = next(sample for sample in body["samples"] if sample["sample_id"] == ids[2])
        assert graded["grades"] == {"wit": {"grade": 4, "note": "wry"}}
        assert body["progress"]["recorded_grades"] == 1
        assert "partition" not in json.dumps(body["samples"])
        assert "origin" not in json.dumps(body["samples"])
        again = client.get("/api/v1/goals/house_voice/calibration/grading").json()
        assert again["samples"] == body["samples"], "the order holds across reloads"

    def test_a_grade_for_a_sample_this_goal_does_not_have_lands_nothing(
        self, client: TestClient
    ) -> None:
        self._judged(client)
        response = client.post(
            "/api/v1/goals/house_voice/calibration/grades",
            json={
                "grades": [
                    {"sample_id": "01J00000000000000000000000", "criterion": "wit", "grade": 3}
                ]
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_a_promotion_names_a_real_run_sample_and_a_paste_carries_text(
        self, client: TestClient
    ) -> None:
        self._judged(client)
        promoted = client.post(
            "/api/v1/goals/house_voice/calibration/samples",
            json={"samples": [{"source_sample_id": "01J00000000000000000000000"}]},
        )
        assert promoted.status_code == 400
        assert "not a completed sample" in promoted.json()["error"]["message"]
        empty = client.post("/api/v1/goals/house_voice/calibration/samples", json={"samples": [{}]})
        assert empty.status_code == 400


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

    def test_a_juror_s_bias_figures_are_its_latest_judge_run(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import UTC, datetime
        from types import SimpleNamespace

        import freeweight.services.results as results

        def row(run_id: str, key: str, value: float, *, test: str | None = None) -> Any:
            return SimpleNamespace(
                run_id=run_id,
                run_test_id=test,
                metric_key=key,
                numeric_value=value,
                run_created_at=datetime(2026, 9, 2 if run_id == "new" else 1, tzinfo=UTC),
            )

        rows = (
            row("new", "swap_consistency", 0.5, test="t1"),
            row("new", "swap_consistency", 0.9),
            row("new", "pairwise_accuracy", 0.8, test="t1"),
            row("new", "not_a_bias_figure", 1.0),
            row("old", "transitivity_violation_rate", 0.4),
        )
        asked: list[tuple[str | None, str | None]] = []

        def query(database: Any, wanted: Any) -> Any:
            asked.append((wanted.model, wanted.suite))
            return SimpleNamespace(rows=rows)

        monkeypatch.setattr(results, "query_results", query)
        items = client.get("/api/v1/judges").json()["items"]
        figures = items[0]["judge_results"]
        assert figures["run_id"] == "new"
        assert figures["created_at"] == "2026-09-02T00:00:00.000Z"
        assert figures["metrics"]["swap_consistency"] == 0.9, "the run's roll-up wins"
        assert figures["metrics"]["pairwise_accuracy"] == 0.8
        assert figures["metrics"]["transitivity_violation_rate"] is None, "another run's figure"
        assert "not_a_bias_figure" not in figures["metrics"]
        assert asked[0] == (items[0]["model"], "native.judge")

    def test_a_model_never_measured_as_a_judge_has_no_figures(self, client: TestClient) -> None:
        items = client.get("/api/v1/judges").json()["items"]
        assert items
        assert all(item["judge_results"] is None for item in items)


class TestDraftsOverTheApi:
    """The wizard's drafts, driven over the API as the pages drive them (api.md §3a)."""

    def _draft(self, client: TestClient) -> dict[str, Any]:
        response = client.post(
            "/api/v1/goals/drafts",
            json={"intent": "Essays that sound like me.", "name": "My voice"},
        )
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    def test_a_draft_is_authored_step_by_step_and_written_once(self, client: TestClient) -> None:
        base = f"/api/v1/goals/drafts/{self._draft(client)['draft_id']}"
        added = client.post(
            f"{base}/criteria", json={"action": "add", "name": "Dry wit", "intent": "Wry."}
        ).json()
        assert [criterion["key"] for criterion in added["criteria"]] == ["dry_wit"]
        assert added["criteria"][0]["needs_descriptors"] is True
        described = client.post(
            f"{base}/criteria",
            json={
                "action": "describe", "criterion": "dry_wit", "points": 5,
                "top": "Wry.", "middle": "Flat.", "bottom": "Earnest.",
            },
        ).json()  # fmt: skip
        assert described["criteria"][0]["descriptors"] == {
            "5": "Wry.",
            "3": "Flat.",
            "1": "Earnest.",
        }
        answered = client.post(
            f"{base}/criteria",
            json={"action": "answer", "criterion": "dry_wit", "graded_alike": False},
        ).json()
        assert answered["criteria"][0]["needs_attention"] is True
        client.post(f"{base}/criteria", json={"action": "add", "name": "No LLM tells"})
        before = client.get(base).json()
        assert not any(proposal["accepted"] for proposal in before["proposals"])
        assert before["weight_shift"]["deterministic"] == 0
        accepted = client.post(
            f"{base}/rules",
            json={
                "criterion": "no_llm_tells",
                "rule_type": "forbidden_phrases",
                "parameters": {"phrases": ["delve"]},
            },
        ).json()
        assert [p["criterion"] for p in accepted["proposals"] if p["accepted"]] == ["no_llm_tells"]
        assert accepted["weight_shift"]["deterministic"] == 0.5
        tasked = client.post(
            f"{base}/tasks", json={"name": "Warehouse", "prompt_text": "Write about the night."}
        ).json()
        assert [task["key"] for task in tasked["tasks"]] == ["warehouse"]

        saved = client.post(f"{base}/save", json={})
        assert saved.status_code == 200, saved.text
        assert saved.json()["goal"]["slug"] == "my_voice"
        assert client.get("/api/v1/goals/my_voice").status_code == 200
        again = client.post(f"{base}/save", json={"slug": "another_name"})
        assert again.json()["goal"]["slug"] == "my_voice", "a saved draft answers its own pack"
        listed = client.get("/api/v1/goals/drafts").json()["items"]
        assert listed[0]["saved_slug"] == "my_voice"

    def test_a_starter_arrives_as_a_draft_of_starter_tasks(self, client: TestClient) -> None:
        response = client.post("/api/v1/goals/drafts", json={"starter": "creative_voice"})
        assert response.status_code == 201
        draft = response.json()
        assert draft["forked_from"] == "creative_voice"
        assert draft["criteria"]
        assert draft["tasks"] and all(task["is_starter"] for task in draft["tasks"])
        unknown = client.post("/api/v1/goals/drafts", json={"starter": "no_such_starter"})
        assert unknown.status_code == 404

    def test_the_list_names_live_drafts_and_an_abandoned_one_is_gone(
        self, client: TestClient
    ) -> None:
        draft_id = self._draft(client)["draft_id"]
        listed = client.get("/api/v1/goals/drafts").json()
        assert [item["draft_id"] for item in listed["items"]] == [draft_id]
        assert listed["items"][0]["criteria"] == 0
        assert listed["items"][0]["expires_at"]
        assert client.delete(f"/api/v1/goals/drafts/{draft_id}").status_code == 204
        assert client.get(f"/api/v1/goals/drafts/{draft_id}").status_code == 404
        assert client.delete(f"/api/v1/goals/drafts/{draft_id}").status_code == 404
        assert client.get("/api/v1/goals/drafts").json()["items"] == []

    def test_refusals_are_the_service_s_own(self, client: TestClient) -> None:
        base = f"/api/v1/goals/drafts/{self._draft(client)['draft_id']}"
        client.post(f"{base}/criteria", json={"action": "add", "name": "Dry wit"})
        thin = client.post(
            f"{base}/criteria",
            json={"action": "describe", "criterion": "dry_wit", "points": 5, "top": "Wry."},
        )
        assert thin.status_code == 400
        nobody = client.post(f"{base}/criteria", json={"action": "answer", "criterion": "nope"})
        assert nobody.status_code == 400
        unanchored = client.post(f"{base}/save", json={})
        assert unanchored.status_code == 400
        assert client.post("/api/v1/goals/drafts", json={"intent": "  "}).status_code == 400
        assert client.post(f"{base}/criteria", json={"action": "shout"}).status_code == 400


class TestThePackIsServedWhole:
    """The documents `PUT` takes, the bundle that round-trips, and the calibration fields."""

    def test_the_detail_carries_the_documents_put_takes(self, client: TestClient) -> None:
        created = _create(client)
        body = client.get("/api/v1/goals/house_voice").json()
        assert body["pack"] == {"goal": _goal_body(), "tasks": [_task_record()]}
        assert body["calibration"] is None
        replaced = client.put("/api/v1/goals/house_voice", json=body["pack"]).json()
        assert replaced["hash_change"]["separates"] is False
        assert replaced["goal_hash"] == created["goal_hash"]

    def test_a_replacement_carries_the_pack_s_other_files_over(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _create(client)
        kept = tmp_path / "goals" / "house_voice" / "calibration" / "notes.json"
        kept.parent.mkdir(parents=True)
        kept.write_text('{"kept": true}\n', encoding="utf-8")
        pack = client.get("/api/v1/goals/house_voice").json()["pack"]
        pack["goal"]["name"] = "House voice, renamed"
        assert client.put("/api/v1/goals/house_voice", json=pack).status_code == 200
        assert kept.read_text(encoding="utf-8") == '{"kept": true}\n'

    def test_the_bundle_is_the_cli_s_document_and_imports_back_to_the_same_hash(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        from freeweight.services.goals import bundle_text, get_goal

        created = _create(client)
        response = client.get("/api/v1/goals/house_voice/bundle")
        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            'attachment; filename="house_voice.goal-bundle.json"'
        )
        assert response.text == bundle_text(get_goal(tmp_path / "goals", "house_voice"))
        bundle = response.json()
        collision = client.post("/api/v1/goals/import", json={"bundle": bundle})
        assert collision.status_code == 409
        assert collision.json()["error"]["details"]["existing_goal_hash"] == created["goal_hash"]
        assert client.delete("/api/v1/goals/house_voice?dry_run=false").status_code == 200
        imported = client.post("/api/v1/goals/import", json={"bundle": bundle})
        assert imported.status_code == 201
        assert imported.json()["goal_hash"] == created["goal_hash"]

    def test_the_listing_carries_the_calibration_fields(self, client: TestClient) -> None:
        _create(client)
        item = client.get("/api/v1/goals").json()["items"][0]
        assert item["calibration_state"] == "calibrated", "a rules-only goal needs no calibration"
        assert item["kappa_w"] is None
        assert item["n_holdout"] is None
        assert item["calibrated_at"] is None
        assert item["calibration_stale"] is False


class TestAGoalWrittenWhileServingIsRunnable:
    """A goal created or edited while FreeWeight runs is runnable at once, as it now stands.

    The registry was built once at startup: a goal written since was ``BENCHMARK_NOT_FOUND`` when
    the scheduler claimed its run (found by row WP4's demonstration), and an edited goal would have
    run under its startup rubric.
    """

    def test_a_goal_created_and_edited_after_startup_runs_its_current_rubric(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        database = tmp_path / "freeweight.sqlite3"
        for name, value in {
            "FREEWEIGHT_STORAGE__DATABASE_URL": f"sqlite:///{database}",
            "FREEWEIGHT_PROVIDER__KIND": "fake",
            "FREEWEIGHT_GOALS__ROOT": str(tmp_path / "goals"),
            "FREEWEIGHT_EXECUTION__WARMUP_REPETITIONS": "0",
            "FREEWEIGHT_EXECUTION__COOLDOWN_SECONDS": "0",
            "FREEWEIGHT_EXECUTION__IDLE_GPU_THRESHOLD_PERCENT": "0",
        }.items():
            monkeypatch.setenv(name, value)
        engine = create_engine_for(f"sqlite:///{database}")
        try:
            MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        finally:
            engine.dispose()
        settings = load_settings(config_path=tmp_path / "missing.toml").settings
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            assert client.post("/api/v1/models/discover").status_code == 200
            model = client.get("/api/v1/models").json()["items"][0]["canonical_id"]

            def run_to_the_end(execution: dict[str, Any]) -> dict[str, Any]:
                started = client.post(
                    "/api/v1/runs",
                    json={"model": model, "suites": ["goal.house_voice"], "execution": execution},
                )
                assert started.status_code == 201, started.text
                run_id = started.json()["id"]
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    body: dict[str, Any] = client.get(f"/api/v1/runs/{run_id}").json()
                    if body["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                        return body
                    time.sleep(0.2)
                raise AssertionError(f"run {run_id} did not finish")

            created = _create(client)
            first = run_to_the_end({"measured_repetitions": 1})
            assert first["status"] == "completed", first.get("error")
            assert first["suite"]["version"].endswith(created["goal_hash"][7:15])

            pack = client.get("/api/v1/goals/house_voice").json()["pack"]
            pack["goal"]["criteria"][0]["rule"]["phrases"] = ["delve", "tapestry"]
            edited = client.put("/api/v1/goals/house_voice", json=pack).json()
            assert edited["hash_change"]["separates"] is True
            second = run_to_the_end({"measured_repetitions": 1})
            assert second["status"] == "completed", second.get("error")
            assert second["suite"]["version"].endswith(edited["goal_hash"][7:15])
            # A deletion orphans the run measured before the edit as well as the one after it
            # (the preview counted only the current hash's runs; found by row WP4's demonstration).
            assert client.delete("/api/v1/goals/house_voice").json()["orphaned_runs"] == 2


class TestAForkLosesItsBadgeWhenItsContentIsEdited:
    """Subjective Goals §8: ``unforked`` until the criteria or the tasks are edited — and no longer.

    Nothing cleared the field: a fork edited through ``PUT`` stayed badged ``unforked`` with its
    lint's ``UNFORKED_STARTER`` warning (found by row WP4's demonstration).
    """

    def test_a_criteria_edit_clears_the_badge_and_a_rename_does_not(
        self, client: TestClient
    ) -> None:
        forked = client.post("/api/v1/goals/starters/creative_voice/fork", json={"slug": "mine"})
        assert forked.json()["unforked"] is True
        pack = client.get("/api/v1/goals/mine").json()["pack"]

        renamed = copy.deepcopy(pack)
        renamed["goal"]["name"] = "Mine"
        assert client.put("/api/v1/goals/mine", json=renamed).json()["unforked"] is True

        edited = copy.deepcopy(pack)
        edited["goal"]["criteria"][0]["weight"] = round(
            edited["goal"]["criteria"][0]["weight"] + 0.05, 4
        )
        edited["goal"]["criteria"][1]["weight"] = round(
            edited["goal"]["criteria"][1]["weight"] - 0.05, 4
        )
        preview = client.put("/api/v1/goals/mine?dry_run=true", json=edited).json()
        assert preview["unforked"] is False
        assert client.get("/api/v1/goals/mine").json()["unforked"] is True, (
            "a dry run writes nothing"
        )
        applied = client.put("/api/v1/goals/mine", json=edited).json()
        assert applied["unforked"] is False
        assert client.get("/api/v1/goals/mine").json()["pack"]["goal"]["unforked"] is False
