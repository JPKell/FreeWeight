"""Phase 6 integration: a measured run, its telemetry, its provenance and its repeat.

Every test here drives the *real* run engine against :class:`~modelrack.testing.FakeProvider` and
scripted telemetry readers, so the whole path — create, calibrate, settle, prepare, warm, execute,
aggregate, complete — runs with no GPU, no Ollama and no network.

Covers the Phase 6 test list that needs a database:

* telemetry rows written only during a run, one host row per observation and one GPU row per
  device, cascade-deleted with the run (:class:`TestTelemetryPersistence`);
* idle detection's three outcomes (:class:`TestIdleDetection`);
* the fingerprint document stored and diffable, and ``run repeat``'s refusal and ``--force``
  (:class:`TestRepeat`);
* acceptance criteria 1-4 (:class:`TestAcceptanceCriteria`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from baseaicore import UNSUPPORTED, utc_now
from modelrack import Timing
from modelrack.testing import FakeGeneration, FakeModel, FakeProvider, FakeScript
from sweatmeter import GpuSample, TelemetryCollector
from sweatmeter.testing import HostReading, ScriptedGpuReader, ScriptedHostReader

from freeweight.config import ExecutionSettings, TelemetrySettings
from freeweight.domain.provenance import diff_documents
from freeweight.infrastructure.db.models_runs import Run
from freeweight.infrastructure.db.repositories.telemetry import TelemetryRepository
from freeweight.services.models import discover_models
from freeweight.services.runs import (
    ExecutionConfig,
    RepeatRefused,
    create_run,
    get_run,
    list_samples,
    repeat_run,
)
from freeweight.services.scheduler import RunScheduler
from freeweight.services.telemetry_recording import (
    TelemetryRecorder,
    calibrate_sampling_overhead,
    load_series,
    wait_for_idle,
)

_TIMED = FakeGeneration(
    text="ok",
    input_tokens=512,
    output_tokens=64,
    backend_timing=Timing(
        backend_prompt_eval_ms=256.0,
        backend_decode_ms=2000.0,
        backend_total_ms=2256.0,
    ),
    first_chunk_delay_ms=40.0,
    chunk_delay_ms=10.0,
)
"""One scripted generation with a full provider account of its own work.

