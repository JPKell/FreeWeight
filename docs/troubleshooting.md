# Troubleshooting

`freeweight doctor` diagnoses each of these. Every heading below is one health component the doctor
reports, so the fastest first step for any problem is:

```bash
freeweight doctor
```

It prints one line per component with a `✓` (ok), `!` (degraded) or `✗` (unavailable) and the
detail that names the cause. The sections below explain each component's failures and their
remedies. A CI test asserts that every component the doctor reports has a heading here and vice
versa, so this guide cannot drift from what the doctor checks.

## database

The application's own SQLite (or PostgreSQL) database.

* **`pending migration: at <rev>, head is <head>`** — the database is behind this build. Run
  `freeweight db upgrade` (it takes a backup first). On PostgreSQL, `auto_migrate` is off by
  default; run the upgrade deliberately.
* **`integrity check failed`** — the SQLite file is corrupt. Restore the most recent backup with
  `freeweight db restore <path>` (see [backup and restore](backup-restore.md)).
* **`could not open the database`** — the `storage.database_url` path is unwritable, or a
  PostgreSQL server is unreachable. Check the path's permissions, or the connection string.

## provider

The model provider (Ollama by default).

* **`unavailable`** — the provider is not running or not reachable at `provider.base_url`. Start
  Ollama (`ollama serve`) or correct the URL. FreeWeight starts and serves its UI regardless;
  only a *run* needs the provider.
* **`degraded`** — the provider answered but reported a problem; the detail carries its own
  message.

## gpu_telemetry

Live GPU telemetry through SweatMeter.

* **`no GPU telemetry available (no_gpus)`** — no GPU was detected. Every quality benchmark still
  runs; only memory, KV-cache and energy benchmarks skip (spec §16). On a GPU machine, check that
  `nvidia-smi` is on `PATH`.

## machine

This host's static hardware/OS profile.

* **`host platform not fully identified`** — SweatMeter could not read `/proc` and `/sys` (a
  non-Linux host, or a restricted container). Quality benchmarks and exports still work.

## evidence

The capability-evidence store LoadCoach consumes.

* **`no capability evidence yet`** (ok) — you have not completed a run that produced evidence.
* **`evidence unreadable`** — usually a database not yet migrated; run `freeweight db upgrade`.

## prompts

The shipped prompt pack.

* **`prompt pack unreadable`** — the installed package is damaged. Reinstall FreeWeight. The
  detail names the pack hash when healthy, which you can compare against a release.

## sandbox

The code-execution sandbox tier (container → bwrap → refuse; ADR-0018).

* **`tier container (docker)`** / **`tier bwrap (bwrap)`** (ok) — a sandbox is available and which
  one a code-execution benchmark would use.
* **degraded, `no sandbox tier is available…`** — neither a container runtime (podman/docker) nor
  a functional `bwrap` is present. Every non-code benchmark still runs; code-execution benchmarks
  (EvalPlus, CRUXEval) are skipped with `sandbox_unavailable`, never run on the host. Install
  Docker or bubblewrap to enable them, or set `sandbox.tier` to match what you have.

## external_benchmarks

The external benchmark adapters (spec §4, ADR-0018).

* **`N adapter(s), M installed`** (ok) — how many external benchmarks are registered and how many
  have their environment installed. Install one with `freeweight external install <key>`; verify
  its datasets with `freeweight external verify <key>`.
* A `DATASET_HASH_MISMATCH` from `verify` means an installed dataset no longer matches its pinned
  hash — re-install it. A result measured against an unpinned dataset would not be comparable.

## goals

The user's goal packs (subjective goals).

* **`N goal pack(s) parse and validate`** (ok).
* **`M goal(s) fail lint: <names>`** — a pack under `goals.root` has an error-level lint finding.
  Run `freeweight goals validate <slug>` to see every finding at once, and fix the pack (it is
  hand-editable JSON you own).

## judges

The jury a goal's judged criteria are scored by.

* **`jury_size N`** (ok) — the configured jury size.
* **`jury disabled (jury_size ≤ 1)`** — judged criteria will skip with `judge_unavailable`;
  rule-only goals are unaffected. Set `judge.jury_size` to 3 (the default) and ensure at least
  three distinct eligible models are installed. `freeweight judges validate` reports whether a
  jury can actually be assembled from the installed models.

## Common non-component problems

* **`freeweight serve` refuses to start with `INSECURE_BINDING`** — a non-loopback
  `server.host` needs both `server.allowed_hosts` and `auth.tokens`, and `0.0.0.0` additionally
  needs `server.allow_lan_exposure = true`. This is deliberate (ADR-0026); a typo in a host string
  must not expose the service.
* **A page returns `421 Misdirected Request`** — you reached the UI through a hostname the bind
  does not accept. On a non-loopback bind, add it to `server.allowed_hosts`.
* **A form submission returns `403 CSRF_FAILED`** — the page's CSRF cookie is missing. This
  happens if cookies are blocked, or if you are reaching a loopback bind over plain HTTP through a
  hostname other than `localhost`/`127.0.0.1` (a `__Host-` cookie needs a secure context). Reach
  it as `http://localhost:8765` or terminate TLS in front.
