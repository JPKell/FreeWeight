"""Integration tests for kill-mid-run recovery and resume (development plan, Phase 5).

The phase's test-list item, in full: "Kill the process mid-run: on restart the run is
``interrupted``, completed tests are retained, and resume continues from the right place."

"Kill the process" is simulated the only way a test can and still assert anything afterwards: the
executor is stopped inside a generation call by an exception the run engine does not catch, the
database handle is closed as a dying process's would be, and a *new* handle — a new "process" — is
opened over the same file and asked to recover. Everything the real failure would leave behind is
therefore left behind: committed samples, an in-flight ``run_tests`` row, no terminal event.

The two things that make this more than "the status column changed" are asserted explicitly: the
samples written before the kill are still there afterwards, and the resumed run does not measure
any of them a second time — checked by comparing sample ids across the resume, not just counts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from tests.conftest import RunEnvironment

from freeweight.config import ExecutionSettings
from freeweight.domain.run_state import RunStatus
from freeweight.services.database import Database
from freeweight.services.events import RunEventPublisher, read_events
from freeweight.services.runs import (
    ExecutionConfig,
    build_registry,
    create_run,
    get_run,
    list_samples,
    resume_run,
)
from freeweight.services.scheduler import RunScheduler

_ECHO_SAMPLES = 5
"""``native.echo`` at one repetition: three ``echo.short`` cases and two ``echo.long`` ones."""


class _ProcessKilled(BaseException):
    """Stands in for the process going away, and it must be unhandleable to mean that.

    A ``BaseException`` rather than an ``Exception`` on purpose. The run engine contains every
    ``Exception`` deliberately — a failed sample does not fail its test, a failed test does not
    fail its run — so an ``Exception`` here would exercise the *failure* path and end the run
    ``failed``, which is a different outcome with a different meaning (spec §13). A process that
    is killed handles nothing, and only a ``BaseException`` reproduces that. ``session_scope``
    still rolls the in-flight write back, exactly as it would for a real ``SIGINT``.
    """


class _DyingProvider:
    """Delegates to a real provider, then dies after ``die_after`` successful generations."""

    def __init__(self, inner: Any, *, die_after: int) -> None:
        self._inner = inner
        self._die_after = die_after
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def generate(self, request: Any) -> Any:
        if self.calls >= self._die_after:
            raise _ProcessKilled
        self.calls += 1
        return self._inner.generate(request)


@pytest.fixture
def environment(run_environment: Callable[..., RunEnvironment]) -> RunEnvironment:
    return run_environment()


def _queue(environment: RunEnvironment) -> str:
    summary = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=ExecutionConfig.resolve(
            ExecutionSettings(warmup_repetitions=0, randomize_case_order=False),
            measured_repetitions=1,
        ),
    )
    return summary.id


def _sample_ids(database: Database, run_id: str) -> set[str]:
    detail = get_run(database, run_id)
    return {sample.id for test in detail.tests for sample in list_samples(database, test.id)}


def _kill_mid_run(environment: RunEnvironment, run_id: str, *, after: int) -> None:
    """Execute the run until ``after`` samples are stored, then stop as a killed process would."""
    provider = _DyingProvider(environment.provider, die_after=after)
    scheduler = RunScheduler(environment.database, provider, registry=environment.registry)
    with pytest.raises(_ProcessKilled):
        scheduler.run_once()
    assert provider.calls == after


class TestRecoveryAtStartup:
    def test_a_killed_run_is_interrupted_not_failed(self, environment: RunEnvironment) -> None:
        run_id = _queue(environment)
        _kill_mid_run(environment, run_id, after=2)
        assert get_run(environment.database, run_id).run.status == RunStatus.RUNNING.value

        environment.database.close()
        with Database.from_url(environment.database_url) as restarted:
            RunScheduler(restarted, environment.provider, registry=build_registry()).recover()
            assert get_run(restarted, run_id).run.status == RunStatus.INTERRUPTED.value

    def test_completed_samples_survive_the_kill(self, environment: RunEnvironment) -> None:
        run_id = _queue(environment)
        _kill_mid_run(environment, run_id, after=2)
        before = _sample_ids(environment.database, run_id)
        assert len(before) == 2

        environment.database.close()
        with Database.from_url(environment.database_url) as restarted:
            RunScheduler(restarted, environment.provider, registry=build_registry()).recover()
            assert _sample_ids(restarted, run_id) == before

    def test_the_interruption_is_visible_on_the_event_stream(
        self, environment: RunEnvironment
    ) -> None:
        """A browser reconnecting after the crash must be told why, not left in silence."""
        run_id = _queue(environment)
        _kill_mid_run(environment, run_id, after=2)
        environment.database.close()
        with Database.from_url(environment.database_url) as restarted:
            RunScheduler(restarted, environment.provider, registry=build_registry()).recover()
            types = [event.event_type for event in read_events(restarted, run_id, limit=500)]
        assert types[-1] == "run.interrupted"

    def test_a_kill_before_the_first_sample_still_recovers(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queue(environment)
        _kill_mid_run(environment, run_id, after=0)
        environment.database.close()
        with Database.from_url(environment.database_url) as restarted:
            RunScheduler(restarted, environment.provider, registry=build_registry()).recover()
            detail = get_run(restarted, run_id)
        assert detail.run.status == RunStatus.INTERRUPTED.value
        assert detail.metrics == ()


class TestResume:
    def test_resume_requeues_and_finishes_the_run(self, environment: RunEnvironment) -> None:
        run_id = _queue(environment)
        _kill_mid_run(environment, run_id, after=2)
        environment.database.close()

        with Database.from_url(environment.database_url) as restarted:
            scheduler = RunScheduler(restarted, environment.provider, registry=build_registry())
            scheduler.recover()
            resumed = resume_run(restarted, RunEventPublisher(restarted), run_id)
            assert resumed.status == RunStatus.QUEUED.value

            assert scheduler.run_once() == run_id
            detail = get_run(restarted, run_id)
            assert detail.run.status == RunStatus.COMPLETED.value
            assert {test.status for test in detail.tests} == {"completed"}

    def test_resume_does_not_re_measure_a_stored_sample(self, environment: RunEnvironment) -> None:
        """ "Continues from the right place": every pre-kill sample id is still there, unchanged.

        Comparing ids rather than counts is what makes this test mean something — a resume that
        deleted the old samples and measured everything again would produce the same *count* and
        the wrong data.
        """
        run_id = _queue(environment)
        _kill_mid_run(environment, run_id, after=2)
        before = _sample_ids(environment.database, run_id)
        environment.database.close()

        with Database.from_url(environment.database_url) as restarted:
            scheduler = RunScheduler(restarted, environment.provider, registry=build_registry())
            scheduler.recover()
            resume_run(restarted, RunEventPublisher(restarted), run_id)
            scheduler.run_once()

            after = _sample_ids(restarted, run_id)
            assert before <= after
            assert len(after) == _ECHO_SAMPLES

    def test_a_test_completed_before_the_kill_is_not_run_again(
        self, environment: RunEnvironment
    ) -> None:
        """Kill after ``echo.short``'s three cases: that test is done and stays done."""
        run_id = _queue(environment)
        _kill_mid_run(environment, run_id, after=3)
        environment.database.close()

        with Database.from_url(environment.database_url) as restarted:
            detail_before = get_run(restarted, run_id)
            short_before = next(
                test for test in detail_before.tests if test.test_key == "echo.short"
            )
            assert short_before.status == "completed"
            short_samples = {sample.id for sample in list_samples(restarted, short_before.id)}

            scheduler = RunScheduler(restarted, environment.provider, registry=build_registry())
            scheduler.recover()
            resume_run(restarted, RunEventPublisher(restarted), run_id)
            scheduler.run_once()

            detail_after = get_run(restarted, run_id)
            short_after = next(test for test in detail_after.tests if test.test_key == "echo.short")
            assert short_after.id == short_before.id
            assert {
                sample.id for sample in list_samples(restarted, short_after.id)
            } == short_samples

    def test_the_resumed_run_aggregates_over_everything(self, environment: RunEnvironment) -> None:
        run_id = _queue(environment)
        _kill_mid_run(environment, run_id, after=2)
        environment.database.close()

        with Database.from_url(environment.database_url) as restarted:
            scheduler = RunScheduler(restarted, environment.provider, registry=build_registry())
            scheduler.recover()
            resume_run(restarted, RunEventPublisher(restarted), run_id)
            scheduler.run_once()

            run_level = [
                metric
                for metric in get_run(restarted, run_id).metrics
                if metric.run_test_id is None
            ]
            assert len(run_level) == 1
            assert run_level[0].sample_count == _ECHO_SAMPLES
            assert run_level[0].excluded_count == 0

    def test_the_event_sequence_continues_across_the_restart(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queue(environment)
        _kill_mid_run(environment, run_id, after=2)
        before = read_events(environment.database, run_id, limit=500)
        environment.database.close()

        with Database.from_url(environment.database_url) as restarted:
            scheduler = RunScheduler(restarted, environment.provider, registry=build_registry())
            scheduler.recover()
            resume_run(restarted, RunEventPublisher(restarted), run_id)
            scheduler.run_once()
            after = read_events(restarted, run_id, limit=500)

        assert [event.sequence for event in after] == list(range(1, len(after) + 1))
        assert [event.event_id for event in after[: len(before)]] == [
            event.event_id for event in before
        ]

    def test_resuming_a_run_that_is_not_interrupted_is_refused(
        self, environment: RunEnvironment
    ) -> None:
        from freeweight.domain.run_state import IllegalTransition

        run_id = _queue(environment)
        with pytest.raises(IllegalTransition):
            resume_run(environment.database, environment.publisher, run_id)