The default fake reports no ``backend_*`` timing at all, which is honest — it ran no model — but
means every throughput metric is ``UNSUPPORTED``. These figures are what let the tests below assert
on real numbers rather than on the absence of them.
"""


def _script(**overrides: Any) -> FakeScript:
    return FakeScript(generations=(_TIMED,), **overrides)


def _collector(*, cpu_percent: float = 1.0, gpus: int = 0, steps: int = 600) -> TelemetryCollector:
    """A collector replaying one fixed reading, enough times for any test here."""
    host = ScriptedHostReader([HostReading(cpu_percent=cpu_percent)] * steps)
    if gpus == 0:
        return TelemetryCollector(host=host)
    devices = tuple(
        GpuSample(
            index=index,
            uuid=f"GPU-{index}",
            utilization_percent=2.0,
            vram_used_bytes=1_000_000 * (index + 1),
            vram_total_bytes=16_000_000,
            temperature_c=40.0 + index,
            power_watts=50.0 + index,
        )
        for index in range(gpus)
    )
    return TelemetryCollector(host=host, gpu=ScriptedGpuReader([devices] * steps))


def _execution(**overrides: Any) -> ExecutionConfig:
    fields: dict[str, Any] = {
        "warmup_repetitions": 0,
        "cooldown_seconds": 0,
        "idle_gpu_threshold_percent": 0,
        "randomize_case_order": False,
    }
    fields.update(overrides)
    return ExecutionConfig.resolve(ExecutionSettings(**fields), measured_repetitions=1)


def _run_suite(
    environment: Any,
    suite: str,
    *,
    collector: TelemetryCollector | None = None,
    telemetry: TelemetrySettings | None = None,
    execution: ExecutionConfig | None = None,
) -> Any:
    """Create and execute one run, returning its detail."""
    summary = create_run(
        environment.database,
        environment.provider,
        collector if collector is not None else environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key=suite,
        execution=execution if execution is not None else _execution(),
    )
    RunScheduler(
        environment.database,
        environment.provider,
        registry=environment.registry,
        collector=collector,
        telemetry=telemetry,
    ).run_once()
    return get_run(environment.database, summary.id)


@pytest.fixture
def environment(run_environment: Callable[..., Any]) -> Any:
    """A run environment whose provider reports its own timings."""
    return run_environment(script=_script())


class TestThePerformanceSuiteRuns:
    """``native.performance`` end to end, and what it produces."""

    def test_it_completes_and_reports_throughput_ttft_and_load(self, environment: Any) -> None:
        detail = _run_suite(environment, "native.performance")
        assert detail.run.status == "completed"
        values = {
            metric.metric_key: metric
            for metric in detail.metrics
            if metric.run_test_id is None and metric.unavailable_reason is None
        }
        # 512 prompt tokens in 256 ms, and 64 output tokens in 2000 ms.
        assert values["prompt_tokens_per_second"].numeric_value == pytest.approx(2000.0)
        assert values["decode_tokens_per_second"].numeric_value == pytest.approx(32.0)
        assert values["ttft_ms"].numeric_value > 0

    def test_a_case_larger_than_the_served_context_is_skipped_with_its_reason(
        self, environment: Any
    ) -> None:
        detail = _run_suite(environment, "native.performance")
        test = next(t for t in detail.tests if t.test_key == "performance.prompt_processing")
        samples = list_samples(environment.database, test.id)
        skipped = [sample for sample in samples if sample.status == "skipped"]
        # The fake advertises a 32 768-token context. Each case asks for its prompt plus room to
        # answer in, so the 32 768- and 65 536-token cases both need more than it serves.
        assert [sample.case_id for sample in skipped] == ["prompt-32768", "prompt-65536"]
        assert all(sample.error_code == "CONTEXT_LIMIT_EXCEEDED" for sample in skipped)
        assert "32768" in (skipped[0].error_text or "")
        assert all(sample.score is None for sample in skipped)

    def test_a_skipped_sample_is_excluded_but_still_counted(self, environment: Any) -> None:
        detail = _run_suite(environment, "native.performance")
        test = next(t for t in detail.tests if t.test_key == "performance.prompt_processing")
        metric = next(
            m
            for m in detail.metrics
            if m.run_test_id == test.id and m.metric_key == "prompt_tokens_per_second"
        )
        assert metric.sample_count == 7
        assert metric.excluded_count == 2

    def test_every_sample_names_the_prompt_record_that_produced_it(self, environment: Any) -> None:
        detail = _run_suite(environment, "native.performance")
        test = next(t for t in detail.tests if t.test_key == "performance.decode_throughput")
        samples = list_samples(environment.database, test.id)
        assert samples
        assert all(sample.prompt_id == "benchmarks.performance.probe" for sample in samples)
        assert all(sample.prompt_version == "1.0.0" for sample in samples)

    def test_streamed_tests_record_a_first_token_time_and_non_streamed_ones_do_not(
        self, environment: Any
    ) -> None:
        detail = _run_suite(environment, "native.performance")
        streamed = next(t for t in detail.tests if t.test_key == "performance.streaming_latency")
        blocking = next(t for t in detail.tests if t.test_key == "performance.decode_throughput")
        assert all(
            sample.client_ttft_ms is not None
            for sample in list_samples(environment.database, streamed.id)
        )
        assert all(
            sample.client_ttft_ms is None
            for sample in list_samples(environment.database, blocking.id)
        )

    def test_cold_load_and_the_warm_tests_do_not_average_together(self, environment: Any) -> None:
        detail = _run_suite(environment, "native.performance")
        run_level = next(
            m for m in detail.metrics if m.run_test_id is None and m.metric_key == "total_ms"
        )
        # ``total_ms`` is declared by a warm test (combined_request) and by the cold one.
        assert run_level.numeric_value is None
        assert run_level.unavailable_reason == "cold_and_warm_not_comparable"

    def test_the_token_economy_suite_runs_and_reports_per_success_cost(
        self, environment: Any
    ) -> None:
        detail = _run_suite(environment, "native.token_economy")
        assert detail.run.status == "completed"
        metric = next(
            m
            for m in detail.metrics
            if m.run_test_id is None and m.metric_key == "output_tokens_per_success"
        )
        assert metric.numeric_value == pytest.approx(64.0)
        chars = next(
            m for m in detail.metrics if m.run_test_id is None and m.metric_key == "output_chars"
        )
        # Benchmark catalog §3.3: token counts are never shown without the size counts beside them.
        assert chars.numeric_value is not None


class TestTelemetryPersistence:
    """Rows exist only for the window they describe, and never double-count a host field."""

    def test_a_run_with_telemetry_records_host_rows(self, environment: Any) -> None:
        detail = _run_suite(
            environment,
            "native.echo",
            collector=_collector(gpus=1),
            telemetry=TelemetrySettings(interval_ms=10),
        )
        series = load_series(environment.database, detail.run.id)
        assert series.sample_count > 0
        assert len(series.gpus) == 1

    def test_no_rows_exist_before_the_run_and_none_appear_after_it(self, environment: Any) -> None:
        collector = _collector(gpus=1)
        summary = create_run(
            environment.database,
            environment.provider,
            collector,
            environment.registry,
            model_ref=environment.model_ref,
            suite_key="native.echo",
            execution=_execution(),
        )
        with environment.database.read() as session:
            assert TelemetryRepository().count_for_run(session, summary.id) == 0
        RunScheduler(
            environment.database,
            environment.provider,
            registry=environment.registry,
            collector=collector,
            telemetry=TelemetrySettings(interval_ms=10),
        ).run_once()
        with environment.database.read() as session:
            after = TelemetryRepository().count_for_run(session, summary.id)
        assert after > 0
        # The recorder is stopped inside ``execute_run``'s ``finally``; nothing may write later.
        time.sleep(0.1)
        with environment.database.read() as session:
            assert TelemetryRepository().count_for_run(session, summary.id) == after

    def test_two_gpus_produce_one_host_row_per_sample_and_one_row_per_device(
        self, environment: Any
    ) -> None:
        summary = create_run(
            environment.database,
            environment.provider,
            environment.collector,
            environment.registry,
            model_ref=environment.model_ref,
            suite_key="native.echo",
            execution=_execution(),
        )
        recorder = TelemetryRecorder(
            environment.database, summary.id, _collector(gpus=2), interval_seconds=0.01
        )
        recorder.start()
        deadline = time.monotonic() + 5.0
        while recorder.written < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        recorder.stop()

        with environment.database.read() as session:
            repository = TelemetryRepository()
            hosts = repository.list_for_run(session, summary.id)
            gpus = repository.list_gpu_for_run(session, summary.id)
        assert len(hosts) >= 3
        # One GPU row per device per host row — never one host row per device, which would
        # double-count every host field on this machine (ADR-0027 §4).
        assert len(gpus) == 2 * len(hosts)
        assert {row.gpu_index for row in gpus} == {0, 1}
        parents = [row.telemetry_sample_id for row in gpus]
        assert all(parents.count(host.id) == 2 for host in hosts)
        # Host fields live on the host row only; the GPU rows carry no CPU column at all.
        assert all(host.cpu_percent == pytest.approx(1.0) for host in hosts)

    def test_multi_gpu_without_reported_placement_refuses_memory_and_energy_figures(
        self, environment: Any
    ) -> None:
        detail = _run_suite(
            environment,
            "native.echo",
            collector=_collector(gpus=2),
            telemetry=TelemetrySettings(interval_ms=10),
        )
        vram = next(m for m in detail.metrics if m.metric_key == "peak_vram_bytes")
        assert vram.numeric_value is None
        assert vram.unavailable_reason == "multi_gpu_placement_unknown"
        assert detail.run.multi_gpu_visible is True

    def test_telemetry_is_cascade_deleted_with_its_run(self, environment: Any) -> None:
        detail = _run_suite(
            environment,
            "native.echo",
            collector=_collector(gpus=1),
            telemetry=TelemetrySettings(interval_ms=10),
        )
        with environment.database.read() as session:
            assert TelemetryRepository().count_for_run(session, detail.run.id) > 0
        with environment.database.write() as session:
            session.delete(session.get(Run, detail.run.id))
        with environment.database.read() as session:
            repository = TelemetryRepository()
            assert repository.count_for_run(session, detail.run.id) == 0
            assert repository.list_gpu_for_run(session, detail.run.id) == []


class TestSamplingOverheadCalibration:
    """Acceptance criterion 3: the overhead is measured, is small, and is stored on the run."""

    def test_it_is_measured_against_the_configured_interval(self) -> None:
        calibration = calibrate_sampling_overhead(_collector(), interval_ms=1000, observations=3)
        assert calibration.observations == 3
        assert calibration.interval_ms == 1000
        assert calibration.overhead_percent == pytest.approx(
            calibration.mean_collect_ms / 1000 * 100
        )

    @pytest.mark.parametrize(("interval", "observations"), [(0, 3), (-1, 3), (1000, 0)])
    def test_impossible_calibrations_are_refused(self, interval: float, observations: int) -> None:
        with pytest.raises(ValueError, match="must be"):
            calibrate_sampling_overhead(
                _collector(), interval_ms=interval, observations=observations
            )

    def test_the_run_stores_it_and_it_is_within_the_one_percent_budget(
        self, environment: Any
    ) -> None:
        detail = _run_suite(
            environment,
            "native.echo",
            collector=_collector(gpus=1),
            telemetry=TelemetrySettings(interval_ms=1000),
        )
        assert detail.run.telemetry_overhead_percent is not None
        assert detail.run.telemetry_overhead_percent <= 1.0


class TestIdleDetection:
    """Spec §13's three outcomes, each of which leaves a record."""

    def test_a_quiet_machine_proceeds(self) -> None:
        outcome = wait_for_idle(
            _collector(cpu_percent=1.0),
            threshold_percent=10.0,
            required_samples=2,
            timeout_seconds=5.0,
            sleep=lambda _seconds: None,
        )
        assert outcome.idle is True
        assert outcome.consecutive_idle_samples >= 2

    def test_a_zero_threshold_disables_the_check_without_claiming_the_machine_was_quiet(
        self,
    ) -> None:
        outcome = wait_for_idle(
            _collector(), threshold_percent=0.0, required_samples=1, timeout_seconds=1.0
        )
        assert outcome.idle is True
        assert outcome.disabled is True

    def test_a_busy_machine_times_out_and_reports_what_it_saw(self) -> None:
        outcome = wait_for_idle(
            _collector(cpu_percent=95.0),
            threshold_percent=10.0,
            required_samples=3,
            timeout_seconds=0.0,
            sleep=lambda _seconds: None,
        )
        assert outcome.idle is False
        assert outcome.as_detail()["cpu_percent"] == pytest.approx(95.0)

    def test_warn_proceeds_and_records_measured_while_busy_with_the_numbers(
        self, environment: Any
    ) -> None:
        detail = _run_suite(
            environment,
            "native.echo",
            collector=_collector(cpu_percent=95.0),
            telemetry=TelemetrySettings(interval_ms=50),
            execution=_execution(
                idle_gpu_threshold_percent=10.0,
                idle_wait_timeout_seconds=0.0,
                on_idle_timeout="warn",
            ),
        )
        assert detail.run.status == "completed"
        recorded = next(
            item for item in detail.run.degradations if item["kind"] == "measured_while_busy"
        )
        assert recorded["detail"]["cpu_percent"] == pytest.approx(95.0)
        assert recorded["detail"]["threshold_percent"] == pytest.approx(10.0)

    def test_refuse_fails_the_run_with_the_numbers(self, environment: Any) -> None:
        detail = _run_suite(
            environment,
            "native.echo",
            collector=_collector(cpu_percent=95.0),
            telemetry=TelemetrySettings(interval_ms=50),
            execution=_execution(
                idle_gpu_threshold_percent=10.0,
                idle_wait_timeout_seconds=0.0,
                on_idle_timeout="refuse",
            ),
        )
        assert detail.run.status == "failed"
        assert detail.run.error_code == "INSUFFICIENT_RESOURCES"
        assert "95.0" in (detail.run.error_text or "")


