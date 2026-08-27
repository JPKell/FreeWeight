"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-26 21:49:45.518631
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import freeweight.infrastructure.db.types

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_sha256", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("last_used_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=True),
        sa.Column("expires_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=True),
        sa.Column("revoked_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_sha256", name=op.f("uq_api_tokens_token_sha256")),
    )
    op.create_table(
        "machines",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("machine_fingerprint", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=True),
        sa.Column("os_name", sa.String(), nullable=True),
        sa.Column("os_version", sa.String(), nullable=True),
        sa.Column("kernel", sa.String(), nullable=True),
        sa.Column("architecture", sa.String(), nullable=True),
        sa.Column("cpu_model", sa.String(), nullable=True),
        sa.Column("physical_cores", sa.Integer(), nullable=True),
        sa.Column("logical_cores", sa.Integer(), nullable=True),
        sa.Column("ram_bytes", sa.BigInteger(), nullable=True),
        sa.Column("gpus_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("storage_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("python_version", sa.String(), nullable=True),
        sa.Column(
            "first_seen_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False
        ),
        sa.Column("last_seen_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_machines")),
        sa.UniqueConstraint("machine_fingerprint", name=op.f("uq_machines_machine_fingerprint")),
    )
    op.create_table(
        "models",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("provider_kind", sa.String(), nullable=False),
        sa.Column("provider_model_name", sa.String(), nullable=False),
        sa.Column("artifact_digest", sa.String(), nullable=True),
        sa.Column("canonical_id", sa.String(), nullable=False),
        sa.Column("identity_confidence", sa.String(), nullable=False),
        sa.Column(
            "first_seen_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False
        ),
        sa.Column("last_seen_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("aliases_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.CheckConstraint(
            "identity_confidence IN ('digest', 'name_only')",
            name=op.f("ck_models_identity_confidence"),
        ),
        sa.CheckConstraint(
            "provider_kind IN ('ollama', 'openai_compatible', 'llamacpp', 'vllm', 'fake')",
            name=op.f("ck_models_provider_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_models")),
    )
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.create_index("ix_models_canonical_id", ["canonical_id"], unique=False)
        batch_op.create_index(
            "ix_models_provider_kind_provider_model_name",
            ["provider_kind", "provider_model_name"],
            unique=False,
        )
        batch_op.create_index(
            "uq_models_identity_triple",
            ["provider_kind", "provider_model_name", "artifact_digest"],
            unique=True,
        )
        batch_op.create_index(
            "uq_models_name_only",
            ["provider_kind", "provider_model_name"],
            unique=True,
            sqlite_where=sa.text("artifact_digest IS NULL"),
            postgresql_where=sa.text("artifact_digest IS NULL"),
        )

    op.create_table(
        "runtime_profiles",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("profile_hash", sa.String(), nullable=False),
        sa.Column("context_size", sa.Integer(), nullable=True),
        sa.Column("kv_cache_precision", sa.String(), nullable=True),
        sa.Column("gpu_layers", sa.Integer(), nullable=True),
        sa.Column("flash_attention", sa.Boolean(), nullable=True),
        sa.Column("threads", sa.Integer(), nullable=True),
        sa.Column("batch_size", sa.Integer(), nullable=True),
        sa.Column("keep_alive", sa.String(), nullable=True),
        sa.Column(
            "provider_options_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_profiles")),
        sa.UniqueConstraint("profile_hash", name=op.f("uq_runtime_profiles_profile_hash")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("updated_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "model_descriptors",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("observed_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("family", sa.String(), nullable=True),
        sa.Column("architecture", sa.String(), nullable=True),
        sa.Column("parameter_count", sa.BigInteger(), nullable=True),
        sa.Column("active_parameter_count", sa.BigInteger(), nullable=True),
        sa.Column("expert_count", sa.Integer(), nullable=True),
        sa.Column("quantization", sa.String(), nullable=True),
        sa.Column("weight_format", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("max_context", sa.Integer(), nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("layers", sa.Integer(), nullable=True),
        sa.Column("attention_heads", sa.Integer(), nullable=True),
        sa.Column("kv_heads", sa.Integer(), nullable=True),
        sa.Column("head_dim", sa.Integer(), nullable=True),
        sa.Column("vocab_size", sa.Integer(), nullable=True),
        sa.Column(
            "rope_config_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column("sliding_window", sa.Integer(), nullable=True),
        sa.Column(
            "declared_capabilities_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("license_text", sa.String(), nullable=True),
        sa.Column("raw_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("descriptor_hash", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_model_descriptors_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_descriptors")),
    )
    with op.batch_alter_table("model_descriptors", schema=None) as batch_op:
        batch_op.create_index(
            "ix_model_descriptors_descriptor_hash", ["descriptor_hash"], unique=False
        )
        batch_op.create_index(
            "ix_model_descriptors_model_id_observed_at", ["model_id", "observed_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("model_descriptors", schema=None) as batch_op:
        batch_op.drop_index("ix_model_descriptors_model_id_observed_at")
        batch_op.drop_index("ix_model_descriptors_descriptor_hash")

    op.drop_table("model_descriptors")
    op.drop_table("settings")
    op.drop_table("runtime_profiles")
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_models_name_only",
            sqlite_where=sa.text("artifact_digest IS NULL"),
            postgresql_where=sa.text("artifact_digest IS NULL"),
        )
        batch_op.drop_index("uq_models_identity_triple")
        batch_op.drop_index("ix_models_provider_kind_provider_model_name")
        batch_op.drop_index("ix_models_canonical_id")

    op.drop_table("models")
    op.drop_table("machines")
    op.drop_table("api_tokens")
