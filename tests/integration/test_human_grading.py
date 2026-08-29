"""Rung-4 (``human``) criteria, graded after the run: the second entry point of the grading UI.

Subjective Goals §3.3: a ``human`` criterion queues the sample for the user to grade in a blinded
UI. Phase 10A shipped the calibration half; this is the run half, and it lands in Phase 11 because
this is where a human grade first has somewhere to go — evidence. The properties asserted:

* the samples are presented **blinded** (the model is never fetched) and **shuffled** (not the
  order they were produced in), and the order is stable across reloads;
* a grade lands on the sample's own criterion row, the sample's composite follows, the run's
  aggregate metrics follow, and the subject's ``user.<slug>`` evidence follows — by the same path
  a rule's score took during the run;
* regrading replaces; a grade outside the scale, on a sample outside the run, or against a rubric
  that has changed since the run is refused;
* the screen and the CLI are the same service.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from baseaicore import ValidationError
from fastapi.testclient import TestClient
from modelrack.testing import FakeGeneration, FakeScript
from typer.testing import CliRunner

from freeweight.cli.main import app as cli_app
from freeweight.config import ExecutionSettings, Settings, load_settings
from freeweight.infrastructure.db.models_goals import CriterionScore
from freeweight.infrastructure.db.repositories.runs import MetricValueRepository
from freeweight.services.calibration import (
    RunGradeSubmission,
    RunNotGradeable,
    record_run_grades,
    run_grading_view,
)
from freeweight.services.evidence import EvidenceQuery, query_evidence
from freeweight.services.goals import load_goals, replace_pack, sync_goals, write_pack
from freeweight.services.runs import (
    ExecutionConfig,
    RunNotFound,
    build_registry,
    create_run,
    get_run,
    list_samples,
)
from freeweight.services.scheduler import RunScheduler
from freeweight.web.app import create_app

_ANSWER = "The inventory did not add up. I counted the pallets twice and wrote the number down."
_TASKS = ("warehouse", "ledger", "night_shift", "audit", "forklift", "morning")


def _goal_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": "plain_voice",
        "name": "Plain voice",
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "created_by": "tester",
        "criteria": [
            {
                "key": "tells",
                "name": "No LLM tells",
                "rung": "rule",
                "weight": 0.5,
                "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
            },
            {
                "key": "would_ship",
                "name": "Would ship it",
                "rung": "human",
                "weight": 0.5,
                "scale": {"points": 5, "descriptors": {"5": "Yes.", "3": "Maybe.", "1": "No."}},
            },
        ],
        "calibration": {"min_samples": 8, "target_samples": 12, "holdout_fraction": 0.4},
    }
    body.update(changes)
    return body


def _task_record(key: str) -> dict[str, Any]:
    return {
        "prompt_id": f"goals.plain_voice.{key}",
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": "One task.",
        "task": "goal.plain_voice",
        "capability": "creative_writing",
        "system": None,
        "template": f"Write two paragraphs about the {key.replace('_', ' ')}.",
        "variables": {},
        "response": {"format": "text", "json_schema_ref": None, "expectations": []},
        "model_requirements": {
            "min_context_tokens": 2048,
            "requires_capabilities": [],
            "recommended_temperature": 0.8,
        },
        "metadata": {
            "author": "tester",
            "created_at": "2026-08-27T00:00:00Z",
            "changed_at": "2026-08-27T00:00:00Z",
            "change_reason": "First version.",
            "supersedes": None,
            "tags": ["goal"],
            "goal_task": {"key": key, "name": key.replace("_", " ").title()},
        },
    }


def _execution() -> ExecutionConfig:
    return ExecutionConfig.resolve(
        ExecutionSettings(
            warmup_repetitions=0,
            cooldown_seconds=0,
            idle_gpu_threshold_percent=0,
            randomize_case_order=False,
        ),
        measured_repetitions=1,
    )


@pytest.fixture
def goals_root(tmp_path: Path) -> Path:
    root = tmp_path / "goals"
    root.mkdir()
    return root


@pytest.fixture
def written_goal(goals_root: Path) -> Any:
    return write_pack(goals_root, goal=_goal_body(), tasks=[_task_record(key) for key in _TASKS])


@pytest.fixture
def graded_run(
    run_environment: Callable[..., Any], goals_root: Path, written_goal: Any
) -> tuple[Any, str]:
    """A completed run of the goal, with its human criterion still pending."""
    goals = load_goals(goals_root)
    environment = run_environment(
        script=FakeScript(generations=(FakeGeneration(text=_ANSWER),)),
        registry=build_registry(goals=goals),
    )
    sync_goals(environment.database, goals)
    summary = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="goal.plain_voice",
        execution=_execution(),
    )
    RunScheduler(
        environment.database,
        environment.provider,
        registry=environment.registry,
        settings=Settings(),
    ).run_once()
    detail = get_run(environment.database, summary.id)
    assert detail.run.status == "completed"
    return environment, str(summary.id)


def _stored_order(environment: Any, run_id: str) -> list[str]:
    detail = get_run(environment.database, run_id)
    ordered: list[str] = []
    for test in detail.tests:
        ordered.extend(sample.id for sample in list_samples(environment.database, test.id))
    return ordered


def _composite_metric(environment: Any, run_id: str) -> float | None:
    with environment.database.read() as session:
        rows = MetricValueRepository().list_for_run(session, run_id)
    row = next(r for r in rows if r.metric_key == "composite_score" and r.run_test_id is None)
    return row.numeric_value


class TestTheViewIsBlindedAndShuffled:
    def test_every_completed_sample_is_offered_with_its_text(
        self, graded_run: tuple[Any, str]
    ) -> None:
        environment, run_id = graded_run
        view = run_grading_view(environment.database, run_id)
        assert len(view.samples) == len(_TASKS)
        assert all(sample.response_text == _ANSWER for sample in view.samples)
        assert [criterion.key for criterion in view.criteria] == ["would_ship"]
        assert view.expected == len(_TASKS)
        assert view.recorded == 0
        assert view.complete is False

    def test_the_model_is_never_fetched(self, graded_run: tuple[Any, str]) -> None:
        environment, run_id = graded_run
        view = run_grading_view(environment.database, run_id)
        rendered = json.dumps(view.as_json())
        assert environment.model_ref not in rendered
        assert "model" not in rendered.lower()

    def test_the_order_is_not_the_stored_order_and_is_stable(
        self, graded_run: tuple[Any, str]
    ) -> None:
        environment, run_id = graded_run
        first = [
            sample.sample_id for sample in run_grading_view(environment.database, run_id).samples
        ]
        second = [
            sample.sample_id for sample in run_grading_view(environment.database, run_id).samples
        ]
        assert first == second
        assert sorted(first) == sorted(_stored_order(environment, run_id))
        assert first != _stored_order(environment, run_id)

    def test_before_grading_the_human_criterion_is_pending_and_excluded(
        self, graded_run: tuple[Any, str]
    ) -> None:
        environment, run_id = graded_run
        with environment.database.read() as session:
            rows = list(session.query(CriterionScore).filter_by(criterion_key="would_ship"))
        assert rows and all(row.status == "skipped" for row in rows)
        assert all(row.skip_reason == "human_grade_pending" for row in rows)
        assert all(row.raw_score is None for row in rows)


class TestAGradeReachesTheEvidence:
    def test_a_grade_finishes_the_sample_the_run_and_the_evidence(
        self, graded_run: tuple[Any, str]
    ) -> None:
        environment, run_id = graded_run
        before_composite = _composite_metric(environment, run_id)
        before_evidence = query_evidence(environment.database, EvidenceQuery()).records[0]
        view = run_grading_view(environment.database, run_id)
        submissions = [
            RunGradeSubmission(sample.sample_id, "would_ship", 5, note="ship it")
            for sample in view.samples
        ]
        recorded = record_run_grades(
            environment.database,
            run_id,
            submissions,
            graded_by="tester",
            registry=environment.registry,
        )
        assert recorded == len(_TASKS)

        with environment.database.read() as session:
            rows = list(session.query(CriterionScore).filter_by(criterion_key="would_ship"))
        assert all(row.status == "scored" and row.raw_score == 1.0 for row in rows)
        assert all(row.detail_json["human_grade"] == 5 for row in rows)
        assert all(row.detail_json["graded_by"] == "tester" for row in rows)

        after = run_grading_view(environment.database, run_id)
        assert after.complete is True
        assert all(sample.grades["would_ship"]["grade"] == 5 for sample in after.samples)

        # The rule scored 1.0 (no "delve") and the grade scored 1.0, so the composite is 1.0
        # over the *whole* rubric; before, the composite covered the rule alone.
        after_composite = _composite_metric(environment, run_id)
        assert before_composite == pytest.approx(1.0)
        assert after_composite == pytest.approx(1.0)
        detail = get_run(environment.database, run_id)
        samples = list_samples(environment.database, detail.tests[0].id)
        assert all(
            sample.detail["applied_weight_share"] == pytest.approx(1.0) for sample in samples
        )
        assert all(
            sample.detail["score_method_mix"]["human"] == pytest.approx(0.5) for sample in samples
        )

        after_evidence = query_evidence(environment.database, EvidenceQuery()).records[0]
        assert after_evidence.capability_id == "user.plain_voice"
        assert after_evidence.judge_validity_factor == 1.0, "a human grade is valid by definition"
        assert after_evidence.score_method_mix is not None
        assert after_evidence.score_method_mix["human"] == pytest.approx(0.5)
        assert before_evidence.score_method_mix is not None
        assert before_evidence.score_method_mix.get("human", 0.0) == pytest.approx(0.0)
        assert after_evidence.computed_at >= before_evidence.computed_at

    def test_a_low_grade_lowers_the_composite(self, graded_run: tuple[Any, str]) -> None:
        environment, run_id = graded_run
        sample = run_grading_view(environment.database, run_id).samples[0]
        record_run_grades(
            environment.database,
            run_id,
            [RunGradeSubmission(sample.sample_id, "would_ship", 1)],
            graded_by="tester",
            registry=environment.registry,
        )
        detail = get_run(environment.database, run_id)
        graded = next(
            s
            for s in list_samples(environment.database, detail.tests[0].id)
            if s.id == sample.sample_id
        )
        assert graded.score == pytest.approx(0.5)  # 0.5 × rule 1.0 + 0.5 × grade 0.0

    def test_regrading_replaces_rather_than_appends(self, graded_run: tuple[Any, str]) -> None:
        environment, run_id = graded_run
        sample = run_grading_view(environment.database, run_id).samples[0]
        for grade in (2, 4):
            record_run_grades(
                environment.database,
                run_id,
                [RunGradeSubmission(sample.sample_id, "would_ship", grade)],
                graded_by="tester",
                registry=environment.registry,
            )
        view = run_grading_view(environment.database, run_id)
        assert view.recorded == 1
        graded = next(s for s in view.samples if s.sample_id == sample.sample_id)
        assert graded.grades["would_ship"]["grade"] == 4


class TestRefusals:
    def test_a_grade_outside_the_scale_is_refused(self, graded_run: tuple[Any, str]) -> None:
        environment, run_id = graded_run
        sample = run_grading_view(environment.database, run_id).samples[0]
        with pytest.raises(ValidationError, match="scale"):
            record_run_grades(
                environment.database,
                run_id,
                [RunGradeSubmission(sample.sample_id, "would_ship", 6)],
                graded_by="tester",
                registry=environment.registry,
            )

    def test_a_criterion_that_is_not_human_is_refused(self, graded_run: tuple[Any, str]) -> None:
        environment, run_id = graded_run
        sample = run_grading_view(environment.database, run_id).samples[0]
        with pytest.raises(ValidationError, match="human criterion"):
            record_run_grades(
                environment.database,
                run_id,
                [RunGradeSubmission(sample.sample_id, "tells", 3)],
                graded_by="tester",
                registry=environment.registry,
            )

    def test_a_sample_outside_the_run_is_refused(self, graded_run: tuple[Any, str]) -> None:
        environment, run_id = graded_run
        with pytest.raises(ValidationError, match="not a completed sample"):
            record_run_grades(
                environment.database,
                run_id,
                [RunGradeSubmission("01J00000000000000000000000", "would_ship", 3)],
                graded_by="tester",
                registry=environment.registry,
            )

    def test_an_unknown_run_is_not_found(self, graded_run: tuple[Any, str]) -> None:
        environment, _run_id = graded_run
        with pytest.raises(RunNotFound):
            run_grading_view(environment.database, "01J00000000000000000000000")

    def test_a_run_of_a_goal_without_human_criteria_is_not_gradeable(
        self, run_environment: Callable[..., Any], goals_root: Path
    ) -> None:
        body = _goal_body(criteria=[_goal_body()["criteria"][0] | {"weight": 1.0}])
        write_pack(goals_root, goal=body, tasks=[_task_record("warehouse")])
        goals = load_goals(goals_root)
        environment = run_environment(
            script=FakeScript(generations=(FakeGeneration(text=_ANSWER),)),
            registry=build_registry(goals=goals),
        )
        sync_goals(environment.database, goals)
        summary = create_run(
            environment.database,
            environment.provider,
            environment.collector,
            environment.registry,
            model_ref=environment.model_ref,
            suite_key="goal.plain_voice",
            execution=_execution(),
        )
        RunScheduler(
            environment.database, environment.provider, registry=environment.registry
        ).run_once()
        with pytest.raises(RunNotGradeable, match="no human criterion"):
            run_grading_view(environment.database, summary.id)

    def test_a_rubric_changed_since_the_run_is_refused(
        self, graded_run: tuple[Any, str], goals_root: Path, written_goal: Any
    ) -> None:
        """A grade against a different rubric would belong to a measurement it was not part of."""
        environment, run_id = graded_run
        changed = _goal_body()
        changed["criteria"][0]["rule"]["phrases"] = ["delve", "tapestry"]
        replace_pack(
            goals_root,
            slug="plain_voice",
            goal=changed,
            tasks=[_task_record(key) for key in _TASKS],
        )
        sync_goals(environment.database, load_goals(goals_root))
        with pytest.raises(RunNotGradeable, match="has changed since"):
            run_grading_view(environment.database, run_id)


class TestTheScreenAndTheCli:
    @pytest.fixture
    def client(
        self,
        graded_run: tuple[Any, str],
        goals_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> Iterator[tuple[TestClient, Any, str]]:
        environment, run_id = graded_run
        monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", environment.database_url)
        monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
        monkeypatch.setenv("FREEWEIGHT_GOALS__ROOT", str(goals_root))
        loaded = load_settings(config_path=tmp_path / "missing.toml")
        with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
            yield test_client, environment, run_id

    def test_the_screen_renders_blinded_and_records_a_grade(
        self, client: tuple[TestClient, Any, str]
    ) -> None:
        test_client, environment, run_id = client
        page = test_client.get(f"/runs/{run_id}/grade")
        assert page.status_code == 200
        assert "Grade run" in page.text
        assert environment.model_ref not in page.text
        assert _ANSWER in page.text

        sample = run_grading_view(environment.database, run_id).samples[0]
        posted = test_client.post(
            f"/runs/{run_id}/grade",
            data={
                "sample_id": sample.sample_id,
                "criterion": "would_ship",
                "grade": "5",
                "note": "ship it",
            },
            follow_redirects=False,
        )
        assert posted.status_code == 303
        reloaded = test_client.get(f"/runs/{run_id}/grade").text
        assert "graded 5" in reloaded
        assert "1 / 6" in reloaded

    def test_a_rejected_grade_lands_nothing_and_says_so(
        self, client: tuple[TestClient, Any, str]
    ) -> None:
        test_client, environment, run_id = client
        sample = run_grading_view(environment.database, run_id).samples[0]
        rejected = test_client.post(
            f"/runs/{run_id}/grade",
            data={"sample_id": sample.sample_id, "criterion": "would_ship", "grade": "9"},
            follow_redirects=False,
        )
        assert rejected.status_code == 400
        assert "scale" in rejected.text
        assert run_grading_view(environment.database, run_id).recorded == 0

    def test_the_run_page_links_to_the_grading_screen(
        self, client: tuple[TestClient, Any, str]
    ) -> None:
        test_client, _environment, run_id = client
        assert f"/runs/{run_id}/grade" in test_client.get(f"/runs/{run_id}").text

    def test_an_ungradeable_run_explains_itself(self, client: tuple[TestClient, Any, str]) -> None:
        test_client, _environment, _run_id = client
        missing = test_client.get("/runs/01J00000000000000000000000/grade")
        assert missing.status_code == 404
        assert "RUN_NOT_FOUND" in missing.text

    def test_the_cli_records_the_same_grades(
        self,
        graded_run: tuple[Any, str],
        goals_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        environment, run_id = graded_run
        monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", environment.database_url)
        monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
        monkeypatch.setenv("FREEWEIGHT_GOALS__ROOT", str(goals_root))
        samples = run_grading_view(environment.database, run_id).samples
        grades = tmp_path / "grades.json"
        grades.write_text(
            json.dumps(
                [
                    {"sample_id": sample.sample_id, "criterion": "would_ship", "grade": 4}
                    for sample in samples[:2]
                ]
            ),
            encoding="utf-8",
        )
        result = CliRunner().invoke(
            cli_app,
            ["goals", "grade", "plain_voice", "--run", run_id, "--file", str(grades), "--json"],
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.stdout)
        assert body["recorded"] == 2
        assert body["recorded_grades"] == 2
        assert body["expected_grades"] == len(_TASKS)
        assert "model" not in result.stdout.lower()
