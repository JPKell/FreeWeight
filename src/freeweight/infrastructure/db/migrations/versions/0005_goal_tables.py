"""goal tables

Data model §2's four goal tables, unreachable until Phase 8A gave the application a runner kind
that reads them: ``goals``, ``goal_criteria``, ``goal_tasks`` and ``criterion_scores``
([ADR-0031](../../../../../../../docs/adr/0031-user-defined-goal-benchmarks.md)).

Additive only — no table from ``0001``–``0004`` is altered, so upgrading a populated Phase 7
database touches no existing row.

``benchmark_suites.goal_id`` and ``goal_hash`` already exist as plain columns (Phase 5 declared
them). They deliberately gain **no** foreign key here. Adding one on SQLite means recreating
``benchmark_suites``, which ``runs.suite_id`` references with ``ON DELETE RESTRICT``; recreating a
table that a populated ``runs`` points at is a materially riskier operation than a nullable
constraint is worth, and the only writer of a goal-backed suite row is
:mod:`freeweight.services.goals`, which has the goal row in hand when it writes one.

``downgrade`` drops all four tables. That destroys the user's goal definitions *as loaded*; the
packs themselves are files on disk under ``$XDG_CONFIG_HOME/freeweight/goals/`` and are the source
of truth, so a downgrade followed by an upgrade re-imports them.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-27 14:03:40.288112
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import freeweight.infrastructure.db.types

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("goal_pack_version", sa.String(), nullable=False),
        sa.Column("goal_hash", sa.String(), nullable=False),
        sa.Column("contributes_to", sa.String(), nullable=True),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column(
            "judge_config_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column(
            "calibration_config_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("pack_path", sa.String(), nullable=False),
        sa.Column("pack_sha256", sa.String(), nullable=False),
        sa.Column("forked_from", sa.String(), nullable=True),
        sa.Column("unforked", sa.Boolean(), nullable=False),
        sa.Column(
            "imported_from_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("lint_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("updated_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goals")),
        sa.UniqueConstraint("slug", name="uq_goals_slug"),
    )

    with op.batch_alter_table("goals", schema=None) as batch_op:
        batch_op.create_index("ix_goals_capability_id", ["capability_id"], unique=False)

    op.create_table(
        "goal_criteria",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("goal_id", sa.String(length=26), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("rung", sa.String(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("is_gate", sa.Boolean(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("rule_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("scale_points", sa.Integer(), nullable=True),
        sa.Column(
            "scale_descriptors_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("lint_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.CheckConstraint(
            "rung IN ('rule', 'reference', 'human', 'judge')", name=op.f("ck_goal_criteria_rung")
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["goals.id"],
            name=op.f("fk_goal_criteria_goal_id_goals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_criteria")),
        sa.UniqueConstraint("goal_id", "key", name="uq_goal_criteria_goal_id_key"),
    )

    op.create_table(
        "goal_tasks",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("goal_id", sa.String(length=26), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("prompt_id", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("prompt_sha256", sa.String(), nullable=False),
        sa.Column("rendered_prompt_hash", sa.String(), nullable=True),
        sa.Column("source_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("is_starter", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name=op.f("fk_goal_tasks_goal_id_goals"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_tasks")),
        sa.UniqueConstraint("goal_id", "key", name="uq_goal_tasks_goal_id_key"),
    )

    op.create_table(
        "criterion_scores",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("sample_id", sa.String(length=26), nullable=False),
        sa.Column("goal_criterion_id", sa.String(length=26), nullable=False),
        sa.Column("criterion_key", sa.String(), nullable=False),
        sa.Column("rung", sa.String(), nullable=False),
        sa.Column("raw_score", sa.Float(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("gated", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("skip_reason", sa.String(), nullable=True),
        sa.Column("detail_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "status = 'scored' OR raw_score IS NULL",
            name=op.f("ck_criterion_scores_score_null_unless_scored"),
        ),
        sa.CheckConstraint(
            "status IN ('scored', 'skipped', 'error')", name=op.f("ck_criterion_scores_status")
        ),
        sa.ForeignKeyConstraint(
            ["goal_criterion_id"],
            ["goal_criteria.id"],
            name=op.f("fk_criterion_scores_goal_criterion_id_goal_criteria"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sample_id"],
            ["samples.id"],
            name=op.f("fk_criterion_scores_sample_id_samples"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_criterion_scores")),
        sa.UniqueConstraint(
            "sample_id", "goal_criterion_id", name="uq_criterion_scores_sample_id_criterion_id"
        ),
    )

    with op.batch_alter_table("criterion_scores", schema=None) as batch_op:
        batch_op.create_index(
            "ix_criterion_scores_goal_criterion_id", ["goal_criterion_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("criterion_scores", schema=None) as batch_op:
        batch_op.drop_index("ix_criterion_scores_goal_criterion_id")
    op.drop_table("criterion_scores")
    op.drop_table("goal_tasks")
    op.drop_table("goal_criteria")
    with op.batch_alter_table("goals", schema=None) as batch_op:
        batch_op.drop_index("ix_goals_capability_id")
    op.drop_table("goals")
