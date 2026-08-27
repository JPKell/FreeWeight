"""freeweight.infrastructure.db.repositories.model_descriptors — the descriptor table's writer.

Every row is a point-in-time snapshot and is immutable once written: a refresh that describes the
same model again inserts a new row rather than updating the last one, so a run's
``model_descriptor_id`` (Phase 5) always resolves to the exact snapshot it was measured against,
even after the model has been re-described (data model §2, ``model_descriptors``). Discovery's own
idempotency requirement — re-running it with nothing changed must not grow this table — is enforced
one layer up, in :mod:`freeweight.services.models`, by comparing the newly computed
``descriptor_hash`` against :meth:`ModelDescriptorRepository.latest_for_model` before deciding
whether to insert at all; this repository itself never skips a write it is asked to make.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from freeweight.infrastructure.db.models import ModelDescriptor

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

__all__ = ["ModelDescriptorRepository"]


class ModelDescriptorRepository:
    """Reads and inserts :class:`~freeweight.infrastructure.db.models.ModelDescriptor` rows.

    Stateless: holds no session and no cache, so one instance is safely shared across requests.
    There is no ``update`` method — see the module docstring.
    """

    def latest_for_model(self, session: Session, model_id: str) -> ModelDescriptor | None:
        """Return the most recently observed descriptor for ``model_id``, or ``None`` if none."""
        return session.scalars(
            select(ModelDescriptor)
            .where(ModelDescriptor.model_id == model_id)
            .order_by(ModelDescriptor.observed_at.desc())
            .limit(1)
        ).one_or_none()

    def history_for_model(self, session: Session, model_id: str) -> list[ModelDescriptor]:
        """Return every descriptor recorded for ``model_id``, most recent first."""
        return list(
            session.scalars(
                select(ModelDescriptor)
                .where(ModelDescriptor.model_id == model_id)
                .order_by(ModelDescriptor.observed_at.desc())
            ).all()
        )

    def insert(
        self,
        session: Session,
        *,
        model_id: str,
        observed_at: datetime,
        family: str | None,
        architecture: str | None,
        parameter_count: int | None,
        active_parameter_count: int | None,
        expert_count: int | None,
        quantization: str | None,
        weight_format: str | None,
        size_bytes: int | None,
        max_context: int | None,
        embedding_dim: int | None,
        layers: int | None,
        attention_heads: int | None,
        kv_heads: int | None,
        head_dim: int | None,
        vocab_size: int | None,
        rope_config_json: Any,  # noqa: ANN401 — already JSON-serializable
        sliding_window: int | None,
        declared_capabilities_json: Any,  # noqa: ANN401 — already JSON-serializable
        license_text: str | None,
        raw_json: Any,  # noqa: ANN401 — already JSON-serializable
        descriptor_hash: str,
    ) -> ModelDescriptor:
        """Insert a new, immutable descriptor snapshot.

        Every numeric field is a plain ``int | None`` rather than a
        :data:`~baseaicore.measurement.Measurement`: this table, like ``machines``, deliberately
        keeps plain nullable columns (data model §2), so the caller has already collapsed
        :data:`~baseaicore.measurement.UNSUPPORTED` to ``None`` before calling this method.

        Args:
            session: The caller's active session.
            model_id: The :class:`~freeweight.infrastructure.db.models.Model` this describes.
            observed_at: When this snapshot was read from the provider. Timezone-aware, UTC.
            descriptor_hash: The content hash the caller computed over the measurement-defining
                subset of these fields — see
                :func:`freeweight.services.models.compute_descriptor_hash`.
            **Everything else:** see
            :class:`~baseaicore.descriptor.ModelDescriptor`, whose fields these mirror one for one.

        Returns:
            The newly inserted row.
        """
        descriptor = ModelDescriptor(
            model_id=model_id,
            observed_at=observed_at,
            family=family,
            architecture=architecture,
            parameter_count=parameter_count,
            active_parameter_count=active_parameter_count,
            expert_count=expert_count,
            quantization=quantization,
            weight_format=weight_format,
            size_bytes=size_bytes,
            max_context=max_context,
            embedding_dim=embedding_dim,
            layers=layers,
            attention_heads=attention_heads,
            kv_heads=kv_heads,
            head_dim=head_dim,
            vocab_size=vocab_size,
            rope_config_json=rope_config_json,
            sliding_window=sliding_window,
            declared_capabilities_json=declared_capabilities_json,
            license_text=license_text,
            raw_json=raw_json,
            descriptor_hash=descriptor_hash,
        )
        session.add(descriptor)
        session.flush()
        return descriptor
