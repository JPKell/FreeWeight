"""freeweight.services.runs — creating, reading, cancelling and executing a run.

Everything the web routes and the CLI commands do to a run goes through this module, which is why
``freeweight run start`` and ``POST /api/v1/runs`` cannot disagree about what a run is: they call
the same function.

**The order of writes is the whole design.** For every sample: call the provider, write the
sample, commit, *then* publish the event that announces it. For every test: write every sample,
then aggregate. For the run: aggregate, then complete. The phase's own risk note names the
opposite ordering as a likely failure mode — "partial aggregates written before samples are
durable" — and the only defence against it is that aggregation reads the samples table rather than
an in-memory accumulator. It cannot run ahead of data that is not there, because it has no other
source.

**A failure never propagates further than it should.** A failed sample is stored with
``score=NULL`` and its error, and is excluded from the aggregate while remaining in the counts; it
does not fail its test. A failed test is recorded on its row; it does not fail the run. Only a
failure of the machinery itself — the suite cannot be loaded, the model cannot be resolved — fails
a run (spec §13).

**Cancellation is checked at every boundary**: before preparing, before warming, before each test,
before each case, before each repetition, and before aggregating. Between two checks the only
blocking call is one provider generation, bounded by ``test_timeout_seconds``.

**Provenance is assembled once, in the domain.** The fingerprint document of
[Machine Identity §4](../../../../docs/architecture/machine-identity-and-reproducibility.md) is
built by :mod:`freeweight.domain.provenance` from values this module resolves — the model and its
digest, the runtime profile, the provider and its version, the machine and the drift-sensitive
environment, the benchmark's manifest hash and its ``prompt_subset_hash``, and the execution
parameters including the served context with its source and the target GPU index. The document is
stored, not just its hash, which is what lets ``run repeat --check`` show a field-level diff
instead of two hex strings that differ.

**Telemetry is recorded for the duration of a run and for no longer.** The sampler starts after
the idle check and stops before the run is marked terminal, so every persisted observation lies
inside the window it describes. What the sampler itself costs is measured before the first
provider call and stored on the run (spec §15).

**Aggregation reads the samples table and refuses to mix cold with warm.** It is
:mod:`freeweight.domain.aggregation`'s job; this module hands it what it read and writes back the
rows it produced.
"""

from __future__ import annotations

import json
import logging
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import (
    UNSUPPORTED,
    ConflictError,
    Measurement,
    ModelIdentity,
    NotFoundError,
    ProviderKind,
    RuntimeProfile,
    SuiteError,
    ValidationError,
    canonical_json,
    elapsed_ms,
    is_supported,
    monotonic_ns,
    sha256_of,
    utc_now,
)
from modelrack import GenerationRequest, Message, Role, SamplingParameters
from modelrack.errors import ProviderError
from modelrack.streaming import StreamCompleted, StreamFailed, ThinkingDelta, TokenDelta

