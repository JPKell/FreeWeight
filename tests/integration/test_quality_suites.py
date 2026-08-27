"""Phase 7 integration: the five deterministic quality suites, end to end.

Every test drives the *real* run engine against :class:`~modelrack.testing.FakeProvider`, so the
whole path — create, prepare, warm, execute (tool loops and corrective retries included), score,
aggregate, complete — runs with no GPU, no Ollama and no network.

Covers the Phase 7 assertions that need a database or a provider:

* the five suites run end to end and produce interpretable metrics
  (:class:`TestFiveSuitesRunEndToEnd`, acceptance criterion 1);
* nothing in the phase is scored by a model (:class:`TestNothingIsJudged`, criterion 2);
* a provider without tool calling or structured output records
  ``skipped (unsupported_capability)`` and contributes no score
  (:class:`TestCapabilityGating`, criterion 3);
* the tool loop actually loops, and the toolbox's answers reach the model
  (:class:`TestTheToolLoop`);
* the structured-output corrective retry is measured as its own figure
  (:class:`TestStructuredOutputRetry`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from modelrack.provider import ProviderCapabilities
from modelrack.testing import (
    MINIMAL_CAPABILITIES,
    FakeFailure,
    FakeFailureMode,
    FakeGeneration,
    FakeProvider,
    FakeScript,
    FakeToolCall,
)

from freeweight.benchmarks.interaction import ToolSession
from freeweight.benchmarks.tool_use.benchmark import build as build_tool_use
from freeweight.benchmarks.tool_use.benchmark import toolbox_for
from freeweight.config import ExecutionSettings

# Aliased: pytest tries to collect any module-level name beginning with ``Test``.
from freeweight.domain.run_state import RunStatus
from freeweight.domain.run_state import TestStatus as RunTestStatus
from freeweight.domain.scoring import ScoreMethod
from freeweight.infrastructure.db.repositories.runs import RunTestRepository
from freeweight.services.runs import (
    ExecutionConfig,
    build_registry,
    create_run,
    get_run,
    list_samples,
    shipped_prompt_library,
)
from freeweight.services.scheduler import RunScheduler

QUALITY_SUITES = (
    "native.instruction_following",
    "native.structured_output",
    "native.tool_use",
    "native.tool_recovery",
    "native.agent",
)
"""The five suites Phase 7 delivers, in the order the development plan names them."""

_TOOL_CALLING_ONLY = ProviderCapabilities(streaming=True, token_counts=True, tool_calling=True)
"""A provider with tools but no schema enforcement — the gate's other side."""


def _execution(**overrides: Any) -> ExecutionConfig:
    fields: dict[str, Any] = {
        "warmup_repetitions": 0,
        "cooldown_seconds": 0,
        "idle_gpu_threshold_percent": 0,
        "randomize_case_order": False,
    }
    fields.update(overrides)
    return ExecutionConfig.resolve(ExecutionSettings(**fields), measured_repetitions=1)


def _run_suite(environment: Any, suite: str) -> Any:
    """Create and execute one run, returning its detail."""
    summary = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key=suite,
        execution=_execution(),
    )
    RunScheduler(
        environment.database, environment.provider, registry=environment.registry
    ).run_once()
    return get_run(environment.database, summary.id)


def _test_rows(environment: Any, run_id: str) -> list[Any]:
    with environment.database.read() as session:
        return RunTestRepository().list_for_run(session, run_id)


def _samples(environment: Any, detail: Any) -> list[Any]:
    """Every sample of every test of one run, in test order."""
    return [
        sample
        for test in detail.tests
        for sample in list_samples(environment.database, test.id, limit=1000)
    ]


@pytest.fixture
def environment(run_environment: Callable[..., Any]) -> Any:
    """A run environment whose fake provider declares everything the quality suites need."""
    return run_environment(script=FakeScript(generations=(FakeGeneration(word_count=12),)))


