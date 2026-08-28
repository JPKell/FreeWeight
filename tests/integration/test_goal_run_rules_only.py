"""Phase 8A end to end: a hand-written, rules-only goal, run with no judge anywhere.

The phase's acceptance criteria, in order:

1. a hand-written goal pack with only rule criteria runs end to end and produces a composite
   score, per-criterion scores and ``score_method_mix = {rule: 1.0}``;
2. the run works with **no judge configured at all**, and its ``judge_validity_factor`` is 1.0;
3. every headline number drills to the criterion and the sample that produced it in at most two
   interactions;
4. renaming a criterion leaves ``goal_hash`` unchanged, changing its phrase list changes it, and
   the change reports how many existing runs it separates before it is applied;
5. ``goals validate`` on a deliberately bad pack names every problem with a severity — which is a
   unit-level assertion and lives in ``tests/unit/test_goal_lint.py``.

Everything here runs against :class:`~modelrack.testing.FakeProvider`: no GPU, no Ollama, no
network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from modelrack.testing import FakeGeneration, FakeScript
from sqlalchemy import select

from freeweight.config import ExecutionSettings
from freeweight.domain.goals.criteria import CriterionStatus, SkipReason
from freeweight.infrastructure.db.models_goals import CriterionScore, Goal, GoalCriterion
from freeweight.infrastructure.db.models_runs import BenchmarkSuite, Sample
from freeweight.services.goals import (
    get_goal,
    goal_hash_change,
    list_goals,
    load_goals,
    replace_pack,
    sync_goals,
    write_pack,
)
from freeweight.services.runs import (
    ExecutionConfig,
    build_registry,
    create_run,
    get_run,
    list_samples,
)
from freeweight.services.scheduler import RunScheduler

_ANSWER = (
    "I counted the pallets twice. The second count matched the first, which was the problem.\n\n"
    "Nobody had signed the log since Tuesday. I walked the north aisle and found nothing, then "
    "walked it again more slowly and found the same nothing, arranged differently.\n\n"
    "I wrote the number on my hand and went home. It was still wrong in the morning."
)


def _goal_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": "noir_tech_voice",
        "name": "Noir-ish tech essay voice",
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "intent": "Essays that sound like me: dry, concrete, unhurried.",
        "created_by": "tester",
        "criteria": [
            {
                "key": "no_llm_tells",
                "name": "No LLM tells",
                "rung": "rule",
                "weight": 0.4,
                "gate": True,
                "rule": {
                    "type": "forbidden_phrases",
                    "phrases": ["delve", "tapestry", "in today's landscape"],
                },
            },
            {
                "key": "sentence_rhythm",
                "name": "Varied sentence rhythm",
                "rung": "rule",
                "weight": 0.35,
                "rule": {
                    "type": "sentence_length_distribution",
                    "mean_words": {"min": 8, "max": 24},
                    "cv": {"min": 0.2},
                },
            },
            {
                "key": "first_person_past",
                "name": "First person, past tense",
                "rung": "rule",
                "weight": 0.25,
                "rule": {"type": "pov_tense", "person": "first", "tense": "past"},
            },
        ],
    }
    body.update(changes)
    return body


def _task_record(**changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "prompt_id": "goals.noir_tech_voice.warehouse",
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": "One essay prompt from the author's own work.",
        "task": "goal.noir_tech_voice",
        "capability": "creative_writing",
        "system": "Write in your own voice.",
        "template": "Write three short paragraphs about the night the inventory did not add up.",
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
            "goal_task": {"key": "warehouse", "name": "Warehouse night"},
        },
    }
    record.update(changes)
    return record


@pytest.fixture
def goals_root(tmp_path: Path) -> Path:
    """A goal-pack root under the test's own throwaway tree."""
    root = tmp_path / "goals"
    root.mkdir()
    return root


@pytest.fixture
def written_goal(goals_root: Path) -> Any:
    """One hand-written, rules-only goal pack on disk."""
    return write_pack(goals_root, goal=_goal_body(), tasks=[_task_record()])


def _execution(**overrides: Any) -> ExecutionConfig:
    fields: dict[str, Any] = {
        "warmup_repetitions": 0,
        "cooldown_seconds": 0,
        "idle_gpu_threshold_percent": 0,
        "randomize_case_order": False,
    }
    fields.update(overrides)
    return ExecutionConfig.resolve(ExecutionSettings(**fields), measured_repetitions=1)


