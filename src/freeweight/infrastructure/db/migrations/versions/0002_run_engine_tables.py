"""run engine tables

The eight tables of the Phase 5 run engine: ``benchmark_suites``, ``benchmark_tests``, ``runs``,
``run_tests``, ``samples``, ``metric_values``, ``run_events`` and ``artifacts``. Additive only —
no table from ``0001`` is altered, so upgrading a populated Phase 4 database touches no existing
row.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-27 08:57:50.390534
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import freeweight.infrastructure.db.types

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "benchmark_suites",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("runner", sa.String(), nullable=False),
        sa.Column("goal_id", sa.String(length=26), nullable=True),
        sa.Column("goal_hash", sa.String(), nullable=True),
        sa.Column("manifest_hash", sa.String(), nullable=False),
        sa.Column(
            "manifest_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column(
            "dataset_hashes_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column("prompt_subset_hash", sa.String(), nullable=True),
        sa.Column(
            "prompt_refs_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column("source_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("license", sa.String(), nullable=True),
        sa.Column("installed_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "runner IN ('native', 'external', 'goal')", name=op.f("ck_benchmark_suites_runner")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_suites")),
        sa.UniqueConstraint("key", "version", name="uq_benchmark_suites_key_version"),
    )
    with op.batch_alter_table("benchmark_suites", schema=None) as batch_op:
        batch_op.create_index("ix_benchmark_suites_key", ["key"], unique=False)

    op.create_table(
        "benchmark_tests",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("suite_id", sa.String(length=26), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("scorer", sa.String(), nullable=False),
        sa.Column("config_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column(
            "metric_definitions_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column(
            "requires_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["suite_id"],
            ["benchmark_suites.id"],
            name=op.f("fk_benchmark_tests_suite_id_benchmark_suites"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_tests")),
        sa.UniqueConstraint("suite_id", "key", name="uq_benchmark_tests_suite_id_key"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("machine_id", sa.String(length=26), nullable=False),
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column("model_descriptor_id", sa.String(length=26), nullable=False),
        sa.Column("runtime_profile_id", sa.String(length=26), nullable=False),
        sa.Column("suite_id", sa.String(length=26), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("started_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=True),
        sa.Column("completed_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=True),
        sa.Column(
            "effective_config_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("reproducibility_fingerprint", sa.String(), nullable=False),
        sa.Column(
            "fingerprint_document_json",
            freeweight.infrastructure.db.types.PortableJSON(),
            nullable=True,
        ),
        sa.Column("provider_kind", sa.String(), nullable=True),
        sa.Column("provider_version", sa.String(), nullable=True),
        sa.Column("application_version", sa.String(), nullable=True),
        sa.Column("git_commit", sa.String(), nullable=True),
        sa.Column("prompt_pack_id", sa.String(), nullable=True),
        sa.Column("prompt_pack_version", sa.String(), nullable=True),
        sa.Column("prompt_pack_hash", sa.String(), nullable=True),
        sa.Column("served_context", sa.Integer(), nullable=True),
        sa.Column("served_context_source", sa.String(), nullable=True),
        sa.Column("gpu_index", sa.Integer(), nullable=True),
        sa.Column("multi_gpu_visible", sa.Boolean(), nullable=False),
        sa.Column("sandbox_tier", sa.String(), nullable=True),
        sa.Column("telemetry_overhead_percent", sa.Float(), nullable=True),
        sa.Column(
            "degradations_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True
        ),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'preparing', 'warming', 'running', 'cancelling', "
            "'completed', 'failed', 'cancelled', 'interrupted')",
            name=op.f("ck_runs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["machine_id"],
            ["machines.id"],
            name=op.f("fk_runs_machine_id_machines"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_descriptor_id"],
            ["model_descriptors.id"],
            name=op.f("fk_runs_model_descriptor_id_model_descriptors"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"], ["models.id"], name=op.f("fk_runs_model_id_models"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["runtime_profile_id"],
            ["runtime_profiles.id"],
            name=op.f("fk_runs_runtime_profile_id_runtime_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["suite_id"],
            ["benchmark_suites.id"],
            name=op.f("fk_runs_suite_id_benchmark_suites"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_runs_machine_id_created_at", ["machine_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_runs_model_id_created_at", ["model_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_runs_reproducibility_fingerprint", ["reproducibility_fingerprint"], unique=False
        )
        batch_op.create_index("ix_runs_status_created_at", ["status", "created_at"], unique=False)
        batch_op.create_index(
            "ix_runs_suite_id_created_at", ["suite_id", "created_at"], unique=False
        )

    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("timestamp", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("progress_completed", sa.Integer(), nullable=True),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("data_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_events_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_events")),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_run_id_sequence"),
    )
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.create_index("ix_run_events_run_id_sequence", ["run_id", "sequence"], unique=False)

    op.create_table(
        "run_tests",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("test_id", sa.String(length=26), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("skip_reason", sa.String(), nullable=True),
        sa.Column("completed_cases", sa.Integer(), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column("started_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=True),
        sa.Column("completed_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=True),
        sa.Column("measurement_class", sa.String(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'cancelled')",
            name=op.f("ck_run_tests_status"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_tests_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["test_id"],
            ["benchmark_tests.id"],
            name=op.f("fk_run_tests_test_id_benchmark_tests"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_tests")),
        sa.UniqueConstraint("run_id", "test_id", name="uq_run_tests_run_id_test_id"),
    )
    with op.batch_alter_table("run_tests", schema=None) as batch_op:
        batch_op.create_index("ix_run_tests_run_id_status", ["run_id", "status"], unique=False)

    op.create_table(
        "samples",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("run_test_id", sa.String(length=26), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("prompt_hash", sa.String(), nullable=True),
        sa.Column("rendered_prompt_hash", sa.String(), nullable=True),
        sa.Column("prompt_id", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("response_hash", sa.String(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("thinking_tokens", sa.Integer(), nullable=True),
        sa.Column("tool_tokens", sa.Integer(), nullable=True),
        sa.Column("output_chars", sa.Integer(), nullable=True),
        sa.Column("output_words", sa.Integer(), nullable=True),
        sa.Column("output_bytes", sa.Integer(), nullable=True),
        sa.Column("client_wall_ms", sa.Float(), nullable=True),
        sa.Column("client_ttft_ms", sa.Float(), nullable=True),
        sa.Column("backend_load_ms", sa.Float(), nullable=True),
        sa.Column("backend_prompt_eval_ms", sa.Float(), nullable=True),
        sa.Column("backend_decode_ms", sa.Float(), nullable=True),
        sa.Column("backend_total_ms", sa.Float(), nullable=True),
        sa.Column("finish_reason", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("score_method", sa.String(), nullable=True),
        sa.Column("judge_model_id", sa.String(length=26), nullable=True),
        sa.Column("result_json", freeweight.infrastructure.db.types.PortableJSON(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        # When the request went out. `created_at` is written when it comes *back*, so without this
        # a sample's window has to be reconstructed as `created_at - client_wall_ms` — an
        # approximation that can attribute a telemetry reading to the wrong request whenever the
        # sampler interval is close to the request duration (ADR-0034's inputs are only as good as
        # the windows they slice).
        sa.Column("started_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=True),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "started_at IS NULL OR started_at <= created_at",
            name=op.f("ck_samples_started_at_before_created_at"),
        ),
        sa.CheckConstraint(
            "status = 'completed' OR score IS NULL",
            name=op.f("ck_samples_score_null_unless_completed"),
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'awaiting_judgement', 'failed', 'timeout', 'cancelled', "
            "'skipped')",
            name=op.f("ck_samples_status"),
        ),
        sa.ForeignKeyConstraint(
            ["judge_model_id"],
            ["models.id"],
            name=op.f("fk_samples_judge_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_test_id"],
            ["run_tests.id"],
            name=op.f("fk_samples_run_test_id_run_tests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_samples")),
        sa.UniqueConstraint(
            "run_test_id",
            "case_id",
            "ordinal",
            "repetition",
            name="uq_samples_run_test_id_case_id_ordinal_repetition",
        ),
    )
    with op.batch_alter_table("samples", schema=None) as batch_op:
        batch_op.create_index("ix_samples_created_at", ["created_at"], unique=False)
        batch_op.create_index(
            "ix_samples_run_test_id_ordinal", ["run_test_id", "ordinal"], unique=False
        )
        batch_op.create_index(
            "ix_samples_run_test_id_status", ["run_test_id", "status"], unique=False
        )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("run_test_id", sa.String(length=26), nullable=True),
        sa.Column("sample_id", sa.String(length=26), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('raw_response', 'generated_code', 'external_output', 'export', 'log')",
            name=op.f("ck_artifacts_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_artifacts_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_test_id"],
            ["run_tests.id"],
            name=op.f("fk_artifacts_run_test_id_run_tests"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sample_id"],
            ["samples.id"],
            name=op.f("fk_artifacts_sample_id_samples"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifacts")),
    )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.create_index("ix_artifacts_run_id", ["run_id"], unique=False)

    op.create_table(
        "metric_values",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("run_test_id", sa.String(length=26), nullable=True),
        sa.Column("sample_id", sa.String(length=26), nullable=True),
        sa.Column("metric_key", sa.String(), nullable=False),
        sa.Column("numeric_value", sa.Float(), nullable=True),
        sa.Column("text_value", sa.String(), nullable=True),
        sa.Column("unavailable_reason", sa.String(), nullable=True),
        sa.Column("gpu_index", sa.Integer(), nullable=True),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("aggregation", sa.String(), nullable=False),
        sa.Column("higher_is_better", sa.Boolean(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=True),
        sa.Column("excluded_count", sa.Integer(), nullable=True),
        sa.Column("stddev", sa.Float(), nullable=True),
        sa.Column("coefficient_of_variation", sa.Float(), nullable=True),
        sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_metric_values_run_id_runs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_test_id"],
            ["run_tests.id"],
            name=op.f("fk_metric_values_run_test_id_run_tests"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sample_id"],
            ["samples.id"],
            name=op.f("fk_metric_values_sample_id_samples"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metric_values")),
    )
    with op.batch_alter_table("metric_values", schema=None) as batch_op:
        batch_op.create_index(
            "ix_metric_values_metric_key_numeric_value",
            ["metric_key", "numeric_value"],
            unique=False,
        )
        batch_op.create_index(
            "ix_metric_values_run_id_metric_key", ["run_id", "metric_key"], unique=False
        )
        batch_op.create_index(
            "ix_metric_values_run_test_id_metric_key", ["run_test_id", "metric_key"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("metric_values", schema=None) as batch_op:
        batch_op.drop_index("ix_metric_values_run_test_id_metric_key")
        batch_op.drop_index("ix_metric_values_run_id_metric_key")
        batch_op.drop_index("ix_metric_values_metric_key_numeric_value")

    op.drop_table("metric_values")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index("ix_artifacts_run_id")

    op.drop_table("artifacts")
    with op.batch_alter_table("samples", schema=None) as batch_op:
        batch_op.drop_index("ix_samples_run_test_id_status")
        batch_op.drop_index("ix_samples_run_test_id_ordinal")
        batch_op.drop_index("ix_samples_created_at")

    op.drop_table("samples")
    with op.batch_alter_table("run_tests", schema=None) as batch_op:
        batch_op.drop_index("ix_run_tests_run_id_status")

    op.drop_table("run_tests")
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.drop_index("ix_run_events_run_id_sequence")

    op.drop_table("run_events")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index("ix_runs_suite_id_created_at")
        batch_op.drop_index("ix_runs_status_created_at")
        batch_op.drop_index("ix_runs_reproducibility_fingerprint")
        batch_op.drop_index("ix_runs_model_id_created_at")
        batch_op.drop_index("ix_runs_machine_id_created_at")

    op.drop_table("runs")
    op.drop_table("benchmark_tests")
    with op.batch_alter_table("benchmark_suites", schema=None) as batch_op:
        batch_op.drop_index("ix_benchmark_suites_key")

    op.drop_table("benchmark_suites")
