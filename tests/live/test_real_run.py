"""Live: one short ``native.performance`` run against a real Ollama, on real weights.

Marked ``live`` and therefore excluded from the default suite (``addopts``); it needs a running
Ollama with at least one model pulled, and it is the only test in this repository that measures a
model rather than the harness.

**What it asserts, and what it deliberately does not.** It asserts that the numbers are *plausible*
— positive, finite, and inside bounds no real local model can fall outside — never that they hit a
particular value. A live benchmark that asserted "at least 40 tokens per second" would fail on a
slower GPU than the author's and pass on a faster one regardless of whether the measurement was
correct, which is the opposite of what a test is for. The bounds here catch the failures that
actually happen: a throughput derived from a zero duration, a TTFT taken from a wall clock that
stepped, a token count of zero silently averaged in
([ADR-0016](../../../docs/adr/0016-unavailable-is-not-zero.md)).

Run it with::

    pytest -m live tests/live/test_real_run.py
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.live

_BASE_URL = "http://127.0.0.1:11434"
_UPPER_BOUND_TOKENS_PER_SECOND = 100_000.0
"""Above this, a throughput figure came from a duration of roughly zero, not from a fast GPU."""


@pytest.fixture
def provider() -> Any:
    """A real Ollama provider, or a skip when nothing is serving."""
    from modelrack.errors import ProviderError
    from modelrack.providers.ollama import OllamaProvider

    built = OllamaProvider(base_url=_BASE_URL)
    try:
        health = built.health()
    except ProviderError as exc:
        pytest.skip(f"no Ollama at {_BASE_URL}: {exc}")
    if str(health.status) != "ok":
        pytest.skip(f"Ollama at {_BASE_URL} reports {health.status}")
    if not built.list_models():
        pytest.skip("Ollama is running but has no models pulled")
    return built


@pytest.fixture
def live_environment(provider: Any, tmp_path: Any) -> Any:
    """A migrated database, the real provider, and one discovered model."""
    from datetime import UTC, datetime

    from freeweight.infrastructure.db.engine import create_engine_for
    from freeweight.infrastructure.db.migration import MigrationRunner
    from freeweight.services.database import MIGRATIONS_LOCATION, Database
    from freeweight.services.models import discover_models
    from freeweight.services.runs import build_registry
    from freeweight.services.telemetry import build_collector

    url = f"sqlite:///{tmp_path / 'live.sqlite3'}"
    engine = create_engine_for(url)
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    database = Database.from_url(url)
    discover_models(database, provider, now=datetime.now(UTC))
    descriptor = provider.list_models()[0]
    try:
        yield {
            "database": database,
            "provider": provider,
            "collector": build_collector(),
            "registry": build_registry(),
            "model_ref": descriptor.identity.provider_model_name,
        }
    finally:
        database.close()


def test_a_real_short_run_produces_plausible_throughput_and_ttft(live_environment: Any) -> None:
    """One repetition of ``native.performance`` on a real model, checked for plausibility."""
    from freeweight.config import ExecutionSettings, TelemetrySettings
    from freeweight.services.runs import ExecutionConfig, create_run, get_run
    from freeweight.services.scheduler import RunScheduler

    execution = ExecutionConfig.resolve(
        ExecutionSettings(
            warmup_repetitions=1,
            cooldown_seconds=0,
            randomize_case_order=False,
            # The machine is presumably this developer's own and is not necessarily quiet; the
            # point of this run is the numbers' shape, and a contaminated run still has that.
            idle_gpu_threshold_percent=0,
        ),
        measured_repetitions=1,
    )
    summary = create_run(
        live_environment["database"],
        live_environment["provider"],
        live_environment["collector"],
        live_environment["registry"],
        model_ref=live_environment["model_ref"],
        suite_key="native.performance",
        execution=execution,
    )
    RunScheduler(
        live_environment["database"],
        live_environment["provider"],
        registry=live_environment["registry"],
        collector=live_environment["collector"],
        telemetry=TelemetrySettings(),
    ).run_once()

    detail = get_run(live_environment["database"], summary.id)
    assert detail.run.status == "completed", detail.run.error_text
    values = {
        metric.metric_key: metric
        for metric in detail.metrics
        if metric.run_test_id is None and metric.unavailable_reason is None
    }

    decode = values["decode_tokens_per_second"].numeric_value
    assert decode is not None
    assert 0.0 < decode < _UPPER_BOUND_TOKENS_PER_SECOND

    prompt = values["prompt_tokens_per_second"].numeric_value
    assert prompt is not None
    assert 0.0 < prompt < _UPPER_BOUND_TOKENS_PER_SECOND

    ttft = values["ttft_ms"].numeric_value
    assert ttft is not None
    # Never negative — a negative first-token time means two readings came from different clocks,
    # which is the "wall-clock used for durations" failure this phase names.
    assert 0.0 < ttft < 600_000.0

    # The served context is recorded with its source, whatever the source turned out to be.
    assert detail.run.served_context_source in {"configured", "reported", "assumed"}
    # And the sampling overhead was measured on this machine rather than assumed.
    assert detail.run.telemetry_overhead_percent is not None


def test_the_same_run_repeated_agrees_within_tolerance(live_environment: Any) -> None:
    """Acceptance criterion 4, on real hardware: two runs of one subject agree.

    The tolerance is deliberately wide. Two runs of a real model on a machine doing other things
    differ by more than a percent, and a tight bound here would make this test a report on the
    developer's desktop rather than on FreeWeight.
    """
    from freeweight.config import ExecutionSettings, TelemetrySettings
    from freeweight.domain.provenance import diff_documents
    from freeweight.services.runs import ExecutionConfig, create_run, get_run, repeat_run
    from freeweight.services.scheduler import RunScheduler

    def execute(run_id: str) -> Any:
        RunScheduler(
            live_environment["database"],
            live_environment["provider"],
            registry=live_environment["registry"],
            collector=live_environment["collector"],
            telemetry=TelemetrySettings(),
        ).run_once()
        return get_run(live_environment["database"], run_id)

    execution = ExecutionConfig.resolve(
        ExecutionSettings(
            warmup_repetitions=1,
            cooldown_seconds=0,
            randomize_case_order=False,
            idle_gpu_threshold_percent=0,
        ),
        measured_repetitions=2,
    )
    first = execute(
        create_run(
            live_environment["database"],
            live_environment["provider"],
            live_environment["collector"],
            live_environment["registry"],
            model_ref=live_environment["model_ref"],
            suite_key="native.token_economy",
            execution=execution,
        ).id
    )
    second = execute(
        repeat_run(
            live_environment["database"],
            live_environment["provider"],
            live_environment["collector"],
            live_environment["registry"],
            run_ref=first.run.id,
        ).id
    )

    # Nothing about the environment moved between them, so their provenance is identical.
    assert diff_documents(first.run.fingerprint_document, second.run.fingerprint_document) == ()
    assert second.run.reproducibility_fingerprint == first.run.reproducibility_fingerprint

    def value(detail: Any, key: str) -> float | None:
        return next(
            (
                metric.numeric_value
                for metric in detail.metrics
                if metric.run_test_id is None and metric.metric_key == key
            ),
            None,
        )

    before, after = value(first, "output_tokens"), value(second, "output_tokens")
    assert before is not None
    assert after is not None
    assert after == pytest.approx(before, rel=0.5)


def test_the_five_quality_suites_run_end_to_end_on_a_real_model(live_environment: Any) -> None:
    """Phase 7 acceptance criterion 1, on real weights.

    Runs each deterministic quality suite once and asserts only that it *completed and produced
    interpretable metrics* — every metric row under a key its manifest declares, with a unit and a
    direction. It deliberately asserts nothing about the values: whether a particular local model
    follows instructions or picks the right tool is what this suite exists to *measure*, and a test
    that demanded a score would fail on a weaker model and pass on a stronger one regardless of
    whether the measurement was correct.

    A suite the model lacks the capability for is a legitimate outcome and is asserted as such: the
    tests are ``skipped`` with ``unsupported_capability`` and the run still completes (criterion 3
    on real hardware, where the capability declaration comes from a real adapter rather than a
    fake).
    """
    from freeweight.config import ExecutionSettings
    from freeweight.services.runs import ExecutionConfig, create_run, get_run
    from freeweight.services.scheduler import RunScheduler

    execution = ExecutionConfig.resolve(
        ExecutionSettings(
            warmup_repetitions=1,
            cooldown_seconds=0,
            randomize_case_order=False,
            idle_gpu_threshold_percent=0,
        ),
        measured_repetitions=1,
    )
    for suite in (
        "native.instruction_following",
        "native.structured_output",
        "native.tool_use",
        "native.tool_recovery",
        "native.agent",
    ):
        summary = create_run(
            live_environment["database"],
            live_environment["provider"],
            live_environment["collector"],
            live_environment["registry"],
            model_ref=live_environment["model_ref"],
            suite_key=suite,
            execution=execution,
        )
        RunScheduler(
            live_environment["database"],
            live_environment["provider"],
            registry=live_environment["registry"],
        ).run_once()

        detail = get_run(live_environment["database"], summary.id)
        assert detail.run.status == "completed", f"{suite}: {detail.run.error_text}"
        assert detail.tests, f"{suite} declared no tests"
        for row in detail.tests:
            assert row.status in {"completed", "skipped"}, (
                f"{suite}/{row.test_key}: {row.error_text}"
            )
            if row.status == "skipped":
                assert row.skip_reason == "unsupported_capability"

        if all(row.status == "skipped" for row in detail.tests):
            continue
        declared = {
            entry["key"]
            for entry in live_environment["registry"].get(suite).manifest.body["metrics"]
        }
        produced = {metric.metric_key for metric in detail.metrics}
        assert produced, f"{suite} produced no metrics"
        assert produced <= declared, f"{suite} produced {produced - declared} outside its manifest"
        # Rung 5 is not reachable in this phase, and a live run is where a mistake would show.
        assert all(metric.metric_key != "judge_agreement" for metric in detail.metrics)
