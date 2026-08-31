"""freeweight.services.database — engine construction, startup migration and status.

The one place that turns :class:`~freeweight.config.Settings` into a live database connection.
Route handlers and CLI command bodies never call
:func:`~weightsdb.engine.create_engine_for` directly (CLI standards §1 / coding
standards §5); they call a function here, which is what makes ``freeweight health --json`` and
``GET /api/v1/health`` report identical database status by construction, the same way Phase 1's
health report does for the rest of the application.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from weightsdb import (
    DatabaseError,
    DatabaseUnavailable,
    MigrationOutcome,
    MigrationRequired,
    MigrationRunner,
    SchemaAhead,
    create_engine_for,
    session_factory,
    session_scope,
    transaction,
)
from weightsdb.backup import database_size_bytes, integrity_check

from freeweight.infrastructure.db.models import (
    ApiToken,
    Machine,
    Model,
    ModelDescriptor,
    RuntimeProfile,
    Setting,
)
from freeweight.infrastructure.db.models_evidence import CapabilityEvidence
from freeweight.infrastructure.db.models_goals import (
    CalibrationGrade,
    CalibrationReport,
    CalibrationSample,
    CriterionScore,
    Goal,
    GoalCriterion,
    GoalTaskRow,
    JudgeVerdict,
)
from freeweight.infrastructure.db.models_runs import (
    Artifact,
    BenchmarkSuite,
    BenchmarkTestRow,
    MetricValue,
    Run,
    RunEvent,
    RunTest,
    Sample,
    TelemetryGpuSample,
    TelemetrySample,
    ToolCall,
)
from freeweight.services.health import HealthComponent

__all__ = [
    "MIGRATIONS_LOCATION",
    "Database",
    "DatabaseStatus",
    "build_engine",
    "database_health_component",
    "ensure_ready",
    "get_status",
    "migration_runner",
    "upgrade",
]

MIGRATIONS_LOCATION = str(
    Path(__file__).resolve().parent.parent / "infrastructure" / "db" / "migrations"
)

_APPLICATION_NAME = "freeweight"

_ROW_COUNT_MODELS = (
    # Identity and configuration.
    Machine,
    Model,
    ModelDescriptor,
    RuntimeProfile,
    Setting,
    ApiToken,
    # Benchmarks and their runs. Added at Phase 10: database standards §7 lists row counts as part
    # of `db status`, and a report that omitted `runs`, `samples` and `metric_values` omitted every
    # table a user's disk usage and every table a deletion actually touches — which is precisely
    # what the database page has to show before it offers to remove any of them.
    BenchmarkSuite,
    BenchmarkTestRow,
    Run,
    RunTest,
    Sample,
    ToolCall,
    MetricValue,
    RunEvent,
    Artifact,
    TelemetrySample,
    TelemetryGpuSample,
    # Goals and calibration.
    Goal,
    GoalCriterion,
    GoalTaskRow,
    CriterionScore,
    CalibrationSample,
    CalibrationGrade,
    CalibrationReport,
    JudgeVerdict,
    # Capability evidence (Phase 11).
    CapabilityEvidence,
)


def build_engine(
    settings_storage_database_url: str, *, statement_timeout_ms: int | None = None
) -> Engine:
    """Build the engine for the configured database URL.

    Passes ``application_name`` through on PostgreSQL. Without it every FreeWeight connection
    shows up in ``pg_stat_activity`` as an anonymous client, which is precisely the situation the
    setting exists to avoid — an engine option nothing ever supplies is configuration that only
    looks present.

    Args:
        settings_storage_database_url: ``settings.storage.database_url`` — always non-``None`` by
            the time :class:`~freeweight.config.Settings` has validated
            (:meth:`~freeweight.config.StorageSettings._apply_data_dir_defaults`).
        statement_timeout_ms: ``settings.storage.statement_timeout_ms``; PostgreSQL only, and
            ``None`` leaves the server default in place.

    Returns:
        A new :class:`~sqlalchemy.Engine` with dialect-correct settings applied.
    """
    return create_engine_for(
        settings_storage_database_url,
        statement_timeout_ms=statement_timeout_ms,
        application_name=_APPLICATION_NAME,
    )


class Database:
    """The application's live connection to its database: one engine, for as long as it serves.

    **Owned by the caller, not by the functions that use it.** The web application creates one in
    its lifespan and disposes it at shutdown; a CLI command creates one, runs, and closes it on
    the way out. Every service function below takes a handle rather than building an engine from
    a URL, so nothing in the service layer has an opinion about how long a connection should live
    — which is the only way the same function can serve both a long-running server and a one-shot
    command without being wrong for one of them.

    That matters more than it sounds. An engine is a connection pool plus SQLAlchemy's compiled-
    statement cache; building and discarding one per call throws both away every time. Measured
    on this schema, the same query costs 0.12 ms through a live handle and 0.95 ms if the engine
    is rebuilt around it on SQLite, and 0.38 ms against 7.41 ms on PostgreSQL — where every call
    was also opening a fresh backend connection, making the configured ``pool_size`` meaningless.

    Read and write transactions are asked for separately (:meth:`read`, :meth:`write`) because on
    SQLite they are genuinely different transactions — see
    :data:`~weightsdb.engine.READ_ONLY_EXECUTION_OPTION`. There is no default;
    a caller has to say which it wants.
    """

    __slots__ = ("_engine", "_sessions")

    def __init__(self, engine: Engine) -> None:
        """Wrap an existing engine. Prefer :meth:`from_url` unless you built the engine yourself."""
        self._engine = engine
        self._sessions = session_factory(engine)

    @classmethod
    def from_url(cls, database_url: str, *, statement_timeout_ms: int | None = None) -> Database:
        """Build a handle for ``database_url``. Opens no connection until first use."""
        return cls(build_engine(database_url, statement_timeout_ms=statement_timeout_ms))

    @property
    def engine(self) -> Engine:
        """The underlying engine, for the file-level operations that need one directly."""
        return self._engine

    @property
    def sessions(self) -> sessionmaker[Session]:
        """The session factory bound to this handle's engine."""
        return self._sessions

    @contextmanager
    def read(self) -> Iterator[Session]:
        """One read-only unit of work.

        Enforced, not merely declared: a write attempted inside this scope is refused by SQLite
        rather than silently taken. WeightsDB's :func:`~weightsdb.session_scope` dropped the
        ``read_only`` parameter its FreeWeight ancestor had; read-only intent is now declared by
        composing :func:`~weightsdb.transaction` with ``immediate=False`` inside the scope
        (WeightsDB adoption checklist §3), which enforces exactly what the old parameter did.
        """
        with session_scope(self._sessions) as session, transaction(session, immediate=False):
            yield session

    @contextmanager
    def write(self) -> Iterator[Session]:
        """One read-write unit of work, committed on success and rolled back on any exception."""
        with session_scope(self._sessions) as session:
            yield session

    def close(self) -> None:
        """Dispose the pool. The handle must not be used afterwards."""
        self._engine.dispose()

    def __enter__(self) -> Database:
        """Support ``with Database.from_url(...) as db:`` for one-shot callers like the CLI."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always dispose the pool, whether the body succeeded or raised."""
        self.close()


