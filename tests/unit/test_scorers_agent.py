"""The agent trajectory scorer: task success as the headline, path quality beside it.

Benchmark catalog §3.8. The property this module is really guarding is that *correctness and
efficiency stay separable*: a model that reached the right answer in nine steps and one that
reached it in three both score ``1.0``, and the difference is a number a reader can see.
"""

from __future__ import annotations

import pytest

from freeweight.domain.benchmark import BenchmarkCase
from freeweight.domain.scorers.agent import AgentTrajectoryScorer, trajectory_metrics
from freeweight.domain.scorers.tools import (
    STOPPED_STEP_LIMIT,
    ExpectedCall,
    ToolExpectation,
    ToolInvocation,
    ToolTranscript,
    TrajectoryScorer,
)

_OFFERED = ("search_symbol", "read_file", "list_directory")
_GOAL: dict[str, object] = {
    "required_calls": [
        {"name": "search_symbol", "arguments": {"symbol": "restock_cost"}},
        {"name": "read_file", "arguments": {"path": "pkg/pricing.py"}},
    ],
    "ordered": True,
}


def _case(answer: str = "total_units") -> BenchmarkCase:
    return BenchmarkCase(
        case_id="goal-1",
        ordinal=0,
        prompt="p",
        expectation={"tools": _GOAL, "exact": {"any_of": [answer], "contains": True}},
    )


def _call(name: str, *, ok: bool = True, step: int = 1, **arguments: object) -> ToolInvocation:
    return ToolInvocation(
        step=step,
        name=name,
        arguments=arguments,
        ok=ok,
        error_code=None if ok else "NOT_FOUND",
        known_tool=name in _OFFERED,
    )


def _transcript(
    *calls: ToolInvocation,
    final_text: str = "It imports total_units.",
    steps: int = 3,
    stopped: str = "answered",
) -> ToolTranscript:
    return ToolTranscript(
        calls=calls, final_text=final_text, steps=steps, stopped=stopped, offered_tools=_OFFERED
    )


class TestKnownPassAndFail:
    """The headline is task success and nothing else."""

    def test_it_is_a_trajectory_scorer(self) -> None:
        assert isinstance(AgentTrajectoryScorer(), TrajectoryScorer)

    def test_a_correct_answer_scores_one(self) -> None:
        verdict = AgentTrajectoryScorer().score_trajectory(
            _case(),
            _transcript(
                _call("search_symbol", symbol="restock_cost"),
                _call("read_file", step=2, path="pkg/pricing.py"),
            ),
        )
        assert verdict.score == 1.0
        assert verdict.detail["steps_to_completion"] == 3
        assert verdict.detail["wrong_turns"] == 0

    def test_a_wrong_answer_scores_zero_however_tidy_the_path(self) -> None:
        verdict = AgentTrajectoryScorer().score_trajectory(
            _case(),
            _transcript(
                _call("search_symbol", symbol="restock_cost"),
                _call("read_file", step=2, path="pkg/pricing.py"),
                final_text="It imports nothing.",
            ),
        )
        assert verdict.score == 0.0
        assert verdict.detail["ordering_accuracy"] == 1.0, (
            "a perfect path with a wrong answer still reports the path honestly"
        )

    def test_efficiency_never_discounts_correctness(self) -> None:
        efficient = AgentTrajectoryScorer().score_trajectory(
            _case(),
            _transcript(
                _call("search_symbol", symbol="restock_cost"),
                _call("read_file", step=2, path="pkg/pricing.py"),
            ),
        )
        wandering = AgentTrajectoryScorer().score_trajectory(
            _case(),
            _transcript(
                _call("list_directory", path="."),
                _call("list_directory", step=2, path="pkg"),
                _call("search_symbol", step=3, symbol="restock_cost"),
                _call("read_file", step=4, path="pkg/pricing.py"),
                steps=6,
            ),
        )
        assert efficient.score == wandering.score == 1.0
        assert wandering.detail["unnecessary_actions"] == 2
        assert wandering.detail["wrong_turns"] == 2
        assert wandering.detail["steps_to_completion"] == 6


