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

LIVE_CONTEXT_TOKENS = 8192
"""The context every live run is served at, pinned explicitly.

**This exists because a machine was taken down by its absence.** Before ``--context-size``, a run
was served at whatever the provider chose from the model's advertised maximum — and a modern local
model advertises 128K-262K. On one 30 GiB / 16 GB machine a 15.7B model at a 112K slot asked for
21.9 GiB of CPU KV cache, 7.7 GiB of VRAM KV cache and a 5.4 GiB compute buffer, fell back to nine
of its twenty-eight layers on the GPU, dropped to 3.9 tokens/second, and took the display driver
and then the kernel with it. No OOM killer fired, because most of that allocation was mmap-backed.

Pinning it is now possible (ADR-0023 §4), so these tests pin it. 8192 is large enough that nothing
here truncates and small enough that the KV cache is a fraction of any model's weights, which makes
the binding constraint the weights alone — see :data:`MAX_MODEL_BYTES`."""

DISPLAY_RESERVE_BYTES = 2 * 1024**3
"""VRAM left to whatever else owns the card — a compositor, a browser, an editor.

The crash this guard exists to prevent happened on a machine whose GPU was also drawing a desktop.
A budget that assumed the whole card would have admitted the model that took it down."""

KV_ALLOWANCE_BYTES = 2 * 1024**3
"""VRAM allowed for the KV cache and compute buffers at :data:`LIVE_CONTEXT_TOKENS`.

Measured rather than guessed: on this suite's reference machine ``qwen3:8b`` costs about
157 KB per token of context, so 8192 tokens is ~1.3 GiB, and 2 GiB covers a model with a wider
KV geometry."""

FALLBACK_VRAM_BUDGET_BYTES = 6 * 1024**3
"""Weights budget when the machine's VRAM cannot be read at all.

Deliberately small. An unreadable card is not evidence of a large one, and the failure mode this
whole guard exists to avoid is discovering the limit by hitting it."""


def _weights_budget() -> int:
    """How many bytes of model weights this machine can hold at :data:`LIVE_CONTEXT_TOKENS`.

    Derived from the card rather than hard-coded, so the same suite admits more models on a bigger
    GPU and fewer on a smaller one instead of encoding one developer's hardware as a constant. The
    largest single device is used, not the sum: a model is served from one device unless the
    provider says otherwise, and summing across cards is exactly the arithmetic ADR-0027 forbids.
    """
    from baseaicore import is_supported

    from freeweight.services.telemetry import build_collector

    try:
        gpus = build_collector().machine_profile().gpus
    except Exception:  # noqa: BLE001 — an unreadable machine gets the conservative budget
        return FALLBACK_VRAM_BUDGET_BYTES
    totals = [
        float(gpu.vram_total_bytes)
        for gpu in gpus
        if gpu.vram_total_bytes is not None and is_supported(gpu.vram_total_bytes)
    ]
    if not totals:
        return FALLBACK_VRAM_BUDGET_BYTES
    return max(0, int(max(totals)) - DISPLAY_RESERVE_BYTES - KV_ALLOWANCE_BYTES)


def _servable(descriptor: Any, *, budget: int | None = None) -> bool:  # noqa: ANN401
    """Whether this machine can serve this model at :data:`LIVE_CONTEXT_TOKENS`.

    Only the weights are checked, because the context is no longer the provider's choice and
    :data:`KV_ALLOWANCE_BYTES` is already held back for it. A model whose size the provider does
    not report is accepted: an unknown size is not evidence of a large one.
    """
    from baseaicore import is_supported

    size = descriptor.size_bytes
    if size is None or not is_supported(size):
        return True
    return float(size) <= (budget if budget is not None else _weights_budget())


@pytest.fixture
def provider() -> Any:
    """A real Ollama provider, or a skip when nothing is serving.

    Skips when nothing installed is small enough to serve safely — see
    :data:`MAX_MODEL_BYTES` and :data:`LIVE_CONTEXT_TOKENS` for what "safely" means.
    """
    from modelrack.errors import ProviderError
    from modelrack.providers.ollama import OllamaProvider

    built = OllamaProvider(base_url=_BASE_URL)
    try:
        health = built.health()
    except ProviderError as exc:
        pytest.skip(f"no Ollama at {_BASE_URL}: {exc}")
    if str(health.status) != "ok":
        pytest.skip(f"Ollama at {_BASE_URL} reports {health.status}")
    descriptors = built.list_models()
    if not descriptors:
        pytest.skip("Ollama is running but has no models pulled")
    budget = _weights_budget()
    if not any(_servable(descriptor, budget=budget) for descriptor in descriptors):
        sizes = ", ".join(
            f"{descriptor.identity.provider_model_name}={descriptor.size_bytes}"
            for descriptor in descriptors
        )
        pytest.skip(
            f"no installed model fits this machine's {budget / 1024**3:.1f} GiB weights budget "
            f"at {LIVE_CONTEXT_TOKENS} tokens of context. Saw: {sizes}"
        )
    return built


