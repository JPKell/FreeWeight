"""capability evidence

Data model §2's ``capability_evidence`` table: the aggregated, exportable evidence LoadCoach
consumes, with the normative field set of
[ADR-0022 §1](../../../../../../../docs/adr/0022-capability-evidence-record-contract.md) and the
goal-sourced group of
[ADR-0032 §5](../../../../../../../docs/adr/0032-judge-validity-and-user-capability-namespace.md).

A new revision rather than a column folded into an earlier one: nothing that existed before
Phase 11 owns this table, so there is no revision it belongs inside.

The unique constraint carries ``policy_version`` (ADR-0022 §3) so a recomputation under a new
policy sits beside the old one rather than replacing it. Every identity foreign key is
``RESTRICT``: a result deletion never removes a model, a profile or a machine, and evidence
pointing at one would otherwise be the only thing keeping it.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-28 15:12:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import freeweight.infrastructure.db.types

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "capability_evidence",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column("runtime_profile_id", sa.String(length=26), nullable=False),
        sa.Column("machine_id", sa.String(length=26), nullable=False),
        sa.Column("model_descriptor_id", sa.String(length=26), nullable=True),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("dispersion", sa.Float(), nullable=True),
        sa.Column("dispersion_unavailable_reason", sa.String(), nullable=True),
        sa.Column("identity_confidence", sa.String(), nullable=False),
        sa.Column(
            "source_run_ids_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column(
            "contributing_metrics_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column(
            "benchmark_versions_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column(
            "dataset_hashes_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column(
            "prompt_subset_hashes_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column(
            "environment_snapshot_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("measured_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("computed_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("policy_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("vocabulary_version", sa.String(), nullable=False),
        sa.Column("judge_validity_factor", sa.Float(), nullable=False),
        sa.Column(
            "confidence_factors_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("goal_id", sa.String(length=26), nullable=True),
        sa.Column("goal_hash", sa.String(), nullable=True),
        sa.Column("goal_pack_version", sa.String(), nullable=True),
        sa.Column(
            "score_method_mix_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column(
            "judge_set_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column(
            "calibration_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name=op.f("ck_capability_evidence_confidence_range"),
        ),
        sa.CheckConstraint(
            "identity_confidence IN ('digest', 'name_only')",
            name=op.f("ck_capability_evidence_identity_confidence"),
        ),
        sa.CheckConstraint(
            "measured_at <= computed_at",
            name=op.f("ck_capability_evidence_measured_before_computed"),
        ),
        sa.CheckConstraint(
            "score >= 0.0 AND score <= 1.0", name=op.f("ck_capability_evidence_score_range")
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["goals.id"],
            name=op.f("fk_capability_evidence_goal_id_goals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["machine_id"],
            ["machines.id"],
            name=op.f("fk_capability_evidence_machine_id_machines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_descriptor_id"],
            ["model_descriptors.id"],
            name=op.f("fk_capability_evidence_model_descriptor_id_model_descriptors"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_capability_evidence_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_profile_id"],
            ["runtime_profiles.id"],
            name=op.f("fk_capability_evidence_runtime_profile_id_runtime_profiles"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_evidence")),
        sa.UniqueConstraint(
            "model_id",
            "runtime_profile_id",
            "machine_id",
            "capability_id",
            "policy_version",
            name="uq_capability_evidence_subject_capability_policy",
        ),
    )
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.create_index(
            "ix_capability_evidence_capability_id_score", ["capability_id", "score"], unique=False
        )
        batch_op.create_index("ix_capability_evidence_computed_at", ["computed_at"], unique=False)
        batch_op.create_index("ix_capability_evidence_goal_id", ["goal_id"], unique=False)
        batch_op.create_index("ix_capability_evidence_machine_id", ["machine_id"], unique=False)
        batch_op.create_index(
            "ix_capability_evidence_model_id_capability_id",
            ["model_id", "capability_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_capability_evidence_runtime_profile_id", ["runtime_profile_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.drop_index("ix_capability_evidence_runtime_profile_id")
        batch_op.drop_index("ix_capability_evidence_model_id_capability_id")
        batch_op.drop_index("ix_capability_evidence_machine_id")
        batch_op.drop_index("ix_capability_evidence_goal_id")
        batch_op.drop_index("ix_capability_evidence_computed_at")
        batch_op.drop_index("ix_capability_evidence_capability_id_score")
    op.drop_table("capability_evidence")
