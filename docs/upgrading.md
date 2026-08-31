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
