"""End-to-end: the machines and models pages, Phase 2's "and the UI shows it" half.

The Phase 2 goal is "the database exists, migrates, **and the UI shows a real (empty)
machines/models page backed by real tables**". Empty is the state these pages spend Phase 2 in —
machines fill at Phase 4, models at Phase 3 — so the empty state is the acceptance criterion here,
not a placeholder for one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from freeweight.config import load_settings
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.infrastructure.db.repositories.machines import MachineRepository
from freeweight.infrastructure.db.repositories.models import ModelRepository
from freeweight.infrastructure.db.session import session_factory, session_scope
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.web.app import create_app

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client against an app whose database exists and is migrated to head.

    Entered as a context manager so the application's lifespan runs — that is what creates the
    ``Database`` handle the routes read through, and disposes it afterwards. A ``TestClient`` used
    without ``with`` never starts the lifespan, so it would exercise an application that has no
    database handle: not the thing the server actually is.
    """
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    # Phase 3's health check reaches a real provider; pinned to the fake here (testing standards
    # §1: e2e runs "through HTTP and CLI" against it) so these page tests stay deterministic.
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_machines_page_renders_its_empty_state(client: TestClient) -> None:
    response = client.get("/machines")

    assert response.status_code == 200
    assert "No machines yet" in response.text
    assert "Phase 4" in response.text


def test_models_page_renders_its_empty_state(client: TestClient) -> None:
    response = client.get("/models")

    assert response.status_code == 200
    assert "No models yet" in response.text


def test_models_page_renders_a_real_row(client: TestClient, tmp_path: Path) -> None:
    """Backed by the real table: a row written through the repository shows up on the page."""
    engine = create_engine_for(f"sqlite:///{tmp_path / 'freeweight.sqlite3'}")
    try:
        with session_scope(session_factory(engine)) as session:
            ModelRepository().upsert_identity(
                session,
                provider_kind="ollama",
                provider_model_name="llama3:8b",
                artifact_digest="sha256:" + "a" * 64,
                canonical_id="ollama/llama3:8b@sha256:" + "a" * 64,
                identity_confidence="digest",
                now=NOW,
            )
    finally:
        engine.dispose()

    response = client.get("/models")

    assert response.status_code == 200
    assert "llama3:8b" in response.text
    assert "No models yet" not in response.text
    # The suite's own RFC 3339 rendering (baseaicore.to_rfc3339), millisecond precision and all.
    assert "2026-08-26T12:00:00.000Z" in response.text


def test_machines_page_renders_a_real_row(client: TestClient, tmp_path: Path) -> None:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'freeweight.sqlite3'}")
    try:
        with session_scope(session_factory(engine)) as session:
            MachineRepository().upsert(
                session,
                machine_fingerprint="fp-abc",
                hostname="workstation",
                os_name="Linux",
                os_version="6.8",
                kernel="6.8.0",
                architecture="x86_64",
                cpu_model="Ryzen 9",
                physical_cores=8,
                logical_cores=16,
                ram_bytes=34359738368,
                gpus_json=None,
                storage_json=None,
                python_version="3.13.1",
                now=NOW,
            )
    finally:
        engine.dispose()

    response = client.get("/machines")

    assert response.status_code == 200
    assert "workstation" in response.text
    assert "32.0 GiB" in response.text


def test_an_unreported_reading_shows_an_em_dash_not_a_zero(
    client: TestClient, tmp_path: Path
) -> None:
    """UI standards §3: unavailable readings show ``—``, never ``0`` (ADR-0016's spirit)."""
    engine = create_engine_for(f"sqlite:///{tmp_path / 'freeweight.sqlite3'}")
    try:
        with session_scope(session_factory(engine)) as session:
            MachineRepository().upsert(
                session,
                machine_fingerprint="fp-sparse",
                hostname=None,
                os_name=None,
                os_version=None,
                kernel=None,
                architecture=None,
                cpu_model=None,
                physical_cores=None,
                logical_cores=None,
                ram_bytes=None,
                gpus_json=None,
                storage_json=None,
                python_version=None,
                now=NOW,
            )
    finally:
        engine.dispose()

    response = client.get("/machines")

    assert "—" in response.text
    assert ">0<" not in response.text


def test_pages_report_the_error_state_when_the_database_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UI standards §6: every view designs its error state, and says what to do next."""
    monkeypatch.setenv(
        "FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'never-migrated.sqlite3'}"
    )
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as client:
        response = client.get("/models")

    assert response.status_code == 503
    assert "could not be read" in response.text
    assert "freeweight db upgrade" in response.text


def test_every_page_carries_navigation_and_a_skip_link(client: TestClient) -> None:
    """UI standards §7: a skip-to-content link on every page."""
    for path in ("/", "/machines", "/models"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert 'class="skip-link"' in response.text, path
        assert 'href="/models"' in response.text, path


def test_the_application_owns_one_database_handle_for_its_whole_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One engine per server, not one per request.

    An engine is a connection pool plus SQLAlchemy's compiled-statement cache; rebuilding it per
    request discards both every time. This pins the lifecycle: the handle exists while the
    application serves, is the *same* handle across requests, and is disposed at shutdown.
    """
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    app = create_app(loaded.settings)

    assert app.state.database is None, "create_app must not open anything"

    with TestClient(app, base_url="http://127.0.0.1") as client:
        handle = app.state.database
        assert handle is not None
        client.get("/models")
        client.get("/machines")
        assert app.state.database is handle, "a second request must not rebuild the engine"
        pool = handle.engine.pool

    assert app.state.database is None
    # dispose() replaces the pool rather than emptying it, so a new pool object is the observable
    # signal that shutdown actually released the old one.
    assert handle.engine.pool is not pool


def test_the_health_endpoint_reports_on_the_handle_it_serves_from(client: TestClient) -> None:
    """Checking a different connection than requests use answers a question nobody asked."""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["components"][0]["name"] == "database"
    assert body["components"][0]["status"] == "ok"
