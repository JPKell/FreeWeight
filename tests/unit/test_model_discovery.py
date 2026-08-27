"""Unit tests for freeweight.services.models: discovery through ModelRack.

Phase 3's own test list (development plan): discovery against ``FakeProvider`` with 0, 1 and 20
models; digest presence driving identity confidence; idempotent re-discovery; a changed digest
creating a new identity while keeping the old one; and an alias resolution recorded, not hidden.
Every test here runs with no GPU, no Ollama and no network — the whole suite is exercised against
:class:`~modelrack.testing.FakeProvider`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from baseaicore import ValidationError
from modelrack import ProviderStatus
from modelrack.errors import ModelNotFound
from modelrack.testing import FakeModel, FakeProvider, FakeScript

from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.infrastructure.db.models import Model
from freeweight.infrastructure.db.session import session_scope
from freeweight.services.database import MIGRATIONS_LOCATION, Database
from freeweight.services.inventory import list_models
from freeweight.services.models import (
    compute_descriptor_hash,
    discover_models,
    get_last_discovery,
    get_model_detail,
)

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 27, 13, 0, 0, tzinfo=UTC)
EVEN_LATER = datetime(2026, 8, 27, 14, 0, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    """A migrated, throwaway SQLite database — real storage, no provider (testing standards §7)."""
    engine = create_engine_for(f"sqlite:///{tmp_path / 'freeweight.sqlite3'}")
    MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    handle = Database(engine)
    try:
        yield handle
    finally:
        handle.close()


def _provider(
    *models: FakeModel, health_status: ProviderStatus = ProviderStatus.OK
) -> FakeProvider:
    return FakeProvider(FakeScript(models=models, health_status=health_status))


class TestDiscoveryCounts:
    def test_zero_models_is_not_a_failure(self, database: Database) -> None:
        outcome = discover_models(database, _provider(), now=NOW)

        assert (outcome.added, outcome.updated, outcome.unchanged, outcome.total) == (0, 0, 0, 0)
        assert list_models(database) == ()

    def test_one_model_is_added_and_persisted_with_its_digest(self, database: Database) -> None:
        provider = _provider(FakeModel(name="qwen3.5:9b-q8_0", digest=DIGEST_A, family="qwen3.5"))

        outcome = discover_models(database, provider, now=NOW)

        assert (outcome.added, outcome.updated, outcome.unchanged, outcome.total) == (1, 0, 0, 1)
        (model,) = list_models(database)
        assert model.provider_model_name == "qwen3.5:9b-q8_0"
        assert model.artifact_digest == DIGEST_A
        assert model.identity_confidence == "digest"
        assert model.canonical_id.startswith("fake/qwen3.5:9b-q8_0@sha256:")

    def test_twenty_models_are_all_discovered(self, database: Database) -> None:
        models = tuple(FakeModel(name=f"model-{i}", digest=None) for i in range(20))
        provider = _provider(*models)

        outcome = discover_models(database, provider, now=NOW)

        assert outcome.total == 20
        assert outcome.added == 20
        assert len(list_models(database)) == 20


class TestIdentityConfidence:
    def test_digest_present_yields_digest_confidence(self, database: Database) -> None:
        provider = _provider(FakeModel(name="a", digest=DIGEST_A))

        discover_models(database, provider, now=NOW)

        (model,) = list_models(database)
        assert model.identity_confidence == "digest"
        assert model.artifact_digest == DIGEST_A

    def test_absent_digest_yields_name_only_confidence(self, database: Database) -> None:
        provider = _provider(FakeModel(name="a", digest=None))

        discover_models(database, provider, now=NOW)

        (model,) = list_models(database)
        assert model.identity_confidence == "name_only"
        assert model.artifact_digest is None


class TestRediscovery:
    def test_rediscovery_with_nothing_changed_is_idempotent(self, database: Database) -> None:
        provider = _provider(FakeModel(name="a", digest=DIGEST_A, family="qwen3.5", layers=32))

        first = discover_models(database, provider, now=NOW)
        second = discover_models(database, provider, now=LATER)

        assert (first.added, first.updated, first.unchanged) == (1, 0, 0)
        assert (second.added, second.updated, second.unchanged) == (0, 0, 1)
        (model,) = list_models(database)
        # last_seen_at moved; the identity and its one descriptor snapshot did not multiply.
        assert model.last_seen_at == LATER
        with session_scope(database.sessions, read_only=True) as session:
            from freeweight.infrastructure.db.repositories.model_descriptors import (
                ModelDescriptorRepository,
            )

            history = ModelDescriptorRepository().history_for_model(session, model.id)
        assert len(history) == 1

    def test_changed_digest_creates_a_new_identity_and_keeps_the_old_one(
        self, database: Database
    ) -> None:
        discover_models(database, _provider(FakeModel(name="a", digest=DIGEST_A)), now=NOW)
        discover_models(database, _provider(FakeModel(name="a", digest=DIGEST_B)), now=LATER)

        models = {model.artifact_digest: model for model in list_models(database)}
        assert set(models) == {DIGEST_A, DIGEST_B}
        assert models[DIGEST_A].last_seen_at == NOW  # untouched by the second run
        assert models[DIGEST_B].last_seen_at == LATER

    def test_name_only_identity_upgrades_in_place_and_counts_as_updated(
        self, database: Database
    ) -> None:
        first = discover_models(database, _provider(FakeModel(name="a", digest=None)), now=NOW)
        second = discover_models(
            database, _provider(FakeModel(name="a", digest=DIGEST_A)), now=LATER
        )

        assert (first.added, first.updated) == (1, 0)
        assert (second.added, second.updated, second.unchanged) == (0, 1, 0)
        (model,) = list_models(database)
        assert model.identity_confidence == "digest"
        assert model.artifact_digest == DIGEST_A

    def test_a_real_content_change_is_reported_as_updated_and_snapshotted(
        self, database: Database
    ) -> None:
        discover_models(
            database, _provider(FakeModel(name="a", digest=DIGEST_A, quantization="Q4_0")), now=NOW
        )
        outcome = discover_models(
            database,
            _provider(FakeModel(name="a", digest=DIGEST_A, quantization="Q8_0")),
            now=LATER,
        )

        assert (outcome.added, outcome.updated, outcome.unchanged) == (0, 1, 0)
        with session_scope(database.sessions, read_only=True) as session:
            from freeweight.infrastructure.db.repositories.model_descriptors import (
                ModelDescriptorRepository,
            )
            from freeweight.infrastructure.db.repositories.models import ModelRepository

            model = ModelRepository().get_by_identity(
                session, provider_kind="fake", provider_model_name="a", artifact_digest=DIGEST_A
            )
            assert model is not None
            history = ModelDescriptorRepository().history_for_model(session, model.id)
        assert len(history) == 2
        assert history[0].quantization == "Q8_0"
        assert history[1].quantization == "Q4_0"


class TestProviderUnavailable:
    def test_discovery_records_the_failure_and_raises_rather_than_crashing(
        self, database: Database
    ) -> None:
        from modelrack.errors import ProviderUnavailable

        provider = _provider(health_status=ProviderStatus.UNAVAILABLE)

        with pytest.raises(ProviderUnavailable):
            discover_models(database, provider, now=NOW)

        last = get_last_discovery(database)
        assert last is not None
        assert last.ok is False
        assert last.checked_at == NOW
        assert "scripted unavailable" in last.detail

    def test_get_last_discovery_is_none_before_any_attempt(self, database: Database) -> None:
        assert get_last_discovery(database) is None

    def test_a_successful_run_after_a_failed_one_replaces_the_record(
        self, database: Database
    ) -> None:
        from modelrack.errors import ProviderUnavailable

        with pytest.raises(ProviderUnavailable):
            discover_models(database, _provider(health_status=ProviderStatus.UNAVAILABLE), now=NOW)

        discover_models(database, _provider(FakeModel(name="a", digest=DIGEST_A)), now=LATER)

        last = get_last_discovery(database)
        assert last is not None
        assert last.ok is True
        assert last.added == 1


class TestModelDetailAndAliasResolution:
    def test_alias_resolution_is_recorded_not_hidden(self, database: Database) -> None:
        provider = _provider(
            FakeModel(name="qwen3.5:9b-q8_0", digest=DIGEST_A, aliases=("qwen3.5:latest",))
        )
        discover_models(database, provider, now=NOW)

        detail = get_model_detail(database, provider, "qwen3.5:latest", now=LATER)

        assert detail.provider_model_name == "qwen3.5:9b-q8_0"
        assert detail.resolved_alias == "qwen3.5:latest"
        assert {"alias": "qwen3.5:latest", "resolved_at": detail.aliases[0]["resolved_at"]} == (
            detail.aliases[0]
        )

        # Recorded for good, not just returned once.
        again = get_model_detail(database, provider, "qwen3.5:9b-q8_0", now=LATER)
        assert again.aliases == detail.aliases

        # Re-resolving the same alias moves its timestamp rather than duplicating the entry.
        resolved_again = get_model_detail(database, provider, "qwen3.5:latest", now=EVEN_LATER)
        assert len(resolved_again.aliases) == 1
        assert resolved_again.aliases[0]["resolved_at"] != detail.aliases[0]["resolved_at"]

    def test_lookup_by_stored_id_needs_no_provider_call(self, database: Database) -> None:
        provider = _provider(FakeModel(name="a", digest=DIGEST_A))
        discover_models(database, provider, now=NOW)
        (model,) = list_models(database)

        unavailable_provider = _provider(health_status=ProviderStatus.UNAVAILABLE)
        detail = get_model_detail(database, unavailable_provider, model.id, now=LATER)

        assert detail.id == model.id
        assert detail.resolved_alias is None

    def test_resolving_to_an_undiscovered_identity_names_the_fix(self, database: Database) -> None:
        provider = _provider(FakeModel(name="a", digest=DIGEST_A))

        with pytest.raises(ModelNotFound) as excinfo:
            get_model_detail(database, provider, "a", now=NOW)

        assert "models refresh" in excinfo.value.message

    def test_reference_matching_nothing_at_all_raises_model_not_found(
        self, database: Database
    ) -> None:
        provider = _provider(FakeModel(name="a", digest=DIGEST_A))
        discover_models(database, provider, now=NOW)

        with pytest.raises(ModelNotFound):
            get_model_detail(database, provider, "does-not-exist", now=LATER)

    def test_ambiguous_local_prefix_is_refused(self, database: Database) -> None:
        with session_scope(database.sessions) as session:
            session.add(
                Model(
                    id="01HZZZZZZZZZZZZZZZZZZZZZZA",
                    provider_kind="ollama",
                    provider_model_name="a",
                    artifact_digest=None,
                    canonical_id="ollama/a@unknown",
                    identity_confidence="name_only",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                )
            )
            session.add(
                Model(
                    id="01HZZZZZZZZZZZZZZZZZZZZZZB",
                    provider_kind="ollama",
                    provider_model_name="b",
                    artifact_digest=None,
                    canonical_id="ollama/b@unknown",
                    identity_confidence="name_only",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                )
            )

        with pytest.raises(ValidationError) as excinfo:
            get_model_detail(database, _provider(), "01HZZZZZZZZZZZZZZZZZZZZZZ", now=NOW)

        assert len(excinfo.value.details["candidates"]) == 2


class TestUnmigratedDatabase:
    """A never-migrated database is a driver failure to translate, not a crash to leak.

    Regression coverage: :func:`~freeweight.services.models.list_models_with_latest_descriptor`
    once bypassed the same translation
    :func:`~freeweight.services.inventory.list_models` applies to its own reads, so a route calling
    it against an unmigrated database saw a raw ``sqlalchemy.exc.OperationalError`` instead of the
    ``DatabaseError`` its ``except`` clause was written to catch.
    """

    @pytest.fixture
    def unmigrated_database(self, tmp_path: Path) -> Iterator[Database]:
        engine = create_engine_for(f"sqlite:///{tmp_path / 'freeweight.sqlite3'}")
        handle = Database(engine)
        try:
            yield handle
        finally:
            handle.close()

    def test_inventory_list_models_also_translates(self, unmigrated_database: Database) -> None:
        """The Phase 2 helper this module's own translation was modelled on, checked directly."""
        from freeweight.infrastructure.db.errors import DatabaseUnavailable

        with pytest.raises(DatabaseUnavailable):
            list_models(unmigrated_database)

    def test_list_raises_a_translated_error(self, unmigrated_database: Database) -> None:
        from freeweight.infrastructure.db.errors import DatabaseUnavailable
        from freeweight.services.models import list_models_with_latest_descriptor

        with pytest.raises(DatabaseUnavailable):
            list_models_with_latest_descriptor(unmigrated_database)

    def test_get_last_discovery_raises_a_translated_error(
        self, unmigrated_database: Database
    ) -> None:
        from freeweight.infrastructure.db.errors import DatabaseUnavailable

        with pytest.raises(DatabaseUnavailable):
            get_last_discovery(unmigrated_database)

    def test_get_model_detail_raises_a_translated_error(
        self, unmigrated_database: Database
    ) -> None:
        from freeweight.infrastructure.db.errors import DatabaseUnavailable

        with pytest.raises(DatabaseUnavailable):
            get_model_detail(unmigrated_database, _provider(), "anything", now=NOW)

    def test_get_model_detail_still_raises_model_not_found_on_a_real_database(
        self, database: Database
    ) -> None:
        """The translation added for the case above must not swallow ordinary domain errors."""
        with pytest.raises(ModelNotFound):
            get_model_detail(database, _provider(), "anything", now=NOW)


class TestDescriptorHash:
    def test_unchanged_between_two_reads_of_the_same_content(self) -> None:
        provider = _provider(FakeModel(name="a", digest=DIGEST_A, family="qwen3.5", layers=32))

        first, second = provider.list_models(), provider.list_models()

        assert compute_descriptor_hash(first[0]) == compute_descriptor_hash(second[0])

    def test_changes_when_a_hashed_field_changes(self) -> None:
        provider_a = _provider(FakeModel(name="a", digest=DIGEST_A, layers=32))
        provider_b = _provider(FakeModel(name="a", digest=DIGEST_A, layers=40))

        assert compute_descriptor_hash(provider_a.list_models()[0]) != compute_descriptor_hash(
            provider_b.list_models()[0]
        )

    def test_unaffected_by_observed_at(self) -> None:
        provider = _provider(FakeModel(name="a", digest=DIGEST_A))

        descriptor = provider.list_models()[0]
        from dataclasses import replace

        shifted = replace(descriptor, observed_at=LATER)

        assert compute_descriptor_hash(descriptor) == compute_descriptor_hash(shifted)
