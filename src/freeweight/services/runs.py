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

Phase 5 scope: the reproducibility fingerprint here covers the inputs this phase has (see
:func:`compute_fingerprint`), and aggregation is the mean of sample scores (see
:func:`_aggregate_test`). Phase 6 replaces both — with the full fingerprint document of
[Machine Identity §6](../../../../docs/architecture/machine-identity-and-reproducibility.md) and
with ``domain/aggregation.py`` — and both are marked in place so neither can be mistaken for
finished work.
"""

from __future__ import annotations

import json
import logging
import random
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import (
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

from freeweight.__about__ import __version__
from freeweight.benchmarks.echo import benchmark as echo_benchmark
from freeweight.domain.benchmark import Benchmark, BenchmarkRegistry, BenchmarkTest
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

if TYPE_CHECKING:
    from collections.abc import Iterator

    from baseaicore.timeutil import Clock
    from modelrack.provider import Provider
    from sqlalchemy.orm import Session
    from sweatmeter import TelemetryCollector

    from freeweight.config import ExecutionSettings
    from freeweight.services.database import Database

__all__ = [
    "ExecutionConfig",
    "MetricSummary",
    "RunDetail",
    "RunNotFound",
    "RunSummary",
    "RunTestSummary",
    "SampleSummary",
    "build_registry",
    "cancel_run",
    "compute_fingerprint",
    "create_run",
    "execute_run",
    "get_run",
    "list_runs",
    "list_samples",
    "resume_run",
]

logger = logging.getLogger(__name__)


class RunNotFound(NotFoundError):
    """No run matches the given id or prefix (spec §13, ``RUN_NOT_FOUND``)."""

    code: ClassVar[str] = "RUN_NOT_FOUND"


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
    ) -> ExecutionConfig:
        """Resolve the application defaults against this run's overrides.

        Args:
            defaults: ``settings.execution``.
            measured_repetitions: Override, or ``None`` to take the default.
            seed: Override, or ``None`` to take the default.
            store_responses: Override, or ``None`` to take the default.
            temperature: Sampling temperature for this run, or ``None`` for the provider default.
            max_output_tokens: Output cap for this run, or ``None`` for the provider default.

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
        )


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One run as every surface shows it: the list page, the API, the CLI."""

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


@dataclass(frozen=True, slots=True)
class SampleSummary:
    """One raw sample, as the drill-down page and the API show it."""

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


@dataclass(frozen=True, slots=True)
class RunDetail:
    """A run with everything the detail page renders: its tests and its aggregate metrics."""

    run: RunSummary
    tests: tuple[RunTestSummary, ...]
    metrics: tuple[MetricSummary, ...]
    effective_config: ExecutionConfig
    last_event_sequence: int


def build_registry() -> BenchmarkRegistry:
    """Build the registry of benchmarks this build can run.

    The one list. A suite that is not named here cannot be run, which is the point: benchmark
    availability is a deliberate, reviewable fact rather than a consequence of which modules
    happened to be imported. Phase 6 adds ``native.performance`` and ``native.token_economy``
    here, Phase 7 the quality suites.
    """
    return BenchmarkRegistry([echo_benchmark.build()])


def compute_fingerprint(document: dict[str, Any]) -> str:
    """Return the ``sha256:``-prefixed fingerprint of a run's input document.

    Over :func:`~baseaicore.canonical_json`, so the same inputs hash identically in another
    process, on another platform and after a Python upgrade.

    **This is the Phase 5 fingerprint, and it is incomplete on purpose.** It covers the model
    identity and digest, the descriptor snapshot, the runtime profile hash, the suite key, version
    and manifest hash, the machine fingerprint, the provider kind and version, the application
    version and the resolved execution config — every input this phase actually has. Phase 6 adds
    the prompt subset hash, the served context and its source, and the GPU attribution
    (ADR-0027/ADR-0028), and assembles the document in ``domain/provenance.py`` instead of here.
    Runs fingerprinted by the two phases are therefore *not* comparable by fingerprint, which is
    correct: they were produced by measurements with different provenance.

    Args:
        document: The fingerprint document, already JSON-safe.

    Returns:
        ``"sha256:"`` followed by 64 lowercase hex characters.
    """
    return f"sha256:{sha256_of(canonical_json(document))}"


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
        provider_version = _provider_version(provider)
        document = {
            "model": {
                "canonical_id": model.canonical_id,
                "artifact_digest": model.artifact_digest,
                "identity_confidence": model.identity_confidence,
                "descriptor_hash": descriptor.descriptor_hash,
            },
            "runtime_profile_hash": runtime_profile.profile_hash,
            "suite": {
                "key": benchmark.manifest.key,
                "version": benchmark.manifest.version,
                "manifest_hash": benchmark.manifest.manifest_hash,
            },
            "machine_fingerprint": machine_profile.machine_fingerprint,
            "provider": {"kind": model.provider_kind, "version": provider_version},
            "application_version": __version__,
            "execution": execution.to_json(),
            "fingerprint_scope": "phase-5",
        }
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
            provider_version=provider_version,
            application_version=__version__,
            label=label,
            now=now,
        )
        summary = _summarize_run(session, run)
    logger.info("run.created", extra={"run_id": summary.id, "suite": suite_key, "model": model_ref})
    return summary


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


def execute_run(
    database: Database,
    provider: Provider,
    registry: BenchmarkRegistry,
    publisher: RunEventPublisher,
    run_id: str,
    *,
    clock: Clock = utc_now,
) -> RunStatus:
    """Execute one claimed run to a terminal state, and return that state.

    The run is already in ``preparing`` when this is called — claiming it is what moved it there
    (:meth:`~freeweight.infrastructure.db.repositories.runs.RunRepository.claim_next_queued`), so
    that no run can be claimed twice.

    Phases, each preceded by a cancellation check: **prepare** (load the suite, enumerate tests),
    **warm** (unscored generations that take model loading out of the measurement), **execute**
    (every case × repetition, one sample per generation), **aggregate** (read the stored samples
    back and write metric rows), **complete**.

    Never raises for a *measurement* failure. A failed sample and a failed test are recorded and
    execution continues. Only a failure of the machinery itself — the suite is not registered, the
    run's rows are inconsistent — moves the run to ``failed``, and even then the error is recorded
    rather than propagated, because the scheduler thread must survive it.

    Args:
        database: The application's database handle.
        provider: The provider to generate through.
        registry: The benchmarks this build can run.
        publisher: The event publisher.
        run_id: The claimed run.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The terminal status the run reached.
    """
    try:
        return _execute_run_inner(database, provider, registry, publisher, run_id, clock=clock)
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


def _execute_run_inner(
    database: Database,
    provider: Provider,
    registry: BenchmarkRegistry,
    publisher: RunEventPublisher,
    run_id: str,
    *,
    clock: Clock,
) -> RunStatus:
    """Drive one run through its phases. See :func:`execute_run` for the contract."""
    with database.read() as session:
        run = RunRepository().get_by_id(session, run_id)
        if run is None:
            raise RunNotFound(f"No run matches {run_id!r}.", details={"run": run_id})
        suite = session.get(_suite_model(), run.suite_id)
        if suite is None:  # pragma: no cover — a RESTRICT foreign key makes this unreachable
            raise DatabaseUnavailable(f"Run {run_id!r} points at a suite that is not installed.")
        suite_key = suite.key
        suite_id = suite.id
        config = ExecutionConfig.from_json(run.effective_config_json)
        model = ModelRepository().get_by_id(session, run.model_id)
        if model is None:  # pragma: no cover — a RESTRICT foreign key makes this unreachable
            raise DatabaseUnavailable(f"Run {run_id!r} points at a model that is not stored.")
        identity = ModelIdentity(
            provider_kind=ProviderKind(model.provider_kind),
            provider_model_name=model.provider_model_name,
            artifact_digest=model.artifact_digest,
        )

    benchmark = registry.get(suite_key)
    publisher.publish(
        run_id,
        "run.started",
        message=f"Run started: {suite_key} against {model.canonical_id}.",
        data={"suite": suite_key, "model": model.canonical_id},
    )

    # --- prepare -----------------------------------------------------------------------------
    _check_cancelled(database, run_id)
    run_test_ids = _prepare(database, run_id, benchmark, suite_id, config)

    # --- warm --------------------------------------------------------------------------------
    _check_cancelled(database, run_id)
    _transition(database, run_id, RunStatus.WARMING)
    _warm(provider, identity, benchmark, config)

    # --- execute -----------------------------------------------------------------------------
    _check_cancelled(database, run_id)
    _transition(database, run_id, RunStatus.RUNNING)
    # Read back from the ``run_tests`` rows `_prepare` just wrote, rather than re-enumerating
    # every test's cases. Re-enumerating here would put a benchmark's own code on the run's
    # critical path *outside* the per-test error handling, so one test that cannot list its cases
    # would fail the whole run — the exact containment rule spec §13 states ("a failed test never
    # fails the run"). The persisted counts are also the ones a resumed run has to agree with.
    with database.read() as session:
        total_samples = sum(
            row.total_cases * row.repetitions
            for row in RunTestRepository().list_for_run(session, run_id)
        )
    completed_samples = 0
    for test in benchmark.tests:
        _check_cancelled(database, run_id)
        completed_samples = _execute_test(
            database,
            provider,
            publisher,
            run_id=run_id,
            run_test_id=run_test_ids[test.key],
            identity=identity,
            test=test,
            config=config,
            completed_samples=completed_samples,
            total_samples=total_samples,
            clock=clock,
        )

    # --- aggregate ---------------------------------------------------------------------------
    _check_cancelled(database, run_id)
    _aggregate_run(database, run_id, benchmark, run_test_ids, clock=clock)

    # --- complete ----------------------------------------------------------------------------
    _finish(database, publisher, run_id, RunStatus.COMPLETED, clock=clock)
    return RunStatus.COMPLETED


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
                total_cases=len(tuple(test.cases())),
                repetitions=config.measured_repetitions,
            )
            run_test_ids[test.key] = row.id
        return run_test_ids


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
    the cold/warm mixing Phase 6's aggregation rules forbid.

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
            provider.generate(_build_request(identity, first_case.prompt, config))
        except ProviderError as exc:
            logger.warning("run.warmup_failed", extra={"code": exc.code})
            return


def _build_request(
    identity: ModelIdentity, prompt: str, config: ExecutionConfig
) -> GenerationRequest:
    """Build one provider request from a prompt and the run's frozen execution config."""
    return GenerationRequest(
        identity=identity,
        messages=(Message(role=Role.USER, content=prompt),),
        sampling=SamplingParameters(
            temperature=config.temperature,
            top_p=config.top_p,
            seed=config.seed,
            max_output_tokens=config.max_output_tokens,
        ),
        timeout_seconds=config.test_timeout_seconds,
    )


