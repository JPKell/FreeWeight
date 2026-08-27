"""The tool scorers, and tool metrics for every scenario class benchmark catalog §3.6 names.

The phase asks for exactly this: "tool metrics computed correctly for every scenario class,
including 'no tool required' and 'hallucinated tool'". :class:`TestEveryScenarioClass` is that
list, one test per row of the catalog.

The other thing under test here is *absence*. A rate whose denominator is empty — ordering for a
one-call case, calls-per-success for a failed one — must be missing from the detail rather than
reported as zero, because aggregation excludes a missing figure and averages a zero (ADR-0016).
"""

from __future__ import annotations

import pytest

from freeweight.domain.benchmark import BenchmarkCase
from freeweight.domain.scorers.tools import (
    ExpectedCall,
    ToolArgumentScorer,
    ToolExpectation,
    ToolInvocation,
    ToolSelectionScorer,
    ToolTranscript,
    TrajectoryScorer,
    tool_metrics,
)

_OFFERED = ("get_inventory", "lookup_record", "calculator", "search_symbol", "read_file")


def _case(tools: dict[str, object], answer: str = "12") -> BenchmarkCase:
    return BenchmarkCase(
        case_id="case-1",
        ordinal=0,
        prompt="p",
        expectation={"tools": tools, "exact": {"any_of": [answer], "contains": True}},
    )


def _call(name: str, **arguments: object) -> ToolInvocation:
    return ToolInvocation(step=1, name=name, arguments=arguments, known_tool=name in _OFFERED)


def _transcript(*calls: ToolInvocation, final_text: str = "12", steps: int = 2) -> ToolTranscript:
    return ToolTranscript(calls=calls, final_text=final_text, steps=steps, offered_tools=_OFFERED)


