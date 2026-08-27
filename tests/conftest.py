"""Shared pytest fixtures: isolated XDG roots, a deterministic clock, and the run environment.

No test may read or write the developer's real config, data or state directories (Testing
Standards §9), so every test runs against a throwaway tree by default.

``run_environment`` (Phase 5) is the one piece of setup four run-engine test modules share.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolated_xdg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point every XDG directory at a throwaway tree and clear stray FREEWEIGHT_* variables."""
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    for path in (config_home, data_home, state_home):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.chdir(tmp_path)
    # Every FREEWEIGHT_* variable is the application's own configuration and must not leak in
    # from the developer's shell. Harness configuration (a PostgreSQL URL for the integration
    # suite) deliberately uses the FWTEST_ prefix instead, precisely so it survives this.
    for key in list(os.environ):
        if key.startswith("FREEWEIGHT_"):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def frozen_instant() -> datetime:
    """A fixed, timezone-aware UTC instant for deterministic timestamp assertions."""
    return datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RunEnvironment:
    """Everything a run-engine test needs, already wired and already discovered.

    Attributes:
        database: A handle on a migrated, empty database.
        database_url: Its URL, for a test that needs to open a second handle (recovery, which has
            to simulate a fresh process).
        provider: A :class:`~modelrack.testing.FakeProvider` over the test's script.
        collector: A GPU-less, host-less telemetry collector, so no test touches ``nvidia-smi``.
        registry: The benchmark registry under test.
        publisher: An event publisher over ``database``.
        model_ref: The canonical ID of the one discovered model, ready to pass to ``create_run``.
    """

    database: Any
    database_url: str
    provider: Any
    collector: Any
    registry: Any
    publisher: Any
    model_ref: str


@pytest.fixture
def run_environment(tmp_path: Path) -> Iterator[Callable[..., RunEnvironment]]:
    """Return a factory that builds a :class:`RunEnvironment`.

    Added for Phase 5: four of its test modules need the same six-line setup (migrate, build a
    fake provider, discover a model so a run has a descriptor snapshot to point at), and a
    duplicated setup is four places to get "why does this run fail to start" wrong.

    A factory rather than a plain fixture because most run-engine tests need to *script* the
    provider — a failing generation, a slow one, a specific text — and a fixture that took no
    arguments would force every one of them to rebuild the environment by hand anyway.

    Every import is inside the function: this module is loaded for the unit suite too, and
    ``--help``-fast imports matter less in tests than not paying for SQLAlchemy in a suite that
    does not use it.
    """
    from modelrack.testing import FakeProvider, FakeScript
    from sweatmeter import TelemetryCollector
    from sweatmeter.testing import NullGpuReader, NullHostReader

    from freeweight.infrastructure.db.engine import create_engine_for
    from freeweight.infrastructure.db.migration import MigrationRunner
    from freeweight.services.database import MIGRATIONS_LOCATION, Database
    from freeweight.services.events import RunEventPublisher
    from freeweight.services.models import discover_models
    from freeweight.services.runs import build_registry

    handles: list[Any] = []

    def build(
        *, script: Any = None, seed: int = 7, registry: Any = None, name: str = "run.sqlite3"
    ) -> RunEnvironment:
        url = f"sqlite:///{tmp_path / name}"
        engine = create_engine_for(url)
        try:
            MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        finally:
            engine.dispose()
        database = Database.from_url(url)
        handles.append(database)
        provider = FakeProvider(script if script is not None else FakeScript(), seed=seed)
        collector = TelemetryCollector(host=NullHostReader(), gpu=NullGpuReader())
        # A run points at a stored descriptor snapshot, so discovery has to have happened —
        # exactly as it does for a user, who refreshes models before benchmarking one.
        discover_models(database, provider, now=datetime.now(UTC))
        model = provider.list_models()[0]
        return RunEnvironment(
            database=database,
            database_url=url,
            provider=provider,
            collector=collector,
            registry=registry if registry is not None else build_registry(),
            publisher=RunEventPublisher(database),
            model_ref=model.identity.canonical_id,
        )

    try:
        yield build
    finally:
        for handle in handles:
            handle.close()
