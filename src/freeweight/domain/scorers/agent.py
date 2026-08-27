"""freeweight.domain.scorers.agent — rung-2 scoring of a multi-step trajectory.

Benchmark catalog §3.8 measures a *path*, not an answer: task success, steps to completion, tool
calls, wrong turns, retries, unnecessary actions and recovery rate. All of it is countable, which
is what keeps agent behaviour on rung 2 and out of reach of a judge.

**The headline is task success, and only task success.** The path metrics travel beside it and are
never folded into it: a model that reached the right answer in nine steps and one that reached it
in three both succeeded, and the difference between them is `steps_to_completion`, not a discount
on correctness. Blending the two would make "efficient" and "correct" impossible to read apart —
which is precisely what a person comparing two models needs to do.

**A wrong turn is a defined event, not an impression.** It is a call the goal's required sequence
does not contain, or one whose tool answered with an error. Both are things the transcript records;
neither needs anybody's opinion about whether the model was "confused".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from freeweight.domain.scorers.exact import ExactMatchScorer
from freeweight.domain.scorers.tools import (
    STOPPED_STEP_LIMIT,
    ToolExpectation,
    ToolInvocation,
    ToolTranscript,
    tool_metrics,
)
from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from freeweight.domain.benchmark import BenchmarkCase

__all__ = ["EXPECTATION_KEY", "AgentTrajectoryScorer", "trajectory_metrics"]

EXPECTATION_KEY = "tools"
"""Agent cases declare their required sequence in the same shape the tool suites use.

One vocabulary rather than two: an agent goal *is* a required tool sequence plus a finishing
condition, and a second, near-identical declaration format would be a second thing to get wrong.
"""

ERROR_NO_EXPECTATION = "NO_EXPECTATION"
"""The case declared no required sequence, so its trajectory cannot be judged."""


def trajectory_metrics(
    expectation: ToolExpectation, transcript: ToolTranscript, *, succeeded: bool
) -> dict[str, float]:
    """Compute benchmark catalog §3.8's per-sample figures.

    Builds on :func:`~freeweight.domain.scorers.tools.tool_metrics` rather than recomputing the
    shared counts, so "how many calls were redundant" means the same thing in the agent suite as
    in the tool suite and a comparison across the two is meaningful.

    Args:
        expectation: The goal's required sequence and forbidden tools.
        transcript: What the model actually did.
        succeeded: Whether the final answer met the case's finishing condition.

    Returns:
        The metrics, by key. ``steps_to_completion`` and ``recovery_rate`` are **omitted** where
        they have no denominator — a failed run has no completion to count steps to, and a run
        with no failed call has no recovery to have succeeded or failed at (ADR-0016).
    """
    metrics = tool_metrics(expectation, transcript, succeeded=succeeded)
    calls = transcript.calls
    required_names = {call.name for call in expectation.required_calls}
    wrong_turns = sum(1 for call in calls if call.name not in required_names or not call.ok)
    failed_calls = [call for call in calls if not call.ok]
    metrics["tool_calls"] = float(len(calls))
    metrics["wrong_turns"] = float(wrong_turns)
    metrics["unnecessary_actions"] = float(max(0, len(calls) - len(expectation.required_calls)))
    metrics["hit_step_limit"] = 1.0 if transcript.stopped == STOPPED_STEP_LIMIT else 0.0
    if succeeded:
        metrics["steps_to_completion"] = float(transcript.steps)
    if failed_calls:
        # Recovery is judged over the *failures*: a call that failed and was followed by a
        # different call recovered from it. A trajectory with no failure has no recovery rate,
        # and reporting 1.0 there would make an untested model look maximally resilient.
        recovered = sum(
            1 for index, call in enumerate(calls) if not call.ok and _recovered_after(calls, index)
        )
        metrics["recovery_rate"] = recovered / len(failed_calls)
        metrics["retry_count"] = float(
            sum(
                1
                for index, call in enumerate(calls)
                if index > 0 and call.signature == calls[index - 1].signature
            )
        )
        metrics["repeated_error_count"] = float(
            len(failed_calls) - len({call.signature for call in failed_calls})
        )
    return metrics


def _recovered_after(calls: Sequence[ToolInvocation], index: int) -> bool:
    """Return whether any call after ``index`` succeeded.

    Deliberately not "a *different* call succeeded": one of the catalog's injected failures is a
    tool timeout, and retrying the identical call is the correct response to a timeout. Whether
    the model merely retried rather than rethought is measured separately and honestly, by
    ``repeated_error_count`` and ``retry_count`` — folding it into the recovery rate would make
    those two figures redundant and this one ambiguous.

    Giving up is not recovery: a failure with nothing after it counts against the rate rather than
    being excluded from its denominator.
    """
    return any(call.ok for call in calls[index + 1 :])


@dataclass(frozen=True, slots=True)
class AgentTrajectoryScorer:
    """Scores a multi-step goal: did it get there, and what did the path look like.

    The headline ``score`` is task success — ``1.0`` when the final answer meets the case's
    finishing condition, ``0.0`` when it does not. Everything else is reported in ``detail`` and
    picked up by the suite's metric definitions.

    A goal whose final turn never arrived — the model was still calling tools when the step budget
    ran out — is scored ``0.0``, not ``None``: running out of steps is a failure to complete the
    task, and it is exactly the behaviour the ``hit_step_limit`` figure exists to make visible.
    """

    key: str = "agent_trajectory"
    method: ScoreMethod = ScoreMethod.RULE

    def score_trajectory(self, case: BenchmarkCase, transcript: ToolTranscript) -> ScoreResult:
        """Score one goal's trajectory. See
        :class:`~freeweight.domain.scorers.tools.TrajectoryScorer`.

        Args:
            case: The case, carrying its required sequence and its finishing condition.
            transcript: The interaction the run engine recorded.

        Returns:
            The verdict, with the path metrics in ``detail``. ``score=None`` when the case
            declares no required sequence — a defect in the case, not a failure by the model.
        """
        declared = case.expectation.get(EXPECTATION_KEY)
        if not isinstance(declared, dict):
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_NO_EXPECTATION,
                error_text=(
                    f"Case {case.case_id!r} declares no expectation[{EXPECTATION_KEY!r}], so its "
                    "trajectory cannot be judged."
                ),
            )
        expectation = ToolExpectation.from_json(declared)
        # The answer's own evidence travels onto the sample beside the path metrics, so a goal
        # that failed because the model declined the task is distinguishable from one that failed
        # because it could not find its way — the phase's named failure mode, "scoring a refusal
        # as a failure of capability".
        verdict = ExactMatchScorer().score(case, transcript.final_text)
        succeeded = verdict.score is not None and verdict.score > 0.0
        metrics = trajectory_metrics(expectation, transcript, succeeded=succeeded)
        return ScoreResult(
            score=metrics["task_success"],
            method=self.method,
            detail={
                "case": case.case_id,
                **metrics,
                "answer": verdict.detail,
                "transcript": transcript.as_json(),
            },
        )