class TestEveryScenarioClass:
    """One assertion per benchmark catalog §3.6 scenario."""

    def test_one_correct_tool(self) -> None:
        expectation = ToolExpectation((ExpectedCall("get_inventory", {"sku": "A1"}),))
        metrics = tool_metrics(
            expectation, _transcript(_call("get_inventory", sku="A1")), succeeded=True
        )
        assert metrics["tool_selection_accuracy"] == 1.0
        assert metrics["argument_semantic_correctness"] == 1.0
        assert metrics["unnecessary_call_rate"] == 0.0
        assert metrics["calls_per_success"] == 1.0

    def test_several_similar_tools_penalises_the_wrong_choice(self) -> None:
        expectation = ToolExpectation((ExpectedCall("lookup_record", {"record_id": "CUST-1002"}),))
        metrics = tool_metrics(
            expectation, _transcript(_call("get_inventory", sku="A1")), succeeded=False
        )
        assert metrics["tool_selection_accuracy"] == 0.0
        assert metrics["missed_tool_rate"] == 1.0
        assert metrics["unnecessary_call_rate"] == 1.0
        assert "calls_per_success" not in metrics, "a failed case has no success to divide by"

    def test_no_tool_required_and_none_called(self) -> None:
        expectation = ToolExpectation(no_tool_required=True)
        metrics = tool_metrics(expectation, _transcript(final_text="7"), succeeded=True)
        assert metrics["multi_tool_sequence_accuracy"] == 1.0
        assert metrics["unnecessary_call_rate"] == 0.0
        assert "tool_selection_accuracy" not in metrics, (
            "there is nothing to select, so the rate would have an empty denominator"
        )

    def test_no_tool_required_but_one_was_called(self) -> None:
        expectation = ToolExpectation(no_tool_required=True)
        metrics = tool_metrics(expectation, _transcript(_call("calculator")), succeeded=True)
        assert metrics["multi_tool_sequence_accuracy"] == 0.0
        assert metrics["unnecessary_call_rate"] == 1.0

    def test_tool_required_and_missed(self) -> None:
        expectation = ToolExpectation((ExpectedCall("get_inventory", {"sku": "A1"}),))
        metrics = tool_metrics(expectation, _transcript(final_text="I cannot"), succeeded=False)
        assert metrics["missed_tool_rate"] == 1.0
        assert metrics["multi_tool_sequence_accuracy"] == 0.0
        assert metrics["hallucinated_tool_rate"] == 0.0

    def test_sequential_tools_in_order_and_out_of_order(self) -> None:
        expectation = ToolExpectation(
            (ExpectedCall("search_symbol"), ExpectedCall("read_file")), ordered=True
        )
        in_order = tool_metrics(
            expectation, _transcript(_call("search_symbol"), _call("read_file")), succeeded=True
        )
        reversed_order = tool_metrics(
            expectation, _transcript(_call("read_file"), _call("search_symbol")), succeeded=True
        )
        assert in_order["ordering_accuracy"] == 1.0
        assert reversed_order["ordering_accuracy"] == 0.0
        assert reversed_order["multi_tool_sequence_accuracy"] == 1.0, (
            "both calls were made; only their order was wrong, and the two are separate figures"
        )

    def test_parallel_independent_tools_are_not_scored_on_order(self) -> None:
        expectation = ToolExpectation(
            (ExpectedCall("get_inventory"), ExpectedCall("lookup_record")), ordered=False
        )
        metrics = tool_metrics(
            expectation, _transcript(_call("lookup_record"), _call("get_inventory")), succeeded=True
        )
        assert "ordering_accuracy" not in metrics
        assert metrics["multi_tool_sequence_accuracy"] == 1.0

    def test_invalid_argument(self) -> None:
        expectation = ToolExpectation((ExpectedCall("calculator", {"expression": "3*(4+5)"}),))
        malformed = ToolInvocation(
            step=1, name="calculator", arguments={}, arguments_parsed=False, arguments_valid=False
        )
        metrics = tool_metrics(expectation, _transcript(malformed), succeeded=False)
        assert metrics["argument_schema_validity"] == 0.0
        assert metrics["argument_semantic_correctness"] == 0.0
        assert metrics["tool_selection_accuracy"] == 1.0, "the right tool with the wrong arguments"

    def test_tool_failure_does_not_change_selection_accuracy(self) -> None:
        expectation = ToolExpectation((ExpectedCall("get_inventory", {"sku": "A1"}),))
        failed = ToolInvocation(
            step=1,
            name="get_inventory",
            arguments={"sku": "A1"},
            ok=False,
            error_code="TIMEOUT",
        )
        metrics = tool_metrics(expectation, _transcript(failed), succeeded=False)
        assert metrics["tool_selection_accuracy"] == 1.0

    def test_hallucinated_tool(self) -> None:
        expectation = ToolExpectation((ExpectedCall("get_inventory", {"sku": "A1"}),))
        metrics = tool_metrics(
            expectation, _transcript(_call("query_warehouse", sku="A1")), succeeded=False
        )
        assert metrics["hallucinated_tool_rate"] == 1.0
        assert metrics["tool_selection_accuracy"] == 0.0
        assert metrics["unnecessary_call_rate"] == 1.0

    def test_tool_unavailable_is_measured_by_what_was_not_called(self) -> None:
        expectation = ToolExpectation(no_tool_required=True, forbidden_tools=("read_file",))
        clean = tool_metrics(expectation, _transcript(final_text="UNAVAILABLE"), succeeded=True)
        reached = tool_metrics(expectation, _transcript(_call("read_file")), succeeded=False)
        assert clean["forbidden_call_rate"] == 0.0
        assert reached["forbidden_call_rate"] == 1.0

    def test_repeated_and_redundant_calls_are_different_figures(self) -> None:
        expectation = ToolExpectation((ExpectedCall("get_inventory", {"sku": "A1"}),))
        transcript = _transcript(
            _call("get_inventory", sku="A1"),
            _call("get_inventory", sku="A1"),
            _call("get_inventory", sku="B2"),
        )
        metrics = tool_metrics(expectation, transcript, succeeded=True)
        assert metrics["repeated_identical_call_rate"] == pytest.approx(1 / 3)
        assert metrics["redundant_call_rate"] == pytest.approx(2 / 3)


class TestToolSelectionScorer:
    """Known-pass, known-fail, boundary, malformed and missing."""

    def test_it_is_a_trajectory_scorer(self) -> None:
        # The run engine dispatches on this protocol; a scorer that failed the check would be
        # handed the final sentence and would silently score the wrong thing.
        assert isinstance(ToolSelectionScorer(), TrajectoryScorer)

    def test_known_pass(self) -> None:
        case = _case({"required_calls": [{"name": "get_inventory", "arguments": {"sku": "A1"}}]})
        verdict = ToolSelectionScorer().score_trajectory(
            case, _transcript(_call("get_inventory", sku="A1"))
        )
        assert verdict.score == 1.0
        assert verdict.detail["task_success"] == 1.0

    def test_known_fail(self) -> None:
        case = _case({"required_calls": [{"name": "get_inventory", "arguments": {"sku": "A1"}}]})
        verdict = ToolSelectionScorer().score_trajectory(case, _transcript(_call("calculator")))
        assert verdict.score == 0.0

    def test_a_malformed_trajectory_still_scores(self) -> None:
        # "Malformed model response" for a trajectory scorer is a trajectory that never produced
        # an answer at all — the provider failed, or the step budget ran out. It is a zero, not an
        # absence: the model was asked and did not deliver.
        case = _case({"required_calls": [{"name": "get_inventory"}]})
        broken = ToolTranscript(
            calls=(), final_text="", stopped="provider_error", offered_tools=_OFFERED
        )
        assert ToolSelectionScorer().score_trajectory(case, broken).score == 0.0

    def test_missing_data_is_unscoreable(self) -> None:
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="p")
        verdict = ToolSelectionScorer().score_trajectory(case, _transcript())
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"

    def test_the_transcript_travels_with_the_score(self) -> None:
        case = _case({"required_calls": [{"name": "get_inventory", "arguments": {"sku": "A1"}}]})
        detail = (
            ToolSelectionScorer()
            .score_trajectory(case, _transcript(_call("get_inventory", sku="A1")))
            .detail
        )
        assert detail["transcript"]["calls"][0]["name"] == "get_inventory"