def _execute_test(
    database: Database,
    provider: Provider,
    publisher: RunEventPublisher,
    *,
    run_id: str,
    run_test_id: str,
    identity: ModelIdentity,
    test: BenchmarkTest,
    config: ExecutionConfig,
    completed_samples: int,
    total_samples: int,
    clock: Clock,
) -> int:
    """Execute one test: every case, every repetition, one sample per generation.

    A test already in a terminal state is skipped entirely and its samples counted towards
    progress — that is resume, and it is why "completed tests are retained".

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
                session, run_test_id, status=TestStatus.RUNNING.value, started_at=clock()
            )
        already = SampleRepository().existing_keys(session, run_test_id)

    publisher.publish(
        run_id,
        "test.started",
        message=f"Test {test.key} started.",
        data={"test": test.key, "run_test_id": run_test_id},
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
            for repetition in range(1, config.measured_repetitions + 1):
                if (case.case_id, case.ordinal, repetition) in already:
                    completed_samples += 1
                    continue
                _check_cancelled(database, run_id)
                sample = _run_one_case(
                    database,
                    provider,
                    run_test_id=run_test_id,
                    identity=identity,
                    test=test,
                    case=case,
                    repetition=repetition,
                    config=config,
                    clock=clock,
                )
                completed_samples += 1
                publisher.publish(
                    run_id,
                    "sample.completed" if sample["status"] == "completed" else "sample.failed",
                    message=(f"{test.key} {case.case_id} rep {repetition}: {sample['status']}"),
                    progress=(completed_samples, total_samples),
                    data={
                        "run_test_id": run_test_id,
                        "case_id": case.case_id,
                        "repetition": repetition,
                        "status": sample["status"],
                        "score": sample["score"],
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


def _run_one_case(
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

    A provider error becomes a ``failed`` sample carrying the provider's own stable error code and
    ``score = NULL`` — never a zero, and never a failed test (spec §13). A scorer that raises is
    treated identically: the sample is stored ``failed`` with ``SCORER_ERROR``, because a defect
    in one scorer must not discard a run's other measurements.

    Returns:
        The stored column values, for the caller's event payload.
    """
    started_ns = monotonic_ns()
    result = None
    error_code: str | None = None
    error_text: str | None = None
    try:
        result = provider.generate(_build_request(identity, case.prompt, config))
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
            )
    with database.write() as session:
        SampleRepository().insert(session, **values)
    return values


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