class TestRepeat:
    """The reproduction workflow: the document is stored, diffable, and guards the repeat."""

    def test_the_fingerprint_document_is_stored_in_full(self, environment: Any) -> None:
        detail = _run_suite(environment, "native.token_economy")
        document = detail.run.fingerprint_document
        assert set(document) == {
            "model",
            "runtime_profile_hash",
            "provider",
            "machine_fingerprint",
            "environment",
            "benchmark",
            "execution",
            "application",
        }
        assert document["benchmark"]["prompt_subset_hash"].startswith("sha256:")
        assert document["execution"]["gpu_index"] == 0

    def test_the_pack_is_recorded_as_provenance_but_is_not_in_the_document(
        self, environment: Any
    ) -> None:
        detail = _run_suite(environment, "native.token_economy")
        assert detail.run.prompt_pack_id == "freeweight.core"
        assert detail.run.prompt_pack_hash is not None
        assert detail.run.prompt_pack_hash not in str(detail.run.fingerprint_document)

    def test_repeating_an_unchanged_environment_produces_an_identical_document(
        self, environment: Any
    ) -> None:
        first = _run_suite(environment, "native.echo")
        repeated = repeat_run(
            environment.database,
            environment.provider,
            environment.collector,
            environment.registry,
            run_ref=first.run.id,
        )
        second = get_run(environment.database, repeated.id)
        assert diff_documents(first.run.fingerprint_document, second.run.fingerprint_document) == ()
        assert second.run.reproducibility_fingerprint == first.run.reproducibility_fingerprint

    @staticmethod
    def _rediscover_with_a_new_digest(environment: Any) -> FakeProvider:
        """Replace the stored model's weights, keeping its name (ADR-0008: a new identity)."""
        moved = FakeModel(name="fake-model:8b-q8_0", digest="sha256:" + "9" * 64, max_context=32768)
        provider = FakeProvider(_script(models=(moved,)), seed=7)
        discover_models(environment.database, provider, now=utc_now())
        return provider

    def test_it_refuses_when_the_model_digest_has_changed_and_explains_why(
        self, environment: Any
    ) -> None:
        first = _run_suite(environment, "native.echo")
        provider = self._rediscover_with_a_new_digest(environment)
        with pytest.raises(RepeatRefused) as caught:
            repeat_run(
                environment.database,
                provider,
                environment.collector,
                environment.registry,
                run_ref=first.run.id,
            )
        blockers = caught.value.details["blockers"]
        assert [item["reason"] for item in blockers] == ["model_digest_changed"]
        assert "different model" in blockers[0]["explanation"]

    def test_force_proceeds_and_records_the_divergence(self, environment: Any) -> None:
        first = _run_suite(environment, "native.echo")
        provider = self._rediscover_with_a_new_digest(environment)
        summary = repeat_run(
            environment.database,
            provider,
            environment.collector,
            environment.registry,
            run_ref=first.run.id,
            force=True,
        )
        repeated = get_run(environment.database, summary.id)
        forced = next(item for item in repeated.run.degradations if item["kind"] == "repeat_forced")
        assert forced["detail"]["divergences"][0]["field"] == "model.artifact_digest"
        # The two runs are not claimed to be the same measurement.
        assert repeated.run.reproducibility_fingerprint != first.run.reproducibility_fingerprint


