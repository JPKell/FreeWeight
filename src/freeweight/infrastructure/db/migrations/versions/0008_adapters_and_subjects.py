"""adapters and subjects

Phase 15. A measurement subject gains an adapter axis
([ADR-0058](../../../../../../../docs/adr/0058-the-execution-subject-gains-an-adapter-axis.md)):
one new table, one new column on ``runs``, and two on ``capability_evidence``.

**The rule for every existing row: it is a base subject.** ``adapter_id`` is ``NULL`` on every run
and every evidence record that existed before this migration, because that is what they were —
measurements of bare weights, taken when nothing else was possible. ``subject_canonical_id`` is
backfilled from ``models.canonical_id``, which for a subject with no adapter is what
``MeasurementSubject.canonical_subject_id`` returns **byte for byte** (ADR-0058's additive claim).
No existing row changes meaning, and none becomes ambiguous.

``adapters`` is deliberately not a mirror of the operator's directory
([ADR-0061](../../../../../../../docs/adr/0061-the-adapter-registry-is-a-directory-and-a-manifest.md)):
the directory is the registry and FreeWeight only reads it, while this table records the subjects
FreeWeight's own measurements belong to and **outlives** the directory
([ADR-0080](../../../../../../../docs/adr/0080-a-persisted-decision-names-the-subject-by-reference-and-by-string.md)).
Both foreign keys are ``RESTRICT`` to match: deleting an adapter row that evidence points at would
strand that evidence's subject, so the database refuses it rather than the repository remembering
not to.

``adapter_id`` joins ``capability_evidence``'s uniqueness key. Without it a subject's evidence would
collide with its base's on the same capability and policy, which is exactly the mis-attribution
[ADR-0058 §4](../../../../../../../docs/adr/0058-the-execution-subject-gains-an-adapter-axis.md)
refuses.

**On SQLite this migration rebuilds ``runs`` and ``capability_evidence``**, because adding a foreign
key to an existing SQLite table is a copy-drop-rename. ``runs`` has six ``ON DELETE CASCADE``
children, so the rebuild would delete every measurement in a real database with foreign keys
enforced. ``env.py`` suspends enforcement for the migration run
([ADR-0082](../../../../../../../docs/adr/0082-a-migration-run-suspends-sqlite-foreign-key-enforcement.md));
this migration is the reason it has to.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-06 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import freeweight.infrastructure.db.types

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "adapters",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("artifact_sha256", sa.String(), nullable=False),
        sa.Column("artifact_path", sa.String(), nullable=False),
        sa.Column("source_sha256", sa.String(), nullable=True),
        sa.Column("base_model_name", sa.String(), nullable=False),
        sa.Column("base_artifact_digest", sa.String(), nullable=True),
        sa.Column("base_confidence", sa.String(), nullable=False),
        sa.Column(
            "declared_capabilities_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("data_classification", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column(
            "first_seen_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False
        ),
        sa.Column("last_seen_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "base_confidence IN ('digest', 'name_only')",
            name=op.f("ck_adapters_base_confidence"),
        ),
        sa.CheckConstraint(
            "data_classification IN ('public', 'internal', 'confidential', 'restricted')",
            name=op.f("ck_adapters_data_classification"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_adapters")),
    )
    op.create_index("ix_adapters_base_model_name", "adapters", ["base_model_name"], unique=False)
    op.create_index("ix_adapters_name", "adapters", ["name"], unique=False)
    op.create_index("uq_adapters_artifact_sha256", "adapters", ["artifact_sha256"], unique=True)

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("adapter_id", sa.String(length=26), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_runs_adapter_id_adapters"),
            "adapters",
            ["adapter_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_runs_adapter_id_created_at", ["adapter_id", "created_at"], unique=False
        )

    # Added nullable, backfilled, then made NOT NULL: an existing row's subject is its model's
    # canonical ID, which is what a subject with no adapter has always meant. Doing it in one step
    # with a server default would leave the default in the schema for ever.
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.add_column(sa.Column("adapter_id", sa.String(length=26), nullable=True))
        batch_op.add_column(sa.Column("subject_canonical_id", sa.String(), nullable=True))

    op.execute(
        "UPDATE capability_evidence "
        "SET subject_canonical_id = ("
        "  SELECT models.canonical_id FROM models WHERE models.id = capability_evidence.model_id"
        ") "
        "WHERE subject_canonical_id IS NULL"
    )

    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.alter_column("subject_canonical_id", existing_type=sa.String(), nullable=False)
        batch_op.create_foreign_key(
            batch_op.f("fk_capability_evidence_adapter_id_adapters"),
            "adapters",
            ["adapter_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.drop_constraint("uq_capability_evidence_subject_capability_policy", type_="unique")
        batch_op.create_unique_constraint(
            "uq_capability_evidence_subject_capability_policy",
            [
                "model_id",
                "adapter_id",
                "runtime_profile_id",
                "machine_id",
                "capability_id",
                "policy_version",
            ],
        )
        batch_op.create_index(
            "ix_capability_evidence_subject_canonical_id_capability_id",
            ["subject_canonical_id", "capability_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.drop_index("ix_capability_evidence_subject_canonical_id_capability_id")
        batch_op.drop_constraint("uq_capability_evidence_subject_capability_policy", type_="unique")
        batch_op.create_unique_constraint(
            "uq_capability_evidence_subject_capability_policy",
            [
                "model_id",
                "runtime_profile_id",
                "machine_id",
                "capability_id",
                "policy_version",
            ],
        )
        batch_op.drop_constraint(
            batch_op.f("fk_capability_evidence_adapter_id_adapters"), type_="foreignkey"
        )
        batch_op.drop_column("subject_canonical_id")
        batch_op.drop_column("adapter_id")

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index("ix_runs_adapter_id_created_at")
        batch_op.drop_constraint(batch_op.f("fk_runs_adapter_id_adapters"), type_="foreignkey")
        batch_op.drop_column("adapter_id")

    op.drop_index("uq_adapters_artifact_sha256", table_name="adapters")
    op.drop_index("ix_adapters_name", table_name="adapters")
    op.drop_index("ix_adapters_base_model_name", table_name="adapters")
    op.drop_table("adapters")
