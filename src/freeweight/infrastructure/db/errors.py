"""freeweight.infrastructure.db.errors — the database error hierarchy.

Written as if it were WeightsDB's own error module
([spec §7, §13](../../../../../../docs/packages/weightsdb/spec.md)): FreeWeight owns this inline
until the package is extracted at Phase 12 (ADR-0011), and the codes below are the contract that
extraction must not silently renumber.
"""

from __future__ import annotations

from typing import ClassVar

from baseaicore import SuiteError

__all__ = [
    "DatabaseError",
    "DatabaseUnavailable",
    "MigrationFailed",
    "MigrationRequired",
    "SchemaAhead",
    "StorageBusy",
    "StorageFull",
]


class DatabaseError(SuiteError):
    """Base for every error raised by the database layer."""

    code: ClassVar[str] = "DATABASE_ERROR"


class MigrationRequired(DatabaseError):
    """The schema is behind head and this dialect does not auto-migrate.

    Raised at startup on PostgreSQL, where ``storage.auto_migrate`` defaults to false (database
    standards §5.1). ``details`` always carries ``current``, ``head`` and ``command`` so the caller
    can print the exact upgrade invocation without composing it itself.
    """

    code: ClassVar[str] = "MIGRATION_REQUIRED"


class MigrationFailed(DatabaseError):
    """A migration raised partway through ``upgrade`` or ``downgrade``.

    On SQLite the pre-migration backup has already been restored by the time this is raised, and
    ``details["restored"]`` is ``True``. On PostgreSQL no automatic restore is attempted (database
    standards §7); ``details`` instead names the revision reached and the backup to restore from.
    """

    code: ClassVar[str] = "MIGRATION_FAILED"


class DatabaseUnavailable(DatabaseError):
    """The configured database could not be reached or opened.

    ``details["database_url"]`` is always redacted (credentials stripped) before this error is
    constructed — never pass a raw URL here.
    """

    code: ClassVar[str] = "DATABASE_UNAVAILABLE"


class SchemaAhead(DatabaseError):
    """The database's revision is newer than any this build knows about.

    Means the database was written by a later application version; running against it would risk
    misreading rows this build's models do not describe.
    """

    code: ClassVar[str] = "SCHEMA_AHEAD"


class StorageBusy(DatabaseError):
    """SQLite reported ``SQLITE_BUSY`` beyond the configured ``busy_timeout``."""

    code: ClassVar[str] = "STORAGE_BUSY"


class StorageFull(DatabaseError):
    """The device backing the database or its backup directory is out of space."""

    code: ClassVar[str] = "STORAGE_FULL"
