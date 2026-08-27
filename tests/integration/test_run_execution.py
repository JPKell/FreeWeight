"""Integration tests for a complete run against ``FakeProvider`` (development plan, Phase 5).

Four of the phase's test-list items are here:

* "A deliberately failing sample does not fail the test; a failing test does not fail the run."
* "Cancellation in ``queued``, ``preparing``, ``warming`` and ``running``, each leaving consistent
  data."
* "Failed sample stored with ``score = NULL`` and an error, excluded from aggregates, visible in
  counts."
* The whole-machine path: queued → completed with raw samples stored.

Cancellation in ``preparing``, ``warming`` and ``running`` needs the cancel to arrive *while* the
executor is in that phase, which no single-threaded test can arrange by luck. It is arranged by
:class:`_CancellingProvider`, a provider whose first (or nth) generation cancels the run before
answering — the cancel therefore lands exactly where the test wants it, and the executor's boundary
check is what has to notice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from modelrack.testing import FakeFailure, FakeFailureMode, FakeGeneration, FakeScript
from tests.conftest import RunEnvironment

from freeweight.config import ExecutionSettings
from freeweight.domain.run_state import RunStatus
from freeweight.infrastructure.db.repositories.runs import RunRepository
from freeweight.services.events import read_events
from freeweight.services.runs import (
    ExecutionConfig,
    cancel_run,
    create_run,
    execute_run,
    get_run,
    list_samples,
)
from freeweight.services.scheduler import RunScheduler

_ECHO_CASES = 5
"""``native.echo`` ships five cases: three in ``echo.short``, two in ``echo.long``."""


def _queue(environment: RunEnvironment, *, repetitions: int = 1) -> str:
    summary = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=ExecutionConfig.resolve(
            ExecutionSettings(warmup_repetitions=0, cooldown_seconds=0),
            measured_repetitions=repetitions,
        ),
    )
    return summary.id


def _run_to_completion(environment: RunEnvironment) -> str:
    scheduler = RunScheduler(
        environment.database, environment.provider, registry=environment.registry
    )
    run_id = scheduler.run_once()
    assert run_id is not None
    return run_id


def _all_samples(environment: RunEnvironment, run_id: str) -> list[Any]:
    detail = get_run(environment.database, run_id)
    return [
        sample for test in detail.tests for sample in list_samples(environment.database, test.id)
    ]


@pytest.fixture
def environment(run_environment: Callable[..., RunEnvironment]) -> RunEnvironment:
    return run_environment()


class TestHappyPath:
    def test_a_run_completes_and_stores_every_raw_sample(self, environment: RunEnvironment) -> None:
        run_id = _queue(environment, repetitions=2)
        assert _run_to_completion(environment) == run_id
        detail = get_run(environment.database, run_id)
        assert detail.run.status == RunStatus.COMPLETED.value
        assert detail.run.started_at is not None
        assert detail.run.completed_at is not None
        assert len(_all_samples(environment, run_id)) == _ECHO_CASES * 2

    def test_every_sample_records_its_response_hash_and_timing(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queue(environment)
        _run_to_completion(environment)
        for sample in _all_samples(environment, run_id):
            assert sample.status == "completed"
            assert sample.response_hash is not None
            assert sample.score == 1.0
            assert sample.score_method == "rule"
            assert sample.client_wall_ms is not None

    def test_response_text_is_hashed_but_not_stored_by_default(
        self, environment: RunEnvironment
    ) -> None:
        """Spec §14: prompts and responses are hashes by default, full text only on request."""
        run_id = _queue(environment)
        _run_to_completion(environment)
        for sample in _all_samples(environment, run_id):
            assert sample.response_text is None
            assert sample.response_hash.startswith("sha256:")

    def test_store_responses_keeps_the_text(self, environment: RunEnvironment) -> None:
        summary = create_run(
            environment.database,
            environment.provider,
            environment.collector,
            environment.registry,
            model_ref=environment.model_ref,
            suite_key="native.echo",
            execution=ExecutionConfig.resolve(
                ExecutionSettings(warmup_repetitions=0, cooldown_seconds=0),
                measured_repetitions=1,
                store_responses=True,
            ),
        )
        _run_to_completion(environment)
        for sample in _all_samples(environment, summary.id):
            assert sample.response_text

    def test_aggregates_carry_the_sample_and_exclusion_counts(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queue(environment, repetitions=2)
        _run_to_completion(environment)
        detail = get_run(environment.database, run_id)
        run_level = [m for m in detail.metrics if m.run_test_id is None]
        assert len(run_level) == 1
        assert run_level[0].metric_key == "harness_roundtrip_success"
        assert run_level[0].numeric_value == pytest.approx(1.0)
        assert run_level[0].sample_count == _ECHO_CASES * 2
        assert run_level[0].excluded_count == 0
        assert run_level[0].unavailable_reason is None

    def test_the_event_stream_covers_the_whole_run(self, environment: RunEnvironment) -> None:
        run_id = _queue(environment)
        _run_to_completion(environment)
        types = [event.event_type for event in read_events(environment.database, run_id, limit=500)]
        assert types[0] == "run.started"
        assert types[-1] == "run.completed"
        assert types.count("test.started") == 2
        assert types.count("test.completed") == 2
        assert types.count("sample.completed") == _ECHO_CASES

    def test_the_run_records_its_effective_config(self, environment: RunEnvironment) -> None:
        run_id = _queue(environment, repetitions=3)
        detail = get_run(environment.database, run_id)
        assert detail.effective_config.measured_repetitions == 3
        assert detail.run.reproducibility_fingerprint.startswith("sha256:")


class TestFailureContainment:
    def test_a_failing_sample_is_stored_null_excluded_and_still_counted(
        self, environment_with_failures: RunEnvironment
    ) -> None:
        """The phase's own words: ``score = NULL`` and an error, excluded, visible in counts."""
        environment = environment_with_failures
        run_id = _queue(environment)
        _run_to_completion(environment)

        samples = _all_samples(environment, run_id)
        failed = [sample for sample in samples if sample.status != "completed"]
        assert failed, "the scripted provider was supposed to fail at least one generation"
        for sample in failed:
            assert sample.score is None
            assert sample.error_code
            assert sample.score != 0.0

        detail = get_run(environment.database, run_id)
        run_level = next(m for m in detail.metrics if m.run_test_id is None)
        assert run_level.excluded_count == len(failed)
        assert run_level.sample_count == len(samples) - len(failed)

    def test_a_failing_sample_does_not_fail_its_test_or_the_run(
        self, environment_with_failures: RunEnvironment
    ) -> None:
        environment = environment_with_failures
        run_id = _queue(environment)
        _run_to_completion(environment)
        detail = get_run(environment.database, run_id)
        assert detail.run.status == RunStatus.COMPLETED.value
        assert {test.status for test in detail.tests} == {"completed"}

    def test_a_failing_test_does_not_fail_the_run(self, environment: RunEnvironment) -> None:
        """A scorer that raises fails its sample; a test-level fault fails only its test.

        The fault is injected at the *test* level — a benchmark whose ``cases()`` raises — because
        that is the only thing in the engine that can genuinely fail a whole test rather than one
        sample, and the assertion that matters is that the run still reaches ``completed``.
        """
        registry = _registry_with_one_broken_test()
        summary = create_run(
            environment.database,
            environment.provider,
            environment.collector,
            registry,
            model_ref=environment.model_ref,
            suite_key="native.echo",
            execution=ExecutionConfig.resolve(
                ExecutionSettings(warmup_repetitions=0, cooldown_seconds=0), measured_repetitions=1
            ),
        )
        scheduler = RunScheduler(environment.database, environment.provider, registry=registry)
        scheduler.run_once()

        detail = get_run(environment.database, summary.id)
        assert detail.run.status == RunStatus.COMPLETED.value
        statuses = {test.test_key: test.status for test in detail.tests}
        assert statuses["echo.short"] == "failed"
        assert statuses["echo.long"] == "completed"

    def test_a_scorer_that_raises_fails_only_its_sample(self, environment: RunEnvironment) -> None:
        registry = _registry_with_a_raising_scorer()
        summary = create_run(
            environment.database,
            environment.provider,
            environment.collector,
            registry,
            model_ref=environment.model_ref,
            suite_key="native.echo",
            execution=ExecutionConfig.resolve(
                ExecutionSettings(warmup_repetitions=0, cooldown_seconds=0), measured_repetitions=1
            ),
        )
        RunScheduler(environment.database, environment.provider, registry=registry).run_once()
        detail = get_run(environment.database, summary.id)
        assert detail.run.status == RunStatus.COMPLETED.value
        short = next(test for test in detail.tests if test.test_key == "echo.short")
        samples = list_samples(environment.database, short.id)
        assert {sample.status for sample in samples} == {"failed"}
        assert {sample.error_code for sample in samples} == {"SCORER_ERROR"}
        assert all(sample.score is None for sample in samples)