@pytest.fixture
def goal_run(
    run_environment: Callable[..., Any], goals_root: Path, written_goal: Any
) -> Callable[..., Any]:
    """Return a factory that runs the installed goal and hands back the environment and detail."""

    def run(script: Any = None) -> tuple[Any, Any]:
        goals = load_goals(goals_root)
        environment = run_environment(
            script=script
            if script is not None
            else FakeScript(generations=(FakeGeneration(text=_ANSWER),)),
            registry=build_registry(goals=goals),
        )
        sync_goals(environment.database, goals)
        summary = create_run(
            environment.database,
            environment.provider,
            environment.collector,
            environment.registry,
            model_ref=environment.model_ref,
            suite_key=f"goal.{written_goal.pack.slug}",
            execution=_execution(),
        )
        RunScheduler(
            environment.database, environment.provider, registry=environment.registry
        ).run_once()
        return environment, get_run(environment.database, summary.id)

    return run


class TestARulesOnlyGoalRunsEndToEnd:
    """Acceptance criterion 1."""

    def test_the_run_completes(self, goal_run: Callable[..., Any]) -> None:
        _environment, detail = goal_run()
        assert detail.run.status == "completed"

    def test_it_produces_a_composite_score(self, goal_run: Callable[..., Any]) -> None:
        _environment, detail = goal_run()
        composite = next(
            metric
            for metric in detail.metrics
            if metric.metric_key == "composite_score" and metric.run_test_id is None
        )
        assert composite.numeric_value is not None
        assert 0.0 < composite.numeric_value <= 1.0

    def test_it_produces_a_metric_per_criterion(self, goal_run: Callable[..., Any]) -> None:
        _environment, detail = goal_run()
        keys = {metric.metric_key for metric in detail.metrics}
        assert {
            "criterion.no_llm_tells",
            "criterion.sentence_rhythm",
            "criterion.first_person_past",
        } <= keys

    def test_the_score_method_mix_is_entirely_rules(self, goal_run: Callable[..., Any]) -> None:
        _environment, detail = goal_run()
        mix = {
            metric.metric_key: metric.numeric_value
            for metric in detail.metrics
            if metric.metric_key.startswith("score_method_mix_") and metric.run_test_id is None
        }
        assert mix == {
            "score_method_mix_rule": 1.0,
            "score_method_mix_reference": 0.0,
            "score_method_mix_human": 0.0,
            "score_method_mix_judge": 0.0,
        }

    def test_the_suite_is_installed_as_a_goal_runner_with_its_hash(
        self, goal_run: Callable[..., Any], written_goal: Any
    ) -> None:
        environment, _detail = goal_run()
        with environment.database.read() as session:
            suite = session.scalars(
                select(BenchmarkSuite).where(BenchmarkSuite.key == "goal.noir_tech_voice")
            ).one()
        assert suite.runner == "goal"
        assert suite.goal_hash == written_goal.goal_hash
        # The hash is in the version too, so a measurement-defining edit cannot land in this
        # version's series.
        assert written_goal.goal_hash.removeprefix("sha256:")[:8] in suite.version
        assert suite.goal_id is not None


class TestNoJudgeIsInvolvedAnywhere:
    """Acceptance criterion 2."""

    def test_the_goal_declares_no_jury(self, written_goal: Any) -> None:
        assert written_goal.pack.judge is None
        assert written_goal.pack.judged_criteria == ()

    def test_every_criterion_is_scored_at_rung_two(self, goal_run: Callable[..., Any]) -> None:
        environment, detail = goal_run()
        with environment.database.read() as session:
            rows = list(session.scalars(select(CriterionScore)))
        assert rows
        assert {row.rung for row in rows} == {"rule"}
        assert {row.status for row in rows} == {CriterionStatus.SCORED.value}

    def test_the_judge_validity_factor_is_one(self, goal_run: Callable[..., Any]) -> None:
        _environment, detail = goal_run()
        factor = next(
            metric
            for metric in detail.metrics
            if metric.metric_key == "judge_validity_factor" and metric.run_test_id is None
        )
        assert factor.numeric_value == 1.0

    def test_the_sample_score_method_is_rule(self, goal_run: Callable[..., Any]) -> None:
        environment, detail = goal_run()
        samples = list_samples(environment.database, detail.tests[0].id, limit=10)
        assert {sample.score_method for sample in samples} == {"rule"}