def _sample_values(
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
) -> dict[str, Any]:
    """Assemble one ``samples`` row.

    The single place the column set is written down (see
    :meth:`~freeweight.infrastructure.db.repositories.runs.SampleRepository.insert` for why the
    repository does not restate it). ``score`` is ``None`` for every non-``completed`` status,
    which the table's own check constraint also enforces.
    """
    text = result.text if result is not None else ""
    values: dict[str, Any] = {
        "run_test_id": run_test_id,
        "case_id": case.case_id,
        "ordinal": case.ordinal,
        "repetition": repetition,
        "status": status,
        "prompt_hash": f"sha256:{sha256_of(case.prompt)}",
        "rendered_prompt_hash": f"sha256:{sha256_of(case.prompt)}",
        "response_hash": f"sha256:{sha256_of(text)}" if result is not None else None,
        "response_text": text if (result is not None and config.store_responses) else None,
        "output_chars": len(text) if result is not None else None,
        "output_words": len(text.split()) if result is not None else None,
        "output_bytes": len(text.encode("utf-8")) if result is not None else None,
        "client_wall_ms": wall_ms,
        "score": score.score if (score is not None and status == "completed") else None,
        "score_method": score.method.value if score is not None else None,
        "result_json": _json_safe(dict(score.detail)) if score is not None else None,
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


def _aggregate_run(
    database: Database,
    run_id: str,
    benchmark: Benchmark,
    run_test_ids: dict[str, str],
    *,
    clock: Clock,
) -> None:
    """Read this run's stored samples back and write its aggregate metric rows.

    **Reads the database, never an in-memory accumulator.** That is the whole defence against the
    phase's named failure mode, "partial aggregates written before samples are durable": this
    function has no other source of data, so it cannot describe a sample that has not committed.

    Idempotent: it deletes this run's aggregate rows and writes the current ones, so aggregating a
    resumed run twice leaves one correct set rather than two partial ones.

    **Phase 5's aggregation rule is the mean of sample scores**, written into every metric the
    test declares, with ``sample_count`` and ``excluded_count`` beside it. That is exactly right
    for a suite whose metrics are all score-derived (``native.echo``'s one is) and it is not a
    general aggregation engine — Phase 6's ``domain/aggregation.py`` is, and it replaces this.
    A test with no usable scores writes a row with ``numeric_value = NULL`` and
    ``unavailable_reason``, never ``0`` (ADR-0016).
    """
    now = clock()
    rows: list[dict[str, Any]] = []
    with database.read() as session:
        per_test = {
            test.key: SampleRepository().scores_for_run_test(session, run_test_ids[test.key])
            for test in benchmark.tests
        }
    for test in benchmark.tests:
        scores, excluded = per_test[test.key]
        for metric in test.metrics:
            value = sum(scores) / len(scores) if scores else None
            rows.append(
                {
                    "run_id": run_id,
                    "run_test_id": run_test_ids[test.key],
                    "sample_id": None,
                    "metric_key": metric.key,
                    "numeric_value": value,
                    "unavailable_reason": None if value is not None else "no_scored_samples",
                    "unit": metric.unit,
                    "aggregation": metric.aggregation,
                    "higher_is_better": metric.higher_is_better,
                    "sample_count": len(scores),
                    "excluded_count": excluded,
                    "created_at": now,
                }
            )
    rows.extend(_run_level_rows(benchmark, per_test, run_id=run_id, now=now))
    with database.write() as session:
        MetricValueRepository().replace_for_run(session, run_id, rows=rows)


def _run_level_rows(
    benchmark: Benchmark,
    per_test: dict[str, tuple[list[float], int]],
    *,
    run_id: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Build the run-level rows: one per distinct metric key, over every test that declares it."""
    by_key: dict[str, tuple[Any, list[float], int]] = {}
    for test in benchmark.tests:
        scores, excluded = per_test[test.key]
        for metric in test.metrics:
            existing = by_key.get(metric.key)
            if existing is None:
                by_key[metric.key] = (metric, list(scores), excluded)
            else:
                _, all_scores, all_excluded = existing
                by_key[metric.key] = (metric, all_scores + scores, all_excluded + excluded)
    rows: list[dict[str, Any]] = []
    for metric, scores, excluded in by_key.values():
        value = sum(scores) / len(scores) if scores else None
        rows.append(
            {
                "run_id": run_id,
                "run_test_id": None,
                "sample_id": None,
                "metric_key": metric.key,
                "numeric_value": value,
                "unavailable_reason": None if value is not None else "no_scored_samples",
                "unit": metric.unit,
                "aggregation": metric.aggregation,
                "higher_is_better": metric.higher_is_better,
                "sample_count": len(scores),
                "excluded_count": excluded,
                "created_at": now,
            }
        )
    return rows


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