def _servable_models(provider: Any) -> list[Any]:  # noqa: ANN401 — a Provider
    """Every model this machine can serve safely, smallest advertised context first."""
    budget = _weights_budget()
    return sorted(
        (item for item in provider.list_models() if _servable(item, budget=budget)),
        key=lambda item: float(item.size_bytes or 0),
    )


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
    # The smallest-context model this machine can serve, never simply the first one the provider
    # happens to list: see MAX_MODEL_BYTES.
    descriptor = _servable_models(provider)[0]
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


def test_the_four_judgement_dependent_suites_run_end_to_end_on_a_real_model(
    live_environment: Any,
) -> None:
    """Phase 8's suites on real weights, asserting shape rather than values.

    The same discipline as the Phase 7 entry above: a live test asserts that the run *completed
    and produced interpretable metrics*, never that a particular local model audits well or judges
    without position bias — those are what these suites exist to measure, and a test that demanded
    a score would fail on a weaker model regardless of whether the measurement was correct.
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
    for suite in ("native.audit", "native.critique", "native.judge", "native.long_context"):
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
        declared = {
            metric.key
            for test in live_environment["registry"].get(suite).tests
            for metric in test.metrics
        }
        for metric in detail.metrics:
            assert metric.metric_key in declared, f"{suite}: undeclared metric {metric.metric_key}"
            assert metric.unit
            assert metric.numeric_value is not None or metric.unavailable_reason


def test_effective_context_differs_from_advertised_context_and_the_data_explains_it(
    live_environment: Any,
) -> None:
    """Phase 8 acceptance criterion 3, which needs real weights to be demonstrable at all.

    Advertised context is what the runtime accepts; effective context is where accuracy is still
    close to the short-context baseline. This asserts that both numbers exist, that the effective
    one is a length the sweep actually tested, and that the per-sample depth data is there to
    explain the difference — never that a particular model's effective context is a particular
    size, which is the measurement rather than the assertion.

    The criterion says the two "differ on at least one real model". They *may* legitimately agree
    on a model whose long-context behaviour holds up across the shipped sweep, so a difference is
    reported rather than required: what is asserted is that the comparison is *possible* and that
    the data behind it is present.
    """
    from freeweight.config import ExecutionSettings
    from freeweight.services.runs import ExecutionConfig, create_run, get_run, list_samples
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
    summary = create_run(
        live_environment["database"],
        live_environment["provider"],
        live_environment["collector"],
        live_environment["registry"],
        model_ref=live_environment["model_ref"],
        suite_key="native.long_context",
        execution=execution,
    )
    RunScheduler(
        live_environment["database"],
        live_environment["provider"],
        registry=live_environment["registry"],
    ).run_once()

    detail = get_run(live_environment["database"], summary.id)
    assert detail.run.status == "completed", detail.run.error_text

    effective = next(
        metric
        for metric in detail.metrics
        if metric.metric_key == "effective_context_tokens" and metric.run_test_id is None
    )
    advertised = detail.run.served_context
    assert advertised is not None, "the run recorded no served context to compare against"

    depths = [
        float(length)
        for test in detail.tests
        for sample in list_samples(live_environment["database"], test.id, limit=100)
        if isinstance(length := sample.detail.get("context_tokens"), int | float)
    ]
    assert depths, "no sample recorded the context length it ran at"

    if effective.numeric_value is None:
        # A model that answered nothing anywhere has *no* effective context, which is a real and
        # honest outcome rather than a small one — and the reason is on the row.
        assert effective.unavailable_reason in {
            "no_short_context_baseline",
            "no_context_observations",
        }
        return
    assert effective.numeric_value in set(depths), (
        "effective context is not one of the lengths the sweep tested"
    )
    assert effective.numeric_value <= advertised + 1024, (
        "effective context exceeded the advertised context, which cannot happen"
    )


def test_a_rules_only_goal_runs_end_to_end_on_a_real_model(
    live_environment: Any, tmp_path: Any
) -> None:
    """Phase 8A acceptance criteria 1 and 2, on real weights.

    A hand-written goal pack whose criteria are entirely rules, run against a real model, with no
    judge configured anywhere. Asserts a composite score, per-criterion scores, a
    ``score_method_mix`` that is entirely rules, and a ``judge_validity_factor`` of 1.0 — none of
    which depends on how well the model writes, which is what the goal exists to measure.
    """
    from freeweight.config import ExecutionSettings
    from freeweight.services.goals import load_goals, sync_goals, write_pack
    from freeweight.services.runs import ExecutionConfig, build_registry, create_run, get_run
    from freeweight.services.scheduler import RunScheduler

    root = tmp_path / "live-goals"
    root.mkdir()
    write_pack(
        root,
        goal={
            "slug": "live_voice",
            "name": "Live voice",
            "goal_pack_version": "1.0.0",
            "schema_version": "1.0",
            "created_by": "live test",
            "criteria": [
                {
                    "key": "no_llm_tells",
                    "name": "No LLM tells",
                    "rung": "rule",
                    "weight": 0.5,
                    "rule": {
                        "type": "forbidden_phrases",
                        "phrases": ["delve", "tapestry", "in today's landscape"],
                    },
                },
                {
                    "key": "length",
                    "name": "About a paragraph",
                    "rung": "rule",
                    "weight": 0.5,
                    "rule": {"type": "word_count", "min": 40, "max": 200},
                },
            ],
        },
        tasks=[
            {
                "prompt_id": "goals.live_voice.warehouse",
                "version": "1.0.0",
                "schema_version": "1.0",
                "purpose": "One task for the live goal run.",
                "task": "goal.live_voice",
                "capability": "creative_writing",
                "system": None,
                "template": "Write one paragraph about a stock count that did not add up.",
                "variables": {},
                "response": {"format": "text", "json_schema_ref": None, "expectations": []},
                "model_requirements": {
                    "min_context_tokens": 2048,
                    "requires_capabilities": [],
                    "recommended_temperature": 0.8,
                },
                "metadata": {
                    "author": "live test",
                    "created_at": "2026-08-27T00:00:00Z",
                    "changed_at": "2026-08-27T00:00:00Z",
                    "change_reason": "First version.",
                    "supersedes": None,
                    "tags": ["goal"],
                    "goal_task": {"key": "warehouse", "name": "Warehouse night"},
                },
            }
        ],
    )
    goals = load_goals(root)
    sync_goals(live_environment["database"], goals)
    registry = build_registry(goals=goals)
    summary = create_run(
        live_environment["database"],
        live_environment["provider"],
        live_environment["collector"],
        registry,
        model_ref=live_environment["model_ref"],
        suite_key="goal.live_voice",
        execution=ExecutionConfig.resolve(
            ExecutionSettings(
                warmup_repetitions=1, cooldown_seconds=0, idle_gpu_threshold_percent=0
            ),
            measured_repetitions=1,
        ),
    )
    RunScheduler(
        live_environment["database"], live_environment["provider"], registry=registry
    ).run_once()

    detail = get_run(live_environment["database"], summary.id)
    assert detail.run.status == "completed", detail.run.error_text
    values = {
        metric.metric_key: metric.numeric_value
        for metric in detail.metrics
        if metric.run_test_id is None
    }
    assert values["composite_score"] is not None
    assert 0.0 <= values["composite_score"] <= 1.0
    assert values["criterion.no_llm_tells"] is not None
    assert values["criterion.length"] is not None
    assert values["score_method_mix_rule"] == 1.0
    assert values["score_method_mix_judge"] == 0.0
    assert values["judge_validity_factor"] == 1.0


# ---------------------------------------------------------------------------
# Phases 9, 10 and 10A on real weights — the M2 exit condition.
#
# The master roadmap states M2's exit as a demonstration rather than a test result: "a real model
# is benchmarked end to end; results are drillable, comparable and exportable; a subjective goal
# can be authored, calibrated and scored entirely from the UI". Everything below is that
# demonstration, driven the way a person would drive it — the HTTP surface for the parts a person
# uses, the run engine for the parts a person waits on.
#
# **What a test cannot do, and does not pretend to.** The calibration step asks the author to grade
# twelve samples on their own criteria. A test has no opinions, so it supplies grades from a fixed
# pattern and says so. What that proves is that the *pipeline* works on real weights against a real
# jury — samples generated by real models, graded, partitioned, scored by models that never see the
# holdout, and an agreement figure computed and stored. It does not prove that a human's grades
# would agree with the jury, and no automated test can.
#
# `native.memory_kv` is deliberately absent: its context sweep climbs to 128K and is an
# hour-scale, OOM-prone run on most machines. Run it by hand with
# `freeweight run start --suite native.memory_kv` when you want it.
# ---------------------------------------------------------------------------

_MINIMUM_JURORS = 2
"""Models needed before a jury can convene: a juror never judges its own output."""

_SPREAD_MODELS = 2
"""How many distinct models the calibration set is generated across.

