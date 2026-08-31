"""Integration tests for the machines and models repositories, on both supported dialects.

Uses the migrated schema rather than ``Base.metadata.create_all`` — the migration *is* the schema
in this application (database standards §5), so a repository test that bypassed it could pass
against a schema the migration does not actually produce.

The ``engine`` fixture is parametrized over SQLite and PostgreSQL (``conftest.py``). That is not
symmetry for its own sake: ``upsert()``'s ``ON CONFLICT`` has a separate branch per dialect, and
``ModelRepository._touch_or_insert``'s savepoint-and-retry exists specifically for PostgreSQL's
MVCC concurrency — on SQLite alone, neither is ever executed by anything.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from weightsdb import session_factory, session_scope

from freeweight.infrastructure.db.models import Model
from freeweight.infrastructure.db.repositories.machines import MachineRepository
from freeweight.infrastructure.db.repositories.models import ModelRepository

DIGEST = "sha256:" + "a" * 64

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 26, 13, 0, 0, tzinfo=UTC)


class TestMachineRepository:
    def test_upsert_creates_a_new_machine(self, engine: Engine) -> None:
        repo = MachineRepository()
        factory = session_factory(engine)
        with session_scope(factory) as session:
            machine = repo.upsert(
                session,
                machine_fingerprint="fp-1",
                hostname="box1",
                os_name="Linux",
                os_version="Ubuntu 26.04",
                kernel="6.9",
                architecture="x86_64",
                cpu_model="Ryzen 9",
                physical_cores=16,
                logical_cores=32,
                ram_bytes=64 * 1024**3,
                gpus_json=[],
                storage_json=[],
                python_version="3.13.0",
                now=NOW,
            )

        assert machine.machine_fingerprint == "fp-1"
        assert machine.first_seen_at == NOW
        assert machine.last_seen_at == NOW

    def test_upsert_touches_last_seen_at_without_moving_first_seen_at(self, engine: Engine) -> None:
        repo = MachineRepository()
        factory = session_factory(engine)

        def _upsert(session: Session, now: datetime) -> None:
            repo.upsert(
                session,
                machine_fingerprint="fp-1",
                hostname="box1",
                os_name="Linux",
                os_version="Ubuntu 26.04",
                kernel="6.9",
                architecture="x86_64",
                cpu_model="Ryzen 9",
                physical_cores=16,
                logical_cores=32,
                ram_bytes=64 * 1024**3,
                gpus_json=[],
                storage_json=[],
                python_version="3.13.0",
                now=now,
            )

        with session_scope(factory) as session:
            _upsert(session, NOW)
        with session_scope(factory) as session:
            _upsert(session, LATER)

        with session_scope(factory) as session:
            machine = repo.get_by_fingerprint(session, "fp-1")

        assert machine is not None
        assert machine.first_seen_at == NOW
        assert machine.last_seen_at == LATER

    def test_get_by_fingerprint_returns_none_when_absent(self, engine: Engine) -> None:
        repo = MachineRepository()
        factory = session_factory(engine)
        with session_scope(factory) as session:
            assert repo.get_by_fingerprint(session, "does-not-exist") is None


class TestModelRepository:
    def _upsert(
        self,
        repo: ModelRepository,
        session: Session,
        *,
        artifact_digest: str | None,
        now: datetime,
        canonical_id: str = "ollama/qwen3.5:9b@unknown",
    ) -> Model:
        return repo.upsert_identity(
            session,
            provider_kind="ollama",
            provider_model_name="qwen3.5:9b",
            artifact_digest=artifact_digest,
            canonical_id=canonical_id,
            identity_confidence="digest" if artifact_digest else "name_only",
            now=now,
        )

    def test_name_only_sighting_creates_one_row(self, engine: Engine) -> None:
        repo = ModelRepository()
        factory = session_factory(engine)
        with session_scope(factory) as session:
            model = self._upsert(repo, session, artifact_digest=None, now=NOW)

        assert model.identity_confidence == "name_only"
        assert model.artifact_digest is None

        with session_scope(factory) as session:
            all_models = repo.list_all(session)
        assert len(all_models) == 1

    def test_repeated_name_only_sighting_touches_the_same_row(self, engine: Engine) -> None:
        repo = ModelRepository()
        factory = session_factory(engine)
        with session_scope(factory) as session:
            self._upsert(repo, session, artifact_digest=None, now=NOW)
        with session_scope(factory) as session:
            self._upsert(repo, session, artifact_digest=None, now=LATER)

        with session_scope(factory) as session:
            all_models = repo.list_all(session)

        assert len(all_models) == 1
        assert all_models[0].last_seen_at == LATER

    def test_digest_arriving_later_upgrades_the_name_only_row_in_place(
        self, engine: Engine
    ) -> None:
        """Data model §2 `models`: a name_only row is upgraded, never duplicated."""
        repo = ModelRepository()
        factory = session_factory(engine)
        with session_scope(factory) as session:
            name_only = self._upsert(repo, session, artifact_digest=None, now=NOW)
            original_id = name_only.id

        digest = "sha256:" + "a" * 64
        with session_scope(factory) as session:
            digested = self._upsert(
                repo,
                session,
                artifact_digest=digest,
                now=LATER,
                canonical_id=f"ollama/qwen3.5:9b@sha256:{'a' * 12}",
            )

        assert digested.id == original_id
        assert digested.artifact_digest == digest
        assert digested.identity_confidence == "digest"
        assert digested.last_seen_at == LATER

        with session_scope(factory) as session:
            all_models = repo.list_all(session)
        assert len(all_models) == 1

    def test_changed_digest_creates_a_new_identity_and_keeps_the_old_one(
        self, engine: Engine
    ) -> None:
        """A model already pinned to one digest that is later reported with a *different* digest
        is a distinct identity — the old row's history must not be rewritten (canonical model
        identity §7)."""
        repo = ModelRepository()
        factory = session_factory(engine)
        digest_a = "sha256:" + "a" * 64
        digest_b = "sha256:" + "b" * 64

        with session_scope(factory) as session:
            first = self._upsert(repo, session, artifact_digest=digest_a, now=NOW)
            first_id = first.id

        with session_scope(factory) as session:
            second = self._upsert(repo, session, artifact_digest=digest_b, now=LATER)

        assert second.id != first_id
        assert second.artifact_digest == digest_b

        with session_scope(factory) as session:
            all_models = repo.list_all(session)
            original = repo.get_by_id(session, first_id)

        assert len(all_models) == 2
        assert original is not None
        assert original.artifact_digest == digest_a
        assert original.last_seen_at == NOW

    def test_repeated_digest_sighting_touches_the_same_row(self, engine: Engine) -> None:
        repo = ModelRepository()
        factory = session_factory(engine)
        digest = "sha256:" + "c" * 64
        with session_scope(factory) as session:
            first = self._upsert(repo, session, artifact_digest=digest, now=NOW)
            first_id = first.id
        with session_scope(factory) as session:
            second = self._upsert(repo, session, artifact_digest=digest, now=LATER)

        assert second.id == first_id
        assert second.last_seen_at == LATER

        with session_scope(factory) as session:
            all_models = repo.list_all(session)
        assert len(all_models) == 1

    def test_get_by_canonical_id_returns_none_when_absent(self, engine: Engine) -> None:
        repo = ModelRepository()
        factory = session_factory(engine)
        with session_scope(factory) as session:
            assert repo.get_by_canonical_id(session, "ollama/does-not-exist@unknown") is None

    def test_digest_then_no_digest_then_digest_again_does_not_collide(self, engine: Engine) -> None:
        """A provider that intermittently omits the digest must not crash discovery.

        Ollama does exactly this: report a digest, report none for a while, then report it again.
        The middle sighting creates a ``name_only`` row alongside the existing digest row, and the
        third sighting used to drive that ``name_only`` row straight into the digest row already
        holding ``uq_models_identity_triple`` — a raw ``IntegrityError`` out of the repository,
        on the sequence Phase 3's discovery loop produces first.
        """
        repo = ModelRepository()
        factory = session_factory(engine)

        self._sight(repo, factory, DIGEST, NOW)
        self._sight(repo, factory, None, NOW)
        model = self._sight(repo, factory, DIGEST, LATER)

        assert model.artifact_digest == DIGEST
        assert model.last_seen_at == LATER
        with session_scope(factory) as session:
            rows = repo.list_all(session)
        # Two identities, deliberately: the digest row, and the name_only row the middle sighting
        # legitimately recorded. Merging them away would delete real sightings.
        assert len(rows) == 2
        assert sorted(row.identity_confidence for row in rows) == ["digest", "name_only"]

    def test_first_seen_at_comes_from_the_injected_clock(self, engine: Engine) -> None:
        """Not wall-clock time: a row nobody can assert on is a row nobody can test."""
        repo = ModelRepository()
        factory = session_factory(engine)

        model = self._sight(repo, factory, DIGEST, NOW)

        assert model.first_seen_at == NOW

    @staticmethod
    def _sight(
        repo: ModelRepository,
        factory: sessionmaker[Session],
        digest: str | None,
        now: datetime,
    ) -> Model:
        with session_scope(factory) as session:
            return repo.upsert_identity(
                session,
                provider_kind="ollama",
                provider_model_name="llama3:8b",
                artifact_digest=digest,
                canonical_id=f"ollama/llama3:8b@{digest or 'name-only'}",
                identity_confidence="digest" if digest else "name_only",
                now=now,
            )


def test_on_delete_restrict_prevents_removing_a_model_with_descriptors(engine: Engine) -> None:
    """Database standards §3: a reference that must not orphan history uses ``ON DELETE
    RESTRICT``. Deleting results must never delete the model itself (§8)."""
    from sqlalchemy.exc import IntegrityError

    from freeweight.infrastructure.db.models import ModelDescriptor

    repo = ModelRepository()
    factory = session_factory(engine)
    with session_scope(factory) as session:
        model = repo.upsert_identity(
            session,
            provider_kind="ollama",
            provider_model_name="qwen3.5:9b",
            artifact_digest=None,
            canonical_id="ollama/qwen3.5:9b@unknown",
            identity_confidence="name_only",
            now=NOW,
        )
        model_id = model.id
        session.add(
            ModelDescriptor(
                model_id=model_id,
                observed_at=NOW,
                descriptor_hash="deadbeef",
            )
        )

    # pytest.raises must be the outer context manager: session_scope's own except/rollback needs
    # to see the IntegrityError before it is swallowed, or session_scope's success-path commit()
    # runs against a session SQLAlchemy has already deactivated after the failed flush.
    with pytest.raises(IntegrityError), session_scope(factory) as session:
        session.delete(session.get(Model, model_id))
        session.flush()
