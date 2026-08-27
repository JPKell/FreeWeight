"""Unit tests for the ``freeweight db`` service layer's dialect handling.

The paths here are the ones that used to be SQLite-only by accident: ``freeweight db backup`` with
no ``--output`` reached :func:`~freeweight.infrastructure.db.backup.sqlite_path` on a PostgreSQL
engine and failed with "Expected a SQLite engine", which is a true statement about a call the
operator never made.
"""

from __future__ import annotations

from pathlib import Path

from freeweight.config import data_dir
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.services.database_admin import _backups_dir, _default_backup_path


def test_default_backup_path_on_sqlite_sits_beside_the_database(tmp_path: Path) -> None:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'freeweight.sqlite3'}")
    try:
        path = _default_backup_path(engine, revision="0001")
    finally:
        engine.dispose()

    assert path.parent == tmp_path / "backups"
    assert path.name.startswith("freeweight-0001-")
    assert path.suffix == ".sqlite3"


def test_default_backup_path_on_postgresql_uses_the_data_dir() -> None:
    """PostgreSQL has no local database file to sit beside; it must still get a path."""
    engine = create_engine_for("postgresql+psycopg://u:p@h:5432/d")
    try:
        path = _default_backup_path(engine, revision="0001")
        assert _backups_dir(engine) == data_dir() / "backups"
    finally:
        engine.dispose()

    assert path.parent == data_dir() / "backups"
    assert path.name.startswith("freeweight-0001-")
    # A pg_dump custom archive is never restored onto a SQLite file; the extension says so.
    assert path.suffix == ".dump"


def test_default_backup_path_names_an_unmigrated_database_base(tmp_path: Path) -> None:
    engine = create_engine_for(f"sqlite:///{tmp_path / 'freeweight.sqlite3'}")
    try:
        path = _default_backup_path(engine, revision=None)
    finally:
        engine.dispose()

    assert path.name.startswith("freeweight-base-")
