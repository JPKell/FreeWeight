"""Shared pytest fixtures: isolated XDG roots and a deterministic clock.

No test may read or write the developer's real config, data or state directories (Testing
Standards §9), so every test runs against a throwaway tree by default.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

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
