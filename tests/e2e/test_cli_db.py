"""End-to-end: the ``freeweight db`` subcommand group, added in Phase 2."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from freeweight.cli.main import app

runner = CliRunner()


def test_db_upgrade_creates_a_fresh_database() -> None:
    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0, result.output
    assert "->" in result.output


def test_db_upgrade_is_idempotent() -> None:
    first = runner.invoke(app, ["db", "upgrade"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, ["db", "upgrade", "--json"])

    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["from_revision"] == payload["to_revision"]
    assert payload["backed_up"] is False


def test_db_status_json_after_upgrade() -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    result = runner.invoke(app, ["db", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dialect"] == "sqlite"
    assert payload["is_at_head"] is True
    assert payload["integrity_ok"] is True
    assert payload["table_row_counts"] == {
        "api_tokens": 0,
        "machines": 0,
        "model_descriptors": 0,
        "models": 0,
        "runtime_profiles": 0,
        "settings": 0,
    }


def test_db_status_before_any_upgrade_reports_no_revision() -> None:
    result = runner.invoke(app, ["db", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["current_revision"] is None
    assert payload["is_at_head"] is False


def test_db_backup_then_restore_round_trip(tmp_path: Path) -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    backup_path = tmp_path / "backup.sqlite3"

    backup_result = runner.invoke(app, ["db", "backup", "--output", str(backup_path)])
    assert backup_result.exit_code == 0, backup_result.output
    assert backup_path.is_file()

    restore_result = runner.invoke(app, ["db", "restore", str(backup_path), "--yes"])
    assert restore_result.exit_code == 0, restore_result.output


def test_db_restore_without_yes_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(app, ["db", "restore", str(tmp_path / "nothing.sqlite3")])

    assert result.exit_code == 2
    assert "--yes" in result.output


def test_db_vacuum_previews_the_space_it_reclaims() -> None:
    """Database standards §8: destructive-adjacent operations always preview their effect."""
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    result = runner.invoke(app, ["db", "vacuum", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["size_before_bytes"] > 0
    assert payload["size_after_bytes"] > 0
    assert payload["reclaimed_bytes"] >= 0
    assert payload["estimated_reclaimable_bytes"] >= 0


def test_db_vacuum_text_output_shows_before_and_after() -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    result = runner.invoke(app, ["db", "vacuum"])

    assert result.exit_code == 0, result.output
    assert "estimated reclaimable:" in result.output
    assert "reclaimed:" in result.output


def test_db_status_reports_size() -> None:
    """Database standards §7 lists size alongside revision, row counts and integrity."""
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    json_result = runner.invoke(app, ["db", "status", "--json"])
    text_result = runner.invoke(app, ["db", "status"])

    assert json.loads(json_result.output)["size_bytes"] > 0
    assert "size:" in text_result.output


def test_db_backup_defaults_to_a_revision_named_rotating_path() -> None:
    """No --output must work, and the name must say which schema the file holds (§7)."""
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    result = runner.invoke(app, ["db", "backup", "--json"])

    assert result.exit_code == 0, result.output
    written = Path(json.loads(result.output)["path"])
    assert written.is_file()
    assert written.parent.name == "backups"
    assert written.name.startswith(f"freeweight-{_head_revision()}-")


def test_db_backup_rotates_older_default_path_backups() -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    paths = []
    for _ in range(7):
        result = runner.invoke(app, ["db", "backup", "--json"])
        assert result.exit_code == 0, result.output
        paths.append(Path(json.loads(result.output)["path"]))

    backups_dir = paths[-1].parent
    kept = sorted(p.name for p in backups_dir.iterdir() if p.name.startswith("freeweight-"))
    assert len(kept) == 5, kept
    assert paths[-1].name in kept


def test_db_restore_reports_the_restored_revision(tmp_path: Path) -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    backup_path = tmp_path / "backup.sqlite3"
    assert runner.invoke(app, ["db", "backup", "--output", str(backup_path)]).exit_code == 0

    result = runner.invoke(app, ["db", "restore", str(backup_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert f"revision {_head_revision()}" in result.output


def _head_revision() -> str:
    """The migration history's current head — see the note in ``test_backup_restore.py``."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from freeweight.services.database import MIGRATIONS_LOCATION

    config = Config()
    config.set_main_option("script_location", MIGRATIONS_LOCATION)
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return str(head)