class TestFiveSuitesRunEndToEnd:
    """Acceptance criterion 1, minus the real model, which is the live test's job."""

    @pytest.mark.parametrize("suite", QUALITY_SUITES)
    def test_the_suite_completes_and_produces_metrics(self, environment: Any, suite: str) -> None:
        detail = _run_suite(environment, suite)
        assert detail.run.status == RunStatus.COMPLETED.value
        assert detail.tests, f"{suite} declared no tests"
        assert all(row.status == RunTestStatus.COMPLETED.value for row in detail.tests)
        assert detail.metrics, f"{suite} produced no metric rows"

    @pytest.mark.parametrize("suite", QUALITY_SUITES)
    def test_every_case_produced_a_sample(self, environment: Any, suite: str) -> None:
        detail = _run_suite(environment, suite)
        samples = _samples(environment, detail)
        planned = sum(
            row.total_cases * row.repetitions for row in _test_rows(environment, detail.run.id)
        )
        assert len(samples) == planned > 0

    @pytest.mark.parametrize("suite", QUALITY_SUITES)
    def test_the_metrics_are_the_ones_the_manifest_declares(
        self, environment: Any, suite: str
    ) -> None:
        # "Interpretable" means each metric arrives under its own key with its own unit and
        # direction — not the headline score repeated a dozen times under a dozen names.
        detail = _run_suite(environment, suite)
        benchmark = build_registry().get(suite)
        declared = {entry["key"] for entry in benchmark.manifest.body["metrics"]}
        produced = {metric.metric_key for metric in detail.metrics}
        assert produced <= declared
        assert produced, f"{suite} produced no metric under any declared key"

    def test_distinct_metrics_get_distinct_values(self, environment: Any) -> None:
        detail = _run_suite(environment, "native.instruction_following")
        run_level = {
            metric.metric_key: metric.numeric_value
            for metric in detail.metrics
            if metric.run_test_id is None
        }
        assert "strict_prompt_accuracy" in run_level
        assert "instruction_level_accuracy" in run_level
        # A pseudo-text answer meets some constraints and not others, so the two figures must
        # differ; if they did not, every metric would be the headline score wearing a new name.
        assert run_level["strict_prompt_accuracy"] != run_level["instruction_level_accuracy"]


class TestNothingIsJudged:
    """Acceptance criterion 2: no LLM scores anything in this phase."""

    @pytest.mark.parametrize("suite", QUALITY_SUITES)
    def test_every_scorer_is_on_a_deterministic_rung(self, suite: str) -> None:
        for test in build_registry().get(suite).tests:
            assert test.scorer.method in {
                ScoreMethod.EXECUTION,
                ScoreMethod.RULE,
                ScoreMethod.REFERENCE,
            }, f"{suite}/{test.key} would be scored by {test.scorer.method}"

    @pytest.mark.parametrize("suite", QUALITY_SUITES)
    def test_every_sample_records_the_rung_that_produced_it(
        self, environment: Any, suite: str
    ) -> None:
        detail = _run_suite(environment, suite)
        methods = {
            sample.score_method
            for sample in _samples(environment, detail)
            if sample.score_method is not None
        }
        assert methods and ScoreMethod.JUDGE.value not in methods


