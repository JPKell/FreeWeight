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

import dataclasses
import json
import logging
import math
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from modelrack.residency import find_resident
from modelrack.streaming import StreamCompleted, StreamFailed, ThinkingDelta, TokenDelta

from freeweight.__about__ import __version__
from freeweight.benchmarks.agent import benchmark as agent_benchmark
from freeweight.benchmarks.audit import benchmark as audit_benchmark
from freeweight.benchmarks.critique import benchmark as critique_benchmark
from freeweight.benchmarks.echo import benchmark as echo_benchmark
from freeweight.benchmarks.energy import benchmark as energy_benchmark
from freeweight.benchmarks.energy.energy import PowerSample
from freeweight.benchmarks.goal.runner import ERROR_JUDGEMENT_DEFERRED, build_goal_benchmark
from freeweight.benchmarks.instruction_following import benchmark as instruction_following_benchmark
from freeweight.benchmarks.judge import benchmark as judge_benchmark
from freeweight.benchmarks.long_context import benchmark as long_context_benchmark
from freeweight.benchmarks.memory_kv import benchmark as memory_kv_benchmark
from freeweight.benchmarks.memory_kv.benchmark import (
    CONTEXT_TOKENS_DETAIL_KEY,
    SampleWindow,
    stabilized_vram,
)
from freeweight.benchmarks.memory_kv.kv import ContextObservation, KvArchitecture
from freeweight.benchmarks.performance import benchmark as performance_benchmark
from freeweight.benchmarks.reliability import benchmark as reliability_benchmark
from freeweight.benchmarks.reliability.reliability import CaseAttempts
from freeweight.benchmarks.structured_output import benchmark as structured_output_benchmark
from freeweight.benchmarks.token_economy import benchmark as token_economy_benchmark
from freeweight.benchmarks.tool_recovery import benchmark as tool_recovery_benchmark
from freeweight.benchmarks.tool_use import benchmark as tool_use_benchmark
from freeweight.config import BenchmarkSettings, Settings, prompt_override_dir
from freeweight.domain.aggregation import AggregatedMetric, SampleGroup, aggregate_run
from freeweight.domain.benchmark import Benchmark, BenchmarkRegistry, BenchmarkTest
from freeweight.domain.goals.criteria import DEFAULT_RULE_TIMEOUT_MS
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
from freeweight.domain.scorers.tools import (
    ToolExpectation,
    ToolTranscript,
    TrajectoryScorer,
    annotate_calls,
)
from freeweight.domain.scoring import ScoreResult
from freeweight.infrastructure.db.errors import DatabaseUnavailable
from freeweight.infrastructure.db.repositories.calibration import JudgeVerdictRepository
from freeweight.infrastructure.db.repositories.goals import CriterionScoreRepository
from freeweight.infrastructure.db.repositories.model_descriptors import ModelDescriptorRepository
from freeweight.infrastructure.db.repositories.models import ModelRepository
from freeweight.infrastructure.db.repositories.runs import (
    BenchmarkRepository,
    MetricValueRepository,
    RunRepository,
    RunTestRepository,
    RuntimeProfileRepository,
    SampleRepository,
    ToolCallRepository,
)
from freeweight.infrastructure.db.repositories.telemetry import TelemetryRepository
from freeweight.services.events import RunEventPublisher
from freeweight.services.goals import LoadedGoal
from freeweight.services.machine import profile_machine
from freeweight.services.prompts import PromptLibrary, load_pack
from freeweight.services.telemetry_recording import (
    TelemetryRecorder,
    calibrate_sampling_overhead,
    load_series,
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
    "SKIP_UNSUPPORTED_CAPABILITY",
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


class PromptOverrideRefused(ConflictError):
    """A run would render a prompt the user has replaced, and did not ask to.

    Its own stable code rather than a generic conflict: the remedy is a specific flag, and a
    caller that cannot tell this apart from "a run is already active" cannot suggest it.

    Attributes:
        code: ``"PROMPT_OVERRIDE_REFUSED"``.
    """

    code: ClassVar[str] = "PROMPT_OVERRIDE_REFUSED"


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


@lru_cache(maxsize=1)
def active_prompt_library() -> PromptLibrary:
    """Load this build's prompt pack **with the user's overrides applied**, once per process.

    Prompt standards §6's override directory, wired here rather than in
    :func:`shipped_prompt_library` so that the two stay distinguishable: ``freeweight prompts
    show`` describes what shipped, and a run renders what is installed *plus* whatever the user
    dropped into ``$XDG_CONFIG_HOME/freeweight/prompts/``.

    An override does not stop the application from starting — a benchmark manifest is verified
    against the shipped records (:func:`freeweight.benchmarks.loading.verify_prompts`) — but it
    does stop a *run*, unless that run passes ``--allow-prompt-override``. An overridden prompt
    invalidates comparison with results produced by the shipped one, so the refusal is the point.

    Raises:
        PromptPackInvalid: The shipped pack is malformed, or an override file is not a valid
            prompt record. An override that cannot be parsed is a startup failure exactly as a
            shipped record is: the user asked for it to be used.
    """
    return load_pack(override_root=prompt_override_dir())


_DEFAULT_LONG_CONTEXT_MAX_TOKENS: int = BenchmarkSettings().long_context_max_tokens
"""The shipped ceiling, read from the settings model so the default lives in exactly one place."""


def build_registry(
    library: PromptLibrary | None = None,
    goals: Sequence[LoadedGoal] = (),
    *,
    rule_timeout_ms: int = DEFAULT_RULE_TIMEOUT_MS,
    long_context_max_tokens: int = _DEFAULT_LONG_CONTEXT_MAX_TOKENS,
) -> BenchmarkRegistry:
    """Build the registry of benchmarks this build can run.

    The one list. A suite that is not named here cannot be run, which is the point: benchmark
    availability is a deliberate, reviewable fact rather than a consequence of which modules
    happened to be imported. Phase 7's five quality suites, Phase 8's four judgement-dependent
    ones and Phase 9's three resource suites are on this list, which is the whole of what "adding
    a suite" means.

    Goal suites are the exception to "the one list", and deliberately so: they are authored by
    the user, so the list of them is whatever is installed under ``goals.root`` rather than
    something a reviewer approves. They are still built here, through one function, so that the
    web application, the CLI and the scheduler cannot end up with different sets of runnable
    benchmarks (ADR-0031 §1).

    Args:
        library: The prompt pack the suites render from, or ``None`` for this build's own, with
            the user's overrides applied. Every suite gets the *same* library instance, so two
            suites can never disagree about a prompt's hash.
        goals: The loaded goal packs, or none. Each becomes one ``goal.<slug>`` suite.
        rule_timeout_ms: The per-rule, per-sample budget a goal's rule criteria run under.
        long_context_max_tokens: The ceiling ``native.long_context``'s depth sweep is fitted to.
            It reaches the run record through that suite's own ``dataset_hashes``, so two ceilings
            separate results rather than averaging into one.

    Returns:
        The registry.

    Raises:
        ValueError: A suite's manifest declares a ``prompt_subset_hash`` that does not match the
            installed pack, or a goal declares no tasks. Refused at registry-build time — which is
            startup — because a suite whose provenance is wrong must not be runnable at all.
    """
    pack = library if library is not None else active_prompt_library()
    registry = BenchmarkRegistry(
        [
            echo_benchmark.build(),
            performance_benchmark.build(pack),
            token_economy_benchmark.build(pack),
            instruction_following_benchmark.build(pack),
            structured_output_benchmark.build(pack),
            tool_use_benchmark.build(pack),
            tool_recovery_benchmark.build(pack),
            agent_benchmark.build(pack),
            audit_benchmark.build(pack),
            critique_benchmark.build(pack),
            judge_benchmark.build(pack),
            long_context_benchmark.build(pack, max_context_tokens=long_context_max_tokens),
            memory_kv_benchmark.build(pack),
            energy_benchmark.build(pack),
            reliability_benchmark.build(pack),
            *(build_goal_benchmark(goal, rule_timeout_ms=rule_timeout_ms) for goal in goals),
        ]
    )
    _check_declared_capabilities(registry)
    return registry


def build_registry_for(settings: Settings, *, strict: bool = False) -> BenchmarkRegistry:
    """Build the registry for one installation, its user-authored goals included.

    The composition roots — :func:`freeweight.bootstrap.bootstrap`, the web lifespan and every CLI
    command that starts a run — call this rather than :func:`build_registry` directly, so that all
    three end up with the same set of runnable benchmarks. A goal that is installed but only
    reachable from the web UI would be a goal whose CLI runs silently measured something else.

    Args:
        settings: The resolved configuration; ``goals.root`` and ``goals.rule_timeout_ms`` are
            read from it.
        strict: ``True`` refuses the whole set when any pack is invalid, which is what startup
            wants: a malformed pack is a startup failure, not a mid-run surprise. ``False`` skips
            an unparseable pack, which is what a *listing* wants — nine working goals must not be
            hidden by a tenth with a typo in it, and ``goals validate`` is where the tenth is
            explained.

    Returns:
        The registry.

    Raises:
        GoalPackInvalid: ``strict`` is set and a pack is malformed or fails its lint.
    """
    from freeweight.services.goals import list_goals, load_goals

    root = settings.goals.root_path
    goals = load_goals(root) if strict else list_goals(root)
    return build_registry(
        goals=goals,
        rule_timeout_ms=settings.goals.rule_timeout_ms,
        long_context_max_tokens=settings.benchmarks.long_context_max_tokens,
    )


def _check_declared_capabilities(registry: BenchmarkRegistry) -> None:
    """Refuse a suite that requires a capability :class:`ProviderCapabilities` does not have.

    An unrecognised name is treated as *unmet* when a test runs (:func:`_unmet_capabilities`),
    because the honest reading of "I cannot tell whether this provider can do that" is that the
    test must not run. That is the right runtime behaviour and the wrong startup behaviour: a
    manifest saying ``tool_calls`` where it meant ``tool_calling`` would skip its suite on every
    provider, forever, and report a plausible reason for doing so.

    So the names are checked once, here, where "here" is startup — the same place a suite whose
    ``prompt_subset_hash`` is stale is refused, and for the same reason (ADR-0033 §9).

    Args:
        registry: The freshly built registry.

    Raises:
        ValueError: A test requires a name that is not a ``ProviderCapabilities`` field. The
            message lists the offending names and the suite that declared them.
    """
    from modelrack.provider import ProviderCapabilities

    known = {field.name for field in dataclasses.fields(ProviderCapabilities)}
    offenders: dict[str, list[str]] = {}
    for benchmark in registry.all():
        for test in benchmark.tests:
            unknown = sorted(
                str(name)
                for name in test.requires.get("provider_capabilities", ())
                if str(name) not in known
            )
            if unknown:
                offenders[f"{benchmark.manifest.key}/{test.key}"] = unknown
    if offenders:
        raise ValueError(
            f"Benchmark test(s) require capabilities ModelRack does not define: {offenders}. "
            f"The declared capability names are {sorted(known)}. Refused at startup: an unknown "
            "name is treated as unmet when a test runs, so a typo here would skip its suite on "
            "every provider and report a plausible reason for doing so."
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

    A goal suite additionally carries the columns the data model gives it: ``goal_id``, so a
    result joins back to the rubric that produced it, and ``goal_hash``, which separates results
    exactly as a benchmark version does (ADR-0032 §4). The hash is *also* inside the suite's
    version string (:func:`~freeweight.benchmarks.goal.runner.goal_suite_version`), which is what
    makes the separation structural rather than merely recorded.

    Returns:
        ``(suite_id, {test_key: test_row_id})``.
    """
    repository = BenchmarkRepository()
    manifest = benchmark.manifest
    goal_id: str | None = None
    goal_slug = manifest.body.get("goal_slug")
    if manifest.runner == "goal" and isinstance(goal_slug, str):
        from freeweight.infrastructure.db.repositories.goals import GoalRepository

        stored = GoalRepository().get_by_slug(session, goal_slug)
        goal_id = stored.id if stored is not None else None
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
        goal_id=goal_id,
        goal_hash=manifest.body.get("goal_hash"),
        prompt_subset_hash=manifest.prompt_subset_hash,
        prompt_refs_json=_json_safe([dict(entry) for entry in manifest.prompt_ids]),
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
    runtime_profile: RuntimeProfile | None = None,
    label: str | None = None,
    extra_degradations: Sequence[Degradation] | None = None,
    allow_prompt_override: bool = False,
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
        runtime_profile: How the model should be loaded and served (ADR-0023). ``None`` means
            provider defaults — a legal, hashable profile, and the one every run used before this
            was settable. Passing a profile with a ``context_size`` is what lets two runs of one
            model at two contexts be two subjects rather than two indistinguishable runs; it also
            moves the run's ``served_context_source`` from ``assumed`` to ``configured``, because
            the context is then a fact rather than the descriptor's advertised maximum.
        label: The user's label for this run.
        extra_degradations: Conditions to record on the run before it starts — in practice the
            divergences a ``--force``d repeat chose to proceed past, so the new run's provenance
            says it is not the same measurement rather than quietly claiming it is.
        allow_prompt_override: Whether to proceed when a prompt this suite declares has been
            replaced from the user's override directory. ``False`` refuses the run: an overridden
            prompt invalidates comparison with results produced by the shipped one, so the run has
            to say it means it (prompt standards §6). When ``True``, the overridden prompt ids
            become a reproducibility-fingerprint input, so the results separate rather than
            silently merging with runs of the shipped prompt.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The queued run.

    Raises:
        PromptOverrideRefused: A prompt this suite declares is overridden and
            ``allow_prompt_override`` was not passed.
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
    overrides = _declared_overrides(benchmark)
    if overrides and not allow_prompt_override:
        raise PromptOverrideRefused(
            f"Suite {suite_key!r} renders {list(overrides)}, which your override directory "
            f"({prompt_override_dir()}) replaces. A benchmark run with an overridden prompt is "
            "refused unless --allow-prompt-override is passed, because its results are not "
            "comparable with results produced by the shipped prompt.",
            details={"suite": suite_key, "overridden_prompts": list(overrides)},
        )
    if benchmark.manifest.runner == "goal" and not execution.store_responses:
        # Spec §12: a judged score the person who defined the rubric cannot re-read is not
        # auditable, which defeats the purpose. Forced on for goal runs and left alone for every
        # other suite, where the privacy default stands.
        execution = dataclasses.replace(execution, store_responses=True)
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
        # `None` is the caller saying "provider defaults", which ADR-0023 §1 makes a real,
        # hashable profile rather than an absence. Resolved once here so every use below — the
        # stored row, the served-context resolution and the fingerprint — sees the same object.
        runtime_profile = runtime_profile if runtime_profile is not None else RuntimeProfile()
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
            prompt_overrides=overrides,
        )
        degradations = [degradation.as_json() for degradation in (extra_degradations or ())]
        if overrides:
            # Marked on the run itself, not only inside the fingerprint document: prompt standards
            # §6 requires an override to be visible "in every record that used them", and a
            # degradation is the record field a reader already scans for "why is this run not
            # comparable with the others".
            degradations.append(
                Degradation(
                    kind="prompt_overridden",
                    detail={
                        "prompt_ids": list(overrides),
                        "override_root": str(prompt_override_dir()),
                        "prompt_source": "user_override",
                    },
                ).as_json()
            )
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


def _declared_overrides(benchmark: Benchmark) -> tuple[str, ...]:
    """Return the prompts this suite declares that a user override has replaced.

    Only the suite's *own* prompts, never the whole pack: an override of a record this benchmark
    does not render changes nothing about this benchmark's results, and refusing the run over one
    would be the pack-hash mistake ADR-0028 §1 exists to prevent, wearing a different hat.
    """
    declared = {str(entry["prompt_id"]) for entry in benchmark.manifest.prompt_ids}
    if not declared:
        return ()
    return tuple(
        prompt_id for prompt_id in active_prompt_library().overridden_ids if prompt_id in declared
    )


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
    prompt_overrides: Sequence[str] = (),
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
            # Present **only** when an override was actually used (prompt standards §6). Adding
            # the key unconditionally would change every existing run's fingerprint, and a run
            # that used no override is the same measurement it was before this phase.
            **({"prompt_overrides": list(prompt_overrides)} if prompt_overrides else {}),
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
    settings: Settings | None = None,
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
        settings: The whole resolved configuration, or ``None``. Needed only by a **goal** run
            with judged criteria, which has to assemble a jury: ``[judge]`` for its size and
            sampling, ``[calibration]`` for the shrinkage denominator, and
            ``providers.allow_remote`` for half of the remote opt-in. ``None`` is a real state and
            not a degraded one — a goal scored entirely by rules never needs any of it, and its
            judged criteria skip with ``judge_unavailable`` exactly as they do when no model can
            serve them.
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
            settings=settings,
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

    run_id: str
    suite_key: str
    suite_id: str
    config: ExecutionConfig
    identity: ModelIdentity
    model_canonical_id: str
    served_context: int | None
    gpu_index: int
    multi_gpu_visible: bool
    runtime_profile: RuntimeProfile = field(default_factory=RuntimeProfile)
    """The profile this run was created under, read back from its stored row.

    Carried here because it has to reach :func:`_build_request`: a profile that is stored, hashed
    into the fingerprint and never sent to the provider describes a run that did not happen. It is
    read from the row rather than re-resolved from configuration so that a resumed run resumes
    under the profile it started with."""

    criterion_ids: Mapping[str, str] = field(default_factory=dict)
    model_vram_bytes: Measurement = UNSUPPORTED
    """Device memory this model occupies at this run's served context, as the *provider* reports
    it for this model — not the device total.

    The figure a scheduler needs and the one ADR-0027 §3 and ``PHASE9_ISSUES.md`` §2 both ask for:
    device-wide VRAM includes every other process, so a slope fitted against it is contaminated by
    whatever else was resident. ``UNSUPPORTED`` when the provider cannot report per-model
    residency, which is a fact about the provider and never a zero."""

    model_total_bytes: Measurement = UNSUPPORTED
    """Total memory this model occupies, device and host together, as the provider reports it."""

    observed_context: Measurement = UNSUPPORTED
    """The context the provider says it is **actually** serving this model at.

    Distinct from the run's recorded ``served_context``, which is frozen into the fingerprint at
    creation and — when nothing requested a context — is the descriptor's *advertised* maximum
    flagged ``assumed`` (ADR-0023 §4). This is the observation that says whether that assumption
    was right. It is recorded as a metric and, where it disagrees, as a degradation; the frozen
    document is never rewritten, because provenance that changes after the fact is not
    provenance."""
    """``{criterion_key: goal_criteria row id}`` for a goal run; empty for every other suite.

    Resolved once, here, rather than per sample: a goal's criteria do not change during a run —
    the suite version carries the goal hash, so a change would be a different suite — and looking
    them up per sample would put three joins in the hot path for a value that cannot move."""


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
        criterion_ids: dict[str, str] = {}
        if suite.runner == "goal" and suite.goal_id:
            from freeweight.infrastructure.db.repositories.goals import GoalRepository

            criterion_ids = GoalRepository().criterion_ids(session, suite.goal_id)
        return _RunContext(
            run_id=run_id,
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
            runtime_profile=_stored_runtime_profile(session, run.runtime_profile_id),
            criterion_ids=criterion_ids,
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


def _bind_goal_jury(
    database: Database,
    provider: Provider,
    benchmark: Benchmark,
    *,
    context: _RunContext,
    settings: Settings | None,
) -> Benchmark:
    """Bind a jury to a goal benchmark for *this* run's candidate model.

    The candidate is not known when the registry is built — one registry serves every model — but
    it is exactly what decides who may judge: a juror never judges its own output (ADR-0031 §4).
    So the jury is assembled here, once per run, and the goal's scorer is rebound to it.

    Returns the benchmark **unchanged** in four cases, each of which is a real state rather than a
    failure: the suite is not a goal, the goal has no judged criterion, no configuration was
    supplied, or the goal has never been calibrated in a way this run can use. In every one of
    them the judged criteria skip with ``judge_unavailable``, the rule criteria still score, and
    the partial result says so (spec §13).
    """
    if benchmark.manifest.runner != "goal" or settings is None:
        return benchmark
    from freeweight.benchmarks.goal.runner import GoalScorer
    from freeweight.services.calibration import anchors_for_slug, validity_factor_for_slug
    from freeweight.services.jury import build_jury

    tests = list(benchmark.tests)
    scorer = tests[0].scorer if tests else None
    if not isinstance(scorer, GoalScorer) or not scorer.pack.judged_criteria:
        return benchmark
    try:
        available = [descriptor.identity.canonical_id for descriptor in provider.list_models()]
        remote = {descriptor.identity.canonical_id: False for descriptor in provider.list_models()}
    except ProviderError as exc:
        logger.warning("goal.jury_models_unavailable", extra={"code": exc.code})
        return benchmark
    jury = build_jury(
        provider,
        pack=scorer.pack,
        library=active_prompt_library(),
        settings=settings.judge,
        candidate_canonical_id=context.model_canonical_id,
        available=available,
        allow_remote_provider=settings.providers.allow_remote,
        anchors=anchors_for_slug(database, scorer.pack),
        seed=context.config.seed,
        remote=remote,
        # The same profile the candidate is served under: a juror graded at a
        # different context than the answers were generated at would be a second, unrecorded
        # variable in the measurement — and, left unset, is served at its advertised maximum.
        runtime_profile=context.runtime_profile,
    )
    # The jury this run will actually be measured by, recorded on the run itself. Self-judging is
    # refused and **recorded**, not silently discounted (ADR-0031 §4), and a jury smaller than the
    # goal asked for is a degradation the result has to carry — which is what makes "the refusal
    # appears in the run record" true rather than only true of the calibration report.
    _record_degradations(
        database,
        context.run_id,
        [
            Degradation(
                kind="judge_set",
                detail={
                    **jury.refusal_detail(),
                    "judge_validity_factor": validity_factor_for_slug(database, scorer.pack),
                },
            ),
            *(
                [
                    Degradation(
                        kind="jury_reduced",
                        detail={
                            "jurors": list(jury.assembly.jurors),
                            "requested_size": jury.assembly.requested_size,
                            "self_judging_refused": list(jury.assembly.self_judging_refused),
                        },
                    )
                ]
                if jury.assembly.reduced
                else []
            ),
        ],
    )
    bound = dataclasses.replace(
        scorer,
        judge=jury,
        judge_validity_factor=validity_factor_for_slug(database, scorer.pack),
        # The jury is bound now and called later. Generation defers every judged criterion, so the
        # candidate has the machine to itself for the whole of it; the jurors get it to themselves
        # afterwards (:func:`_judge_pending`). Interleaved, the two phases held the candidate and
        # every juror resident at once, which on a memory-constrained machine is the difference
        # between running and thrashing.
        defer_judging=True,
    )
    # ``SuiteBenchmark`` and ``SuiteTest`` are the concrete types a goal benchmark is built from;
    # ``Benchmark`` and ``BenchmarkTest`` are the protocols the run engine consumes. Rebinding is
    # a rebuild of the concrete pair, which is why the names are imported here rather than
    # widening the protocols to promise a ``replace``.
    from freeweight.benchmarks.loading import SuiteBenchmark, SuiteTest

    rebound = tuple(
        dataclasses.replace(test, scorer=bound) for test in tests if isinstance(test, SuiteTest)
    )
    if len(rebound) != len(tests):  # pragma: no cover — a goal suite is built from SuiteTests
        return benchmark
    return SuiteBenchmark(manifest=benchmark.manifest, tests=rebound)


def _execute_run_inner(  # noqa: PLR0913 — mirrors execute_run's collaborators
    database: Database,
    provider: Provider,
    registry: BenchmarkRegistry,
    publisher: RunEventPublisher,
    run_id: str,
    *,
    collector: TelemetryCollector | None,
    telemetry: TelemetrySettings | None,
    settings: Settings | None = None,
    clock: Clock,
) -> RunStatus:
    """Drive one run through its phases. See :func:`execute_run` for the contract."""
    context = _read_context(database, run_id)
    benchmark = _bind_goal_jury(
        database, provider, registry.get(context.suite_key), context=context, settings=settings
    )
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
        _warm(provider, context.identity, benchmark, config, context.runtime_profile)

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
    # After execution, not after warming: with `warmup_repetitions = 0` the warm step is a no-op
    # and the model is not loaded until the first measured call, so an observation taken earlier
    # either sees nothing or — worse — sees the *previous* run's instance still resident and
    # records its footprint against this run. The provider may evict between the last generation
    # and here, which yields UNSUPPORTED and therefore no row, which is the honest outcome.
    context = _observe_residency(provider, context)
    _record_degradations(database, run_id, _context_divergence(context))

    # --- judge ---------------------------------------------------------------------------------
    # After the telemetry recorder stopped and after residency was observed, both deliberately.
    # A juror is a *different model*: readings taken while one is resident describe it rather than
    # the candidate, so judging inside the recorded window would put whichever model happened to be
    # larger into this run's peak VRAM and its energy total. This is the second half of the reason
    # the phases are split — the first is that the candidate is evicted before a juror loads, so
    # the two are never resident together.
    _judge_pending(
        database, provider, publisher, benchmark, run_id=run_id, context=context, clock=clock
    )

    _aggregate_run(database, run_id, benchmark, run_test_ids, context=context, clock=clock)

    # --- complete ----------------------------------------------------------------------------
    _finish(database, publisher, run_id, RunStatus.COMPLETED, clock=clock)
    return RunStatus.COMPLETED


def _observe_residency(provider: Provider, context: _RunContext) -> _RunContext:
    """Record what this model occupies, from the provider's own per-model report.

    ``list_resident`` is live state and a provider is free not to offer it, so every failure path
    here yields ``UNSUPPORTED`` rather than a number: "this provider does not report residency" and
    "this model occupies no memory" are different facts, and only one of them is true
    (ADR-0016 §4).

    Paired with the run's ``served_context``, this is the pair a scheduler needs to decide whether
    a model fits: two runs of one model at two contexts give the bytes-per-token slope exactly,
    without fitting anything against device-wide telemetry that other processes also move.
    """
    from dataclasses import replace as _replace

    try:
        resident = find_resident(provider.list_resident(), context.identity)
    except Exception:  # noqa: BLE001 — provenance about a run must not be able to fail the run
        logger.warning("run.residency_unavailable", extra={"run_id": context.run_id})
        return context
    if resident is None:
        return context
    logger.info(
        "run.residency_observed",
        extra={
            "run_id": context.run_id,
            "vram_bytes": resident.vram_bytes,
            "served_context": context.served_context,
        },
    )
    return _replace(
        context,
        model_vram_bytes=resident.vram_bytes,
        model_total_bytes=resident.total_bytes,
        observed_context=getattr(resident, "context_length", UNSUPPORTED),
    )


def _evict_candidate(provider: Provider, context: _RunContext) -> bool:
    """Ask the provider to unload the candidate before the jury loads.

    The point of the two-phase split: with the candidate evicted, a juror has the machine to
    itself, so the peak footprint of a goal run is the larger of the two models rather than their
    sum. On a memory-constrained card that is the difference between running and spilling the KV
    cache to host memory.

    Best-effort by design. A provider that cannot be asked (no ``force_unload``) or fails the
    request is not a run failure — the jury will simply load alongside a candidate the provider
    evicts on its own schedule, which is what happened before this phase existed.

    Args:
        provider: The provider serving both models.
        context: The run's frozen inputs, naming the candidate.

    Returns:
        ``True`` when the candidate is known to be gone.
    """
    try:
        evicted = provider.unload(context.identity)
    except Exception:  # noqa: BLE001 — an eviction that fails must not fail the run
        logger.info("run.candidate_not_evicted", extra={"run_id": context.run_id})
        return False
    logger.info("run.candidate_evicted", extra={"run_id": context.run_id, "was_resident": evicted})
    return True


def _judge_pending(  # noqa: PLR0913 — the judging phase needs the run's whole context
    database: Database,
    provider: Provider,
    publisher: RunEventPublisher,
    benchmark: Benchmark,
    *,
    run_id: str,
    context: _RunContext,
    clock: Clock,
) -> int:
    """Score every sample this run left awaiting judgement, with the candidate evicted.

    The second half of a goal run. Generation scored the deterministic criteria and stored each
    sample as ``awaiting_judgement``; this reads those samples back, runs the jury over the
    **stored response text**, recombines the two halves into the composite, and finishes the
    sample.

    **Nothing about the measurement depends on the two phases being adjacent.** The jury reads
    text, not a live model — :meth:`JudgeCollaborator.score_judged` takes a ``str`` — so *when* it
    reads changes nothing about what it reads. What the split buys is that the candidate and the
    jurors are never resident together, and that the telemetry window closed before any juror
    loaded, so a goal run's memory and power figures describe the candidate rather than whichever
    model happened to be larger.

    A sample whose jury fails is finished as ``failed`` with the reason, not left pending: a run
    that completed with samples still awaiting judgement would be a run whose own status lied.

    Args:
        database: The application's database handle.
        provider: The provider serving the jurors.
        publisher: The run's event publisher.
        benchmark: The suite, whose scorer carries the bound jury.
        run_id: The run being judged.
        context: The run's frozen inputs.
        clock: The injected clock.

    Returns:
        How many samples were judged. ``0`` for every run that is not a goal run with judged
        criteria, which is most of them.
    """
    from freeweight.benchmarks.goal.runner import GoalScorer
    from freeweight.infrastructure.db.models_runs import Sample as SampleRow

    tests = list(benchmark.tests)
    scorer = tests[0].scorer if tests else None
    if not isinstance(scorer, GoalScorer) or scorer.judge is None or not scorer.defer_judging:
        return 0

    with database.read() as session:
        pending = SampleRepository().list_awaiting_judgement(session, run_id)
    if not pending:
        return 0

    _evict_candidate(provider, context)
    publisher.publish(
        run_id,
        "test.progress",
        message=f"Judging {len(pending)} samples with the configured jury.",
        data={"phase": "judging", "pending": len(pending)},
    )

    cases_by_id = {case.case_id: case for test in tests for case in test.cases()}
    judged = 0
    for position, row in enumerate(pending, start=1):
        _check_cancelled(database, run_id)
        case = cases_by_id.get(row.case_id)
        if case is None:  # pragma: no cover — the case set is this run's own suite
            continue
        try:
            verdict = _judge_one(scorer, case, row)
        except Exception as exc:  # noqa: BLE001 — one sample's jury must not fail the run
            # The same containment the generation phase gives a scorer defect (spec §13: a failed
            # test never fails the run). It matters more here: this phase runs after every sample
            # has been generated, so aborting would throw away a whole run's worth of work over one
            # unjudgeable answer.
            logger.warning("judging.failed", extra={"sample_id": row.id}, exc_info=exc)
            verdict = None
            error: tuple[str, str] | None = ("JUDGE_ERROR", str(exc))
        else:
            error = None
        status = "failed" if verdict is None else _scored_status(verdict)
        now = clock()
        with database.write() as session:
            sample = session.get(SampleRow, row.id)
            if sample is None:  # pragma: no cover — read moments ago in this process
                continue
            sample.status = status
            sample.score = None if verdict is None else verdict.score
            if verdict is not None:
                sample.score_method = verdict.method.value
                sample.result_json = _json_safe(dict(verdict.detail))
            sample.error_code = error[0] if error else (verdict.error_code if verdict else None)
            sample.error_text = error[1] if error else (verdict.error_text if verdict else None)
            _store_criterion_scores(session, sample.id, verdict, context, now=now)
        judged += 1
        publisher.publish(
            run_id,
            _sample_event_type(status),
            message=f"Judged {row.case_id} ({position}/{len(pending)}).",
            progress=(position, len(pending)),
            data={"phase": "judging", "case": row.case_id, "sample_id": row.id},
        )
    logger.info("run.judging_finished", extra={"run_id": run_id, "judged": judged})
    return judged


def _judge_one(scorer: Any, case: Any, row: Any) -> ScoreResult:  # noqa: ANN401 — see below
    """Score one stored sample's judged criteria and recombine it with its stored rules.

    The rule outcomes are read back from the sample rather than recomputed: they were measured
    against the same text by the same criteria, and running them twice invites two answers to one
    question. Only the judged criteria are new work.

    Args:
        scorer: The goal's :class:`~freeweight.benchmarks.goal.runner.GoalScorer`, jury bound.
        case: The case this sample answered.
        row: The stored ``samples`` row.

    Returns:
        The finished verdict — or one carrying ``JUDGE_UNAVAILABLE`` when the jury could not be
        reached, which is a degradation of this sample rather than a failure of the run.
    """
    from freeweight.benchmarks.goal.runner import finish_deferred

    return finish_deferred(
        scorer,
        case=case,
        response_text=row.response_text or "",
        stored_detail=row.result_json if isinstance(row.result_json, dict) else {},
    )


def _context_divergence(context: _RunContext) -> list[Degradation]:
    """Record it when the context this run *assumed* is not the one it got.

    A run that names no ``context_size`` records the descriptor's advertised maximum, flagged
    ``assumed`` — and the provider frequently serves something else entirely. The frozen
    fingerprint is never rewritten (provenance that changes after the fact is not provenance), so
    the disagreement is surfaced the way every other "the conditions were not what the record
    implies" fact is: as a degradation carrying both numbers, which a reader sees beside the
    results rather than discovering months later as unexplained dispersion.

    Silent when the run configured its own context, because then there is nothing to assume.
    """
    if not is_supported(context.observed_context):
        return []
    recorded = context.served_context
    observed = int(float(context.observed_context))
    if recorded is None or recorded == observed:
        return []
    return [
        Degradation(
            kind="served_context_assumed_incorrectly",
            detail={
                "recorded_served_context": recorded,
                "observed_served_context": observed,
                "explanation": (
                    f"This run's record says it was served {recorded} tokens of context, which "
                    "was assumed from the model's advertised maximum because nothing requested "
                    f"one. The provider reports it actually served {observed}. Set "
                    "[runtime].context_size, or pass --context-size, to make the recorded number "
                    "a fact."
                ),
            },
        )
    ]


def _residency_rows(context: _RunContext, *, run_id: str, now: datetime) -> list[dict[str, Any]]:
    """Build the run-level rows for what the model occupied while it was measured.

    **Only what was actually observed**, exactly as :func:`_telemetry_rows` emits nothing when no
    telemetry was recorded. A provider that does not report per-model residency produces no row
    rather than a row saying "unsupported": every other run-level metric in this application is one
    a suite declared, and inventing two undeclared keys on every run of every suite would be metric
    sprawl that ``tests/integration/test_quality_suites.py`` refuses on purpose. The run still
    records ``provider_kind``, so a consumer can tell which providers can answer this at all.
    """
    return [
        {
            "run_id": run_id,
            "run_test_id": None,
            "sample_id": None,
            "metric_key": key,
            "numeric_value": float(value),
            "unavailable_reason": None,
            "gpu_index": None,
            "unit": unit,
            "aggregation": "single",
            "higher_is_better": False,
            "sample_count": 1,
            "excluded_count": 0,
            "stddev": None,
            "coefficient_of_variation": None,
            "created_at": now,
        }
        for key, value, unit in (
            ("model_vram_bytes", context.model_vram_bytes, "bytes"),
            ("model_total_bytes", context.model_total_bytes, "bytes"),
            ("served_context_observed", context.observed_context, "tokens"),
        )
        if is_supported(value)
    ]


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
    runtime_profile: RuntimeProfile,
) -> None:
    """Run the configured warm-up generations, discarding their results.

    Warm-up exists so that first-call model loading is not counted as inference time, and it warms
    under the **same runtime profile** the measured calls use — warming at one context and
    measuring at another would force the provider to reload between them, which is precisely the
    cost warm-up exists to move outside the measurement. Its output is deliberately thrown away —
    a warm-up sample stored beside measured ones would be exactly the cold/warm mixing benchmark
    catalog §3.1 forbids.

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
            provider.generate(_build_request(identity, first_case, config, runtime_profile))
        except ProviderError as exc:
            logger.warning("run.warmup_failed", extra={"code": exc.code})
            return


def _build_request(
    identity: ModelIdentity,
    case: Any,  # noqa: ANN401 — a BenchmarkCase
    config: ExecutionConfig,
    runtime_profile: RuntimeProfile | None = None,
) -> GenerationRequest:
    """Build one provider request from a case, the run's execution config and its runtime profile.

    A case's system turn, where it has one, becomes a leading ``SYSTEM`` message rather than being
    prepended to the user text: a provider applies its own template to the two roles differently,
    and merging them would measure a prompt nobody wrote.

    **The runtime profile is sent, not merely stored.** It is what carries ``context_size`` to the
    provider as ``num_ctx`` (ADR-0023 §4). Omitting it — which this function did until the profile
    became settable — meant every run was served at whatever the provider chose while its record
    claimed a profile it had never been asked for.
    """
    messages = []
    if getattr(case, "system_prompt", None):
        messages.append(Message(role=Role.SYSTEM, content=case.system_prompt))
    messages.append(Message(role=Role.USER, content=case.prompt))
    return GenerationRequest(
        identity=identity,
        messages=tuple(messages),
        runtime_profile=runtime_profile if runtime_profile is not None else RuntimeProfile(),
        sampling=SamplingParameters(
            temperature=config.temperature,
            top_p=config.top_p,
            seed=config.seed,
            max_output_tokens=config.max_output_tokens,
        ),
        timeout_seconds=config.test_timeout_seconds,
    )


SKIP_UNSUPPORTED_CAPABILITY = "unsupported_capability"
"""``run_tests.skip_reason`` for a test the provider cannot be asked to perform (data model §2).

Spec §13's first-named skip reason, and the one Phase 7 makes reachable: a model or provider
without tool calling or structured output records this and **no score at all**. A zero here would
say the model tried and failed; the truth is that it was never asked
([graceful degradation](../../../../docs/architecture/graceful-degradation.md), "Model lacks a
required capability").
"""


def _unmet_capabilities(provider: Provider, test: BenchmarkTest) -> set[str]:
    """Return the capabilities ``test`` requires that ``provider`` does not declare.

    The names in a test's ``requires["provider_capabilities"]`` are
    :class:`~modelrack.provider.ProviderCapabilities` field names, matched exactly. A requirement
    naming a flag this build's ModelRack does not have is treated as **unmet**, not as satisfied:
    the honest reading of "I cannot tell whether this provider can do that" is that the test must
    not run, and the alternative would silently run a suite against a provider nobody checked.

    A provider that cannot be asked about its capabilities at all (:class:`ProviderError` from
    :meth:`capabilities`) leaves the requirement unmet for the same reason.

    Args:
        provider: The provider this run uses.
        test: The test about to run.

    Returns:
        The missing capability names, empty when everything the test needs is declared.
    """
    required = test.requires.get("provider_capabilities", ())
    wanted = [str(name) for name in required]
    if not wanted:
        return set()
    capabilities = _provider_capabilities(provider)
    return {name for name in wanted if not getattr(capabilities, name, False)}


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
        planned = row.total_cases * row.repetitions
        unmet = _unmet_capabilities(provider, test)
        if unmet and current is TestStatus.PENDING:
            require_test_transition(current, TestStatus.SKIPPED)
            RunTestRepository().set_status(
                session,
                run_test_id,
                status=TestStatus.SKIPPED.value,
                skip_reason=SKIP_UNSUPPORTED_CAPABILITY,
                completed_at=clock(),
                error_code="CAPABILITY_UNSUPPORTED",
                error_text=(
                    f"This provider does not declare {sorted(unmet)}, which {test.key} requires. "
                    "The test was not run, and contributes no score."
                ),
                measurement_class=test.measurement_class,
            )
            skipped = True
        else:
            skipped = False
        if current is TestStatus.PENDING and not skipped:
            require_test_transition(current, TestStatus.RUNNING)
            RunTestRepository().set_status(
                session,
                run_test_id,
                status=TestStatus.RUNNING.value,
                started_at=clock(),
                measurement_class=test.measurement_class,
            )
        already = SampleRepository().existing_keys(session, run_test_id)

    if skipped:
        publisher.publish(
            run_id,
            "test.completed",
            message=(f"Test {test.key} skipped: the provider does not declare {sorted(unmet)}."),
            progress=(completed_samples + planned, total_samples),
            data={
                "test": test.key,
                "run_test_id": run_test_id,
                "status": TestStatus.SKIPPED.value,
                "skip_reason": SKIP_UNSUPPORTED_CAPABILITY,
                "missing_capabilities": sorted(unmet),
            },
        )
        return completed_samples + planned

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
                        context=context,
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
        # Generated, rules scored, jury still to come. `test.progress` for the same reason a skip
        # uses it: the sample is not finished, so announcing `sample.completed` would be a lie to
        # anything counting completions, and the vocabulary gains no frame-shaped addition.
        "awaiting_judgement": "test.progress",
    }.get(status, "sample.failed")


def _scored_status(verdict: ScoreResult | None) -> str:
    """The sample status a scorer's verdict implies.

    ``awaiting_judgement`` when the scorer deferred its judged criteria to the judging phase: the
    model answered and the rules scored, so calling it ``failed`` would blame the generation for
    work that has not been attempted yet, and calling it ``completed`` would publish a composite
    computed over the rules alone.
    """
    if verdict is None:
        return "failed"
    if verdict.error_code == ERROR_JUDGEMENT_DEFERRED:
        return "awaiting_judgement"
    return "completed" if verdict.score is not None else "failed"


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
    context: _RunContext,
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

    A test declaring an ``interaction`` — a tool loop, or a call plus one corrective retry — is
    executed through :func:`_run_interactive_case` instead. The difference is declared by the
    benchmark rather than inferred from its scorer, so a suite that needs several turns cannot be
    quietly run as one call and scored on the wrong text.

    Returns:
        The stored column values, for the caller's event payload.
    """
    interaction = getattr(test, "interaction", None)
    if interaction is not None:
        return _run_interactive_case(
            database,
            provider,
            run_test_id=run_test_id,
            context=context,
            test=test,
            case=case,
            repetition=repetition,
            config=config,
            clock=clock,
            interaction=interaction,
        )
    request = _build_request(context.identity, case, config, context.runtime_profile)
    # Both clocks, deliberately: the monotonic one measures the request, the wall-clock one places
    # it on the same timeline as the telemetry samples so a window can be intersected rather than
    # reconstructed.
    started_at = clock()
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

    verdict: ScoreResult | None = None
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
            started_at=started_at,
            extra_detail=stream_detail,
        )
    else:
        try:
            verdict = test.scorer.score(case, result.text)
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
                started_at=started_at,
                extra_detail=stream_detail,
            )
        else:
            values = _sample_values(
                run_test_id=run_test_id,
                case=case,
                repetition=repetition,
                status=_scored_status(verdict),
                result=result,
                score=verdict,
                wall_ms=wall_ms,
                config=config,
                error_code=verdict.error_code,
                error_text=verdict.error_text,
                now=clock(),
                started_at=started_at,
                extra_detail=stream_detail,
            )
        if values["client_ttft_ms"] is None and is_supported(observed_ttft):
            # The adapter's own client timing wins where it has one — it starts its clock closer
            # to the socket than this module can — and this observation fills in where it does not.
            values["client_ttft_ms"] = float(observed_ttft)
    with database.write() as session:
        sample = SampleRepository().insert(session, **values)
        _store_criterion_scores(session, sample.id, verdict, context, now=values["created_at"])
    return values


def _run_interactive_case(  # noqa: PLR0913 — one sample needs its whole context
    database: Database,
    provider: Provider,
    *,
    run_test_id: str,
    context: _RunContext,
    test: BenchmarkTest,
    case: Any,  # noqa: ANN401 — freeweight.domain.benchmark.BenchmarkCase
    repetition: int,
    config: ExecutionConfig,
    clock: Clock,
    interaction: Any,  # noqa: ANN401 — freeweight.benchmarks.interaction.Interaction
) -> dict[str, Any]:
    """Execute one case that needs more than one provider call, and store the sample.

    The engine keeps everything that is not the benchmark's business: it builds each request from
    the run's *frozen* execution config — same sampling, same seed, same timeout on every turn, so
    a five-turn trajectory is as reproducible as a one-turn answer — counts what every turn cost,
    and stores one sample for the whole interaction. The benchmark decides only what to say next.

    **Token counts are summed across the turns.** A tool trajectory's ``output_tokens`` is what
    the whole trajectory generated, not what its last turn did; ``token_economy`` figures over a
    tool suite would otherwise report a fraction of the real cost. The provider's own per-call
    timings are left on the last turn alone, because summing a backend duration across calls
    would invent a figure no provider reported.

    **A trajectory is scored by a trajectory scorer.** Where the interaction produced one and the
    test's scorer accepts one, ``score_trajectory`` is used; otherwise the final text is scored
    the ordinary way. A scorer that raises is contained exactly as in :func:`_run_one_case` — one
    failed sample, never a failed run.

    Returns:
        The stored column values, for the caller's event payload.
    """
    results: list[Any] = []

    def caller(
        messages: Sequence[Message],
        *,
        tools: Sequence[Any] = (),
        response_format: Any = None,  # noqa: ANN401 — modelrack.ResponseFormat
    ) -> Any:  # noqa: ANN401 — modelrack.GenerationResult
        """Produce the next assistant turn under this run's frozen execution parameters."""
        result = provider.generate(
            GenerationRequest(
                identity=context.identity,
                messages=tuple(messages),
                sampling=SamplingParameters(
                    temperature=config.temperature,
                    top_p=config.top_p,
                    seed=config.seed,
                    max_output_tokens=config.max_output_tokens,
                ),
                tools=tuple(tools),
                response_format=response_format,
                timeout_seconds=config.test_timeout_seconds,
            )
        )
        results.append(result)
        return result

    # An interaction's window spans every turn it made, so it opens before the first one.
    started_at = clock()
    started_ns = monotonic_ns()
    error_code: str | None = None
    error_text: str | None = None
    try:
        outcome = interaction.run(caller, case)
    except Exception as exc:  # noqa: BLE001 — a broken interaction fails one sample (spec §13)
        logger.warning("sample.interaction_error", extra={"test": test.key}, exc_info=exc)
        outcome = None
        error_code, error_text = "INTERNAL_ERROR", str(exc)
    wall_ms = elapsed_ms(started_ns)

    last = results[-1] if results else None
    detail: dict[str, Any] = {}
    verdict: ScoreResult | None = None
    if outcome is not None:
        detail.update(outcome.detail)
        if outcome.error_code is not None:
            error_code, error_text = outcome.error_code, outcome.error_text
        if last is not None:
            try:
                verdict = _score_interaction(test, case, outcome)
            except Exception as exc:  # noqa: BLE001 — a scorer defect fails one sample
                logger.warning("sample.scorer_error", extra={"test": test.key}, exc_info=exc)
                error_code, error_text = "SCORER_ERROR", str(exc)

    status = _scored_status(verdict)
    if last is None:
        status = "timeout" if error_code == "PROVIDER_TIMEOUT" else "failed"
    values = _sample_values(
        run_test_id=run_test_id,
        case=case,
        repetition=repetition,
        status=status,
        result=last,
        score=verdict,
        wall_ms=wall_ms,
        config=config,
        error_code=error_code if verdict is None else verdict.error_code or error_code,
        error_text=error_text if verdict is None else verdict.error_text or error_text,
        now=clock(),
        started_at=started_at,
        extra_detail=detail,
        text=outcome.text if outcome is not None else "",
    )
    _sum_usage(values, results)
    with database.write() as session:
        sample = SampleRepository().insert(session, **values)
        transcript = outcome.transcript if outcome is not None else None
        if transcript is not None:
            ToolCallRepository().insert_many(
                session, _tool_call_rows(sample.id, case, transcript, now=values["created_at"])
            )
        _store_criterion_scores(session, sample.id, verdict, context, now=values["created_at"])
    return values


def _store_criterion_scores(
    session: Session,
    sample_id: str,
    verdict: ScoreResult | None,
    context: _RunContext,
    *,
    now: datetime,
) -> None:
    """Write one goal sample's ``criterion_scores`` rows, in the sample's own transaction.

    Data model §2's per-criterion record: what a goal's headline number drills to. Written here
    rather than by the scorer for the same two reasons the tool-call rows are — a
    :class:`~freeweight.domain.scoring.ScoreResult` has nowhere to put a row, and a suite is
    entitled to its evidence on the record even when its scorer refused to produce a number.

    In the **same transaction as the sample**, so a composite can never be read back with fewer
    criteria than the sample it belongs to. A sample whose judging was deferred gets **no rows
    until the judging phase finishes it**, for the same reason: half a criterion set is exactly
    the partial read this rule exists to prevent.

    A skipped or errored criterion is written with ``raw_score = NULL``; the check constraint
    ``ck_criterion_scores_score_null_unless_scored`` makes that structural rather than a
    convention (ADR-0016).

    A judged criterion additionally writes its ``judge_verdicts`` — one row per juror per
    repetition, refusals included — in the same transaction. Kept in full rather than summarized:
    the jury's dispersion *is* the measurement's error bar, and averaging it at write time would
    destroy the thing being characterized.

    Args:
        session: The session the sample was just written in.
        sample_id: The stored sample.
        verdict: The scorer's result, or ``None`` when scoring never ran.
        context: The run's context, carrying ``{criterion_key: row id}``.
        now: The sample's own timestamp.
    """
    if not context.criterion_ids or verdict is None:
        return
    if verdict.error_code == ERROR_JUDGEMENT_DEFERRED:
        # A deferred sample has no complete criterion set yet, so it gets no rows at all. Writing
        # the rule outcomes now and the judged ones later would break this function's own
        # invariant — that a composite can never be read back with fewer criteria than the sample
        # it belongs to — in the window between the phases, and would collide with the judging
        # phase's insert besides. The rule outcomes are not lost: they are carried in the sample's
        # `result_json` and read back by `finish_deferred`.
        return
    declared = verdict.detail.get("criteria")
    if not isinstance(declared, list) or not declared:
        return
    rows = [
        {
            "sample_id": sample_id,
            "goal_criterion_id": context.criterion_ids[str(entry["key"])],
            "criterion_key": str(entry["key"]),
            "rung": str(entry["rung"]),
            "raw_score": entry.get("raw_score"),
            "weight": float(entry["weight"]),
            "gated": bool(entry.get("gated", False)),
            "status": str(entry["status"]),
            "skip_reason": entry.get("skip_reason"),
            "detail_json": _json_safe(entry.get("detail") or {}),
            "created_at": now,
        }
        for entry in declared
        if isinstance(entry, dict) and str(entry.get("key", "")) in context.criterion_ids
    ]
    if not rows:
        return
    stored = CriterionScoreRepository().insert_many(session, rows)
    verdicts: list[dict[str, Any]] = []
    by_key = {str(entry["key"]): entry for entry in declared if isinstance(entry, dict)}
    for score in stored:
        entry = by_key.get(score.criterion_key, {})
        detail = entry.get("detail") if isinstance(entry, dict) else None
        recorded = detail.get("judge_verdicts") if isinstance(detail, dict) else None
        if not isinstance(recorded, list):
            continue
        verdicts.extend(
            {
                "criterion_score_id": score.id,
                "juror_model_id": None,
                "juror_canonical_id": str(item.get("juror_canonical_id", "")),
                "juror_ordinal": int(item.get("juror_ordinal", 0)),
                "repetition": int(item.get("repetition", 1)),
                "grade": item.get("grade"),
                "pairwise_choice": item.get("pairwise_choice"),
                "presentation_order": str(item.get("presentation_order", "candidate_first")),
                "rationale": item.get("rationale"),
                "rationale_sha256": item.get("rationale_sha256"),
                "prompt_id": item.get("prompt_id"),
                "prompt_version": item.get("prompt_version"),
                "judge_prompt_sha256": item.get("judge_prompt_sha256"),
                "remote": bool(item.get("remote", False)),
                "latency_ms": item.get("latency_ms"),
                "input_tokens": item.get("input_tokens"),
                "output_tokens": item.get("output_tokens"),
                "refused_reason": item.get("refused_reason"),
                "created_at": now,
            }
            for item in recorded
            if isinstance(item, dict)
        )
    if verdicts:
        JudgeVerdictRepository().insert_many(session, verdicts)


def _tool_call_rows(
    sample_id: str,
    case: Any,  # noqa: ANN401 — freeweight.domain.benchmark.BenchmarkCase
    transcript: ToolTranscript,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Build this sample's ``tool_calls`` rows (data model §2).

    Written in the **same transaction as the sample**, so a trajectory can never be read back
    shorter than the sample it belongs to, and cascade-deleted with it.

    The comparison against what the case required is
    :func:`~freeweight.domain.scorers.tools.annotate_calls`' — the per-call view of the same greedy
    pairing the metrics aggregate — so a stored row and the rate computed over it cannot disagree
    about which call satisfied which requirement. It is done here rather than in the scorer because
    a :class:`~freeweight.domain.scoring.ScoreResult` has nowhere to put a row, and because a suite
    is entitled to a trajectory on the record even when its scorer refused to produce a number.
    """
    declared = case.expectation.get("tools")
    expectation = ToolExpectation.from_json(declared if isinstance(declared, dict) else {})
    verdicts = annotate_calls(expectation, transcript)
    per_turn: dict[int, int] = {}
    rows: list[dict[str, Any]] = []
    for call, verdict in zip(transcript.calls, verdicts, strict=True):
        call_index = per_turn.get(call.step, 0)
        per_turn[call.step] = call_index + 1
        rows.append(
            {
                "sample_id": sample_id,
                "turn_index": call.step,
                "call_index": call_index,
                "tool_name": call.name,
                "arguments_json": _json_safe(dict(call.arguments)),
                "schema_valid": call.arguments_parsed and call.arguments_valid,
                "expected_tool": verdict.expected_tool,
                "correct_tool": verdict.correct_tool,
                "correct_arguments": verdict.correct_arguments,
                "status": call.status,
                "latency_ms": call.duration_ms,
                "result_hash": call.result_hash,
                "created_at": now,
            }
        )
    return rows


def _score_interaction(
    test: BenchmarkTest,
    case: Any,  # noqa: ANN401 — freeweight.domain.benchmark.BenchmarkCase
    outcome: Any,  # noqa: ANN401 — freeweight.benchmarks.interaction.InteractionOutcome
) -> ScoreResult:
    """Score one interaction with whichever instrument its test's scorer is.

    A transcript is scored by a :class:`~freeweight.domain.scorers.tools.TrajectoryScorer`; there
    is no fallback that would score a trajectory on its final sentence, because a suite measuring
    tool selection would then silently report an exact-match figure instead.
    """
    scorer = test.scorer
    if outcome.transcript is not None and isinstance(scorer, TrajectoryScorer):
        return scorer.score_trajectory(case, outcome.transcript)
    return scorer.score(case, outcome.text)


def _sum_usage(values: dict[str, Any], results: Sequence[Any]) -> None:
    """Replace the last turn's token counts with the whole interaction's.

    Summed only over the turns that reported a count: a provider that counted three turns of four
    has told the truth about three, and treating the fourth as zero is the fabrication ADR-0016
    forbids. When no turn reported one at all the column stays ``None`` — "not reported", which is
    what it was.
    """
    if len(results) < 2:
        return
    for column, path in (
        ("input_tokens", ("usage", "tokens", "input_tokens")),
        ("output_tokens", ("usage", "tokens", "output_tokens")),
        ("thinking_tokens", ("usage", "thinking_tokens")),
        ("tool_tokens", ("usage", "tool_tokens")),
    ):
        reported: list[float] = []
        for result in results:
            value: Any = result
            for attribute in path:
                value = getattr(value, attribute)
            if is_supported(value):
                reported.append(float(value))
        values[column] = sum(reported) if reported else None


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
    started_at: datetime | None = None,
    extra_detail: Mapping[str, Any] | None = None,
    text: str | None = None,
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

    ``started_at`` is the wall-clock instant the request went out, recorded so a sample's telemetry
    window is a *fact* rather than the ``now - client_wall_ms`` reconstruction it used to be. It is
    ``None`` for a sample that was never sent — a skip has no window, and a zero-length one is the
    honest answer for it.
    """
    # ``text`` is supplied by a multi-turn interaction, whose answer is not necessarily its last
    # turn's text — a trajectory that ran out of steps ended on a tool request, and hashing that
    # as the response would attribute the model an answer it never gave.
    if text is None:
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
        "started_at": started_at,
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

    Three suites produce figures no scorer can see — a KV slope needs the descriptor and the
    device's memory readings, an energy estimate needs the power series, a ``pass@k`` needs every
    repetition of every case. :func:`_suite_derived_metrics` is the one seam that hands those
    suites what they need; the arithmetic stays in each benchmark package, where it is testable
    without a database (Phase 9).
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
    rows.extend(_residency_rows(context, run_id=run_id, now=now))
    rows.extend(
        _metric_row(metric, run_id=run_id, now=now)
        for metric in _suite_derived_metrics(database, run_id, benchmark, run_test_ids, context)
    )
    with database.write() as session:
        MetricValueRepository().replace_for_run(session, run_id, rows=rows)


_SUITES_WITH_DERIVED_METRICS: frozenset[str] = frozenset(
    {"native.memory_kv", "native.energy", "native.reliability"}
)
"""Suites whose run-level metrics cannot be computed from a sample alone.

A short, explicit list rather than a hook every benchmark may implement: three suites need the
descriptor, the telemetry series or every stored repetition, and a general extension point would
invite the fourth to reach for the database from inside a scorer."""


def _metric_row(metric: AggregatedMetric, *, run_id: str, now: datetime) -> dict[str, Any]:
    """Render one aggregate into the ``metric_values`` row shape."""
    return {
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


@dataclass(frozen=True, slots=True)
class _StoredSample:
    """One stored sample, in the shape the Phase 9 derivations read it in.

    Deliberately not :class:`~freeweight.domain.metrics.SampleFacts`: these derivations need the
    two things ``SampleFacts`` drops because no per-sample formula needs them — *when* the sample
    ran, so a telemetry reading can be attributed to it, and the response hash, so two repetitions
    can be compared without either storing its response text (spec §14).
    """

    case_id: str
    status: str
    score: float | None
    response_hash: str | None
    started_at: datetime
    ended_at: datetime
    prompt_eval_ms: float | None
    output_tokens: int | None
    detail: Mapping[str, Any]


def _stored_samples(session: Session, run_test_id: str) -> list[_StoredSample]:
    """Read one test's samples in the shape the derivations need.

    A sample's window is ``[started_at, created_at]`` — both recorded, the first when the request
    went out and the second when it came back. It decides which telemetry observations fell inside
    a request, which is what makes energy attributable to work rather than to a run's whole
    wall-clock span.

    A row written before ``started_at`` existed, or a sample that was never sent, falls back to the
    old reconstruction ``created_at - client_wall_ms``, and a row with neither collapses to a
    zero-length window that no observation falls inside — an absent window, not a guessed one.
    """
    rows = SampleRepository().list_for_run_test(session, run_test_id, limit=100_000)
    samples: list[_StoredSample] = []
    for row in rows:
        ended = row.created_at
        if row.started_at is not None:
            began = row.started_at
        else:
            wall_ms = float(row.client_wall_ms) if row.client_wall_ms is not None else 0.0
            began = ended - timedelta(milliseconds=wall_ms)
        samples.append(
            _StoredSample(
                case_id=row.case_id,
                status=row.status,
                score=row.score,
                response_hash=row.response_hash,
                started_at=began,
                ended_at=ended,
                prompt_eval_ms=row.backend_prompt_eval_ms,
                output_tokens=row.output_tokens,
                detail=row.result_json if isinstance(row.result_json, dict) else {},
            )
        )
    return samples


def _kv_architecture(session: Session, run: Any) -> KvArchitecture:  # noqa: ANN401 — a runs row
    """Read the descriptor snapshot and runtime profile this run was measured against.

    The snapshot the run recorded, never the latest one: a descriptor refreshed since the run
    describes a model this run did not measure.
    """
    from freeweight.infrastructure.db.models import ModelDescriptor, RuntimeProfile

    descriptor = session.get(ModelDescriptor, run.model_descriptor_id)
    profile = session.get(RuntimeProfile, run.runtime_profile_id)

    def measured(value: int | None) -> Measurement:
        return UNSUPPORTED if value is None else float(value)

    return KvArchitecture(
        layers=measured(descriptor.layers) if descriptor is not None else UNSUPPORTED,
        kv_heads=measured(descriptor.kv_heads) if descriptor is not None else UNSUPPORTED,
        head_dim=measured(descriptor.head_dim) if descriptor is not None else UNSUPPORTED,
        architecture=descriptor.architecture if descriptor is not None else None,
        kv_cache_precision=profile.kv_cache_precision if profile is not None else None,
    )


def _mean_prompt_eval_ms(samples: Sequence[_StoredSample]) -> Measurement:
    """Mean provider-reported prompt-evaluation time over the completed samples of one test."""
    reported = [
        sample.prompt_eval_ms
        for sample in samples
        if sample.status == "completed" and sample.prompt_eval_ms is not None
    ]
    return math.fsum(reported) / len(reported) if reported else UNSUPPORTED


def _memory_kv_metrics(
    database: Database,
    run: Any,  # noqa: ANN401 — a runs row
    by_test: Mapping[str, list[_StoredSample]],
    architecture: KvArchitecture,
    context: _RunContext,
) -> tuple[AggregatedMetric, ...]:
    """Derive ``native.memory_kv``'s run-level figures from the descriptor and the telemetry."""
    series = load_series(database, run.id)
    device = next((item for item in series.gpus if item.gpu_index == context.gpu_index), None)
    observations: list[ContextObservation] = []
    if device is not None:
        for sample in by_test.get("memory_kv.context_slope", []):
            if sample.status != "completed":
                continue
            tokens = sample.detail.get(CONTEXT_TOKENS_DETAIL_KEY)
            if isinstance(tokens, bool) or not isinstance(tokens, int):
                continue
            vram = stabilized_vram(
                SampleWindow(
                    context_tokens=tokens,
                    started_at=sample.started_at,
                    ended_at=sample.ended_at,
                    succeeded=True,
                ),
                series.timestamps,
                device.vram_used_bytes,
            )
            if is_supported(vram):
                observations.append(
                    ContextObservation(context_tokens=tokens, vram_used_bytes=float(vram))
                )
    attempts = [
        (int(sample.detail.get(CONTEXT_TOKENS_DETAIL_KEY, 0)), sample.status == "completed")
        for sample in by_test.get("memory_kv.max_context_fit", [])
        if isinstance(sample.detail.get(CONTEXT_TOKENS_DETAIL_KEY), int)
    ]
    return memory_kv_benchmark.derive(
        architecture=architecture,
        observations=observations,
        attempts=attempts,
        cold_prefill_ms=_mean_prompt_eval_ms(by_test.get("memory_kv.prefix_first_pass", [])),
        warm_prefill_ms=_mean_prompt_eval_ms(by_test.get("memory_kv.prefix_reuse", [])),
        gpu_index=context.gpu_index,
        multi_gpu_visible=context.multi_gpu_visible,
        placement_known=False,
        configured_limit=context.served_context,
    )


def _energy_metrics(
    database: Database,
    run: Any,  # noqa: ANN401 — a runs row
    by_test: Mapping[str, list[_StoredSample]],
    context: _RunContext,
) -> tuple[AggregatedMetric, ...]:
    """Derive ``native.energy``'s run-level figures from the run's persisted power series."""
    series = load_series(database, run.id)
    device = next((item for item in series.gpus if item.gpu_index == context.gpu_index), None)
    power = (
        [
            PowerSample(
                timestamp=stamp,
                power_watts=UNSUPPORTED if watts is None else float(watts),
            )
            for stamp, watts in zip(series.timestamps, device.power_watts, strict=True)
        ]
        if device is not None
        else []
    )
    with database.read() as session:
        temperatures = [
            row.cpu_temperature_c
            for row in TelemetryRepository().list_for_run(session, run.id)
            if row.cpu_temperature_c is not None
        ]
    samples = [sample for group in by_test.values() for sample in group]
    completed = [sample for sample in samples if sample.status == "completed"]
    tokens = [sample.output_tokens for sample in completed if sample.output_tokens is not None]
    window = load_window(database, run.id)
    verdict = window.suspected_throttling(context.gpu_index) if window.sample_count() else None
    # Only the samples that were actually sent carry a window. A skip has none, and giving it a
    # zero-length one would be indistinguishable from a request that took no time.
    request_windows = [
        (sample.started_at, sample.ended_at)
        for sample in samples
        if sample.status != "skipped" and sample.ended_at > sample.started_at
    ]
    return energy_benchmark.derive(
        power,
        requests=len(samples),
        successes=sum(1 for sample in completed if (sample.score or 0.0) > 0.0),
        output_tokens=float(sum(tokens)) if tokens else UNSUPPORTED,
        max_cpu_temperature_c=max(temperatures) if temperatures else UNSUPPORTED,
        throttling_suspected=verdict.suspected if verdict is not None else None,
        gpu_index=context.gpu_index,
        multi_gpu_visible=context.multi_gpu_visible,
        placement_known=False,
        request_windows=request_windows,
    )


def _reliability_metrics(
    by_test: Mapping[str, list[_StoredSample]],
) -> tuple[AggregatedMetric, ...]:
    """Derive ``native.reliability``'s run-level figures from every stored repetition."""
    attempts: dict[str, list[_StoredSample]] = {}
    for samples in by_test.values():
        for sample in samples:
            attempts.setdefault(sample.case_id, []).append(sample)
    cases = [
        CaseAttempts(
            case_id=case_id,
            scores=tuple(
                UNSUPPORTED if sample.score is None else float(sample.score) for sample in group
            ),
            answer_labels=tuple(sample.response_hash for sample in group),
        )
        for case_id, group in sorted(attempts.items())
    ]
    return reliability_benchmark.derive(cases)


def _suite_derived_metrics(
    database: Database,
    run_id: str,
    benchmark: Benchmark,
    run_test_ids: Mapping[str, str],
    context: _RunContext,
) -> tuple[AggregatedMetric, ...]:
    """Compute the run-level metrics the Phase 9 suites cannot derive from a sample alone.

    The seam is deliberately narrow and deliberately explicit: three named suites, one dispatch,
    and every formula behind it living in the benchmark package that owns it. Nothing here decides
    what a number means; it reads the descriptor, the telemetry and the stored repetitions, hands
    them over, and turns what comes back into rows.

    Args:
        database: The application's database handle.
        run_id: The run being aggregated.
        benchmark: The suite that ran.
        run_test_ids: ``{test_key: run_tests row id}`` for this run.
        context: The run's frozen inputs — the target device and whether more than one was visible.

    Returns:
        The derived rows, or empty for any suite that is not one of the three. Empty is the normal
        answer, not a failure: twelve of the fifteen shipped suites derive nothing here.
    """
    suite_key = benchmark.manifest.key
    if suite_key not in _SUITES_WITH_DERIVED_METRICS:
        return ()
    with database.read() as session:
        run = RunRepository().get_by_id(session, run_id)
        if run is None:  # pragma: no cover — the caller is holding this run
            return ()
        by_test = {
            test.key: _stored_samples(session, run_test_ids[test.key])
            for test in benchmark.tests
            if test.key in run_test_ids
        }
        architecture = _kv_architecture(session, run)
    if suite_key == "native.memory_kv":
        return _memory_kv_metrics(database, run, by_test, architecture, context)
    if suite_key == "native.energy":
        return _energy_metrics(database, run, by_test, context)
    return _reliability_metrics(by_test)


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
    allow_prompt_override: bool = False,
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
        allow_prompt_override: Passed through to :func:`create_run`. A repeat of a run that was
            allowed to use an override still has to say so: the flag is a statement about *this*
            run, and carrying it implicitly would let an override arrive by inheritance.
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
        # The *original's* runtime profile, not the current configuration's. A repeat that
        # re-resolved the profile would silently repeat a different measurement the moment
        # `[runtime]` changed — the same reason the frozen ExecutionConfig is reused verbatim
        # rather than re-resolved. ADR-0017 makes a differing profile a hard separation, so a
        # "repeat" under a new one would not be a repeat at all.
        original_profile = _stored_runtime_profile(session, original.runtime_profile_id)

    observed = _observed_document(
        database,
        provider,
        collector,
        registry,
        suite_key,
        model_ref,
        config,
        runtime_profile=original_profile,
        clock=clock,
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
        runtime_profile=original_profile,
        model_ref=model_ref,
        suite_key=suite_key,
        execution=config,
        label=label if label is not None else f"repeat of {original_id[:10]}",
        extra_degradations=degradations,
        allow_prompt_override=allow_prompt_override,
        clock=clock,
    )


def _stored_runtime_profile(session: Session, profile_id: str) -> RuntimeProfile:
    """Rebuild the :class:`~baseaicore.RuntimeProfile` a stored run was measured under.

    Read back from the row rather than re-resolved from configuration, so a repeat repeats the
    profile as well as the execution parameters. A row that has gone missing yields provider
    defaults, which is the same profile every run used before ``[runtime]`` existed.
    """
    from freeweight.infrastructure.db.models import RuntimeProfile as RuntimeProfileRow

    row = session.get(RuntimeProfileRow, profile_id)
    if row is None:  # pragma: no cover — a run cannot outlive its profile row (RESTRICT)
        return RuntimeProfile()
    options = row.provider_options_json if isinstance(row.provider_options_json, dict) else {}
    return RuntimeProfile(
        context_size=row.context_size,
        kv_cache_precision=row.kv_cache_precision,
        gpu_layers=row.gpu_layers,
        flash_attention=row.flash_attention,
        threads=row.threads,
        batch_size=row.batch_size,
        keep_alive=row.keep_alive,
        provider_options=dict(options),
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
    runtime_profile: RuntimeProfile,
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
    # The same override list ``create_run`` would compute, so that a repeat's observed document
    # and the original's recorded one describe the same measurement. The *refusal* stays in
    # ``create_run``: this function only describes what the environment would produce.
    overrides = _declared_overrides(benchmark)
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
                prompt_overrides=overrides,
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