class TestToolArgumentScorer:
    """Its headline is semantic argument correctness, and it refuses to invent one."""

    def test_known_pass_and_fail(self) -> None:
        case = _case({"required_calls": [{"name": "get_inventory", "arguments": {"sku": "A1"}}]})
        right = ToolArgumentScorer().score_trajectory(
            case, _transcript(_call("get_inventory", sku="A1"))
        )
        wrong = ToolArgumentScorer().score_trajectory(
            case, _transcript(_call("get_inventory", sku="B2"))
        )
        assert (right.score, wrong.score) == (1.0, 0.0)

    def test_surrounding_whitespace_in_a_string_argument_is_not_a_failure(self) -> None:
        case = _case({"required_calls": [{"name": "read_file", "arguments": {"path": "a.py"}}]})
        verdict = ToolArgumentScorer().score_trajectory(
            case, _transcript(_call("read_file", path=" a.py "))
        )
        assert verdict.score == 1.0

    def test_only_the_named_arguments_are_compared(self) -> None:
        case = _case(
            {"required_calls": [{"name": "database_query", "arguments": {"table": "orders"}}]}
        )
        verdict = ToolArgumentScorer().score_trajectory(
            case, _transcript(_call("database_query", table="orders", field="sku", value="B2"))
        )
        assert verdict.score == 1.0

    def test_a_case_requiring_no_call_has_no_arguments_to_score(self) -> None:
        case = _case({"required_calls": [], "no_tool_required": True}, answer="7")
        verdict = ToolArgumentScorer().score_trajectory(case, _transcript(final_text="7"))
        assert verdict.score is None
        assert verdict.error_code == "NO_CALLS_EXPECTED"

    def test_missing_data_is_unscoreable(self) -> None:
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="p")
        assert ToolArgumentScorer().score_trajectory(case, _transcript()).score is None


class TestARefusalIsNotACapabilityFailure:
    """The phase's named failure mode: "scoring a refusal as a failure of capability".

    Nothing here reads meaning — the scorer cannot tell a refusal from confusion and must not try.
    What it can do is keep the evidence: the trajectory and the answer both travel onto the
    sample, so the two zero-scoring trajectories below are told apart by a person looking at one
    sample rather than by a heuristic guessing at intent.
    """

    def test_a_refusal_and_a_wrong_tool_both_score_zero_but_are_distinguishable(self) -> None:
        case = _case({"required_calls": [{"name": "get_inventory", "arguments": {"sku": "A1"}}]})
        refused = ToolSelectionScorer().score_trajectory(
            case, _transcript(final_text="I would rather not do that.")
        )
        confused = ToolSelectionScorer().score_trajectory(
            case, _transcript(_call("calculator", expression="1+1"), final_text="2")
        )
        assert refused.score == confused.score == 0.0
        assert refused.detail["transcript"]["calls"] == []
        assert confused.detail["transcript"]["calls"], "the wrong tool is on the record"
        assert refused.detail["answer"]["response_chars"] > 0, (
            "the refusal is on the record too, or nothing distinguishes the two"
        )

    def test_the_capability_gate_is_a_different_mechanism_entirely(self) -> None:
        # A model that *cannot* call tools never reaches a scorer: the run engine skips the test
        # with ``unsupported_capability`` before any sample exists
        # (tests/integration/test_quality_suites.py::TestCapabilityGating). A scorer therefore
        # never has to distinguish "will not" from "cannot" — only "will not" reaches it.
        case = _case({"required_calls": [{"name": "get_inventory"}]})
        verdict = ToolSelectionScorer().score_trajectory(case, _transcript(final_text="no"))
        assert verdict.score == 0.0
        assert verdict.error_code is None