def migration_runner(engine: Engine, *, backup_retention: int = 5) -> MigrationRunner:
    """Build the :class:`~weightsdb.MigrationRunner` for ``engine``.

    Always points at this application's own migration scripts
    (``freeweight/infrastructure/db/migrations``) — every application in the suite owns its own
    linear history (database standards §1).

    Args:
        engine: The engine migrations run against.
        backup_retention: ``settings.storage.backup_retention`` — how many automatic
            pre-migration backups to keep (database standards §7).
    """
    return MigrationRunner(
        engine, script_location=MIGRATIONS_LOCATION, backup_retention=backup_retention
    )


def ensure_ready(
    database: Database, *, auto_migrate: bool, backup_retention: int = 5
) -> MigrationOutcome | None:
    """Apply the startup revision check (database standards §5.1).

    ```text
    current == head              -> start
    current is None               -> new database: migrate to head
    current < head, auto_migrate  -> back up -> upgrade -> start
    current < head, not auto_migrate -> refuse with MigrationRequired
    current unknown to this build -> refuse with SchemaAhead (written by a newer version)
    ```

    Args:
        database: The application's database handle. Runs against the same engine the server will
            serve from, so a pragma or timeout that is wrong for the application is wrong here too
            rather than being masked by a second, differently built connection.
        auto_migrate: ``settings.storage.auto_migrate``. This function only does what it is told;
            the dialect-dependent default (true on SQLite, false on PostgreSQL — database
            standards §5.1) is applied by
            :meth:`~freeweight.config.StorageSettings._auto_migrate_default`.
        backup_retention: ``settings.storage.backup_retention``; how many automatic pre-migration
            backups to keep.

    Returns:
        The :class:`MigrationOutcome` if a migration ran, else ``None`` when the database was
        already at head.

    Raises:
        MigrationRequired: The database is behind head and ``auto_migrate`` is ``False``.
        SchemaAhead: The database's current revision is not one this build's migrations produce.
        DatabaseUnavailable: The database could not be reached at all.
    """
    try:
        runner = migration_runner(database.engine, backup_retention=backup_retention)
        current = runner.current()
        heads = runner.heads()
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(
            f"Could not open the database to check its migration state: {exc}",
        ) from exc

    if not heads:
        raise DatabaseError(
            "No migrations are registered under "
            f"{MIGRATIONS_LOCATION}; the migration history is missing or empty.",
        )
    head = heads[0]

    if current == head:
        return None
    if current is not None and current not in runner.known_revisions():
        raise SchemaAhead(
            f"The database is at revision {current!r}, which this build's migrations do not "
            f"produce (known head: {head!r}). It was likely written by a newer application "
            "version.",
            details={"current": current, "head": head},
        )
    if current is not None and not auto_migrate:
        raise MigrationRequired(
            f"The database is at revision {current!r}; head is {head!r}. Run "
            "`freeweight db upgrade` to migrate.",
            details={"current": current, "head": head, "command": "freeweight db upgrade"},
        )
    return runner.upgrade(backup=current is not None)