class TestCancellation:
    def test_cancelling_a_queued_run_cancels_it_outright(self, environment: RunEnvironment) -> None:
        run_id = _queue(environment)
        summary = cancel_run(environment.database, environment.publisher, run_id)
        assert summary.status == RunStatus.CANCELLED.value
        assert summary.completed_at is not None
        # Nothing was executed, so the scheduler must not claim it afterwards.
        assert (
            RunScheduler(
                environment.database, environment.provider, registry=environment.registry
            ).run_once()
            is None
        )
        assert _all_samples(environment, run_id) == []

    @pytest.mark.parametrize("phase", ["preparing", "warming", "running"])
    def test_cancelling_mid_flight_leaves_consistent_data(
        self, run_environment: Callable[..., RunEnvironment], phase: str
    ) -> None:
        """Cancel while the executor is in each phase, and check the data it left behind.

        ``preparing`` and ``warming`` are reached by cancelling from a provider hook that fires on
        the warm-up call; ``running`` by cancelling from the third measured generation. In all
        three the run must end ``cancelled``, no test may be left ``running``, and every sample
        that was written must be complete rather than half-written.
        """
        environment = run_environment()
        cancel_on_call = {"preparing": 0, "warming": 0, "running": 3}[phase]
        provider = _CancellingProvider(
            environment.provider,
            environment=environment,
            cancel_on_call=cancel_on_call,
        )
        warmups = 0 if phase == "running" else 1
        summary = create_run(
            environment.database,
            provider,
            environment.collector,
            environment.registry,
            model_ref=environment.model_ref,
            suite_key="native.echo",
            execution=ExecutionConfig.resolve(
                ExecutionSettings(warmup_repetitions=warmups, cooldown_seconds=0),
                measured_repetitions=2,
            ),
        )
        provider.run_id = summary.id
        RunScheduler(environment.database, provider, registry=environment.registry).run_once()

        detail = get_run(environment.database, summary.id)
        assert detail.run.status == RunStatus.CANCELLED.value
        assert detail.run.completed_at is not None
        assert all(test.status in {"cancelled", "completed", "failed"} for test in detail.tests)
        for sample in _all_samples(environment, summary.id):
            assert sample.status in {"completed", "failed", "timeout"}
            assert sample.case_id
        types = [
            event.event_type for event in read_events(environment.database, summary.id, limit=500)
        ]
        assert types[-1] == "run.cancelled"

    def test_cancelling_a_terminal_run_is_refused(self, environment: RunEnvironment) -> None:
        from freeweight.domain.run_state import RunNotCancellable

        run_id = _queue(environment)
        _run_to_completion(environment)
        with pytest.raises(RunNotCancellable) as caught:
            cancel_run(environment.database, environment.publisher, run_id)
        assert caught.value.code == "RUN_NOT_CANCELLABLE"

    def test_cancelling_twice_while_cancelling_is_a_no_op(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queue(environment)
        with environment.database.write() as session:
            RunRepository().set_status(session, run_id, status=RunStatus.RUNNING.value)
        first = cancel_run(environment.database, environment.publisher, run_id)
        second = cancel_run(environment.database, environment.publisher, run_id)
        assert first.status == RunStatus.CANCELLING.value
        assert second.status == RunStatus.CANCELLING.value

    def test_a_cancelled_run_never_writes_aggregates(self, environment: RunEnvironment) -> None:
        """Aggregation is after the last cancellation check, so a cancelled run has no metrics."""
        run_id = _queue(environment)
        with environment.database.write() as session:
            RunRepository().set_status(session, run_id, status=RunStatus.PREPARING.value)
        cancel_run(environment.database, environment.publisher, run_id)
        execute_run(
            environment.database,
            environment.provider,
            environment.registry,
            environment.publisher,
            run_id,
        )
        assert get_run(environment.database, run_id).metrics == ()


# --------------------------------------------------------------------------------------------
# Fixtures and doubles
# --------------------------------------------------------------------------------------------


@pytest.fixture
def environment_with_failures(
    run_environment: Callable[..., RunEnvironment],
) -> RunEnvironment:
    """A provider whose second generation times out and whose fourth is unreachable.

    ``repeat_final_generation=False`` is deliberately *not* used: the script cycles is not what is
    under test, and a run needs more generations than the script has entries. The final entry
    repeats, so every later call succeeds and the run has both failed and successful samples in
    it — which is the mix the aggregate has to handle.
    """
    return run_environment(
        script=FakeScript(
            generations=(
                FakeGeneration(),
                FakeGeneration(failure=FakeFailure(mode=FakeFailureMode.TIMEOUT)),
                FakeGeneration(),
                FakeGeneration(failure=FakeFailure(mode=FakeFailureMode.UNAVAILABLE)),
                FakeGeneration(),
            )
        )
    )


def _registry_with_one_broken_test() -> Any:
    """A registry whose ``echo.short`` test raises when its cases are enumerated for execution.

    The break is in ``cases()`` rather than in a scorer because a scorer failure is contained at
    the *sample* level by design, and this test is about the level above it.
    """
    from dataclasses import dataclass, replace

    from freeweight.benchmarks.echo.benchmark import EchoTest, build
    from freeweight.domain.benchmark import BenchmarkRegistry

    benchmark = build()

    @dataclass(frozen=True, slots=True)
    class _BrokenTest(EchoTest):
        calls: list[int]

        def cases(self) -> Any:
            # The first call (test preparation, which counts cases) must succeed so the run has a
            # test row to fail; the second (execution) is the one that breaks.
            self.calls.append(1)
            if len(self.calls) > 1:
                raise RuntimeError("this test cannot enumerate its cases")
            return iter(tuple(EchoTest.cases(self)))

    original_short, original_long = benchmark.tests
    broken = _BrokenTest(
        key=original_short.key,
        name=original_short.name,
        category=original_short.category,
        prompts=original_short.prompts,
        calls=[],
    )

    @dataclass(frozen=True, slots=True)
    class _Benchmark:
        manifest: Any
        tests: Any

    return BenchmarkRegistry(
        [_Benchmark(manifest=benchmark.manifest, tests=(broken, replace(original_long)))]
    )


def _registry_with_a_raising_scorer() -> Any:
    """A registry whose ``echo.short`` scorer raises on every sample."""
    from dataclasses import dataclass, replace

    from freeweight.benchmarks.echo.benchmark import EchoTest, build
    from freeweight.domain.benchmark import BenchmarkRegistry
    from freeweight.domain.scoring import ScoreMethod

    benchmark = build()

    @dataclass(frozen=True, slots=True)
    class _RaisingScorer:
        key: str = "always_raises"
        method: ScoreMethod = ScoreMethod.RULE

        def score(self, case: Any, response_text: str) -> Any:
            raise RuntimeError("this scorer is broken")

    @dataclass(frozen=True, slots=True)
    class _BrokenScorerTest(EchoTest):
        @property
        def scorer(self) -> Any:
            return _RaisingScorer()

    original_short, original_long = benchmark.tests

    @dataclass(frozen=True, slots=True)
    class _Benchmark:
        manifest: Any
        tests: Any

    return BenchmarkRegistry(
        [
            _Benchmark(
                manifest=benchmark.manifest,
                tests=(
                    _BrokenScorerTest(
                        key=original_short.key,
                        name=original_short.name,
                        category=original_short.category,
                        prompts=original_short.prompts,
                    ),
                    replace(original_long),
                ),
            )
        ]
    )


class _CancellingProvider:
    """Wraps a provider and cancels the run from inside the nth ``generate`` call.

    That is how a cancellation is placed in a *specific* execution phase without a second thread
    and without a sleep: the cancel is written to the database at the exact moment the executor is
    between two of its own boundary checks, which is the hardest case for it and the one the
    phase's risk note ("a run stuck in ``cancelling``") is about.

    Args:
        inner: The real provider to delegate to.
        environment: Supplies the database handle and publisher the cancel is written through.
        cancel_on_call: The 0-based call index that triggers the cancel.
    """

    def __init__(self, inner: Any, *, environment: RunEnvironment, cancel_on_call: int) -> None:
        self._inner = inner
        self._environment = environment
        self._cancel_on_call = cancel_on_call
        self._calls = 0
        self.run_id: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def generate(self, request: Any) -> Any:
        if self._calls == self._cancel_on_call and self.run_id is not None:
            cancel_run(self._environment.database, self._environment.publisher, self.run_id)
        self._calls += 1
        return self._inner.generate(request)


class TestScoreResultRefusesADishonestRow:
    """:class:`~freeweight.domain.scoring.ScoreResult`'s own guards, at the level below the engine.

    They live beside the engine's failure-containment tests because they are the same rule stated
    twice: a sample either has a measurement or has a recorded reason for not having one, and
    never a zero standing in for the second (ADR-0016). The engine writes whatever a scorer
    returns, so the guard has to be in the value object rather than in the writer.
    """

    def test_a_scored_result_may_not_also_carry_an_error(self) -> None:
        from freeweight.domain.scoring import ScoreMethod, ScoreResult

        with pytest.raises(ValueError, match="must not carry an error code"):
            ScoreResult(score=1.0, method=ScoreMethod.RULE, error_code="BOOM")

    def test_an_unscored_result_must_say_why(self) -> None:
        from freeweight.domain.scoring import ScoreMethod, ScoreResult

        with pytest.raises(ValueError, match="must carry an error_code"):
            ScoreResult(score=None, method=ScoreMethod.JUDGE)

    @pytest.mark.parametrize("score", [-0.01, 1.01, 42.0])
    def test_a_score_outside_zero_to_one_is_refused(self, score: float) -> None:
        from freeweight.domain.scoring import ScoreMethod, ScoreResult

        with pytest.raises(ValueError, match=r"within 0\.0\.\.1\.0"):
            ScoreResult(score=score, method=ScoreMethod.RULE)

    def test_zero_is_a_measurement_and_is_accepted(self) -> None:
        """``0.0`` means "measured, and wrong"; it is ``None`` that means "not measured"."""
        from freeweight.domain.scoring import ScoreMethod, ScoreResult

        assert ScoreResult(score=0.0, method=ScoreMethod.RULE).score == 0.0

    def test_the_echo_scorer_scores_an_empty_response_zero_not_null(self) -> None:
        from freeweight.benchmarks.echo.benchmark import HarnessRoundTripScorer
        from freeweight.domain.benchmark import BenchmarkCase

        case = BenchmarkCase(case_id="c1", ordinal=0, prompt="p", expectation={"marker": "c1"})
        verdict = HarnessRoundTripScorer().score(case, "   ")
        assert verdict.score == 0.0
        assert verdict.error_code is None
