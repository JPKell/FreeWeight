# WeightsDB — Specification

**Type:** Python package · **Import/distribution name:** `weightsdb` · **Layer:** 3 (capability package)
**Status:** Specified, not implemented. **Extraction timing:** LoadCoach Phase 1, from FreeWeight's
`freeweight.infrastructure.db` ([ADR-0011](../../adr/0011-shared-package-boundaries.md)).
**Decision records:** [ADR-0005](../../adr/0005-database-strategy.md), [ADR-0006](../../adr/0006-sqlite-and-postgresql-roles.md).

---

## 1. Purpose

Give every application the same database plumbing — engine construction, dialect pragmas, session
scope, transaction helpers, migrations, backup and health — without giving them a shared schema.
Each application keeps its own database, its own tables and its own migration history; only the
mechanics are shared.

## 2. Scope

* Engine and session factories for SQLite and PostgreSQL, with correct settings applied at connect.
* Transaction and session-scope helpers.
* Portable column types: timezone-aware datetime, JSON (`JSONB` on PostgreSQL), ULID primary key.
* Alembic integration: configuration helper, programmatic upgrade/downgrade/current, autogenerate
  parity check for CI.
* Backup, restore and integrity checks for both dialects.
* Database health reporting.
* Test utilities: temporary databases, both dialects, migration harness.

## 3. Explicit non-goals

* **No application tables.** Its `MetaData` is empty, and a test asserts that.
* No shared `Base` with domain meaning; each application declares its own `DeclarativeBase`.
* No cross-application database, no shared connection, no "common schema".
* No repository or query implementations — those are application code.
* No ORM event magic, no automatic auditing, no soft-delete framework.
* No connection to another application's database, ever.
* No support for dialects other than SQLite and PostgreSQL.

## 4. Responsibilities

| Responsibility | Detail |
|---|---|
| Engine | `create_engine_for(url, …)` applying dialect-correct settings and pooling |
| SQLite settings | `foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout`, `synchronous=NORMAL`, applied per connection |
| PostgreSQL settings | `statement_timeout`, `lock_timeout`, application name, schema selection |
| Sessions | `session_factory`, `session_scope()` context manager with commit/rollback semantics |
| Transactions | `transaction(session)` including `BEGIN IMMEDIATE` on SQLite |
| Types | `UtcDateTime`, `PortableJSON`, `ULIDPrimaryKey`, `Measurement` column helper (value + reason) |
| Migrations | `MigrationRunner` wrapping Alembic: `current()`, `heads()`, `upgrade()`, `downgrade()`, `stamp()`, `check_parity()` |
| Backup | `backup(path)`, `restore(path)`, `integrity_check()` for both dialects |
| Health | `DatabaseHealth` snapshot: dialect, version, revision vs head, journal mode, size, free space, last backup age |
| Testing | `temporary_database(dialect)` fixture helpers used by every application |

## 5. Dependencies

`baseaicore`, `sqlalchemy>=2.0.30,<3`, `alembic>=1.13,<2`. Optional extra: `psycopg[binary]` for
PostgreSQL.

## 6. Consumers

FreeWeight, LoadCoach, IdeaPress. (Extracted only once the second consumer exists.)

## 7. Public API

