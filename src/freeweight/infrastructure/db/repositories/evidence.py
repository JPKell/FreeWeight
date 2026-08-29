"""freeweight.infrastructure.db.repositories.evidence — reads and writes ``capability_evidence``.

Two write operations and two reads, and the writes are both *replacements*: evidence is recomputed,
never edited (data model §4, "Recomputation replaces rows for the same policy version; two policy
versions coexist"). A subject's rows under one policy version are deleted and rewritten in one
transaction, so a capability that lost its evidence — its only run deleted, its goal now below the
gate — disappears rather than lingering as a stale claim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from freeweight.infrastructure.db.models_evidence import CapabilityEvidence

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.orm import Session

__all__ = ["EvidenceRepository"]


class EvidenceRepository:
    """Reads and writes ``capability_evidence``."""

    def replace_for_subject(  # noqa: PLR0913 — a subject is exactly these three keys
        self,
        session: Session,
        *,
        model_id: str,
        runtime_profile_id: str,
        machine_id: str,
        policy_version: str,
        rows: Sequence[dict[str, Any]],
    ) -> int:
        """Delete one subject's rows under one policy version and write ``rows`` in their place.

        Args:
            session: The caller's active write session.
            model_id: The subject's model.
            runtime_profile_id: The subject's runtime profile.
            machine_id: The subject's machine.
            policy_version: The policy version being rewritten. Rows under another version are
                left alone — that is what lets two policies coexist.
            rows: Column mappings to insert. May be empty, which clears the subject's evidence
                under this policy.

        Returns:
            How many rows were written.
        """
        session.execute(
            delete(CapabilityEvidence).where(
                CapabilityEvidence.model_id == model_id,
                CapabilityEvidence.runtime_profile_id == runtime_profile_id,
                CapabilityEvidence.machine_id == machine_id,
                CapabilityEvidence.policy_version == policy_version,
            )
        )
        for row in rows:
            session.add(CapabilityEvidence(**row))
        session.flush()
        return len(rows)

    def delete_policy(self, session: Session, *, policy_version: str) -> int:
        """Delete every row under one policy version, for a full recomputation.

        Returns:
            How many rows were removed.
        """
        result = session.execute(
            delete(CapabilityEvidence).where(CapabilityEvidence.policy_version == policy_version)
        )
        # ``Session.execute`` is typed as a generic ``Result``; a DELETE returns a ``CursorResult``
        # whose ``rowcount`` is the number removed.
        return int(getattr(result, "rowcount", 0) or 0)

    def list_all(
        self,
        session: Session,
        *,
        capability_id: str | None = None,
        model_id: str | None = None,
        machine_id: str | None = None,
        runtime_profile_id: str | None = None,
        min_confidence: float | None = None,
        policy_version: str | None = None,
        since: datetime | None = None,
    ) -> list[CapabilityEvidence]:
        """Return evidence rows, filtered, in a total order.

        Ordered by ``(capability_id, id)`` — the capability first because a reader asks "who is
        good at coding", and the ULID second so the order is total and a cursor can neither skip
        nor repeat a row (API standards §6).

        Args:
            session: The read session.
            capability_id: Exact capability, when given.
            model_id: Exact model row, when given.
            machine_id: Exact machine row, when given.
            runtime_profile_id: Exact runtime profile row, when given.
            min_confidence: Rows at or above this confidence, when given.
            policy_version: Rows under this policy version, when given.
            since: Rows whose ``computed_at`` is strictly later, when given — the incremental
                export filter (ADR-0022 §5).

        Returns:
            The rows.
        """
        statement = select(CapabilityEvidence).order_by(
            CapabilityEvidence.capability_id.asc(), CapabilityEvidence.id.asc()
        )
        if capability_id is not None:
            statement = statement.where(CapabilityEvidence.capability_id == capability_id)
        if model_id is not None:
            statement = statement.where(CapabilityEvidence.model_id == model_id)
        if machine_id is not None:
            statement = statement.where(CapabilityEvidence.machine_id == machine_id)
        if runtime_profile_id is not None:
            statement = statement.where(CapabilityEvidence.runtime_profile_id == runtime_profile_id)
        if min_confidence is not None:
            statement = statement.where(CapabilityEvidence.confidence >= min_confidence)
        if policy_version is not None:
            statement = statement.where(CapabilityEvidence.policy_version == policy_version)
        if since is not None:
            statement = statement.where(CapabilityEvidence.computed_at > since)
        return list(session.scalars(statement).all())

    def get(self, session: Session, evidence_id: str) -> CapabilityEvidence | None:
        """Return one row by id, or ``None``."""
        return session.get(CapabilityEvidence, evidence_id)