class TestCapabilityGating:
    """Acceptance criterion 3: an unsupported capability is a recorded skip, never a low score."""

    @pytest.fixture
    def toolless(self, run_environment: Callable[..., Any]) -> Any:
        return run_environment(
            script=FakeScript(
                capabilities=MINIMAL_CAPABILITIES, generations=(FakeGeneration(word_count=8),)
            )
        )

    @pytest.mark.parametrize("suite", ["native.tool_use", "native.tool_recovery", "native.agent"])
    def test_a_model_without_tool_support_is_skipped_with_a_reason(
        self, toolless: Any, suite: str
    ) -> None:
        detail = _run_suite(toolless, suite)
        assert detail.run.status == RunStatus.COMPLETED.value, "a skip is not a failure"
        assert detail.tests
        for row in detail.tests:
            assert row.status == RunTestStatus.SKIPPED.value
            assert row.skip_reason == "unsupported_capability"

    def test_structured_output_is_gated_on_its_own_capability(self, run_environment: Any) -> None:
        # A provider with tools but no schema enforcement skips this suite and only this suite,
        # which is what makes the gate per-capability rather than per-suite-family.
        environment = run_environment(
            script=FakeScript(
                capabilities=_TOOL_CALLING_ONLY, generations=(FakeGeneration(word_count=8),)
            )
        )
        gated = _run_suite(environment, "native.structured_output")
        ungated = _run_suite(environment, "native.tool_use")
        assert all(row.status == RunTestStatus.SKIPPED.value for row in gated.tests)
        assert all(row.status == RunTestStatus.COMPLETED.value for row in ungated.tests)

    def test_a_skipped_test_contributes_no_score_and_no_sample(self, toolless: Any) -> None:
        detail = _run_suite(toolless, "native.tool_use")
        assert _samples(toolless, detail) == []
        values = [
            metric.numeric_value for metric in detail.metrics if metric.metric_key == "task_success"
        ]
        assert all(value is None for value in values), (
            "a capability the model never had must not average in as a zero"
        )

    def test_the_skip_names_the_missing_capability(self, toolless: Any) -> None:
        detail = _run_suite(toolless, "native.agent")
        rows = _test_rows(toolless, detail.run.id)
        assert rows and all(row.error_code == "CAPABILITY_UNSUPPORTED" for row in rows)
        assert all("tool_calling" in (row.error_text or "") for row in rows)


class TestTheToolLoop:
    """The interaction really loops, and the toolbox's answer really reaches the model."""

    def _case(self, case_id: str) -> Any:
        for test in build_tool_use(shipped_prompt_library()).tests:
            for case in test.declared_cases:
                if case.case_id == case_id:
                    return case
        raise AssertionError(f"no case {case_id!r}")

    def _caller(self, provider: FakeProvider) -> Any:
        from modelrack import GenerationRequest

        def call(messages: Any, *, tools: Any = (), response_format: Any = None) -> Any:
            return provider.generate(
                GenerationRequest(
                    identity=provider.resolve("fake-model"),
                    messages=tuple(messages),
                    tools=tuple(tools),
                    response_format=response_format,
                )
            )

        return call

    def test_a_scripted_trajectory_calls_the_tool_and_answers_from_its_result(self) -> None:
        provider = FakeProvider(
            FakeScript(
                generations=(
                    FakeGeneration(
                        text="",
                        tool_calls=(FakeToolCall(name="get_inventory", arguments={"sku": "A1"}),),
                    ),
                    FakeGeneration(text="12"),
                )
            )
        )
        case = self._case("one-correct-tool")
        outcome = ToolSession(toolbox=toolbox_for).run(self._caller(provider), case)
        assert outcome.transcript is not None
        assert [call.name for call in outcome.transcript.calls] == ["get_inventory"]
        assert outcome.transcript.calls[0].ok
        assert outcome.transcript.stopped == "answered"
        assert outcome.text == "12"
        assert outcome.transcript.steps == 2

    def test_a_hallucinated_tool_is_answered_with_a_refusal_not_executed(self) -> None:
        provider = FakeProvider(
            FakeScript(
                generations=(
                    FakeGeneration(
                        text="", tool_calls=(FakeToolCall(name="shell", arguments={"cmd": "ls"}),)
                    ),
                    FakeGeneration(text="I cannot."),
                )
            )
        )
        outcome = ToolSession(toolbox=toolbox_for).run(
            self._caller(provider), self._case("one-correct-tool")
        )
        assert outcome.transcript is not None
        call = outcome.transcript.calls[0]
        assert not call.known_tool
        assert not call.executed
        assert call.error_code == "UNKNOWN_TOOL"

    def test_the_step_budget_ends_a_loop_and_is_recorded(self) -> None:
        provider = FakeProvider(
            FakeScript(
                generations=(
                    FakeGeneration(
                        text="",
                        tool_calls=(FakeToolCall(name="get_inventory", arguments={"sku": "A1"}),),
                    ),
                ),
                repeat_final_generation=True,
            )
        )
        outcome = ToolSession(toolbox=toolbox_for, max_steps=3).run(
            self._caller(provider), self._case("one-correct-tool")
        )
        assert outcome.transcript is not None
        assert outcome.transcript.stopped == "step_limit"
        assert outcome.transcript.steps == 3
        assert outcome.text == ""

    def test_an_injected_failure_is_answered_and_the_second_call_works(self) -> None:
        provider = FakeProvider(
            FakeScript(
                generations=(
                    FakeGeneration(
                        text="",
                        tool_calls=(FakeToolCall(name="get_inventory", arguments={"sku": "A1"}),),
                    ),
                    FakeGeneration(
                        text="",
                        tool_calls=(FakeToolCall(name="get_inventory", arguments={"sku": "A1"}),),
                    ),
                    FakeGeneration(text="12"),
                )
            )
        )
        outcome = ToolSession(toolbox=toolbox_for).run(
            self._caller(provider), self._case("tool-failure")
        )
        assert outcome.transcript is not None
        assert [call.ok for call in outcome.transcript.calls] == [False, True]
        assert outcome.transcript.calls[0].error_code == "TIMEOUT"

    def test_the_whole_trajectory_is_stored_on_the_sample(self, environment: Any) -> None:
        detail = _run_suite(environment, "native.tool_use")
        samples = _samples(environment, detail)
        stored = [sample for sample in samples if "transcript" in sample.detail]
        assert stored, "no sample carried its trajectory"
        assert "offered_tools" in stored[0].detail["transcript"]


