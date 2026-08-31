"""freeweight.services.evidence — capability evidence: aggregation, storage, query and export.

The FreeWeight → LoadCoach value proposition, as one service. Every completed run of a measurement
subject — a model under a runtime profile on a machine (ADR-0023) — is folded into one
``capability.evidence`` record per capability, with ADR-0017's confidence beside the score, and the
records leave this process only through SetSpec's own models: ``GET /api/v1/evidence`` returns a
collection of ``capability.evidence`` envelopes, ``GET /api/v1/evidence/export`` one
``benchmark.evidence_bundle``, and ``freeweight evidence show|export`` are the same two functions
with a different front end.

Five decisions shape everything below.

**Evidence is recomputed, never edited.** :func:`recompute_evidence` reads the stored runs back and
replaces a subject's rows under the current policy version in one transaction. It runs when a run
completes and on request; it never runs on a read. A capability that lost its evidence disappears,
and one that never had any is **absent** — never scored zero (ADR-0017).

**Freshness decays from ``measured_at``**, the latest ``completed_at`` among the contributing runs,
and recomputing over unchanged runs changes ``computed_at`` and nothing else. A test asserts that
re-aggregation does not raise confidence, which is what makes ADR-0017 mean what it says
(ADR-0022 §2).

**Hard separations partition; they never discount.** Within one subject, runs of one suite are
partitioned by ``(suite version, dataset hashes, prompt subset hash)`` and only the newest
partition contributes; the others are named in the report as separated. A goal's ``goal_hash`` and
its jury are part of the suite version, so they separate structurally (ADR-0032 §4).

**A goal below its calibration gate emits nothing**, and the report says so rather than skipping
silently — for ``user.<slug>`` **and** for the capability it ``contributes_to``, because the second
half is the one that gets forgotten (ADR-0032 §3). A goal above it is emitted twice: once as
``user.<slug>`` keeping the goal's identity, and once as one weighted source among the shipped ones
inside the capability it contributes to — never *only* as the shipped one.

**The policy travels with the number.** Every record carries the confidence parameters it was
computed under, the capability mapping's version, and the six-factor breakdown, so the UI can
explain any score without recomputing it and a consumer can tell two policies apart.
"""

from __future__ import annotations

import base64
import json
import logging
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import (
    UNSUPPORTED,
    MetricKind,
    NotFoundError,
    ProviderKind,
    SuiteError,
    ValidationError,
    canonical_json,
    new_id,
    to_rfc3339,
    utc_now,
)
from setspec.envelope import GeneratorInfo, SchemaVersion, dump_envelope
from setspec.vocabulary import CAPABILITY_VOCABULARY_VERSION
from weightsdb import DatabaseUnavailable

from freeweight.__about__ import __version__
from freeweight.config import EvidenceSettings
from freeweight.domain.capability_mapping import (
    CapabilityMapping,
    MappingInvalid,
    normalize_value,
    parse_mapping,
    weighted_score,
)
from freeweight.domain.comparison import metric_kind_for
from freeweight.domain.confidence import (
    ConfidencePolicy,
    Environment,
    compute_confidence,
    freshness_factor,
    is_stale,
)
from freeweight.infrastructure.db.repositories.evidence import EvidenceRepository
from freeweight.services.export import model_identity_payload

if TYPE_CHECKING:
    from baseaicore import Clock
    from sqlalchemy.orm import Session

    from freeweight.services.database import Database

__all__ = [
    "BUNDLE_SCHEMA",
    "BUNDLE_SCHEMA_VERSION",
    "DEFAULT_EVIDENCE_LIMIT",
    "EVIDENCE_SCHEMA",
    "EVIDENCE_SCHEMA_VERSION",
    "MAX_EVIDENCE_LIMIT",
    "AggregationReport",
    "ContributingMetric",
    "EvidenceNotFound",
    "EvidencePage",
    "EvidenceQuery",
    "EvidenceRecord",
    "Staleness",
    "Subject",
    "WithheldEvidence",
    "evidence_bundle",
    "iter_evidence_export",
    "load_capability_mapping",
    "newest_evidence_ages",
    "policy_for",
    "policy_version_for",
    "query_evidence",
    "recompute_evidence",
    "recompute_for_run",
    "staleness_of",
    "subject_of_run",
]

logger = logging.getLogger(__name__)

EVIDENCE_SCHEMA = "capability.evidence"
EVIDENCE_SCHEMA_VERSION = SchemaVersion(1, 0)
"""The ``capability.evidence`` version this build writes (spec §7.3)."""

BUNDLE_SCHEMA = "benchmark.evidence_bundle"
BUNDLE_SCHEMA_VERSION = SchemaVersion(1, 0)
"""The ``benchmark.evidence_bundle`` version this build writes."""

DEFAULT_EVIDENCE_LIMIT = 50
MAX_EVIDENCE_LIMIT = 500

_GENERATOR = GeneratorInfo(name="freeweight", version=__version__)
_SHIPPED_WEIGHTS = Path(__file__).resolve().parent.parent / "config" / "capability_weights.toml"
_GOAL_RUNNER = "goal"
_GOAL_ROOT = "user"
_COMPOSITE_KEY = "composite_score"
_CRITERION_PREFIX = "criterion."
_MIX_PREFIX = "score_method_mix_"
_RUNGS = ("rule", "reference", "human", "judge")
_MIX_TOLERANCE = 1e-9
_UNRECORDED = "unrecorded"


class EvidenceNotFound(NotFoundError):
    """No evidence row matches the reference.

    Its own stable code per spec §13 rather than the generic ``NOT_FOUND``, so a caller can tell
    "no such evidence" from "no such run".
    """

    code: ClassVar[str] = "EVIDENCE_NOT_FOUND"


# ---------------------------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subject:
    """One measurement subject: a model under a runtime profile on a machine (ADR-0023).

    Attributes:
        model_id: The ``models`` row.
        runtime_profile_id: The ``runtime_profiles`` row.
        machine_id: The ``machines`` row.
    """

    model_id: str
    runtime_profile_id: str
    machine_id: str


@dataclass(frozen=True, slots=True)
class ContributingMetric:
    """One benchmark metric's contribution to a capability score (ADR-0022 §1).

    Attributes:
        metric_key: ``<suite>.<metric>`` for a shipped source, ``criterion.<key>`` for a goal's
            own record, ``goal.<slug>.composite_score`` for a goal contributing to a shipped
            capability.
        weight: The source's weight.
        sample_count: Supported samples behind it.
        contribution: The normalised ``0..1`` value it contributed. Internal — the wire form
            carries the three fields above, so a consumer can weigh but not re-derive.
    """

    metric_key: str
    weight: float
    sample_count: int
    contribution: float

    def wire(self) -> dict[str, Any]:
        """The ``contributing_metrics`` entry as ``capability.evidence`` carries it."""
        return {
            "metric_key": self.metric_key,
            "weight": self.weight,
            "sample_count": self.sample_count,
        }

    def as_json(self) -> dict[str, Any]:
        """The entry as the evidence row stores it and the UI explains it."""
        return {**self.wire(), "contribution": self.contribution}