from freeweight.__about__ import __version__
from freeweight.benchmarks.echo import benchmark as echo_benchmark
from freeweight.benchmarks.performance import benchmark as performance_benchmark
from freeweight.benchmarks.token_economy import benchmark as token_economy_benchmark
from freeweight.domain.aggregation import SampleGroup, aggregate_run
from freeweight.domain.benchmark import Benchmark, BenchmarkRegistry, BenchmarkTest
from freeweight.domain.metrics import MeasurementClass, SampleFacts
from freeweight.domain.provenance import (
    Degradation,
    ServedContext,
    build_fingerprint_document,
    case_selection_hash,
    check_repeatable,
    compute_fingerprint,
    divergence_degradation,
    resolve_served_context,
)
from freeweight.domain.run_state import (
    RunStatus,
    TestStatus,
    cancellation_target,
    require_run_transition,
    require_test_transition,
)
from freeweight.domain.scoring import ScoreResult
from freeweight.infrastructure.db.errors import DatabaseUnavailable
from freeweight.infrastructure.db.repositories.model_descriptors import ModelDescriptorRepository
from freeweight.infrastructure.db.repositories.models import ModelRepository
from freeweight.infrastructure.db.repositories.runs import (
    BenchmarkRepository,
    MetricValueRepository,
    RunRepository,
    RunTestRepository,
    RuntimeProfileRepository,
    SampleRepository,
)
from freeweight.services.events import RunEventPublisher
from freeweight.services.machine import profile_machine
from freeweight.services.prompts import PromptLibrary, load_pack
from freeweight.services.telemetry_recording import (
    TelemetryRecorder,
    calibrate_sampling_overhead,
    load_window,
    summarize_gpu_telemetry,
    wait_for_idle,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from baseaicore.timeutil import Clock
    from modelrack.provider import Provider
    from sqlalchemy.orm import Session
    from sweatmeter import TelemetryCollector

    from freeweight.config import ExecutionSettings, TelemetrySettings
    from freeweight.services.database import Database

__all__ = [
    "ExecutionConfig",
    "InsufficientResources",
    "MetricSummary",
    "RepeatRefused",
    "RunDetail",
    "RunNotFound",
    "RunSummary",
    "RunTestSummary",
    "SampleSummary",
    "build_registry",
    "cancel_run",
    "create_run",
    "execute_run",
    "get_run",
    "list_runs",
    "list_samples",
    "repeat_run",
    "resume_run",
    "shipped_prompt_library",
]

logger = logging.getLogger(__name__)


class RunNotFound(NotFoundError):
    """No run matches the given id or prefix (spec §13, ``RUN_NOT_FOUND``)."""

    code: ClassVar[str] = "RUN_NOT_FOUND"


class InsufficientResources(SuiteError):
    """The machine cannot give this run the conditions it was configured to need.

    Raised by the idle check under ``on_idle_timeout = "refuse"``, carrying the utilization that
    was actually observed. It fails the run deliberately: the user asked not to measure a busy
    machine, and producing the numbers anyway would answer a question they did not ask (spec §13).
    """

    code: ClassVar[str] = "INSUFFICIENT_RESOURCES"


class RepeatRefused(ConflictError):
    """The environment can no longer satisfy a recorded run's configuration.

    Carries every blocker in ``details["blockers"]`` — the field that moved, what was recorded,
    what is here now, and one sentence on why it matters. ``--force`` proceeds past all of them
    and records the divergence on the new run
    ([Machine Identity §7](../../../../docs/architecture/machine-identity-and-reproducibility.md)).
    """

    code: ClassVar[str] = "REPEAT_REFUSED"


@contextmanager
def _translated() -> Iterator[None]:
    """Translate raw driver failures into the suite's error hierarchy.

    Identical in purpose to :func:`freeweight.services.models._translated`: without it an
    unmigrated database reaches a route as ``sqlalchemy.exc.OperationalError`` and 500s a page
    that already has an error state to render. A :class:`~baseaicore.SuiteError` passes through
    unchanged — a provider error raised inside the block is not a database failure.
    """
    try:
        yield
    except SuiteError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(f"Could not read the database: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """The execution parameters one run actually used, frozen into its record.

    Resolved once at run creation from ``[execution]`` plus the request's overrides, stored as
    ``runs.effective_config_json``, and read back by the executor — never re-resolved from live
    configuration. That is what makes a run reproducible after someone edits ``config.toml``.

    Attributes:
        measured_repetitions: How many scored samples each case produces.
        warmup_repetitions: Unscored generations run before measurement, to take first-call model
            loading out of the numbers. Recorded here; the warm phase runs them.
        test_timeout_seconds: Per-provider-call timeout.
        run_timeout_seconds: Total budget for the run.
        cooldown_seconds: Pause between tests. Honoured from Phase 6, recorded from Phase 5 so a
            run's record is complete from the first one.
        randomize_case_order: Whether case order is shuffled within a test.
        seed: The seed for that shuffle and for the provider's own sampling. ``0`` is a real seed,
            not "unset" — every run records the seed it used.
        store_responses: Whether full response text is stored beside its hash (spec §14: hashes by
            default).
        temperature: Sampling temperature; ``None`` leaves the provider's default, which is
            recorded as ``None`` rather than guessed.
        top_p: Nucleus sampling parameter, or ``None``.
        max_output_tokens: Output cap, or ``None``.
        gpu_index: The device this run's metrics are attributed to. One device, named — there is
            no machine-wide GPU figure in this system
            ([ADR-0027 §3](../../../../docs/adr/0027-multi-gpu-semantics.md)).
        idle_gpu_threshold_percent: The utilization below which the machine counts as quiet.
            ``0`` disables the check.
        idle_required_samples: Consecutive quiet observations required before measuring.
        idle_wait_timeout_seconds: How long to wait for them.
        on_idle_timeout: ``warn`` proceeds and records ``measured_while_busy`` with the observed
            numbers; ``refuse`` fails the run with ``INSUFFICIENT_RESOURCES`` and those same
            numbers. Silently proceeding is not one of the options (spec §13).
    """

    measured_repetitions: int
    warmup_repetitions: int
    test_timeout_seconds: float
    run_timeout_seconds: float
    cooldown_seconds: float
    randomize_case_order: bool
    seed: int
    store_responses: bool
    temperature: float | None
    top_p: float | None
    max_output_tokens: int | None
    gpu_index: int = 0
    idle_gpu_threshold_percent: float = 0.0
    idle_required_samples: int = 3
    idle_wait_timeout_seconds: float = 120.0
    on_idle_timeout: str = "warn"

    @classmethod
    def resolve(
        cls,
        defaults: ExecutionSettings,
        *,
        measured_repetitions: int | None = None,
        seed: int | None = None,
        store_responses: bool | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        gpu_index: int | None = None,
    ) -> ExecutionConfig:
        """Resolve the application defaults against this run's overrides.

        This is the *execution-parameter* precedence chain of
        [Configuration Standards §1.1](../../../../docs/standards/configuration-standards.md) —
        application defaults, then the run's overrides — and it is a different axis from the one
        that loaded ``settings`` in the first place. Its resolved output is frozen into the run
        record, so editing ``config.toml`` tomorrow changes nothing about a run measured today.

        Args:
            defaults: ``settings.execution``.
            measured_repetitions: Override, or ``None`` to take the default.
            seed: Override, or ``None`` to take the default.
            store_responses: Override, or ``None`` to take the default.
            temperature: Sampling temperature for this run, or ``None`` for the provider default.
            max_output_tokens: Output cap for this run, or ``None`` for the provider default.
            gpu_index: The device to attribute this run's metrics to, or ``None`` to take the
                configured default.

        Returns:
            The resolved configuration.

        Raises:
            ValidationError: ``measured_repetitions`` is below 1. A run with no measured
                repetition measures nothing, and would produce a suite of empty aggregates rather
                than an error.
        """
        repetitions = (
            defaults.measured_repetitions if measured_repetitions is None else measured_repetitions
        )
        if repetitions < 1:
            raise ValidationError(
                f"measured_repetitions must be at least 1; got {repetitions}.",
                details={"field": "execution.measured_repetitions", "value": repetitions},
            )
        return cls(
            measured_repetitions=repetitions,
            warmup_repetitions=defaults.warmup_repetitions,
            test_timeout_seconds=defaults.test_timeout_seconds,
            run_timeout_seconds=defaults.run_timeout_seconds,
            cooldown_seconds=defaults.cooldown_seconds,
            randomize_case_order=defaults.randomize_case_order,
            seed=defaults.seed if seed is None else seed,
            store_responses=(
                defaults.store_responses if store_responses is None else store_responses
            ),
            temperature=temperature,
            top_p=None,
            max_output_tokens=max_output_tokens,
            gpu_index=defaults.gpu_index if gpu_index is None else gpu_index,
            idle_gpu_threshold_percent=defaults.idle_gpu_threshold_percent,
            idle_required_samples=defaults.idle_required_samples,
            idle_wait_timeout_seconds=defaults.idle_wait_timeout_seconds,
            on_idle_timeout=defaults.on_idle_timeout,
        )

    def to_json(self) -> dict[str, Any]:
        """Render as the JSON stored in ``runs.effective_config_json``."""
        return {
            "measured_repetitions": self.measured_repetitions,
            "warmup_repetitions": self.warmup_repetitions,
            "test_timeout_seconds": self.test_timeout_seconds,
            "run_timeout_seconds": self.run_timeout_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "randomize_case_order": self.randomize_case_order,
            "seed": self.seed,
            "store_responses": self.store_responses,
            "sampling": {
                "temperature": self.temperature,
                "top_p": self.top_p,
                "max_output_tokens": self.max_output_tokens,
            },
            "gpu_index": self.gpu_index,
            "idle": {
                "gpu_threshold_percent": self.idle_gpu_threshold_percent,
                "required_samples": self.idle_required_samples,
                "wait_timeout_seconds": self.idle_wait_timeout_seconds,
                "on_timeout": self.on_idle_timeout,
            },
        }

    @classmethod
    def from_json(cls, body: Any) -> ExecutionConfig:  # noqa: ANN401 — a stored JSON column
        """Rebuild from ``runs.effective_config_json``.

        Tolerant of a missing key, deliberately: a run written by an older build must still be
        readable and resumable by a newer one, and a run that cannot be read is a run whose
        samples cannot be shown.
        """
        data: dict[str, Any] = body if isinstance(body, dict) else {}
        raw_sampling = data.get("sampling")
        sampling: dict[str, Any] = raw_sampling if isinstance(raw_sampling, dict) else {}
        raw_idle = data.get("idle")
        idle: dict[str, Any] = raw_idle if isinstance(raw_idle, dict) else {}
        return cls(
            measured_repetitions=int(data.get("measured_repetitions", 1)),
            warmup_repetitions=int(data.get("warmup_repetitions", 0)),
            test_timeout_seconds=float(data.get("test_timeout_seconds", 600.0)),
            run_timeout_seconds=float(data.get("run_timeout_seconds", 86400.0)),
            cooldown_seconds=float(data.get("cooldown_seconds", 0.0)),
            randomize_case_order=bool(data.get("randomize_case_order", False)),
            seed=int(data.get("seed", 0)),
            store_responses=bool(data.get("store_responses", False)),
            temperature=sampling.get("temperature"),
            top_p=sampling.get("top_p"),
            max_output_tokens=sampling.get("max_output_tokens"),
            gpu_index=int(data.get("gpu_index", 0)),
            idle_gpu_threshold_percent=float(idle.get("gpu_threshold_percent", 0.0)),
            idle_required_samples=int(idle.get("required_samples", 3)),
            idle_wait_timeout_seconds=float(idle.get("wait_timeout_seconds", 120.0)),
            on_idle_timeout=str(idle.get("on_timeout", "warn")),
        )


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One run as every surface shows it: the list page, the API, the CLI.

    Carries its provenance, not only its identity: the served context and *how that was
    established*, the device its metrics are attributed to, whether more than one was visible, what
    telemetry sampling cost, the installed prompt pack, the degradations recorded against it, and
    the full fingerprint document. Machine Identity §4 rule 2 requires the document to be stored
    rather than only its hash, and a summary that dropped it would put every surface one extra
    query away from the thing that explains the number it is showing.
    """

    id: str
    status: str
    suite_key: str
    suite_version: str
    model_canonical_id: str
    label: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    reproducibility_fingerprint: str
    error_code: str | None
    error_text: str | None
    served_context: int | None = None
    served_context_source: str | None = None
    gpu_index: int | None = None
    multi_gpu_visible: bool = False
    telemetry_overhead_percent: float | None = None
    prompt_pack_id: str | None = None
    prompt_pack_version: str | None = None
    prompt_pack_hash: str | None = None
    degradations: tuple[Mapping[str, Any], ...] = ()
    fingerprint_document: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """Whether this run has finished, one way or another."""
        return RunStatus(self.status) in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }


@dataclass(frozen=True, slots=True)
class RunTestSummary:
    """One test of one run, with its progress and the reason it was skipped if it was."""

    id: str
    test_key: str
    test_name: str
    status: str
    skip_reason: str | None
    completed_cases: int
    total_cases: int
    repetitions: int
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_text: str | None


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """One aggregate metric, with the counts that make its exclusions visible."""

    metric_key: str
    run_test_id: str | None
    numeric_value: float | None
    unavailable_reason: str | None
    unit: str
    aggregation: str
    higher_is_better: bool
    sample_count: int | None
    excluded_count: int | None
    gpu_index: int | None = None
    stddev: float | None = None
    coefficient_of_variation: float | None = None


@dataclass(frozen=True, slots=True)
class SampleSummary:
    """One raw sample, as the drill-down page and the API show it.

    ``prompt_id`` and ``prompt_version`` are here because prompt standards §4 requires a benchmark
    result to be able to name the exact prompt that produced it and re-render it; a drill-down that
    showed the response but not which prompt version asked for it cannot do that.
    """

    id: str
    case_id: str
    ordinal: int
    repetition: int
    status: str
    score: float | None
    score_method: str | None
    response_hash: str | None
    response_text: str | None
    output_chars: int | None
    input_tokens: int | None
    output_tokens: int | None
    client_wall_ms: float | None
    finish_reason: str | None
    error_code: str | None
    error_text: str | None
    detail: dict[str, Any]
    prompt_id: str | None = None
    prompt_version: str | None = None
    client_ttft_ms: float | None = None


@dataclass(frozen=True, slots=True)
class RunDetail:
    """A run with everything the detail page renders: its tests and its aggregate metrics."""

    run: RunSummary
    tests: tuple[RunTestSummary, ...]
    metrics: tuple[MetricSummary, ...]
    effective_config: ExecutionConfig
    last_event_sequence: int


@lru_cache(maxsize=1)
def shipped_prompt_library() -> PromptLibrary:
    """Load and validate this build's prompt pack, once per process.

    Cached because loading parses and validates every record, and because two benchmarks holding
    two separately-loaded copies of the same pack could disagree about a hash after a user dropped
    an override into place mid-process — which would put two different ``prompt_subset_hash``
    values into two runs that used the same prompt.

    Raises:
        PromptPackInvalid: The pack is malformed or its manifest is stale. Loaded at startup by
            :func:`freeweight.bootstrap.bootstrap` so this surfaces as a startup failure rather
            than as a surprise in the middle of a run (prompt standards §5).
    """
    return load_pack()


def build_registry(library: PromptLibrary | None = None) -> BenchmarkRegistry:
    """Build the registry of benchmarks this build can run.

    The one list. A suite that is not named here cannot be run, which is the point: benchmark
    availability is a deliberate, reviewable fact rather than a consequence of which modules
    happened to be imported. Phase 7 adds the quality suites here.

    Args:
        library: The prompt pack the suites render from, or ``None`` for this build's own. Every
            suite gets the *same* library instance, so two suites can never disagree about a
            prompt's hash.

    Returns:
        The registry.

    Raises:
        ValueError: A suite's manifest declares a ``prompt_subset_hash`` that does not match the
            installed pack. Refused at registry-build time — which is startup — because a suite
            whose provenance is wrong must not be runnable at all.
    """
    pack = library if library is not None else shipped_prompt_library()
    return BenchmarkRegistry(
        [
            echo_benchmark.build(),
            performance_benchmark.build(pack),
            token_economy_benchmark.build(pack),
        ]
    )


def _json_safe(value: object) -> Any:  # noqa: ANN401 — a JSON value has no narrower type
    """Round-trip a structure through canonical JSON into plain, JSON-safe Python."""
    return json.loads(canonical_json(value))


def _resolve_run(session: Session, run_ref: str) -> Any:  # noqa: ANN401 — an ORM row, kept internal
    """Resolve a full ULID or an unambiguous prefix to one run row.

    Raises:
        RunNotFound: Nothing matches ``run_ref``.
        ValidationError: ``run_ref`` is a prefix matching more than one run. Refused by naming the
            candidates rather than resolved by picking one (CLI standards §7).
    """
    repository = RunRepository()
    exact = repository.get_by_id(session, run_ref)
    if exact is not None:
        return exact
    matches = repository.get_by_id_prefix(session, run_ref)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValidationError(
            f"{run_ref!r} matches {len(matches)} runs; use a longer prefix.",
            details={"run": run_ref, "candidates": [row.id for row in matches]},
        )
    raise RunNotFound(f"No run matches {run_ref!r}.", details={"run": run_ref})


def _summarize_run(session: Session, run: Any) -> RunSummary:  # noqa: ANN401 — an ORM row
    """Build a :class:`RunSummary` from a run row, joining in the names a person reads."""
    suite = session.get(_suite_model(), run.suite_id)
    model = ModelRepository().get_by_id(session, run.model_id)
    return RunSummary(
        id=run.id,
        status=run.status,
        suite_key=suite.key if suite is not None else "unknown",
        suite_version=suite.version if suite is not None else "unknown",
        model_canonical_id=model.canonical_id if model is not None else "unknown",
        label=run.label,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        reproducibility_fingerprint=run.reproducibility_fingerprint,
        error_code=run.error_code,
        error_text=run.error_text,
        served_context=run.served_context,
        served_context_source=run.served_context_source,
        gpu_index=run.gpu_index,
        multi_gpu_visible=bool(run.multi_gpu_visible),
        telemetry_overhead_percent=run.telemetry_overhead_percent,
        prompt_pack_id=run.prompt_pack_id,
        prompt_pack_version=run.prompt_pack_version,
        prompt_pack_hash=run.prompt_pack_hash,
        degradations=tuple(
            item for item in (run.degradations_json or ()) if isinstance(item, dict)
        ),
        fingerprint_document=(
            dict(run.fingerprint_document_json)
            if isinstance(run.fingerprint_document_json, dict)
            else {}
        ),
    )


def _suite_model() -> Any:  # noqa: ANN401 — the mapped class, not an instance
    """Return the ``BenchmarkSuite`` mapped class.

    A function rather than a module-level import so that this module's import graph stays
    services → repositories, with ORM classes reached only where a repository does not already
    hand back what is needed.
    """
    from freeweight.infrastructure.db.models_runs import BenchmarkSuite

    return BenchmarkSuite


def _install_benchmark(
    session: Session, benchmark: Benchmark, *, now: datetime
) -> tuple[str, dict[str, str]]:
    """Install this suite version and its tests if they are not installed already.

    Returns:
        ``(suite_id, {test_key: test_row_id})``.
    """
    repository = BenchmarkRepository()
    manifest = benchmark.manifest
    suite = repository.install_suite(
        session,
        key=manifest.key,
        name=manifest.name,
        version=manifest.version,
        category=manifest.category,
        runner=manifest.runner,
        manifest_hash=manifest.manifest_hash,
        manifest_json=_json_safe(manifest.body),
        dataset_hashes_json=_json_safe(dict(manifest.dataset_hashes)),
        license=manifest.license,
        now=now,
    )
    test_ids: dict[str, str] = {}
    for test in benchmark.tests:
        row = repository.install_test(
            session,
            suite_id=suite.id,
            key=test.key,
            name=test.name,
            category=test.category,
            scorer=test.scorer.key,
            config_json=None,
            metric_definitions_json=_json_safe(
                [
                    {
                        "key": metric.key,
                        "unit": metric.unit,
                        "higher_is_better": metric.higher_is_better,
                        "aggregation": metric.aggregation,
                        "description": metric.description,
                    }
                    for metric in test.metrics
                ]
            ),
            requires_json=_json_safe(dict(test.requires)),
        )
        test_ids[test.key] = row.id
    return suite.id, test_ids


def create_run(
    database: Database,
    provider: Provider,
    collector: TelemetryCollector,
    registry: BenchmarkRegistry,
    *,
    model_ref: str,
    suite_key: str,
    execution: ExecutionConfig,
    label: str | None = None,
    extra_degradations: Sequence[Degradation] | None = None,
    clock: Clock = utc_now,
) -> RunSummary:
    """Validate a run request, persist it as ``queued``, and return it.

    Validation happens before anything is written, so a rejected request creates nothing (api.md
    §4). Everything the run needs to be reproducible is resolved here and frozen: the model
    identity and its descriptor snapshot, the runtime profile, the installed suite version, the
    machine, and the effective execution config.

    The machine is profiled and upserted as part of this call
    (:func:`~freeweight.services.machine.profile_machine`), which is why a run started from the
    CLI on a machine the server has never run on still records where it was measured.

    Args:
        database: The application's database handle.
        provider: The configured provider, used only to read its kind and version for provenance —
            no generation happens here.
        collector: The telemetry collector, used to profile this machine.
        registry: The benchmarks this build can run.
        model_ref: A stored ULID or prefix, a canonical ID, or the exact provider model name.
        suite_key: The suite to run, e.g. ``"native.echo"``.
        execution: The resolved execution parameters.
        label: The user's label for this run.
        extra_degradations: Conditions to record on the run before it starts — in practice the
            divergences a ``--force``d repeat chose to proceed past, so the new run's provenance
            says it is not the same measurement rather than quietly claiming it is.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The queued run.

    Raises:
        BenchmarkNotFound: ``suite_key`` names no registered suite.
        ModelNotFound: ``model_ref`` resolves to no stored model. Discovery has to have run first
            — a run records the descriptor snapshot it measured against, and there is none for a
            model this installation has never seen.
        ValidationError: ``model_ref`` is an ambiguous prefix, or the model has no stored
            descriptor.
        DatabaseUnavailable: The database could not be read or written.
    """
    from modelrack.errors import ModelNotFound

    benchmark = registry.get(suite_key)
    machine_profile = profile_machine(database, collector, clock=clock)
    capabilities = _provider_capabilities(provider)
    now = clock()
    with _translated(), database.write() as session:
        model = _lookup_model(session, model_ref)
        if model is None:
            raise ModelNotFound(
                f"No stored model matches {model_ref!r}. Run `freeweight models refresh` first: "
                "a run records the descriptor snapshot it measured against, and this "
                "installation has none for that model.",
                details={"model": model_ref},
            )
        descriptor = ModelDescriptorRepository().latest_for_model(session, model.id)
        if descriptor is None:
            raise ValidationError(
                f"Model {model.canonical_id!r} has no stored descriptor snapshot; run "
                "`freeweight models refresh` to record one before benchmarking it.",
                details={"model": model.canonical_id},
            )
        machine = _machine_row(session, machine_profile.machine_fingerprint)
        runtime_profile = RuntimeProfile()
        profile_row = RuntimeProfileRepository().get_or_create(
            session,
            profile_hash=runtime_profile.profile_hash,
            context_size=runtime_profile.context_size,
            kv_cache_precision=runtime_profile.kv_cache_precision,
            gpu_layers=runtime_profile.gpu_layers,
            flash_attention=runtime_profile.flash_attention,
            threads=runtime_profile.threads,
            batch_size=runtime_profile.batch_size,
            keep_alive=runtime_profile.keep_alive,
            provider_options_json=_json_safe(dict(runtime_profile.provider_options)),
            now=now,
        )
        suite_id, _ = _install_benchmark(session, benchmark, now=now)
        served = resolve_served_context(
            requested_context=runtime_profile.context_size,
            context_configurable=capabilities.context_configurable,
            advertised_max_context=(
                UNSUPPORTED if descriptor.max_context is None else float(descriptor.max_context)
            ),
        )
        library = _benchmark_library(benchmark)
        document = _fingerprint_document(
            model=model,
            descriptor=descriptor,
            runtime_profile=runtime_profile,
            provider_version=_provider_version(provider),
            machine_profile=machine_profile,
            benchmark=benchmark,
            execution=execution,
            served=served,
        )
        degradations = [degradation.as_json() for degradation in (extra_degradations or ())]
        run = RunRepository().insert(
            session,
            machine_id=machine.id,
            model_id=model.id,
            model_descriptor_id=descriptor.id,
            runtime_profile_id=profile_row.id,
            suite_id=suite_id,
            status=RunStatus.QUEUED.value,
            effective_config_json=_json_safe(execution.to_json()),
            reproducibility_fingerprint=compute_fingerprint(document),
            fingerprint_document_json=_json_safe(document),
            provider_kind=model.provider_kind,
            provider_version=document["provider"]["version"],
            application_version=__version__,
            label=label,
            now=now,
            prompt_pack_id=library.pack_id if library is not None else None,
            prompt_pack_version=library.pack_version if library is not None else None,
            prompt_pack_hash=library.pack_hash() if library is not None else None,
            served_context=served.numeric_tokens,
            served_context_source=served.source.value,
            gpu_index=execution.gpu_index,
            multi_gpu_visible=len(machine_profile.gpus) > 1,
            degradations_json=degradations or None,
        )
        summary = _summarize_run(session, run)
    logger.info("run.created", extra={"run_id": summary.id, "suite": suite_key, "model": model_ref})
    return summary


def _provider_capabilities(provider: Provider) -> Any:  # noqa: ANN401 — ProviderCapabilities
    """Return what the provider declares, or an all-``False`` declaration if it cannot say.

    Never raises, for the same reason :func:`_provider_version` does not: a provider that is
    momentarily unreachable must not stop a run being *queued*. An all-``False`` declaration is
    also the honest fallback — a capability that appears by omission is one nobody tested — and it
    only affects whether the served context is recorded as ``configured`` or as ``assumed``.
    """
    from modelrack.provider import ProviderCapabilities

    try:
        return provider.capabilities()
    except ProviderError:
        return ProviderCapabilities()


def _benchmark_library(benchmark: Benchmark) -> PromptLibrary | None:
    """Return the prompt pack a benchmark renders from, or ``None`` for one that uses none.

    ``getattr`` rather than a protocol member: a suite whose cases carry literal text
    (``native.echo``) has no pack, and requiring every benchmark to declare one would make the
    self-test depend on the prompt library it exists to be independent of.
    """
    library = getattr(benchmark, "library", None)
    return library if isinstance(library, PromptLibrary) else None


def _environment_section(machine_profile: Any, gpu_index: int) -> dict[str, Any]:  # noqa: ANN401
    """Build the drift-sensitive environment section of the fingerprint document.

    Driver and CUDA versions are read from **the device the run is attributed to**, not from
    "the GPU" — on a two-GPU machine those can differ, and a fingerprint that recorded the wrong
    one would claim an environment the measurement did not happen in (ADR-0027 §3). Every field is
    present even when unknown: "we could not read the driver version" is part of the record, and
    Machine Identity §8 rule 6 says missing environment information is ``unsupported`` and never
    assumed.
    """
    target = next((gpu for gpu in machine_profile.gpus if gpu.index == gpu_index), None)
    return {
        "gpu_driver_version": target.driver_version if target is not None else None,
        "cuda_version": target.cuda_version if target is not None else None,
        "os_version": machine_profile.os_version,
    }


def _case_selection(benchmark: Benchmark) -> list[str]:
    """List the cases a run of this benchmark would execute, qualified by their test.

    Qualified by test key because two tests may legitimately reuse a case id, and an unqualified
    list would hash two different selections identically.

    A test that cannot enumerate its cases contributes a marker rather than raising. Enumeration
    is the benchmark's own code, and spec §13 keeps a broken test inside its own test: failing
    *run creation* over one would refuse to record a measurement of the four tests that work. The
    marker is not cosmetic — it makes the fingerprint differ from a run in which that test
    enumerated normally, which is exactly right, because it is a different selection of cases.
    """
    selection: list[str] = []
    for test in benchmark.tests:
        try:
            selection.extend(f"{test.key}/{case.case_id}" for case in test.cases())
        except Exception:  # noqa: BLE001 — a broken test never fails the run (spec §13)
            logger.warning("benchmark.cases_unavailable", extra={"test": test.key})
            selection.append(f"{test.key}/!unenumerable")
    return selection


def _fingerprint_document(  # noqa: PLR0913 — every argument is a fingerprint input
    *,
    model: Any,  # noqa: ANN401 — a models row
    descriptor: Any,  # noqa: ANN401 — a model_descriptors row
    runtime_profile: RuntimeProfile,
    provider_version: str | None,
    machine_profile: Any,  # noqa: ANN401 — a baseaicore MachineProfile
    benchmark: Benchmark,
    execution: ExecutionConfig,
    served: ServedContext,
) -> dict[str, Any]:
    """Assemble one run's fingerprint document from resolved inputs.

    The one place the document is built, so ``create_run`` and ``repeat_run`` cannot disagree
    about what a run's provenance is — which matters more here than anywhere else, because
    :func:`~freeweight.domain.provenance.check_repeatable` compares two documents built by these
    two callers and any asymmetry would read as an environment change.
    """
    manifest = benchmark.manifest
    case_ids = _case_selection(benchmark)
    return build_fingerprint_document(
        model={
            "provider_kind": model.provider_kind,
            "provider_model_name": model.provider_model_name,
            "artifact_digest": model.artifact_digest,
            "identity_confidence": model.identity_confidence,
            "descriptor_hash": descriptor.descriptor_hash,
        },
        runtime_profile_hash=runtime_profile.profile_hash,
        provider={"kind": model.provider_kind, "version": provider_version},
        machine_fingerprint=machine_profile.machine_fingerprint,
        environment=_environment_section(machine_profile, execution.gpu_index),
        benchmark={
            "suite_key": manifest.key,
            "suite_version": manifest.version,
            "manifest_hash": manifest.manifest_hash,
            "dataset_hashes": dict(manifest.dataset_hashes),
            # The per-benchmark subset, never the pack hash: editing a prompt this suite does not
            # use must separate nothing (ADR-0028 §1).
            "prompt_subset_hash": manifest.prompt_subset_hash,
        },
        execution={
            "effective_parameters": execution.to_json(),
            "repetitions": execution.measured_repetitions,
            "seed": execution.seed,
            "case_selection_hash": case_selection_hash(case_ids),
            "served_context": served.tokens,
            "served_context_source": served.source.value,
            "gpu_index": execution.gpu_index,
            "multi_gpu_visible": len(machine_profile.gpus) > 1,
        },
        application={"name": "freeweight", "version": __version__, "git_commit": None},
    )


def _lookup_model(session: Session, reference: str) -> Any:  # noqa: ANN401 — an ORM row
    """Resolve a model reference against stored identities only. ``None`` if nothing matches.

    Deliberately local, not a live provider ``resolve``: unlike the models page, a run must point
    at a *stored* identity with a *stored* descriptor, and resolving through the provider would
    produce an identity with neither.
    """
    repository = ModelRepository()
    matches = repository.get_by_id_prefix(session, reference)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValidationError(
            f"{reference!r} matches {len(matches)} models; use a longer prefix.",
            details={"model": reference, "candidates": [row.id for row in matches]},
        )
    by_canonical = repository.get_by_canonical_id(session, reference)
    if by_canonical is not None:
        return by_canonical
    return repository.get_by_provider_model_name(session, reference)


def _machine_row(session: Session, machine_fingerprint: str) -> Any:  # noqa: ANN401 — an ORM row
    """Return this machine's row, which :func:`profile_machine` has just upserted."""
    from freeweight.infrastructure.db.repositories.machines import MachineRepository

    machine = MachineRepository().get_by_fingerprint(session, machine_fingerprint)
    if machine is None:  # pragma: no cover — profile_machine upserts it in the preceding call
        raise DatabaseUnavailable(
            f"Machine {machine_fingerprint!r} is not stored despite having just been profiled."
        )
    return machine


def _provider_version(provider: Provider) -> str | None:
    """Return the provider's reported version, or ``None`` if it will not say.

    Never raises: this is provenance on the way into a run record, and a provider that is
    momentarily unreachable must not prevent a run from being *queued* — the executor will find
    out soon enough, and will record a real failure with a real error code.
    """
    try:
        health = provider.health()
    except ProviderError:
        return None
    version = getattr(health, "version", None)
    return str(version) if version else None


def list_runs(
    database: Database, *, status: str | None = None, limit: int = 50
) -> tuple[RunSummary, ...]:
    """Return runs newest-first, optionally filtered by status."""
    with _translated(), database.read() as session:
        rows = RunRepository().list_runs(session, status=status, limit=limit)
        return tuple(_summarize_run(session, row) for row in rows)


def get_run(database: Database, run_ref: str) -> RunDetail:
    """Return one run with its tests and aggregate metrics.

    Args:
        database: The application's database handle.
        run_ref: A full ULID or an unambiguous prefix.

    Returns:
        The run detail.

    Raises:
        RunNotFound: Nothing matches ``run_ref``.
        ValidationError: ``run_ref`` is an ambiguous prefix.
    """
    from freeweight.infrastructure.db.repositories.runs import RunEventRepository

    with _translated(), database.read() as session:
        run = _resolve_run(session, run_ref)
        summary = _summarize_run(session, run)
        test_rows = RunTestRepository().list_for_run(session, run.id)
        names = _test_names(session, run.suite_id)
        tests = tuple(
            RunTestSummary(
                id=row.id,
                test_key=names.get(row.test_id, ("unknown", "unknown"))[0],
                test_name=names.get(row.test_id, ("unknown", "unknown"))[1],
                status=row.status,
                skip_reason=row.skip_reason,
                completed_cases=row.completed_cases,
                total_cases=row.total_cases,
                repetitions=row.repetitions,
                started_at=row.started_at,
                completed_at=row.completed_at,
                error_code=row.error_code,
                error_text=row.error_text,
            )
            for row in test_rows
        )
        metrics = tuple(
            MetricSummary(
                metric_key=row.metric_key,
                run_test_id=row.run_test_id,
                numeric_value=row.numeric_value,
                unavailable_reason=row.unavailable_reason,
                unit=row.unit,
                aggregation=row.aggregation,
                higher_is_better=row.higher_is_better,
                sample_count=row.sample_count,
                excluded_count=row.excluded_count,
                gpu_index=row.gpu_index,
                stddev=row.stddev,
                coefficient_of_variation=row.coefficient_of_variation,
            )
            for row in MetricValueRepository().list_for_run(session, run.id)
        )
        last_sequence = RunEventRepository().next_sequence(session, run.id) - 1
        return RunDetail(
            run=summary,
            tests=tests,
            metrics=metrics,
            effective_config=ExecutionConfig.from_json(run.effective_config_json),
            last_event_sequence=last_sequence,
        )


def _test_names(session: Session, suite_id: str) -> dict[str, tuple[str, str]]:
    """Map ``benchmark_tests.id`` to ``(key, name)`` for one suite version."""
    return {
        row.id: (row.key, row.name) for row in BenchmarkRepository().list_tests(session, suite_id)
    }


def list_samples(
    database: Database, run_test_id: str, *, limit: int = 500
) -> tuple[SampleSummary, ...]:
    """Return one test's samples in declaration order.

    Args:
        database: The application's database handle.
        run_test_id: The ``run_tests`` row to drill into.
        limit: Maximum samples returned.

    Returns:
        The samples, ordered by ``(ordinal, repetition)``.

    Raises:
        RunNotFound: No run test has this id — the same code as a missing run, because from a
            caller's point of view a test id that resolves to nothing is a run it cannot reach.
    """
    with _translated(), database.read() as session:
        run_test = RunTestRepository().get_by_id(session, run_test_id)
        if run_test is None:
            raise RunNotFound(
                f"No run test matches {run_test_id!r}.", details={"run_test": run_test_id}
            )
        rows = SampleRepository().list_for_run_test(session, run_test_id, limit=limit)
        return tuple(
            SampleSummary(
                id=row.id,
                case_id=row.case_id,
                ordinal=row.ordinal,
                repetition=row.repetition,
                status=row.status,
                score=row.score,
                score_method=row.score_method,
                response_hash=row.response_hash,
                response_text=row.response_text,
                output_chars=row.output_chars,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                client_wall_ms=row.client_wall_ms,
                finish_reason=row.finish_reason,
                error_code=row.error_code,
                error_text=row.error_text,
                detail=dict(row.result_json) if isinstance(row.result_json, dict) else {},
                prompt_id=row.prompt_id,
                prompt_version=row.prompt_version,
                client_ttft_ms=row.client_ttft_ms,
            )
            for row in rows
        )


def cancel_run(
    database: Database, publisher: RunEventPublisher, run_ref: str, *, clock: Clock = utc_now
) -> RunSummary:
    """Request cancellation of a run, and return its new state.

    A ``queued``, ``preparing`` or ``warming`` run is cancelled outright, here, in this
    transaction. A ``running`` run enters ``cancelling`` and is finished by the scheduler at its
    next boundary check — the request thread cannot interrupt a provider call on the executor
    thread, and pretending otherwise is how a run gets stuck in ``cancelling`` forever. A run
    already in ``cancelling`` is a no-op, not an error: the user asked for something that is
    already happening.

    Args:
        database: The application's database handle.
        publisher: The event publisher, so the cancellation appears on the run's own stream.
        run_ref: A full ULID or an unambiguous prefix.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The run in its new state.

    Raises:
        RunNotFound: Nothing matches ``run_ref``.
        RunNotCancellable: The run is in a terminal or non-cancellable state (spec §13).
    """
    now = clock()
    with _translated(), database.write() as session:
        run = _resolve_run(session, run_ref)
        current = RunStatus(run.status)
        if current is RunStatus.CANCELLING:
            return _summarize_run(session, run)
        target = cancellation_target(current)
        require_run_transition(current, target)
        RunRepository().set_status(
            session,
            run.id,
            status=target.value,
            completed_at=now if target is RunStatus.CANCELLED else None,
        )
        if target is RunStatus.CANCELLED:
            _cancel_pending_tests(session, run.id)
        session.flush()
        summary = _summarize_run(session, RunRepository().get_by_id(session, run.id))
    if target is RunStatus.CANCELLED:
        publisher.publish(
            summary.id, "run.cancelled", message="Run cancelled before it started executing."
        )
    else:
        publisher.publish(
            summary.id,
            "run.progress",
            message="Cancellation requested; stopping at the next boundary.",
        )
    return summary


def _cancel_pending_tests(session: Session, run_id: str) -> None:
    """Move every non-terminal test of a cancelled run to ``cancelled``.

    Leaves terminal tests exactly as they are: a test that completed before the user cancelled
    produced real samples, and rewriting its status would throw away a measurement that was made.
    """
    repository = RunTestRepository()
    for row in repository.list_for_run(session, run_id):
        current = TestStatus(row.status)
        if current in {TestStatus.PENDING, TestStatus.RUNNING}:
            require_test_transition(current, TestStatus.CANCELLED)
            repository.set_status(session, row.id, status=TestStatus.CANCELLED.value)


def resume_run(database: Database, publisher: RunEventPublisher, run_ref: str) -> RunSummary:
    """Re-queue an ``interrupted`` run so the scheduler picks it up where it left off.

    The only legal successor of ``interrupted`` is ``queued`` — the executor's own idempotency
    (existing ``run_tests`` rows are reused, existing samples are skipped) is what makes that a
    resume rather than a restart.

    Args:
        database: The application's database handle.
        publisher: The event publisher.
        run_ref: A full ULID or an unambiguous prefix.

    Returns:
        The re-queued run.

    Raises:
        RunNotFound: Nothing matches ``run_ref``.
        IllegalTransition: The run is not ``interrupted``.
    """
    with _translated(), database.write() as session:
        run = _resolve_run(session, run_ref)
        current = RunStatus(run.status)
        require_run_transition(current, RunStatus.QUEUED)
        RunRepository().set_status(session, run.id, status=RunStatus.QUEUED.value)
        session.flush()
        summary = _summarize_run(session, RunRepository().get_by_id(session, run.id))
    publisher.publish(summary.id, "run.progress", message="Run re-queued for resume.")
    return summary


class _Cancelled(Exception):  # noqa: N818 — a control-flow signal, not an error condition
    """Internal signal that a cancellation was observed at a boundary.

    Not a :class:`~baseaicore.SuiteError`: nothing has gone wrong, and it never leaves this
    module. It exists so that a cancellation check deep inside test execution unwinds to
    :func:`execute_run`'s one cancellation handler instead of threading a boolean back up through
    four call frames, each of which would have to remember to check it.
    """


def execute_run(  # noqa: PLR0913 — the executor needs every collaborator it is handed
    database: Database,
    provider: Provider,
    registry: BenchmarkRegistry,
    publisher: RunEventPublisher,
    run_id: str,
    *,
    collector: TelemetryCollector | None = None,
    telemetry: TelemetrySettings | None = None,
    clock: Clock = utc_now,
) -> RunStatus:
    """Execute one claimed run to a terminal state, and return that state.

    The run is already in ``preparing`` when this is called — claiming it is what moved it there
    (:meth:`~freeweight.infrastructure.db.repositories.runs.RunRepository.claim_next_queued`), so
    that no run can be claimed twice.

    Phases, each preceded by a cancellation check: **calibrate** (measure what telemetry sampling
    costs, before anything is measured with it), **settle** (wait for the machine to go quiet, and
    record what was seen either way), **prepare** (load the suite, enumerate tests), **warm**
    (unscored generations that take model loading out of the measurement), **execute** (every case
    × repetition, one sample per generation, with telemetry recording throughout), **aggregate**
    (read the stored samples back and write metric rows), **complete**.

    Never raises for a *measurement* failure. A failed sample and a failed test are recorded and
    execution continues. Only a failure of the machinery itself — the suite is not registered, the
    run's rows are inconsistent, the machine refused to go idle under ``on_idle_timeout =
    "refuse"`` — moves the run to ``failed``, and even then the error is recorded rather than
    propagated, because the scheduler thread must survive it.

    Args:
        database: The application's database handle.
        provider: The provider to generate through.
        registry: The benchmarks this build can run.
        publisher: The event publisher.
        run_id: The claimed run.
        collector: The telemetry collector to sample and idle-check through, or ``None`` to do
            neither. ``None`` is a real configuration — a machine with no readable telemetry — and
            produces a run with no telemetry rows and an idle check that did not happen, both
            visible rather than assumed.
        telemetry: The ``[telemetry]`` settings, or ``None`` for this build's defaults.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The terminal status the run reached.
    """
    try:
        return _execute_run_inner(
            database,
            provider,
            registry,
            publisher,
            run_id,
            collector=collector,
            telemetry=telemetry,
            clock=clock,
        )
    except _Cancelled:
        _finish(database, publisher, run_id, RunStatus.CANCELLED, clock=clock)
        return RunStatus.CANCELLED
    except KeyboardInterrupt:
        # A ``Ctrl-C`` in a run hosted by the CLI (CLI standards §5: "the in-flight unit is
        # marked cancelled, partial results are preserved, the database is left consistent").
        # ``session_scope`` has already rolled back whatever write was in flight — it catches
        # ``BaseException`` for exactly this — so nothing is half-committed, and finishing the
        # run here is what stops it being left in ``cancelling`` forever. The signal is then
        # re-raised, because the caller asked to be interrupted, not to be handled.
        _finish(
            database,
            publisher,
            run_id,
            RunStatus.CANCELLED,
            error_code="CANCELLED",
            error_text="Cancelled by signal.",
            clock=clock,
        )
        raise
    except Exception as exc:  # noqa: BLE001 — the scheduler thread must survive any run failure
        logger.exception("run.failed", extra={"run_id": run_id})
        code = exc.code if isinstance(exc, SuiteError) else "INTERNAL_ERROR"
        _finish(
            database,
            publisher,
            run_id,
            RunStatus.FAILED,
            error_code=code,
            error_text=str(exc),
            clock=clock,
        )
        return RunStatus.FAILED


@dataclass(frozen=True, slots=True)
class _RunContext:
    """What one execution needs to know about the run it is executing.

    Read once, from the run's own row, and never re-resolved from live configuration: the served
    context, the target device and the execution parameters are what this run was *created* with,
    and a run resumed after someone edited ``config.toml`` must still be the run that was queued.
    """

    suite_key: str
    suite_id: str
    config: ExecutionConfig
    identity: ModelIdentity
    model_canonical_id: str
    served_context: int | None
    gpu_index: int
    multi_gpu_visible: bool


def _read_context(database: Database, run_id: str) -> _RunContext:
    """Load the run's frozen inputs, or refuse with the reason the row is unusable."""
    with database.read() as session:
        run = RunRepository().get_by_id(session, run_id)
        if run is None:
            raise RunNotFound(f"No run matches {run_id!r}.", details={"run": run_id})
        suite = session.get(_suite_model(), run.suite_id)
        if suite is None:  # pragma: no cover — a RESTRICT foreign key makes this unreachable
            raise DatabaseUnavailable(f"Run {run_id!r} points at a suite that is not installed.")
        model = ModelRepository().get_by_id(session, run.model_id)
        if model is None:  # pragma: no cover — a RESTRICT foreign key makes this unreachable
            raise DatabaseUnavailable(f"Run {run_id!r} points at a model that is not stored.")
        return _RunContext(
            suite_key=suite.key,
            suite_id=suite.id,
            config=ExecutionConfig.from_json(run.effective_config_json),
            identity=ModelIdentity(
                provider_kind=ProviderKind(model.provider_kind),
                provider_model_name=model.provider_model_name,
                artifact_digest=model.artifact_digest,
            ),
            model_canonical_id=model.canonical_id,
            served_context=run.served_context,
            gpu_index=run.gpu_index if run.gpu_index is not None else 0,
            multi_gpu_visible=bool(run.multi_gpu_visible),
        )


def _calibrate(
    database: Database,
    run_id: str,
    collector: TelemetryCollector | None,
    telemetry: TelemetrySettings | None,
) -> None:
    """Measure what telemetry sampling costs on this machine and store it on the run.

    Before the first provider call, so the calibration is outside the window it describes. A
    failure to calibrate is logged and dropped: the number is provenance about the measurement,
    and losing it must not lose the measurement (spec §15 asks for it to be recorded, not for the
    run to depend on it).
    """
    if collector is None or telemetry is None or not telemetry.calibrate_overhead:
        return
    try:
        calibration = calibrate_sampling_overhead(collector, interval_ms=telemetry.interval_ms)
    except Exception:  # noqa: BLE001 — provenance about a run must not be able to fail the run
        logger.warning("run.calibration_failed", extra={"run_id": run_id})
        return
    with database.write() as session:
        RunRepository().set_observations(
            session, run_id, telemetry_overhead_percent=calibration.overhead_percent
        )
    logger.info(
        "run.telemetry_calibrated",
        extra={"run_id": run_id, "overhead_percent": calibration.overhead_percent},
    )


def _settle(
    database: Database,
    publisher: RunEventPublisher,
    run_id: str,
    collector: TelemetryCollector | None,
    config: ExecutionConfig,
) -> list[Degradation]:
    """Wait for the machine to go quiet, and record what was observed either way.

    Spec §13's idle-detection outcome, in full. There are exactly three results and every one of
    them leaves a record:

    * The machine settled — nothing to record.
    * It did not, and ``on_idle_timeout = "warn"`` — the run proceeds and carries a
      ``measured_while_busy`` degradation with the utilization that was actually seen, so
      contamination is visible in the provenance rather than turning up months later as
      unexplained dispersion.
    * It did not, and ``on_idle_timeout = "refuse"`` — the run fails with
      ``INSUFFICIENT_RESOURCES`` and those same numbers.

    Silently proceeding with no record was the previously unspecified fourth option, and it is
    the one this function exists to make impossible.

    Raises:
        InsufficientResources: The machine stayed busy and ``on_idle_timeout`` is ``refuse``.
    """
    if collector is None or config.idle_gpu_threshold_percent <= 0:
        return []
    outcome = wait_for_idle(
        collector,
        threshold_percent=config.idle_gpu_threshold_percent,
        required_samples=config.idle_required_samples,
        timeout_seconds=config.idle_wait_timeout_seconds,
    )
    if outcome.idle:
        return []
    detail = outcome.as_detail()
    if config.on_idle_timeout == "refuse":
        raise InsufficientResources(
            "The machine did not go idle within "
            f"{config.idle_wait_timeout_seconds:g}s: GPU {detail['gpu_utilization_percent']}%, "
            f"CPU {detail['cpu_percent']}% against a {config.idle_gpu_threshold_percent:g}% "
            "threshold. Configured to refuse rather than measure a busy machine.",
            details={"run": run_id, **detail},
        )
    publisher.publish(
        run_id,
        "run.degraded",
        message=(
            "Measuring a busy machine: it did not fall below "
            f"{config.idle_gpu_threshold_percent:g}% within "
            f"{config.idle_wait_timeout_seconds:g}s."
        ),
        data={"degradation": "measured_while_busy", **detail},
    )
    return [Degradation(kind="measured_while_busy", detail=detail)]


def _record_degradations(
    database: Database, run_id: str, degradations: Sequence[Degradation]
) -> None:
    """Merge new degradations into the run's stored list.

    Read-modify-write inside one transaction, because a run created by a forced repeat already
    carries the divergences it proceeded past and the idle check must not overwrite them.
    """
    if not degradations:
        return
    with database.write() as session:
        run = RunRepository().get_by_id(session, run_id)
        stored = run.degradations_json if run is not None else None
        existing = list(stored) if isinstance(stored, list) else []
        RunRepository().set_observations(
            session,
            run_id,
            degradations_json=existing + [item.as_json() for item in degradations],
        )


def _execute_run_inner(  # noqa: PLR0913 — mirrors execute_run's collaborators
    database: Database,
    provider: Provider,
    registry: BenchmarkRegistry,
    publisher: RunEventPublisher,
    run_id: str,
    *,
    collector: TelemetryCollector | None,
    telemetry: TelemetrySettings | None,
    clock: Clock,
) -> RunStatus:
    """Drive one run through its phases. See :func:`execute_run` for the contract."""
    context = _read_context(database, run_id)
    benchmark = registry.get(context.suite_key)
    config = context.config
    publisher.publish(
        run_id,
        "run.started",
        message=f"Run started: {context.suite_key} against {context.model_canonical_id}.",
        data={"suite": context.suite_key, "model": context.model_canonical_id},
    )

    # --- calibrate and settle ------------------------------------------------------------------
    _check_cancelled(database, run_id)
    _calibrate(database, run_id, collector, telemetry)
    _check_cancelled(database, run_id)
    _record_degradations(database, run_id, _settle(database, publisher, run_id, collector, config))

    # --- prepare -----------------------------------------------------------------------------
    _check_cancelled(database, run_id)
    run_test_ids = _prepare(database, run_id, benchmark, context.suite_id, config)

    persist = telemetry is not None and telemetry.persist_during_runs and collector is not None
    interval_seconds = (telemetry.interval_ms / 1000.0) if telemetry is not None else 1.0
    recorder = (
        TelemetryRecorder(
            database,
            run_id,
            collector,
            interval_seconds=interval_seconds,
            enabled=True,
        )
        if persist and collector is not None
        else None
    )
    try:
        if recorder is not None:
            recorder.start()

        # --- warm ----------------------------------------------------------------------------
        _check_cancelled(database, run_id)
        _transition(database, run_id, RunStatus.WARMING)
        _warm(provider, context.identity, benchmark, config)

        # --- execute -------------------------------------------------------------------------
        _check_cancelled(database, run_id)
        _transition(database, run_id, RunStatus.RUNNING)
        # Read back from the ``run_tests`` rows `_prepare` just wrote, rather than re-enumerating
        # every test's cases. Re-enumerating here would put a benchmark's own code on the run's
        # critical path *outside* the per-test error handling, so one test that cannot list its
        # cases would fail the whole run — the exact containment rule spec §13 states ("a failed
        # test never fails the run"). The persisted counts are also the ones a resumed run has to
        # agree with.
        with database.read() as session:
            total_samples = sum(
                row.total_cases * row.repetitions
                for row in RunTestRepository().list_for_run(session, run_id)
            )
        completed_samples = 0
        for position, test in enumerate(benchmark.tests):
            _check_cancelled(database, run_id)
            if position > 0:
                _cooldown(config)
            completed_samples = _execute_test(
                database,
                provider,
                publisher,
                run_id=run_id,
                run_test_id=run_test_ids[test.key],
                context=context,
                test=test,
                config=config,
                completed_samples=completed_samples,
                total_samples=total_samples,
                clock=clock,
            )
    finally:
        # Stopped before aggregation, not after: an observation written while the aggregate was
        # being computed would describe a window the numbers do not cover.
        if recorder is not None:
            recorder.stop()

    # --- aggregate ---------------------------------------------------------------------------
    _check_cancelled(database, run_id)
    _aggregate_run(database, run_id, benchmark, run_test_ids, context=context, clock=clock)

    # --- complete ----------------------------------------------------------------------------
    _finish(database, publisher, run_id, RunStatus.COMPLETED, clock=clock)
    return RunStatus.COMPLETED


def _cooldown(config: ExecutionConfig) -> None:
    """Pause between tests so one test's heat is not the next test's starting condition.

    Recorded on every run since Phase 5 and honoured from Phase 6. ``0`` skips the sleep entirely
    rather than calling ``sleep(0)``, so a test suite configured with no cooldown does not pay a
    scheduler round trip per test.
    """
    if config.cooldown_seconds > 0:
        time.sleep(config.cooldown_seconds)


def _current_status(database: Database, run_id: str) -> RunStatus:
    """Read a run's status now, from the database, not from anything cached in this thread."""
    with database.read() as session:
        run = RunRepository().get_by_id(session, run_id)
        if run is None:
            raise RunNotFound(f"No run matches {run_id!r}.", details={"run": run_id})
        return RunStatus(run.status)


def _check_cancelled(database: Database, run_id: str) -> None:
    """Raise :class:`_Cancelled` if the run has been asked to stop.

    One indexed primary-key read. It is deliberately a fresh read every time rather than a cached
    flag: cancellation arrives on a *different thread*, through the database, and a cached value
    is a cancellation the executor never notices.

    Raises:
        _Cancelled: The run is ``cancelling`` or already ``cancelled``.
    """
    status = _current_status(database, run_id)
    if status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
        raise _Cancelled


def _transition(database: Database, run_id: str, target: RunStatus) -> None:
    """Move a run to ``target``, refusing an illegal move.

    The current status is read inside the same transaction that writes the new one, so the check
    is against what is actually stored rather than against what this thread last saw.
    """
    with database.write() as session:
        run = RunRepository().get_by_id(session, run_id)
        if run is None:  # pragma: no cover — the executor holds a claimed run
            raise RunNotFound(f"No run matches {run_id!r}.", details={"run": run_id})
        require_run_transition(RunStatus(run.status), target)
        RunRepository().set_status(session, run_id, status=target.value)


def _prepare(
    database: Database,
    run_id: str,
    benchmark: Benchmark,
    suite_id: str,
    config: ExecutionConfig,
) -> dict[str, str]:
    """Enumerate this run's tests, creating the rows that do not exist yet.

    Idempotent, which is what makes resume work: a run prepared once already has its ``run_tests``
    rows, terminal statuses included, and this call finds them rather than replacing them.

    Returns:
        ``{test_key: run_test_id}``.
    """
    with database.write() as session:
        test_ids = _test_row_ids(session, suite_id)
        repository = RunTestRepository()
        run_test_ids: dict[str, str] = {}
        for test in benchmark.tests:
            row = repository.get_or_create(
                session,
                run_id=run_id,
                test_id=test_ids[test.key],
                total_cases=_case_count(test),
                repetitions=config.measured_repetitions,
            )
            run_test_ids[test.key] = row.id
        return run_test_ids


def _case_count(test: BenchmarkTest) -> int:
    """Count a test's cases, or ``0`` for a test that cannot enumerate them.

    Tolerated rather than raised for the same reason :func:`_case_selection` tolerates it: the
    test will raise again when execution asks it for the same cases, and *that* is where it is
    recorded as a failed test with its error. Failing here would fail the run instead, which is
    the containment rule inverted (spec §13).
    """
    try:
        return len(tuple(test.cases()))
    except Exception:  # noqa: BLE001 — a broken test never fails the run (spec §13)
        logger.warning("benchmark.cases_unavailable", extra={"test": test.key})
        return 0


def _test_row_ids(session: Session, suite_id: str) -> dict[str, str]:
    """Map test key to ``benchmark_tests.id`` for one installed suite version."""
    return {row.key: row.id for row in BenchmarkRepository().list_tests(session, suite_id)}


def _warm(
    provider: Provider,
    identity: ModelIdentity,
    benchmark: Benchmark,
    config: ExecutionConfig,
) -> None:
    """Run the configured warm-up generations, discarding their results.

    Warm-up exists so that first-call model loading is not counted as inference time. Its output
    is deliberately thrown away — a warm-up sample stored beside measured ones would be exactly
    the cold/warm mixing benchmark catalog §3.1 forbids.

    A warm-up failure is *not* a run failure: the provider is about to be asked the same thing
    again for real, and the real attempt records a real error. Swallowing it here keeps a
    transient hiccup during warm-up from failing a run that would otherwise have succeeded.
    """
    if config.warmup_repetitions <= 0:
        return
    first_case = next(iter(benchmark.tests[0].cases()), None)
    if first_case is None:  # pragma: no cover — every shipped suite has at least one case
        return
    for _ in range(config.warmup_repetitions):
        try:
            provider.generate(_build_request(identity, first_case, config))
        except ProviderError as exc:
            logger.warning("run.warmup_failed", extra={"code": exc.code})
            return


def _build_request(
    identity: ModelIdentity, case: Any, config: ExecutionConfig
) -> GenerationRequest:  # noqa: ANN401 — a BenchmarkCase
    """Build one provider request from a case and the run's frozen execution config.

    A case's system turn, where it has one, becomes a leading ``SYSTEM`` message rather than being
    prepended to the user text: a provider applies its own template to the two roles differently,
    and merging them would measure a prompt nobody wrote.
    """
    messages = []
    if getattr(case, "system_prompt", None):
        messages.append(Message(role=Role.SYSTEM, content=case.system_prompt))
    messages.append(Message(role=Role.USER, content=case.prompt))
    return GenerationRequest(
        identity=identity,
        messages=tuple(messages),
        sampling=SamplingParameters(
            temperature=config.temperature,
            top_p=config.top_p,
            seed=config.seed,
            max_output_tokens=config.max_output_tokens,
        ),
        timeout_seconds=config.test_timeout_seconds,
    )


def _skips_for_context(case: Any, served_context: int | None) -> str | None:  # noqa: ANN401
    """Return a skip reason when this case needs more context than the run is served, else ``None``.

    Benchmark catalog §3.1's "only those the model supports", decided per case. Sending a case the
    model cannot hold and recording the refusal as a failure would report a model as unreliable
    for being asked something it never claimed to do.
    """
    needed = getattr(case, "required_context_tokens", None)
    if needed is None or served_context is None or needed <= served_context:
        return None
    return (
        f"This case needs about {needed} tokens of context; the model is served {served_context}."
    )


@dataclass(frozen=True, slots=True)
class _StreamObservation:
    """What this process observed while consuming one stream.

    Attributes:
        result: The assembled result, or ``None`` when the stream failed.
        error: The provider's typed failure, or ``None``.
        ttft_ms: Time observed before the first delta arrived, with
            :func:`~baseaicore.monotonic_ns` — never a wall clock (spec §15).
        inter_chunk_ms: The gap before each delta after the first, in arrival order.
        token_level_chunks: Whether the provider declared one delta to be one token. Recorded on
            the sample so that no later reader can turn a chunk figure into a token figure.
    """

    result: Any = None  # noqa: ANN401 — modelrack.GenerationResult
    error: ProviderError | None = None
    ttft_ms: Measurement = UNSUPPORTED
    inter_chunk_ms: tuple[float, ...] = ()
    token_level_chunks: bool = False

    def detail(self) -> dict[str, Any]:
        """The streaming evidence stored in ``samples.result_json``."""
        return {
            "inter_chunk_ms": list(self.inter_chunk_ms),
            "chunk_count": len(self.inter_chunk_ms) + 1 if self.inter_chunk_ms else 0,
            "token_level_chunks": self.token_level_chunks,
        }


def _consume_stream(
    provider: Provider, request: GenerationRequest, *, token_level_chunks: bool
) -> _StreamObservation:
    """Consume one stream, timing the deltas as they arrive.

    Timing is :func:`~baseaicore.monotonic_ns` throughout: a wall clock can step backwards under
    NTP and would produce a negative inter-chunk gap, which is the failure mode Phase 6 names as
    "wall-clock used for durations".

    Only content deltas are timed. A :class:`~modelrack.streaming.ThinkingDelta` is content the
    model produced too, so it counts; a tool-call delta does not, because a caller measuring
    output latency is measuring text arriving.

    Args:
        provider: The provider to stream from.
        request: The request to stream.
        token_level_chunks: What the provider declared. Carried through untouched — this function
            records the claim, it does not evaluate it.

    Returns:
        The observation, whose ``error`` is set when the stream terminated in
        :class:`~modelrack.streaming.StreamFailed`.
    """
    started = monotonic_ns()
    previous: int | None = None
    ttft: Measurement = UNSUPPORTED
    gaps: list[float] = []
    result: Any = None
    error: ProviderError | None = None
    for event in provider.stream(request):
        if isinstance(event, TokenDelta | ThinkingDelta):
            now = monotonic_ns()
            if previous is None:
                ttft = (now - started) / 1_000_000.0
            else:
                gaps.append((now - previous) / 1_000_000.0)
            previous = now
        elif isinstance(event, StreamCompleted):
            result = event.result
        elif isinstance(event, StreamFailed):
            error = event.error
    return _StreamObservation(
        result=result,
        error=error,
        ttft_ms=ttft,
        inter_chunk_ms=tuple(gaps),
        token_level_chunks=token_level_chunks,
    )


def _execute_test(  # noqa: PLR0913 — one test's execution needs all of its context
    database: Database,
    provider: Provider,
    publisher: RunEventPublisher,
    *,
    run_id: str,
    run_test_id: str,
    context: _RunContext,
    test: BenchmarkTest,
    config: ExecutionConfig,
    completed_samples: int,
    total_samples: int,
    clock: Clock,
) -> int:
    """Execute one test: every case, every repetition, one sample per generation.

    A test already in a terminal state is skipped entirely and its samples counted towards
    progress — that is resume, and it is why "completed tests are retained".

    A case that needs more context than the run is served is stored as a ``skipped`` sample with
    the reason, not sent (:func:`_skips_for_context`). A skipped sample carries no score, is
    excluded from every aggregate, and stays visible in the counts.

    Returns:
        The running total of completed samples, for the caller's progress events.
    """
    with database.write() as session:
        row = RunTestRepository().get_by_id(session, run_test_id)
        if row is None:  # pragma: no cover — _prepare created it
            raise DatabaseUnavailable(f"Run test {run_test_id!r} vanished mid-run.")
        current = TestStatus(row.status)
        if current in {
            TestStatus.COMPLETED,
            TestStatus.FAILED,
            TestStatus.SKIPPED,
            TestStatus.CANCELLED,
        }:
            return completed_samples + row.total_cases * row.repetitions
        if current is TestStatus.PENDING:
            require_test_transition(current, TestStatus.RUNNING)
            RunTestRepository().set_status(
                session,
                run_test_id,
                status=TestStatus.RUNNING.value,
                started_at=clock(),
                measurement_class=test.measurement_class,
            )
        already = SampleRepository().existing_keys(session, run_test_id)

    publisher.publish(
        run_id,
        "test.started",
        message=f"Test {test.key} started.",
        data={
            "test": test.key,
            "run_test_id": run_test_id,
            "measurement_class": test.measurement_class,
        },
    )

    error_code: str | None = None
    error_text: str | None = None
    finished_cases = 0
    try:
        # Inside the handler, not before it: enumerating cases is the benchmark's own code, and a
        # benchmark that cannot produce its cases must fail *this test* rather than the run
        # (spec §13). It is the one line of third-party code that runs before any sample exists,
        # which makes it the easiest one to leave outside the containment by accident.
        cases = list(test.cases())
        if config.randomize_case_order:
            # Seeded from the run's own recorded seed and the test key, so the order is
            # reproducible from the run record alone — a shuffle seeded from wall-clock time would
            # make "the same run, again" impossible to mean anything.
            random.Random(f"{config.seed}:{test.key}").shuffle(cases)  # noqa: S311 — order, not crypto
        for case in cases:
            _check_cancelled(database, run_id)
            skip_reason = _skips_for_context(case, context.served_context)
            for repetition in range(1, config.measured_repetitions + 1):
                if (case.case_id, case.ordinal, repetition) in already:
                    completed_samples += 1
                    continue
                _check_cancelled(database, run_id)
                if skip_reason is not None:
                    sample = _store_skipped(
                        database,
                        run_test_id=run_test_id,
                        case=case,
                        repetition=repetition,
                        reason=skip_reason,
                        now=clock(),
                    )
                else:
                    sample = _run_one_case(
                        database,
                        provider,
                        run_test_id=run_test_id,
                        identity=context.identity,
                        test=test,
                        case=case,
                        repetition=repetition,
                        config=config,
                        clock=clock,
                    )
                completed_samples += 1
                publisher.publish(
                    run_id,
                    _sample_event_type(sample["status"]),
                    message=(f"{test.key} {case.case_id} rep {repetition}: {sample['status']}"),
                    progress=(completed_samples, total_samples),
                    data={
                        "run_test_id": run_test_id,
                        "case_id": case.case_id,
                        "repetition": repetition,
                        "status": sample["status"],
                        "score": sample["score"],
                        "error_code": sample["error_code"],
                    },
                )
            finished_cases += 1
            with database.write() as session:
                RunTestRepository().set_completed_cases(
                    session, run_test_id, completed=finished_cases
                )
    except _Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — a failed test never fails its run (spec §13)
        logger.warning("test.failed", extra={"run_id": run_id, "test": test.key}, exc_info=exc)
        error_code = exc.code if isinstance(exc, SuiteError) else "INTERNAL_ERROR"
        error_text = str(exc)

    target = TestStatus.FAILED if error_code is not None else TestStatus.COMPLETED
    with database.write() as session:
        row = RunTestRepository().get_by_id(session, run_test_id)
        if row is not None:
            require_test_transition(TestStatus(row.status), target)
            RunTestRepository().set_status(
                session,
                run_test_id,
                status=target.value,
                completed_at=clock(),
                error_code=error_code,
                error_text=error_text,
            )
    publisher.publish(
        run_id,
        "test.completed",
        message=f"Test {test.key} {target.value}.",
        progress=(completed_samples, total_samples),
        data={"test": test.key, "run_test_id": run_test_id, "status": target.value},
    )
    return completed_samples


def _sample_event_type(status: str) -> str:
    """Map a stored sample status to a declared run event type (api.md §4).

    There is no ``sample.skipped`` in the vocabulary, and inventing one would be a change to a
    public contract for the sake of a stream frame. A skipped sample is announced as
    ``test.progress`` instead — which is true, carries the same ``progress`` pair and the same
    ``error_code``, and keeps the live view moving rather than going silent for nine cases the
    model could not be served.
    """
    return {
        "completed": "sample.completed",
        "skipped": "test.progress",
    }.get(status, "sample.failed")


def _store_skipped(
    database: Database,
    *,
    run_test_id: str,
    case: Any,  # noqa: ANN401 — freeweight.domain.benchmark.BenchmarkCase
    repetition: int,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    """Store one sample that was never sent, with the reason it was not.

    A skip is a *recorded* absence, not a missing row: it keeps the sample count honest, keeps the
    exclusion visible beside the aggregate, and lets a person see that a 64K case was skipped
    rather than wonder why the suite reports eight prompt sizes on one model and nine on another.
    """
    values = _sample_values(
        run_test_id=run_test_id,
        case=case,
        repetition=repetition,
        status="skipped",
        result=None,
        score=None,
        wall_ms=0.0,
        config=ExecutionConfig.from_json({}),
        error_code="CONTEXT_LIMIT_EXCEEDED",
        error_text=reason,
        now=now,
    )
    values["client_wall_ms"] = None
    with database.write() as session:
        SampleRepository().insert(session, **values)
    return values


def _run_one_case(  # noqa: PLR0913 — one sample needs its whole context
    database: Database,
    provider: Provider,
    *,
    run_test_id: str,
    identity: ModelIdentity,
    test: BenchmarkTest,
    case: Any,  # noqa: ANN401 — freeweight.domain.benchmark.BenchmarkCase, imported lazily
    repetition: int,
    config: ExecutionConfig,
    clock: Clock,
) -> dict[str, Any]:
    """Generate one response, score it, and store the sample. Never raises for a bad response.

    A test that declares ``streaming`` is executed through :meth:`~modelrack.Provider.stream` and
    the deltas are timed as they arrive; anything else goes through :meth:`generate`. The
    difference is recorded rather than inferred: only a streamed sample has a ``ttft_ms`` or an
    inter-chunk series, and a non-streamed one leaves both genuinely absent instead of zero.

    A provider error becomes a ``failed`` sample carrying the provider's own stable error code and
    ``score = NULL`` — never a zero, and never a failed test (spec §13). A scorer that raises is
    treated identically: the sample is stored ``failed`` with ``SCORER_ERROR``, because a defect
    in one scorer must not discard a run's other measurements.

    Returns:
        The stored column values, for the caller's event payload.
    """
    request = _build_request(identity, case, config)
    started_ns = monotonic_ns()
    result: Any = None
    error_code: str | None = None
    error_text: str | None = None
    stream_detail: dict[str, Any] = {}
    observed_ttft: Measurement = UNSUPPORTED
    if test.streaming:
        try:
            observation = _consume_stream(
                provider, request, token_level_chunks=_token_level_chunks(provider)
            )
        except ProviderError as exc:
            observation = _StreamObservation(error=exc)
        result = observation.result
        observed_ttft = observation.ttft_ms
        stream_detail = observation.detail()
        if observation.error is not None:
            error_code, error_text = observation.error.code, observation.error.message
    else:
        try:
            result = provider.generate(request)
        except ProviderError as exc:
            error_code, error_text = exc.code, exc.message
    wall_ms = elapsed_ms(started_ns)

    if result is None:
        values = _sample_values(
            run_test_id=run_test_id,
            case=case,
            repetition=repetition,
            status="timeout" if error_code == "PROVIDER_TIMEOUT" else "failed",
            result=None,
            score=None,
            wall_ms=wall_ms,
            config=config,
            error_code=error_code,
            error_text=error_text,
            now=clock(),
            extra_detail=stream_detail,
        )
    else:
        try:
            verdict: ScoreResult | None = test.scorer.score(case, result.text)
        except Exception as exc:  # noqa: BLE001 — a scorer defect fails one sample, not the run
            logger.warning("sample.scorer_error", extra={"test": test.key}, exc_info=exc)
            verdict = None
            error_code, error_text = "SCORER_ERROR", str(exc)
        if verdict is None:
            values = _sample_values(
                run_test_id=run_test_id,
                case=case,
                repetition=repetition,
                status="failed",
                result=result,
                score=None,
                wall_ms=wall_ms,
                config=config,
                error_code=error_code,
                error_text=error_text,
                now=clock(),
                extra_detail=stream_detail,
            )
        else:
            values = _sample_values(
                run_test_id=run_test_id,
                case=case,
                repetition=repetition,
                status="completed" if verdict.score is not None else "failed",
                result=result,
                score=verdict,
                wall_ms=wall_ms,
                config=config,
                error_code=verdict.error_code,
                error_text=verdict.error_text,
                now=clock(),
                extra_detail=stream_detail,
            )
        if values["client_ttft_ms"] is None and is_supported(observed_ttft):
            # The adapter's own client timing wins where it has one — it starts its clock closer
            # to the socket than this module can — and this observation fills in where it does not.
            values["client_ttft_ms"] = float(observed_ttft)
    with database.write() as session:
        SampleRepository().insert(session, **values)
    return values


def _token_level_chunks(provider: Provider) -> bool:
    """Whether this provider declares one streamed delta to be one token.

    ``False`` when the provider cannot be asked. The honest default for "did this adapter declare
    it?" is no, and the consequence of guessing ``True`` would be a per-token latency figure
    derived from chunks (ModelRack spec §11.4).
    """
    try:
        return bool(provider.capabilities().token_level_chunks)
    except ProviderError:
        return False


def _measurement_or_none(value: object) -> Any:  # noqa: ANN401 — narrows a Measurement union
    """Return a measurement's number, or ``None`` when the provider did not report one.

    ``None`` in a *nullable numeric column* is this schema's "not reported", distinct from a real
    zero — the same distinction ``UNSUPPORTED`` draws in memory (ADR-0016). Where the distinction
    needs a *reason* as well, the column pair from
    :func:`~freeweight.infrastructure.db.types.measurement_columns` is used instead; on ``samples``
    the data model specifies plain nullable columns, because "the provider did not report a token
    count" needs no per-row explanation beyond the provider's declared capabilities.
    """
    return value if is_supported(value) else None


def _sample_values(  # noqa: PLR0913 — this *is* the column set
    *,
    run_test_id: str,
    case: Any,  # noqa: ANN401 — freeweight.domain.benchmark.BenchmarkCase
    repetition: int,
    status: str,
    result: Any,  # noqa: ANN401 — modelrack.GenerationResult, or None when the call failed
    score: ScoreResult | None,
    wall_ms: float,
    config: ExecutionConfig,
    error_code: str | None,
    error_text: str | None,
    now: datetime,
    extra_detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one ``samples`` row.

    The single place the column set is written down (see
    :meth:`~freeweight.infrastructure.db.repositories.runs.SampleRepository.insert` for why the
    repository does not restate it). ``score`` is ``None`` for every non-``completed`` status,
    which the table's own check constraint also enforces.

    ``prompt_id`` and ``prompt_version`` come from the case, so every sample can name the exact
    prompt record that produced it and re-render it (prompt standards §4). ``extra_detail`` is
    merged into ``result_json`` beneath the scorer's own evidence — that is where a streamed
    sample's inter-chunk series and its ``token_level_chunks`` claim live.
    """
    text = result.text if result is not None else ""
    detail: dict[str, Any] = dict(extra_detail or {})
    if score is not None:
        detail.update(score.detail)
    values: dict[str, Any] = {
        "run_test_id": run_test_id,
        "case_id": case.case_id,
        "ordinal": case.ordinal,
        "repetition": repetition,
        "status": status,
        "prompt_hash": f"sha256:{sha256_of(case.prompt)}",
        "rendered_prompt_hash": f"sha256:{sha256_of(case.prompt)}",
        "prompt_id": getattr(case, "prompt_id", None),
        "prompt_version": getattr(case, "prompt_version", None),
        "response_hash": f"sha256:{sha256_of(text)}" if result is not None else None,
        "response_text": text if (result is not None and config.store_responses) else None,
        "output_chars": len(text) if result is not None else None,
        "output_words": len(text.split()) if result is not None else None,
        "output_bytes": len(text.encode("utf-8")) if result is not None else None,
        "client_wall_ms": wall_ms,
        "client_ttft_ms": None,
        "score": score.score if (score is not None and status == "completed") else None,
        "score_method": score.method.value if score is not None else None,
        "result_json": _json_safe(detail) if detail else None,
        "error_code": error_code,
        "error_text": error_text,
        "created_at": now,
    }
    if result is not None:
        values.update(
            {
                "input_tokens": _measurement_or_none(result.usage.tokens.input_tokens),
                "output_tokens": _measurement_or_none(result.usage.tokens.output_tokens),
                "thinking_tokens": _measurement_or_none(result.usage.thinking_tokens),
                "tool_tokens": _measurement_or_none(result.usage.tool_tokens),
                "client_ttft_ms": _measurement_or_none(result.timing.client_ttft_ms),
                "backend_load_ms": _measurement_or_none(result.timing.backend_load_ms),
                "backend_prompt_eval_ms": _measurement_or_none(
                    result.timing.backend_prompt_eval_ms
                ),
                "backend_decode_ms": _measurement_or_none(result.timing.backend_decode_ms),
                "backend_total_ms": _measurement_or_none(result.timing.backend_total_ms),
                "finish_reason": str(result.finish_reason),
            }
        )
    return values


def _sample_row_facts(row: Any) -> SampleFacts:  # noqa: ANN401 — a samples row
    """Turn one stored sample row into the facts the metric formulas take."""
    return SampleFacts.from_row(
        {
            "status": row.status,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "thinking_tokens": row.thinking_tokens,
            "tool_tokens": row.tool_tokens,
            "output_chars": row.output_chars,
            "output_words": row.output_words,
            "output_bytes": row.output_bytes,
            "client_wall_ms": row.client_wall_ms,
            "client_ttft_ms": row.client_ttft_ms,
            "backend_load_ms": row.backend_load_ms,
            "backend_prompt_eval_ms": row.backend_prompt_eval_ms,
            "backend_decode_ms": row.backend_decode_ms,
            "backend_total_ms": row.backend_total_ms,
            "score": row.score,
            "result_json": row.result_json,
        }
    )


def _aggregate_run(  # noqa: PLR0913 — aggregation needs the run, its suite and its context
    database: Database,
    run_id: str,
    benchmark: Benchmark,
    run_test_ids: dict[str, str],
    *,
    context: _RunContext,
    clock: Clock,
) -> None:
    """Read this run's stored samples back and write its aggregate metric rows.

    **Reads the database, never an in-memory accumulator.** That is the whole defence against the
    phase's named failure mode, "partial aggregates written before samples are durable": this
    function has no other source of data, so it cannot describe a sample that has not committed.

    Idempotent: it deletes this run's aggregate rows and writes the current ones, so aggregating a
    resumed run twice leaves one correct set rather than two partial ones.

    The arithmetic itself is :func:`freeweight.domain.aggregation.aggregate_run`'s, which is where
    the cold/warm separation lives. The telemetry summary is appended as run-level rows carrying
    their ``gpu_index``, because there is no machine-wide GPU figure (ADR-0027 §5).
    """
    now = clock()
    with database.read() as session:
        repository = SampleRepository()
        groups = [
            SampleGroup(
                test_key=test.key,
                run_test_id=run_test_ids[test.key],
                measurement_class=MeasurementClass(test.measurement_class),
                metrics=test.metrics,
                samples=[
                    _sample_row_facts(row)
                    for row in repository.list_for_run_test(
                        session, run_test_ids[test.key], limit=100_000
                    )
                ],
            )
            for test in benchmark.tests
        ]
    rows = [
        {
            "run_id": run_id,
            "run_test_id": metric.run_test_id,
            "sample_id": None,
            "metric_key": metric.metric_key,
            "numeric_value": metric.numeric_value,
            "unavailable_reason": metric.unavailable_reason,
            "gpu_index": metric.gpu_index,
            "unit": metric.unit,
            "aggregation": metric.aggregation,
            "higher_is_better": metric.higher_is_better,
            "sample_count": metric.sample_count,
            "excluded_count": metric.excluded_count,
            "stddev": metric.stddev,
            "coefficient_of_variation": metric.coefficient_of_variation,
            "created_at": now,
        }
        for metric in aggregate_run(groups)
    ]
    rows.extend(_telemetry_rows(database, run_id, context=context, now=now))
    with database.write() as session:
        MetricValueRepository().replace_for_run(session, run_id, rows=rows)


def _telemetry_rows(
    database: Database, run_id: str, *, context: _RunContext, now: datetime
) -> list[dict[str, Any]]:
    """Build the run-level rows derived from this run's persisted telemetry.

    Empty when nothing was recorded — a run with no telemetry rows produces no telemetry metrics,
    rather than four rows of zero.

    Every row names its device. Where more than one GPU was visible and the provider does not
    report placement, each figure is written with ``unavailable_reason =
    "multi_gpu_placement_unknown"`` instead of a number (ADR-0027 §3).
    """
    window = load_window(database, run_id)
    if window.sample_count() == 0:
        return []
    summary = summarize_gpu_telemetry(
        window,
        gpu_index=context.gpu_index,
        multi_gpu_visible=context.multi_gpu_visible,
        # No adapter reports model placement per device yet; ADR-0027's "revisit when" names
        # exactly that as the trigger. Stated as a value rather than left implicit so the day one
        # does, this is the line that changes.
        placement_known=False,
    )
    definitions = {
        "peak_vram_bytes": ("bytes", False),
        "mean_gpu_power_watts": ("W", False),
        "gpu_energy_joules": ("J", False),
        "max_gpu_temperature_c": ("°C", False),
    }
    return [
        {
            "run_id": run_id,
            "run_test_id": None,
            "sample_id": None,
            "metric_key": key,
            "numeric_value": result.numeric_value,
            "unavailable_reason": result.unavailable_reason,
            "gpu_index": summary.gpu_index,
            "unit": definitions[key][0],
            "aggregation": "max" if key.startswith(("peak", "max")) else "mean",
            "higher_is_better": definitions[key][1],
            "sample_count": summary.sample_count,
            "excluded_count": 0,
            "stddev": None,
            "coefficient_of_variation": None,
            "created_at": now,
        }
        for key, result in summary.metric_results().items()
    ]


def repeat_run(  # noqa: PLR0913 — a repeat takes everything a fresh run does, plus the original
    database: Database,
    provider: Provider,
    collector: TelemetryCollector,
    registry: BenchmarkRegistry,
    *,
    run_ref: str,
    force: bool = False,
    label: str | None = None,
    clock: Clock = utc_now,
) -> RunSummary:
    """Queue a new run with a recorded run's identical effective configuration.

    The reproduction workflow of
    [Machine Identity §7](../../../../docs/architecture/machine-identity-and-reproducibility.md).
    The original run's frozen ``ExecutionConfig`` is reused verbatim — not re-resolved from
    configuration, which would silently repeat a *different* run whenever a default had changed —
    and the environment is checked against the original's fingerprint document before anything is
    written.

    Args:
        database: The application's database handle.
        provider: The configured provider.
        collector: The telemetry collector, used to profile this machine.
        registry: The benchmarks this build can run.
        run_ref: The run to repeat: a full ULID or an unambiguous prefix.
        force: Proceed past every blocker, recording the divergence on the new run.
        label: A label for the new run; defaults to naming the run it repeats.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The queued run.

    Raises:
        RunNotFound: ``run_ref`` matches no run.
        RepeatRefused: The environment can no longer satisfy the recorded configuration and
            ``force`` is ``False``. ``details["blockers"]`` names every field that moved, what was
            recorded and what is here now.
        BenchmarkNotFound: The original run's suite is not registered in this build.
    """
    with _translated(), database.read() as session:
        original = _resolve_run(session, run_ref)
        recorded = dict(original.fingerprint_document_json or {})
        config = ExecutionConfig.from_json(original.effective_config_json)
        suite = session.get(_suite_model(), original.suite_id)
        suite_key = suite.key if suite is not None else ""
        model = ModelRepository().get_by_id(session, original.model_id)
        # By the provider's *name*, not by the canonical ID. A canonical ID contains the digest, so
        # resolving by it would always find the weights the original run measured and could never
        # notice that the same name now serves different ones — which is the single most important
        # thing a repeat has to notice (ADR-0008, ADR-0024).
        model_ref = model.provider_model_name if model is not None else ""
        original_id = original.id

    observed = _observed_document(
        database, provider, collector, registry, suite_key, model_ref, config, clock=clock
    )
    blockers = check_repeatable(recorded, observed)
    if blockers and not force:
        raise RepeatRefused(
            f"Run {original_id} cannot be repeated here: "
            + "; ".join(blocker.explanation for blocker in blockers)
            + " Pass --force to proceed and record the divergence.",
            details={"run": original_id, "blockers": [item.as_json() for item in blockers]},
        )
    degradations = [divergence_degradation(blockers)] if blockers else []
    return create_run(
        database,
        provider,
        collector,
        registry,
        model_ref=model_ref,
        suite_key=suite_key,
        execution=config,
        label=label if label is not None else f"repeat of {original_id[:10]}",
        extra_degradations=degradations,
        clock=clock,
    )


def _observed_document(  # noqa: PLR0913 — mirrors the inputs create_run resolves
    database: Database,
    provider: Provider,
    collector: TelemetryCollector,
    registry: BenchmarkRegistry,
    suite_key: str,
    model_ref: str,
    config: ExecutionConfig,
    *,
    clock: Clock,
) -> dict[str, Any]:
    """Build the fingerprint document this environment would produce for the same request.

    Deliberately the *same* assembly function ``create_run`` uses
    (:func:`_fingerprint_document`): the whole point of the comparison is that any difference is a
    difference in the environment, and a second assembly path would eventually introduce one of
    its own.

    Raises:
        RunNotFound: The original run's model is no longer stored, which is itself the answer —
            a run cannot be repeated against a model this installation has forgotten.
    """
    benchmark = registry.get(suite_key)
    machine_profile = profile_machine(database, collector, clock=clock)
    capabilities = _provider_capabilities(provider)
    with _translated(), database.read() as session:
        model = _lookup_model(session, model_ref)
        if model is None:
            raise RunNotFound(
                f"Model {model_ref!r} is no longer stored, so the original run cannot be "
                "repeated against it.",
                details={"model": model_ref},
            )
        descriptor = ModelDescriptorRepository().latest_for_model(session, model.id)
        if descriptor is None:
            raise RunNotFound(
                f"Model {model_ref!r} has no descriptor snapshot; run `freeweight models refresh`.",
                details={"model": model_ref},
            )
        runtime_profile = RuntimeProfile()
        served = resolve_served_context(
            requested_context=runtime_profile.context_size,
            context_configurable=capabilities.context_configurable,
            advertised_max_context=(
                UNSUPPORTED if descriptor.max_context is None else float(descriptor.max_context)
            ),
        )
        document: dict[str, Any] = _json_safe(
            _fingerprint_document(
                model=model,
                descriptor=descriptor,
                runtime_profile=runtime_profile,
                provider_version=_provider_version(provider),
                machine_profile=machine_profile,
                benchmark=benchmark,
                execution=config,
                served=served,
            )
        )
        return document


def _finish(
    database: Database,
    publisher: RunEventPublisher,
    run_id: str,
    target: RunStatus,
    *,
    error_code: str | None = None,
    error_text: str | None = None,
    clock: Clock = utc_now,
) -> None:
    """Move a run to its terminal state and emit the terminal event.

    The terminal event is always emitted before the stream can close (API standards §8), which is
    what lets a client tell "finished" from "the connection dropped".

    Tolerates a run that is already terminal: a cancellation racing the final transition would
    otherwise raise out of the scheduler thread over a run that has already reached exactly the
    state everyone wanted.
    """
    now = clock()
    with database.write() as session:
        run = RunRepository().get_by_id(session, run_id)
        if run is None:  # pragma: no cover — the executor holds a claimed run
            return
        current = RunStatus(run.status)
        if current is target or current in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return
        if target is RunStatus.CANCELLED and current is RunStatus.RUNNING:
            # ``running → cancelled`` is not in the table (data model §3): a running run is
            # cancelled *through* ``cancelling``. Stepping through it here keeps the transition
            # table the single authority rather than adding a special case to it, and it is the
            # path a signal-stopped run takes when nothing set ``cancelling`` first.
            require_run_transition(current, RunStatus.CANCELLING)
            RunRepository().set_status(session, run_id, status=RunStatus.CANCELLING.value)
            current = RunStatus.CANCELLING
        require_run_transition(current, target)
        RunRepository().set_status(
            session,
            run_id,
            status=target.value,
            completed_at=now,
            error_code=error_code,
            error_text=error_text,
        )
        if target is RunStatus.CANCELLED:
            _cancel_pending_tests(session, run_id)
    event_type = {
        RunStatus.COMPLETED: "run.completed",
        RunStatus.FAILED: "run.failed",
        RunStatus.CANCELLED: "run.cancelled",
        RunStatus.INTERRUPTED: "run.interrupted",
    }[target]
    publisher.publish(
        run_id,
        event_type,
        message=error_text or f"Run {target.value}.",
        data={"status": target.value, "error_code": error_code},
    )
