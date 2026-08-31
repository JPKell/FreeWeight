"""freeweight.infrastructure.db.models_goals — the user-authored goal tables.

Phase 8A's four ([Data Model §2](../../../../../../docs/apps/freeweight/data-model.md)): ``goals``,
``goal_criteria``, ``goal_tasks`` and ``criterion_scores``, plus Phase 8B's four —
``calibration_samples``, ``calibration_grades``, ``calibration_reports`` and ``judge_verdicts``.
They live in their own module rather than in :mod:`freeweight.infrastructure.db.models_runs` purely
for file size; all three modules share one
:class:`~freeweight.infrastructure.db.base.Base`, and all three are imported by
:mod:`freeweight.services.database` and by Alembic's ``env.py`` so autogenerate's parity check sees
every table.

Four rules from the data model are enforced here rather than left to application code:

* **The pack on disk is the source of truth.** These rows are its loaded, validated projection, so
  ``goals`` carries ``pack_path`` and ``pack_sha256`` and everything below it cascades from the
  goal. Deleting a goal deletes its criteria, tasks, grades and reports; it does not touch a run.
* **A skipped criterion has ``raw_score = NULL``, never ``0``**
  ([ADR-0016](../../../../../../docs/adr/0016-unavailable-is-not-zero.md)). The check constraint
  ``ck_criterion_scores_score_null_unless_scored`` states it in DDL, so no future code path can
  quietly write a zero into one.
* **The user's grades are the most valuable rows in the database.** ``calibration_grades`` is
  ``UNIQUE (calibration_sample_id, goal_criterion_id)`` so a re-grade updates rather than
  duplicates, and it cascades from the sample so a deleted sample cannot leave an orphan grade
  that nobody can re-read.
* **A jury's dispersion is the measurement's error bar.** ``judge_verdicts`` keeps one row per
  juror per repetition, in full: averaging at write time would destroy the thing being
  characterized.

ORM models only. They never leave the repository layer (coding standards §4).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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

from freeweight.infrastructure.db import models as _models  # noqa: F401 — see below
from freeweight.infrastructure.db import models_runs as _models_runs  # noqa: F401 — see below
from freeweight.infrastructure.db.base import Base, utcnow

# The two sibling model modules are imported for their side effect of registering ``models``,
# ``samples`` and the rest on the shared ``Base.metadata``. Three tables here point at them —
# ``criterion_scores.sample_id``, ``calibration_samples.source_sample_id`` and
# ``calibration_samples.model_id`` — and SQLAlchemy resolves a foreign key's target by *name* at
# flush time. A process that imported only this module would therefore fail its first write with
# ``NoReferencedTableError`` rather than at import, which is the worst possible moment to find out.
# Alembic's ``env.py`` imports all three for the same reason; this makes the dependency the
# module's own rather than every caller's.

__all__ = [
    "CalibrationGrade",
    "CalibrationReport",
    "CalibrationSample",
    "CriterionScore",
    "Goal",
    "GoalCriterion",
    "GoalTaskRow",
    "JudgeVerdict",
    "WizardDraft",
]

_RUNGS = ("rule", "reference", "human", "judge")
_CRITERION_STATUSES = ("scored", "skipped", "error")
_PARTITIONS = ("anchor", "holdout")
_SAMPLE_ORIGINS = ("generated", "pasted", "imported_run_sample")


def _in_list(column: str, allowed: tuple[str, ...]) -> str:
    """Render ``column IN ('a', 'b')`` for a ``CheckConstraint``.

    The same helper and the same reasoning as its two siblings in
    :mod:`freeweight.infrastructure.db.models` and
    :mod:`freeweight.infrastructure.db.models_runs`: a tuple's ``repr`` is valid SQL only by
    coincidence, and stops being valid for a one-element tuple.
    """
    if any("'" in value for value in allowed):
        raise ValueError(f"CheckConstraint values must not contain a quote: {allowed!r}")
    rendered = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({rendered})"


class Goal(Base):
    """One user-authored measurement definition, as loaded from its pack.

    ``capability_id`` is always ``user.<slug>`` (ADR-0032 §1) and is stored rather than derived so
    a query for "which goal produced this evidence" is a join rather than a string operation.

    ``goal_hash`` separates results exactly as a benchmark version does, and it is on this row as
    well as on ``benchmark_suites`` because the suite row is created per goal *version* while this
    row tracks the goal as the user currently has it.
    """

    __tablename__ = "goals"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_goals_slug"),
        Index("ix_goals_capability_id", "capability_id"),
    )

    id: Mapped[str] = ulid_primary_key()
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    intent: Mapped[str | None] = mapped_column(Text)
    goal_pack_version: Mapped[str] = mapped_column(String, nullable=False)
    goal_hash: Mapped[str] = mapped_column(String, nullable=False)
    contributes_to: Mapped[str | None] = mapped_column(String)
    capability_id: Mapped[str] = mapped_column(String, nullable=False)
    judge_config_json: Mapped[object | None] = mapped_column(PortableJSON)
    calibration_config_json: Mapped[object | None] = mapped_column(PortableJSON)
    pack_path: Mapped[str] = mapped_column(String, nullable=False)
    pack_sha256: Mapped[str] = mapped_column(String, nullable=False)
    forked_from: Mapped[str | None] = mapped_column(String)
    unforked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    imported_from_json: Mapped[object | None] = mapped_column(PortableJSON)
    created_by: Mapped[str | None] = mapped_column(String)
    lint_json: Mapped[object | None] = mapped_column(PortableJSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class GoalCriterion(Base):
    """One criterion of one goal.

    ``rung = 'judge'`` with no ``scale_descriptors_json`` fails validation before this row is ever
    written: an unanchored ordinal scale reliably produces ``kappa_w`` near zero, so it is refused
    at authoring time rather than discovered after twelve samples have been graded. The rule is in
    :mod:`freeweight.domain.goals.lint` because it must be reported *with every other problem the
    pack has*, and a DDL constraint would report it alone.
    """

    __tablename__ = "goal_criteria"
    __table_args__ = (
        CheckConstraint(_in_list("rung", _RUNGS), name="rung"),
        UniqueConstraint("goal_id", "key", name="uq_goal_criteria_goal_id_key"),
    )

    id: Mapped[str] = ulid_primary_key()
    goal_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    rung: Mapped[str] = mapped_column(String, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    is_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rule_json: Mapped[object | None] = mapped_column(PortableJSON)
    scale_points: Mapped[int | None] = mapped_column(Integer)
    scale_descriptors_json: Mapped[object | None] = mapped_column(PortableJSON)
    mode: Mapped[str | None] = mapped_column(String)
    lint_json: Mapped[object | None] = mapped_column(PortableJSON)


class GoalTaskRow(Base):
    """One task a candidate answers.

    Named ``GoalTaskRow`` rather than ``GoalTask`` so it cannot be confused with
    :class:`freeweight.domain.goals.pack.GoalTask`, the domain value object. They are different
    things on purpose: the domain object carries the *rendered* prompt, this row records that the
    task existed and which prompt record produced it.
    """

    __tablename__ = "goal_tasks"
    __table_args__ = (UniqueConstraint("goal_id", "key", name="uq_goal_tasks_goal_id_key"),)

    id: Mapped[str] = ulid_primary_key()
    goal_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_id: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String, nullable=False)
    rendered_prompt_hash: Mapped[str | None] = mapped_column(String)
    source_json: Mapped[object | None] = mapped_column(PortableJSON)
    is_starter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CriterionScore(Base):
    """One criterion's verdict on one sample. This is what a goal's headline number drills to.

    ``raw_score`` is ``NULL`` for anything that is not ``scored``, and the check constraint says
    so: a skipped criterion is excluded from the composite with the exclusion visible in the
    weight actually applied, and a zero written here would be a claim about something the run
    never observed (ADR-0016).
    """

    __tablename__ = "criterion_scores"
    __table_args__ = (
        CheckConstraint(_in_list("status", _CRITERION_STATUSES), name="status"),
        CheckConstraint("status = 'scored' OR raw_score IS NULL", name="score_null_unless_scored"),
        UniqueConstraint(
            "sample_id", "goal_criterion_id", name="uq_criterion_scores_sample_id_criterion_id"
        ),
        Index("ix_criterion_scores_goal_criterion_id", "goal_criterion_id"),
    )

    id: Mapped[str] = ulid_primary_key()
    sample_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("samples.id", ondelete="CASCADE"), nullable=False
    )
    goal_criterion_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("goal_criteria.id", ondelete="CASCADE"), nullable=False
    )
    criterion_key: Mapped[str] = mapped_column(String, nullable=False)
    rung: Mapped[str] = mapped_column(String, nullable=False)
    raw_score: Mapped[float | None] = mapped_column(Float)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    gated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String)
    detail_json: Mapped[object | None] = mapped_column(PortableJSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class CalibrationSample(Base):
    """One candidate output presented to the user for grading.

    Content is always stored, unlike a benchmark sample's response: a judged score the grader
    cannot re-read is not auditable, and the whole feature rests on the grader being able to look
    again at what they graded (data model §2).
    """

    __tablename__ = "calibration_samples"
    __table_args__ = (
        CheckConstraint(_in_list("partition", _PARTITIONS), name="partition"),
        CheckConstraint(_in_list("origin", _SAMPLE_ORIGINS), name="origin"),
        Index("ix_calibration_samples_goal_id_partition", "goal_id", "partition"),
    )

    id: Mapped[str] = ulid_primary_key()
    goal_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    goal_task_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("goal_tasks.id", ondelete="SET NULL")
    )
    origin: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="SET NULL")
    )
    source_sample_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("samples.id", ondelete="SET NULL")
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String, nullable=False)
    partition: Mapped[str] = mapped_column(String, nullable=False)
    partition_seed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class CalibrationGrade(Base):
    """The user's own grade for one sample on one criterion. The ground truth of the feature."""

    __tablename__ = "calibration_grades"
    __table_args__ = (
        UniqueConstraint(
            "calibration_sample_id",
            "goal_criterion_id",
            name="uq_calibration_grades_sample_id_criterion_id",
        ),
    )

    id: Mapped[str] = ulid_primary_key()
    calibration_sample_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("calibration_samples.id", ondelete="CASCADE"), nullable=False
    )
    goal_criterion_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("goal_criteria.id", ondelete="CASCADE"), nullable=False
    )
    grade: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    graded_by: Mapped[str] = mapped_column(String, nullable=False)
    graded_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class CalibrationReport(Base):
    """One criterion's measured agreement, or — with ``goal_criterion_id`` ``NULL`` — the goal's.

    Ages like evidence: ``measured_at`` is what staleness decays from, and ``<app> health``
    reports it.
    """

    __tablename__ = "calibration_reports"
    __table_args__ = (
        Index("ix_calibration_reports_goal_id_measured_at", "goal_id", "measured_at"),
    )

    id: Mapped[str] = ulid_primary_key()
    goal_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("goals.id", ondelete="CASCADE"), nullable=False
    )
    goal_criterion_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("goal_criteria.id", ondelete="CASCADE")
    )
    goal_hash: Mapped[str] = mapped_column(String, nullable=False)
    judge_set_json: Mapped[object | None] = mapped_column(PortableJSON)
    kappa_w: Mapped[float | None] = mapped_column(Float)
    rho: Mapped[float | None] = mapped_column(Float)
    mae: Mapped[float | None] = mapped_column(Float)
    bias: Mapped[float | None] = mapped_column(Float)
    n_anchor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_holdout: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inter_juror_alpha: Mapped[float | None] = mapped_column(Float)
    passed_gate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    min_agreement: Mapped[float] = mapped_column(Float, nullable=False, default=0.4)
    judge_validity_factor: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    disagreement_json: Mapped[object | None] = mapped_column(PortableJSON)
    graded_by: Mapped[str | None] = mapped_column(String)
    measured_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    policy_version: Mapped[str] = mapped_column(String, nullable=False)


