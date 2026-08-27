"""Integration tests for backup, restore, rotation and size reporting (database standards §7).

The restore tests are SQLite-only by design, not by omission: the automatic guarantee exists only
there, and PostgreSQL's refusal-with-instructions is asserted in its own test at the bottom.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from freeweight.infrastructure.db.backup import (
    backup,
    backup_revision,
    checkpoint,
    database_size_bytes,
    integrity_check,
    prune_backups,
    reclaimable_bytes,
    restore,
    sqlite_path,
)
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Engine:
    """A migrated SQLite database with one row in it, and its own engine."""
    engine = create_engine_for(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO settings (key, value_json, updated_at) "
                "VALUES ('original', '1', '2026-08-26 00:00:00')"
            )
        )
    return engine


def _keys(database: Path) -> set[str]:
    """Read the settings keys a *fresh* reader sees — WAL replay included."""
    connection = sqlite3.connect(database)
    try:
        return {row[0] for row in connection.execute("SELECT key FROM settings")}
    finally:
        connection.close()


def test_restore_undoes_writes_still_sitting_in_the_wal(
    sqlite_engine: Engine, tmp_path: Path
) -> None:
    """The regression that matters: a restore that ignores the ``-wal`` sidecar restores nothing.

    ``journal_mode=WAL`` means committed data can live in ``<db>-wal`` until a checkpoint folds it
    in. Copying the backup over the main file alone leaves that sidecar, and the next reader
    replays it right back on top — so the database still contains exactly the writes the restore
    was called to undo, while reporting success.
    """
    good = tmp_path / "good.sqlite3"
    backup(sqlite_engine, good)

    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO settings (key, value_json, updated_at) "
                "VALUES ('written-after-the-backup', '1', '2026-08-26 00:00:00')"
            )
        )
    database = sqlite_path(sqlite_engine)
    assert Path(f"{database}-wal").exists(), "precondition: the write is still in the WAL"

    result = restore(sqlite_engine, good, confirm=True)

    assert result.revision == _head_revision()
    assert _keys(database) == {"original"}
    assert not Path(f"{database}-wal").exists()


def test_restore_leaves_no_pre_restore_file_behind(sqlite_engine: Engine, tmp_path: Path) -> None:
    good = tmp_path / "good.sqlite3"
    backup(sqlite_engine, good)

    restore(sqlite_engine, good, confirm=True)

    database = sqlite_path(sqlite_engine)
    assert not Path(f"{database}.pre-restore").exists()


def test_restore_puts_the_original_back_when_the_new_file_is_corrupt(
    sqlite_engine: Engine, tmp_path: Path
) -> None:
    """The ``.pre-restore`` copy is the rollback, so it must survive until the new file opens.

    The corruption is introduced *after* the source passes its own verification, which is the only
    way to reach the post-swap failure branch — a backup that was already unreadable is refused
    before anything is touched.
    """
    good = tmp_path / "good.sqlite3"
    backup(sqlite_engine, good)
    database = sqlite_path(sqlite_engine)
    original = database.read_bytes()

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(good.read_bytes())
    with corrupt.open("r+b") as handle:
        handle.seek(4096)
        handle.write(b"\x00" * 4096)

    with pytest.raises(DatabaseError) as excinfo:
        restore(sqlite_engine, corrupt, confirm=True)

    assert "integrity check" in str(excinfo.value)
    assert database.read_bytes() == original
    assert integrity_check(sqlite_engine).ok is True
    assert _keys(database) == {"original"}


def test_restore_refuses_a_backup_at_an_unknown_revision(
    sqlite_engine: Engine, tmp_path: Path
) -> None:
    """Database standards §7: a restore verifies the backup is at a known revision first."""
    good = tmp_path / "good.sqlite3"
    backup(sqlite_engine, good)

    with pytest.raises(DatabaseError) as excinfo:
        restore(sqlite_engine, good, confirm=True, known_revisions=frozenset({"9998", "9999"}))

    assert _head_revision() in str(excinfo.value)
    assert _keys(sqlite_path(sqlite_engine)) == {"original"}


def test_restore_refuses_without_confirmation(sqlite_engine: Engine, tmp_path: Path) -> None:
    good = tmp_path / "good.sqlite3"
    backup(sqlite_engine, good)

    with pytest.raises(DatabaseError, match="confirm=True"):
        restore(sqlite_engine, good, confirm=False)


def test_restore_refuses_a_backup_that_does_not_exist(
    sqlite_engine: Engine, tmp_path: Path
) -> None:
    with pytest.raises(DatabaseError, match="does not exist"):
        restore(sqlite_engine, tmp_path / "nope.sqlite3", confirm=True)


def test_backup_refuses_when_there_is_no_database_file(tmp_path: Path) -> None:
    """An empty backup of a database that was never created looks like a successful one."""
    engine = create_engine_for(f"sqlite:///{tmp_path / 'absent.sqlite3'}")
    try:
        with pytest.raises(DatabaseError, match="no database at"):
            backup(engine, tmp_path / "out.sqlite3")
    finally:
        engine.dispose()


def test_backup_file_is_private_before_it_holds_data(sqlite_engine: Engine, tmp_path: Path) -> None:
    result = backup(sqlite_engine, tmp_path / "out.sqlite3")

    assert result.path.stat().st_mode & 0o777 == 0o600


def test_backup_revision_reads_the_stamped_revision(sqlite_engine: Engine, tmp_path: Path) -> None:
    result = backup(sqlite_engine, tmp_path / "out.sqlite3")

    assert backup_revision(result.path) == _head_revision()


def test_backup_revision_is_none_for_an_unmigrated_database(tmp_path: Path) -> None:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'bare.sqlite3'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE t (v TEXT)"))
        result = backup(engine, tmp_path / "bare-backup.sqlite3")
    finally:
        engine.dispose()

    assert backup_revision(result.path) is None


def test_compressed_backup_is_written_and_the_plain_one_removed(
    sqlite_engine: Engine, tmp_path: Path
) -> None:
    result = backup(sqlite_engine, tmp_path / "out.sqlite3", compress=True)

    assert result.path.name == "out.sqlite3.gz"
    assert not (tmp_path / "out.sqlite3").exists()


def test_rotation_keeps_only_the_newest_matching_backups(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    for index in range(6):
        path = directory / f"pre-migration-0001-{index}.sqlite3"
        path.write_text(str(index))
        # Explicit mtimes: six files written in the same millisecond would otherwise sort by name
        # and make this test agree with the implementation for the wrong reason.
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
    (directory / "an-operators-own-backup.sqlite3").write_text("keep me")

    pruned = prune_backups(directory, prefix="pre-migration-", keep=2)

    assert len(pruned) == 4
    remaining = {path.name for path in directory.iterdir()}
    assert remaining == {
        "pre-migration-0001-4.sqlite3",
        "pre-migration-0001-5.sqlite3",
        "an-operators-own-backup.sqlite3",
    }


def test_rotation_never_touches_a_directory_that_does_not_exist(tmp_path: Path) -> None:
    assert prune_backups(tmp_path / "nope", prefix="pre-migration-", keep=5) == ()


def test_rotation_refuses_a_negative_retention(tmp_path: Path) -> None:
    with pytest.raises(DatabaseError, match="must not be negative"):
        prune_backups(tmp_path, prefix="pre-migration-", keep=-1)


def test_pre_migration_backups_rotate_and_carry_the_revision(tmp_path: Path) -> None:
    """Every upgrade backs up first; without rotation the directory grows without bound."""
    database = tmp_path / "test.sqlite3"
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION, backup_retention=2)
        runner.upgrade(backup=False)
        for _ in range(4):
            runner.downgrade("base")
            outcome = runner.upgrade()
        backups = sorted((database.parent / "backups").iterdir())
    finally:
        engine.dispose()

    assert outcome.backed_up is False  # a downgrade to base leaves nothing to back up
    assert all(path.name.startswith("pre-migration-") for path in backups)
    assert len(backups) <= 2, [path.name for path in backups]


def test_size_and_reclaimable_reporting(sqlite_engine: Engine) -> None:
    assert database_size_bytes(sqlite_engine) > 0
    assert reclaimable_bytes(sqlite_engine) >= 0


def test_size_of_an_in_memory_database_is_zero_rather_than_an_error() -> None:
    engine = create_engine_for("sqlite:///:memory:")
    try:
        assert database_size_bytes(engine) == 0
    finally:
        engine.dispose()


def test_restore_on_postgresql_refuses_and_names_pg_restore(
    postgres_url: str, tmp_path: Path
) -> None:
    """Database standards §7: PostgreSQL restores are an operator's ``pg_restore``, not ours."""
    engine = create_engine_for(postgres_url)
    try:
        with pytest.raises(DatabaseError) as excinfo:
            restore(engine, tmp_path / "whatever.dump", confirm=True)
    finally:
        engine.dispose()

    assert "pg_restore" in str(excinfo.value)
    assert "pg_restore" in str(excinfo.value.details["command"])


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump is not on PATH")
def test_backup_on_postgresql_produces_a_pg_dump_archive(postgres_url: str, tmp_path: Path) -> None:
    """The ``pg_dump`` branch of :func:`backup`, which SQLite-only testing never executes."""
    engine = create_engine_for(postgres_url)
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        destination = tmp_path / "freeweight.dump"
        result = backup(engine, destination)
    finally:
        engine.dispose()

    assert result.dialect == "postgresql"
    assert result.size_bytes > 0
    assert destination.stat().st_mode & 0o777 == 0o600
    # pg_dump's custom format starts with the magic "PGDMP".
    assert destination.read_bytes()[:5] == b"PGDMP"


