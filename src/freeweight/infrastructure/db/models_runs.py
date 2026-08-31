"""freeweight.infrastructure.db.models_runs — Phase 5 tables: the run engine's own schema.

Ten tables, per [Data Model §2](../../../../../../docs/apps/freeweight/data-model.md):
``benchmark_suites``, ``benchmark_tests``, ``runs``, ``run_tests``, ``samples``, ``metric_values``,
``run_events`` and ``artifacts`` from Phase 5, and ``telemetry_samples`` /
``telemetry_gpu_samples`` from Phase 6. Split from :mod:`freeweight.infrastructure.db.models` (which
holds Phase 2's identity tables) purely for file size; both modules share one
:class:`~freeweight.infrastructure.db.base.Base`, and both are imported by
:mod:`freeweight.services.database` so Alembic's autogenerate parity check sees every table.

Three rules from the data model are enforced here rather than left to application code:

* **Results never delete identity.** ``runs``' foreign keys to ``machines``, ``models``,
  ``model_descriptors``, ``runtime_profiles`` and ``benchmark_suites`` are ``ON DELETE RESTRICT``;
  everything *below* a run (``run_tests`` → ``samples`` → ``metric_values``, ``run_events``,
  ``artifacts``) is ``ON DELETE CASCADE``. Deleting results is a supported operation; deleting the
  model it measured, as a side effect, is not (data model §4).
* **A gap-free event sequence.** ``UNIQUE (run_id, sequence)`` is what makes "no gap, no
  duplicate" a property the database holds rather than one the writer promises.
* **A failed sample's score is ``NULL``, never ``0``**
  ([ADR-0016](../../../../../../docs/adr/0016-unavailable-is-not-zero.md)). The check constraint
  ``ck_samples_score_null_unless_completed`` states it in DDL: a non-``completed`` sample cannot
  carry a score at all, so no future code path can quietly write a zero into one.

ORM models only. They never leave the repository layer (coding standards §4).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from weightsdb import PortableJSON, UtcDateTime, ulid_primary_key

from freeweight.infrastructure.db.base import Base, utcnow

__all__ = [
    "Artifact",
    "BenchmarkSuite",
    "BenchmarkTestRow",
    "MetricValue",
    "Run",
    "RunEvent",
    "RunTest",
    "Sample",
    "TelemetryGpuSample",
    "TelemetrySample",
    "ToolCall",
]

_RUN_STATUSES = (
    "queued",
    "preparing",
    "warming",
    "running",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
)
_TEST_STATUSES = ("pending", "running", "completed", "failed", "skipped", "cancelled")
_SAMPLE_STATUSES = (
    "completed",
    "awaiting_judgement",
    "failed",
    "timeout",
    "cancelled",
    "skipped",
)
"""The states one sample can be in.

``awaiting_judgement`` is the only non-terminal one: the model answered, the deterministic criteria
scored, and the jury has not run yet. It exists because a goal run judges in a **second phase**, so
that the candidate and the jurors are never resident at the same time — and a sample between the
two phases is genuinely in a state of its own. Recording it as ``completed`` with a partial
composite would publish a score whose missing judged weight looked like a measurement; recording it
as ``failed`` would say the generation went wrong when it did not."""

_TOOL_CALL_STATUSES = ("ok", "error", "unknown_tool", "invalid_arguments")
"""What became of one requested call.

