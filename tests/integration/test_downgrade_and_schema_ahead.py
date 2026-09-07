"""Integration test for the downgrade drill packaging standards §6.1 promises.

"Downgrading the application without downgrading the database is refused, not attempted: a
database ahead of the code raises ``SchemaAhead`` at startup and names both revisions and the
backup directory. A supported downgrade path is: stop the application, restore the automatic
pre-migration backup, install the older version." Until this test, nothing in any of the four
applications' suites drove that drill end to end (M9_AUDIT.md Group 3, item O2) — this is the
one that does, on FreeWeight's own bootstrap path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from weightsdb import SchemaAhead, create_engine_for

from freeweight.infrastructure.db.repositories.settings import SettingsRepository
from freeweight.services.database import (
    Database,
    _backup_directory,
    ensure_ready,
    migration_runner,
)
from freeweight.services.database_admin import backup_database, restore_database


def test_schema_ahead_is_refused_and_the_pre_migration_backup_restores_it(
    tmp_path: Path,
) -> None:
    """Upgrade, write a row, back up, jump the version ahead, refuse, restore, start again."""
    database_path = tmp_path / "test.sqlite3"
    engine = create_engine_for(f"sqlite:///{database_path}")
    database = Database(engine)
    repository = SettingsRepository()
    try:
        # 1. Bootstrap's own path to a fresh database: migrate to head.
        ensure_ready(database, auto_migrate=True)
        head = migration_runner(engine).heads()[0]

        # 2. Write one row through the repository layer, not raw SQL.
        with database.write() as session:
            repository.set(session, "written-before-the-jump", "1", now=datetime.now(UTC))

        # 3. `freeweight db backup` — the real path an operator runs before anything risky.
        backup_result = backup_database(database, output=tmp_path / "pre-jump.sqlite3")
        assert backup_result.path.is_file()

        # 4. Simulate a newer application version having migrated this database further: hand-set
        # `alembic_version` to a revision this build's history does not contain.
        fake_future_revision = "9999_from_the_future"
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": fake_future_revision},
            )
        database.close()

        # 5. Startup now refuses — the exact drill packaging standards §6.1 promises.
        reopened = Database(create_engine_for(f"sqlite:///{database_path}"))
        try:
            try:
                ensure_ready(reopened, auto_migrate=True)
            except SchemaAhead as exc:
                assert fake_future_revision in str(exc)
                assert head in str(exc)
                assert exc.details["current"] == fake_future_revision
                assert exc.details["head"] == head
                expected_backup_directory = str(_backup_directory(reopened.engine))
                assert expected_backup_directory in str(exc)
                assert exc.details["backup_directory"] == expected_backup_directory
            else:  # pragma: no cover — defensive: the test proves nothing if this branch runs
                raise AssertionError("SchemaAhead was not raised for a database ahead of head")
        finally:
            reopened.close()

        # 6. The supported downgrade path: restore the pre-migration backup.
        restored = Database(create_engine_for(f"sqlite:///{database_path}"))
        try:
            restore_database(restored, source=backup_result.path, confirm=True)

            # 7. The application starts again, at the revision it knew about all along, and the
            # row written before the jump is intact.
            ensure_ready(restored, auto_migrate=True)
            assert migration_runner(restored.engine).current() == head
            with restored.read() as session:
                assert repository.get(session, "written-before-the-jump") == "1"
        finally:
            restored.close()
    finally:
        database.close()
