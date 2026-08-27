"""Unit tests for the run scheduler (development plan, Phase 5).

Two of the phase's requirements live here: "one GPU workload at a time; queueing beyond that" —
tested as "a second run while one is active is queued, not run concurrently" — and the startup
recovery that makes a killed run ``interrupted`` rather than lost.

Every test drives :meth:`~freeweight.services.scheduler.RunScheduler.run_once` rather than the
background thread, so claim-and-execute is exercised deterministically. The thread itself is
covered by :class:`TestThreadLifecycle`, which asserts what only the thread can be wrong about:
that it starts, that it drains a queue, and that it stops.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

import pytest
from tests.conftest import RunEnvironment

from freeweight.config import ExecutionSettings
from freeweight.domain.run_state import RunStatus
from freeweight.infrastructure.db.repositories.runs import RunRepository
from freeweight.services.runs import ExecutionConfig, create_run, get_run
from freeweight.services.scheduler import RunScheduler


@pytest.fixture
def environment(run_environment: Callable[..., RunEnvironment]) -> RunEnvironment:
    return run_environment()


def _queue_run(environment: RunEnvironment, *, label: str | None = None) -> str:
    summary = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=ExecutionConfig.resolve(
            ExecutionSettings(cooldown_seconds=0), measured_repetitions=1
        ),
        label=label,
    )
    return summary.id


def _scheduler(environment: RunEnvironment) -> RunScheduler:
    return RunScheduler(
        environment.database,
        environment.provider,
        registry=environment.registry,
        poll_interval_seconds=0.01,
    )


def _status(environment: RunEnvironment, run_id: str) -> str:
    return get_run(environment.database, run_id).run.status


class TestClaiming:
    def test_an_empty_queue_claims_nothing(self, environment: RunEnvironment) -> None:
        assert _scheduler(environment).run_once() is None

    def test_a_queued_run_is_claimed_and_executed_to_completion(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queue_run(environment)
        assert _scheduler(environment).run_once() == run_id
        assert _status(environment, run_id) == RunStatus.COMPLETED.value

    def test_runs_are_claimed_oldest_first(self, environment: RunEnvironment) -> None:
        first = _queue_run(environment, label="first")
        second = _queue_run(environment, label="second")
        scheduler = _scheduler(environment)
        assert scheduler.run_once() == first
        assert scheduler.run_once() == second

    def test_a_claim_moves_the_run_to_preparing_and_stamps_started_at(
        self, environment: RunEnvironment
    ) -> None:
        """The claim itself, without the execution that normally follows it."""
        run_id = _queue_run(environment)
        with environment.database.write() as session:
            claimed = RunRepository().claim_next_queued(session, now=_now())
            assert claimed is not None
            assert claimed.id == run_id
            assert claimed.status == RunStatus.PREPARING.value
            assert claimed.started_at is not None

    def test_a_second_claimer_gets_nothing_while_a_run_is_in_flight(
        self, environment: RunEnvironment
    ) -> None:
        """ "One GPU workload at a time": the slot is taken, so the next run stays queued.

        This is what a ``freeweight run start`` typed while a server is serving must do — and it
        is checked at the claim, so it holds across processes, not only within one scheduler.
        """
        first = _queue_run(environment)
        second = _queue_run(environment)
        with environment.database.write() as session:
            RunRepository().claim_next_queued(session, now=_now())

        assert _scheduler(environment).run_once() is None
        assert _status(environment, first) == RunStatus.PREPARING.value
        assert _status(environment, second) == RunStatus.QUEUED.value

    def test_queue_depth_and_active_run_report_the_real_state(
        self, environment: RunEnvironment
    ) -> None:
        scheduler = _scheduler(environment)
        assert scheduler.queue_depth() == 0
        assert scheduler.active_run_id() is None
        first = _queue_run(environment)
        _queue_run(environment)
        assert scheduler.queue_depth() == 2
        with environment.database.write() as session:
            RunRepository().claim_next_queued(session, now=_now())
        assert scheduler.active_run_id() == first
        assert scheduler.queue_depth() == 1


class TestRecovery:
    def test_recovery_leaves_an_empty_database_alone(self, environment: RunEnvironment) -> None:
        outcome = _scheduler(environment).recover()
        assert outcome.interrupted_run_ids == ()
        assert outcome.queued_depth == 0

    def test_recovery_does_not_touch_a_queued_run(self, environment: RunEnvironment) -> None:
        run_id = _queue_run(environment)
        outcome = _scheduler(environment).recover()
        assert outcome.interrupted_run_ids == ()
        assert outcome.queued_depth == 1
        assert _status(environment, run_id) == RunStatus.QUEUED.value

    @pytest.mark.parametrize(
        "orphan_status",
        [
            RunStatus.PREPARING.value,
            RunStatus.WARMING.value,
            RunStatus.RUNNING.value,
            RunStatus.CANCELLING.value,
        ],
    )
    def test_every_in_flight_status_becomes_interrupted(
        self, environment: RunEnvironment, orphan_status: str
    ) -> None:
        run_id = _queue_run(environment)
        with environment.database.write() as session:
            RunRepository().set_status(session, run_id, status=orphan_status)
        outcome = _scheduler(environment).recover()
        assert outcome.interrupted_run_ids == (run_id,)
        assert _status(environment, run_id) == RunStatus.INTERRUPTED.value

    def test_recovery_emits_a_run_interrupted_event(self, environment: RunEnvironment) -> None:
        from freeweight.services.events import read_events

        run_id = _queue_run(environment)
        with environment.database.write() as session:
            RunRepository().set_status(session, run_id, status=RunStatus.RUNNING.value)
        _scheduler(environment).recover()
        types = [event.event_type for event in read_events(environment.database, run_id)]
        assert types == ["run.interrupted"]

    def test_recovery_does_not_touch_a_terminal_run(self, environment: RunEnvironment) -> None:
        run_id = _queue_run(environment)
        _scheduler(environment).run_once()
        assert _status(environment, run_id) == RunStatus.COMPLETED.value
        assert _scheduler(environment).recover().interrupted_run_ids == ()
        assert _status(environment, run_id) == RunStatus.COMPLETED.value


class TestThreadLifecycle:
    def test_the_thread_drains_the_queue_and_stops_cleanly(
        self, environment: RunEnvironment
    ) -> None:
        first = _queue_run(environment)
        second = _queue_run(environment)
        scheduler = _scheduler(environment)
        scheduler.start()
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if all(
                    _status(environment, run_id) == RunStatus.COMPLETED.value
                    for run_id in (first, second)
                ):
                    break
                time.sleep(0.02)
        finally:
            scheduler.stop(timeout=30)
        assert _status(environment, first) == RunStatus.COMPLETED.value
        assert _status(environment, second) == RunStatus.COMPLETED.value
        assert not scheduler.is_running()

    def test_the_two_runs_did_not_overlap(self, environment: RunEnvironment) -> None:
        """Sequential, not concurrent: the first run ends before the second one starts.

        Asserted on the stored timestamps rather than on the scheduler's own bookkeeping, because
        the timestamps are what a user (and every later comparison) actually reads.
        """
        first = _queue_run(environment)
        second = _queue_run(environment)
        scheduler = _scheduler(environment)
        assert scheduler.run_once() == first
        assert scheduler.run_once() == second
        first_detail = get_run(environment.database, first).run
        second_detail = get_run(environment.database, second).run
        assert first_detail.completed_at is not None
        assert second_detail.started_at is not None
        assert first_detail.completed_at <= second_detail.started_at

    def test_start_is_idempotent_and_stop_is_safe_without_start(
        self, environment: RunEnvironment
    ) -> None:
        scheduler = _scheduler(environment)
        scheduler.stop(timeout=1)
        scheduler.start()
        scheduler.start()
        try:
            assert scheduler.is_running()
        finally:
            scheduler.stop(timeout=30)
        assert not scheduler.is_running()


def _now() -> datetime:
    from baseaicore import utc_now

    return utc_now()