``unknown_tool`` and ``invalid_arguments`` are distinct from ``error`` because the harness did not
run the tool in either case, and "the tool failed" and "the harness refused to run it" are different
facts about the model.
"""
_RUNNERS = ("native", "external", "goal")
_ARTIFACT_KINDS = ("raw_response", "generated_code", "external_output", "export", "log")


def _in_list(column: str, allowed: tuple[str, ...]) -> str:
    """Render ``column IN ('a', 'b')`` for a ``CheckConstraint``.

    The same helper, and the same reasoning, as
    :func:`freeweight.infrastructure.db.models._in_list`: a tuple's ``repr`` is valid SQL only by
    coincidence and stops being valid for a one-element tuple. Duplicated rather than imported
    because ``models.py`` and this module are peers with no dependency between them, and a
    four-line private formatter is not worth a third module to share it.
    """
    if any("'" in value for value in allowed):
        raise ValueError(f"CheckConstraint values must not contain a quote: {allowed!r}")
    rendered = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({rendered})"


class BenchmarkSuite(Base):
    """One version of one benchmark suite, as installed in this database.

    Keyed by ``(key, version)``, not by ``key`` alone: a suite version change **separates**
    results rather than invalidating them (spec §19), which only works if both versions can be
    present at once and a run points at the exact one it executed.

    ``goal_id``, ``goal_hash``, ``prompt_subset_hash`` and ``prompt_refs_json`` are declared here
    and left ``NULL`` by Phase 5 — the prompt library arrives at Phase 6 (ADR-0028) and goal suites
    at Phase 8A (ADR-0031). ``goal_id`` deliberately carries no foreign key yet: ``goals`` does not
    exist as a table until Phase 8A, and a constraint cannot reference a table that has not been
    created. It becomes a real ``FK → goals`` in that phase's migration.
    """

    __tablename__ = "benchmark_suites"
    __table_args__ = (
        CheckConstraint(_in_list("runner", _RUNNERS), name="runner"),
        UniqueConstraint("key", "version", name="uq_benchmark_suites_key_version"),
        Index("ix_benchmark_suites_key", "key"),
    )

    id: Mapped[str] = ulid_primary_key()
    key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str | None] = mapped_column(String)
    runner: Mapped[str] = mapped_column(String, nullable=False)
    goal_id: Mapped[str | None] = mapped_column(String(26))
    goal_hash: Mapped[str | None] = mapped_column(String)
    manifest_hash: Mapped[str] = mapped_column(String, nullable=False)
    manifest_json: Mapped[object | None] = mapped_column(PortableJSON)
    dataset_hashes_json: Mapped[object | None] = mapped_column(PortableJSON)
    prompt_subset_hash: Mapped[str | None] = mapped_column(String)
    prompt_refs_json: Mapped[object | None] = mapped_column(PortableJSON)
    source_json: Mapped[object | None] = mapped_column(PortableJSON)
    license: Mapped[str | None] = mapped_column(String)
    installed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class BenchmarkTestRow(Base):
    """One test declared by one suite version.

    Named ``BenchmarkTestRow`` rather than ``BenchmarkTest`` so it cannot be confused with
    :class:`freeweight.domain.benchmark.BenchmarkTest`, the domain protocol. They are different
    things on purpose: the domain object knows how to produce cases, this row only records that
    the test existed in the suite version a run executed.

    ``ON DELETE CASCADE`` from the suite, but ``run_tests`` references *this* row with ``ON DELETE
    RESTRICT`` — so a suite with runs against it cannot be deleted, which is the intended
    protection rather than an oversight.
    """

    __tablename__ = "benchmark_tests"
    __table_args__ = (UniqueConstraint("suite_id", "key", name="uq_benchmark_tests_suite_id_key"),)

    id: Mapped[str] = ulid_primary_key()
    suite_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("benchmark_suites.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str | None] = mapped_column(String)
    scorer: Mapped[str] = mapped_column(String, nullable=False)
    config_json: Mapped[object | None] = mapped_column(PortableJSON)
    metric_definitions_json: Mapped[object | None] = mapped_column(PortableJSON)
    requires_json: Mapped[object | None] = mapped_column(PortableJSON)


class Run(Base):
    """One execution of one suite against one model on one machine.

    The five identity foreign keys are ``NOT NULL`` and ``ON DELETE RESTRICT``: a run that cannot
    say which model, which descriptor snapshot, which serving parameters, which machine and which
    suite version produced it is not a measurement, and a result deletion must never take those
    rows with it (data model §4).

    Several columns are declared here and filled by later phases, because they belong to this
    table in the data model and adding them later would be a migration on a table with rows in it:
    ``served_context``/``served_context_source``, ``gpu_index``, ``multi_gpu_visible``,
    ``sandbox_tier``, ``telemetry_overhead_percent`` and ``prompt_pack_*`` are Phase 6's
    (provenance, telemetry, ADR-0027); ``sandbox_tier`` is Phase 9's. Phase 5 writes a
    ``reproducibility_fingerprint`` over the inputs it actually has — see
    :func:`freeweight.services.runs.compute_fingerprint`, which documents exactly which inputs
    those are and which Phase 6 adds.
    """

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in_list("status", _RUN_STATUSES), name="status"),
        Index("ix_runs_status_created_at", "status", "created_at"),
        Index("ix_runs_model_id_created_at", "model_id", "created_at"),
        Index("ix_runs_machine_id_created_at", "machine_id", "created_at"),
        Index("ix_runs_suite_id_created_at", "suite_id", "created_at"),
        Index("ix_runs_reproducibility_fingerprint", "reproducibility_fingerprint"),
    )

    id: Mapped[str] = ulid_primary_key()
    machine_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False
    )
    model_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
    )
    model_descriptor_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("model_descriptors.id", ondelete="RESTRICT"), nullable=False
    )
    runtime_profile_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("runtime_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    suite_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("benchmark_suites.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    effective_config_json: Mapped[object | None] = mapped_column(PortableJSON)
    reproducibility_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint_document_json: Mapped[object | None] = mapped_column(PortableJSON)
    provider_kind: Mapped[str | None] = mapped_column(String)
    provider_version: Mapped[str | None] = mapped_column(String)
    application_version: Mapped[str | None] = mapped_column(String)
    git_commit: Mapped[str | None] = mapped_column(String)
    prompt_pack_id: Mapped[str | None] = mapped_column(String)
    prompt_pack_version: Mapped[str | None] = mapped_column(String)
    prompt_pack_hash: Mapped[str | None] = mapped_column(String)
    served_context: Mapped[int | None] = mapped_column(Integer)
    served_context_source: Mapped[str | None] = mapped_column(String)
    gpu_index: Mapped[int | None] = mapped_column(Integer)
    multi_gpu_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sandbox_tier: Mapped[str | None] = mapped_column(String)
    telemetry_overhead_percent: Mapped[float | None] = mapped_column(Float)
    degradations_json: Mapped[object | None] = mapped_column(PortableJSON)
    error_code: Mapped[str | None] = mapped_column(String)
    error_text: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text)


class RunTest(Base):
    """One test of one run: its status, its progress and why it was skipped if it was.

    ``skip_reason`` is not decoration. A test that reports ``skipped`` with no reason is
    indistinguishable from one nobody got round to running, and spec §13 requires the reason
    (``unsupported_capability``, ``sandbox_unavailable``, ``dataset_missing``,
    ``insufficient_vram``, ``user_excluded``) to be recorded.
    """

    __tablename__ = "run_tests"
    __table_args__ = (
        CheckConstraint(_in_list("status", _TEST_STATUSES), name="status"),
        UniqueConstraint("run_id", "test_id", name="uq_run_tests_run_id_test_id"),
        Index("ix_run_tests_run_id_status", "run_id", "status"),
    )

    id: Mapped[str] = ulid_primary_key()
    run_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    test_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("benchmark_tests.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String)
    completed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    measurement_class: Mapped[str] = mapped_column(String, nullable=False, default="n/a")
    error_code: Mapped[str | None] = mapped_column(String)
    error_text: Mapped[str | None] = mapped_column(Text)


class Sample(Base):
    """The raw record: one case, one repetition, one response. Every headline number drills here.

    ``UNIQUE (run_test_id, case_id, ordinal, repetition)`` is load-bearing twice over. It is the
    natural key a person compares between two runs, and it is what makes **resume** safe: a run
    that died mid-test is continued by skipping the (case, ordinal, repetition) tuples already
    present, and the constraint is what turns a bug in that logic into an error rather than a
    duplicated measurement.

    ``response_text`` is ``NULL`` unless the run explicitly asked for content storage (spec §14:
    "prompts and responses are stored as hashes by default"). ``response_hash`` is always written,
    so two runs can be compared for identical output without either storing the output.
    """

    __tablename__ = "samples"
    __table_args__ = (
        CheckConstraint(_in_list("status", _SAMPLE_STATUSES), name="status"),
        CheckConstraint(
            "status = 'completed' OR score IS NULL", name="score_null_unless_completed"
        ),
        CheckConstraint(
            "started_at IS NULL OR started_at <= created_at", name="started_at_before_created_at"
        ),
        UniqueConstraint(
            "run_test_id",
            "case_id",
            "ordinal",
            "repetition",
            name="uq_samples_run_test_id_case_id_ordinal_repetition",
        ),
        Index("ix_samples_run_test_id_ordinal", "run_test_id", "ordinal"),
        Index("ix_samples_run_test_id_status", "run_test_id", "status"),
        Index("ix_samples_created_at", "created_at"),
    )

    id: Mapped[str] = ulid_primary_key()
    run_test_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("run_tests.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    prompt_hash: Mapped[str | None] = mapped_column(String)
    rendered_prompt_hash: Mapped[str | None] = mapped_column(String)
    prompt_id: Mapped[str | None] = mapped_column(String)
    prompt_version: Mapped[str | None] = mapped_column(String)
    response_hash: Mapped[str | None] = mapped_column(String)
    response_text: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer)
    tool_tokens: Mapped[int | None] = mapped_column(Integer)
    output_chars: Mapped[int | None] = mapped_column(Integer)
    output_words: Mapped[int | None] = mapped_column(Integer)
    output_bytes: Mapped[int | None] = mapped_column(Integer)
    client_wall_ms: Mapped[float | None] = mapped_column(Float)
    client_ttft_ms: Mapped[float | None] = mapped_column(Float)
    backend_load_ms: Mapped[float | None] = mapped_column(Float)
    backend_prompt_eval_ms: Mapped[float | None] = mapped_column(Float)
    backend_decode_ms: Mapped[float | None] = mapped_column(Float)
    backend_total_ms: Mapped[float | None] = mapped_column(Float)
    finish_reason: Mapped[str | None] = mapped_column(String)
    score: Mapped[float | None] = mapped_column(Float)
    score_method: Mapped[str | None] = mapped_column(String)
    judge_model_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="RESTRICT")
    )
    result_json: Mapped[object | None] = mapped_column(PortableJSON)
    error_code: Mapped[str | None] = mapped_column(String)
    error_text: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class ToolCall(Base):
    """One tool invocation a model requested, and what the harness did with it.

    Data model §2's ``tool_calls``. It exists so a tool metric drills to *the call that went wrong*
    rather than to a rate: a ``tool_selection_accuracy`` of 0.6 tells a reader nothing until they
    can see which two of five calls named the wrong tool, and with what arguments.

    **A row is what the model asked for, not what exists.** A call naming a tool that was never
    offered — the catalog's *hallucinated tool* — is a row with ``status = "unknown_tool"``, never a
    missing one: the whole point of the metric is to count them.

    ``correct_tool`` and ``correct_arguments`` are ``NULL`` when the case declares no expectation to
    compare this call against, which is not the same as ``False`` (ADR-0016). ``result_hash`` and
    never the result text: a tool result is content, and content is stored as a hash by default
    (spec §14).
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(_in_list("status", _TOOL_CALL_STATUSES), name="status"),
        Index(
            "ix_tool_calls_sample_id_turn_index_call_index",
            "sample_id",
            "turn_index",
            "call_index",
        ),
    )

    id: Mapped[str] = ulid_primary_key()
    sample_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("samples.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    arguments_json: Mapped[object | None] = mapped_column(PortableJSON)
    schema_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expected_tool: Mapped[str | None] = mapped_column(String)
    correct_tool: Mapped[bool | None] = mapped_column(Boolean)
    correct_arguments: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String, nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    result_hash: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class MetricValue(Base):
    """One metric at one of three levels: run, run_test or sample.

    ``unavailable_reason`` non-``NULL`` **is** the ``"unsupported"`` representation in storage
    (ADR-0016): the row exists, ``numeric_value`` is ``NULL``, and the reason says why. A metric
    this machine cannot produce is never absent and never zero.

    ``excluded_count`` is what keeps a failed sample visible: it is the number of samples that
    contributed nothing to ``numeric_value``, alongside the ``sample_count`` that did (spec §13).
    """

    __tablename__ = "metric_values"
    __table_args__ = (
        Index("ix_metric_values_run_id_metric_key", "run_id", "metric_key"),
        Index("ix_metric_values_run_test_id_metric_key", "run_test_id", "metric_key"),
        Index("ix_metric_values_metric_key_numeric_value", "metric_key", "numeric_value"),
    )

    id: Mapped[str] = ulid_primary_key()
    run_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    run_test_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("run_tests.id", ondelete="CASCADE")
    )
    sample_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("samples.id", ondelete="CASCADE")
    )
    metric_key: Mapped[str] = mapped_column(String, nullable=False)
    numeric_value: Mapped[float | None] = mapped_column(Float)
    text_value: Mapped[str | None] = mapped_column(String)
    unavailable_reason: Mapped[str | None] = mapped_column(String)
    gpu_index: Mapped[int | None] = mapped_column(Integer)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    aggregation: Mapped[str] = mapped_column(String, nullable=False)
    higher_is_better: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sample_count: Mapped[int | None] = mapped_column(Integer)
    excluded_count: Mapped[int | None] = mapped_column(Integer)
    stddev: Mapped[float | None] = mapped_column(Float)
    coefficient_of_variation: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class RunEvent(Base):
    """One persisted domain event for one run — the source of truth for SSE replay.

    ``UNIQUE (run_id, sequence)`` with sequences starting at 1 and no gaps. The uniqueness
    constraint is the mechanism, not a safety net: two threads computing ``max(sequence) + 1``
    concurrently produce the same number, and this constraint turns that into a rolled-back
    transaction the writer retries rather than into a stream a client cannot reassemble.

    ``progress_completed``/``progress_total`` are the API's ``progress`` object (API standards §8)
    and are ``NULL`` on events that carry no progress, rather than ``0/0`` — which a progress bar
    would render as "0 %".
    """

    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_id_sequence"),
        Index("ix_run_events_run_id_sequence", "run_id", "sequence"),
    )

    id: Mapped[str] = ulid_primary_key()
    run_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    progress_completed: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    data_json: Mapped[object | None] = mapped_column(PortableJSON)


