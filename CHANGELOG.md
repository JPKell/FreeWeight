# Changelog

All notable changes to `freeweight` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

### Added
- Phase 1: `freeweight serve` starts a web server answering `GET /api/v1/health` and
  `GET /api/v1/version`, and rendering the application shell; `freeweight health --json` and
  `freeweight version --json` return the same data through the same service layer. Typer CLI with
  `serve`, `health`, `version`, `doctor`, and `config show|validate|init|path`. Typed settings with
  full precedence (defaults → file → environment → CLI) and source tracking, with the documented
  unsafe-binding refusals (non-loopback without tokens or `allowed_hosts`; `0.0.0.0` without
  `allow_lan_exposure`). Structured JSON/text logging with request-ID correlation. The standard
  error envelope, `Host`-header validation, and a request body size limit.
- Phase 2: the storage foundation. SQLAlchemy models for `machines`, `models`, `model_descriptors`,
  `runtime_profiles`, `settings` and `api_tokens`, with the `models` table's partial unique index
  enforcing at most one `name_only` identity per `(provider_kind, provider_model_name)` and a
  repository method that upgrades that row in place when a digest later arrives, rather than
  duplicating it. Engine and session plumbing written as if it were the future `weightsdb` package:
  dialect-correct pragmas (SQLite `foreign_keys`/`WAL`/`busy_timeout`/`synchronous`, PostgreSQL
  `statement_timeout`/`lock_timeout`/`application_name`) applied on every connection including
  after a pool reconnect, `BEGIN IMMEDIATE` transactions on SQLite, a portable `UtcDateTime` and
  `PortableJSON`, and the one sanctioned dialect-correct upsert. Alembic migration `0001` creates
  the full Phase 2 schema; `MigrationRunner` wraps it with an autogenerate parity check, and
  `freeweight db upgrade` takes an automatic backup first and restores it on failure (SQLite).
  `freeweight db upgrade|status|backup|restore|vacuum`. The `database` health component, backed by
  the same startup revision check as `freeweight serve`.
- Phase 2: the machines and models pages, server-rendered from the real tables, with the empty and
  error states UI standards §6 requires. Both are legitimately empty until Phase 3 (models) and
  Phase 4 (machines) fill them.
- `storage.backup_retention` (default 5) and `storage.statement_timeout_ms` (PostgreSQL only,
  unset by default).
- `freeweight db status` reports database size, and `freeweight db vacuum` previews the space it
  expects to reclaim before running and reports what it actually reclaimed (database standards §7,
  §8).
- Automatic pre-migration backups carry the schema revision in their name and rotate to
  `storage.backup_retention` generations, with each rotation logged (database standards §7).
- Repository scaffold generated from the suite's development plan.
- A read-only transaction mode (``session_scope(..., read_only=True)``, ``Database.read()``). On
  SQLite it takes a deferred ``BEGIN`` instead of ``BEGIN IMMEDIATE``, and is enforced with
  ``PRAGMA query_only`` rather than trusted.
- ``Database``, the application's connection handle. The web application creates one in its
  lifespan and disposes it at shutdown; a CLI command owns one for the length of the command.
  Service functions take a handle and no longer build engines of their own.
- ``storage.statement_timeout_ms`` (PostgreSQL only; also applied as ``lock_timeout``).

### Fixed
- `freeweight db restore` silently restored nothing when the database had uncheckpointed writes in
  its WAL sidecar. The main database file was replaced, the stale `<db>-wal` was not, and the next
  reader replayed it straight back on top — so the writes the restore was called to undo survived,
  while the command reported success. Restore now checkpoints, releases the pool and removes the
  sidecars before the swap. This also affected the automatic restore after a failed migration, and
  therefore database standards §7's byte-identical guarantee.
- `freeweight db restore` deleted its `.pre-restore` rollback copy without ever checking that the
  restored file opened, and never verified that the backup sat at a revision this build knows.
  Both are now done, and a restored file that fails its integrity check puts the original back.
- `ModelRepository.upsert_identity` raised a raw `IntegrityError` out of the repository when a
  provider reported a digest, then omitted it, then reported it again — a sequence Ollama produces
  in practice, and the first thing Phase 3's discovery loop would have hit.
- Every PostgreSQL connection carrying an `application_name` failed outright: `SET x = %s` is a
  syntax error, since PostgreSQL's `SET` takes no bind parameter. It now uses `set_config`. The
  setting was also never passed by `build_engine`, so it had no effect even where it worked.
- `freeweight db backup` with no `--output` failed on PostgreSQL with "Expected a SQLite engine",
  and `freeweight db restore` failed the same way instead of naming the `pg_restore` command.
- `storage.auto_migrate` defaulted to true on both dialects. It now defaults to false on
  PostgreSQL, where a failed migration cannot be rolled back automatically (database standards
  §5.1, §7); an explicit setting is still honoured on either dialect.
- `freeweight db backup` no longer writes an empty, successful-looking backup when the SQLite
  database does not exist, and creates the backup file with mode `0600` before writing data into
  it rather than narrowing the permissions afterwards.
- The machines and models pages return their error state rather than a 500 when the database
  cannot be read.
- Every transaction took SQLite's single write lock, including transactions that only read, so two
  concurrent page views queued behind each other for up to ``busy_timeout`` and then failed. WAL's
  concurrent reads are back for the paths that only read. ``BEGIN IMMEDIATE`` stays for writes: a
  deferred read-then-write transaction gets ``SQLITE_BUSY_SNAPSHOT``, which ``busy_timeout`` does
  not apply to — measured, it fails in 0.0 ms with a 2000 ms timeout configured — and whose only
  escape is discarding work and starting over.
- The application rebuilt an engine, and therefore a connection pool and a compiled-statement
  cache, for every database read — including once per page view. Measured on this schema, the same
  query cost 0.95 ms that way against 0.12 ms through a live handle on SQLite, and 7.41 ms against
  0.38 ms on PostgreSQL, where each call also opened a fresh backend connection and made the
  configured ``pool_size`` meaningless.
- ``GET /api/v1/health`` reported on a connection it opened for the check rather than the one the
  server serves requests from.

### Changed
- The migration suite and the repository suite both run on SQLite **and** PostgreSQL in CI, rather
  than one PostgreSQL smoke test alongside a SQLite-only suite. `upsert()`'s `ON CONFLICT` branch
  and the savepoint-retry path that exists for PostgreSQL's MVCC were previously executed by
  nothing.
- `MigrationOutcome.restored`, which could never be `True`, is replaced by
  `restore_on_failure_available`, which states the dialect difference database standards §7
  requires to be stated rather than papered over. `MigrationOutcome` also carries `pruned_backups`.
- Widened the `sweatmeter` pin to `>=0.4,<0.5`. SweatMeter's first published release is `0.4.0`
  (`0.3.0` completed its development plan but never reached the index), and it adds the in-process
  NVML GPU backend, selected automatically wherever the optional `pynvml` extra is installed.
