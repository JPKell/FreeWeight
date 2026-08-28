"""A goal run generates with the candidate, then judges with the jury — never both at once.

Judging used to happen inside the per-sample loop, immediately after each generation. Nothing about
the measurement required that: :meth:`JudgeCollaborator.score_judged` takes a ``str``, so the jury
grades stored text and *when* it reads changes nothing about what it reads. What it cost was
memory — with the provider's default ``keep_alive`` the candidate stayed resident while each juror
loaded, so a three-juror jury held four models at once — and honesty, because every telemetry
reading taken during judging described a juror rather than the candidate.

So the run has two phases. What is asserted here is that they are genuinely separate, and that
separating them changed no number:

* every generation happens before every judgement, with the candidate evicted in between;
* a sample between the phases is ``awaiting_judgement`` — its own state, with no score;
* the verdict a two-phase run produces is **identical** to the one-phase verdict;
* a jury that fails takes one sample down, not the run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from modelrack.testing import FakeGeneration, FakeScript
from sqlalchemy import select

from freeweight.benchmarks.goal.runner import (
    ERROR_JUDGEMENT_DEFERRED,
    GoalScorer,
    finish_deferred,
)
from freeweight.config import ExecutionSettings, Settings
from freeweight.domain.goals.criteria import CriterionOutcome, CriterionStatus, SkipReason
from freeweight.domain.goals.pack import Rung
from freeweight.infrastructure.db.models_runs import Sample
from freeweight.services.goals import load_goals, sync_goals, write_pack
from freeweight.services.runs import (
    ExecutionConfig,
    build_registry,
    create_run,
    get_run,
)
from freeweight.services.scheduler import RunScheduler

_ANSWER = "I counted the pallets twice. The numbers did not agree. I went home."

_ANCHORED = {
    "points": 5,
    "descriptors": {"5": "Dry and concrete.", "3": "Occasionally so.", "1": "Earnest throughout."},
}


def _goal_body() -> dict[str, Any]:
    """A goal with one rule criterion and one judged criterion, so both phases have work."""
    return {
        "slug": "two_phase_voice",
        "name": "Two-phase voice",
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "intent": "Essays that sound like me.",
        "created_by": "tester",
        "criteria": [
            {
                "key": "no_llm_tells",
                "name": "No LLM tells",
                "rung": "rule",
                "weight": 0.5,
                "rule": {"type": "forbidden_phrases", "phrases": ["delve", "tapestry"]},
            },
            {
                "key": "dry_wit",
                "name": "Dry wit",
                "rung": "judge",
                "weight": 0.5,
                "scale": _ANCHORED,
            },
        ],
        "judge": {"jury_size": 2, "repetitions": 1},
    }


def _task_record() -> dict[str, Any]:
    return {
        "prompt_id": "goals.two_phase_voice.warehouse",
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": "One essay prompt.",
        "task": "goal.two_phase_voice",
        "capability": "creative_writing",
        "system": "Write in your own voice.",
        "template": "Write about the night the inventory did not add up.",
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
            "goal_task": {"key": "warehouse", "name": "Warehouse night"},
        },
    }


@dataclass
class RecordingJury:
    """A jury that grades deterministically and records when it was asked.

    The ``log`` is shared with the provider double, so the two phases' calls land on one timeline
    and their order is a fact rather than an inference.
    """

    log: list[str]
    grade: float = 0.75
    fail: bool = False
    assembly: Any = field(default=None)

    def refusal_detail(self) -> dict[str, Any]:
        return {"jurors": ["juror-a", "juror-b"], "self_judging_refused": []}

    def score_judged(
        self, *, criteria: Sequence[Any], response_text: str, case: Any
    ) -> list[CriterionOutcome]:
        del response_text, case
        self.log.append("judge")
        if self.fail:
            raise RuntimeError("the jury could not be reached")
        return [
            CriterionOutcome(
                criterion_key=criterion.key,
                rung=Rung.JUDGE,
                weight=criterion.weight,
                raw_score=self.grade,
                status=CriterionStatus.SCORED,
                gated=False,
                skip_reason=None,
                detail={"judge_verdicts": []},
            )
            for criterion in criteria
        ]


@dataclass
class _Assembly:
    jurors: tuple[str, ...] = ("juror-a", "juror-b")
    requested_size: int = 2
    self_judging_refused: tuple[str, ...] = ()
    reduced: bool = False


@pytest.fixture
def goals_root(tmp_path: Path) -> Path:
    root = tmp_path / "goals"
    root.mkdir()
    return root


@pytest.fixture
def written_goal(goals_root: Path) -> Any:
    return write_pack(goals_root, goal=_goal_body(), tasks=[_task_record()])


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
def judged_run(
    run_environment: Callable[..., Any],
    goals_root: Path,
    written_goal: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Any]:
    """Run the goal with a recording jury bound, and hand back the environment and the log."""

    def run(*, fail: bool = False) -> tuple[Any, Any, list[str]]:
        log: list[str] = []
        jury = RecordingJury(log=log, fail=fail, assembly=_Assembly())

        import freeweight.services.jury as jury_module

        monkeypatch.setattr(jury_module, "build_jury", lambda *a, **k: jury)

        goals = load_goals(goals_root)
        environment = run_environment(
            script=FakeScript(generations=(FakeGeneration(text=_ANSWER),)),
            registry=build_registry(goals=goals),
        )

        # The provider double logs a generation on the same timeline the jury logs a judgement,
        # and logs the eviction that is supposed to sit between the two phases.
        provider = environment.provider
        real_generate = provider.generate
        real_unload = getattr(provider, "unload", None)

        def generate(request: Any) -> Any:
            log.append("generate")
            return real_generate(request)

        def unload(identity: Any) -> bool:
            log.append("unload")
            return real_unload(identity) if real_unload is not None else True

        monkeypatch.setattr(provider, "generate", generate)
        monkeypatch.setattr(provider, "unload", unload)

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
            environment.database,
            environment.provider,
            registry=environment.registry,
            settings=Settings(),
        ).run_once()
        return environment, get_run(environment.database, summary.id), log

    return run


class TestThePhasesAreSeparate:
    def test_every_generation_happens_before_every_judgement(
        self, judged_run: Callable[..., Any]
    ) -> None:
        """The property the whole change exists for."""
        _environment, _detail, log = judged_run()

        assert "generate" in log and "judge" in log, log
        last_generation = max(index for index, entry in enumerate(log) if entry == "generate")
        first_judgement = min(index for index, entry in enumerate(log) if entry == "judge")
        assert last_generation < first_judgement, log

    def test_the_candidate_is_evicted_between_the_phases(
        self, judged_run: Callable[..., Any]
    ) -> None:
        """So a juror has the machine to itself rather than loading beside the candidate."""
        _environment, _detail, log = judged_run()

        assert "unload" in log, log
        eviction = log.index("unload")
        assert log.index("judge") > eviction
        assert max(index for index, entry in enumerate(log) if entry == "generate") < eviction

    def test_the_run_still_completes_with_a_composite(self, judged_run: Callable[..., Any]) -> None:
        _environment, detail, _log = judged_run()

        assert detail.run.status == "completed"
        composite = next(
            metric
            for metric in detail.metrics
            if metric.metric_key == "composite_score" and metric.run_test_id is None
        )
        assert composite.numeric_value is not None

    def test_no_sample_is_left_awaiting_judgement(self, judged_run: Callable[..., Any]) -> None:
        """A completed run with pending samples would be a run whose own status lied."""
        environment, _detail, _log = judged_run()

        with environment.database.read() as session:
            statuses = set(session.scalars(select(Sample.status)))

        assert "awaiting_judgement" not in statuses
        assert statuses == {"completed"}


class TestOneSampleAtATime:
    def test_a_jury_that_fails_takes_one_sample_not_the_run(
        self, judged_run: Callable[..., Any]
    ) -> None:
        """This phase runs after every sample has been generated: aborting would throw away a
        whole run's work over one unjudgeable answer."""
        environment, detail, _log = judged_run(fail=True)

        assert detail.run.status == "completed"
        with environment.database.read() as session:
            rows = list(session.scalars(select(Sample)))
        assert rows
        assert {row.status for row in rows} == {"failed"}
        assert {row.error_code for row in rows} == {"JUDGE_ERROR"}