class TestAcceptanceCriteria:
    """The phase's own four criteria, stated as assertions."""

    def test_criterion_1_a_run_reports_throughput_ttft_peak_vram_and_a_telemetry_series(
        self, environment: Any
    ) -> None:
        detail = _run_suite(
            environment,
            "native.performance",
            collector=_collector(gpus=1),
            telemetry=TelemetrySettings(interval_ms=10),
        )
        keys = {metric.metric_key for metric in detail.metrics}
        assert {
            "prompt_tokens_per_second",
            "decode_tokens_per_second",
            "ttft_ms",
            "peak_vram_bytes",
        } <= keys
        assert load_series(environment.database, detail.run.id).sample_count > 0

    @pytest.mark.performance
    def test_criterion_2_overhead_outside_the_provider_call_is_under_ten_ms_per_sample(
        self, environment: Any
    ) -> None:
        # The fake returns immediately, so a sample's whole observed wall time *is* FreeWeight's
        # overhead plus a negligible provider call — the only way to measure this without a model.
        detail = _run_suite(environment, "native.token_economy")
        for test in detail.tests:
            for sample in list_samples(environment.database, test.id):
                if sample.client_wall_ms is not None:
                    assert sample.client_wall_ms <= 10.0

    def test_criterion_3_the_sampling_overhead_is_stored_on_the_run(self, environment: Any) -> None:
        detail = _run_suite(
            environment,
            "native.echo",
            collector=_collector(gpus=1),
            telemetry=TelemetrySettings(interval_ms=1000),
        )
        assert detail.run.telemetry_overhead_percent is not None

    def test_criterion_4_two_runs_of_the_same_subject_agree(self, environment: Any) -> None:
        first = _run_suite(environment, "native.performance")
        second = _run_suite(environment, "native.performance")

        def values(detail: Any) -> dict[str, float | None]:
            return {
                metric.metric_key: metric.numeric_value
                for metric in detail.metrics
                if metric.run_test_id is None
            }

        before, after = values(first), values(second)
        assert before.keys() == after.keys()
        for key, value in before.items():
            if value is None or after[key] is None:
                assert value == after[key]
            else:
                # A deterministic provider and a frozen configuration leave no room for drift;
                # the documented tolerance is only ever needed against a real model.
                assert after[key] == pytest.approx(value, rel=0.05)


def test_unsupported_never_becomes_a_number_on_the_way_into_a_metric() -> None:
    """A guard on the sentinel itself: the arithmetic that would fabricate a value still raises."""
    with pytest.raises(TypeError):
        float(UNSUPPORTED)