class JudgeVerdict(Base):
    """One juror's grade for one criterion on one sample, for one repetition.

    Retained in full. The jury's dispersion *is* the measurement's error bar, and averaging it at
    write time would destroy the thing being characterized.
    """

    __tablename__ = "judge_verdicts"
    __table_args__ = (
        UniqueConstraint(
            "criterion_score_id",
            "juror_ordinal",
            "repetition",
            name="uq_judge_verdicts_score_id_juror_repetition",
        ),
    )

    id: Mapped[str] = ulid_primary_key()
    criterion_score_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("criterion_scores.id", ondelete="CASCADE"), nullable=False
    )
    juror_model_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="SET NULL")
    )
    juror_canonical_id: Mapped[str] = mapped_column(String, nullable=False)
    juror_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)
    grade: Mapped[int | None] = mapped_column(Integer)
    pairwise_choice: Mapped[str | None] = mapped_column(String)
    presentation_order: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    rationale_sha256: Mapped[str | None] = mapped_column(String)
    prompt_id: Mapped[str | None] = mapped_column(String)
    prompt_version: Mapped[str | None] = mapped_column(String)
    judge_prompt_sha256: Mapped[str | None] = mapped_column(String)
    remote: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    input_tokens: Mapped[float | None] = mapped_column(Float)
    output_tokens: Mapped[float | None] = mapped_column(Float)
    refused_reason: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class WizardDraft(Base):
    """A goal-authoring session between steps 1 and 4, before any pack has been written.

    Its own table rather than a key in ``settings``, which is where it used to live. A draft is
    user data with a lifecycle — created, edited over minutes or days, then deleted when its pack
    is written or abandoned — and none of that was expressible in a key-value settings store: no
    expiry, no index, and ``db status`` counted drafts as settings.

    ``expires_at`` is what makes an abandoned draft disappear rather than accumulate. It is a
    column rather than a sweep because the read path already has to decide whether a draft is
    still live, and a draft past its expiry is *gone* whether or not anything has collected it
    yet — which is the same rule :meth:`load_draft` applies.
    """

    __tablename__ = "wizard_drafts"
    __table_args__ = (Index("ix_wizard_drafts_expires_at", "expires_at"),)

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    slug: Mapped[str | None] = mapped_column(String)
    body_json: Mapped[object] = mapped_column(PortableJSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
