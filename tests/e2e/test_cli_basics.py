"""End-to-end: the Typer CLI surface for Phase 1."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from freeweight.cli.main import app

runner = CliRunner()


def test_serve_command_starts_uvicorn_with_resolved_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def _fake_run(target: str, **kwargs: object) -> None:
        calls.append({"target": target, **kwargs})

    monkeypatch.setattr("uvicorn.run", _fake_run)

    result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "9100"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["target"] == "freeweight.bootstrap:create_app_from_environment"
    assert calls[0]["factory"] is True
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 9100


def test_serve_command_reports_configuration_error_without_starting_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setenv("FREEWEIGHT_SERVER__HOST", "192.168.1.5")

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 3
    assert calls == []
    assert "INSECURE_BINDING" in result.output


def test_default_invocation_with_no_subcommand_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append((a, k)))

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_version_command_json() -> None:
    result = runner.invoke(app, ["version", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["application"]["name"] == "freeweight"
    assert payload["api"]["current"] == "v1"


def test_version_command_text() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.startswith("freeweight ")


def test_version_flag_on_root() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "freeweight" in result.output


def test_health_command_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """A freshly isolated environment has never run ``db upgrade``, so the ``database`` component
    honestly reports a pending migration rather than silently claiming ``ok``.

    ``provider.kind`` is pinned to ``fake`` (testing standards §1) so the ``provider`` component
    Phase 3 adds is deterministic rather than depending on this machine's real Ollama port.
    """
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")

    result = runner.invoke(app, ["health", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "degraded"
    # gpu_telemetry and machine (Phase 4) join database and provider; their own status reflects
    # this machine's real hardware, but a pending migration alone already makes the required
    # database component degrade the overall status, regardless of what they report.
    assert [component["name"] for component in payload["components"]] == [
        "database",
        "provider",
        "gpu_telemetry",
        "machine",
        "evidence",
    ]
    assert payload["components"][0]["status"] == "degraded"
    assert payload["components"][1]["status"] == "ok"


def test_health_command_reports_ok_after_db_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    upgrade_result = runner.invoke(app, ["db", "upgrade"])
    assert upgrade_result.exit_code == 0, upgrade_result.output

    result = runner.invoke(app, ["health", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    components_by_name = {component["name"]: component for component in payload["components"]}
    assert components_by_name["database"]["status"] == "ok"
    assert components_by_name["provider"]["status"] == "ok"
    # gpu_telemetry (Phase 4) reflects this machine's real hardware: "ok" with a GPU, "degraded"
    # without one — either is correct here, and the overall status follows suit (Graceful
    # Degradation §3), so neither is hardcoded to keep this test honest on a GPU-less runner.
    assert set(components_by_name) == {
        "database",
        "provider",
        "gpu_telemetry",
        "machine",
        "evidence",
    }
    assert components_by_name["evidence"]["status"] == "ok"
    assert components_by_name["evidence"]["detail"] == "no capability evidence yet"
    assert payload["status"] in ("ok", "degraded")


def test_doctor_command_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "status: degraded" in result.output
    assert "provider" in result.output
    assert "database" in result.output


def test_config_show_reports_source_of_every_value() -> None:
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert "server.host" in result.output
    assert "(default)" in result.output


def test_config_show_redacts_tokens(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[auth]\ntokens = ["super-secret-value"]\n', encoding="utf-8")

    result = runner.invoke(app, ["config", "show", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "super-secret-value" not in result.output
    assert "********" in result.output


def test_config_validate_succeeds_on_defaults() -> None:
    result = runner.invoke(app, ["config", "validate"])

    assert result.exit_code == 0


def test_config_validate_fails_on_unsafe_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREEWEIGHT_SERVER__HOST", "0.0.0.0")  # noqa: S104 — testing the refusal

    result = runner.invoke(app, ["config", "validate"])

    assert result.exit_code == 3


def test_config_path_prints_a_path() -> None:
    result = runner.invoke(app, ["config", "path"])

    assert result.exit_code == 0
    assert result.output.strip().endswith("config.toml")


def test_config_init_writes_example_file(tmp_path: Path) -> None:
    target = tmp_path / "written.toml"

    result = runner.invoke(app, ["config", "init", "--config", str(target)])

    assert result.exit_code == 0
    assert target.is_file()
    assert "[server]" in target.read_text(encoding="utf-8")


def test_config_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    target = tmp_path / "written.toml"
    target.write_text("existing", encoding="utf-8")

    result = runner.invoke(app, ["config", "init", "--config", str(target)])

    assert result.exit_code == 3
    assert target.read_text(encoding="utf-8") == "existing"


def test_config_init_force_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "written.toml"
    target.write_text("existing", encoding="utf-8")

    result = runner.invoke(app, ["config", "init", "--config", str(target), "--force"])

    assert result.exit_code == 0
    assert "[server]" in target.read_text(encoding="utf-8")


def test_root_help_renders() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "serve" in result.output
    assert "health" in result.output


def test_python_dash_m_freeweight_runs_the_cli(tmp_path: Path) -> None:
    """``python -m freeweight`` reaches ``freeweight.__main__`` and dispatches to the Typer app."""
    completed = subprocess.run(
        [sys.executable, "-m", "freeweight", "version", "--json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["application"]["name"] == "freeweight"


def test_help_imports_no_heavy_dependencies() -> None:
    """CLI Standards §12: ``--help`` must not import FastAPI, SQLAlchemy, httpx or Jinja2."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from typer.testing import CliRunner\n"
            "from freeweight.cli.main import app\n"
            "CliRunner().invoke(app, ['--help'])\n"
            "heavy = {'fastapi', 'sqlalchemy', 'httpx', 'jinja2'}\n"
            "loaded = heavy & set(sys.modules)\n"
            "assert not loaded, f'unexpectedly imported: {loaded}'\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
