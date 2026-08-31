"""freeweight.infrastructure.db.models — Phase 2 tables: machines and model identity.

Six tables, per [Data Model §2](../../../../../../docs/apps/freeweight/data-model.md): ``machines``,
``models``, ``model_descriptors``, ``runtime_profiles``, ``settings`` and ``api_tokens``. The
run/sample/benchmark tables are deferred to Phase 5 and live in
:mod:`freeweight.infrastructure.db.models_runs`, which stays an unimplemented stub until then.

These are ORM models only — mapping, constraints and indexes. They never leave the repository
layer (coding standards §4); a service or route that needs one of these rows gets a plain value
back from a repository method, never a live, session-bound instance.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from weightsdb import PortableJSON, UtcDateTime, ulid_primary_key

from freeweight.infrastructure.db.base import Base, utcnow

__all__ = ["ApiToken", "Machine", "Model", "ModelDescriptor", "RuntimeProfile", "Setting"]

_PROVIDER_KINDS = ("ollama", "openai_compatible", "llamacpp", "vllm", "fake")
_IDENTITY_CONFIDENCES = ("digest", "name_only")


def _in_list(column: str, allowed: tuple[str, ...]) -> str:
    """Render ``column IN ('a', 'b')`` for a ``CheckConstraint``.

    Not ``f"{column} IN {allowed!r}"``: a tuple's repr is valid SQL only by coincidence, and stops
    being valid the moment the tuple has exactly one element — ``('fake',)`` carries Python's
    disambiguating trailing comma into the DDL as a syntax error, on a constraint nothing would
    exercise until someone narrowed the vocabulary. The values are enumerated in this module and
    are never user input; the guard below states that rather than assuming it.
    """
    if any("'" in value for value in allowed):
        raise ValueError(f"CheckConstraint values must not contain a quote: {allowed!r}")
    rendered = ", ".join(f"'{value}'" for value in allowed)
    return f"{column} IN ({rendered})"


class Machine(Base):
    """One physical or virtual machine a measurement was produced on.

    One row per :attr:`machine_fingerprint`
    (docs/architecture/machine-identity-and-reproducibility.md),
    upserted by fingerprint at every startup (Phase 4). ``physical_cores``, ``logical_cores`` and
    ``ram_bytes`` are plain nullable columns rather than a
    :func:`~freeweight.infrastructure.db.types.measurement_columns` pair: unlike a GPU's VRAM or
    temperature, a host reporting no core count or no RAM total is not a real-world case this
    schema needs to distinguish from "not yet collected" (data model §2, ``machines``).
    """

    __tablename__ = "machines"

    id: Mapped[str] = ulid_primary_key()
    machine_fingerprint: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    hostname: Mapped[str | None] = mapped_column(String)
    os_name: Mapped[str | None] = mapped_column(String)
    os_version: Mapped[str | None] = mapped_column(String)
    kernel: Mapped[str | None] = mapped_column(String)
    architecture: Mapped[str | None] = mapped_column(String)
    cpu_model: Mapped[str | None] = mapped_column(String)
    physical_cores: Mapped[int | None] = mapped_column(Integer)
    logical_cores: Mapped[int | None] = mapped_column(Integer)
    ram_bytes: Mapped[int | None] = mapped_column(BigInteger)
    gpus_json: Mapped[object | None] = mapped_column(PortableJSON)
    storage_json: Mapped[object | None] = mapped_column(PortableJSON)
    python_version: Mapped[str | None] = mapped_column(String)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class Model(Base):
    """One canonical model identity — which weights, on which kind of provider.

    At most one row per ``(provider_kind, provider_model_name)`` may carry a ``NULL``
    ``artifact_digest`` (a ``name_only`` identity); ``uq_models_name_only`` enforces this at the
    database level because a plain unique constraint over all three columns treats every ``NULL``
    as distinct and would happily admit two. When a digest later arrives for a name that was
    previously ``name_only``, the repository upgrades that row in place rather than inserting a
    second one (data model §2, ``models``); a changed digest on an already-pinned identity instead
    creates a **new** row and leaves the old one untouched, so re-tagging never rewrites history.
    """

    __tablename__ = "models"
    __table_args__ = (
        # Short, local names here: Base's naming convention template
        # ("ck_%(table_name)s_%(constraint_name)s") already prepends "ck_models_" — passing the
        # fully-qualified name here would double it to "ck_models_ck_models_provider_kind".
        CheckConstraint(_in_list("provider_kind", _PROVIDER_KINDS), name="provider_kind"),
        CheckConstraint(
            _in_list("identity_confidence", _IDENTITY_CONFIDENCES), name="identity_confidence"
        ),
        Index(
            "uq_models_name_only",
            "provider_kind",
            "provider_model_name",
            unique=True,
            sqlite_where=text("artifact_digest IS NULL"),
            postgresql_where=text("artifact_digest IS NULL"),
        ),
        Index(
            "ix_models_provider_kind_provider_model_name", "provider_kind", "provider_model_name"
        ),
        Index("ix_models_canonical_id", "canonical_id"),
        Index(
            "uq_models_identity_triple",
            "provider_kind",
            "provider_model_name",
            "artifact_digest",
            unique=True,
        ),
    )

    id: Mapped[str] = ulid_primary_key()
    provider_kind: Mapped[str] = mapped_column(String, nullable=False)
    provider_model_name: Mapped[str] = mapped_column(String, nullable=False)
    artifact_digest: Mapped[str | None] = mapped_column(String)
    canonical_id: Mapped[str] = mapped_column(String, nullable=False)
    identity_confidence: Mapped[str] = mapped_column(String, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    aliases_json: Mapped[object | None] = mapped_column(PortableJSON)


class ModelDescriptor(Base):
    """A point-in-time snapshot of what a provider reports about a model's architecture.

    Immutable once written: a refresh inserts a new row rather than updating the last one, so a
    run's ``model_descriptor_id`` (Phase 5) always resolves to the exact snapshot it was measured
    against, even after the model has been re-described (data model §2, ``model_descriptors``).
    ``ON DELETE RESTRICT`` on ``model_id`` matches: deleting results must never delete the model or
    its descriptor history (database standards §8).
    """

    __tablename__ = "model_descriptors"
    __table_args__ = (
        Index("ix_model_descriptors_model_id_observed_at", "model_id", "observed_at"),
        Index("ix_model_descriptors_descriptor_hash", "descriptor_hash"),
    )

    id: Mapped[str] = ulid_primary_key()
    model_id: Mapped[str] = mapped_column(
        String, ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    family: Mapped[str | None] = mapped_column(String)
    architecture: Mapped[str | None] = mapped_column(String)
    parameter_count: Mapped[int | None] = mapped_column(BigInteger)
    active_parameter_count: Mapped[int | None] = mapped_column(BigInteger)
    expert_count: Mapped[int | None] = mapped_column(Integer)
    quantization: Mapped[str | None] = mapped_column(String)
    weight_format: Mapped[str | None] = mapped_column(String)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    max_context: Mapped[int | None] = mapped_column(Integer)
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    layers: Mapped[int | None] = mapped_column(Integer)
    attention_heads: Mapped[int | None] = mapped_column(Integer)
    kv_heads: Mapped[int | None] = mapped_column(Integer)
    head_dim: Mapped[int | None] = mapped_column(Integer)
    vocab_size: Mapped[int | None] = mapped_column(Integer)
    rope_config_json: Mapped[object | None] = mapped_column(PortableJSON)
    sliding_window: Mapped[int | None] = mapped_column(Integer)
    declared_capabilities_json: Mapped[object | None] = mapped_column(PortableJSON)
    license_text: Mapped[str | None] = mapped_column(String)
    raw_json: Mapped[object | None] = mapped_column(PortableJSON)
    descriptor_hash: Mapped[str] = mapped_column(String, nullable=False)


class RuntimeProfile(Base):
    """A named set of serving parameters a model was run under (context size, quantization of the
    KV cache, GPU layer offload, …).

    Deduplicated by :attr:`profile_hash`, a content hash of the fields below, so two runs executed
    under identical serving parameters share one row rather than each recording their own copy
    (data model §2, ``runtime_profiles``).
    """

    __tablename__ = "runtime_profiles"

    id: Mapped[str] = ulid_primary_key()
    profile_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    context_size: Mapped[int | None] = mapped_column(Integer)
    kv_cache_precision: Mapped[str | None] = mapped_column(String)
    gpu_layers: Mapped[int | None] = mapped_column(Integer)
    flash_attention: Mapped[bool | None] = mapped_column(Boolean)
    threads: Mapped[int | None] = mapped_column(Integer)
    batch_size: Mapped[int | None] = mapped_column(Integer)
    keep_alive: Mapped[str | None] = mapped_column(String)
    provider_options_json: Mapped[object | None] = mapped_column(PortableJSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class Setting(Base):
    """One runtime-changeable configuration value.

    Never a security-relevant one — those live only in ``config.toml``/environment variables
    (configuration standards §7) — so this table has no column for a token, a credential or a
    binding decision.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[object | None] = mapped_column(PortableJSON)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class ApiToken(Base):
    """One bearer token accepted for a non-loopback bind (ADR-0014).

    ``token_sha256`` is the only form of the token this table ever stores; the bearer value itself
    is shown to the operator exactly once, at creation, and is not recoverable from this row.
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = ulid_primary_key()
    name: Mapped[str] = mapped_column(String, nullable=False)
    token_sha256: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
