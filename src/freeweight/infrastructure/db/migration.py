"""freeweight.infrastructure.db.migration — a programmatic Alembic wrapper.

One linear history per application, from the first release (database standards §5). This module
never shells out to the ``alembic`` CLI or reads an ``alembic.ini`` — it builds an in-memory
:class:`alembic.config.Config` and drives :mod:`alembic.command` directly, always against the
already-configured :class:`~sqlalchemy.Engine` the caller built with
:func:`~freeweight.infrastructure.db.engine.create_engine_for`, so every migration runs with the
same pragmas and transaction semantics as the rest of the application — never a second, differently
configured connection built from the bare URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from freeweight.infrastructure.db.backup import backup as take_backup
from freeweight.infrastructure.db.backup import restore as restore_backup
from freeweight.infrastructure.db.backup import sqlite_path
from freeweight.infrastructure.db.base import utcnow
from freeweight.infrastructure.db.errors import MigrationFailed

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine, MetaData

__all__ = ["MigrationOutcome", "MigrationRunner", "ParityResult"]

# The filename family `prune_backups` rotates. An operator-named backup never starts with
# this, and is therefore never a rotation candidate.
_PRE_MIGRATION_PREFIX = "pre-migration-"


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    """The result of :meth:`MigrationRunner.upgrade` or :meth:`MigrationRunner.downgrade`.

    Attributes:
        from_revision: The revision before this operation ran, or ``None`` for a fresh database.
        to_revision: The revision after this operation ran, or ``None`` when a downgrade reached
            ``base`` (no revision at all — an unmigrated database).
        backed_up: Whether a backup was taken first.
        backup_path: Where the backup was written, or ``None`` when ``backed_up`` is ``False``.
        pruned_backups: Older automatic backups rotated out by this call, oldest first.
        restore_on_failure_available: Whether a failure of *this* migration would have been rolled
            back automatically. ``True`` only on SQLite with a backup in hand; ``False`` on
            PostgreSQL, where the guarantee does not exist (database standards §7), and ``False``
            for a fresh database, where there is nothing to roll back to. This is the field that
            states the dialect difference rather than papering over it — an always-``False``
            "restored" flag stated nothing, since a successful outcome never restores anything and
            a failed one raises instead of returning.
        dialect: ``"sqlite"`` or ``"postgresql"``.
    """

    from_revision: str | None
    to_revision: str | None
    backed_up: bool
    backup_path: Path | None
    pruned_backups: tuple[Path, ...]
    restore_on_failure_available: bool
    dialect: str


@dataclass(frozen=True, slots=True)
class ParityResult:
    """The result of :meth:`MigrationRunner.check_parity`.

    Attributes:
        matches: ``True`` when autogenerate finds no difference between the live schema and the
            given metadata.
        diff: A human-readable rendering of the drift found, empty when ``matches`` is ``True``.
            Alembic's autogenerate comparator does not reliably detect a changed ``CheckConstraint``
            or a partial (``WHERE``-qualified) index — a known upstream limitation, not something
            this wrapper works around; a migration that changes only one of those needs its own
            explicit test, not a mechanical parity check.
    """

    matches: bool
    diff: str


class MigrationRunner:
    """Runs one application's own migration history against one engine.

    Stateless beyond its constructor arguments: every method opens its own connection (or is
    handed one internally) and closes it, so one instance is safely reused across calls but is not
    itself a resource that needs closing.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        script_location: str,
        version_table: str = "alembic_version",
        backup_retention: int = 5,
    ) -> None:
        """Build a runner for ``engine``'s migration history under ``script_location``.

        Args:
            engine: The engine migrations run against. Reused as-is — this class never builds its
                own engine from a bare URL, so the caller's pragmas and transaction semantics
                (``BEGIN IMMEDIATE`` on SQLite) apply to every migration exactly as they apply to
                the rest of the application.
            script_location: Filesystem path to the ``migrations/`` directory containing ``env.py``
                and ``versions/``.
            version_table: The table Alembic records the current revision in. Defaults to Alembic's
                own default; overridable so two applications sharing infrastructure-but-not-schema
                (not a real case in this suite, since every application owns its own database) could
                coexist without colliding on table name.
            backup_retention: How many automatic pre-migration backups to keep (database standards
                §7, default 5). Rotation applies only to this runner's own ``pre-migration-*``
                files; a backup an operator asked for by name is never rotated.
        """
        self._engine = engine
        self._script_location = script_location
        self._version_table = version_table
        self._backup_retention = backup_retention
        self._script_config = Config()
        self._script_config.set_main_option("script_location", script_location)

    def current(self) -> str | None:
        """Return the database's current revision, or ``None`` for an unmigrated database."""
        with self._engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"version_table": self._version_table}
            )
            return context.get_current_revision()

    def heads(self) -> tuple[str, ...]:
        """Return this script directory's head revision(s) — normally exactly one."""
        script = ScriptDirectory.from_config(self._script_config)
        return tuple(script.get_heads())

    def known_revisions(self) -> frozenset[str]:
        """Return every revision ID this script directory's history contains.

        Used to detect :class:`~freeweight.infrastructure.db.errors.SchemaAhead`: a database whose
        current revision is not in this set was written by a build whose migrations this one does
        not have.
        """
        script = ScriptDirectory.from_config(self._script_config)
        return frozenset(revision.revision for revision in script.walk_revisions())

    def is_at_head(self) -> bool:
        """Return whether the database's current revision is a head revision."""
        current = self.current()
        return current is not None and current in self.heads()

    def upgrade(self, revision: str = "head", *, backup: bool = True) -> MigrationOutcome:
        """Migrate to ``revision``, taking a backup first (database standards §5.1, §7).

        A no-op when already at ``revision`` — ``command.upgrade`` is itself idempotent, and this
        method takes no backup and performs no write in that case (CLI standards §11).

        Args:
            revision: The target revision, or ``"head"``.
            backup: Take a backup before migrating. Ignored when the database is unmigrated (there
                is nothing to back up) or when the dialect is PostgreSQL, where the automatic
                restore-on-failure guarantee does not apply (database standards §7) and a caller
                that wants a PostgreSQL backup takes one explicitly via ``freeweight db backup``.

        Returns:
            The :class:`MigrationOutcome`.

        Raises:
            MigrationFailed: The migration raised. On SQLite, the pre-migration backup has already
                been restored by the time this is raised (``details["restored"] is True``) and the
                original database is byte-identical. On PostgreSQL, no restore is attempted;
                ``details`` names the revision actually reached and, if one was taken, the backup to
                restore from manually.
        """
        return self._run(command.upgrade, revision, backup=backup)

    def downgrade(self, revision: str) -> MigrationOutcome:
        """Migrate down to ``revision``. Same backup and failure semantics as :meth:`upgrade`."""
        return self._run(command.downgrade, revision, backup=True)

    def _run(self, alembic_command: object, revision: str, *, backup: bool) -> MigrationOutcome:
        from_revision = self.current()
        dialect = self._engine.dialect.name

        script = ScriptDirectory.from_config(self._script_config)
        target_revision = script.as_revision_number(revision)
        if target_revision == from_revision:
            # Already there: a genuine no-op (CLI standards §11), not merely "alembic ran and
            # changed nothing" — no backup is taken and alembic is never invoked, so this is also
            # the case that keeps `upgrade()` cheap when called opportunistically at every
            # startup.
            return MigrationOutcome(
                from_revision=from_revision,
                to_revision=from_revision,
                backed_up=False,
                backup_path=None,
                pruned_backups=(),
                restore_on_failure_available=False,
                dialect=dialect,
            )

        backed_up = False
        backup_path: Path | None = None
        pruned: tuple[Path, ...] = ()
        if backup and from_revision is not None and dialect == "sqlite":
            result = take_backup(
                self._engine,
                self._pre_migration_backup_path(from_revision),
                keep=self._backup_retention,
                prefix=_PRE_MIGRATION_PREFIX,
            )
            backed_up = True
            backup_path = result.path
            pruned = result.pruned

        try:
            with self._engine.connect() as connection:
                config = self._connection_config(connection)
                alembic_command(config, revision)  # type: ignore[operator]
        except Exception as exc:
            restored = False
            if backed_up and backup_path is not None and dialect == "sqlite":
                restore_backup(
                    self._engine,
                    backup_path,
                    confirm=True,
                    known_revisions=self.known_revisions(),
                )
                restored = True
            raise MigrationFailed(
                f"Migration to {revision!r} failed: {exc}",
                details={
                    "restored": restored,
                    "backup_path": str(backup_path) if backup_path else None,
                    "reached_revision": self.current(),
                    "restore_command": None
                    if restored
                    else f"freeweight db restore {backup_path} --yes"
                    if backup_path
                    else None,
                },
            ) from exc

        # `to_revision` legitimately ends up `None` here for a downgrade all the way to `base` —
        # an unmigrated database is not a broken outcome, so nothing above raises for it; a
        # migration that fails to reach any real revision would have raised inside the `try`
        # block above instead of returning normally.
        to_revision = self.current()
        return MigrationOutcome(
            from_revision=from_revision,
            to_revision=to_revision,
            backed_up=backed_up,
            backup_path=backup_path,
            pruned_backups=pruned,
            restore_on_failure_available=backed_up and dialect == "sqlite",
            dialect=dialect,
        )

    def stamp(self, revision: str = "head") -> None:
        """Mark the database as being at ``revision`` without running any migration.

        For recovering a database whose schema is known to already match a revision — never for
        routine use, and never called automatically by anything in this module.
        """
        with self._engine.connect() as connection:
            config = self._connection_config(connection)
            command.stamp(config, revision)

    def check_parity(self, metadata: MetaData) -> ParityResult:
        """Diff ``metadata`` against the database's live schema (database standards §5.2).

        Independent of Alembic's own revision bookkeeping — this reflects the actual tables,
        columns and indexes in the database and compares them to ``metadata`` directly, so it
        catches a model changed without a matching migration even if the ``alembic_version`` table
        is technically at head.

        Args:
            metadata: Normally :attr:`freeweight.infrastructure.db.base.Base.metadata`.

        Returns:
            The :class:`ParityResult`.
        """
        with self._engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"version_table": self._version_table}
            )
            diff = compare_metadata(context, metadata)
        if not diff:
            return ParityResult(matches=True, diff="")
        return ParityResult(matches=False, diff="\n".join(repr(item) for item in diff))

    def _connection_config(self, connection: Connection) -> Config:
        """Build a per-call :class:`Config` bound to an already-open connection.

        A fresh object every call rather than a cached one: ``config.attributes["connection"]``
        must point at *this* call's connection, and reusing one :class:`Config` across calls would
        leak the previous, already-closed connection into the next migration's ``env.py``.
        """
        config = Config()
        config.set_main_option("script_location", self._script_location)
        config.set_main_option(
            "sqlalchemy.url", self._engine.url.render_as_string(hide_password=True)
        )
        config.attributes["connection"] = connection
        config.attributes["version_table"] = self._version_table
        return config

    def _pre_migration_backup_path(self, from_revision: str) -> Path:
        """Choose ``<data_dir>/backups/pre-migration-<revision>-<UTC timestamp>.sqlite3``.

        The revision is part of the name, not only the timestamp (database standards §7): an
        operator picking a file out of a backups directory needs to know which schema it holds
        without opening it, and rotation keeps several generations side by side.
        """
        source = sqlite_path(self._engine)
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        return (
            source.parent
            / "backups"
            / f"{_PRE_MIGRATION_PREFIX}{from_revision}-{stamp}{source.suffix}"
        )
