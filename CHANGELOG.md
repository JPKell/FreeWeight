# Changelog

All notable changes to `freeweight` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

### Added
- Phase 7: five deterministic quality suites. `native.instruction_following` checks
  machine-checkable constraints across format, length, keyword, structure and language classes and
  reports strict, loose and instruction-level accuracy separately; `native.structured_output`
  measures JSON validity and JSON-Schema conformance with one corrective retry, reporting the
  first-attempt rate and the recovery rate as distinct figures; `native.tool_use` covers the
  catalog's eleven tool scenarios; `native.tool_recovery` injects six deliberate tool failures;
  and `native.agent` scores four multi-step goals on their trajectory as well as their answer.
  **No model scores anything in any of them.**
- A mock tool harness over shipped fixture data (`freeweight.benchmarks.fixtures`): ten tools —
  calculator, `read_file`, `list_directory`, `search_text`, `search_symbol`, `lookup_record`,
  `database_query`, `get_inventory`, `write_sandbox_file`, `run_mock_test` — with no shell, no
  unrestricted filesystem, no network and no real database (spec §14). Reads are contained to the
  fixture repository and writes to a run-scoped sandbox by `contained_path`, which resolves
  symlinks before deciding; arguments are validated against each tool's own schema before it runs;
  and the calculator parses arithmetic with its own recursive-descent parser rather than any form
  of `eval`.
- Six scorers, all on rung 2 of the scoring ladder: exact match with declared normalization,
  constraint checking, a bounded JSON-Schema validator that **refuses** a keyword it does not
  decide rather than reporting conformance it never checked, tool selection, tool arguments, and
  agent trajectory.
- `freeweight prompts list|show|build` (prompt standards §3). `build` regenerates the pack
  manifest; `build --check` reports drift and exits 5 without writing, so a prompt record edited
  without a regenerated manifest fails CI instead of shipping hashes that describe prompts nobody
  installed.
- Benchmark prompt records for the five suites, each declared in its suite's manifest so its
  `prompt_subset_hash` covers exactly the prompts that suite renders.
- The `tool_calls` table (migration `0004`), specified in the data model since the freeze and
  unreachable until there were suites that call tools. One row per invocation a model requested,
  written in the same transaction as its sample and cascade-deleted with it, so a tool metric
  drills to the call that went wrong rather than to a rate. A call naming a tool that was never
  offered is a row with `status = "unknown_tool"`, not a missing one; `correct_tool` and
  `correct_arguments` are `NULL` — never `false` — where the case declares nothing to compare
  against. Tool results are stored as a hash and a short digest, never in full.
- Phase 6: the first real measurements. `native.performance` measures prompt-evaluation and
  decode throughput at the catalog's prompt sizes and output lengths, time to first token,
  streamed inter-chunk latency and cold model load; `native.token_economy` measures what an
  answer costs in tokens, characters, words and bytes, with the four derived per-success figures.
  Cold and warm results are separated rather than averaged, and a per-token latency figure appears
  only where the provider reports one token per streamed delta.
- The prompt library (`freeweight.services.prompts`): versioned JSON prompt records, a pack
  manifest, a `StrictUndefined` renderer, canonical record hashing and `prompt_subset_hash`, with
  the whole pack loaded and validated once at startup. The **per-benchmark subset hash** — not the
  pack hash — is the reproducibility-fingerprint input, so editing a prompt no benchmark uses
  separates no results (ADR-0028). Written as an in-application module that becomes
  `setspec.prompts` at LoadCoach P4.
- The reproducibility fingerprint of Machine Identity §4, assembled in
  `freeweight.domain.provenance`: model identity and digest, runtime profile, provider and
  version, machine, the drift-sensitive environment, the benchmark's manifest hash and prompt
  subset hash, the resolved execution parameters, the served context with its source, and the
  target GPU index. The full document is stored, not only its hash, and two documents diff field
  by field.
- `freeweight run repeat <run>` re-executes a recorded run with its identical frozen
  configuration; `--check` prints the field-level provenance diff afterwards. It refuses, naming
  every field that moved, when the model digest, the machine, the provider version or a dataset
  hash has changed, and `--force` proceeds and records the divergence on the new run.
- Telemetry persisted for the duration of a run, split across two tables and migration `0003`:
  `telemetry_samples` holds one row per observation with the host fields and
  `telemetry_gpu_samples` one row per visible device, so no host field is double-counted on a
  multi-GPU machine (ADR-0027). Every derived figure names its device, and memory, power, energy
  and temperature are `unsupported` with `multi_gpu_placement_unknown` when more than one GPU is
  visible and the provider does not report placement.
- The telemetry-sampling overhead is measured on the machine before each run and stored on the
  run (`runs.telemetry_overhead_percent`), so the distortion is provenance rather than an
  assumption (spec §15).