class TestStructuredOutputRetry:
    """One corrective retry, measured as its own figure and never folded into the first."""

    _SCHEMA_ANSWER = json.dumps({"sku": "A1", "name": "Bracket", "units": 3})

    def test_a_first_attempt_that_conforms_needs_no_retry(
        self, run_environment: Callable[..., Any]
    ) -> None:
        environment = run_environment(
            script=FakeScript(generations=(FakeGeneration(text=self._SCHEMA_ANSWER),))
        )
        detail = _run_suite(environment, "native.structured_output")
        samples = _samples(environment, detail)
        first = next(sample for sample in samples if sample.case_id == "flat-record")
        assert first.detail["first_attempt_conformed"] == 1.0
        assert first.detail["retried"] == 0.0
        assert first.score == 1.0

    def test_a_failed_first_attempt_is_retried_once_and_the_recovery_is_recorded(
        self, run_environment: Callable[..., Any]
    ) -> None:
        environment = run_environment(
            script=FakeScript(
                generations=(
                    FakeGeneration(text="Here is the record you asked for."),
                    FakeGeneration(text=self._SCHEMA_ANSWER),
                ),
                repeat_final_generation=True,
            )
        )
        detail = _run_suite(environment, "native.structured_output")
        samples = _samples(environment, detail)
        first = next(sample for sample in samples if sample.case_id == "flat-record")
        assert first.detail["first_attempt_conformed"] == 0.0
        assert first.detail["recovered_after_retry"] == 1.0
        assert first.score == 1.0, "the scorer scores the final attempt"
        assert first.detail["first_attempt_failures"]

    def test_the_two_rates_are_stored_as_separate_metrics(
        self, run_environment: Callable[..., Any]
    ) -> None:
        environment = run_environment(
            script=FakeScript(
                generations=(
                    FakeGeneration(text="not json"),
                    FakeGeneration(text=self._SCHEMA_ANSWER),
                ),
                repeat_final_generation=True,
            )
        )
        detail = _run_suite(environment, "native.structured_output")
        run_level = {
            metric.metric_key: metric.numeric_value
            for metric in detail.metrics
            if metric.run_test_id is None
        }
        assert run_level["first_attempt_conformed"] == 0.0
        assert run_level["recovered_after_retry"] is not None
        assert run_level["schema_conformance"] != run_level["first_attempt_conformed"]