Subjective Goals §5.1 wants a *spread*, because a set produced by one model at one temperature has
little variance and variance is what an agreement figure is computed over. Two is the smallest
number that is a spread. It is capped rather than "every model this machine serves" because each
additional model is a cold load of several gigabytes, and a demonstration that spends twenty
minutes swapping weights in and out is one nobody runs twice."""


def _profile() -> Any:  # noqa: ANN401 — a RuntimeProfile
    """The runtime profile every live run here is served under.

    Pinned rather than left to the provider, which is the whole point of ADR-0023 §4: the run
    records ``served_context_source = "configured"``, the number in its fingerprint is a fact, and
    the memory it asks for is one this machine chose.
    """
    from freeweight.config import RuntimeSettings

    return RuntimeSettings(context_size=LIVE_CONTEXT_TOKENS).to_profile()


def _execution(**changes: Any) -> Any:  # noqa: ANN401 — an ExecutionConfig
    """The execution settings every live run here shares."""
    from freeweight.config import ExecutionSettings
    from freeweight.services.runs import ExecutionConfig

    return ExecutionConfig.resolve(
        ExecutionSettings(
            warmup_repetitions=0,
            cooldown_seconds=0,
            randomize_case_order=False,
            # This is somebody's working machine and is not necessarily quiet. The point of these
            # runs is that the numbers have the right shape; a contaminated run still has that,
            # and the degradation would be recorded if the check were on.
            idle_gpu_threshold_percent=0,
            **changes,
        ),
        measured_repetitions=changes.pop("measured_repetitions", 1),
    )


def _execute(environment: Any, suite_key: str, **kwargs: Any) -> Any:  # noqa: ANN401
    """Create one run of ``suite_key`` and drive it to completion. Returns its detail."""
    from freeweight.config import TelemetrySettings
    from freeweight.services.runs import create_run, get_run
    from freeweight.services.scheduler import RunScheduler

    summary = create_run(
        environment["database"],
        environment["provider"],
        environment["collector"],
        kwargs.pop("registry", environment["registry"]),
        model_ref=environment["model_ref"],
        suite_key=suite_key,
        execution=_execution(),
        runtime_profile=_profile(),
        **kwargs,
    )
    RunScheduler(
        environment["database"],
        environment["provider"],
        registry=kwargs.get("registry", environment["registry"]),
        collector=environment["collector"],
        telemetry=TelemetrySettings(),
    ).run_once()
    return get_run(environment["database"], summary.id)


def test_the_phase_nine_suites_run_end_to_end_on_a_real_model(live_environment: Any) -> None:
    """``native.reliability`` and ``native.energy`` on real weights.

    Both are run-level derived suites: reliability needs every stored repetition of every case,
    energy needs the power series. Asserted for *shape*, because both are machine-dependent —
    reliability's dispersion depends on the model's sampling and energy's figures depend on whether
    this machine can read GPU power at all. A machine that cannot must report ``unsupported`` with
    a reason, never ``0`` (ADR-0016 §4), and that is the assertion that matters here.
    """
    reliability = _execute(live_environment, "native.reliability")
    assert reliability.run.status == "completed", reliability.run.error_text

    figures = {metric.metric_key: metric for metric in reliability.metrics}
    assert figures, "the reliability suite produced no metrics"
    for metric in figures.values():
        if metric.numeric_value is None:
            assert metric.unavailable_reason, f"{metric.metric_key} is absent with no reason"
        else:
            assert metric.numeric_value == metric.numeric_value, metric.metric_key  # not NaN

    energy = _execute(live_environment, "native.energy")
    assert energy.run.status == "completed", energy.run.error_text
    energy_figures = {metric.metric_key: metric for metric in energy.metrics}
    for key in ("gpu_energy_joules", "joules_per_output_token", "peak_gpu_power_watts"):
        assert key in energy_figures, f"{key} was not emitted at all"
        metric = energy_figures[key]
        if metric.numeric_value is None:
            assert metric.unavailable_reason, key
        else:
            assert metric.numeric_value >= 0.0, key


def test_results_from_a_real_run_are_drillable_comparable_and_exportable(
    live_environment: Any, tmp_path: Any
) -> None:
    """M2's middle clause, on real weights.

    Two runs of one suite, then: the dashboard shows a figure from them, that figure links to a
    run whose test page lists real stored samples, the two runs compare without being merged
    across a boundary they do not cross, and the export reads back into the same metrics.
    """
    from freeweight.services.export import ExportScope, ExportSelection, iter_export, read_export
    from freeweight.services.results import DashboardFilter, build_dashboard
    from freeweight.services.runs import list_samples

    database = live_environment["database"]
    first = _execute(live_environment, "native.performance")
    second = _execute(live_environment, "native.performance")
    assert first.run.status == "completed", first.run.error_text
    assert second.run.status == "completed", second.run.error_text

    # Drillable: a dashboard cell names a run, and that run's test holds stored samples.
    dashboard = build_dashboard(database, DashboardFilter())
    assert dashboard.cards.completed_runs >= 2  # noqa: PLR2004
    assert dashboard.heatmap.cells, "the dashboard showed nothing for two completed runs"
    cell = next(iter(dashboard.heatmap.cells.values()))
    assert cell.run_id in {first.run.id, second.run.id}
    samples = list_samples(database, second.tests[0].id)
    assert samples, "the run stored no samples to drill into"
    assert any(sample.response_hash for sample in samples), "no response was recorded"

    # Comparable: two runs of one suite on one machine are mergeable, and say so.
    from freeweight.services.comparison import compare_runs

    comparison = compare_runs(database, [first.run.id, second.run.id])
    assert comparison.rows
    assert any(row.mergeable for row in comparison.rows), (
        "two runs of one suite on one machine were separated; the fingerprint diff says why: "
        f"{[entry.reason for entry in comparison.separations]}"
    )

    # Exportable: the document reads back into the figures the dashboard showed.
    exported = read_export(
        "".join(
            iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=second.run.id))
        )
    )
    assert len(exported) == 1
    for metric in second.metrics:
        if metric.run_test_id is not None:
            continue
        carried = exported[0].metric(metric.metric_key)
        assert carried is not None, f"{metric.metric_key} is missing from the export"
        assert carried.value == metric.numeric_value, metric.metric_key


def test_a_subjective_goal_is_authored_calibrated_and_scored_on_real_weights(
    provider: Any, tmp_path: Any
) -> None:
    """M2's last clause: the whole goal path against a real model **and a real jury**.

    Driven through the HTTP surface a person uses — the wizard's forms, the grading form, the
    calibration endpoint — because "entirely from the UI" is the claim being demonstrated. The
    grades are supplied from a fixed pattern rather than by a person, which is the one substitution
    a test has to make and the one thing it therefore does not prove.

    Skips when fewer than two models are pulled: a juror never judges its own output, so a
    one-model machine has no jury to convene and the goal would score its rules and honestly skip
    its judged criterion.
    """
    import json
    import time

    from fastapi.testclient import TestClient

    from freeweight.config import load_settings
    from freeweight.infrastructure.db.engine import create_engine_for
    from freeweight.infrastructure.db.migration import MigrationRunner
    from freeweight.services.database import MIGRATIONS_LOCATION
    from freeweight.web.app import create_app

    descriptors = _servable_models(provider)
    if len(descriptors) < _MINIMUM_JURORS:
        pytest.skip(
            f"a jury needs at least {_MINIMUM_JURORS} models this machine can serve and it has "
            f"{len(descriptors)}; see MAX_MODEL_BYTES"
        )
    candidate = descriptors[0].identity.provider_model_name
    juror = descriptors[1].identity.canonical_id

    database_path = tmp_path / "live-goal.sqlite3"
    goals_root = tmp_path / "live-goal-packs"
    goals_root.mkdir()
    import os

    for key, value in {
        "FREEWEIGHT_STORAGE__DATABASE_URL": f"sqlite:///{database_path}",
        "FREEWEIGHT_PROVIDER__KIND": "ollama",
        "FREEWEIGHT_PROVIDER__BASE_URL": _BASE_URL,
        "FREEWEIGHT_GOALS__ROOT": str(goals_root),
        "FREEWEIGHT_EXECUTION__COOLDOWN_SECONDS": "0",
        "FREEWEIGHT_EXECUTION__IDLE_GPU_THRESHOLD_PERCENT": "0",
        "FREEWEIGHT_EXECUTION__MEASURED_REPETITIONS": "1",
        "FREEWEIGHT_EXECUTION__WARMUP_REPETITIONS": "0",
        # One juror, one repetition: the jury's *composition* is exercised by
        # tests/integration/test_jury_assembly.py, and three jurors × three repetitions × five
        # holdout samples is forty-five real generations for a demonstration that needs one.
        "FREEWEIGHT_JUDGE__JURY_SIZE": "1",
        "FREEWEIGHT_JUDGE__REPETITIONS": "1",
        # **Name the juror.** Left unset, `build_jury` selects from every model installed, in sort
        # order — which on the machine this test was written against picked a 15.7B model with a
        # 164K advertised context and brought the machine down. Jury *selection* is exercised by
        # tests/integration/test_jury_assembly.py; what this test needs is a juror it can serve.
        "FREEWEIGHT_JUDGE__MODELS": juror,
    }.items():
        os.environ[key] = value

    engine = create_engine_for(f"sqlite:///{database_path}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")

    slug = "live_authored_voice"
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as client:
        # The models page's own form, because that is the surface that exists: spec §7.1 lists
        # `POST /api/v1/models/discover` and it is not implemented — models are HTML-only today
        # (recorded in PHASE10_ISSUES.md). Clicking the button is what a person does anyway.
        discovered = client.post("/models/discover", follow_redirects=False)
        assert discovered.status_code == 303, discovered.text  # noqa: PLR2004 — See Other

        # --- Steps 1-4 and 7, through the wizard's own forms.
        started = client.post(
            "/goals/new",
            data={
                "intent": "Short answers that stay concrete and do not pad.",
                "name": "Live authored voice",
            },
            follow_redirects=False,
        )
        assert started.status_code == 303, started.text
        draft_id = started.headers["location"].split("/goals/new/", 1)[1].split("/", 1)[0]

        client.post(
            f"/goals/new/{draft_id}/criteria",
            data={"action": "add", "name": "No padding", "intent": "No filler phrases."},
        )
        client.post(
            f"/goals/new/{draft_id}/criteria",
            data={"action": "add", "name": "Concrete", "intent": "Anchored to something specific."},
        )
        client.post(
            f"/goals/new/{draft_id}/criteria",
            data={
                "action": "describe",
                "criterion": "concrete",
                "points": "5",
                "top": "Every claim is anchored to a specific thing.",
                "middle": "Mixed; abstractions carry about half of it.",
                "bottom": "Abstraction throughout; nothing you could photograph.",
            },
        )
        # One criterion accepts a rule, so the goal's score_method_mix has both halves in it.
        client.post(
            f"/goals/new/{draft_id}/rules",
            data={
                "action": "accept",
                "criterion": "no_padding",
                "rule_type": "forbidden_phrases",
                "parameters": json.dumps(
                    {"phrases": ["delve", "in today's landscape", "it is worth noting"]}
                ),
            },
        )
        client.post(
            f"/goals/new/{draft_id}/tasks",
            data={
                "action": "add",
                "name": "Warehouse night",
                "prompt_text": (
                    "In one short paragraph, describe a warehouse at night. Be specific."
                ),
            },
        )
        saved = client.post(
            f"/goals/new/{draft_id}/save", data={"name": "Live authored voice", "slug": slug}
        )
        assert saved.status_code == 200, saved.text
        assert (goals_root / slug / "goal.json").is_file(), "the wizard wrote no pack"

        # --- Step 5: candidate outputs from a spread of real models, graded through the form.
        prompt = "In one short paragraph, describe a warehouse at night. Be specific."
        contents = _generate_spread(provider, prompt, wanted=8)
        added = client.post(
            f"/api/v1/goals/{slug}/calibration/samples",
            json={"samples": [{"content": text} for text in contents]},
        )
        assert added.status_code in (200, 201), added.text
        sample_ids = [
            item["id"] for item in client.get(f"/api/v1/goals/{slug}/calibration").json()["items"]
        ]
        assert len(sample_ids) >= 8, "fewer samples than the calibration minimum"  # noqa: PLR2004

        for index, sample_id in enumerate(sample_ids):
            posted = client.post(
                f"/goals/{slug}/grade",
                data={
                    "sample_id": sample_id,
                    "criterion": "concrete",
                    # A spread across the scale: a set graded all-alike has no variance to agree
                    # about, and the calibration would refuse it (Subjective Goals §5.1).
                    "grade": str((index % 5) + 1),
                    "note": f"live grade {index}",
                },
            )
            assert posted.status_code == 200, posted.text
        progress = client.get(f"/api/v1/goals/{slug}/calibration").json()["progress"]
        assert progress["complete"] is True, progress

        # --- Step 6: a real jury scores the holdout it has never seen.
        measured = client.post(
            f"/api/v1/goals/{slug}/calibration/run", json={"graded_by": "live test"}
        )
        assert measured.status_code == 200, measured.text
        report = measured.json()
        assert report["calibration_state"] in {"calibrated", "uncalibrated"}, report
        assert report["n_holdout"] > 0, "the jury scored no held-out sample"
        for criterion in report["criteria"]:
            # Every coefficient carries its n. A kappa_w without one is a number pretending to be
            # a fact (Subjective Goals §5.4).
            assert criterion["n_holdout"] > 0, criterion
            assert criterion["band"], criterion
        assert 0.0 <= report["judge_validity_factor"] <= 1.0, report

        # The report screen states the band in words, not as a bare coefficient.
        page = client.get(f"/goals/{slug}/report")
        assert page.status_code == 200
        assert "What the number means" in page.text

    # --- Run and score the goal itself, in a fresh process: the registry is built at startup.
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as second:
        created = second.post("/api/v1/runs", json={"model": candidate, "suite": f"goal.{slug}"})
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]
        terminal = {"completed", "failed", "cancelled", "interrupted"}
        deadline = time.monotonic() + 1800.0
        while second.get(f"/api/v1/runs/{run_id}").json()["status"] not in terminal:
            assert time.monotonic() < deadline, "the goal run never finished"
            time.sleep(0.5)
        body = second.get(f"/api/v1/runs/{run_id}").json()
        assert body["status"] == "completed", body.get("error_text")

        metrics = {metric["metric_key"]: metric for metric in body["metrics"]}
        assert "composite_score" in metrics, sorted(metrics)
        # score_method_mix beside the score, always: this goal has one rule and one judged
        # criterion, so both shares must be present and neither may be the whole of it.
        rule_share = metrics["score_method_mix_rule"]["value"]
        judge_share = metrics["score_method_mix_judge"]["value"]
        assert rule_share not in (None, "unsupported"), metrics["score_method_mix_rule"]
        assert judge_share not in (None, "unsupported"), metrics["score_method_mix_judge"]
        assert float(rule_share) > 0.0 and float(judge_share) > 0.0

        detail = second.get(f"/results/goals/{slug}")
        assert detail.status_code == 200
        assert "Score method mix" in detail.text


def _generate_spread(provider: Any, prompt: str, *, wanted: int) -> list[str]:
    """Generate ``wanted`` candidate outputs across every model this machine serves.

    A spread on purpose (Subjective Goals §5.1): a calibration set produced by one model at one
    temperature has far less variance than one produced by several, and variance is the thing an
    agreement figure is computed over. Across :data:`_SPREAD_MODELS` models and three temperatures,
    which is a spread without being a weight-loading marathon.
    """
    from modelrack.types import GenerationRequest, Message, Role, SamplingParameters

    descriptors = list(provider.list_models())[:_SPREAD_MODELS]
    texts: list[str] = []
    index = 0
    while len(texts) < wanted:
        descriptor = descriptors[index % len(descriptors)]
        result = provider.generate(
            GenerationRequest(
                identity=descriptor.identity,
                messages=(Message(role=Role.USER, content=prompt),),
                sampling=SamplingParameters(
                    temperature=0.2 + 0.3 * (index % 3), seed=index, max_output_tokens=160
                ),
            )
        )
        text = (result.text or "").strip()
        if text:
            texts.append(text)
        index += 1
        assert index < wanted * 4, "the provider kept returning empty text"
    return texts
