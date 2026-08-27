"""freeweight.infrastructure.db.repositories.telemetry — reads and writes the two telemetry tables.

One repository for both tables, deliberately: a host row and its GPU rows are written in the same
statement batch and are meaningless apart, so splitting them across two repositories would only
create an ordering rule for a caller to get wrong. The cascade does the same job in the other
direction — deleting a run deletes its host rows, and each host row takes its device rows with it
([ADR-0027 §4](../../../../../../../docs/adr/0027-multi-gpu-semantics.md)).

Every write here happens inside a run. There is no method to persist a sample without a
``run_id``, because telemetry outside a run belongs to nothing that could be read back against a
measurement (spec §10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select

from freeweight.infrastructure.db.models_runs import TelemetryGpuSample, TelemetrySample

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.orm import Session

__all__ = ["TelemetryRepository"]


class TelemetryRepository:
    """Reads and writes ``telemetry_samples`` and ``telemetry_gpu_samples``."""

    def insert_sample(
        self,
        session: Session,
        *,
        run_id: str,
        timestamp: datetime,
        host: dict[str, Any],
        gpus: Sequence[dict[str, Any]],
    ) -> TelemetrySample:
        """Write one observation: one host row and one row per visible GPU.

        Args:
            session: The caller's active session.
            run_id: The run being observed.
            timestamp: When collection began, timezone-aware.
            host: The host column values, ``None`` for anything this platform cannot read.
            gpus: One mapping per visible device, each carrying ``gpu_index``.

        Returns:
            The inserted host row, flushed so its id is available to the GPU rows.
        """
        sample = TelemetrySample(run_id=run_id, timestamp=timestamp, **host)
        session.add(sample)
        session.flush()
        for values in gpus:
            session.add(TelemetryGpuSample(telemetry_sample_id=sample.id, run_id=run_id, **values))
        session.flush()
        return sample

    def list_for_run(
        self, session: Session, run_id: str, *, limit: int = 5000
    ) -> list[TelemetrySample]:
        """Return this run's host rows oldest-first (``ix_telemetry_samples_run_id_timestamp``)."""
        return list(
            session.scalars(
                select(TelemetrySample)
                .where(TelemetrySample.run_id == run_id)
                .order_by(TelemetrySample.timestamp.asc(), TelemetrySample.id.asc())
                .limit(limit)
            ).all()
        )

    def list_gpu_for_run(
        self, session: Session, run_id: str, *, gpu_index: int | None = None, limit: int = 20000
    ) -> list[TelemetryGpuSample]:
        """Return this run's device rows, optionally for one device only.

        Ordered by the parent's timestamp through a join rather than by the child's own id, so a
        chart reads the series in the order it was observed even where two host rows share an
        instant.
        """
        statement = (
            select(TelemetryGpuSample)
            .join(TelemetrySample, TelemetrySample.id == TelemetryGpuSample.telemetry_sample_id)
            .where(TelemetryGpuSample.run_id == run_id)
            .order_by(
                TelemetrySample.timestamp.asc(),
                TelemetryGpuSample.gpu_index.asc(),
            )
            .limit(limit)
        )
        if gpu_index is not None:
            statement = statement.where(TelemetryGpuSample.gpu_index == gpu_index)
        return list(session.scalars(statement).all())

    def count_for_run(self, session: Session, run_id: str) -> int:
        """Return how many host observations were persisted for this run."""
        return int(
            session.scalar(
                select(func.count())
                .select_from(TelemetrySample)
                .where(TelemetrySample.run_id == run_id)
            )
            or 0
        )

    def delete_for_run(self, session: Session, run_id: str) -> int:
        """Delete this run's telemetry and return how many host rows went.

        Exists for the retention policy and for a resumed run that re-records its own window; the
        ordinary path is the cascade from ``runs``, not this.
        """
        count = self.count_for_run(session, run_id)
        session.execute(delete(TelemetrySample).where(TelemetrySample.run_id == run_id))
        return count
