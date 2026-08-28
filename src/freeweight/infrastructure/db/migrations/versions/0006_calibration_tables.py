"""calibration tables

Data model §2's four calibration tables: ``calibration_samples``, ``calibration_grades``,
``calibration_reports`` and ``judge_verdicts``
([ADR-0031 §3](../../../../../../../docs/adr/0031-user-defined-goal-benchmarks.md),
[ADR-0032](../../../../../../../docs/adr/0032-judge-validity-and-user-capability-namespace.md)).

Separate from ``0005`` because they are separate work: a goal whose criteria are entirely rules
never writes a row in any of them, and Phase 8A ships a fully usable goal runner without them.

``calibration_grades`` holds the user's own grades and is the most valuable table in this schema —
they are the ground truth of the whole feature, they were expensive to produce, and nothing else
can reconstruct them. ``downgrade`` drops them, which is why the pack on disk carries a copy.

``judge_verdicts`` keeps one row per juror per repetition rather than a mean: the jury's dispersion
*is* the measurement's error bar.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-27 14:03:40.288112
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import freeweight.infrastructure.db.types

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "calibration_samples",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("goal_id", sa.String(length=26), nullable=False),
        sa.Column("goal_task_id", sa.String(length=26), nullable=True),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(length=26), nullable=True),
        sa.Column("source_sample_id", sa.String(length=26), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(), nullable=False),
        sa.Column("partition", sa.String(), nullable=False),
        sa.Column("partition_seed", sa.Integer(), nullable=False),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "origin IN ('generated', 'pasted', 'imported_run_sample')",
            name=op.f("ck_calibration_samples_origin"),
        ),
        sa.CheckConstraint(
            "partition IN ('anchor', 'holdout')", name=op.f("ck_calibration_samples_partition")
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["goals.id"],
            name=op.f("fk_calibration_samples_goal_id_goals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["goal_task_id"],
            ["goal_tasks.id"],
            name=op.f("fk_calibration_samples_goal_task_id_goal_tasks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_calibration_samples_model_id_models"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_sample_id"],
            ["samples.id"],
            name=op.f("fk_calibration_samples_source_sample_id_samples"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calibration_samples")),
    )

    with op.batch_alter_table("calibration_samples", schema=None) as batch_op:
        batch_op.create_index(
            "ix_calibration_samples_goal_id_partition", ["goal_id", "partition"], unique=False
        )

    op.create_table(
        "calibration_grades",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("calibration_sample_id", sa.String(length=26), nullable=False),
        sa.Column("goal_criterion_id", sa.String(length=26), nullable=False),
        sa.Column("grade", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("graded_by", sa.String(), nullable=False),
        sa.Column("graded_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["calibration_sample_id"],
            ["calibration_samples.id"],
            name=op.f("fk_calibration_grades_calibration_sample_id_calibration_samples"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["goal_criterion_id"],
            ["goal_criteria.id"],
            name=op.f("fk_calibration_grades_goal_criterion_id_goal_criteria"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calibration_grades")),
        sa.UniqueConstraint(
            "calibration_sample_id",
            "goal_criterion_id",
            name="uq_calibration_grades_sample_id_criterion_id",
        ),
    )

    op.create_table(
        "calibration_reports",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("goal_id", sa.String(length=26), nullable=False),
        sa.Column("goal_criterion_id", sa.String(length=26), nullable=True),
        sa.Column("goal_hash", sa.String(), nullable=False),
        sa.Column(
            "judge_set_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column("kappa_w", sa.Float(), nullable=True),
        sa.Column("rho", sa.Float(), nullable=True),
        sa.Column("mae", sa.Float(), nullable=True),
        sa.Column("bias", sa.Float(), nullable=True),
        sa.Column("n_anchor", sa.Integer(), nullable=False),
        sa.Column("n_holdout", sa.Integer(), nullable=False),
        sa.Column("inter_juror_alpha", sa.Float(), nullable=True),
        sa.Column("passed_gate", sa.Boolean(), nullable=False),
        sa.Column("min_agreement", sa.Float(), nullable=False),
        sa.Column("judge_validity_factor", sa.Float(), nullable=False),
        sa.Column(
            "disagreement_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column("graded_by", sa.String(), nullable=True),
        sa.Column("measured_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["goal_criterion_id"],
            ["goal_criteria.id"],
            name=op.f("fk_calibration_reports_goal_criterion_id_goal_criteria"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["goals.id"],
            name=op.f("fk_calibration_reports_goal_id_goals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calibration_reports")),
    )

    with op.batch_alter_table("calibration_reports", schema=None) as batch_op:
        batch_op.create_index(
            "ix_calibration_reports_goal_id_measured_at", ["goal_id", "measured_at"], unique=False
        )

    op.create_table(
        "judge_verdicts",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("criterion_score_id", sa.String(length=26), nullable=False),
        sa.Column("juror_model_id", sa.String(length=26), nullable=True),
        sa.Column("juror_canonical_id", sa.String(), nullable=False),
        sa.Column("juror_ordinal", sa.Integer(), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("grade", sa.Integer(), nullable=True),
        sa.Column("pairwise_choice", sa.String(), nullable=True),
        sa.Column("presentation_order", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("rationale_sha256", sa.String(), nullable=True),
        sa.Column("prompt_id", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("judge_prompt_sha256", sa.String(), nullable=True),
        sa.Column("remote", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("input_tokens", sa.Float(), nullable=True),
        sa.Column("output_tokens", sa.Float(), nullable=True),
        sa.Column("refused_reason", sa.String(), nullable=True),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["criterion_score_id"],
            ["criterion_scores.id"],
            name=op.f("fk_judge_verdicts_criterion_score_id_criterion_scores"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["juror_model_id"],
            ["models.id"],
            name=op.f("fk_judge_verdicts_juror_model_id_models"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_judge_verdicts")),
        sa.UniqueConstraint(
            "criterion_score_id",
            "juror_ordinal",
            "repetition",
            name="uq_judge_verdicts_score_id_juror_repetition",
        ),
    )


def downgrade() -> None:
    op.drop_table("judge_verdicts")
    with op.batch_alter_table("calibration_reports", schema=None) as batch_op:
        batch_op.drop_index("ix_calibration_reports_goal_id_measured_at")
    op.drop_table("calibration_reports")
    op.drop_table("calibration_grades")
    with op.batch_alter_table("calibration_samples", schema=None) as batch_op:
        batch_op.drop_index("ix_calibration_samples_goal_id_partition")
    op.drop_table("calibration_samples")