class TestTwoPhasesScoreTheSameAsOne:
    """The equivalence that makes the split a scheduling change rather than a measurement one."""

    @staticmethod
    def _scorer(goal: Any, jury: Any, *, defer: bool) -> GoalScorer:
        return GoalScorer(pack=goal.pack, judge=jury, defer_judging=defer)

    def test_the_deferred_verdict_matches_the_single_phase_one(self, written_goal: Any) -> None:
        case = next(iter(_cases(written_goal)))
        jury = RecordingJury(log=[])

        single = self._scorer(written_goal, jury, defer=False).score(case, _ANSWER)

        deferring = self._scorer(written_goal, jury, defer=True)
        partial = deferring.score(case, _ANSWER)
        finished = finish_deferred(
            deferring, case=case, response_text=_ANSWER, stored_detail=partial.detail
        )

        assert partial.score is None
        assert partial.error_code == ERROR_JUDGEMENT_DEFERRED
        assert finished.score == single.score
        assert finished.method == single.method
        assert finished.detail == single.detail

    def test_the_deferred_pass_marks_judged_criteria_as_not_yet_rather_than_never(
        self, written_goal: Any
    ) -> None:
        """``judge_deferred`` and ``judge_unavailable`` are different facts: one says the jury has
        not run, the other that no jury could be assembled at all."""
        case = next(iter(_cases(written_goal)))
        partial = self._scorer(written_goal, RecordingJury(log=[]), defer=True).score(case, _ANSWER)

        judged = [
            entry for entry in partial.detail["criteria"] if entry["rung"] == Rung.JUDGE.value
        ]
        assert judged
        assert {entry["skip_reason"] for entry in judged} == {SkipReason.JUDGE_DEFERRED.value}

    def test_the_rules_are_read_back_rather_than_run_twice(self, written_goal: Any) -> None:
        """A rule with any time dependence would otherwise give two answers to one question."""
        case = next(iter(_cases(written_goal)))
        deferring = self._scorer(written_goal, RecordingJury(log=[]), defer=True)
        partial = deferring.score(case, _ANSWER)

        # Corrupt the stored rule outcome. If the finish re-ran the rules it would overwrite this;
        # reading it back means the corruption survives, which is what proves the direction.
        tampered = {
            "criteria": [
                {**entry, "raw_score": 0.123} if entry["rung"] == Rung.RULE.value else entry
                for entry in partial.detail["criteria"]
            ]
        }
        finished = finish_deferred(
            deferring, case=case, response_text=_ANSWER, stored_detail=tampered
        )

        rules = [entry for entry in finished.detail["criteria"] if entry["rung"] == Rung.RULE.value]
        assert [entry["raw_score"] for entry in rules] == [0.123]


def _cases(goal: Any) -> Any:
    """The goal's own cases, built the way the run engine builds them."""
    from freeweight.benchmarks.goal.runner import build_goal_benchmark

    benchmark = build_goal_benchmark(goal)
    return [case for test in benchmark.tests for case in test.cases()]
