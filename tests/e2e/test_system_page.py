"""End-to-end: the System page — the suite's help/about page (UI/UX standards §12).

Every other application has one; FreeWeight did not until now. The page renders no business logic
of its own — it reads through :func:`freeweight.services.health.get_health_report`, the identical
call ``GET /api/v1/health`` and ``freeweight doctor`` make — so this test pins the same contract
those surfaces already hold: the version string and every health component name are on the page.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from weightsdb import MigrationRunner, create_engine_for

from freeweight.__about__ import __version__
from freeweight.config import load_settings
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.services.health import get_health_report
from freeweight.web.app import create_app
from freeweight.web.rendering import NAV_ITEMS


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


def test_system_page_renders_the_version_and_every_health_component(client: TestClient) -> None:
    response = client.get("/system", headers={"accept": "text/html"})

    assert response.status_code == 200
    text = response.text
    assert __version__ in text
    report = get_health_report(settings=load_settings().settings)
    for component in report.components:
        assert component.name in text, component.name


def test_system_is_in_the_navigation(client: TestClient) -> None:
    assert any(item["key"] == "system" and item["href"] == "/system" for item in NAV_ITEMS)
    response = client.get("/machines")
    assert 'href="/system"' in response.text


def test_system_page_links_the_documentation(client: TestClient) -> None:
    text = client.get("/system").text
    assert "quickstart.md" in text
    assert "troubleshooting.md" in text
    assert "api.md" in text
