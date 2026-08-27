"""freeweight.domain.scorers.tools — the tool-call transcript, and the two scorers that read it.

Benchmark catalog §3.6 measures a *trajectory*, not an answer: which tools were chosen, whether
their arguments were right, how many calls it took, and how much of that work was wasted. None of
that is visible in the model's final sentence, so this module defines the record the run engine
keeps — :class:`ToolTranscript` — and the scorers that turn it into numbers.

**The transcript is the evidence, and it is stored.** Every invocation carries its tool, its parsed
arguments, whether that tool exists at all, whether the arguments validated against the tool's own
schema, and what the mock tool answered. A failing sample therefore drills to the exact call that
went wrong rather than to a rate.

**Every figure here is a count.** "Did it call ``search_symbol``", "did the arguments validate",
"how many calls were there" — no step needs a model, which is what makes this suite deterministic
and what keeps the whole of Phase 7 on rung 2 of the scoring ladder.

**A missing measurement is missing, not zero.** A rate whose denominator is empty — ordering
accuracy for a case that requires one call, calls-per-success for a case that failed — is *absent*
from the detail rather than reported as ``0.0``. Aggregation excludes the sample from that metric
and says so in the excluded count (ADR-0016); a zero there would be a claim about ordering the run
never observed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from freeweight.domain.scorers.exact import ExactMatchScorer
from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.domain.benchmark import BenchmarkCase

__all__ = [
    "EXPECTATION_KEY",
    "ExpectedCall",
    "ToolArgumentScorer",
    "ToolExpectation",
    "ToolInvocation",
    "ToolSelectionScorer",
    "ToolTranscript",
    "TrajectoryScorer",
    "tool_metrics",
]

EXPECTATION_KEY = "tools"
"""The key under which a case declares the calls it expects."""

ERROR_NO_EXPECTATION = "NO_EXPECTATION"
"""The case declared no tool expectation, so its trajectory cannot be judged."""

STOPPED_ANSWERED = "answered"
"""The model stopped calling tools and produced a final turn."""

STOPPED_STEP_LIMIT = "step_limit"
"""The model was still calling tools when the case's step budget ran out."""

STOPPED_PROVIDER_ERROR = "provider_error"
"""The provider failed mid-trajectory; the transcript is what happened before it did."""


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """One tool call the model requested, and what the harness did with it.

    Attributes:
        step: The assistant turn this call came from, from 1.
        name: The tool the model asked for, exactly as it asked.
        arguments: The parsed arguments. Empty for a call whose argument text would not parse —
            which is a distinct failure from a call with no arguments, and ``arguments_parsed``
            is what separates them.
        call_id: The provider's identifier for this call.
        known_tool: Whether ``name`` is a tool that was actually offered. ``False`` is the
            catalog's *hallucinated tool*.
        arguments_parsed: Whether the provider could parse the argument text at all.
        arguments_valid: Whether the parsed arguments satisfy the tool's declared schema.
        executed: Whether the harness ran the tool. ``False`` for a hallucinated tool or invalid
            arguments — the harness answers the model with an error instead, which is what gives
            the recovery suite something to recover from.
        ok: Whether the tool returned a result rather than an error.
        error_code: The mock tool's stable error code, or ``None``.
        result_digest: A short, stable summary of what the tool returned, for the drill-down.
    """

    step: int
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""
    known_tool: bool = True
    arguments_parsed: bool = True
    arguments_valid: bool = True
    executed: bool = True
    ok: bool = True
    error_code: str | None = None
    result_digest: str = ""

    @property
    def signature(self) -> tuple[str, str]:
        """``(name, canonical arguments)`` — the identity a repeated call repeats."""
        items = sorted((str(key), repr(value)) for key, value in self.arguments.items())
        return self.name, ";".join(f"{key}={value}" for key, value in items)

    def as_json(self) -> dict[str, Any]:
        """Render for storage in a sample's ``result_json``."""
        return {
            "step": self.step,
            "name": self.name,
            "arguments": dict(self.arguments),
            "call_id": self.call_id,
            "known_tool": self.known_tool,
            "arguments_parsed": self.arguments_parsed,
            "arguments_valid": self.arguments_valid,
            "executed": self.executed,
            "ok": self.ok,
            "error_code": self.error_code,
            "result_digest": self.result_digest,
        }


