"""telemetry tables

The two Phase 6 tables, split host from device: ``telemetry_samples`` holds one row per
observation with the host fields, ``telemetry_gpu_samples`` holds one row per visible GPU with a
foreign key back to it. A single table repeated every host field across a machine's GPUs, so any
host aggregate double-counted on a two-GPU machine
([ADR-0027 §4](../../../../../../../docs/adr/0027-multi-gpu-semantics.md)).

Additive only — no table from ``0001`` or ``0002`` is altered, so upgrading a populated Phase 5
database touches no existing row. ``downgrade`` drops both tables; that loses telemetry rows, and
only telemetry rows, which is why it is allowed to be silent rather than raising: no run, sample or
metric depends on them.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-27 10:35:02.461485
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import freeweight.infrastructure.db.types

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "telemetry_samples",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("timestamp", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("cpu_percent", sa.Float(), nullable=True),
        sa.Column("load_average_1m", sa.Float(), nullable=True),
        sa.Column("ram_used_bytes", sa.BigInteger(), nullable=True),
        sa.Column("ram_available_bytes", sa.BigInteger(), nullable=True),
        sa.Column("ram_total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("cpu_temperature_c", sa.Float(), nullable=True),
        sa.Column("disk_read_bytes_per_sec", sa.Float(), nullable=True),
        sa.Column("disk_write_bytes_per_sec", sa.Float(), nullable=True),
        sa.Column("process_rss_bytes", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_telemetry_samples_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telemetry_samples")),
    )
    with op.batch_alter_table("telemetry_samples", schema=None) as batch_op:
        batch_op.create_index(
            "ix_telemetry_samples_run_id_timestamp", ["run_id", "timestamp"], unique=False
        )

    op.create_table(
        "telemetry_gpu_samples",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("telemetry_sample_id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("gpu_index", sa.Integer(), nullable=False),
        sa.Column("gpu_uuid", sa.String(), nullable=True),
        sa.Column("gpu_utilization_percent", sa.Float(), nullable=True),
        sa.Column("gpu_memory_utilization_percent", sa.Float(), nullable=True),
        sa.Column("vram_used_bytes", sa.BigInteger(), nullable=True),
        sa.Column("vram_total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("gpu_temperature_c", sa.Float(), nullable=True),
        sa.Column("gpu_memory_temperature_c", sa.Float(), nullable=True),
        sa.Column("gpu_power_watts", sa.Float(), nullable=True),
        sa.Column("gpu_power_limit_watts", sa.Float(), nullable=True),
        sa.Column("gpu_fan_percent", sa.Float(), nullable=True),
        sa.Column("gpu_core_clock_mhz", sa.Float(), nullable=True),
        sa.Column("gpu_memory_clock_mhz", sa.Float(), nullable=True),
        sa.Column(
            "throttle_reasons_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("throttle_reasons_available", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_telemetry_gpu_samples_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["telemetry_sample_id"],
            ["telemetry_samples.id"],
            name=op.f("fk_telemetry_gpu_samples_telemetry_sample_id_telemetry_samples"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telemetry_gpu_samples")),
        sa.UniqueConstraint(
            "telemetry_sample_id", "gpu_index", name="uq_telemetry_gpu_samples_sample_id_gpu_index"
        ),
    )
    with op.batch_alter_table("telemetry_gpu_samples", schema=None) as batch_op:
        batch_op.create_index(
            "ix_telemetry_gpu_samples_run_id_gpu_index", ["run_id", "gpu_index"], unique=False
        )
        batch_op.create_index(
            "ix_telemetry_gpu_samples_telemetry_sample_id", ["telemetry_sample_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("telemetry_gpu_samples", schema=None) as batch_op:
        batch_op.drop_index("ix_telemetry_gpu_samples_telemetry_sample_id")
        batch_op.drop_index("ix_telemetry_gpu_samples_run_id_gpu_index")

    op.drop_table("telemetry_gpu_samples")
    with op.batch_alter_table("telemetry_samples", schema=None) as batch_op:
        batch_op.drop_index("ix_telemetry_samples_run_id_timestamp")

    op.drop_table("telemetry_samples")
