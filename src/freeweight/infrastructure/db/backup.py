"""freeweight.infrastructure.db.backup — backup, restore, rotation, integrity and size.

Database standards §7: the automatic restore-on-failure guarantee is **SQLite-only**. On SQLite, a
backup is a byte-identical copy taken through the SQLite backup API (safe against a live writer),
and a failed migration restores it. On PostgreSQL there is no equivalent —
``pg_dump``/``pg_restore`` is not byte-identical, a restore generally needs privileges this
application's role deliberately does not hold, and a restore cannot run safely underneath a live
database — so PostgreSQL's ``backup()`` exists for the explicit CLI command only, ``restore()``
refuses and names the ``pg_restore`` invocation instead, and
:mod:`freeweight.infrastructure.db.migration` never restores automatically for this dialect.

**Restoring a SQLite database is a file-level operation, and WAL makes that subtle.** The engine
runs ``journal_mode=WAL`` (database standards §2), so committed data lives in a ``-wal`` sidecar
until a checkpoint folds it into the main file. Copying a backup over the main file alone leaves
that sidecar in place, and the next reader replays it straight back on top — the restore silently
does nothing to the very writes it was meant to undo. :func:`restore` therefore checkpoints,
disposes the pool and removes the sidecars before the swap, and verifies the result opens before
discarding the file it replaced.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from freeweight.infrastructure.db.base import utcnow
from freeweight.infrastructure.db.errors import DatabaseError, StorageFull

__all__ = [
    "BackupResult",
    "IntegrityResult",
    "RestoreResult",
    "backup",
    "backup_revision",
    "checkpoint",
    "database_size_bytes",
    "integrity_check",
    "pg_restore_command",
    "prune_backups",
    "reclaimable_bytes",
    "restore",
    "sqlite_path",
]

_LOG = logging.getLogger(__name__)

# Long enough that an ordinary concurrent writer finishes, short enough that a genuinely stuck
# database reports rather than hangs. Matches engine.create_engine_for's own busy_timeout default.
_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0

_SIDECAR_SUFFIXES = ("-wal", "-shm")


@dataclass(frozen=True, slots=True)
class BackupResult:
    """The outcome of a successful :func:`backup`.

    Attributes:
        path: Where the backup was written.
        size_bytes: The backup file's size, for the CLI's "space this will use" reporting.
        created_at: When the backup was taken.
        dialect: ``"sqlite"`` or ``"postgresql"`` — which mechanism produced it, since the two are
            not interchangeable (a ``pg_dump`` archive is never restored onto a SQLite file).
        pruned: Automatic backups deleted by rotation as part of this call, oldest first. Empty
            when rotation was not requested or nothing aged out.
    """

    path: Path
    size_bytes: int
    created_at: datetime
    dialect: str
    pruned: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """The outcome of a successful :func:`restore`.

    Attributes:
        path: The database file that was restored into.
        source: The backup that was restored from.
        restored_at: When the restore completed.
        revision: The Alembic revision the restored database is at, or ``None`` if the backup
            predates any migration.
    """

    path: Path
    source: Path
    restored_at: datetime
    revision: str | None


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    """The outcome of :func:`integrity_check`.

    Attributes:
        ok: Whether the database passed its integrity check.
        detail: The backend's own report — the raw ``PRAGMA integrity_check`` output on SQLite, or
            a summary sentence on PostgreSQL.
    """

    ok: bool
    detail: str


def sqlite_path(engine: Engine) -> Path:
    """Return the on-disk path of a SQLite engine's database file.

    Raises:
        DatabaseError: The engine is not SQLite, or is the special in-memory database, which has
            no file to back up.
    """
    if engine.dialect.name != "sqlite":
        raise DatabaseError(
            f"Expected a SQLite engine; got dialect {engine.dialect.name!r}.",
            details={"dialect": engine.dialect.name},
        )
    # `URL.database` already applies the sqlite dialect's own rule for how many leading slashes
    # separate "sqlite://" from an absolute path; parsing the URL string by hand here is how a
    # "sqlite:////tmp/x" quietly becomes the relative "tmp/x".
    database = engine.url.database
    if not database or database == ":memory:":
        raise DatabaseError(
            "Cannot back up an in-memory SQLite database — it has no file to copy.",
            details={"database_url": "sqlite:///:memory:"},
        )
    return Path(database)


def _sidecars(database: Path) -> tuple[Path, ...]:
    """Return the ``-wal``/``-shm`` companion paths SQLite keeps beside ``database``."""
    return tuple(Path(f"{database}{suffix}") for suffix in _SIDECAR_SUFFIXES)


def checkpoint(engine: Engine) -> None:
    """Fold the WAL into the main database file and truncate the sidecar. A no-op off SQLite.

    Needed wherever the *file* is the thing being measured or moved. Under
    ``journal_mode=WAL`` the main file lags whatever is still in ``<db>-wal``, so a size read or a
    byte-for-byte copy taken without checkpointing first describes neither the old state nor the
    new one.

    Best-effort: a database too damaged to checkpoint is exactly the one a restore is about to
    replace, so a failure here is logged and does not abort the caller.
    """
    if engine.dialect.name != "sqlite":
        return
    try:
        # AUTOCOMMIT is load-bearing. Every ordinary connection from this engine opens its
        # transaction with BEGIN IMMEDIATE, and `wal_checkpoint` cannot run inside a transaction —
        # it reports busy in its result row rather than raising, so a checkpoint issued the
        # ordinary way silently does nothing and every caller here quietly gets the un-checkpointed
        # file it was trying to avoid.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001 — a database too broken to checkpoint is one we still replace
        _LOG.debug("WAL checkpoint failed; continuing", exc_info=True)


def _checkpoint_and_release(engine: Engine) -> None:
    """Checkpoint, then drop every pooled connection to the database.

    Both halves matter before a file-level swap: the checkpoint makes the main file complete (so
    the copy taken of it is worth keeping as a rollback), and the dispose releases the handles
    that would otherwise keep the old ``-wal`` alive underneath the new database.
    """
    checkpoint(engine)
    engine.dispose()


def backup_revision(source: Path, *, version_table: str = "alembic_version") -> str | None:
    """Return the Alembic revision recorded inside a SQLite backup file.

    Args:
        source: The backup file to inspect. Opened read-only; never modified.
        version_table: The table Alembic records the revision in. Must be a plain identifier.

    Returns:
        The revision string, or ``None`` when the file carries no version table at all — a
        legitimate state for a backup taken before the first migration ran.

    Raises:
        DatabaseError: ``version_table`` is not a plain identifier, or ``source`` cannot be opened.
    """
    if not version_table.isidentifier():
        raise DatabaseError(f"Invalid version table name {version_table!r}.")
    try:
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DatabaseError(
            f"Backup {source} could not be opened: {exc}", details={"source": str(source)}
        ) from exc
    try:
        # `version_table` is validated as an identifier immediately above and is never
        # caller-supplied in practice; SQLite has no parameter form for a table name.
        row = connection.execute(f"SELECT version_num FROM {version_table}").fetchone()  # noqa: S608
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return str(row[0]) if row else None


def backup(
    engine: Engine,
    destination: Path,
    *,
    compress: bool = False,
    keep: int | None = None,
    prefix: str | None = None,
) -> BackupResult:
    """Take a consistent backup of the database ``engine`` is connected to.

    On SQLite this uses the SQLite backup API (:meth:`sqlite3.Connection.backup`), which is safe
    to run against a database with an active writer — it never holds a lock for longer than a
    single page copy. On PostgreSQL this shells out to ``pg_dump`` in the custom archive format.

    Args:
        engine: The engine to back up.
        destination: Where to write the backup. Parent directories are created if missing; the
            file is created with mode ``0600`` before a byte is written to it (security
            standards), never widened first and narrowed afterwards.
        compress: Gzip the SQLite backup after writing it. Ignored for PostgreSQL, whose custom
            archive format is already compressed.
        keep: Retain only this many backups matching ``prefix`` in ``destination``'s directory,
            newest first, deleting the rest (database standards §7, default 5 for automatic
            backups). ``None`` disables rotation, which is what an operator-chosen ``--output``
            path gets — this function never deletes a file the operator named.
        prefix: Filename prefix identifying the family of automatic backups ``keep`` applies to.
            Required when ``keep`` is given.

    Returns:
        The :class:`BackupResult`.

    Raises:
        DatabaseError: The backup could not be taken, including :class:`StorageFull` when the
            destination device is out of space (the partial file is removed first). Also raised
            when the SQLite database file does not exist — an empty backup of a database that was
            never created is worse than a refusal, because it looks like a successful one.
    """
    if keep is not None and prefix is None:
        raise DatabaseError("backup(keep=...) requires a prefix identifying which files to rotate.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    dialect = engine.dialect.name

    if dialect == "sqlite":
        source_path = sqlite_path(engine)
        if not source_path.is_file():
            raise DatabaseError(
                f"There is no database at {source_path} to back up.",
                details={"source": str(source_path)},
            )
        _touch_private(destination)
        source = sqlite3.connect(source_path, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS)
        try:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        except sqlite3.OperationalError as exc:
            destination.unlink(missing_ok=True)
            if "disk" in str(exc).lower() or "space" in str(exc).lower():
                raise StorageFull(
                    f"No space left to write backup {destination}.",
                    details={"destination": str(destination)},
                ) from exc
            raise DatabaseError(
                f"SQLite backup of {source_path} to {destination} failed: {exc}",
                details={"source": str(source_path), "destination": str(destination)},
            ) from exc
        finally:
            source.close()
        if compress:
            compressed = Path(f"{destination}.gz")
            _touch_private(compressed)
            try:
                with destination.open("rb") as raw, gzip.open(compressed, "wb") as archive:
                    shutil.copyfileobj(raw, archive)
            except OSError as exc:
                # The uncompressed file is the one that is known-good; leaving a half-written
                # ".gz" beside it would look like a backup and restore as nothing.
                compressed.unlink(missing_ok=True)
                raise DatabaseError(
                    f"Compressing backup {destination} failed: {exc}",
                    details={"destination": str(destination)},
                ) from exc
            destination.unlink()
            destination = compressed
    elif dialect == "postgresql":
        _touch_private(destination)
        url = engine.url
        command = [
            "pg_dump",
            "--format=custom",
            f"--file={destination}",
            f"--host={url.host or 'localhost'}",
            f"--port={url.port or 5432}",
            f"--username={url.username}",
            url.database or "",
        ]
        env = {**os.environ, "PGPASSWORD": url.password} if url.password else None
        try:
            subprocess.run(command, check=True, capture_output=True, env=env)  # noqa: S603, S607
        except FileNotFoundError as exc:
            destination.unlink(missing_ok=True)
            raise DatabaseError(
                "pg_dump is not installed or not on PATH; PostgreSQL backups require the "
                "PostgreSQL client tools.",
            ) from exc
        except subprocess.CalledProcessError as exc:
            destination.unlink(missing_ok=True)
            raise DatabaseError(
                f"pg_dump failed with exit code {exc.returncode}: "
                f"{exc.stderr.decode(errors='replace')}",
            ) from exc
    else:
        raise DatabaseError(
            f"Unsupported dialect {dialect!r}; only sqlite and postgresql are supported.",
            details={"dialect": dialect},
        )

    pruned: tuple[Path, ...] = ()
    if keep is not None and prefix is not None:
        pruned = prune_backups(destination.parent, prefix=prefix, keep=keep)

    return BackupResult(
        path=destination,
        size_bytes=destination.stat().st_size,
        created_at=utcnow(),
        dialect=dialect,
        pruned=pruned,
    )


def _touch_private(path: Path) -> None:
    """Create ``path`` empty with mode ``0600``, before anything writes content into it.

    ``chmod`` after the write leaves a window in which the file exists, holds the whole database,
    and is world-readable. Creating it private first closes that window; the ``O_EXCL``-free open
    is deliberate, since a backup destination may legitimately be overwritten.
    """
    path.unlink(missing_ok=True)
    path.touch(mode=0o600)


def prune_backups(directory: Path, *, prefix: str, keep: int) -> tuple[Path, ...]:
    """Delete all but the ``keep`` newest backups named ``prefix*`` in ``directory``.

    Rotation is logged (database standards §7) so that a backup disappearing is always traceable
    to a policy rather than looking like data loss.

    Args:
        directory: The backups directory. A missing directory prunes nothing.
        prefix: Only files whose name starts with this are candidates — an operator's own
            ``--output`` backup, named anything else, is never a candidate.
        keep: How many to retain. ``0`` deletes every matching file; negative is refused.

    Returns:
        The deleted paths, oldest first.

    Raises:
        DatabaseError: ``keep`` is negative.
    """
    if keep < 0:
        raise DatabaseError(f"backup retention must not be negative; got {keep}.")
    if not directory.is_dir():
        return ()
    candidates = sorted(
        (path for path in directory.iterdir() if path.is_file() and path.name.startswith(prefix)),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    doomed = candidates[: max(0, len(candidates) - keep)]
    for path in doomed:
        path.unlink(missing_ok=True)
        _LOG.info("rotated out backup %s (retention: keep %d)", path, keep)
    return tuple(doomed)


def pg_restore_command(engine: Engine, source: Path) -> str:
    """Return the ``pg_restore`` invocation an operator runs to restore ``source`` by hand."""
    url = engine.url
    return (
        f"pg_restore --clean --if-exists --host={url.host or 'localhost'} "
        f"--port={url.port or 5432} --username={url.username} "
        f"--dbname={url.database or ''} {source}"
    )


def restore(
    engine: Engine,
    source: Path,
    *,
    confirm: bool,
    known_revisions: frozenset[str] | None = None,
) -> RestoreResult:
    """Restore the database ``engine`` is connected to from a backup.

    The backup is verified before anything is touched — it must open, pass its own integrity
    check, and (when ``known_revisions`` is given) sit at a revision this build knows how to read.
    The database being replaced is then checkpointed, released and copied aside as a
    ``.pre-restore`` sibling, which is deleted only once the restored file has been opened and has
    passed an integrity check of its own; if it does not, the ``.pre-restore`` copy is put back and
    the failure is raised (database standards §7).

    ``engine``'s connection pool is disposed as part of this — a file-level swap cannot happen
    safely underneath live handles, and the stale ``-wal`` those handles keep alive would be
    replayed on top of the restored file. The engine remains usable afterwards; SQLAlchemy opens a
    fresh pool on next use.

    Args:
        engine: The engine whose database will be replaced. Must be SQLite — restoring a
            PostgreSQL database from a ``pg_dump`` archive is a deliberate, privileged operation
            this function does not perform, and refuses while naming the ``pg_restore`` command.
        source: The backup file to restore from.
        confirm: Must be ``True`` or the restore is refused. There is no implicit destructive path
            (WeightsDB spec §14).
        known_revisions: Every revision this build's migration history contains. When given, a
            backup at a revision outside it is refused rather than restored into a build that
            cannot read it. ``None`` skips the check, for callers that have no history to check
            against.

    Returns:
        The :class:`RestoreResult`.

    Raises:
        DatabaseError: ``confirm`` is ``False``, ``engine`` is not SQLite, ``source`` does not
            exist, ``source`` fails to open or fails its integrity check, ``source`` is at an
            unknown revision, or the restored file itself fails to open — in which case the
            original database has already been put back.
    """
    if not confirm:
        raise DatabaseError(
            "restore() requires confirm=True; there is no implicit destructive path.",
        )
    if engine.dialect.name == "postgresql":
        raise DatabaseError(
            "Restoring a PostgreSQL database is not performed in-process (database standards §7). "
            f"Run: {pg_restore_command(engine, source)}",
            details={"command": pg_restore_command(engine, source), "source": str(source)},
        )
    if not source.is_file():
        raise DatabaseError(
            f"Backup file {source} does not exist.", details={"source": str(source)}
        )

    target_path = sqlite_path(engine)
    _verify_backup_file(source)
    revision = backup_revision(source)
    if known_revisions is not None and revision is not None and revision not in known_revisions:
        raise DatabaseError(
            f"Backup {source} is at revision {revision!r}, which this build's migration history "
            "does not contain; it was written by a newer version and restoring it would leave a "
            "database this build cannot read.",
            details={"source": str(source), "revision": revision},
        )

    _checkpoint_and_release(engine)

    pre_restore = Path(f"{target_path}.pre-restore")
    had_original = target_path.is_file()
    if had_original:
        shutil.copy2(target_path, pre_restore)
        pre_restore.chmod(0o600)
    for sidecar in _sidecars(target_path):
        sidecar.unlink(missing_ok=True)

    _touch_private(target_path)
    shutil.copyfile(source, target_path)

    verification = integrity_check(engine)
    if not verification.ok:
        engine.dispose()
        for sidecar in _sidecars(target_path):
            sidecar.unlink(missing_ok=True)
        if had_original:
            shutil.copyfile(pre_restore, target_path)
            pre_restore.unlink(missing_ok=True)
        else:
            target_path.unlink(missing_ok=True)
        raise DatabaseError(
            f"Restored database from {source} failed its integrity check "
            f"({verification.detail}); the original database has been put back.",
            details={"source": str(source), "detail": verification.detail},
        )

    # Release the handle the verification above opened, so the restored database is left
    # checkpointed and sidecar-free exactly as the backup was.
    engine.dispose()
    if had_original:
        pre_restore.unlink(missing_ok=True)

    return RestoreResult(path=target_path, source=source, restored_at=utcnow(), revision=revision)


def _verify_backup_file(source: Path) -> None:
    """Raise unless ``source`` opens as SQLite and passes ``PRAGMA integrity_check``."""
    try:
        probe = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DatabaseError(
            f"Backup {source} could not be opened: {exc}", details={"source": str(source)}
        ) from exc
    try:
        row = probe.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        # Damage bad enough that the pragma cannot run at all — a corrupt page 1, a file that was
        # never SQLite — is reported by raising rather than by returning a row naming the bad
        # pages. Which of the two shapes a given corruption produces is not stable across SQLite
        # versions, so both say "failed its integrity check": the refusal is the same refusal.
        raise DatabaseError(
            f"Backup {source} failed its integrity check: it is not a readable SQLite "
            f"database ({exc}).",
            details={"source": str(source)},
        ) from exc
    finally:
        probe.close()
    if row is None or row[0] != "ok":
        raise DatabaseError(
            f"Backup {source} failed its integrity check: {row[0] if row else 'unreadable'}.",
            details={"source": str(source)},
        )


def integrity_check(engine: Engine) -> IntegrityResult:
    """Run the database's own integrity check.

    Args:
        engine: The engine to check.

    Returns:
        The :class:`IntegrityResult`. On SQLite this runs ``PRAGMA integrity_check``. On
        PostgreSQL, which has no equivalent single command, this reports ``ok`` from a successful
        connection and a trivial query — a real corruption check there is an operator running
        ``pg_amcheck`` or restoring onto a scratch instance, out of scope for an in-process call.

        A SQLite database the pragma cannot run against at all — damaged beyond opening, or
        otherwise refusing the statement — is reported as ``ok=False`` carrying the driver's own
        message, never by raising: SQLite answers the same corruption either by
        listing the bad pages in a row or by failing the statement with "database disk image is
        malformed", and which one it picks varies with the damage and the SQLite version. Every
        caller here acts on a failed check rather than propagating it — :func:`restore` rolls the
        original database back, ``db status`` reports the database as failing — and neither can
        do that if half of all corruptions arrive as an exception instead.
    """
    if engine.dialect.name == "sqlite":
        try:
            with engine.connect() as connection:
                row = connection.execute(text("PRAGMA integrity_check")).fetchone()
        except SQLAlchemyError as exc:
            cause = exc.orig if isinstance(exc, DBAPIError) and exc.orig is not None else exc
            return IntegrityResult(ok=False, detail=str(cause))
        detail = row[0] if row else "unreadable"
        return IntegrityResult(ok=detail == "ok", detail=str(detail))

    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return IntegrityResult(ok=True, detail="connection and a trivial query succeeded")


def database_size_bytes(engine: Engine) -> int:
    """Return how much space the database occupies, for ``freeweight db status`` (§7).

    On SQLite this is the main file plus its ``-wal``/``-shm`` sidecars, because those are real
    bytes on the user's disk and a report that omitted them would understate a busy database. On
    PostgreSQL it is ``pg_database_size(current_database())``.

    Returns:
        The size in bytes; ``0`` when the SQLite file does not exist yet.
    """
    if engine.dialect.name == "sqlite":
        if not engine.url.database or engine.url.database == ":memory:":
            # An in-memory database occupies no disk. Reporting that is right; raising, the way
            # sqlite_path() does for callers that need a file to copy, would make a size report
            # the thing that breaks `db status`.
            return 0
        database = sqlite_path(engine)
        paths = (database, *_sidecars(database))
        return sum(path.stat().st_size for path in paths if path.is_file())
    with engine.connect() as connection:
        size = connection.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    return int(size)


def reclaimable_bytes(engine: Engine) -> int:
    """Estimate what a ``VACUUM`` would reclaim, so the CLI can preview it before running (§8).

    On SQLite this is exact and cheap: the free page count times the page size. On PostgreSQL
    there is no comparable cheap estimate — a real one needs ``pgstattuple`` — so this reports
    ``0``, and the caller reports the actual before/after difference instead of a prediction.
    """
    if engine.dialect.name != "sqlite":
        return 0
    with engine.connect() as connection:
        free_pages = connection.execute(text("PRAGMA freelist_count")).scalar_one()
        page_size = connection.execute(text("PRAGMA page_size")).scalar_one()
    return int(free_pages) * int(page_size)
