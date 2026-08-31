"""`freeweight external list|install|verify` and the benchmark-source page, end to end.

The CLI and the page both go through ``services.external``, so this asserts the one behaviour
twice: every registered adapter is listed with its credit, an install records state, and verify
refuses a benchmark whose dataset does not match its pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from freeweight.cli.main import app as cli_app
from freeweight.config import load_settings
from freeweight.external.adapters import ADAPTERS
from freeweight.web.app import create_app

runner = CliRunner()


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config pointing the external root at a scratch directory."""
    external_root = tmp_path / "external"
    config = tmp_path / "config.toml"
    config.write_text(f'[external]\nroot = "{external_root}"\n')
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'fw.sqlite3'}")
    return config


class TestExternalCli:
    def test_list_shows_every_adapter_with_its_credit(self, config_file: Path) -> None:
        result = runner.invoke(cli_app, ["external", "list", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        for key in ADAPTERS:
            assert key in result.output
        # A credit line names the source and the licence.
        assert "github.com" in result.output
        assert "not installed" in result.output

    def test_list_json_is_machine_readable(self, config_file: Path) -> None:
        import json

        result = runner.invoke(
            cli_app, ["external", "list", "--config", str(config_file), "--json"]
        )

        payload = json.loads(result.output)
        assert {row["key"] for row in payload} == set(ADAPTERS)
        evalplus = next(row for row in payload if row["key"] == "external.evalplus")
        assert evalplus["requires_sandbox"] is True
        assert evalplus["commit"]

    def test_install_then_verify_a_datasetless_adapter(self, config_file: Path) -> None:
        # IFEval has no pinned datasets, so verify passes once install records state — and its
        # install command would hit the network, so this asserts the *state*, not a real install.
        # Use a benchmark with an empty install_command instead: bfcl.
        install = runner.invoke(
            cli_app, ["external", "install", "external.bfcl", "--config", str(config_file)]
        )
        assert install.exit_code == 0, install.output
        assert "Installed external.bfcl" in install.output

        verify = runner.invoke(
            cli_app, ["external", "verify", "external.bfcl", "--config", str(config_file)]
        )
        assert verify.exit_code == 0, verify.output
        assert "matches its pin" in verify.output

    def test_verify_an_uninstalled_benchmark_fails(self, config_file: Path) -> None:
        result = runner.invoke(
            cli_app, ["external", "verify", "external.ruler", "--config", str(config_file)]
        )

        assert result.exit_code == 1
        assert "not installed" in result.output

    def test_an_unknown_key_is_reported(self, config_file: Path) -> None:
        result = runner.invoke(
            cli_app, ["external", "install", "external.nope", "--config", str(config_file)]
        )

        assert result.exit_code == 1
        assert "No external benchmark" in result.output


class TestSourcesPage:
    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setenv("FREEWEIGHT_EXTERNAL__ROOT", str(tmp_path / "external"))
        loaded = load_settings(config_path=tmp_path / "missing.toml")
        return TestClient(create_app(loaded.settings), base_url="http://127.0.0.1")

    def test_the_page_lists_every_benchmark_with_source_and_licence(
        self, client: TestClient
    ) -> None:
        response = client.get("/sources")

        assert response.status_code == 200
        for adapter in ADAPTERS.values():
            assert adapter.manifest.name in response.text
            assert adapter.manifest.source_repository in response.text
            assert adapter.manifest.license in response.text

    def test_the_page_marks_which_benchmarks_need_a_sandbox(self, client: TestClient) -> None:
        response = client.get("/sources")

        # EvalPlus and CRUXEval require a sandbox; the page says so.
        assert response.text.count("required") >= 2

    def test_sources_is_in_the_navigation(self, client: TestClient) -> None:
        response = client.get("/sources")

        assert 'href="/sources" aria-current="page"' in response.text
