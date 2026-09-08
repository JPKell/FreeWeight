# Upgrading

FreeWeight migrations are forward-only, tested from every released version, and take a backup
before they touch anything. Your measurements survive an upgrade unchanged — a metric is never
silently reinterpreted (if a metric definition changes, the metric gets a new key and the old key
is retained; spec §19).

## The normal upgrade

```bash
pip install --upgrade freeweight
freeweight db upgrade      # backs up, migrates to head, restores on failure
freeweight db status       # confirm: at head, integrity ok
freeweight serve
```

On SQLite the startup migration is automatic by default (`storage.auto_migrate = true`), so simply
starting the new version migrates it — with a pre-migration backup taken first. On PostgreSQL
`auto_migrate` defaults to **off**: run `freeweight db upgrade` deliberately, because the automatic
restore-on-failure guarantee is SQLite-only and a shared PostgreSQL database should not migrate
itself on first startup.

## What a migration does and does not do

* It **adds** schema; it never reinterprets stored numbers. Two runs of the same subject with the
  same fingerprint stay comparable across an upgrade.
* It takes a backup into `~/.local/share/freeweight/backups/` first and restores it if the
  migration fails (SQLite). See [backup and restore](backup-restore.md).
* A database written by a *newer* FreeWeight than the one you are running is refused with
  `SCHEMA_AHEAD` rather than downgraded — downgrades are not supported.

## Migration notes

| Version | Migration | What it adds |
|---|---|---|
| 1.1.2 | none | No schema change — a release-plumbing version bump only; nothing behaves differently for an operator. |
| 1.1.1 | none | No schema change — a `mirrorwall` dependency-floor fix (so the declared lowest range resolves) plus test and documentation additions; nothing behaves differently for an operator. |
| 1.1.0 | `0008` | The `adapters` table, `runs.adapter_id`, and `capability_evidence`'s `adapter_id` + `subject_canonical_id`. Every existing row is a **base subject** — `adapter_id` is `NULL` and `subject_canonical_id` is backfilled from `models.canonical_id`. Additive throughout; no existing value changes |
| 1.0.0 | — | The 1.0 schema this document's baseline assumes |

## Behaviour changes at 1.1.0

* **Nothing changes for an installation with no `[adapters] directory`.** The feature is off by
  default (empty directory), and with it empty FreeWeight measures exactly as 1.0 did.
* **`provider.kind = "llamacpp"` is new.** Naming it before 1.1 was a configuration error; it now
  serves GGUF weights from `model_directory` (required) through a supervised llama.cpp server.
* **A run under `--adapter` now actually serves the adapter** (fixed in this release —
  `_build_request` previously never set `GenerationRequest.adapter`, so every adapter-bearing run
  before 1.1.0 measured the bare base while recording an adapter's identity). **Every
  adapter-bearing evidence record produced before 1.1.0 measures the bare base and should be
  discarded and re-measured.**
* **`GET`/`PUT /api/v1/settings` gains the suite's `settings`/`definitions` shape** beside the
  original `items`, which is unchanged and now deprecated (removed only at a future `/api/v2`).
* **The comparison grouping caps a base's subjects at 12**, naming what it withheld — a base with
  more subjects no longer renders one row apiece with no way to compare anything in.
* **A migration run now suspends SQLite foreign-key enforcement for its own connection.** This
  matters only if you run `alembic` commands directly against a SQLite database outside
  `freeweight db upgrade`; the CLI path already accounts for it.

## Rollback

If you need to go back to the previous version:

1. Reinstall the previous FreeWeight (`pip install freeweight==<old-version>`).
2. Restore the pre-migration backup the upgrade took:
   `freeweight db restore ~/.local/share/freeweight/backups/pre-migration-<rev>.sqlite3`.

A database migrated forward cannot be opened by an older build (it would be `SCHEMA_AHEAD`), which
is why the rollback path is *restore the backup*, not *downgrade the schema*. The pre-migration
backup is exactly the database as it was before the upgrade.

## Checking an upgrade before committing to it

```bash
cp ~/.local/share/freeweight/freeweight.sqlite3 /tmp/fw-test.sqlite3
FREEWEIGHT_STORAGE__DATABASE_URL="sqlite:////tmp/fw-test.sqlite3" freeweight db upgrade
FREEWEIGHT_STORAGE__DATABASE_URL="sqlite:////tmp/fw-test.sqlite3" freeweight db status
```

This migrates a copy, leaving your real database untouched until you are satisfied.
