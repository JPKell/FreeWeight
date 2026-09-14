"""ADR-0148 end to end on the fake provider: each rung served at itself, the gate, and the fit
becoming the context every later benchmark of the model runs at."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from baseaicore import RuntimeProfile
from modelrack import GenerationRequest, GenerationResult, ProviderUnavailable
from modelrack.testing import FakeProvider, FakeScript

from freeweight.config import ExecutionSettings
from freeweight.services.models import discover_models
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
    context_fit_margin_tokens: int = 4096,
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
        context_fit_margin_tokens=context_fit_margin_tokens,
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

    # ADR-0152: the measured 32 768 less one 4 096-token step.
    assert summary.served_context == 28672
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


class _Card(FakeProvider):
    """A fake whose card refuses to launch a server past ``limit`` tokens, as llama-server does."""

    def __init__(self, limit: int) -> None:
        model = dataclasses.replace(FakeScript().models[0], max_context=262_144)
        super().__init__(FakeScript(models=(model,)))
        self._limit = limit

    def generate(self, request: GenerationRequest) -> GenerationResult:
        if (request.runtime_profile.context_size or 0) > self._limit:
            message = "llama-server exited with code 1 before it became healthy"
            raise ProviderUnavailable(message)
        return super().generate(request)


def test_the_fit_is_refined_between_the_last_rung_that_served_and_the_first_refused(
    run_environment: Callable[..., Any],
) -> None:
    """ADR-0151: a card that holds 40 000 tokens is reported at 36 864, not at 32 768."""
    base = run_environment(registry=build_registry(max_fit_context_tokens=131_072))
    card = _Card(limit=40_000)
    discover_models(base.database, card, now=datetime.now(UTC))
    environment = dataclasses.replace(base, provider=card)

    detail = _complete(environment, "native.context_fit")

    test = detail.tests[0]
    outcomes = {s.case_id: s.status for s in list_samples(environment.database, test.id)}
    assert outcomes == {
        "fit-8192": "completed",
        "fit-16384": "completed",
        "fit-32768": "completed",
        "fit-65536": "failed",
        "fit-131072": "failed",
        "fit-49152": "failed",
        "fit-40960": "failed",
        "fit-36864": "completed",
    }
    assert test.total_cases == 8
    values = {m.metric_key: m.numeric_value for m in detail.metrics if m.run_test_id is None}
    assert values["max_successful_context_tokens"] == 36864
    assert values["max_context_capped_by_configuration"] == 0.0


def test_the_margin_below_the_fit_is_a_setting(
    environment: Any,  # noqa: ANN401 — a RunEnvironment
) -> None:
    """ADR-0153: ``benchmarks.context_fit_margin_tokens``; ``0`` serves the fit as measured."""
    _complete(environment, "native.context_fit")

    exact = _start(
        environment,
        "native.performance",
        require_context_fit=True,
        context_from_fit=True,
        context_fit_margin_tokens=0,
    )
    wider = _start(
        environment,
        "native.performance",
        require_context_fit=True,
        context_from_fit=True,
        context_fit_margin_tokens=8192,
    )

    assert exact.served_context == 32768
    assert wider.served_context == 24576