def test_vacuum_reports_a_real_reclaim_rather_than_wal_growth(sqlite_engine: Engine) -> None:
    """The before/after sizes must be comparable, which under WAL means checkpointed.

    Measured without a checkpoint on each side, VACUUM's own (large) WAL is counted against the
    "after" size, and a vacuum that genuinely reclaimed pages reports the database as having grown.
    """
    from freeweight.services.database import Database
    from freeweight.services.database_admin import vacuum_database

    url = str(sqlite_engine.url)
    padding = "x" * 200
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO settings (key, value_json, updated_at) "
                "VALUES (:key, :value, '2026-08-26 00:00:00')"
            ),
            [{"key": f"k{index}", "value": f'"{padding}"'} for index in range(2000)],
        )
    with sqlite_engine.begin() as connection:
        connection.execute(text("DELETE FROM settings WHERE key LIKE 'k%'"))
    sqlite_engine.dispose()

    with Database.from_url(url) as database:
        outcome = vacuum_database(database)

    assert outcome.estimated_reclaimable_bytes > 0
    assert outcome.size_after_bytes < outcome.size_before_bytes
    assert outcome.reclaimed_bytes > 0


def test_checkpoint_truncates_the_wal(sqlite_engine: Engine) -> None:
    """A checkpoint issued inside a transaction reports busy and does nothing; this asserts it did.

    ``wal_checkpoint`` returns its busy flag in a result row rather than raising, so the only way
    to know the checkpoint happened is to look at the sidecar it was supposed to truncate.
    """
    with sqlite_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO settings (key, value_json, updated_at) "
                "VALUES ('in-the-wal', '1', '2026-08-26 00:00:00')"
            )
        )
    wal = Path(f"{sqlite_path(sqlite_engine)}-wal")
    assert wal.stat().st_size > 0

    checkpoint(sqlite_engine)

    assert wal.stat().st_size == 0


def _head_revision() -> str:
    """The migration history's current head, read from the scripts rather than written down.

    Every assertion below that used to spell ``"0001"`` is really asserting "the revision this
    build migrates to", and that changes with every phase that adds a migration. Reading it keeps
    these tests testing backup and restore rather than testing which phase is current.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", MIGRATIONS_LOCATION)
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return str(head)