```python
# Engine and sessions
def create_engine_for(url: str, *, echo: bool = False, pool_size: int | None = None,
                      statement_timeout_ms: int | None = None,
                      sqlite_busy_timeout_ms: int = 5000,
                      application_name: str | None = None) -> Engine: ...
def session_factory(engine: Engine) -> sessionmaker[Session]: ...

@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: commits on success, rolls back on any exception, always closes."""

@contextmanager
def transaction(session: Session, *, immediate: bool = True) -> Iterator[Session]:
    """Explicit transaction; on SQLite uses BEGIN IMMEDIATE so lock contention fails fast."""

# Types
class UtcDateTime(TypeDecorator[datetime]): ...      # stores UTC, returns timezone-aware
class PortableJSON(TypeDecorator[Any]): ...          # JSONB on PostgreSQL, JSON elsewhere
def ulid_primary_key() -> Mapped[str]: ...           # CHAR(26), default from baseaicore.new_id
def measurement_columns(name: str) -> tuple[Column, Column]:
    """Returns (``<name>``, ``<name>_unavailable_reason``) — NULL alone never means 'unmeasurable'.

    The names are fixed, not suggested: a schema that stores a measurement in one column typed
    'Measurement' has lost the distinction this pair exists to keep, and a metadata test asserts
    the pairing across every consumer's tables.
    """

def upsert(session, model, values, *, index_elements) -> None:
    """Dialect-correct ``INSERT … ON CONFLICT DO UPDATE`` against a named unique index.

    The one sanctioned upsert in the suite. Select-then-insert is a race under both dialects, and
    hand-written ``ON CONFLICT`` is how a dialect-specific variant gets in
    (:doc:`ADR-0006 <../../adr/0006-sqlite-and-postgresql-roles>`).
    """

# Migrations
class MigrationRunner:
    def __init__(self, engine: Engine, *, script_location: str, version_table: str = "alembic_version") -> None: ...
    def current(self) -> str | None
    def heads(self) -> tuple[str, ...]
    def is_at_head(self) -> bool
    def upgrade(self, revision: str = "head", *, backup: bool = True) -> MigrationOutcome
    def downgrade(self, revision: str) -> MigrationOutcome
    def stamp(self, revision: str = "head") -> None
    def check_parity(self, metadata: MetaData) -> ParityResult   # autogenerate diff must be empty

# Backup and health
def backup(engine: Engine, destination: Path, *, compress: bool = False) -> BackupResult
def restore(engine: Engine, source: Path, *, confirm: bool) -> RestoreResult
def integrity_check(engine: Engine) -> IntegrityResult
def database_health(engine: Engine, runner: MigrationRunner | None = None) -> DatabaseHealth

# Errors
DatabaseError                MIGRATION_REQUIRED / MIGRATION_FAILED / STORAGE_BUSY /
├── MigrationRequired        STORAGE_FULL / DATABASE_UNAVAILABLE / SCHEMA_AHEAD
├── MigrationFailed
├── DatabaseUnavailable
├── SchemaAhead
└── StorageBusy

# Testing helpers (shipped as supported API)
weightsdb.testing.temporary_sqlite() -> Iterator[Engine]
weightsdb.testing.temporary_postgres() -> Iterator[Engine]
    # Skips when no server is configured, EXCEPT when WEIGHTSDB_REQUIRE_POSTGRES=1, which turns the
    # skip into a failure. CI sets it: a silently skipped dialect is an untested dialect, and the
    # both-dialects promise is only as good as its enforcement.
weightsdb.testing.migration_harness(script_location, metadata) -> MigrationHarness
```

## 8. Inputs

A database URL, an Alembic script location, an application's `MetaData`, filesystem paths for
backups.

## 9. Outputs

Engines, sessions, migration outcomes, backup and health results, typed errors.

## 10. Data ownership

**None.** WeightsDB owns no table and no row. It owns the `alembic_version` table's *management*, not
its content, and each application names its own version table.

## 11. Public contracts

1. `create_engine_for` applies the documented settings on every new connection, including after a
   pool recycle (verified by a test that forces reconnection).
2. `session_scope` commits on success and rolls back on **any** exception, including
   `KeyboardInterrupt`.
3. `UtcDateTime` always returns timezone-aware UTC on both dialects; naive input is rejected.
4. `upgrade(backup=True)` takes a backup first. **On SQLite** it restores that backup on failure and
   the original file is byte-identical afterwards. **On PostgreSQL there is no equivalent and none is
   claimed**: `pg_dump`/`pg_restore` is not byte-identical, restoring generally needs privileges the
   application's role deliberately does not hold, and a restore cannot run safely underneath a live
   database. PostgreSQL therefore runs the migration transactionally where the DDL permits and, where
   it does not, reports the exact revision reached, refuses to start, and names the backup and the
   command to restore it. `MigrationOutcome` states which behaviour applied; the difference is
   surfaced, not papered over. This is also why PostgreSQL's `auto_migrate` defaults to off.
5. `check_parity` returns a non-empty diff whenever models and migrations disagree.
6. Nothing in this package refers to any application's tables.

## 12. Configuration

Constructor and function arguments only. WeightsDB never reads a config file or an environment
variable; the application resolves configuration and passes values in.

## 13. Error behaviour

| Condition | Error / behaviour |
|---|---|
| Database unreachable | `DatabaseUnavailable` with a redacted URL (credentials stripped) |
| Revision behind head | `MigrationRequired` naming current, head and the exact command |
| Revision ahead of head | `SchemaAhead` — the database was written by a newer application version |
| Migration raises | `MigrationFailed`; automatic restore attempted; both the failure and the restore outcome reported |
| SQLite locked beyond `busy_timeout` | `StorageBusy` with the timeout value |
| Disk full during backup or migration | `StorageError` (`STORAGE_FULL`), backup file removed |
| Restore of a corrupt backup | Refused before swapping; the live database is untouched |
| Naive datetime bound to `UtcDateTime` | `ValidationError` naming the column |