class TestEveryHeadlineNumberDrillsToItsEvidence:
    """Acceptance criterion 3: the number, the criterion, the sample — two steps."""

    def test_step_one_the_run_s_metrics_name_the_criterion(
        self, goal_run: Callable[..., Any]
    ) -> None:
        _environment, detail = goal_run()
        assert any(metric.metric_key == "criterion.no_llm_tells" for metric in detail.metrics)

    def test_step_two_the_sample_names_what_matched_and_what_was_measured(
        self, goal_run: Callable[..., Any]
    ) -> None:
        environment, detail = goal_run()
        sample = list_samples(environment.database, detail.tests[0].id, limit=1)[0]
        by_key = {entry["key"]: entry for entry in sample.detail["criteria"]}
        # Which phrases matched...
        assert by_key["no_llm_tells"]["detail"]["matched_phrases"] == {}
        assert by_key["no_llm_tells"]["detail"]["phrases_checked"] == 3
        # ...and which distributions were measured.
        rhythm = by_key["sentence_rhythm"]["detail"]
        assert rhythm["sentence_count"] >= 3
        assert rhythm["mean_words"] > 0
        assert rhythm["coefficient_of_variation"] is not None

    def test_the_criterion_rows_are_queryable_beside_the_sample(
        self, goal_run: Callable[..., Any]
    ) -> None:
        environment, detail = goal_run()
        sample = list_samples(environment.database, detail.tests[0].id, limit=1)[0]
        with environment.database.read() as session:
            rows = list(
                session.scalars(select(CriterionScore).where(CriterionScore.sample_id == sample.id))
            )
        assert {row.criterion_key for row in rows} == {
            "no_llm_tells",
            "sentence_rhythm",
            "first_person_past",
        }
        assert all(row.raw_score is not None for row in rows)

    def test_the_response_is_stored_so_the_author_can_re_read_it(
        self, goal_run: Callable[..., Any]
    ) -> None:
        # Spec §12: a judged score the person who defined the rubric cannot re-read is not
        # auditable. Forced on for goal runs, and left alone for every other suite.
        environment, detail = goal_run()
        sample = list_samples(environment.database, detail.tests[0].id, limit=1)[0]
        assert sample.response_text == _ANSWER

    def test_a_criterion_row_cascades_with_its_sample(self, goal_run: Callable[..., Any]) -> None:
        environment, detail = goal_run()
        with environment.database.write() as session:
            sample = session.scalars(select(Sample)).first()
            session.delete(sample)
        with environment.database.read() as session:
            assert list(session.scalars(select(CriterionScore))) == []


class TestRenamingVersusChangingTheMeasurement:
    """Acceptance criterion 4."""

    def test_renaming_a_criterion_leaves_the_hash_alone(
        self, goals_root: Path, written_goal: Any
    ) -> None:
        body = _goal_body()
        body["criteria"][0]["name"] = "No giveaway phrases"
        _previous, current = replace_pack(
            goals_root, slug="noir_tech_voice", goal=body, tasks=[_task_record()]
        )
        assert current.goal_hash == written_goal.goal_hash

    def test_changing_the_phrase_list_changes_the_hash(
        self, goals_root: Path, written_goal: Any
    ) -> None:
        body = _goal_body()
        body["criteria"][0]["rule"]["phrases"].append("leverage")
        _previous, current = replace_pack(
            goals_root, slug="noir_tech_voice", goal=body, tasks=[_task_record()]
        )
        assert current.goal_hash != written_goal.goal_hash

    def test_the_change_reports_how_many_runs_it_separates(
        self, goal_run: Callable[..., Any], goals_root: Path
    ) -> None:
        environment, _detail = goal_run()
        existing = get_goal(goals_root, "noir_tech_voice")
        body = _goal_body()
        body["criteria"][0]["rule"]["phrases"].append("leverage")
        staged = write_pack(
            goals_root.parent / "staged",
            goal={**body, "slug": "noir_tech_voice"},
            tasks=[_task_record()],
        )
        change = goal_hash_change(
            environment.database, slug="noir_tech_voice", existing=existing, replacement=staged
        )
        assert change.separates is True
        assert change.separated_runs == 1
        assert "criteria" in change.changed_fields

    def test_a_rename_reports_no_separation(
        self, goal_run: Callable[..., Any], goals_root: Path
    ) -> None:
        environment, _detail = goal_run()
        existing = get_goal(goals_root, "noir_tech_voice")
        body = _goal_body()
        body["criteria"][0]["name"] = "No giveaway phrases"
        staged = write_pack(goals_root.parent / "renamed", goal=body, tasks=[_task_record()])
        change = goal_hash_change(
            environment.database, slug="noir_tech_voice", existing=existing, replacement=staged
        )
        assert change.separates is False
        assert change.changed_fields == ()

    def test_a_dry_run_leaves_the_stored_pack_alone(
        self, goals_root: Path, written_goal: Any
    ) -> None:
        body = _goal_body()
        body["criteria"][0]["rule"]["phrases"].append("leverage")
        _previous, staged = replace_pack(
            goals_root, slug="noir_tech_voice", goal=body, tasks=[_task_record()], dry_run=True
        )
        assert staged.goal_hash != written_goal.goal_hash
        assert get_goal(goals_root, "noir_tech_voice").goal_hash == written_goal.goal_hash

    def test_a_goal_cannot_be_renamed_in_place(self, goals_root: Path, written_goal: Any) -> None:
        # The slug is the capability its evidence is emitted under, so a rename is a new goal.
        del written_goal
        with pytest.raises(Exception, match="cannot be renamed"):
            replace_pack(
                goals_root,
                slug="noir_tech_voice",
                goal=_goal_body(slug="something_else"),
                tasks=[_task_record()],
            )