class Artifact(Base):
    """One file produced by a run, recorded with its hash so the row and the file can be checked.

    Files live under the artifact directory with mode ``0600`` and are removed with their rows
    (data model §4). Phase 5 creates the table and writes no rows into it: raw-response artifacts
    arrive with content storage in Phase 6 and generated code in Phase 9. The table exists now
    because ``runs``' cascade has to include it from the first run that can be deleted.
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(_in_list("kind", _ARTIFACT_KINDS), name="kind"),
        Index("ix_artifacts_run_id", "run_id"),
    )

    id: Mapped[str] = ulid_primary_key()
    run_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    run_test_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("run_tests.id", ondelete="CASCADE")
    )
    sample_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("samples.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_type: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class TelemetrySample(Base):
    """One host telemetry observation taken during a run — **one row per sample**.

    Persisted only while a run is executing (``telemetry.persist_during_runs``): SweatMeter owns no
    storage (spec §10), and telemetry outside a run belongs to nothing that could ever be read back
    against a measurement.

    The host fields live here and the per-device fields live in :class:`TelemetryGpuSample`
    because a single table repeated every host field across a machine's GPUs, so any host aggregate
    double-counted on a two-GPU machine — silently, and only on hardware the reference machine does
    not have ([ADR-0027 §4](../../../../../../docs/adr/0027-multi-gpu-semantics.md)). The split also
    removes the "one row per sample with ``gpu_index NULL``" special case: a machine with no GPU
    produces host rows and no GPU rows, which is ordinary rather than special.
    """

    __tablename__ = "telemetry_samples"
    __table_args__ = (Index("ix_telemetry_samples_run_id_timestamp", "run_id", "timestamp"),)

    id: Mapped[str] = ulid_primary_key()
    run_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    cpu_percent: Mapped[float | None] = mapped_column(Float)
    load_average_1m: Mapped[float | None] = mapped_column(Float)
    ram_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    ram_available_bytes: Mapped[int | None] = mapped_column(BigInteger)
    ram_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    cpu_temperature_c: Mapped[float | None] = mapped_column(Float)
    disk_read_bytes_per_sec: Mapped[float | None] = mapped_column(Float)
    disk_write_bytes_per_sec: Mapped[float | None] = mapped_column(Float)
    process_rss_bytes: Mapped[int | None] = mapped_column(BigInteger)


class TelemetryGpuSample(Base):
    """One device's telemetry within one host sample. Zero or more rows per host row.

    ``UNIQUE (telemetry_sample_id, gpu_index)`` states in DDL that a device appears at most once
    per observation — the constraint a "sum across GPUs" bug would have to violate before it could
    double a VRAM figure.

    Written for **every** visible GPU regardless of which device a run is attributed to
    (ADR-0027 §3), so where the provider actually placed the model can be seen after the fact even
    on a run whose memory metrics were recorded as ``multi_gpu_placement_unknown``.
    """

    __tablename__ = "telemetry_gpu_samples"
    __table_args__ = (
        UniqueConstraint(
            "telemetry_sample_id",
            "gpu_index",
            name="uq_telemetry_gpu_samples_sample_id_gpu_index",
        ),
        Index("ix_telemetry_gpu_samples_run_id_gpu_index", "run_id", "gpu_index"),
        Index("ix_telemetry_gpu_samples_telemetry_sample_id", "telemetry_sample_id"),
    )

    id: Mapped[str] = ulid_primary_key()
    telemetry_sample_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("telemetry_samples.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized from the parent purely so the run detail page's chart query reads one table per
    # series instead of joining every GPU row back to its host row to find the run it belongs to
    # (data model §2). It cascades from ``runs`` as well, so a deleted run takes both rows.
    run_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_uuid: Mapped[str | None] = mapped_column(String)
    gpu_utilization_percent: Mapped[float | None] = mapped_column(Float)
    gpu_memory_utilization_percent: Mapped[float | None] = mapped_column(Float)
    vram_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    vram_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    gpu_temperature_c: Mapped[float | None] = mapped_column(Float)
    gpu_memory_temperature_c: Mapped[float | None] = mapped_column(Float)
    gpu_power_watts: Mapped[float | None] = mapped_column(Float)
    gpu_power_limit_watts: Mapped[float | None] = mapped_column(Float)
    gpu_fan_percent: Mapped[float | None] = mapped_column(Float)
    gpu_core_clock_mhz: Mapped[float | None] = mapped_column(Float)
    gpu_memory_clock_mhz: Mapped[float | None] = mapped_column(Float)
    throttle_reasons_json: Mapped[object | None] = mapped_column(PortableJSON)
    throttle_reasons_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