- Idle detection with a defined outcome (spec §13): the run waits for GPU and CPU to fall below
  `execution.idle_gpu_threshold_percent`; `on_idle_timeout = "warn"` proceeds and records a
  `measured_while_busy` degradation with the observed utilization, and `"refuse"` fails the run
  with `INSUFFICIENT_RESOURCES` and the same numbers.
- The served context is resolved and recorded with its source (`configured` | `reported` |
  `assumed`), and a benchmark case that needs more context than the model is served is stored as
  a `skipped` sample with its reason rather than sent and failed.
- The run detail page shows the provenance table, the stored fingerprint document, per-device
  telemetry charts (server-rendered inline SVG with a text summary beside each), metric spread and
  device attribution; the sample drill-down names the prompt record and version behind every
  sample and shows its time to first token.
- `execution.cooldown_seconds` is now honoured between tests, and `execution.gpu_index` and the
  idle settings are frozen into every run's effective configuration.

### Changed
- A benchmark test's `requires.provider_capabilities` is now **enforced**. A provider that has not
  declared a capability a test needs makes that test `skipped` with
  `run_tests.skip_reason = "unsupported_capability"` and `CAPABILITY_UNSUPPORTED` on the row; the
  test produces no samples and contributes no score, and the run completes normally. A model
  without tool calling therefore yields a recorded skip, never a low score (spec §13, graceful
  degradation).
- The run engine can execute a benchmark test through a declared *interaction* — a bounded tool
  loop, or a call plus one corrective retry — instead of a single provider call. Token counts are
  summed across an interaction's turns, the whole trajectory is stored on the sample, and a
  trajectory is scored by a trajectory scorer rather than on its final sentence.
- Aggregation now reads a per-sample metric value from the scorer's own detail where the scorer
  measured one, so a suite whose scorer measures several things at once reports each under its own
  key. A sample that did not measure a given figure is excluded from it with
  `not_measured_for_this_case` rather than contributing a zero (ADR-0016).
- Every capability name a benchmark test requires is validated against `ProviderCapabilities` when
  the registry is built — which is startup. An unknown name is treated as *unmet* at run time, so a
  manifest typo would otherwise have skipped its suite on every provider and reported a plausible
  reason for doing so.
- A `MockToolbox` offering `write_sandbox_file` must be given a `sandbox_root`; the default toolbox
  offers every tool except that one and cannot write at all. No shipped case writes, and no default
  directory is invented for one that might.
- `GET /api/v1/runs/{id}` gained `provenance` (served context and source, GPU attribution,
  telemetry overhead, prompt pack, fingerprint document) and `degradations`; metric objects gained
  `gpu_index`, `stddev` and `coefficient_of_variation`.
- Aggregation moved out of the run service into `freeweight.domain.aggregation`, which derives
  each metric from the stored samples through `freeweight.domain.metrics` and refuses to combine
  tests of different measurement classes into one run-level figure.
- A benchmark test that cannot enumerate its cases now fails only that test at *every* stage,
  including run creation and preparation, rather than failing the run (spec §13).

- Phase 5: the run engine, end to end against `FakeProvider`. `freeweight run start --suite
  native.echo` queues a run, executes it (claim → prepare → warm → execute → aggregate →
  complete), stores every raw sample, and streams progress to the browser and the terminal;
  `freeweight run list|show|cancel|wait` and the run list, detail and sample pages read the same
  data through the same service functions as `POST /api/v1/runs`, `GET /api/v1/runs`,
  `GET /api/v1/runs/{id}`, `POST /api/v1/runs/{id}/cancel`, `GET /api/v1/runs/{id}/tests`,
  `GET /api/v1/runs/{id}/tests/{test_id}/samples` and `GET /api/v1/runs/{id}/events`.
- Eight new tables and migration `0002`: `benchmark_suites`, `benchmark_tests`, `runs`,
  `run_tests`, `samples`, `metric_values`, `run_events` and `artifacts`. Results cascade downward
  and never delete identity upward (`ON DELETE RESTRICT` on every model, machine, descriptor and
  suite reference), and `ck_samples_score_null_unless_completed` states in DDL that a sample which
  is not `completed` cannot carry a score at all.
- The run and test state machines as explicit, enumerable transition tables
  (`freeweight.domain.run_state`), with every legal transition, every illegal one and the
  immutability of every terminal state asserted over all 81 (and 36) ordered pairs.
- Persisted run events with gap-free per-run sequences from 1, and `GET /api/v1/runs/{id}/events`
  (SSE) with `Last-Event-ID` replay. Events are committed before they are published, so replay,
  a reload mid-run and a restart all resume with no gap and no duplicate; the run detail page
  renders the sequence it got to, and the browser reconnects from exactly there.