class TestAGatedSample:
    """A hard gate zeroes the composite and says which gate did it, end to end."""

    def test_a_forbidden_phrase_zeroes_the_sample(self, goal_run: Callable[..., Any]) -> None:
        script = FakeScript(
            generations=(
                FakeGeneration(
                    text=(
                        "Let us delve into the tapestry of warehouse inventory. "
                        "I counted the pallets twice and wrote the number down."
                    )
                ),
            )
        )
        environment, detail = goal_run(script)
        sample = list_samples(environment.database, detail.tests[0].id, limit=1)[0]
        assert sample.score == 0.0
        assert sample.detail["gated_by"] == "no_llm_tells"
        rate = next(
            metric
            for metric in detail.metrics
            if metric.metric_key == "gated_sample_rate" and metric.run_test_id is None
        )
        assert rate.numeric_value == 1.0


class TestASkippedCriterion:
    """A criterion that could not measure is excluded, and the applied weight says so."""

    def test_an_empty_answer_skips_every_rule_and_the_sample_is_unmeasured(
        self, goal_run: Callable[..., Any]
    ) -> None:
        script = FakeScript(generations=(FakeGeneration(text=""),))
        environment, detail = goal_run(script)
        sample = list_samples(environment.database, detail.tests[0].id, limit=1)[0]
        assert sample.score is None
        assert sample.error_code == "GOAL_UNMEASURED"
        with environment.database.read() as session:
            rows = list(session.scalars(select(CriterionScore)))
        assert {row.status for row in rows} == {CriterionStatus.SKIPPED.value}
        assert {row.skip_reason for row in rows} == {SkipReason.UNSUPPORTED.value}
        assert all(row.raw_score is None for row in rows)


class TestTheGoalIsProjectedIntoTheDatabase:
    """The pack on disk is the source of truth; these rows are its validated projection."""

    def test_the_rows_match_the_pack(self, goal_run: Callable[..., Any], written_goal: Any) -> None:
        environment, _detail = goal_run()
        with environment.database.read() as session:
            goal = session.scalars(select(Goal)).one()
            criteria = list(
                session.scalars(select(GoalCriterion).where(GoalCriterion.goal_id == goal.id))
            )
        assert goal.slug == "noir_tech_voice"
        assert goal.capability_id == "user.noir_tech_voice"
        assert goal.goal_hash == written_goal.goal_hash
        assert goal.pack_sha256 == written_goal.pack_sha256
        assert [criterion.key for criterion in sorted(criteria, key=lambda row: row.ordinal)] == [
            "no_llm_tells",
            "sentence_rhythm",
            "first_person_past",
        ]

    def test_a_replaced_pack_replaces_its_criteria_rather_than_accumulating_them(
        self, goal_run: Callable[..., Any], goals_root: Path
    ) -> None:
        environment, _detail = goal_run()
        body = _goal_body()
        body["criteria"] = [body["criteria"][0] | {"weight": 1.0}]
        replace_pack(goals_root, slug="noir_tech_voice", goal=body, tasks=[_task_record()])
        sync_goals(environment.database, list_goals(goals_root))
        with environment.database.read() as session:
            goal = session.scalars(select(Goal)).one()
            criteria = list(
                session.scalars(select(GoalCriterion).where(GoalCriterion.goal_id == goal.id))
            )
        assert [criterion.key for criterion in criteria] == ["no_llm_tells"]

    def test_the_lint_findings_travel_onto_the_row(self, goal_run: Callable[..., Any]) -> None:
        environment, _detail = goal_run()
        with environment.database.read() as session:
            goal = session.scalars(select(Goal)).one()
        codes = {finding["code"] for finding in goal.lint_json}
        assert "DETERMINISTIC_WEIGHT_SHARE" in codes


class TestTheGoalIsRunnableWithNoProviderAtAll:
    """Spec §13: a goal whose criteria are entirely rungs 1-3 needs no model judging."""

    def test_the_pack_carries_no_judge_configuration_to_resolve(self, written_goal: Any) -> None:
        assert (
            json.dumps({"judge": None if written_goal.pack.judge is None else "present"})
            == '{"judge": null}'
        )
