"""Assembling a jury against a real provider: refusals, degradation, and the empty case.

The phase's own test list, three clauses of it:

* **self-judging is refused and recorded, not silently discounted**;
* a jury **reduced below ``jury_size`` records ``jury_reduced`` and still scores**;
* **zero eligible jurors ⇒ judged criteria ``skipped (judge_unavailable)``, rule criteria still
  score, and the partial result says so**.

Everything runs against :class:`~modelrack.testing.FakeProvider`: no GPU, no Ollama, no network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from modelrack.providers.fake import FakeModel
from modelrack.testing import FakeGeneration, FakeProvider, FakeScript

from freeweight.config import JudgeSettings
from freeweight.domain.goals.criteria import CriterionStatus, SkipReason
from freeweight.domain.goals.pack import GoalPack, GoalTask, parse_pack
from freeweight.domain.judging import REASON_REMOTE_NOT_PERMITTED, REASON_SELF_JUDGING
from freeweight.domain.jury import assemble_jury
from freeweight.services.jury import build_jury
from freeweight.services.prompts import load_pack

_ANCHORED = {
    "points": 5,
    "descriptors": {"5": "Wry and understated.", "3": "Flat.", "1": "Earnest."},
}


def _pack(**changes: Any) -> GoalPack:
    body: dict[str, Any] = {
        "slug": "voice",
        "name": "Voice",
        "goal_pack_version": "1.0.0",
        "criteria": [
            {
                "key": "tells",
                "name": "No LLM tells",
                "rung": "rule",
                "weight": 0.5,
                "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
            },
            {"key": "wit", "name": "Dry wit", "rung": "judge", "weight": 0.5, "scale": _ANCHORED},
        ],
        "judge": {"jury_size": 3, "repetitions": 1},
    }
    body.update(changes)
    task = GoalTask(
        key="t1",
        name="Task",
        prompt_id="goals.voice.t1",
        prompt_version="1.0.0",
        prompt_sha256="sha256:" + "ab" * 32,
        rendered_prompt_hash="sha256:" + "cd" * 32,
        prompt_text="Write about a warehouse.",
    )
    return parse_pack(body, tasks=[task])


def _provider(names: list[str], *, grade: int = 4) -> FakeProvider:
    """A fake provider serving several models, every one of which grades the same way."""
    script = FakeScript(
        models=tuple(FakeModel(name=name) for name in names),
        generations=(FakeGeneration(text=json.dumps({"grade": grade, "reason": "because"})),),
    )
    return FakeProvider(script, seed=7)


def _canonical(provider: FakeProvider) -> list[str]:
    return sorted(descriptor.identity.canonical_id for descriptor in provider.list_models())


def _case(prompt: str = "Write about a warehouse.") -> Any:
    from freeweight.domain.benchmark import BenchmarkCase

    return BenchmarkCase(case_id="c1", ordinal=0, prompt=prompt, metadata={"task_text": prompt})


class TestSelfJudgingIsRefusedAndRecorded:
    def test_the_candidate_is_excluded_from_its_own_jury(self) -> None:
        provider = _provider(["alpha", "beta", "gamma"])
        candidate = _canonical(provider)[0]
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=3, repetitions=1),
            candidate_canonical_id=candidate,
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        assert candidate not in [juror.canonical_id for juror in jury.jurors]
        assert candidate in jury.assembly.self_judging_refused

    def test_the_refusal_is_recorded_rather_than_discounted(self) -> None:
        provider = _provider(["alpha", "beta", "gamma"])
        candidate = _canonical(provider)[0]
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=3, repetitions=1),
            candidate_canonical_id=candidate,
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        detail = jury.refusal_detail()
        refused = {entry["model"]: entry["reasons"] for entry in detail["refused"]}
        assert REASON_SELF_JUDGING in refused[candidate]
        assert detail["self_judging_refused"] == [candidate]

    def test_the_refusal_can_be_turned_off_only_deliberately(self) -> None:
        # ``judge.refuse_self_judging`` exists so the behaviour is configuration rather than
        # folklore; it defaults on and nothing here flips it by accident.
        provider = _provider(["alpha", "beta", "gamma"])
        candidate = _canonical(provider)[0]
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=3, repetitions=1, refuse_self_judging=False),
            candidate_canonical_id=candidate,
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        assert candidate in [juror.canonical_id for juror in jury.jurors]


class TestAReducedJuryStillScores:
    def test_two_eligible_models_for_a_jury_of_three(self) -> None:
        provider = _provider(["alpha", "beta", "gamma"])
        candidate = _canonical(provider)[0]
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=3, repetitions=1),
            candidate_canonical_id=candidate,
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        assert len(jury.jurors) == 2  # noqa: PLR2004 — three installed, one is the candidate
        assert jury.assembly.reduced is True
        assert jury.assembly.as_json()["reduction_reason"] == "jury_reduced"

    def test_and_it_still_produces_a_score(self) -> None:
        provider = _provider(["alpha", "beta", "gamma"], grade=5)
        candidate = _canonical(provider)[0]
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=3, repetitions=1),
            candidate_canonical_id=candidate,
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        pack = _pack()
        outcomes = jury.score_judged(
            criteria=list(pack.judged_criteria), response_text="An answer.", case=_case()
        )
        assert outcomes[0].status is CriterionStatus.SCORED
        assert outcomes[0].raw_score == 1.0

    def test_a_single_juror_loses_inter_juror_agreement_and_says_so(self) -> None:
        provider = _provider(["alpha", "beta"])
        candidate = _canonical(provider)[0]
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=3, repetitions=1),
            candidate_canonical_id=candidate,
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        pack = _pack()
        result = jury.grade_all(list(pack.judged_criteria), "An answer.", _case())[0]
        assert len(jury.jurors) == 1
        assert result.inter_juror_alpha is None
        assert result.outcome.status is CriterionStatus.SCORED


class TestZeroEligibleJurors:
    def test_judged_criteria_skip_with_judge_unavailable(self) -> None:
        provider = _provider(["alpha"])
        candidate = _canonical(provider)[0]
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=3, repetitions=1),
            candidate_canonical_id=candidate,
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        assert jury.assembly.available is False
        pack = _pack()
        outcomes = jury.score_judged(
            criteria=list(pack.judged_criteria), response_text="An answer.", case=_case()
        )
        assert outcomes[0].status is CriterionStatus.SKIPPED
        assert outcomes[0].skip_reason == SkipReason.JUDGE_UNAVAILABLE.value
        assert outcomes[0].raw_score is None

    def test_and_the_rule_criteria_still_score(self) -> None:
        # Spec §13: "Rule criteria never depend on a provider."
        from freeweight.benchmarks.goal.runner import GoalScorer

        pack = _pack()
        provider = _provider(["alpha"])
        candidate = _canonical(provider)[0]
        jury = build_jury(
            provider,
            pack=pack,
            library=load_pack(),
            settings=JudgeSettings(jury_size=3, repetitions=1),
            candidate_canonical_id=candidate,
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        verdict = GoalScorer(pack=pack, judge=jury).score(_case(), "A clean answer, no tells.")
        assert verdict.score == 1.0
        by_key = {entry["key"]: entry for entry in verdict.detail["criteria"]}
        assert by_key["tells"]["status"] == "scored"
        assert by_key["wit"]["status"] == "skipped"

    def test_and_the_partial_result_says_which_weight_contributed(self) -> None:
        from freeweight.benchmarks.goal.runner import GoalScorer

        pack = _pack()
        verdict = GoalScorer(pack=pack, judge=None).score(_case(), "A clean answer, no tells.")
        assert verdict.detail["applied_weight"] == pytest.approx(0.5)
        assert verdict.detail["declared_weight"] == pytest.approx(1.0)
        assert verdict.detail["applied_weight_share"] == pytest.approx(0.5)
        assert verdict.detail["score_method_mix"]["judge"] == 0.0


class TestTheRemoteOptIn:
    """Two opt-ins, and neither can be satisfied by accident (ADR-0031 §4)."""

    _AVAILABLE = ["ollama/local@sha256:" + "ab" * 32, "openai/remote@sha256:" + "cd" * 32]
    _REMOTE = {_AVAILABLE[1]: True}

    def test_neither_flag_refuses_the_remote_juror(self) -> None:
        verdicts = assemble_jury(
            self._AVAILABLE, candidate=None, remote=self._REMOTE, allow_remote=False
        )
        assert verdicts.jurors == (self._AVAILABLE[0],)
        assert REASON_REMOTE_NOT_PERMITTED in verdicts.refusals[0].reasons

    def test_both_flags_admit_it_and_the_jury_records_that_it_is_remote(self) -> None:
        assembly = assemble_jury(
            self._AVAILABLE, candidate=None, remote=self._REMOTE, allow_remote=True
        )
        assert set(assembly.jurors) == set(self._AVAILABLE)
        assert assembly.remote is True
        assert assembly.as_json()["remote"] is True

    def test_the_goal_s_own_flag_is_required_as_well_as_the_provider_s(self) -> None:
        provider = _provider(["alpha", "beta"])
        pack = _pack(judge={"jury_size": 2, "repetitions": 1, "allow_remote": False})
        jury = build_jury(
            provider,
            pack=pack,
            library=load_pack(),
            settings=JudgeSettings(jury_size=2, repetitions=1, allow_remote=True),
            candidate_canonical_id="",
            available=_canonical(provider),
            allow_remote_provider=True,
            remote=dict.fromkeys(_canonical(provider), True),
        )
        # The provider allows remote and the settings allow remote, but the goal does not.
        assert jury.jurors == ()

    def test_a_remote_jury_is_a_different_instrument_on_the_wire(self) -> None:
        from freeweight.domain.jury import judge_set_identity

        local = judge_set_identity(
            assemble_jury(self._AVAILABLE[:1], candidate=None),
            prompt_id="goals.judge.rubric",
            prompt_version="1.0.0",
            prompt_sha256="sha256:" + "ef" * 32,
        )
        remote = judge_set_identity(
            assemble_jury(self._AVAILABLE, candidate=None, remote=self._REMOTE, allow_remote=True),
            prompt_id="goals.judge.rubric",
            prompt_version="1.0.0",
            prompt_sha256="sha256:" + "ef" * 32,
        )
        assert local["remote"] is False
        assert remote["remote"] is True
        assert local["jurors"] != remote["jurors"]


class TestTheJuryLinksToItsOwnBiasResults:
    def test_every_judged_score_can_reach_the_juror_s_judge_benchmark(self) -> None:
        from freeweight.domain.judging import JUDGE_SUITE_KEY

        provider = _provider(["alpha", "beta"])
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=2, repetitions=1),
            candidate_canonical_id="",
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        detail = jury.refusal_detail()
        assert detail["prompt_id"] == "goals.judge.rubric"
        assert detail["prompt_sha256"].startswith("sha256:")
        from freeweight.domain.jury import judge_set_identity

        identity = judge_set_identity(
            jury.assembly,
            prompt_id=detail["prompt_id"],
            prompt_version=detail["prompt_version"],
            prompt_sha256=detail["prompt_sha256"],
        )
        assert identity["judge_benchmark"]["suite_key"] == JUDGE_SUITE_KEY


class TestAJurorThatAnswersBadly:
    def test_prose_is_a_protocol_refusal_rather_than_a_grade(self) -> None:
        script = FakeScript(
            models=(FakeModel(name="alpha"), FakeModel(name="beta")),
            generations=(FakeGeneration(text="I think it is quite good, really."),),
        )
        provider = FakeProvider(script, seed=7)
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=2, repetitions=1),
            candidate_canonical_id="",
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        pack = _pack()
        result = jury.grade_all(list(pack.judged_criteria), "An answer.", _case())[0]
        assert result.outcome.status is CriterionStatus.SKIPPED
        assert result.outcome.detail["refusals"] == ["protocol_error"]

    def test_a_grade_off_the_scale_is_refused_rather_than_clamped(self) -> None:
        # A juror that answered 7 on a five-point scale did not understand the rubric, and
        # clamping would hide that behind a plausible 5.
        script = FakeScript(
            models=(FakeModel(name="alpha"), FakeModel(name="beta")),
            generations=(FakeGeneration(text=json.dumps({"grade": 7, "reason": "great"})),),
        )
        provider = FakeProvider(script, seed=7)
        jury = build_jury(
            provider,
            pack=_pack(),
            library=load_pack(),
            settings=JudgeSettings(jury_size=2, repetitions=1),
            candidate_canonical_id="",
            available=_canonical(provider),
            allow_remote_provider=False,
        )
        pack = _pack()
        result = jury.grade_all(list(pack.judged_criteria), "An answer.", _case())[0]
        assert result.outcome.status is CriterionStatus.SKIPPED


class TestTheJuryAppearsInTheRunRecord:
    """Acceptance criterion 4: the refusal appears in the run record, not only in the report."""

    def test_a_goal_run_records_its_jury_and_every_refusal(
        self, tmp_path: Path, run_environment: Callable[..., Any]
    ) -> None:
        from freeweight.config import ExecutionSettings, GoalSettings, Settings
        from freeweight.services.goals import load_goals, sync_goals, write_pack
        from freeweight.services.runs import (
            ExecutionConfig,
            build_registry,
            create_run,
            get_run,
        )
        from freeweight.services.scheduler import RunScheduler

        root = tmp_path / "goals"
        root.mkdir()
        body: dict[str, Any] = {
            "slug": "judged_voice",
            "name": "Judged voice",
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
                    "key": "wit",
                    "name": "Dry wit",
                    "rung": "judge",
                    "weight": 0.5,
                    "scale": _ANCHORED,
                },
            ],
            "judge": {"jury_size": 3, "repetitions": 1},
        }
        record: dict[str, Any] = {
            "prompt_id": "goals.judged_voice.t1",
            "version": "1.0.0",
            "schema_version": "1.0",
            "purpose": "One task.",
            "task": "goal.judged_voice",
            "capability": "creative_writing",
            "system": None,
            "template": "Write about the warehouse.",
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
                "change_reason": "First.",
                "supersedes": None,
                "tags": ["goal"],
                "goal_task": {"key": "t1", "name": "Task"},
            },
        }
        write_pack(root, goal=body, tasks=[record])
        goals = load_goals(root)
        script = FakeScript(
            models=(FakeModel(name="alpha"), FakeModel(name="beta")),
            generations=(
                FakeGeneration(text="I counted the pallets twice."),
                FakeGeneration(text=json.dumps({"grade": 4, "reason": "wry"})),
            ),
        )
        environment = run_environment(script=script, registry=build_registry(goals=goals))
        sync_goals(environment.database, goals)
        settings = Settings(goals=GoalSettings(root=str(root)))
        summary = create_run(
            environment.database,
            environment.provider,
            environment.collector,
            environment.registry,
            model_ref=environment.model_ref,
            suite_key="goal.judged_voice",
            execution=ExecutionConfig.resolve(
                ExecutionSettings(
                    warmup_repetitions=0,
                    cooldown_seconds=0,
                    idle_gpu_threshold_percent=0,
                ),
                measured_repetitions=1,
            ),
        )
        RunScheduler(
            environment.database,
            environment.provider,
            registry=environment.registry,
            settings=settings,
        ).run_once()
        detail = get_run(environment.database, summary.id)
        kinds = {entry["kind"] for entry in detail.run.degradations}
        assert "judge_set" in kinds
        judge_set = next(
            entry["detail"] for entry in detail.run.degradations if entry["kind"] == "judge_set"
        )
        # The candidate is excluded from its own jury, and the exclusion is on the run.
        assert environment.model_ref in judge_set["self_judging_refused"]
        assert environment.model_ref not in judge_set["jurors"]
        # A jury of one where three were asked for is a recorded degradation, not a silent one.
        assert "jury_reduced" in kinds
        assert judge_set["prompt_id"] == "goals.judge.rubric"
