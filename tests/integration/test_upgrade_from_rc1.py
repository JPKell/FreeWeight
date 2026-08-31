"""Upgrade from the only released tag (v1.0.0rc1) preserves real data, through the running app.

P14 asks for upgrade testing from every released version with real data preserved. v1.0.0rc1 is the
only released tag, and Phase 12's acceptance criterion forbade a new migration, so an rc1 database
is already at head — there is no forward migration to apply, which is itself the thing to prove.
This test goes past the migration check in ``test_migrations.py`` and boots the whole application
(WeightsDB, MirrorWall, the run engine, the API) against a copy of a real rc1 database, then reads
the rc1 rows back through the API — the honest "the new version opens last version's data and
serves it" test, not just "the schema matches".
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from freeweight.config import load_settings
from freeweight.services.database import Database, ensure_ready
from freeweight.web.app import create_app

RC1_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "databases" / "freeweight-1.0.0rc1.sqlite3"
)


@pytest.fixture
def rc1_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A writable copy of a real rc1 database, wired as the application's database."""
    working = tmp_path / "freeweight.sqlite3"
    shutil.copyfile(RC1_FIXTURE, working)
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{working}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    return working


def test_this_version_opens_an_rc1_database_with_no_migration(rc1_database: Path) -> None:
    with Database.from_url(f"sqlite:///{rc1_database}") as database:
        outcome = ensure_ready(database, auto_migrate=True)

    assert outcome is None, "an rc1 database must need no migration on this version"


def test_the_application_boots_on_an_rc1_database_and_serves_its_rows(rc1_database: Path) -> None:
    loaded = load_settings(config_path=rc1_database.parent / "missing.toml")

    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as client:
        # The whole stack came up (WeightsDB, MirrorWall, the scheduler) against last version's
        # database — health answers, which it could not if the boot had failed.
        health = client.get("/api/v1/health")
        assert health.status_code in (200, 503)  # 503 only if the provider is down, never the DB
        database_component = next(
            component
            for component in health.json()["components"]
            if component["name"] == "database"
        )
        assert database_component["status"] == "ok", "the rc1 database did not open cleanly"

        # The rc1 machine row (written by the real rc1 install that produced the fixture) is served
        # back through the API on the new version — data preserved across the upgrade.
        machines = client.get("/api/v1/machines").json()
        hostnames = {machine.get("hostname") for machine in machines.get("items", machines)}
        assert "rc1-fixture-host" in hostnames

        # The machines page renders on the new MirrorWall shell over the rc1 data.
        page = client.get("/machines")
        assert page.status_code == 200
        assert "rc1-fixture-host" in page.text