@dataclass(frozen=True, slots=True)
class WithheldEvidence:
    """One capability that produced **no** record, and the reason it did not.

    The aggregation service says so in its report rather than skipping silently, because "we
    emitted it quietly at the floor" and "we dropped it quietly" are the two failures the gate and
    the absence rule exist to prevent.

    Attributes:
        capability_id: What was withheld.
        code: A stable reason code — ``GOAL_UNCALIBRATED``, ``CALIBRATION_REQUIRED``,
            ``CALIBRATION_STALE``, ``UNMEASURED``.
        reason: The reason, in words.
        model_canonical_id: Whose evidence it would have been.
        goal_slug: The goal, when a goal was the source.
    """

    capability_id: str
    code: str
    reason: str
    model_canonical_id: str
    goal_slug: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the withholding as the report renders it."""
        return {
            "capability_id": self.capability_id,
            "code": self.code,
            "reason": self.reason,
            "model": self.model_canonical_id,
            "goal_slug": self.goal_slug,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:  # noqa: PLR0904 — a record is read in several shapes, all thin
    """One ``capability.evidence`` record, as stored and as exported.

    Every field of ADR-0022 §1's normative set plus ADR-0032 §5's goal group, the three internal
    columns the row carries beside them (the factor breakdown, the policy parameters, the goal
    id), and the identity rows the wire form is built from.
    """

    id: str
    model_id: str
    model_canonical_id: str
    identity_confidence: str
    model_payload: Mapping[str, Any]
    model_descriptor_id: str | None
    runtime_profile_id: str
    runtime_profile_hash: str
    machine_id: str
    machine_fingerprint: str
    capability_id: str
    score: float
    confidence: float
    sample_count: int
    excluded_count: int
    dispersion: float | None
    dispersion_unavailable_reason: str | None
    measured_at: datetime
    computed_at: datetime
    policy_version: str
    policy: Mapping[str, Any]
    vocabulary_version: str
    benchmark_versions: Mapping[str, str]
    dataset_hashes: Mapping[str, str]
    prompt_subset_hashes: Mapping[str, str]
    contributing_metrics: tuple[ContributingMetric, ...]
    source_run_ids: tuple[str, ...]
    environment: Environment
    judge_validity_factor: float
    factors: Mapping[str, Any]
    goal_id: str | None = None
    goal_slug: str | None = None
    goal_hash: str | None = None
    goal_pack_version: str | None = None
    score_method_mix: Mapping[str, float] | None = None
    judge_set: Mapping[str, Any] | None = None
    calibration: Mapping[str, Any] | None = None

    @property
    def is_goal_sourced(self) -> bool:
        """Whether a user-authored goal produced or contributed to this record."""
        return self.goal_hash is not None

    @property
    def kind(self) -> MetricKind:
        """The comparability class the confidence was computed under."""
        half_life = self.factors.get("half_life_days")
        if half_life is not None and float(half_life) < float(
            self.policy.get("quality_half_life_days", 90.0)
        ):
            return MetricKind.PERFORMANCE
        return MetricKind.QUALITY

    def wire_payload(self) -> Any:  # noqa: ANN401 — a CapabilityEvidenceOut instance
        """Build and validate the ``capability.evidence`` payload through SetSpec's writer model.

        Through the *outbound* model, so every coherence rule the contract has — the goal group's
        five rules, ``measured_at ≤ computed_at``, the score method mix summing to one — runs over
        what FreeWeight exports rather than over what a test happened to cover.

        Raises:
            ValidationError: The stored row does not satisfy the contract. Raised rather than
                repaired: an export that silently corrected a stored inconsistency would hide a
                database problem behind a document that looks fine.
        """
        from pydantic import ValidationError as PydanticValidationError
        from setspec.capability.v1 import CapabilityEvidenceOut

        payload: dict[str, Any] = {
            "model": dict(self.model_payload),
            "runtime_profile_hash": self.runtime_profile_hash,
            "machine_fingerprint": self.machine_fingerprint,
            "capability_id": self.capability_id,
            "score": self.score,
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "excluded_count": self.excluded_count,
            "dispersion": UNSUPPORTED if self.dispersion is None else self.dispersion,
            "measured_at": self.measured_at,
            "computed_at": self.computed_at,
            "policy_version": self.policy_version,
            "vocabulary_version": self.vocabulary_version,
            "benchmark_versions": dict(self.benchmark_versions),
            "dataset_hashes": dict(self.dataset_hashes),
            "prompt_subset_hashes": dict(self.prompt_subset_hashes),
            "contributing_metrics": [metric.wire() for metric in self.contributing_metrics],
            "source_run_ids": list(self.source_run_ids),
            "environment": {
                "provider_kind": ProviderKind(self.environment.provider_kind),
                "provider_version": self.environment.provider_version or _UNRECORDED,
                "gpu_driver_version": self.environment.gpu_driver_version,
                "cuda_version": self.environment.cuda_version,
                "os_version": self.environment.os_version,
            },
            "judge_validity_factor": self.judge_validity_factor,
        }
        if self.goal_hash is not None:
            payload.update(
                {
                    "goal_hash": self.goal_hash,
                    "goal_pack_version": self.goal_pack_version,
                    "score_method_mix": (
                        dict(self.score_method_mix) if self.score_method_mix is not None else None
                    ),
                    "judge_set": dict(self.judge_set) if self.judge_set is not None else None,
                    "calibration": (
                        dict(self.calibration) if self.calibration is not None else None
                    ),
                    "uncalibrated": False,
                }
            )
        try:
            return CapabilityEvidenceOut.model_validate(payload)
        except PydanticValidationError as exc:
            raise ValidationError(
                f"Evidence {self.id!r} ({self.capability_id}) cannot be exported as "
                f"capability.evidence: {exc.errors()[0]['msg']}",
                details={"evidence": self.id, "capability_id": self.capability_id},
            ) from exc

    def envelope(self, *, generated_at: datetime) -> dict[str, Any]:
        """Return this record as a parsed ``capability.evidence`` SetSpec envelope.

        Serialised canonically and read back, so the mapping a caller receives is exactly what a
        consumer would parse from the file form — not a near-copy that could drift from it.
        """
        text = dump_envelope(
            self.wire_payload(),
            schema=EVIDENCE_SCHEMA,
            version=EVIDENCE_SCHEMA_VERSION,
            generator=_GENERATOR,
            generated_at=generated_at,
        )
        parsed: dict[str, Any] = json.loads(text)
        return parsed

    def explanation(self) -> dict[str, Any]:
        """The parts a person needs to answer "why is this score what it is".

        Spec's acceptance criterion 3: every record names its contributing benchmarks, weights and
        sample counts, and the UI can explain any score. The six factors are listed by name with
        the facts they came from, because a single confidence number explains nothing.
        """
        return {
            "contributing_metrics": [metric.as_json() for metric in self.contributing_metrics],
            "factors": dict(self.factors),
            "policy": dict(self.policy),
            "policy_version": self.policy_version,
            "goal_slug": self.goal_slug,
        }

    def row(self) -> dict[str, Any]:
        """Return the ``capability_evidence`` column mapping for this record."""
        return {
            "id": self.id,
            "model_id": self.model_id,
            "runtime_profile_id": self.runtime_profile_id,
            "machine_id": self.machine_id,
            "model_descriptor_id": self.model_descriptor_id,
            "capability_id": self.capability_id,
            "score": self.score,
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "excluded_count": self.excluded_count,
            "dispersion": self.dispersion,
            "dispersion_unavailable_reason": self.dispersion_unavailable_reason,
            "identity_confidence": self.identity_confidence,
            "source_run_ids_json": list(self.source_run_ids),
            "contributing_metrics_json": [m.as_json() for m in self.contributing_metrics],
            "benchmark_versions_json": dict(self.benchmark_versions),
            "dataset_hashes_json": dict(self.dataset_hashes),
            "prompt_subset_hashes_json": dict(self.prompt_subset_hashes),
            "environment_snapshot_json": self.environment.as_json(),
            "measured_at": self.measured_at,
            "computed_at": self.computed_at,
            "policy_version": self.policy_version,
            "policy_json": dict(self.policy),
            "vocabulary_version": self.vocabulary_version,
            "judge_validity_factor": self.judge_validity_factor,
            "confidence_factors_json": dict(self.factors),
            "goal_id": self.goal_id,
            "goal_hash": self.goal_hash,
            "goal_pack_version": self.goal_pack_version,
            "score_method_mix_json": (
                dict(self.score_method_mix) if self.score_method_mix is not None else None
            ),
            "judge_set_json": dict(self.judge_set) if self.judge_set is not None else None,
            "calibration_json": dict(self.calibration) if self.calibration is not None else None,
            "created_at": self.computed_at,
        }


@dataclass(frozen=True, slots=True)
class Staleness:
    """ADR-0017's staleness surface for one record, as of the moment it is read.

    Attributes:
        stale: Whether to badge the record and offer a re-run.
        freshness: The freshness factor *now* — the stored one was true at ``computed_at``.
        age_days: ``now − measured_at``.
        drift: The drifted environment dimensions recorded at computation.
        reasons: Why it is stale, in words; empty when it is not.
    """

    stale: bool
    freshness: float
    age_days: float
    drift: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the verdict as the UI and the CLI render it."""
        return {
            "stale": self.stale,
            "freshness_factor": self.freshness,
            "age_days": self.age_days,
            "drift": list(self.drift),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class AggregationReport:
    """What one recomputation emitted, what it withheld, and what it kept apart.

    Attributes:
        computed_at: The ``computed_at`` every emitted record carries.
        policy_version: The policy version they were written under.
        subjects: How many measurement subjects had completed runs.
        emitted: The records written.
        withheld: Capabilities that produced no record, each with its reason.
        separated: Suite partitions that were kept apart as different measurements.
        notes: Configuration gaps found on the way — a source that could not be normalised.
    """

    computed_at: datetime
    policy_version: str
    subjects: int
    emitted: tuple[EvidenceRecord, ...] = ()
    withheld: tuple[WithheldEvidence, ...] = ()
    separated: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the report as ``evidence show --recompute`` prints it."""
        return {
            "computed_at": to_rfc3339(self.computed_at),
            "policy_version": self.policy_version,
            "subjects": self.subjects,
            "emitted": [
                {
                    "capability_id": record.capability_id,
                    "model": record.model_canonical_id,
                    "score": record.score,
                    "confidence": record.confidence,
                    "sample_count": record.sample_count,
                }
                for record in self.emitted
            ],
            "withheld": [item.as_json() for item in self.withheld],
            "separated": list(self.separated),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    """Filters for ``GET /api/v1/evidence`` and the export (API §6).

    Attributes:
        capability: Exact capability ID.
        model: Model canonical ID, ULID or unambiguous prefix.
        machine: Machine fingerprint.
        runtime_profile: Runtime profile hash.
        min_confidence: Records at or above this confidence.
        since: Records whose ``computed_at`` is later — the incremental export filter. A client
            sends back the previous bundle's ``generated_at``, never its own clock (ADR-0022 §5).
        limit: Page size, clamped to :data:`MAX_EVIDENCE_LIMIT`.
        cursor: Opaque continuation token from a previous page.
    """

    capability: str | None = None
    model: str | None = None
    machine: str | None = None
    runtime_profile: str | None = None
    min_confidence: float | None = None
    since: datetime | None = None
    limit: int = DEFAULT_EVIDENCE_LIMIT
    cursor: str | None = None

    def clamped_limit(self) -> int:
        """The effective page size, clamped into ``[1, MAX_EVIDENCE_LIMIT]``."""
        return max(1, min(self.limit, MAX_EVIDENCE_LIMIT))

    @property
    def selects_everything(self) -> bool:
        """Whether no filter narrows the selection.

        Only such a selection is a **complete** bundle (ADR-0022 §5): a filtered bundle can add
        and update evidence but must never let a consumer infer that something absent from it was
        removed.
        """
        return (
            self.capability is None
            and self.model is None
            and self.machine is None
            and self.runtime_profile is None
            and self.min_confidence is None
            and self.since is None
        )


@dataclass(frozen=True, slots=True)
class EvidencePage:
    """One page of evidence records.

    Attributes:
        records: The records, ordered by ``(capability_id, id)``.
        limit: The limit actually applied.
        next_cursor: The token for the following page, or ``None`` at the end.
        has_more: Always present, per the collection envelope.
        generated_at: The ``generated_at`` every item envelope carries.
    """

    records: tuple[EvidenceRecord, ...]
    limit: int
    next_cursor: str | None
    has_more: bool
    generated_at: datetime

    def as_json(self) -> dict[str, Any]:
        """The collection envelope API standards §3 requires, items being SetSpec envelopes."""
        return {
            "items": [record.envelope(generated_at=self.generated_at) for record in self.records],
            "page": {
                "limit": self.limit,
                "next_cursor": self.next_cursor,
                "has_more": self.has_more,
            },
        }


# ---------------------------------------------------------------------------------------------
# Policy and mapping
# ---------------------------------------------------------------------------------------------


def load_capability_mapping(path: Path | None = None) -> CapabilityMapping:
    """Load the capability mapping — the shipped file, or the user's own.

    Args:
        path: A custom ``capability_weights.toml``, or ``None`` for the shipped one.

    Returns:
        The parsed mapping.

    Raises:
        MappingInvalid: The file is missing, not TOML, or fails
            :func:`~freeweight.domain.capability_mapping.parse_mapping`.
    """
    target = path if path is not None else _SHIPPED_WEIGHTS
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise MappingInvalid(
            f"Cannot read the capability mapping at {target}: {exc}.",
            details={"path": str(target)},
        ) from exc
    try:
        body = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MappingInvalid(
            f"The capability mapping at {target} is not valid TOML: {exc}.",
            details={"path": str(target)},
        ) from exc
    return parse_mapping(body)


def policy_for(settings: EvidenceSettings | None = None) -> ConfidencePolicy:
    """Build the confidence policy from the ``[evidence]`` section, or the shipped defaults."""
    if settings is None:
        return ConfidencePolicy()
    return ConfidencePolicy(
        n_target=settings.n_target,
        quality_half_life_days=settings.quality_half_life_days,
        performance_half_life_days=settings.performance_half_life_days,
        freshness_floor=settings.freshness_floor,
        stale_below=settings.stale_below,
        name_only_identity_factor=settings.name_only_identity_factor,
        performance_drift_factor=settings.performance_drift_factor,
        quality_drift_factor=settings.quality_drift_factor,
    )


def policy_version_for(policy: ConfidencePolicy, mapping: CapabilityMapping) -> str:
    """The ``policy_version`` evidence computed under ``policy`` and ``mapping`` carries.

    The mapping's own version when both are the shipped ones; a content-derived version the
    moment either is customised, so two policies are two versions rather than one version meaning
    two things (ADR-0022 §3).
    """
    customised = mapping.content_hash != load_capability_mapping().content_hash
    if customised:
        return f"{mapping.version}+{mapping.content_hash[len('sha256:') :][:8]}"
    return policy.policy_version(mapping_version=mapping.version, customised=False)


# ---------------------------------------------------------------------------------------------
# Reading the runs back
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MergedMetric:
    """One run-level metric, merged across the runs of one suite partition."""

    metric_key: str
    value: float | None
    unit: str
    higher_is_better: bool
    sample_count: int
    excluded_count: int
    dispersion: float | None


@dataclass(frozen=True, slots=True)
class _GoalFacts:
    """What the database knows about the goal behind a goal suite, at recomputation time."""

    goal_id: str
    slug: str
    goal_hash: str
    goal_pack_version: str
    contributes_to: str | None
    weights: Mapping[str, float]
    judged_keys: tuple[str, ...]
    report: Any  # a calibration_reports goal-level row, or None
    criterion_reports: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _SuiteMeasurement:
    """One subject's newest partition of one suite, merged into one measurement."""

    suite_key: str
    suite_version: str
    runner: str
    dataset_hashes: Mapping[str, str]
    prompt_subset_hash: str | None
    run_ids: tuple[str, ...]
    completed_at: datetime
    environment: Environment
    model_descriptor_id: str | None
    metrics: Mapping[str, _MergedMetric]
    goal: _GoalFacts | None = None
    suite_goal_hash: str | None = None


@dataclass(frozen=True, slots=True)
class _SubjectRuns:
    """Everything one subject's completed runs contribute, read once."""

    subject: Subject
    model: Any
    machine: Any
    profile: Any
    descriptor: Any
    measurements: tuple[_SuiteMeasurement, ...]
    separated: tuple[str, ...]
    current_environment: Environment | None


def _partition_key(suite: Any, run: Any) -> tuple[str, str, str]:  # noqa: ANN401 — ORM rows
    """The hard-separation key within one suite: version, dataset hashes, prompt subset hash."""
    del run
    dataset = (
        {str(k): str(v) for k, v in dict(suite.dataset_hashes_json).items()}
        if isinstance(suite.dataset_hashes_json, dict)
        else {}
    )
    return (str(suite.version), canonical_json(dataset), str(suite.prompt_subset_hash or ""))


def _environment_of(run: Any, model: Any) -> Environment:  # noqa: ANN401 — ORM rows
    """The environment one run was measured in, read from its stored fingerprint document."""
    document = (
        dict(run.fingerprint_document_json)
        if isinstance(run.fingerprint_document_json, dict)
        else {}
    )
    section = document.get("environment")
    section = dict(section) if isinstance(section, dict) else {}
    section["provider_version"] = run.provider_version
    return Environment.from_json(
        section, provider_kind=str(run.provider_kind or model.provider_kind)
    )


def _merge_metrics(rows_by_run: Sequence[Sequence[Any]]) -> dict[str, _MergedMetric]:
    """Merge the run-level metric rows of several runs of one partition, key by key.

    Sample-count-weighted, so a twenty-sample run outweighs a three-sample one; sample and
    excluded counts add; the dispersion is the sample-count-weighted mean of the runs' own
    coefficients of variation, which is an approximation of the pooled figure and is documented
    as one. A key that no run measured is unsupported, never zero.
    """
    by_key: dict[str, list[Any]] = {}
    for rows in rows_by_run:
        for row in rows:
            if row.run_test_id is None and row.sample_id is None:
                by_key.setdefault(str(row.metric_key), []).append(row)
    merged: dict[str, _MergedMetric] = {}
    for key, rows in by_key.items():
        first = rows[0]
        supported = [row for row in rows if row.numeric_value is not None]
        excluded = sum(int(row.excluded_count or 0) for row in rows)
        if not supported:
            merged[key] = _MergedMetric(
                metric_key=key,
                value=None,
                unit=str(first.unit),
                higher_is_better=bool(first.higher_is_better),
                sample_count=0,
                excluded_count=excluded,
                dispersion=None,
            )
            continue
        weights = [max(1, int(row.sample_count or 1)) for row in supported]
        total = sum(weights)
        value = sum(float(row.numeric_value) * w for row, w in zip(supported, weights, strict=True))
        dispersed = [
            (float(row.coefficient_of_variation), w)
            for row, w in zip(supported, weights, strict=True)
            if row.coefficient_of_variation is not None
        ]
        dispersion = (
            sum(cov * w for cov, w in dispersed) / sum(w for _, w in dispersed)
            if dispersed
            else None
        )
        merged[key] = _MergedMetric(
            metric_key=key,
            value=value / total,
            unit=str(first.unit),
            higher_is_better=bool(first.higher_is_better),
            sample_count=total,
            excluded_count=excluded,
            dispersion=dispersion,
        )
    return merged


def _goal_facts(session: Session, suite: Any) -> _GoalFacts | None:  # noqa: ANN401 — an ORM row
    """Read the goal, its criteria and its calibration report for one goal suite."""
    from freeweight.infrastructure.db.models_goals import Goal
    from freeweight.infrastructure.db.repositories.calibration import CalibrationReportRepository
    from freeweight.infrastructure.db.repositories.goals import GoalRepository

    if not suite.goal_id:
        return None
    goal = session.get(Goal, suite.goal_id)
    if goal is None:
        return None
    criteria = GoalRepository().criteria(session, goal.id)
    reports = CalibrationReportRepository().list_for_goal(session, goal.id)
    return _GoalFacts(
        goal_id=goal.id,
        slug=str(goal.slug),
        goal_hash=str(goal.goal_hash),
        goal_pack_version=str(goal.goal_pack_version),
        contributes_to=goal.contributes_to,
        weights={str(row.key): float(row.weight) for row in criteria},
        judged_keys=tuple(str(row.key) for row in criteria if row.rung == "judge"),
        report=next((row for row in reports if row.goal_criterion_id is None), None),
        criterion_reports=tuple(row for row in reports if row.goal_criterion_id is not None),
    )


def _current_environment(session: Session, machine_id: str) -> Environment | None:
    """The machine's environment *now*: its most recently completed run's, on any subject."""
    from sqlalchemy import select

    from freeweight.infrastructure.db.models_runs import Run
    from freeweight.infrastructure.db.repositories.models import ModelRepository

    newest = session.scalars(
        select(Run)
        .where(Run.machine_id == machine_id, Run.status == "completed")
        .order_by(Run.completed_at.desc(), Run.id.desc())
        .limit(1)
    ).first()
    if newest is None:
        return None
    model = ModelRepository().get_by_id(session, newest.model_id)
    if model is None:  # pragma: no cover — a RESTRICT foreign key makes this unreachable
        return None
    return _environment_of(newest, model)


def _read_subjects(session: Session, subject: Subject | None) -> list[_SubjectRuns]:
    """Read every completed run, grouped by subject, merged by suite partition."""
    from sqlalchemy import select

    from freeweight.infrastructure.db.models import Machine, ModelDescriptor, RuntimeProfile
    from freeweight.infrastructure.db.models_runs import BenchmarkSuite, Run
    from freeweight.infrastructure.db.repositories.models import ModelRepository
    from freeweight.infrastructure.db.repositories.runs import MetricValueRepository

    statement = (
        select(Run, BenchmarkSuite)
        .join(BenchmarkSuite, BenchmarkSuite.id == Run.suite_id)
        .where(Run.status == "completed", Run.completed_at.is_not(None))
        .order_by(Run.completed_at.desc(), Run.id.desc())
    )
    if subject is not None:
        statement = statement.where(
            Run.model_id == subject.model_id,
            Run.runtime_profile_id == subject.runtime_profile_id,
            Run.machine_id == subject.machine_id,
        )
    grouped: dict[Subject, list[tuple[Any, Any]]] = {}
    for run, suite in session.execute(statement):
        key = Subject(run.model_id, run.runtime_profile_id, run.machine_id)
        grouped.setdefault(key, []).append((run, suite))

    subjects: list[_SubjectRuns] = []
    environments: dict[str, Environment | None] = {}
    for key, pairs in grouped.items():
        model = ModelRepository().get_by_id(session, key.model_id)
        machine = session.get(Machine, key.machine_id)
        profile = session.get(RuntimeProfile, key.runtime_profile_id)
        if model is None or machine is None or profile is None:  # pragma: no cover — RESTRICT
            continue
        by_suite: dict[str, dict[tuple[str, str, str], list[tuple[Any, Any]]]] = {}
        for run, suite in pairs:
            by_suite.setdefault(str(suite.key), {}).setdefault(
                _partition_key(suite, run), []
            ).append((run, suite))
        measurements: list[_SuiteMeasurement] = []
        separated: list[str] = []
        for suite_key, partitions in by_suite.items():
            # Runs are ordered newest first, so the first partition seen is the newest one.
            newest_key = next(iter(partitions))
            for other_key, others in partitions.items():
                if other_key != newest_key:
                    separated.append(
                        f"{model.canonical_id}: {len(others)} run(s) of {suite_key} at version "
                        f"{other_key[0]} kept apart from version {newest_key[0]} — a different "
                        "suite version, dataset or prompt set is a different measurement."
                    )
            chosen = partitions[newest_key]
            newest_run, newest_suite = chosen[0]
            rows_by_run = [
                MetricValueRepository().list_for_run(session, run.id) for run, _ in chosen
            ]
            measurements.append(
                _SuiteMeasurement(
                    suite_key=suite_key,
                    suite_version=str(newest_suite.version),
                    runner=str(newest_suite.runner),
                    dataset_hashes=(
                        {str(k): str(v) for k, v in dict(newest_suite.dataset_hashes_json).items()}
                        if isinstance(newest_suite.dataset_hashes_json, dict)
                        else {}
                    ),
                    prompt_subset_hash=newest_suite.prompt_subset_hash,
                    run_ids=tuple(run.id for run, _ in chosen),
                    completed_at=newest_run.completed_at,
                    environment=_environment_of(newest_run, model),
                    model_descriptor_id=newest_run.model_descriptor_id,
                    metrics=_merge_metrics(rows_by_run),
                    goal=(
                        _goal_facts(session, newest_suite)
                        if newest_suite.runner == _GOAL_RUNNER
                        else None
                    ),
                    suite_goal_hash=newest_suite.goal_hash,
                )
            )
        newest_overall = max(measurements, key=lambda item: item.completed_at)
        descriptor = (
            session.get(ModelDescriptor, newest_overall.model_descriptor_id)
            if newest_overall.model_descriptor_id
            else None
        )
        if key.machine_id not in environments:
            environments[key.machine_id] = _current_environment(session, key.machine_id)
        subjects.append(
            _SubjectRuns(
                subject=key,
                model=model,
                machine=machine,
                profile=profile,
                descriptor=descriptor,
                measurements=tuple(measurements),
                separated=tuple(separated),
                current_environment=environments[key.machine_id],
            )
        )
    return subjects


# ---------------------------------------------------------------------------------------------
# Computing the records
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _GoalContribution:
    """A calibrated goal's composite, offered to the capability it ``contributes_to``."""

    capability_id: str
    goal: _GoalFacts
    measurement: _SuiteMeasurement
    composite: _MergedMetric
    judge_validity_factor: float
    score_method_mix: Mapping[str, float] | None
    judge_set: Mapping[str, Any] | None
    calibration: Mapping[str, Any] | None


@dataclass(slots=True)
class _Accumulator:
    """One capability's contributions, gathered before the record is built."""

    contributions: list[tuple[float, float]] = field(default_factory=list)
    metrics: list[ContributingMetric] = field(default_factory=list)
    measurements: list[_SuiteMeasurement] = field(default_factory=list)
    dispersions: list[tuple[float, float]] = field(default_factory=list)
    validity: list[tuple[float, float]] = field(default_factory=list)
    mixes: list[tuple[float, Mapping[str, float]]] = field(default_factory=list)
    goals: list[_GoalContribution] = field(default_factory=list)
    kinds: set[MetricKind] = field(default_factory=set)


def _narrow_judge_set(stored: Mapping[str, Any]) -> dict[str, Any] | None:
    """Narrow a stored judge-set record to the four fields SetSpec's own model declares."""
    if not stored:
        return None
    return {
        "jurors": [str(item) for item in stored.get("jurors", ())],
        "prompt_id": str(stored.get("prompt_id", "")),
        "prompt_version": str(stored.get("prompt_version", "")),
        "prompt_sha256": str(stored.get("prompt_sha256", "")),
        "remote": bool(stored.get("remote", False)),
    }


def _calibration_block(goal: _GoalFacts) -> dict[str, Any] | None:
    """Build the ``calibration`` group from the goal-level and per-criterion reports.

    ``kappa_w`` is the goal-level weighted figure the gate compared; ``rho``, ``mae`` and ``bias``
    are the criterion weights' weighted means over the per-criterion reports, because the
    goal-level row records only the coefficient the gate uses. A goal whose per-criterion reports
    carry no agreement figures has no calibration block to export.
    """
    report = goal.report
    if report is None or report.kappa_w is None:
        return None
    measured = [
        (float(goal.weights.get(_key_of(goal, row), 0.0)) or 1.0, row)
        for row in goal.criterion_reports
        if row.rho is not None and row.mae is not None and row.bias is not None
    ]
    if not measured:
        return None
    total = sum(weight for weight, _ in measured)
    return {
        "kappa_w": float(report.kappa_w),
        "rho": sum(weight * float(row.rho) for weight, row in measured) / total,
        "mae": sum(weight * float(row.mae) for weight, row in measured) / total,
        "bias": sum(weight * float(row.bias) for weight, row in measured) / total,
        "n_anchor": int(report.n_anchor),
        "n_holdout": int(report.n_holdout),
        "graded_by": str(report.graded_by or "unknown"),
        "measured_at": to_rfc3339(report.measured_at),
    }


def _key_of(goal: _GoalFacts, report: Any) -> str:  # noqa: ANN401 — a calibration_reports row
    """The criterion key one per-criterion report belongs to, by weight lookup on its id."""
    del goal
    return str(getattr(report, "criterion_key", "") or "")


def _mix_of(measurement: _SuiteMeasurement) -> dict[str, float] | None:
    """The goal run's ``score_method_mix``, from its ``score_method_mix_<rung>`` metrics."""
    shares: dict[str, float] = {}
    for rung in _RUNGS:
        metric = measurement.metrics.get(f"{_MIX_PREFIX}{rung}")
        if metric is not None and metric.value is not None:
            shares[rung] = max(0.0, float(metric.value))
    total = sum(shares.values())
    if total <= 0:
        return None
    return {rung: share / total for rung, share in shares.items()}


def _gate(goal: _GoalFacts, measurement: _SuiteMeasurement) -> tuple[str, str] | None:
    """Return ``(code, reason)`` when the goal's evidence must be withheld, else ``None``."""
    if not goal.judged_keys:
        return None
    report = goal.report
    if report is None:
        return (
            "CALIBRATION_REQUIRED",
            f"goal {goal.slug!r} has judged criteria and has never been calibrated; grade its "
            "samples and run the calibration before its evidence can mean anything.",
        )
    if measurement.suite_goal_hash and str(report.goal_hash) != str(measurement.suite_goal_hash):
        return (
            "CALIBRATION_STALE",
            f"goal {goal.slug!r} was calibrated against rubric {report.goal_hash[:16]} but this "
            f"run measured rubric {str(measurement.suite_goal_hash)[:16]}; agreement measured on "
            "one rubric says nothing about another.",
        )
    if not report.passed_gate:
        return (
            "GOAL_UNCALIBRATED",
            f"goal {goal.slug!r} is below its calibration gate (weighted kappa_w "
            f"{report.kappa_w if report.kappa_w is not None else 'unmeasured'} against "
            f"{report.min_agreement}); the run is inspectable but emits no evidence "
            "(ADR-0032 §3).",
        )
    return None


def _goal_records(  # noqa: PLR0913 — a goal record is a function of exactly these facts
    rows: _SubjectRuns,
    measurement: _SuiteMeasurement,
    *,
    goal: _GoalFacts,
    policy: ConfidencePolicy,
    policy_version: str,
    now: datetime,
) -> tuple[EvidenceRecord | None, _GoalContribution | None, WithheldEvidence | None]:
    """Build a goal suite's ``user.<slug>`` record and its optional contribution."""
    capability_id = f"{_GOAL_ROOT}.{goal.slug}"
    composite = measurement.metrics.get(_COMPOSITE_KEY)
    if composite is None or composite.value is None:
        return (
            None,
            None,
            WithheldEvidence(
                capability_id=capability_id,
                code="UNMEASURED",
                reason=f"goal {goal.slug!r} produced no composite score on this subject.",
                model_canonical_id=rows.model.canonical_id,
                goal_slug=goal.slug,
            ),
        )
    gate = _gate(goal, measurement)
    if gate is not None:
        code, reason = gate
        return (
            None,
            None,
            WithheldEvidence(
                capability_id=capability_id,
                code=code,
                reason=reason,
                model_canonical_id=rows.model.canonical_id,
                goal_slug=goal.slug,
            ),
        )
    factor = float(goal.report.judge_validity_factor) if goal.judged_keys else 1.0
    judge_set = _narrow_judge_set(dict(goal.report.judge_set_json or {})) if goal.report else None
    calibration = _calibration_block(goal) if goal.judged_keys else None
    if factor < 1.0 and (calibration is None or judge_set is None):
        return (
            None,
            None,
            WithheldEvidence(
                capability_id=capability_id,
                code="CALIBRATION_REQUIRED",
                reason=(
                    f"goal {goal.slug!r} carries a validity factor of {factor} but its calibration "
                    "report has no agreement figures to export beside it; recalibrate."
                ),
                model_canonical_id=rows.model.canonical_id,
                goal_slug=goal.slug,
            ),
        )
    contributing = [
        ContributingMetric(
            metric_key=key,
            weight=float(goal.weights.get(key[len(_CRITERION_PREFIX) :], 0.0)) or 1.0,
            sample_count=metric.sample_count,
            contribution=float(metric.value),
        )
        for key, metric in sorted(measurement.metrics.items())
        if key.startswith(_CRITERION_PREFIX) and metric.value is not None
    ] or [
        ContributingMetric(
            metric_key=_COMPOSITE_KEY,
            weight=1.0,
            sample_count=composite.sample_count,
            contribution=float(composite.value),
        )
    ]
    breakdown = compute_confidence(
        sample_count=composite.sample_count,
        dispersion=composite.dispersion,
        measured_at=measurement.completed_at,
        now=now,
        kind=MetricKind.QUALITY,
        measured_environment=measurement.environment,
        current_environment=rows.current_environment,
        identity_confidence=rows.model.identity_confidence,
        judge_validity_factor=factor,
        policy=policy,
    )
    mix = _mix_of(measurement)
    record = EvidenceRecord(
        id=new_id(),
        model_id=rows.model.id,
        model_canonical_id=rows.model.canonical_id,
        identity_confidence=rows.model.identity_confidence,
        model_payload=model_identity_payload(rows.model, rows.descriptor),
        model_descriptor_id=rows.descriptor.id if rows.descriptor is not None else None,
        runtime_profile_id=rows.profile.id,
        runtime_profile_hash=rows.profile.profile_hash,
        machine_id=rows.machine.id,
        machine_fingerprint=rows.machine.machine_fingerprint,
        capability_id=capability_id,
        score=min(1.0, max(0.0, float(composite.value))),
        confidence=breakdown.confidence,
        sample_count=composite.sample_count,
        excluded_count=composite.excluded_count,
        dispersion=composite.dispersion,
        dispersion_unavailable_reason=(
            None if composite.dispersion is not None else "fewer_than_two_samples"
        ),
        measured_at=measurement.completed_at,
        computed_at=now,
        policy_version=policy_version,
        policy=policy.as_json(),
        vocabulary_version=CAPABILITY_VOCABULARY_VERSION,
        benchmark_versions={measurement.suite_key: measurement.suite_version},
        dataset_hashes={
            f"{measurement.suite_key}/{name}": digest
            for name, digest in measurement.dataset_hashes.items()
        },
        prompt_subset_hashes=(
            {measurement.suite_key: measurement.prompt_subset_hash}
            if measurement.prompt_subset_hash
            else {}
        ),
        contributing_metrics=tuple(contributing),
        source_run_ids=measurement.run_ids,
        environment=measurement.environment,
        judge_validity_factor=factor,
        factors=breakdown.as_json(),
        goal_id=goal.goal_id,
        goal_slug=goal.slug,
        goal_hash=measurement.suite_goal_hash or goal.goal_hash,
        goal_pack_version=goal.goal_pack_version,
        score_method_mix=mix,
        judge_set=judge_set,
        calibration=calibration,
    )
    contribution = (
        _GoalContribution(
            capability_id=goal.contributes_to,
            goal=goal,
            measurement=measurement,
            composite=composite,
            judge_validity_factor=factor,
            score_method_mix=mix,
            judge_set=judge_set,
            calibration=calibration,
        )
        if goal.contributes_to
        else None
    )
    return record, contribution, None


def _blend_mix(mixes: Sequence[tuple[float, Mapping[str, float]]]) -> dict[str, float] | None:
    """Blend the score-method mixes of several sources by their weight."""
    total = sum(weight for weight, _ in mixes)
    if total <= 0:
        return None
    blended = dict.fromkeys(_RUNGS, 0.0)
    for weight, mix in mixes:
        for rung, share in mix.items():
            blended[rung] = blended.get(rung, 0.0) + weight * share / total
    scale = sum(blended.values())
    if scale <= 0:
        return None
    return {rung: share / scale for rung, share in blended.items()}


def _capability_record(  # noqa: PLR0913 — a record is a function of exactly these facts
    rows: _SubjectRuns,
    capability_id: str,
    accumulator: _Accumulator,
    *,
    policy: ConfidencePolicy,
    policy_version: str,
    now: datetime,
) -> EvidenceRecord | None:
    """Build one shipped capability's record from what its sources contributed."""
    score = weighted_score(accumulator.contributions)
    if score is None:
        return None
    kind = (
        MetricKind.QUALITY if accumulator.kinds <= {MetricKind.QUALITY} else MetricKind.PERFORMANCE
    )
    seen: dict[str, _SuiteMeasurement] = {}
    for measurement in accumulator.measurements:
        seen.setdefault(measurement.suite_key, measurement)
    sample_count = 0
    excluded_count = 0
    for suite_key, measurement in seen.items():
        own = [m for m in accumulator.metrics if _suite_of(m.metric_key, measurement) == suite_key]
        sample_count += max((m.sample_count for m in own), default=0)
        excluded_count += max(
            (
                measurement.metrics[_bare_key(m.metric_key, measurement)].excluded_count
                for m in own
                if _bare_key(m.metric_key, measurement) in measurement.metrics
            ),
            default=0,
        )
    dispersion = (
        sum(cov * w for cov, w in accumulator.dispersions)
        / sum(w for _, w in accumulator.dispersions)
        if accumulator.dispersions
        else None
    )
    validity_total = sum(w for w, _ in accumulator.validity)
    factor = (
        sum(w * v for w, v in accumulator.validity) / validity_total if validity_total > 0 else 1.0
    )
    factor = min(1.0, max(0.05, factor))
    newest = max(seen.values(), key=lambda item: item.completed_at)
    breakdown = compute_confidence(
        sample_count=sample_count,
        dispersion=dispersion,
        measured_at=newest.completed_at,
        now=now,
        kind=kind,
        measured_environment=newest.environment,
        current_environment=rows.current_environment,
        identity_confidence=rows.model.identity_confidence,
        judge_validity_factor=factor,
        policy=policy,
    )
    goal = max(accumulator.goals, key=lambda item: item.judge_validity_factor, default=None)
    return EvidenceRecord(
        id=new_id(),
        model_id=rows.model.id,
        model_canonical_id=rows.model.canonical_id,
        identity_confidence=rows.model.identity_confidence,
        model_payload=model_identity_payload(rows.model, rows.descriptor),
        model_descriptor_id=rows.descriptor.id if rows.descriptor is not None else None,
        runtime_profile_id=rows.profile.id,
        runtime_profile_hash=rows.profile.profile_hash,
        machine_id=rows.machine.id,
        machine_fingerprint=rows.machine.machine_fingerprint,
        capability_id=capability_id,
        score=score,
        confidence=breakdown.confidence,
        sample_count=sample_count,
        excluded_count=excluded_count,
        dispersion=dispersion,
        dispersion_unavailable_reason=None if dispersion is not None else "fewer_than_two_samples",
        measured_at=newest.completed_at,
        computed_at=now,
        policy_version=policy_version,
        policy=policy.as_json(),
        vocabulary_version=CAPABILITY_VOCABULARY_VERSION,
        benchmark_versions={key: m.suite_version for key, m in sorted(seen.items())},
        dataset_hashes={
            f"{key}/{name}": digest
            for key, m in sorted(seen.items())
            for name, digest in m.dataset_hashes.items()
        },
        prompt_subset_hashes={
            key: m.prompt_subset_hash for key, m in sorted(seen.items()) if m.prompt_subset_hash
        },
        contributing_metrics=tuple(accumulator.metrics),
        source_run_ids=tuple(run_id for _, m in sorted(seen.items()) for run_id in m.run_ids),
        environment=newest.environment,
        judge_validity_factor=factor,
        factors=breakdown.as_json(),
        goal_id=goal.goal.goal_id if goal is not None else None,
        goal_slug=goal.goal.slug if goal is not None else None,
        goal_hash=(goal.measurement.suite_goal_hash or goal.goal.goal_hash) if goal else None,
        goal_pack_version=goal.goal.goal_pack_version if goal is not None else None,
        score_method_mix=_blend_mix(accumulator.mixes) if goal is not None else None,
        judge_set=goal.judge_set if goal is not None else None,
        calibration=goal.calibration if goal is not None else None,
    )


def _suite_of(contributing_key: str, measurement: _SuiteMeasurement) -> str | None:
    """The suite a contributing metric key names, when it is this measurement's."""
    prefix = f"{measurement.suite_key}."
    if contributing_key.startswith(prefix):
        return measurement.suite_key
    return None


def _bare_key(contributing_key: str, measurement: _SuiteMeasurement) -> str:
    """Strip the suite prefix off a contributing metric key."""
    prefix = f"{measurement.suite_key}."
    return contributing_key[len(prefix) :] if contributing_key.startswith(prefix) else ""


def _records_for(  # noqa: PLR0913 — aggregation is a function of exactly these inputs
    rows: _SubjectRuns,
    *,
    mapping: CapabilityMapping,
    policy: ConfidencePolicy,
    policy_version: str,
    settings: EvidenceSettings,
    now: datetime,
) -> tuple[list[EvidenceRecord], list[WithheldEvidence], list[str]]:
    """Compute every record one subject's runs support."""
    records: list[EvidenceRecord] = []
    withheld: list[WithheldEvidence] = []
    notes: list[str] = []
    by_suite = {m.suite_key: m for m in rows.measurements}
    accumulators: dict[str, _Accumulator] = {}

    for measurement in rows.measurements:
        if measurement.goal is None:
            continue
        record, contribution, refusal = _goal_records(
            rows,
            measurement,
            goal=measurement.goal,
            policy=policy,
            policy_version=policy_version,
            now=now,
        )
        if record is not None:
            records.append(record)
        if refusal is not None:
            withheld.append(refusal)
            if measurement.goal.contributes_to:
                withheld.append(
                    WithheldEvidence(
                        capability_id=measurement.goal.contributes_to,
                        code=refusal.code,
                        reason=(
                            f"goal {measurement.goal.slug!r} contributes to this capability and "
                            "is withheld, so it contributes nothing here either."
                        ),
                        model_canonical_id=rows.model.canonical_id,
                        goal_slug=measurement.goal.slug,
                    )
                )
        if contribution is not None:
            accumulator = accumulators.setdefault(contribution.capability_id, _Accumulator())
            weight = settings.goal_contribution_weight
            value = min(1.0, max(0.0, float(contribution.composite.value or 0.0)))
            accumulator.contributions.append((weight, value))
            accumulator.metrics.append(
                ContributingMetric(
                    metric_key=f"goal.{contribution.goal.slug}.{_COMPOSITE_KEY}",
                    weight=weight,
                    sample_count=contribution.composite.sample_count,
                    contribution=value,
                )
            )
            accumulator.measurements.append(contribution.measurement)
            if contribution.composite.dispersion is not None:
                accumulator.dispersions.append((contribution.composite.dispersion, weight))
            accumulator.validity.append((weight, contribution.judge_validity_factor))
            if contribution.score_method_mix is not None:
                accumulator.mixes.append((weight, contribution.score_method_mix))
            accumulator.goals.append(contribution)
            accumulator.kinds.add(MetricKind.QUALITY)

    for capability_id in mapping.capabilities:
        accumulator = accumulators.setdefault(capability_id, _Accumulator())
        for source in mapping.sources[capability_id]:
            found = by_suite.get(source.suite_key)
            if found is None:
                continue
            metric = found.metrics.get(source.metric_key)
            if metric is None or metric.value is None:
                continue
            normalized = normalize_value(
                metric.value,
                unit=metric.unit,
                higher_is_better=metric.higher_is_better,
                source=source,
            )
            if normalized is None:
                notes.append(
                    f"{capability_id}: {source.contributing_metric_key} has unit {metric.unit!r} "
                    "and declares neither full_score_at nor zero_score_at; skipped."
                )
                continue
            accumulator.contributions.append((source.weight, normalized))
            accumulator.metrics.append(
                ContributingMetric(
                    metric_key=source.contributing_metric_key,
                    weight=source.weight,
                    sample_count=metric.sample_count,
                    contribution=normalized,
                )
            )
            accumulator.measurements.append(found)
            if metric.dispersion is not None:
                accumulator.dispersions.append((metric.dispersion, source.weight))
            accumulator.validity.append((source.weight, 1.0))
            accumulator.mixes.append((source.weight, {"rule": 1.0}))
            accumulator.kinds.add(metric_kind_for(source.metric_key))

    for capability_id, accumulator in accumulators.items():
        record = _capability_record(
            rows, capability_id, accumulator, policy=policy, policy_version=policy_version, now=now
        )
        if record is not None:
            records.append(record)
    return records, withheld, notes


# ---------------------------------------------------------------------------------------------
# Recomputation
# ---------------------------------------------------------------------------------------------


def _translate(exc: Exception) -> SuiteError:
    """Turn a raw driver failure into the suite's error hierarchy, as every service does."""
    if isinstance(exc, SuiteError):
        return exc
    return DatabaseUnavailable(f"Could not read the database: {exc}")


def recompute_evidence(
    database: Database,
    *,
    settings: EvidenceSettings | None = None,
    subject: Subject | None = None,
    now: datetime | None = None,
    clock: Clock = utc_now,
) -> AggregationReport:
    """Read the completed runs back and rewrite the evidence they support.

    Args:
        database: The application's database handle.
        settings: The ``[evidence]`` section, or ``None`` for the shipped defaults.
        subject: Recompute one subject only — what a run completion does — or every subject.
        now: The ``computed_at`` every record carries. Injected so a test can compute twice at
            two instants and assert what changed (coding standards §5).
        clock: Where ``now`` comes from when it is not given.

    Returns:
        The report: what was emitted, what was withheld and why, what was kept apart.

    Raises:
        MappingInvalid: The capability mapping cannot be loaded.
        DatabaseUnavailable: The database cannot be read or written.
    """
    settings = settings if settings is not None else EvidenceSettings()
    computed_at = now if now is not None else clock()
    mapping = load_capability_mapping(settings.weights_path)
    policy = policy_for(settings)
    version = policy_version_for(policy, mapping)
    try:
        with database.read() as session:
            subjects = _read_subjects(session, subject)
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise _translate(exc) from exc

    emitted: list[EvidenceRecord] = []
    withheld: list[WithheldEvidence] = []
    separated: list[str] = []
    notes: list[str] = []
    per_subject: list[tuple[Subject, list[EvidenceRecord]]] = []
    for rows in subjects:
        records, refused, found = _records_for(
            rows,
            mapping=mapping,
            policy=policy,
            policy_version=version,
            settings=settings,
            now=computed_at,
        )
        per_subject.append((rows.subject, records))
        emitted.extend(records)
        withheld.extend(refused)
        separated.extend(rows.separated)
        notes.extend(found)

    try:
        with database.write() as session:
            repository = EvidenceRepository()
            if subject is None:
                repository.delete_policy(session, policy_version=version)
            elif not per_subject:
                repository.replace_for_subject(
                    session,
                    model_id=subject.model_id,
                    runtime_profile_id=subject.runtime_profile_id,
                    machine_id=subject.machine_id,
                    policy_version=version,
                    rows=(),
                )
            for key, records in per_subject:
                repository.replace_for_subject(
                    session,
                    model_id=key.model_id,
                    runtime_profile_id=key.runtime_profile_id,
                    machine_id=key.machine_id,
                    policy_version=version,
                    rows=[record.row() for record in records],
                )
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise _translate(exc) from exc
    logger.info(
        "evidence.recomputed",
        extra={
            "subjects": len(subjects),
            "emitted": len(emitted),
            "withheld": len(withheld),
            "policy_version": version,
        },
    )
    return AggregationReport(
        computed_at=computed_at,
        policy_version=version,
        subjects=len(subjects),
        emitted=tuple(emitted),
        withheld=tuple(withheld),
        separated=tuple(dict.fromkeys(separated)),
        notes=tuple(dict.fromkeys(notes)),
    )


def subject_of_run(database: Database, run_id: str) -> Subject:
    """Return the measurement subject one run belongs to.

    Raises:
        NotFoundError: No run has this id.
    """
    from freeweight.infrastructure.db.repositories.runs import RunRepository

    with database.read() as session:
        run = RunRepository().get_by_id(session, run_id)
        if run is None:
            raise NotFoundError(f"No run matches {run_id!r}.", details={"run": run_id})
        return Subject(run.model_id, run.runtime_profile_id, run.machine_id)


def recompute_for_run(
    database: Database,
    run_id: str,
    *,
    settings: EvidenceSettings | None = None,
    clock: Clock = utc_now,
) -> AggregationReport:
    """Recompute the evidence of the subject one run measured.

    What the run engine calls when a run completes. Only that run's subject is recomputed, because
    a completed run changes exactly one subject's evidence and nothing else's.
    """
    return recompute_evidence(
        database, settings=settings, subject=subject_of_run(database, run_id), clock=clock
    )


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


def _record_from_row(session: Session, row: Any) -> EvidenceRecord:  # noqa: ANN401 — an ORM row
    """Rebuild an :class:`EvidenceRecord` from its stored row and the identity rows it points at."""
    from freeweight.infrastructure.db.models import Machine, ModelDescriptor, RuntimeProfile
    from freeweight.infrastructure.db.models_goals import Goal
    from freeweight.infrastructure.db.repositories.models import ModelRepository

    model = ModelRepository().get_by_id(session, row.model_id)
    machine = session.get(Machine, row.machine_id)
    profile = session.get(RuntimeProfile, row.runtime_profile_id)
    if model is None or machine is None or profile is None:  # pragma: no cover — RESTRICT keys
        raise DatabaseUnavailable(f"Evidence {row.id!r} points at identity rows that are missing.")
    descriptor = (
        session.get(ModelDescriptor, row.model_descriptor_id) if row.model_descriptor_id else None
    )
    goal = session.get(Goal, row.goal_id) if row.goal_id else None
    environment = Environment.from_json(
        dict(row.environment_snapshot_json)
        if isinstance(row.environment_snapshot_json, dict)
        else {},
        provider_kind=str(model.provider_kind),
    )
    contributing = tuple(
        ContributingMetric(
            metric_key=str(entry.get("metric_key", "")),
            weight=float(entry.get("weight", 0.0)),
            sample_count=int(entry.get("sample_count", 0)),
            contribution=float(entry.get("contribution", 0.0)),
        )
        for entry in (
            row.contributing_metrics_json if isinstance(row.contributing_metrics_json, list) else []
        )
        if isinstance(entry, dict)
    )
    capability = str(row.capability_id)
    slug = goal.slug if goal is not None else None
    if slug is None and capability.startswith(f"{_GOAL_ROOT}."):
        slug = capability[len(_GOAL_ROOT) + 1 :]
    return EvidenceRecord(
        id=row.id,
        model_id=model.id,
        model_canonical_id=model.canonical_id,
        identity_confidence=str(row.identity_confidence),
        model_payload=model_identity_payload(model, descriptor),
        model_descriptor_id=row.model_descriptor_id,
        runtime_profile_id=profile.id,
        runtime_profile_hash=profile.profile_hash,
        machine_id=machine.id,
        machine_fingerprint=machine.machine_fingerprint,
        capability_id=capability,
        score=float(row.score),
        confidence=float(row.confidence),
        sample_count=int(row.sample_count),
        excluded_count=int(row.excluded_count or 0),
        dispersion=row.dispersion,
        dispersion_unavailable_reason=row.dispersion_unavailable_reason,
        measured_at=row.measured_at,
        computed_at=row.computed_at,
        policy_version=str(row.policy_version),
        policy=dict(row.policy_json) if isinstance(row.policy_json, dict) else {},
        vocabulary_version=str(row.vocabulary_version),
        benchmark_versions=_text_mapping(row.benchmark_versions_json),
        dataset_hashes=_text_mapping(row.dataset_hashes_json),
        prompt_subset_hashes=_text_mapping(row.prompt_subset_hashes_json),
        contributing_metrics=contributing,
        source_run_ids=tuple(
            str(item)
            for item in (
                row.source_run_ids_json if isinstance(row.source_run_ids_json, list) else []
            )
        ),
        environment=environment,
        judge_validity_factor=float(row.judge_validity_factor),
        factors=(
            dict(row.confidence_factors_json)
            if isinstance(row.confidence_factors_json, dict)
            else {}
        ),
        goal_id=row.goal_id,
        goal_slug=slug,
        goal_hash=row.goal_hash,
        goal_pack_version=row.goal_pack_version,
        score_method_mix=(
            {str(k): float(v) for k, v in dict(row.score_method_mix_json).items()}
            if isinstance(row.score_method_mix_json, dict)
            else None
        ),
        judge_set=dict(row.judge_set_json) if isinstance(row.judge_set_json, dict) else None,
        calibration=dict(row.calibration_json) if isinstance(row.calibration_json, dict) else None,
    )


def _text_mapping(value: object) -> dict[str, str]:
    """Read a ``PortableJSON`` mapping column back as ``{str: str}``, tolerating ``NULL``."""
    return {str(k): str(v) for k, v in dict(value).items()} if isinstance(value, dict) else {}


def _resolve_model_id(session: Session, reference: str) -> str:
    """Resolve a model reference to a ``models.id``, or refuse by name."""
    from freeweight.infrastructure.db.repositories.models import ModelRepository

    repository = ModelRepository()
    candidates = repository.get_by_id_prefix(session, reference)
    if len(candidates) == 1:
        return str(candidates[0].id)
    if len(candidates) > 1:
        raise ValidationError(
            f"{reference!r} matches {len(candidates)} models; use more characters.",
            details={"model": reference, "candidates": [row.id for row in candidates]},
        )
    row = (
        repository.get_by_canonical_id(session, reference)
        or repository.get_by_id(session, reference)
        or repository.get_by_provider_model_name(session, reference)
    )
    if row is not None:
        return str(row.id)
    raise NotFoundError(f"No model matches {reference!r}.", details={"model": reference})


def _encode_cursor(record: EvidenceRecord) -> str:
    """Encode a record's sort key as an opaque cursor."""
    raw = json.dumps({"capability_id": record.capability_id, "id": record.id}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    """Decode a cursor, refusing one that was not issued here."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return str(body["capability_id"]), str(body["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError(
            "cursor was not issued by this endpoint.", details={"field": "cursor"}
        ) from exc


def _matching_records(session: Session, query: EvidenceQuery) -> list[EvidenceRecord]:
    """Every record the query selects, in the total order, before pagination."""
    from freeweight.infrastructure.db.models import RuntimeProfile
    from freeweight.infrastructure.db.repositories.machines import MachineRepository

    model_id = _resolve_model_id(session, query.model) if query.model else None
    machine_id: str | None = None
    if query.machine:
        machine = MachineRepository().get_by_fingerprint(session, query.machine)
        if machine is None:
            return []
        machine_id = machine.id
    profile_id: str | None = None
    if query.runtime_profile:
        from sqlalchemy import select

        profile = session.scalars(
            select(RuntimeProfile).where(RuntimeProfile.profile_hash == query.runtime_profile)
        ).first()
        if profile is None:
            return []
        profile_id = profile.id
    rows = EvidenceRepository().list_all(
        session,
        capability_id=query.capability,
        model_id=model_id,
        machine_id=machine_id,
        runtime_profile_id=profile_id,
        min_confidence=query.min_confidence,
        since=query.since,
    )
    return [_record_from_row(session, row) for row in rows]


def query_evidence(
    database: Database, query: EvidenceQuery, *, now: datetime | None = None, clock: Clock = utc_now
) -> EvidencePage:
    """Return one page of evidence records (API §6).

    The collection is bounded — at most one record per subject per capability per policy — so a
    page is cut from the ordered list in memory rather than by a keyset query. The cursor still
    encodes the total order ``(capability_id, id)``, so paging can neither skip nor repeat.

    Args:
        database: The application's database handle.
        query: The filters and the page.
        now: The ``generated_at`` every item envelope carries; injected for byte-identical tests.
        clock: Where ``now`` comes from when it is not given.

    Returns:
        The page.

    Raises:
        NotFoundError: ``query.model`` matches nothing.
        ValidationError: ``query.model`` is ambiguous, or the cursor was not issued here.
        DatabaseUnavailable: The database cannot be read.
    """
    generated_at = now if now is not None else clock()
    try:
        with database.read() as session:
            records = _matching_records(session, query)
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise _translate(exc) from exc
    if query.cursor:
        capability_id, last_id = _decode_cursor(query.cursor)
        records = [
            record
            for record in records
            if (record.capability_id, record.id) > (capability_id, last_id)
        ]
    limit = query.clamped_limit()
    page = records[:limit]
    has_more = len(records) > limit
    return EvidencePage(
        records=tuple(page),
        limit=limit,
        next_cursor=_encode_cursor(page[-1]) if has_more and page else None,
        has_more=has_more,
        generated_at=generated_at,
    )


def _source_id(session: Session) -> str:
    """Which FreeWeight instance produced a bundle: this machine, by fingerprint.

    The most recently seen machine row is the machine this process runs on, and its fingerprint
    is the one stable identity the database holds without writing anything on a read.
    """
    from freeweight.infrastructure.db.repositories.machines import MachineRepository

    machines = MachineRepository().list_all(session)
    if not machines:
        return "freeweight"
    newest = max(machines, key=lambda row: row.last_seen_at)
    return f"freeweight:{newest.machine_fingerprint}"


def evidence_bundle(
    database: Database, query: EvidenceQuery, *, now: datetime | None = None, clock: Clock = utc_now
) -> str:
    """Build one ``benchmark.evidence_bundle`` envelope for the query's selection.

    ``complete`` is ``True`` only when nothing narrows the selection — no ``since``, no filter —
    because only a complete bundle may let a consumer infer removals (ADR-0022 §5).

    Args:
        database: The application's database handle.
        query: The selection. ``limit`` and ``cursor`` are ignored: a bundle is one document.
        now: The envelope's ``generated_at``; injected for byte-identical reproduction.
        clock: Where ``now`` comes from when it is not given.

    Returns:
        Canonical JSON text of the envelope.

    Raises:
        NotFoundError: ``query.model`` matches nothing.
        ValidationError: ``query.model`` is ambiguous, or a stored record fails the contract.
    """
    from setspec.capability.v1 import EvidenceBundleOut

    generated_at = now if now is not None else clock()
    try:
        with database.read() as session:
            records = _matching_records(session, query)
            source_id = _source_id(session)
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise _translate(exc) from exc
    bundle = EvidenceBundleOut.model_validate(
        {
            "source_id": source_id,
            "complete": query.selects_everything,
            "evidence": [record.wire_payload().model_dump() for record in records],
        }
    )
    return dump_envelope(
        bundle,
        schema=BUNDLE_SCHEMA,
        version=BUNDLE_SCHEMA_VERSION,
        generator=_GENERATOR,
        generated_at=generated_at,
    )


def iter_evidence_export(
    database: Database, query: EvidenceQuery, *, now: datetime | None = None, clock: Clock = utc_now
) -> Iterator[str]:
    """Stream the bundle, for the two front ends that write it.

    One chunk: a bundle is bounded (one record per subject per capability) and the SetSpec
    container has to be validated whole, so there is nothing to gain from splitting it and a
    partial bundle would be a document that lies about ``complete``.
    """
    yield evidence_bundle(database, query, now=now, clock=clock)


def staleness_of(
    record: EvidenceRecord, *, now: datetime, policy: ConfidencePolicy | None = None
) -> Staleness:
    """ADR-0017's staleness verdict for one record, as of ``now``.

    Freshness is recomputed from ``measured_at`` because the stored factor was true at
    ``computed_at`` and has decayed since; drift is what was detected at computation.
    """
    policy = policy if policy is not None else ConfidencePolicy()
    half_life = float(record.factors.get("half_life_days") or policy.half_life_days(record.kind))
    freshness = freshness_factor(
        measured_at=record.measured_at,
        now=now,
        half_life_days=half_life,
        floor=policy.freshness_floor,
    )
    drift = tuple(str(item) for item in record.factors.get("drift", ()))
    stale = is_stale(freshness=freshness, drift=drift, policy=policy)
    age_days = max(0.0, (now - record.measured_at).total_seconds() / 86_400.0)
    reasons: list[str] = []
    if freshness < policy.stale_below:
        reasons.append(
            f"measured {age_days:.0f} days ago; freshness {freshness:.2f} is below "
            f"{policy.stale_below:.2f}."
        )
    if drift:
        reasons.append(f"environment drifted since measurement: {', '.join(drift)}.")
    return Staleness(
        stale=stale,
        freshness=freshness,
        age_days=age_days,
        drift=drift,
        reasons=tuple(reasons),
    )


def newest_evidence_ages(
    database: Database, *, now: datetime | None = None, clock: Clock = utc_now
) -> dict[str, float]:
    """The age in days of the newest evidence per capability — what ``health`` reports.

    Returns:
        ``{capability_id: age_days}`` over every stored record, empty when there is none.
    """
    instant = now if now is not None else clock()
    try:
        with database.read() as session:
            rows = EvidenceRepository().list_all(session)
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise _translate(exc) from exc
    newest: dict[str, datetime] = {}
    for row in rows:
        current = newest.get(str(row.capability_id))
        if current is None or row.measured_at > current:
            newest[str(row.capability_id)] = row.measured_at
    return {
        capability: max(0.0, (instant - measured).total_seconds() / 86_400.0)
        for capability, measured in sorted(newest.items())
    }
