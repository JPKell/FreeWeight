"""freeweight.services.database_admin — backup, restore and vacuum for ``freeweight db``.

Kept separate from :mod:`freeweight.services.database`: that module is read-mostly (status,
health, the startup migration check), while this one performs the explicit, operator-initiated
destructive-adjacent operations database standards §7 and §8 govern — always confirmed, always
logged, never triggered as a side effect of anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine, text

from freeweight.config import data_dir
from freeweight.infrastructure.db.backup import (
    BackupResult,
    RestoreResult,
    backup,
    checkpoint,
    database_size_bytes,
    prune_backups,
    reclaimable_bytes,
    restore,
    sqlite_path,
)
from freeweight.infrastructure.db.base import utcnow
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.database import migration_runner

if TYPE_CHECKING:
    from freeweight.services.database import Database

__all__ = [
    "VacuumOutcome",
    "backup_database",
    "prune_database_backups",
    "restore_database",
    "vacuum_database",
]

# The filename family rotation applies to for operator-invoked `freeweight db backup` with no
# --output. A backup written to a path the operator named is never a rotation candidate.
_MANUAL_BACKUP_PREFIX = "freeweight-"


def _backups_dir(engine: Engine) -> Path:
    """Where automatic and default-path backups live.

    Beside the database file on SQLite, and under the XDG data directory on PostgreSQL, which has
    no local file to sit beside — the case that previously reached
    :func:`~freeweight.infrastructure.db.backup.sqlite_path` and failed with "Expected a SQLite
    engine" for the entirely ordinary ``freeweight db backup`` with no ``--output``.
    """
    if engine.dialect.name == "sqlite":
        return sqlite_path(engine).parent / "backups"
    return data_dir() / "backups"


def _default_backup_path(engine: Engine, *, revision: str | None) -> Path:
    """Choose ``<backups>/freeweight-<revision>-<UTC timestamp>.<ext>``.

    The revision is in the name, not only the timestamp (database standards §7): an operator
    picking one file out of a directory of them needs to know which schema it holds without
    opening it. ``.sqlite3`` for a SQLite copy, ``.dump`` for a ``pg_dump`` custom archive — the
    two are not interchangeable, and the extension is what stops someone trying.
    """
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    suffix = ".sqlite3" if engine.dialect.name == "sqlite" else ".dump"
    return _backups_dir(engine) / f"{_MANUAL_BACKUP_PREFIX}{revision or 'base'}-{stamp}{suffix}"


def backup_database(
    database: Database, *, output: Path | None = None, keep: int = 5
) -> BackupResult:
    """Take a backup of the configured database, per ``freeweight db backup``.

    Works on both dialects: the SQLite backup API for SQLite, ``pg_dump --format=custom`` for
    PostgreSQL.

    Args:
        database: The application's database handle.
        output: Where to write the backup. Defaults to a timestamped, revision-named file under
            ``<data_dir>/backups/``.
        keep: How many default-path backups to retain (``settings.storage.backup_retention``).
            Applies only when ``output`` is ``None`` — a path the operator named is theirs, and
            this function never deletes files from a directory they chose.

    Returns:
        The :class:`~freeweight.infrastructure.db.backup.BackupResult`.
    """
    engine = database.engine
    if output is not None:
        return backup(engine, output)
    revision = migration_runner(engine).current()
    return backup(
        engine,
        _default_backup_path(engine, revision=revision),
        keep=keep,
        prefix=_MANUAL_BACKUP_PREFIX,
    )


def restore_database(database: Database, *, source: Path, confirm: bool) -> RestoreResult:
    """Restore the configured database from a backup, per ``freeweight db restore``.

    SQLite only. On PostgreSQL this refuses and names the ``pg_restore`` invocation instead
    (database standards §7) — an in-process restore there would need privileges the application's
    role deliberately does not hold, and cannot run safely underneath a live database.

    Args:
        database: The application's database handle. Its pool is disposed as part of the restore —
            a file-level swap cannot happen safely underneath live handles — and is reopened on
            next use.
        source: The backup file to restore from.
        confirm: Must be ``True`` — CLI standards §5 requires an explicit flag for anything that
            would otherwise prompt.

    Returns:
        The :class:`~freeweight.infrastructure.db.backup.RestoreResult`.

    Raises:
        DatabaseError: ``confirm`` is ``False``, the dialect is PostgreSQL, or the backup is
            missing, fails its integrity check, or sits at a revision this build does not know.
            See :func:`~freeweight.infrastructure.db.backup.restore`.
    """
    engine = database.engine
    known = migration_runner(engine).known_revisions()
    return restore(engine, source, confirm=confirm, known_revisions=known)


def prune_database_backups(database: Database, *, keep: int) -> tuple[Path, ...]:
    """Rotate default-path backups down to ``keep``, per database standards §7.

    Exposed separately from :func:`backup_database` so retention can be applied without taking a
    new backup — for instance after an operator lowers ``storage.backup_retention``.
    """
    return prune_backups(_backups_dir(database.engine), prefix=_MANUAL_BACKUP_PREFIX, keep=keep)


@dataclass(frozen=True, slots=True)
class VacuumOutcome:
    """The before/after picture of a ``freeweight db vacuum``.

    Attributes:
        estimated_reclaimable_bytes: What the free-page count predicted before running, which is
            the preview database standards §8 requires ("always preview"). Exact on SQLite; ``0``
            on PostgreSQL, which offers no comparable cheap estimate.
        size_before_bytes: Database size immediately before the vacuum.
        size_after_bytes: Database size immediately after it.
        reclaimed_bytes: ``size_before_bytes - size_after_bytes``, floored at zero. A vacuum can
            legitimately reclaim less than predicted — or briefly nothing at all — so this is
            reported as measured rather than assumed to equal the estimate.
    """

    estimated_reclaimable_bytes: int
    size_before_bytes: int
    size_after_bytes: int
    reclaimed_bytes: int


def vacuum_database(database: Database) -> VacuumOutcome:
    """Reclaim free space, per ``freeweight db vacuum``.

    ``VACUUM`` cannot run inside a transaction block on either dialect, so this runs it over a
    connection explicitly set to ``AUTOCOMMIT`` rather than through the ORM session — the one
    place in this module that deliberately does not go through
    :func:`~freeweight.infrastructure.db.session.session_scope`.

    Args:
        database: The application's database handle.

    Returns:
        The :class:`VacuumOutcome`, with the pre-run estimate and the measured before/after sizes.

    Raises:
        DatabaseError: The dialect is neither SQLite nor PostgreSQL.
    """
    engine = database.engine
    if engine.dialect.name not in ("sqlite", "postgresql"):
        raise DatabaseError(
            f"Unsupported dialect {engine.dialect.name!r}; only sqlite and postgresql are "
            "supported.",
            details={"dialect": engine.dialect.name},
        )
    estimate = reclaimable_bytes(engine)
    # Checkpoint on both sides of the VACUUM, or the two sizes are not comparable: under WAL
    # the main file lags the sidecar, and VACUUM itself writes a large WAL. Measured without
    # this, a vacuum that genuinely reclaimed space reports the database as having *grown*.
    checkpoint(engine)
    size_before = database_size_bytes(engine)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM"))
    checkpoint(engine)
    size_after = database_size_bytes(engine)
    return VacuumOutcome(
        estimated_reclaimable_bytes=estimate,
        size_before_bytes=size_before,
        size_after_bytes=size_after,
        reclaimed_bytes=max(0, size_before - size_after),
    )
