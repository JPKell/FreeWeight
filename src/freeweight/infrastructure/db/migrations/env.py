"""Alembic environment for FreeWeight's own migration history.

Always run through :class:`freeweight.infrastructure.db.migration.MigrationRunner`, never through
the bare ``alembic`` CLI: ``config.attributes["connection"]`` is always populated by the runner with
an already-open connection from the application's own dialect-configured engine, so this module
never builds its own engine from a URL and never runs in Alembic's offline (SQL-script-generation)
mode — neither is a code path anything in this application uses.
"""

from __future__ import annotations

from alembic import context

from freeweight.infrastructure.db import models as _models  # noqa: F401
from freeweight.infrastructure.db import models_evidence as _models_evidence  # noqa: F401
from freeweight.infrastructure.db import models_goals as _models_goals  # noqa: F401
from freeweight.infrastructure.db import models_runs as _models_runs  # noqa: F401
from freeweight.infrastructure.db.base import Base

# All four model modules are imported for their side effect of registering their tables on
# ``Base.metadata``. Without them, ``target_metadata`` is empty here and autogenerate — including
# ``MigrationRunner.check_parity`` — compares a live database against nothing and reports every
# real table as an extra one to drop. The import is in this module rather than left to whichever
# caller happened to touch a repository first, because Alembic's environment is the one place
# guaranteed to run for every migration operation.

config = context.config
target_metadata = Base.metadata


def _set_foreign_keys(connection: object, *, on: bool) -> None:
    """Set SQLite's ``foreign_keys`` pragma on this connection, outside any transaction.

    Run through the **raw DBAPI cursor** deliberately. WeightsDB puts the driver in autocommit and
    emits ``BEGIN IMMEDIATE`` from SQLAlchemy's ``begin`` event, so executing the pragma through
    the SQLAlchemy connection would open a transaction first — and ``PRAGMA foreign_keys`` is a
    documented no-op inside one. A silent no-op, which is the worst kind.

    Args:
        connection: The open SQLAlchemy connection Alembic was handed.
        on: Whether enforcement should be enabled.
    """
    raw = connection.connection  # type: ignore[attr-defined]  # alembic hands a Connection
    cursor = raw.cursor()
    try:
        cursor.execute(f"PRAGMA foreign_keys={'ON' if on else 'OFF'}")
    finally:
        cursor.close()


def run_migrations_online() -> None:
    """Run migrations against the connection the caller placed in ``config.attributes``.

    **Foreign keys are enforced off for the duration on SQLite**, and restored afterwards
    ([ADR-0082](../../../../../../../docs/adr/0082-a-migration-run-suspends-sqlite-foreign-key-enforcement.md)).
    They are ``ON`` in normal operation (database standards §2); this is that standard's one stated
    exception.

    Adding a constraint to an existing SQLite table is a **table rebuild** — alembic's batch mode
    copies, drops and renames — and dropping a table that other rows reference
    ``ON DELETE CASCADE`` deletes those rows. Migration ``0008`` adds a foreign key to ``runs``,
    which has six cascading children: ``run_tests``, ``run_events``, ``artifacts``,
    ``metric_values``, ``telemetry_samples`` and (through ``run_tests``) ``samples``. With
    enforcement on, upgrading a real 1.0 database would silently delete every measurement in it and
    report success.

    The pragma takes effect only outside a transaction, which is why it runs before
    ``begin_transaction`` and is restored in a ``finally``.
    """
    connection = config.attributes["connection"]
    sqlite = connection.dialect.name == "sqlite"
    if sqlite:
        _set_foreign_keys(connection, on=False)
    version_table = config.attributes.get("version_table", "alembic_version")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=version_table,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if sqlite:
            _set_foreign_keys(connection, on=True)


run_migrations_online()
