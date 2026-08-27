"""End-to-end: Phase 3's model discovery journey, through both HTTP and CLI (testing standards §1).

Acceptance criteria (development plan, Phase 3):

1. With the provider up, ``freeweight models refresh``/``POST /models/discover`` discovers and
   persists every model with its digest, and the UI lists them with quantization, parameters and
   context.
2. FreeWeight contains no provider HTTP code (asserted separately, in
   ``tests/unit/test_provider_factory.py``).
3. With the provider down, the page and CLI still work and say why the data is stale.

Every journey here runs against :class:`~modelrack.testing.FakeProvider` — the running application,
not just its unit tests, needs no GPU, no Ollama and no network.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from freeweight.cli.main import app as cli_app
from freeweight.config import load_settings
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.web.app import create_app

runner = CliRunner()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A migrated app served against a fake provider with one model in its catalogue."""
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


def test_http_discover_then_list_then_detail(client: TestClient) -> None:
    discover_response = client.post("/models/discover", follow_redirects=False)
    assert discover_response.status_code == 303
    assert discover_response.headers["location"] == "/models"

    list_response = client.get("/models")
    assert list_response.status_code == 200
    assert "No models yet" not in list_response.text
    assert "Last refresh" in list_response.text
    assert "added 1" in list_response.text

    health = client.get("/api/v1/health").json()
    provider_component = next(c for c in health["components"] if c["name"] == "provider")
    assert provider_component["status"] == "ok"

    # The list links to the detail page by the application-local ULID, never the canonical ID
    # (ADR-0024: never a path segment).
    href_match = re.search(r'href="(/models/[A-Za-z0-9]+)"', list_response.text)
    assert href_match is not None
    detail_response = client.get(href_match.group(1))
    assert detail_response.status_code == 200
    assert "Latest descriptor" in detail_response.text


def test_http_detail_page_404s_on_an_unknown_reference(client: TestClient) -> None:
    response = client.get("/models/does-not-exist")

    assert response.status_code == 404
    assert "does-not-exist" in response.text


def test_http_discover_is_safe_to_run_twice(client: TestClient) -> None:
    client.post("/models/discover")
    second = client.post("/models/discover", follow_redirects=False)

    assert second.status_code == 303
    assert "unchanged 1" in client.get("/models").text


def test_http_models_page_survives_a_provider_that_cannot_be_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page never crashes; it says the data may be stale rather than pretending to be fresh."""
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__BASE_URL", "http://127.0.0.1:1")  # nothing listens
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")

    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as client:
        discover_response = client.post("/models/discover", follow_redirects=False)
        assert discover_response.status_code == 303

        list_response = client.get("/models")
        assert list_response.status_code == 200
        assert "Data may be stale" in list_response.text

        health = client.get("/api/v1/health").json()

    provider_component = next(c for c in health["components"] if c["name"] == "provider")
    assert provider_component["status"] == "unavailable"
    # An unreachable provider is optional: it degrades the application, never takes it down
    # (Graceful Degradation §3).
    assert health["status"] == "degraded"


def test_cli_refresh_then_list_then_show(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    assert runner.invoke(cli_app, ["db", "upgrade"]).exit_code == 0

    refresh_result = runner.invoke(cli_app, ["models", "refresh", "--json"])
    assert refresh_result.exit_code == 0, refresh_result.output
    refreshed = json.loads(refresh_result.output)
    assert refreshed == {"added": 1, "updated": 0, "unchanged": 0, "total": 1}

    list_result = runner.invoke(cli_app, ["models", "list", "--json"])
    assert list_result.exit_code == 0, list_result.output
    listed = json.loads(list_result.output)
    assert len(listed["models"]) == 1
    assert listed["last_discovery"]["ok"] is True

    model_id = listed["models"][0]["id"]
    show_result = runner.invoke(cli_app, ["models", "show", model_id, "--json"])
    assert show_result.exit_code == 0, show_result.output
    shown = json.loads(show_result.output)
    assert shown["id"] == model_id
    assert shown["latest_descriptor"] is not None


def test_cli_text_output_for_list_show_and_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plain-text mode of every command, not just ``--json`` (testing standards §5)."""
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    assert runner.invoke(cli_app, ["db", "upgrade"]).exit_code == 0

    refresh_result = runner.invoke(cli_app, ["models", "refresh"])
    assert refresh_result.exit_code == 0, refresh_result.output
    assert "1 added" in refresh_result.output

    list_result = runner.invoke(cli_app, ["models", "list"])
    assert list_result.exit_code == 0, list_result.output
    assert "digest" in list_result.output

    model_id = list_result.output.split()[0]
    show_result = runner.invoke(cli_app, ["models", "show", model_id])
    assert show_result.exit_code == 0, show_result.output
    assert "identity confidence: digest" in show_result.output
    assert "descriptor snapshots: 1" in show_result.output


def test_cli_show_of_an_unknown_reference_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    assert runner.invoke(cli_app, ["db", "upgrade"]).exit_code == 0

    result = runner.invoke(cli_app, ["models", "show", "no-such-model"])

    assert result.exit_code == 2
    assert "MODEL_NOT_FOUND" in result.output


def test_cli_show_falling_back_to_an_unreachable_provider_exits_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__BASE_URL", "http://127.0.0.1:1")
    assert runner.invoke(cli_app, ["db", "upgrade"]).exit_code == 0

    result = runner.invoke(cli_app, ["models", "show", "nothing-stored"])

    assert result.exit_code == 4
    assert "PROVIDER_UNAVAILABLE" in result.output


def test_cli_commands_report_a_bad_config_file_as_exit_3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad_config = tmp_path / "broken.toml"
    bad_config.write_text("this is not valid toml [[[")

    for command in (["models", "list"], ["models", "show", "x"], ["models", "refresh"]):
        result = runner.invoke(cli_app, [*command, "--config", str(bad_config)])
        assert result.exit_code == 3, (command, result.output)
        assert "CONFIGURATION_ERROR" in result.output


def test_cli_list_reports_a_failed_refresh_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__BASE_URL", "http://127.0.0.1:1")
    assert runner.invoke(cli_app, ["db", "upgrade"]).exit_code == 0

    refresh_result = runner.invoke(cli_app, ["models", "refresh"])
    assert refresh_result.exit_code == 4

    list_result = runner.invoke(cli_app, ["models", "list"])
    assert list_result.exit_code == 0, list_result.output
    assert "last refresh attempt" in list_result.output
    assert "No models yet" in list_result.output
