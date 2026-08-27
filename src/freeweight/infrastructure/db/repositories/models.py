"""freeweight.infrastructure.db.repositories.models — the ``models`` table's only writer.

The one non-obvious rule this repository enforces (data model §2, ``models``): at most one
``name_only`` row exists per ``(provider_kind, provider_model_name)``, and a digest arriving for a
name previously seen only as ``name_only`` upgrades that row in place rather than creating a
second one. A *changed* digest on an identity that already has one is a different model as far as
comparability goes and creates a new row, leaving the old identity's history untouched
(canonical model identity §7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from baseaicore.timeutil import to_rfc3339
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from freeweight.infrastructure.db.models import Model

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import ColumnElement
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.orm import Session

__all__ = ["ModelRepository"]


class ModelRepository:
    """Reads and writes :class:`~freeweight.infrastructure.db.models.Model` rows.

    Stateless: holds no session and no cache, so one instance is safely shared across requests.
    """

    def get_by_id(self, session: Session, model_id: str) -> Model | None:
        """Return the model with this primary key, or ``None`` if it does not exist."""
        return session.get(Model, model_id)

    def get_by_canonical_id(self, session: Session, canonical_id: str) -> Model | None:
        """Return the model with this canonical ID, or ``None``.

        The canonical ID is a display and lookup key, never a path segment
        (:class:`~baseaicore.identity.ModelIdentity`); this is the lookup it is for.
        """
        return session.scalars(
            select(Model).where(Model.canonical_id == canonical_id)
        ).one_or_none()

    def get_by_identity(
        self,
        session: Session,
        *,
        provider_kind: str,
        provider_model_name: str,
        artifact_digest: str | None,
    ) -> Model | None:
        """Return the model matching this exact identity triple, or ``None``.

        ``artifact_digest=None`` looks up the ``name_only`` row for this
        ``(provider_kind, provider_model_name)``, if one exists — there is at most one, by
        construction (:meth:`upsert_identity`).
        """
        return session.scalars(
            select(Model).where(
                *self._identity_filters(
                    provider_kind=provider_kind,
                    provider_model_name=provider_model_name,
                    artifact_digest=artifact_digest,
                )
            )
        ).one_or_none()

    def list_all(self, session: Session) -> list[Model]:
        """Return every known model identity, most recently seen first."""
        return list(session.scalars(select(Model).order_by(Model.last_seen_at.desc())).all())

    def get_by_id_prefix(self, session: Session, prefix: str) -> list[Model]:
        """Return every model whose ULID starts with ``prefix``.

        Application-local ULIDs, not the canonical ID, are what a caller addresses a specific model
        by (API §2, ADR-0024): "the canonical ID is never a path segment". Zero rows means no match;
        more than one means ``prefix`` is ambiguous and the caller must refuse rather than pick one
        (api.md §2: "an ambiguous prefix returns 400 listing the candidates").
        """
        return list(session.scalars(select(Model).where(Model.id.startswith(prefix))).all())

    def get_by_provider_model_name(
        self, session: Session, provider_model_name: str
    ) -> Model | None:
        """Return the most recently seen model with exactly this provider-reported name.

        A name can legitimately own more than one row over time — a retag keeps the old digest row
        and creates a new one (:meth:`upsert_identity`) — so this returns the current one rather
        than raising on the ambiguity a bare name always carries once a model has been retagged.
        """
        return session.scalars(
            select(Model)
            .where(Model.provider_model_name == provider_model_name)
            .order_by(Model.last_seen_at.desc())
            .limit(1)
        ).one_or_none()

    def record_alias(self, session: Session, model_id: str, *, alias: str, now: datetime) -> Model:
        """Append an observed ``alias -> this identity`` resolution, or refresh its timestamp.

        ModelRack's :meth:`~modelrack.provider.Provider.resolve` only logs the alias it followed
        (its own package owns no persistent state); recording the observation for good is this
        application's job (canonical model identity §2.3: "it does not resolve aliases silently").
        Deduplicated by alias text so re-resolving the same shorthand a hundred times leaves one
        entry with a moving timestamp, not a hundred.

        Args:
            session: The caller's active session.
            model_id: The :class:`Model` this alias resolved to.
            alias: What the caller typed — a bare name, an old tag, or an unambiguous prefix.
            now: The instant this resolution was observed.

        Returns:
            The current :class:`Model` row after the write.

        Raises:
            RuntimeError: ``model_id`` does not name an existing row — a caller error, since the
                alias belongs to an identity that must already have been resolved by the time this
                is called.
        """
        model = self.get_by_id(session, model_id)
        if model is None:
            raise RuntimeError(
                f"Cannot record alias {alias!r} against model {model_id!r}: no such model exists."
            )
        history: list[dict[str, str]] = list(cast("list[dict[str, str]]", model.aliases_json or []))
        observed_at = to_rfc3339(now)
        for entry in history:
            if entry.get("alias") == alias:
                entry["resolved_at"] = observed_at
                break
        else:
            history.append({"alias": alias, "resolved_at": observed_at})
        session.execute(update(Model).where(Model.id == model_id).values(aliases_json=history))
        session.flush()
        refreshed = self.get_by_id(session, model_id)
        if refreshed is None:
            raise RuntimeError(
                f"Model {model_id!r} was not found immediately after recording an alias in this "
                "same transaction; this indicates a driver or session bug."
            )
        return refreshed

    @staticmethod
    def _identity_filters(
        *, provider_kind: str, provider_model_name: str, artifact_digest: str | None
    ) -> tuple[ColumnElement[bool], ...]:
        """The WHERE terms selecting exactly one identity triple, ``NULL`` digest included."""
        return (
            Model.provider_kind == provider_kind,
            Model.provider_model_name == provider_model_name,
            Model.artifact_digest.is_(None)
            if artifact_digest is None
            else Model.artifact_digest == artifact_digest,
        )

    def upsert_identity(
        self,
        session: Session,
        *,
        provider_kind: str,
        provider_model_name: str,
        artifact_digest: str | None,
        canonical_id: str,
        identity_confidence: str,
        now: datetime,
    ) -> Model:
        """Record a sighting of this identity, applying the ``name_only``-upgrade rule.

        Three cases, in order:

        1. ``artifact_digest`` is given, a ``name_only`` row exists for this
           ``(provider_kind, provider_model_name)``, **and no row already holds that digest** —
           the ``name_only`` row is upgraded in place: its digest, canonical ID, confidence and
           ``last_seen_at`` are updated, and no new row is created.
        2. Otherwise, the exact identity (all three columns) is looked up; if found, only
           ``last_seen_at`` (and, defensively, ``canonical_id``/``identity_confidence``) are
           refreshed. If not found, a new row is inserted — this is how a *changed* digest on an
           already-``digest``-confident name produces a second, independent identity.
        3. ``artifact_digest is None`` follows the same touch-or-insert shape against the
           ``name_only`` slot alone.

        The "and no row already holds that digest" qualifier in case 1 is not defensive
        decoration. A provider that reports a digest, then stops reporting one for a while, then
        reports it again leaves both a digest row and a ``name_only`` row for the same name — a
        sequence Ollama produces in practice. Upgrading the ``name_only`` row unconditionally at
        that point drives it straight into the digest row already sitting on
        ``uq_models_identity_triple``, and the resulting ``IntegrityError`` escapes the repository
        as a raw SQLAlchemy exception. The condition lives inside the ``UPDATE``'s own ``WHERE``
        (as a ``NOT EXISTS``) rather than in a preceding ``SELECT``, so it stays a single
        statement and never opens the check-then-act race database standards §2 forbids. The
        ``name_only`` row is then left alone rather than merged away: it records real sightings
        that the digest row cannot account for, and deleting it here would destroy history to tidy
        up a duplicate that is not one.

        Every write is a conditional ``UPDATE`` followed by an insert that is retried as an update
        if a concurrent writer won the race — never a plain ``SELECT`` followed by an
        unconditional ``INSERT``. SQLite's single-writer, ``BEGIN IMMEDIATE`` transactions make
        the race impossible in the first place; the savepoint-and-retry path exists for
        PostgreSQL's ordinary MVCC concurrency.

        Args:
            session: The caller's active session.
            provider_kind: Which kind of provider reported this identity.
            provider_model_name: Exactly as the provider names it.
            artifact_digest: Already normalized (:func:`~baseaicore.identity.normalize_digest`),
                or ``None`` when the provider exposed no digest.
            canonical_id: :attr:`~baseaicore.identity.ModelIdentity.canonical_id` for this sighting.
            identity_confidence: ``"digest"`` or ``"name_only"``, matching ``artifact_digest``.
            now: The instant to record as ``last_seen_at``, and as ``first_seen_at`` on the
                initial insert. Injected so callers are deterministic in tests, exactly as
                :meth:`~freeweight.infrastructure.db.repositories.machines.MachineRepository.upsert`
                does — a repository that let the column default supply wall-clock time here would
                be the one row in the schema nobody could assert on.

        Returns:
            The current :class:`Model` row for this identity after the write.
        """
        exact_filters = self._identity_filters(
            provider_kind=provider_kind,
            provider_model_name=provider_model_name,
            artifact_digest=artifact_digest,
        )
        insert_kwargs: dict[str, object] = {
            "provider_kind": provider_kind,
            "provider_model_name": provider_model_name,
            "artifact_digest": artifact_digest,
            "canonical_id": canonical_id,
            "identity_confidence": identity_confidence,
            "first_seen_at": now,
            "last_seen_at": now,
        }

        upgraded_in_place = False
        if artifact_digest is not None:
            upgraded_in_place = self._upgrade_name_only_row(
                session,
                provider_kind=provider_kind,
                provider_model_name=provider_model_name,
                artifact_digest=artifact_digest,
                canonical_id=canonical_id,
                identity_confidence=identity_confidence,
                now=now,
            )
        if not upgraded_in_place:
            self._touch_or_insert(
                session,
                filters=exact_filters,
                canonical_id=canonical_id,
                identity_confidence=identity_confidence,
                now=now,
                insert_kwargs=insert_kwargs,
            )

        session.flush()
        identity = self.get_by_identity(
            session,
            provider_kind=provider_kind,
            provider_model_name=provider_model_name,
            artifact_digest=artifact_digest,
        )
        if identity is None:
            raise RuntimeError(
                f"Model identity ({provider_kind!r}, {provider_model_name!r}, "
                f"{artifact_digest!r}) was not found immediately after being upserted in this "
                "same transaction; this indicates a driver or session bug."
            )
        return identity

    def _upgrade_name_only_row(
        self,
        session: Session,
        *,
        provider_kind: str,
        provider_model_name: str,
        artifact_digest: str,
        canonical_id: str,
        identity_confidence: str,
        now: datetime,
    ) -> bool:
        """Promote this name's ``name_only`` row to ``artifact_digest``, unless that digest exists.

        Returns:
            Whether a row was upgraded. ``False`` means either there was no ``name_only`` row or
            the digest is already taken by another row — in both cases the caller falls through to
            the ordinary touch-or-insert against the exact triple.
        """
        digest_holder = aliased(Model, name="digest_holder")
        digest_already_taken = (
            select(digest_holder.id)
            .where(
                digest_holder.provider_kind == provider_kind,
                digest_holder.provider_model_name == provider_model_name,
                digest_holder.artifact_digest == artifact_digest,
            )
            .exists()
        )
        upgraded = cast(
            "CursorResult[Any]",
            session.execute(
                update(Model)
                .where(
                    Model.provider_kind == provider_kind,
                    Model.provider_model_name == provider_model_name,
                    Model.artifact_digest.is_(None),
                    ~digest_already_taken,
                )
                .values(
                    artifact_digest=artifact_digest,
                    canonical_id=canonical_id,
                    identity_confidence=identity_confidence,
                    last_seen_at=now,
                )
            ),
        )
        return bool(upgraded.rowcount)

    def _touch_or_insert(
        self,
        session: Session,
        *,
        filters: tuple[ColumnElement[bool], ...],
        canonical_id: str,
        identity_confidence: str,
        now: datetime,
        insert_kwargs: dict[str, object],
    ) -> None:
        """Update the one row matching ``filters`` if it exists, else insert it.

        On a uniqueness violation from a concurrent insert of the same row, the insert is
        abandoned via its savepoint and the update is retried — by then the other writer has
        committed, so the retried update finds exactly one row.
        """
        if self._touch(session, filters, canonical_id, identity_confidence, now):
            return
        savepoint = session.begin_nested()
        try:
            session.add(Model(**insert_kwargs))
            session.flush()
        except IntegrityError:
            savepoint.rollback()
            self._touch(session, filters, canonical_id, identity_confidence, now)
        else:
            savepoint.commit()

    @staticmethod
    def _touch(
        session: Session,
        filters: tuple[ColumnElement[bool], ...],
        canonical_id: str,
        identity_confidence: str,
        now: datetime,
    ) -> bool:
        """Refresh the row matching ``filters``; return whether one existed."""
        touched = cast(
            "CursorResult[Any]",
            session.execute(
                update(Model)
                .where(*filters)
                .values(
                    canonical_id=canonical_id,
                    identity_confidence=identity_confidence,
                    last_seen_at=now,
                )
            ),
        )
        return bool(touched.rowcount)
