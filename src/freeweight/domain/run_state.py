"""freeweight.domain.run_state — the run and test state machines as explicit transition tables.

[Data Model §3](../../../../docs/apps/freeweight/data-model.md) draws two state machines. This
module *is* those diagrams, as data: :data:`RUN_TRANSITIONS` and :data:`TEST_TRANSITIONS` are the
complete, normative sets of legal moves, and every write that changes a status goes through
:func:`require_run_transition` or :func:`require_test_transition` rather than assigning a column.

Written as a table rather than as ``if`` statements in the scheduler for one reason: a transition
table can be enumerated by a test. "Every legal transition; every illegal transition rejected;
terminal states immutable" (development plan, Phase 5) is a property of *all* 81 ordered pairs of
run statuses, and only a table lets a test iterate them. Scattered conditionals can only be tested
for the paths someone thought to write down.

Pure domain: stdlib and :mod:`baseaicore` only, no framework, no session, no clock.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from baseaicore import ConflictError

__all__ = [
    "CANCELLABLE_RUN_STATUSES",
    "RUN_TRANSITIONS",
    "TERMINAL_RUN_STATUSES",
    "TERMINAL_TEST_STATUSES",
    "TEST_TRANSITIONS",
    "IllegalTransition",
    "RunNotCancellable",
    "RunStatus",
    "TestStatus",
    "cancellation_target",
    "is_legal_run_transition",
    "is_legal_test_transition",
    "require_run_transition",
    "require_test_transition",
]


class RunStatus(StrEnum):
    """The nine states a run can be in.

    ``INTERRUPTED`` is not a failure: a run whose process died mid-flight keeps its completed
    tests and is resumable (spec §13, "a run that dies mid-flight is ``interrupted``, not
    ``failed``"). ``CANCELLING`` exists because cancelling a *running* run is a request, not an
    event — the scheduler honours it at its next boundary check, and until then the run is
    observably neither running-as-normal nor cancelled.
    """

    QUEUED = "queued"
    PREPARING = "preparing"
    WARMING = "warming"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TestStatus(StrEnum):
    """The six states one test within a run can be in.

    ``SKIPPED`` always carries a reason on the row (``run_tests.skip_reason``); a skip with no
    recorded reason is a defect, not a state (spec §13).
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.PREPARING, RunStatus.CANCELLED, RunStatus.INTERRUPTED}),
    RunStatus.PREPARING: frozenset(
        {RunStatus.WARMING, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}
    ),
    RunStatus.WARMING: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
            RunStatus.INTERRUPTED,
        }
    ),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.INTERRUPTED}),
    RunStatus.INTERRUPTED: frozenset({RunStatus.QUEUED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}
"""Every legal run transition, exactly as data model §3 draws it, plus two additions it implies.

The two additions are both ``→ INTERRUPTED``, and both are startup recovery rather than a move
any running process makes. Data model §3 draws only ``running → interrupted`` because that is the
overwhelmingly likely moment for a process to die, but a process killed while ``preparing``,
``warming`` or ``cancelling`` leaves a row in exactly the same orphaned condition, and recovery
must have a legal move for it. ``queued → interrupted`` is *not* among them: a queued run has
started nothing and is left queued by a restart, which is why recovery re-queues rather than
interrupts it.

``INTERRUPTED → QUEUED`` is resume. The three terminal states map to the empty set, which is what
makes "terminal states are immutable" checkable rather than merely stated.
"""

TEST_TRANSITIONS: dict[TestStatus, frozenset[TestStatus]] = {
    TestStatus.PENDING: frozenset({TestStatus.RUNNING, TestStatus.SKIPPED, TestStatus.CANCELLED}),
    TestStatus.RUNNING: frozenset({TestStatus.COMPLETED, TestStatus.FAILED, TestStatus.CANCELLED}),
    TestStatus.COMPLETED: frozenset(),
    TestStatus.FAILED: frozenset(),
    TestStatus.SKIPPED: frozenset(),
    TestStatus.CANCELLED: frozenset(),
}
"""Every legal test transition: ``pending → running → completed | failed | cancelled``.

``pending → skipped`` is the requires-check outcome (unsupported capability, missing dataset, …),
decided before the test runs; a test never becomes ``skipped`` after it has started. ``pending →
cancelled`` is a run cancelled before this test's turn came, which is the common case when a
multi-test run is cancelled early.
"""

TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    status for status, allowed in RUN_TRANSITIONS.items() if not allowed
)
"""Run statuses with no legal successor. Derived from the table, never restated beside it."""