def upgrade(
    database: Database, *, revision: str = "head", backup_retention: int = 5
) -> MigrationOutcome:
    """Run ``freeweight db upgrade``: migrate to ``revision``, taking a backup first.

    Idempotent — calling this when already at ``revision`` is a documented no-op (CLI standards
    §11).
    """
    runner = migration_runner(database.engine, backup_retention=backup_retention)
    return runner.upgrade(revision, backup=runner.current() is not None)


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """The ``freeweight db status`` / health-component snapshot.

    Attributes:
        dialect: ``"sqlite"`` or ``"postgresql"``.
        current_revision: The database's current Alembic revision, or ``None`` for a fresh,
            unmigrated database.
        head_revision: The revision this build's migrations produce.
        is_at_head: Whether ``current_revision == head_revision``.
        table_row_counts: Row count per table this application owns.
        size_bytes: How much disk the database occupies — the SQLite file plus its WAL sidecars,
            or ``pg_database_size`` on PostgreSQL. Reported because database standards §7 lists
            size alongside revision and row counts in ``db status``.
        integrity_ok: Whether the integrity check passed.
        integrity_detail: The backend's own integrity report.
    """

    dialect: str
    current_revision: str | None
    head_revision: str
    is_at_head: bool
    table_row_counts: dict[str, int]
    size_bytes: int
    integrity_ok: bool
    integrity_detail: str


def get_status(database: Database) -> DatabaseStatus:
    """Build the full ``freeweight db status`` report.

    Args:
        database: The application's database handle.

    Raises:
        DatabaseUnavailable: The database could not be reached.
    """
    engine = database.engine
    try:
        runner = migration_runner(engine)
        current = runner.current()
        heads = runner.heads()
        head = heads[0] if heads else ""
        row_counts: dict[str, int] = {}
        if current is not None:
            # Counting rows is a read, and says so: on SQLite this is the difference between
            # six SELECTs that other readers can run alongside and six that hold the single
            # write lock while a run is trying to record samples.
            with database.read() as session:
                for model in _ROW_COUNT_MODELS:
                    count = session.execute(select(func.count()).select_from(model)).scalar_one()
                    row_counts[model.__tablename__] = count
        integrity = integrity_check(engine)
        size_bytes = database_size_bytes(engine)
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(f"Could not open the database: {exc}") from exc

    return DatabaseStatus(
        dialect=engine.dialect.name,
        current_revision=current,
        head_revision=head,
        is_at_head=current == head,
        table_row_counts=row_counts,
        size_bytes=size_bytes,
        integrity_ok=integrity.ok,
        integrity_detail=integrity.detail,
    )


def database_health_component(database: Database) -> HealthComponent:
    """Build the ``database`` :class:`~freeweight.services.health.HealthComponent`.

    Never raises: a health check that itself crashes takes the whole health endpoint down with it,
    which is exactly the outcome graceful degradation exists to prevent. Every failure mode becomes
    a ``degraded``/``unavailable`` component instead.
    """
    try:
        status = get_status(database)
    except DatabaseUnavailable as exc:
        return HealthComponent(name="database", status="unavailable", detail=exc.message)
    except DatabaseError as exc:
        return HealthComponent(name="database", status="degraded", detail=exc.message)

    if not status.is_at_head:
        return HealthComponent(
            name="database",
            status="degraded",
            detail=(
                f"pending migration: at {status.current_revision!r}, "
                f"head is {status.head_revision!r}"
            ),
        )
    if not status.integrity_ok:
        return HealthComponent(
            name="database",
            status="degraded",
            detail=f"integrity check failed: {status.integrity_detail}",
        )
    return HealthComponent(name="database", status="ok", detail=f"{status.dialect} at head")