class TestBoundaryAndAbsence:
    """A figure with an empty denominator is missing, never zero."""

    def test_a_failed_goal_reports_no_steps_to_completion(self) -> None:
        metrics = trajectory_metrics(
            ToolExpectation((ExpectedCall("search_symbol"),)),
            _transcript(_call("search_symbol"), final_text="no idea"),
            succeeded=False,
        )
        assert "steps_to_completion" not in metrics

    def test_a_trajectory_with_no_failure_reports_no_recovery_rate(self) -> None:
        metrics = trajectory_metrics(
            ToolExpectation((ExpectedCall("search_symbol"),)),
            _transcript(_call("search_symbol")),
            succeeded=True,
        )
        assert "recovery_rate" not in metrics, (
            "1.0 here would make a model that was never tested look maximally resilient"
        )

    def test_recovery_counts_a_later_success_including_a_plain_retry(self) -> None:
        # Retrying an identical call is the right answer to a timeout, so it counts as recovery;
        # how much of the path was repetition is reported separately.
        metrics = trajectory_metrics(
            ToolExpectation((ExpectedCall("search_symbol"),)),
            _transcript(
                _call("search_symbol", ok=False, symbol="x"),
                _call("search_symbol", step=2, symbol="x"),
            ),
            succeeded=True,
        )
        assert metrics["recovery_rate"] == 1.0
        assert metrics["retry_count"] == 1.0

    def test_a_failure_with_nothing_after_it_is_not_recovered(self) -> None:
        metrics = trajectory_metrics(
            ToolExpectation((ExpectedCall("search_symbol"),)),
            _transcript(_call("search_symbol", ok=False), final_text="giving up"),
            succeeded=False,
        )
        assert metrics["recovery_rate"] == 0.0

    def test_repeated_identical_failures_are_counted(self) -> None:
        metrics = trajectory_metrics(
            ToolExpectation((ExpectedCall("search_symbol"),)),
            _transcript(
                _call("search_symbol", ok=False, symbol="x"),
                _call("search_symbol", ok=False, step=2, symbol="x"),
                final_text="giving up",
            ),
            succeeded=False,
        )
        assert metrics["repeated_error_count"] == 1.0

    def test_running_out_of_steps_is_a_failure_and_is_visible(self) -> None:
        verdict = AgentTrajectoryScorer().score_trajectory(
            _case(),
            _transcript(
                _call("list_directory", path="."), final_text="", stopped=STOPPED_STEP_LIMIT
            ),
        )
        assert verdict.score == 0.0
        assert verdict.detail["hit_step_limit"] == 1.0


class TestMalformedAndMissing:
    """A defect in the goal is unscoreable; a defect in the model is a zero."""

    def test_a_goal_with_no_required_sequence_is_unscoreable(self) -> None:
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="p")
        verdict = AgentTrajectoryScorer().score_trajectory(case, _transcript())
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"

    def test_a_goal_with_no_finishing_condition_cannot_succeed_silently(self) -> None:
        # The exact-match scorer is unscoreable without an expected answer, and this scorer must
        # not read that as success.
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="p", expectation={"tools": _GOAL})
        assert AgentTrajectoryScorer().score_trajectory(case, _transcript()).score == 0.0

    def test_an_empty_trajectory_scores_zero(self) -> None:
        verdict = AgentTrajectoryScorer().score_trajectory(_case(), _transcript(final_text=""))
        assert verdict.score == 0.0
        assert verdict.detail["tool_calls"] == 0.0

    @pytest.mark.parametrize("stopped", ["answered", "provider_error", STOPPED_STEP_LIMIT])
    def test_every_stop_reason_produces_a_scoreable_sample(self, stopped: str) -> None:
        verdict = AgentTrajectoryScorer().score_trajectory(
            _case(), _transcript(final_text="", stopped=stopped)
        )
        assert verdict.score == 0.0