TERMINAL_TEST_STATUSES: frozenset[TestStatus] = frozenset(
    status for status, allowed in TEST_TRANSITIONS.items() if not allowed
)
"""Test statuses with no legal successor."""

CANCELLABLE_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.QUEUED, RunStatus.PREPARING, RunStatus.WARMING, RunStatus.RUNNING}
)
"""The statuses from which a user's cancel request is accepted.

``CANCELLING`` is deliberately absent: a second cancel of a run already cancelling is not an
error the user needs, but it is also not a state change, so the service answers it as a no-op
rather than routing it through :func:`cancellation_target`. ``INTERRUPTED`` is absent because an
interrupted run is not running — there is nothing to stop, and its resumability is the point.
"""


class IllegalTransition(ConflictError):
    """A status change that the transition table does not permit.

    A :class:`~baseaicore.ConflictError` (HTTP 409) rather than a validation error: the request
    was well-formed, and it is the entity's current state that refuses it. Carries ``from`` and
    ``to`` in ``details`` so the API envelope shows which move was attempted.
    """


class RunNotCancellable(ConflictError):
    """A cancel request against a run whose status does not admit one.

    Its own class rather than an :class:`IllegalTransition` because the spec gives it its own
    stable error code (spec §13, ``RUN_NOT_CANCELLABLE``) and the API its own documented response
    (api.md §4, ``409``); a caller branching on ``code`` must not have to inspect a message to
    tell "this run is already finished" from "the scheduler tried an impossible move".
    """

    code: ClassVar[str] = "RUN_NOT_CANCELLABLE"


def is_legal_run_transition(current: RunStatus, target: RunStatus) -> bool:
    """Return whether ``current → target`` is a legal run transition.

    Args:
        current: The run's status now.
        target: The status being moved to.

    Returns:
        ``True`` if the table permits the move. A self-transition (``current == target``) is
        ``False`` for every status: re-declaring a status is not a transition, and treating it as
        one would let a terminal run be "moved" to its own terminal state, quietly making
        immutability untestable.
    """
    return target in RUN_TRANSITIONS[current]


def require_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Permit ``current → target`` or refuse it.

    Args:
        current: The run's status now.
        target: The status being moved to.

    Raises:
        IllegalTransition: The table does not permit the move. The message names the legal
            successors of ``current``, or says the state is terminal when there are none.
    """
    if is_legal_run_transition(current, target):
        return
    allowed = sorted(RUN_TRANSITIONS[current])
    reason = (
        f"{current.value!r} is terminal"
        if not allowed
        else f"legal next states are {[status.value for status in allowed]}"
    )
    raise IllegalTransition(
        f"A run cannot move from {current.value!r} to {target.value!r}: {reason}.",
        details={"from": current.value, "to": target.value},
    )


def is_legal_test_transition(current: TestStatus, target: TestStatus) -> bool:
    """Return whether ``current → target`` is a legal test transition."""
    return target in TEST_TRANSITIONS[current]


def require_test_transition(current: TestStatus, target: TestStatus) -> None:
    """Permit ``current → target`` for one test within a run, or refuse it.

    Raises:
        IllegalTransition: The table does not permit the move.
    """
    if is_legal_test_transition(current, target):
        return
    allowed = sorted(TEST_TRANSITIONS[current])
    reason = (
        f"{current.value!r} is terminal"
        if not allowed
        else f"legal next states are {[status.value for status in allowed]}"
    )
    raise IllegalTransition(
        f"A run test cannot move from {current.value!r} to {target.value!r}: {reason}.",
        details={"from": current.value, "to": target.value},
    )


def cancellation_target(current: RunStatus) -> RunStatus:
    """Return the status a cancel request moves a run in ``current`` to.

    A run that has not yet reached the provider is cancelled outright; a ``running`` run enters
    ``cancelling`` and is finished by the scheduler at its next boundary check, because a thread
    that is inside a generation call cannot be stopped by the thread handling the HTTP request.
    That asymmetry is why cancellation has a target function at all rather than one constant.

    Args:
        current: The run's status now.

    Returns:
        :data:`RunStatus.CANCELLING` for a running run, :data:`RunStatus.CANCELLED` otherwise.

    Raises:
        RunNotCancellable: ``current`` is not in :data:`CANCELLABLE_RUN_STATUSES`.
    """
    if current not in CANCELLABLE_RUN_STATUSES:
        raise RunNotCancellable(
            f"A run in {current.value!r} cannot be cancelled; cancellable states are "
            f"{sorted(status.value for status in CANCELLABLE_RUN_STATUSES)}.",
            details={"status": current.value},
        )
    return RunStatus.CANCELLING if current is RunStatus.RUNNING else RunStatus.CANCELLED