- `native.echo`, a trivial deterministic self-test suite that exercises the whole engine on any
  provider. It measures FreeWeight, not the model: it declares no capabilities and emits no
  capability evidence.
- A single-threaded run scheduler with startup recovery. One GPU workload at a time is a property
  of the claim (`UPDATE … WHERE status = 'queued'`, refused while any run is in flight), so a
  `freeweight run start` typed while a server is serving queues the run and exits `7` rather than
  running it concurrently. A run left mid-flight by a killed process is recovered as `interrupted`
  — never `failed` — keeps its completed tests, and resumes from the exact (case, repetition) it
  had reached.
- Cancellation honoured at every phase boundary: `queued`, `preparing` and `warming` cancel
  outright, `running` enters `cancelling` and stops at the executor's next check. `Ctrl-C` during
  `freeweight run start` or `run wait` cancels cleanly, preserves committed samples and exits `6`.
- `[execution]` configuration section (`warmup_repetitions`, `measured_repetitions`,
  `cooldown_seconds`, `test_timeout_seconds`, `run_timeout_seconds`, `randomize_case_order`,
  `seed`, `gpu_index`, the idle-detection group, `on_idle_timeout`, `store_responses`), resolved
  once per run and frozen into the run record.

- Phase 4: telemetry and machine profile. The current host is profiled through SweatMeter once at
  server startup and upserted by fingerprint (`last_seen_at` refreshed on every later startup),
  populating the machines page for the first time. A single telemetry sampler, owned by the web
  application for as long as it serves, backs the telemetry bar shown on every page (CPU, RAM, GPU
  utilization and temperature, VRAM, power — `—` with a reason for anything unsupported, e.g. no
  GPU), `GET /api/v1/system/telemetry/stream` (SSE, `telemetry.sampled` events, 15 s heartbeat) and
  `GET /api/v1/system/status`. Two new health components, `gpu_telemetry` and `machine`, join
  `database` and `provider` on `GET /api/v1/health`, `freeweight health` and `freeweight doctor`; a
  machine with no GPU degrades the application without making it unavailable.
- Phase 3: model discovery through ModelRack exclusively. `freeweight models refresh` (and the
  models page's "Refresh from provider" action) lists every model the configured provider serves,
  upserts its canonical identity, and stores an immutable descriptor snapshot only when its content
  actually changed — a second refresh with nothing changed leaves the descriptor history exactly as
  long as it was. A digest present yields `identity_confidence = digest`; absent yields `name_only`;
  a name later gaining a digest upgrades that row in place rather than duplicating it; a *changed*
  digest creates a new identity and leaves the old one's history untouched. `freeweight models
  list|show` and the models list/detail pages, with `show`/`GET /models/{model_ref}` falling back to
  a live provider resolution when a reference (a bare name, an old tag, an unambiguous prefix) is not
  yet stored, recording the alias it resolved through rather than hiding it. The provider is
  constructed exactly once, in the composition root
  (`freeweight.infrastructure.providers.factory`) — the only module in this application that reaches
  provider HTTP code, asserted by a boundary test scanning the rest of the source tree.
- The `provider` health component (version and model count on success; `unavailable`/`degraded` with
  a reason otherwise), reported by `GET /api/v1/health`, `freeweight health` and `freeweight doctor`.
  An unreachable provider degrades the application; per Graceful Degradation §3, an optional
  component never makes it `unavailable` on its own — only the required `database` component can.
  With the provider down, the models page and `models list` still work: they read the last
  discovery attempt's recorded outcome rather than probing the provider on every view, so they say
  the data may be stale instead of pretending it is current.
- `provider.kind = "fake"`, constructing `modelrack.testing.FakeProvider` — alongside the production
  `"ollama"` adapter — so the running application, not just its unit tests, can be exercised with no
  GPU, no Ollama and no network (testing standards §1).
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
- `tests/e2e/test_run_journey.py` raced the event publisher by exactly one event: a run reaches its
  terminal status in the database *before* its terminal event is published — deliberately, so a
  client can never see a closed stream without a terminal frame — and the test compared event
  counts across that window. The test now waits for the stream to go quiet; the ordering it was
  testing is unchanged.
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
- `GET /api/v1/system/status` now reports the real `active_run` and `queue_depth`. Both are `null`
  when the queue cannot be read — a database behind head, say — rather than a reassuring `0`.
- Widened the `sweatmeter` pin to `>=0.4,<0.5`. SweatMeter's first published release is `0.4.0`
  (`0.3.0` completed its development plan but never reached the index), and it adds the in-process
  NVML GPU backend, selected automatically wherever the optional `pynvml` extra is installed.

