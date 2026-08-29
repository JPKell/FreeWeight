"""freeweight.infrastructure.db.models_evidence — Phase 11's table: ``capability_evidence``.

One table, per [Data Model §2](../../../../../../docs/apps/freeweight/data-model.md) and the
normative field set of
[ADR-0022 §1](../../../../../../docs/adr/0022-capability-evidence-record-contract.md), split from
:mod:`freeweight.infrastructure.db.models_runs` for the same reason that module is split from
:mod:`freeweight.infrastructure.db.models`: file size. All three share one
:class:`~freeweight.infrastructure.db.base.Base` and all three are imported by
:mod:`freeweight.services.database`, so Alembic's parity check sees every table.

Three rules the data model states are enforced here rather than left to application code:

* **Evidence never deletes identity.** ``model_id``, ``runtime_profile_id`` and ``machine_id`` are
  ``ON DELETE RESTRICT``: a result deletion never takes the model, the profile or the machine with
  it, and an evidence row that outlived its subject would be a claim about nothing.
* **Two policy versions coexist.** ``UNIQUE (model_id, runtime_profile_id, machine_id,
  capability_id, policy_version)`` — ADR-0022 §3's producer key, with ``policy_version`` in it so a
  recomputation under a new policy is a new row beside the old one rather than an overwrite, and a
  re-import on the consumer's side is a row-wise upsert.
* **A goal below its gate writes no row here.** There is no column for "uncalibrated", because an
  uncalibrated goal emits nothing at all (ADR-0032 §3); the absence is what the gate produces, and
  a test asserts the absence directly.

ORM models only. They never leave the repository layer (coding standards §4).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from freeweight.infrastructure.db.base import Base, utcnow
from freeweight.infrastructure.db.types import PortableJSON, UtcDateTime, ulid_primary_key

__all__ = ["CapabilityEvidence"]

_IDENTITY_CONFIDENCES = ("digest", "name_only")


class CapabilityEvidence(Base):
    """One capability score for one measurement subject, with its confidence and its provenance.

    A *subject* is a model under a runtime profile on a machine (ADR-0023): the three foreign keys
    plus ``capability_id`` and ``policy_version`` are the row's natural key. Every other column is
    either a field of ``capability.evidence`` v1 stored in its own name, or one of three internal
    columns the wire contract does not carry: ``confidence_factors_json`` (the six-factor
    breakdown, so the UI can explain the number without recomputing it), ``policy_json`` (the
    parameters the number was computed under — ADR-0017 requires every one to be recorded with the
    evidence), and ``goal_id`` (so "which goal produced this evidence" is a join rather than a
    string operation on ``capability_id``).

    ``dispersion`` follows the measurement-column rule (database standards §3): ``NULL`` **plus**
    ``dispersion_unavailable_reason``, so "not computed" and "undefined for one sample" are
    different rows rather than the same ``NULL``.

    ``measured_at`` is the latest ``completed_at`` among the contributing runs and is what
    freshness decays from; ``computed_at`` is when the aggregation ran and is the ``since`` filter
    for incremental export. Recomputing evidence over unchanged runs changes the second and never
    the first (ADR-0022 §2), and a test asserts it.
    """

    __tablename__ = "capability_evidence"
    __table_args__ = (
        CheckConstraint(
            f"identity_confidence IN ({', '.join(repr(v) for v in _IDENTITY_CONFIDENCES)})",
            name="identity_confidence",
        ),
        CheckConstraint("score >= 0.0 AND score <= 1.0", name="score_range"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
        CheckConstraint("measured_at <= computed_at", name="measured_before_computed"),
        UniqueConstraint(
            "model_id",
            "runtime_profile_id",
            "machine_id",
            "capability_id",
            "policy_version",
            name="uq_capability_evidence_subject_capability_policy",
        ),
        Index("ix_capability_evidence_capability_id_score", "capability_id", "score"),
        Index("ix_capability_evidence_model_id_capability_id", "model_id", "capability_id"),
        Index("ix_capability_evidence_computed_at", "computed_at"),
        Index("ix_capability_evidence_runtime_profile_id", "runtime_profile_id"),
        Index("ix_capability_evidence_machine_id", "machine_id"),
        Index("ix_capability_evidence_goal_id", "goal_id"),
    )

    id: Mapped[str] = ulid_primary_key()
    model_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
    )
    runtime_profile_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("runtime_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    machine_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("machines.id", ondelete="RESTRICT"), nullable=False
    )
    model_descriptor_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("model_descriptors.id", ondelete="SET NULL")
    )
    capability_id: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispersion: Mapped[float | None] = mapped_column(Float)
    dispersion_unavailable_reason: Mapped[str | None] = mapped_column(String)
    identity_confidence: Mapped[str] = mapped_column(String, nullable=False)
    source_run_ids_json: Mapped[object | None] = mapped_column(PortableJSON)
    contributing_metrics_json: Mapped[object | None] = mapped_column(PortableJSON)
    benchmark_versions_json: Mapped[object | None] = mapped_column(PortableJSON)
    dataset_hashes_json: Mapped[object | None] = mapped_column(PortableJSON)
    prompt_subset_hashes_json: Mapped[object | None] = mapped_column(PortableJSON)
    environment_snapshot_json: Mapped[object | None] = mapped_column(PortableJSON)
    measured_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    policy_version: Mapped[str] = mapped_column(String, nullable=False)
    policy_json: Mapped[object | None] = mapped_column(PortableJSON)
    vocabulary_version: Mapped[str] = mapped_column(String, nullable=False)
    judge_validity_factor: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    confidence_factors_json: Mapped[object | None] = mapped_column(PortableJSON)
    goal_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("goals.id", ondelete="SET NULL")
    )
    goal_hash: Mapped[str | None] = mapped_column(String)
    goal_pack_version: Mapped[str | None] = mapped_column(String)
    score_method_mix_json: Mapped[object | None] = mapped_column(PortableJSON)
    judge_set_json: Mapped[object | None] = mapped_column(PortableJSON)
    calibration_json: Mapped[object | None] = mapped_column(PortableJSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
