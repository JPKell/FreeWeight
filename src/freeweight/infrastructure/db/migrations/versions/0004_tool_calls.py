"""tool_calls

Data model §2's ``tool_calls``, specified since the freeze and unreachable until Phase 7 gave the
application suites that call tools. One row per invocation a model requested, so a tool metric
drills to the call that went wrong rather than to a rate
([ADR-0033](../../../../../../../docs/adr/0033-benchmark-interaction-protocol.md)).

Additive only — no table from ``0001``–``0003`` is altered, so upgrading a populated Phase 6
database touches no existing row. ``downgrade`` drops the table; that loses tool trajectories and
only tool trajectories. It is allowed to be silent because no run, sample or metric row has a
foreign key into it, and every sample keeps its own copy of the trajectory in ``result_json`` as
the scorer's evidence.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-27 14:02:11.884213
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import freeweight.infrastructure.db.types

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("sample_id", sa.String(length=26), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column(
            "arguments_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column("schema_valid", sa.Boolean(), nullable=False),
        sa.Column("expected_tool", sa.String(), nullable=True),
        sa.Column("correct_tool", sa.Boolean(), nullable=True),
        sa.Column("correct_arguments", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("result_hash", sa.String(), nullable=True),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'error', 'unknown_tool', 'invalid_arguments')",
            name=op.f("ck_tool_calls_status"),
        ),
        sa.ForeignKeyConstraint(
            ["sample_id"],
            ["samples.id"],
            name=op.f("fk_tool_calls_sample_id_samples"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
    )
    with op.batch_alter_table("tool_calls", schema=None) as batch_op:
        batch_op.create_index(
            "ix_tool_calls_sample_id_turn_index_call_index",
            ["sample_id", "turn_index", "call_index"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_calls", schema=None) as batch_op:
        batch_op.drop_index("ix_tool_calls_sample_id_turn_index_call_index")

    op.drop_table("tool_calls")
