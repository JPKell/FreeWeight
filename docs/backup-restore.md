# Backup and restore

Your measurements are the point of FreeWeight, and a measurement does not expire — a result taken
six months ago is as true as one taken today. So FreeWeight never deletes on a timer, and it backs
up before anything destructive.

## Backups happen automatically before a migration

Every `freeweight db upgrade` (and every automatic startup migration on SQLite) takes a backup
first and restores it if the migration fails. `storage.backup_retention` (default 5) is how many of
these automatic pre-migration backups are kept; older ones rotate out. Backups live inside the data
root at `~/.local/share/freeweight/backups/`, written `0600`, and are never uploaded anywhere.

## Taking a backup on demand

```bash
freeweight db backup                     # writes a timestamped copy into the backups directory
freeweight db backup --output /path/to/freeweight-backup.sqlite3
```

On SQLite this is a consistent online copy (it checkpoints the WAL first). On PostgreSQL the command
prints the `pg_dump` invocation to run, because a database server's backup is the operator's to
schedule.

## Restoring

```bash
freeweight db status                     # shows the current revision, size and integrity
freeweight db restore /path/to/backup.sqlite3
```

`restore` refuses a file that is not a valid database and refuses to overwrite silently — it is a
destructive operation, so it previews what it will replace and requires confirmation (`--yes` to
skip the prompt in a script). After a restore, run `freeweight db status` to confirm the revision
and integrity.

## What is and is not in a backup

A backup is the whole database: machines, models, runs, samples, metrics, telemetry, capability
evidence, goals and the user's calibration grades. The grades are the ground truth of the
subjective-goal feature and are user data — they are backed up with the database and never
transmitted anywhere.

Large artifacts (raw model responses stored out-of-row) live under `~/.local/share/freeweight/
artifacts/`; back that directory up alongside the database if you rely on stored full responses.
Goal packs are hand-editable JSON under `~/.config/freeweight/goals/` — track them in git.

## Deleting deliberately

There is no time-based retention. The deletion you usually want is *by model* — remove every run of
a model you no longer have installed:

```bash
freeweight results delete --scope model --model <ref>     # previews, then confirms
```

Deleting results never removes model or machine history, and always previews exactly what it will
remove first (spec §20 AC11).
