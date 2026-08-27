"""Unit tests for the run and test state machines (development plan, Phase 5).

The phase's test list asks for three things: "every legal transition; every illegal transition
rejected; terminal states immutable". All three are properties of *every* ordered pair of statuses,
so these tests enumerate the full Cartesian product rather than sampling it — 81 run pairs and 36
test pairs — and check each against
:data:`~freeweight.domain.run_state.RUN_TRANSITIONS`. A table nobody enumerates is a table with an
untested cell in it.
"""

from __future__ import annotations

import itertools

import pytest

from freeweight.domain.run_state import (
    CANCELLABLE_RUN_STATUSES,
    RUN_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    TERMINAL_TEST_STATUSES,
    TEST_TRANSITIONS,
    IllegalTransition,
    RunNotCancellable,
    RunStatus,
    cancellation_target,
    is_legal_run_transition,
    is_legal_test_transition,
    require_run_transition,
    require_test_transition,
)

# Imported under an alias: pytest tries to *collect* any module-level name starting with "Test",
# and warns that it cannot instantiate this one. The domain name is correct — data model §3 calls
# these the test states — so the accommodation belongs here, in the test module, not in the domain.
from freeweight.domain.run_state import TestStatus as RunTestStatus


class TestTableShape:
    def test_every_run_status_has_an_entry(self) -> None:
        assert set(RUN_TRANSITIONS) == set(RunStatus)

    def test_every_test_status_has_an_entry(self) -> None:
        assert set(TEST_TRANSITIONS) == set(RunTestStatus)

    def test_no_transition_points_outside_the_vocabulary(self) -> None:
        for run_targets in RUN_TRANSITIONS.values():
            assert run_targets <= set(RunStatus)
        for test_targets in TEST_TRANSITIONS.values():
            assert test_targets <= set(RunTestStatus)

    def test_terminal_sets_match_the_table(self) -> None:
        assert TERMINAL_RUN_STATUSES == {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
        assert TERMINAL_TEST_STATUSES == {
            RunTestStatus.COMPLETED,
            RunTestStatus.FAILED,
            RunTestStatus.SKIPPED,
            RunTestStatus.CANCELLED,
        }

    def test_the_documented_run_diagram_is_the_table(self) -> None:
        """Every edge data model §3 draws is present, spelled out here independently of the table.

        Written as a literal list rather than derived from ``RUN_TRANSITIONS`` on purpose: a test
        that reads the table it is checking proves only that the table equals itself.
        """
        documented = {
            (RunStatus.QUEUED, RunStatus.PREPARING),
            (RunStatus.QUEUED, RunStatus.CANCELLED),
            (RunStatus.PREPARING, RunStatus.WARMING),
            (RunStatus.PREPARING, RunStatus.FAILED),
            (RunStatus.PREPARING, RunStatus.CANCELLED),
            (RunStatus.WARMING, RunStatus.RUNNING),
            (RunStatus.WARMING, RunStatus.CANCELLED),
            (RunStatus.RUNNING, RunStatus.COMPLETED),
            (RunStatus.RUNNING, RunStatus.FAILED),
            (RunStatus.RUNNING, RunStatus.CANCELLING),
            (RunStatus.RUNNING, RunStatus.INTERRUPTED),
            (RunStatus.CANCELLING, RunStatus.CANCELLED),
            (RunStatus.INTERRUPTED, RunStatus.QUEUED),
        }
        for source, target in documented:
            assert is_legal_run_transition(source, target), f"{source} -> {target} must be legal"


class TestEveryOrderedPair:
    @pytest.mark.parametrize(
        ("source", "target"), list(itertools.product(list(RunStatus), list(RunStatus)))
    )
    def test_run_pair_agrees_with_the_table(self, source: RunStatus, target: RunStatus) -> None:
        legal = target in RUN_TRANSITIONS[source]
        assert is_legal_run_transition(source, target) is legal
        if legal:
            require_run_transition(source, target)
        else:
            with pytest.raises(IllegalTransition) as caught:
                require_run_transition(source, target)
            assert caught.value.details == {"from": source.value, "to": target.value}
            assert caught.value.code == "CONFLICT"

    @pytest.mark.parametrize(
        ("source", "target"), list(itertools.product(list(RunTestStatus), list(RunTestStatus)))
    )
    def test_test_pair_agrees_with_the_table(
        self, source: RunTestStatus, target: RunTestStatus
    ) -> None:
        legal = target in TEST_TRANSITIONS[source]
        assert is_legal_test_transition(source, target) is legal
        if legal:
            require_test_transition(source, target)
        else:
            with pytest.raises(IllegalTransition):
                require_test_transition(source, target)

    @pytest.mark.parametrize("status", sorted(RunStatus))
    def test_no_status_transitions_to_itself(self, status: RunStatus) -> None:
        assert not is_legal_run_transition(status, status)


class TestTerminalStatesAreImmutable:
    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (source, target)
            for source in sorted(TERMINAL_RUN_STATUSES)
            for target in sorted(RunStatus)
        ],
    )
    def test_no_run_leaves_a_terminal_state(self, source: RunStatus, target: RunStatus) -> None:
        assert not is_legal_run_transition(source, target)
        with pytest.raises(IllegalTransition, match="is terminal"):
            require_run_transition(source, target)

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (source, target)
            for source in sorted(TERMINAL_TEST_STATUSES)
            for target in sorted(RunTestStatus)
        ],
    )
    def test_no_test_leaves_a_terminal_state(
        self, source: RunTestStatus, target: RunTestStatus
    ) -> None:
        assert not is_legal_test_transition(source, target)
        with pytest.raises(IllegalTransition, match="is terminal"):
            require_test_transition(source, target)

    def test_interrupted_is_not_terminal_because_it_resumes(self) -> None:
        assert RunStatus.INTERRUPTED not in TERMINAL_RUN_STATUSES
        assert is_legal_run_transition(RunStatus.INTERRUPTED, RunStatus.QUEUED)


class TestCancellationTarget:
    @pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.PREPARING, RunStatus.WARMING])
    def test_a_run_before_execution_is_cancelled_outright(self, status: RunStatus) -> None:
        assert cancellation_target(status) is RunStatus.CANCELLED
        require_run_transition(status, RunStatus.CANCELLED)

    def test_a_running_run_enters_cancelling_first(self) -> None:
        assert cancellation_target(RunStatus.RUNNING) is RunStatus.CANCELLING
        require_run_transition(RunStatus.RUNNING, RunStatus.CANCELLING)
        require_run_transition(RunStatus.CANCELLING, RunStatus.CANCELLED)

    @pytest.mark.parametrize(
        "status",
        sorted(set(RunStatus) - CANCELLABLE_RUN_STATUSES),
    )
    def test_a_non_cancellable_status_is_refused_with_its_own_code(self, status: RunStatus) -> None:
        with pytest.raises(RunNotCancellable) as caught:
            cancellation_target(status)
        assert caught.value.code == "RUN_NOT_CANCELLABLE"
        assert caught.value.details == {"status": status.value}

    def test_every_cancellable_status_has_a_legal_target(self) -> None:
        for status in sorted(CANCELLABLE_RUN_STATUSES):
            require_run_transition(status, cancellation_target(status))