## 14. Security considerations

* Connection URLs are redacted (`postgresql://user:***@host/db`) in every error, log and health
  payload — a test asserts no password appears anywhere.
* Backups are written with mode `0600` inside a caller-provided directory, via
  `contained_path`-style validation.
* Restore requires an explicit `confirm=True`; there is no implicit destructive path.
* Parameterized SQL only; the package builds no SQL from user input outside Alembic DDL.
* PostgreSQL connections use the least-privileged role the application configures; WeightsDB never
  requests superuser features.

## 15. Performance

| Measure | Target |
|---|---|
| Engine creation | ≤ 50 ms |
| Session acquisition from the pool | ≤ 1 ms |
| Pragma application per connection (SQLite) | ≤ 2 ms |
| Backup of a 1 GB SQLite database | ≤ 30 s |
| `integrity_check` on 1 GB | ≤ 60 s |
| `check_parity` | ≤ 2 s |

## 16. Cross-platform

Fully portable. Path handling uses `pathlib`; the SQLite backup API is used rather than file copying,
so a live database can be backed up safely on any platform. A network-filesystem check warns when a
SQLite file appears to live on NFS/SMB.

## 17. Observability

* DEBUG logs for connection events and migration steps under `weightsdb.*`; no INFO+ logging from the
  library.
* `database_health()` supplies the `database` component of every application's health endpoint.
* Slow-query logging is offered as an opt-in SQLAlchemy event hook the application installs, with
  statement names but never parameter values.

## 18. Test strategy

| Area | Tests |
|---|---|
| Engine settings | Pragmas present on a fresh connection and after a forced reconnect; PostgreSQL timeouts applied |
| Session scope | Commit on success; rollback on exception (including `KeyboardInterrupt`); always closed; nested use rejected |
| Transactions | `BEGIN IMMEDIATE` used on SQLite; contention raises `StorageBusy` promptly rather than at commit |
| Types | `UtcDateTime` round-trip on both dialects; naive rejected; `PortableJSON` round-trip incl. nested and unicode; ULID default applied |
| Migrations | Fresh, stepwise, idempotent, downgrade, parity check catching a deliberately drifted model; failure + restore on SQLite, and failure + refusal-with-instructions on PostgreSQL |
| Upsert | Concurrent writers of one natural key produce one row and no error, on both dialects |
| Measurement columns | The generated pair is named `<name>` / `<name>_unavailable_reason`; a metadata scan finds no unpaired measurement column in either consumer |
| Backup/restore | Round-trip; live-database backup; corrupt-backup refusal; disk-full handling |
| Health | Every degraded condition produces the documented status |
| Security | No credential in any error, log or health payload |
| Two consumers | An integration test runs **two** independent schemas and migration histories against one WeightsDB, proving no coupling |
| Empty metadata | `weightsdb`'s own `MetaData` contains no table |
| Both dialects | The whole suite runs on SQLite and PostgreSQL in CI |

Coverage floor: **95 %**.

## 19. Compatibility and versioning

* Semantic versioning; pre-1.0 `0.x`.
* SQLAlchemy and Alembic version ranges are part of the contract; widening them is a minor bump,
  narrowing is major.
* Column type behaviour (storage format of `UtcDateTime`, `PortableJSON`) is a **major** contract:
  changing it would require a data migration in every consumer.

## 20. Acceptance criteria

1. Two applications use WeightsDB with completely independent schemas, migration histories and
   database files — proven by an integration test.
2. `weightsdb` declares no table; asserted by a test.
3. Migration failure restores the backup and leaves the original database byte-identical.
4. Every test passes on both SQLite and PostgreSQL.
5. No credential appears in any error, log or health output.
6. `mypy --strict`, `ruff`, `lint-imports` clean; coverage ≥ 95 %.

## 21. Future extensions

* Read-replica/read-only session support for PostgreSQL.
* Optional scheduled-backup helper (an application-level scheduler would call it).
* Query-plan assertion helpers for tests (`assert_uses_index`), currently duplicated in applications.
* Retention/pruning helpers for high-volume tables (samples, telemetry, events).
* Optional DuckDB analytics attachment for FreeWeight, if the aggregate budgets are ever missed
  ([ADR-0006](../../adr/0006-sqlite-and-postgresql-roles.md) revisit trigger).