@dataclass(frozen=True, slots=True)
class ToolTranscript:
    """Everything one case's interaction produced.

    Attributes:
        calls: Every invocation, in the order the model asked for it.
        final_text: The model's last non-tool turn — the answer, where there is one.
        steps: How many assistant turns the interaction took.
        stopped: Why it ended: :data:`STOPPED_ANSWERED`, :data:`STOPPED_STEP_LIMIT` or
            :data:`STOPPED_PROVIDER_ERROR`.
        offered_tools: The tool names that were actually offered, so "hallucinated" is decided
            against what the model was given rather than against the whole toolbox.
        error_code: The provider's error code when the interaction ended in failure.
    """

    calls: tuple[ToolInvocation, ...] = ()
    final_text: str = ""
    steps: int = 0
    stopped: str = STOPPED_ANSWERED
    offered_tools: tuple[str, ...] = ()
    error_code: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Render for storage in a sample's ``result_json``."""
        return {
            "calls": [call.as_json() for call in self.calls],
            "steps": self.steps,
            "stopped": self.stopped,
            "offered_tools": list(self.offered_tools),
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class ExpectedCall:
    """One call a case says a correct trajectory makes.

    Attributes:
        name: The tool that should be called.
        arguments: The argument values that make the call *semantically* correct. Only the keys
            named here are compared: a case that cares about ``path`` should not fail a model for
            also passing a legitimate optional ``encoding``.
    """

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolExpectation:
    """What a case declares about its trajectory.

    Attributes:
        required_calls: The calls a correct trajectory makes.
        ordered: Whether ``required_calls`` must appear in that order. ``False`` for the parallel
            independent-tools scenario, where order carries no meaning and scoring it would
            penalise a correct answer.
        forbidden_tools: Tools that must not be called — the catalog's "tool unavailable" and
            "no tool required" scenarios, made explicit.
        no_tool_required: Whether the correct trajectory calls nothing at all.
    """

    required_calls: tuple[ExpectedCall, ...] = ()
    ordered: bool = True
    forbidden_tools: tuple[str, ...] = ()
    no_tool_required: bool = False

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> ToolExpectation:
        """Build from a case's ``expectation["tools"]`` declaration."""
        calls = tuple(
            ExpectedCall(
                name=str(entry.get("name", "")), arguments=dict(entry.get("arguments", {}))
            )
            for entry in body.get("required_calls", ())
            if isinstance(entry, dict)
        )
        return cls(
            required_calls=calls,
            ordered=bool(body.get("ordered", True)),
            forbidden_tools=tuple(str(name) for name in body.get("forbidden_tools", ())),
            no_tool_required=bool(body.get("no_tool_required", not calls)),
        )


@runtime_checkable
class TrajectoryScorer(Protocol):
    """A scorer that reads a whole interaction rather than one response string.

    Structurally distinct from :class:`~freeweight.domain.scoring.Scorer` on purpose: the run
    engine dispatches on this protocol, so a test whose scorer needs a transcript cannot be run
    through the single-call path and quietly scored on the final sentence alone.
    """

    @property
    def key(self) -> str:
        """The scorer's stable name, as written in a benchmark manifest's ``scorer`` field."""
        ...

    @property
    def method(self) -> ScoreMethod:
        """The ladder rung this scorer occupies."""
        ...

    def score_trajectory(self, case: BenchmarkCase, transcript: ToolTranscript) -> ScoreResult:
        """Score one interaction. Never raises; see :class:`~freeweight.domain.scoring.Scorer`."""
        ...


def tool_metrics(
    expectation: ToolExpectation, transcript: ToolTranscript, *, succeeded: bool
) -> dict[str, float]:
    """Compute benchmark catalog §3.6's per-sample figures from one transcript.

    Every figure is in ``0.0..1.0`` except ``calls_per_success``, which is a count. A figure whose
    denominator is empty is **omitted** rather than reported as zero, so aggregation excludes the
    sample from that metric instead of averaging in a number nobody measured.

    Args:
        expectation: What the case said a correct trajectory looks like.
        transcript: What the model actually did.
        succeeded: Whether the final answer was correct, decided by the case's own answer
            expectation. Passed in rather than derived here: "did it get the right answer" is an
            exact-match question and belongs to :class:`~freeweight.domain.scorers.exact` .

    Returns:
        The metrics, by key.
    """
    calls = transcript.calls
    required = expectation.required_calls
    required_names = [call.name for call in required]
    actual_names = [call.name for call in calls]
    offered = set(transcript.offered_tools)
    metrics: dict[str, float] = {"task_success": 1.0 if succeeded else 0.0}

    if required:
        matched = _match_required(required, calls)
        found = sum(1 for _, call in matched if call is not None)
        metrics["tool_selection_accuracy"] = found / len(required)
        metrics["missed_tool_rate"] = (len(required) - found) / len(required)
        metrics["multi_tool_sequence_accuracy"] = 1.0 if found == len(required) else 0.0
        semantic = sum(
            1
            for expected, call in matched
            if call is not None and _arguments_agree(expected.arguments, call.arguments)
        )
        metrics["argument_semantic_correctness"] = semantic / len(required)
        if expectation.ordered and len(required) > 1:
            metrics["ordering_accuracy"] = (
                1.0 if _is_subsequence(required_names, actual_names) else 0.0
            )
    elif expectation.no_tool_required:
        # The catalog's "no tool required" scenario. There is nothing to select, so selection
        # accuracy would have an empty denominator; what *is* measurable is whether the model
        # left the tools alone, and that is the unnecessary-call rate below.
        metrics["multi_tool_sequence_accuracy"] = 1.0 if not calls else 0.0

    if calls:
        wanted = set(required_names)
        metrics["unnecessary_call_rate"] = sum(
            1 for name in actual_names if name not in wanted
        ) / len(calls)
        metrics["hallucinated_tool_rate"] = sum(
            1 for call in calls if call.name not in offered
        ) / len(calls)
        metrics["argument_schema_validity"] = sum(
            1 for call in calls if call.arguments_parsed and call.arguments_valid
        ) / len(calls)
        seen: set[tuple[str, str]] = set()
        repeated = 0
        for call in calls:
            if call.signature in seen:
                repeated += 1
            seen.add(call.signature)
        metrics["repeated_identical_call_rate"] = repeated / len(calls)
        metrics["redundant_call_rate"] = max(0, len(calls) - len(required)) / len(calls)
        metrics["forbidden_call_rate"] = sum(
            1 for name in actual_names if name in set(expectation.forbidden_tools)
        ) / len(calls)
    else:
        # No calls at all: rates over calls have an empty denominator and are omitted, but "it
        # made no unnecessary calls" is a real observation and the one the no-tool scenario is
        # about.
        metrics["unnecessary_call_rate"] = 0.0
        metrics["hallucinated_tool_rate"] = 0.0
        metrics["forbidden_call_rate"] = 0.0

    if succeeded:
        metrics["calls_per_success"] = float(len(calls))
    return metrics


def _match_required(
    required: Sequence[ExpectedCall], calls: Sequence[ToolInvocation]
) -> list[tuple[ExpectedCall, ToolInvocation | None]]:
    """Pair each required call with the first unclaimed actual call of the same tool.

    First-unclaimed rather than best-fit: a greedy pairing is deterministic and explicable, and a
    scorer that searched for the most flattering assignment would make two runs of the same
    trajectory disagree whenever the search had a tie to break.

    Returns:
        One ``(expected, actual-or-None)`` pair per required call, in declaration order. A list of
        pairs rather than a mapping because :class:`ExpectedCall` carries an argument mapping and
        is therefore unhashable — and making it hashable would mean freezing the arguments into a
        shape a case file cannot express.
    """
    remaining = list(calls)
    matched: list[tuple[ExpectedCall, ToolInvocation | None]] = []
    for expected in required:
        found = next((call for call in remaining if call.name == expected.name), None)
        if found is not None:
            remaining.remove(found)
        matched.append((expected, found))
    return matched


def _arguments_agree(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    """Return whether every argument the case named appears in the call with that value.

    Only the named keys, and string comparison is exact after stripping: a case that says
    ``{"path": "src/app.py"}`` means that path, and a model that passed ``" src/app.py "`` passed
    the same path. Nothing else is normalized — lowercasing a path would pass a call that a real
    filesystem would reject.
    """
    for key, value in expected.items():
        if key not in actual:
            return False
        supplied = actual[key]
        if isinstance(value, str) and isinstance(supplied, str):
            if value.strip() != supplied.strip():
                return False
        elif supplied != value:
            return False
    return True


def _is_subsequence(required: Sequence[str], actual: Sequence[str]) -> bool:
    """Return whether ``required`` appears in ``actual`` in order, extra calls allowed."""
    iterator = iter(actual)
    return all(any(name == candidate for candidate in iterator) for name in required)


def _expectation_of(case: BenchmarkCase) -> ToolExpectation | None:
    """Return the case's tool expectation, or ``None`` when it declares none."""
    declared = case.expectation.get(EXPECTATION_KEY)
    return ToolExpectation.from_json(declared) if isinstance(declared, dict) else None


def _unmeasurable(case: BenchmarkCase, method: ScoreMethod) -> ScoreResult:
    """The result for a case that declares no trajectory to check."""
    return ScoreResult(
        score=None,
        method=method,
        detail={"case": case.case_id},
        error_code=ERROR_NO_EXPECTATION,
        error_text=(
            f"Case {case.case_id!r} declares no expectation[{EXPECTATION_KEY!r}], so its tool "
            "trajectory cannot be judged."
        ),
    )


def _answer(case: BenchmarkCase, transcript: ToolTranscript) -> tuple[bool, Mapping[str, Any]]:
    """Decide whether the final answer was correct, and return the evidence for that decision.

    The evidence travels onto the sample. That is what keeps the phase's named failure mode —
    *scoring a refusal as a failure of capability* — visible rather than merely avoided: a model
    that declined the task made no tool calls and scored zero on selection, and so did a model
    that had no idea which tool to use. The stored answer excerpt is the only thing that tells the
    two apart, and it is bounded by the exact-match scorer's own truncation.
    """
    verdict = ExactMatchScorer().score(case, transcript.final_text)
    return (verdict.score is not None and verdict.score > 0.0), verdict.detail


@dataclass(frozen=True, slots=True)
class ToolSelectionScorer:
    """Scores *which* tools were chosen: selection, misses, hallucinations, wasted calls.

    The headline ``score`` is tool-selection accuracy — the share of the case's required calls the
    model actually made — and for a "no tool required" case it is ``1.0`` exactly when nothing was
    called. Argument correctness is deliberately **not** in the headline: a model that picked the
    right tool and passed the wrong path has a different problem from one that picked the wrong
    tool, and :class:`ToolArgumentScorer` is the instrument for the second.
    """

    key: str = "tool_selection"
    method: ScoreMethod = ScoreMethod.RULE

    def score_trajectory(self, case: BenchmarkCase, transcript: ToolTranscript) -> ScoreResult:
        """Score one trajectory's tool choices. See :class:`TrajectoryScorer`."""
        expectation = _expectation_of(case)
        if expectation is None:
            return _unmeasurable(case, self.method)
        succeeded, answer = _answer(case, transcript)
        metrics = tool_metrics(expectation, transcript, succeeded=succeeded)
        headline = metrics.get(
            "tool_selection_accuracy", metrics.get("multi_tool_sequence_accuracy", 0.0)
        )
        return ScoreResult(
            score=headline,
            method=self.method,
            detail={
                "case": case.case_id,
                **metrics,
                "answer": dict(answer),
                "transcript": transcript.as_json(),
            },
        )


@dataclass(frozen=True, slots=True)
class ToolArgumentScorer:
    """Scores *how* the tools were called: schema validity and semantic correctness.

    The headline ``score`` is semantic argument correctness — the share of required calls made
    with the argument values the case named. Schema validity travels beside it, because "the
    arguments were well-formed but wrong" and "the arguments would not parse" are different
    defects and a single number would hide which one happened.

    A case whose trajectory made no calls at all leaves the headline unscoreable rather than zero:
    there were no arguments to get right, and a zero would read as "it passed bad arguments".
    """

    key: str = "tool_arguments"
    method: ScoreMethod = ScoreMethod.RULE

    def score_trajectory(self, case: BenchmarkCase, transcript: ToolTranscript) -> ScoreResult:
        """Score one trajectory's arguments. See :class:`TrajectoryScorer`."""
        expectation = _expectation_of(case)
        if expectation is None:
            return _unmeasurable(case, self.method)
        succeeded, answer = _answer(case, transcript)
        metrics = tool_metrics(expectation, transcript, succeeded=succeeded)
        detail: dict[str, Any] = {
            "case": case.case_id,
            **metrics,
            "answer": dict(answer),
            "transcript": transcript.as_json(),
        }
        headline = metrics.get("argument_semantic_correctness")
        if headline is None:
            return ScoreResult(
                score=None,
                method=self.method,
                detail=detail,
                error_code="NO_CALLS_EXPECTED",
                error_text=(
                    f"Case {case.case_id!r} requires no tool call, so it has no arguments to "
                    "score. Its trajectory is measured by the selection scorer instead."
                ),
            )
        return ScoreResult(score=headline, method=self.method, detail=detail)
