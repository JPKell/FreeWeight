# FreeWeight documentation

A **mirror**. The canonical documents live in the suite's own documentation repository; these
copies exist so this repository can be worked on standalone.

`scripts/sync_docs.py` writes them, and `scripts/sync_docs.py --check` fails when they have
drifted. Edit the canonical copy and re-run the script — an edit made here is overwritten.

Links that point outside this set — ADRs, standards, architecture notes — are **flattened to plain
text** by the sync, deliberately: a link to a file this repository does not contain looks navigable
and is not.

## Documents

- [Specification](apps/freeweight/spec.md) — what FreeWeight is, its surfaces, configuration,
  error behaviour and security posture
- [API reference](apps/freeweight/api.md) — `/api/v1`, error codes and statuses, exported schemas
- [Benchmark catalogue](apps/freeweight/benchmark-catalog.md) — every suite, its metrics, and how a
  metric acquires a value
- [Data model](apps/freeweight/data-model.md) — the tables and what each one owns
- [Subjective goals](apps/freeweight/subjective-goals.md) — user-authored goal suites, calibration
  and the jury
- [Development plan](apps/freeweight/development-plan.md) — the phases, their acceptance criteria
  and what each defers
- [Risk register](apps/freeweight/risks.md) — the risks this design accepts, and their tells