class TestAProviderFailureMidInteraction:
    """A provider that fails part-way through fails one sample, never the test or the run."""

    @pytest.fixture
    def failing(self, run_environment: Callable[..., Any]) -> Any:
        return run_environment(
            script=FakeScript(
                generations=(
                    FakeGeneration(
                        text="",
                        tool_calls=(FakeToolCall(name="get_inventory", arguments={"sku": "A1"}),),
                    ),
                    FakeGeneration(failure=FakeFailure(mode=FakeFailureMode.TIMEOUT)),
                ),
                repeat_final_generation=True,
            )
        )

    def test_the_run_completes_and_the_samples_carry_the_error(self, failing: Any) -> None:
        detail = _run_suite(failing, "native.tool_use")
        assert detail.run.status == RunStatus.COMPLETED.value
        assert all(row.status == RunTestStatus.COMPLETED.value for row in detail.tests)
        samples = _samples(failing, detail)
        failed = [sample for sample in samples if sample.status in {"failed", "timeout"}]
        assert failed, "the scripted failure produced no failed sample"
        assert all(sample.score is None for sample in failed), (
            "a provider failure is never a zero score (ADR-0016)"
        )

    def test_a_failing_first_call_leaves_a_recorded_sample(
        self, run_environment: Callable[..., Any]
    ) -> None:
        environment = run_environment(
            script=FakeScript(
                generations=(FakeGeneration(failure=FakeFailure(mode=FakeFailureMode.TIMEOUT)),),
                repeat_final_generation=True,
            )
        )
        detail = _run_suite(environment, "native.structured_output")
        assert detail.run.status == RunStatus.COMPLETED.value
        samples = _samples(environment, detail)
        assert samples and all(sample.score is None for sample in samples)
        assert all(sample.error_code is not None for sample in samples)

    def test_a_failure_on_the_corrective_retry_keeps_the_first_attempts_verdict(
        self, run_environment: Callable[..., Any]
    ) -> None:
        environment = run_environment(
            script=FakeScript(
                generations=(
                    FakeGeneration(text="not json"),
                    FakeGeneration(failure=FakeFailure(mode=FakeFailureMode.TIMEOUT)),
                ),
                repeat_final_generation=True,
            )
        )
        detail = _run_suite(environment, "native.structured_output")
        assert detail.run.status == RunStatus.COMPLETED.value
        samples = _samples(environment, detail)
        first = next(sample for sample in samples if sample.case_id == "flat-record")
        assert first.detail["first_attempt_conformed"] == 0.0
        assert "recovered_after_retry" not in first.detail, (
            "a retry that never returned did not recover, and did not fail to recover either"
        )


class TestSuiteBuildRefusals:
    """A suite whose declarations do not describe what is installed refuses to build.

    Startup, not mid-run: a suite whose provenance is wrong must not be runnable at all
    ([ADR-0028 §1](../../../docs/adr/0028-prompt-pack-granularity.md)).
    """

    def test_a_stale_prompt_subset_hash_is_refused(self) -> None:
        from freeweight.benchmarks.loading import verify_prompts
        from freeweight.benchmarks.tool_use.benchmark import load_suite_manifest

        manifest = load_suite_manifest()
        stale = replace(manifest, prompt_subset_hash="sha256:" + "0" * 64)
        with pytest.raises(ValueError, match="prompt_subset_hash"):
            verify_prompts(stale, shipped_prompt_library())

    def test_a_case_file_with_no_tests_is_refused(self, tmp_path: Path) -> None:
        from freeweight.benchmarks.loading import load_cases

        path = tmp_path / "cases.json"
        path.write_text(json.dumps({"cases": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="tests"):
            load_cases(path)

    def test_a_test_naming_an_undeclared_metric_is_refused(self, tmp_path: Path) -> None:
        from freeweight.benchmarks.loading import build_tests
        from freeweight.benchmarks.tool_use.benchmark import PROMPT_ID, load_suite_manifest

        with pytest.raises(KeyError):
            build_tests(
                manifest=load_suite_manifest(),
                cases={
                    "tests": [
                        {
                            "key": "t",
                            "name": "t",
                            "metric_keys": ["no_such_metric"],
                            "cases": [],
                        }
                    ]
                },
                pack=shipped_prompt_library(),
                prompt_id=PROMPT_ID,
                scorer_for=lambda _body: None,
            )
