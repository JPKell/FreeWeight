"""ADR-0148 end to end on the fake provider: each rung served at itself, the gate, and the fit
becoming the context every later benchmark of the model runs at."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from baseaicore import RuntimeProfile

from freeweight.config import ExecutionSettings
from freeweight.services.runs import (
    ContextFitRequired,
    ExecutionConfig,
    build_registry,
    create_run,
    get_run,
    list_samples,
)
from freeweight.services.scheduler import RunScheduler

_PROFILE = RuntimeProfile(context_size=8192)


def _start(
    environment: Any,  # noqa: ANN401 — a RunEnvironment
    suite: str,
    profile: RuntimeProfile = _PROFILE,
    *,
    require_context_fit: bool = False,
    context_from_fit: bool = False,
) -> Any:  # noqa: ANN401 — a RunSummary
    execution = ExecutionConfig.resolve(
        ExecutionSettings(
            warmup_repetitions=0,
            cooldown_seconds=0,
            idle_gpu_threshold_percent=0,
            randomize_case_order=False,
        ),
        measured_repetitions=1,
    )
    return create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key=suite,
        execution=execution,
        runtime_profile=profile,
        require_context_fit=require_context_fit,
        context_from_fit=context_from_fit,
    )


def _complete(environment: Any, suite: str) -> Any:  # noqa: ANN401 — a RunEnvironment, a RunDetail
    summary = _start(environment, suite)
    RunScheduler(
        environment.database, environment.provider, registry=environment.registry
    ).run_once()
    return get_run(environment.database, summary.id)


@pytest.fixture
def environment(run_environment: Callable[..., Any]) -> Any:  # noqa: ANN401 — a RunEnvironment
    """The fake advertises 32 768 tokens of trained context; a 65 536 ceiling goes one rung past."""
    return run_environment(registry=build_registry(max_fit_context_tokens=65_536))


def test_each_rung_is_served_at_itself_and_one_past_the_trained_context_is_skipped(
    environment: Any,  # noqa: ANN401 — a RunEnvironment
) -> None:
    detail = _complete(environment, "native.context_fit")

    assert detail.run.status == "completed"
    samples = list_samples(environment.database, detail.tests[0].id)
    # The run is served at 8 192: without each case's own context the fake would refuse the
    # 16 384- and 32 768-token rungs, exactly as llama-server did before ADR-0148.
    assert {sample.case_id: sample.status for sample in samples} == {
        "fit-8192": "completed",
        "fit-16384": "completed",
        "fit-32768": "completed",
        "fit-65536": "skipped",
    }
    values = {
        metric.metric_key: metric.numeric_value
        for metric in detail.metrics
        if metric.run_test_id is None
    }
    assert values["max_successful_context_tokens"] == 32768
    assert values["max_context_capped_by_configuration"] == 1.0


def test_no_other_suite_starts_before_the_fit_and_every_suite_runs_at_it_afterwards(
    environment: Any,  # noqa: ANN401 — a RunEnvironment
) -> None:
    with pytest.raises(ContextFitRequired):
        _start(environment, "native.performance", require_context_fit=True, context_from_fit=True)

    _complete(environment, "native.context_fit")
    summary = _start(
        environment, "native.performance", require_context_fit=True, context_from_fit=True
    )

    assert summary.served_context == 32768
    assert summary.served_context_source == "configured"


def test_an_explicit_context_wins_and_another_profile_needs_its_own_fit(
    environment: Any,  # noqa: ANN401 — a RunEnvironment
) -> None:
    _complete(environment, "native.context_fit")

    explicit = _start(environment, "native.performance", require_context_fit=True)
    assert explicit.served_context == 8192
    with pytest.raises(ContextFitRequired):
        _start(
            environment,
            "native.performance",
            RuntimeProfile(context_size=8192, keep_alive="1m"),
            require_context_fit=True,
        )
