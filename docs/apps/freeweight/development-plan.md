# FreeWeight — Development Plan

**Sequence position:** first application. Begins after BaseAiCore P4, SetSpec P1–P3, ModelRack P2
(FakeProvider) and SweatMeter P1–P2 are available.
**Milestones:** M2 (beta) at P10 · **M3 (FreeWeight 1.0-rc, contract freeze) at P11** · **M6 (FreeWeight 1.0) at P14**.

Every phase ends in something a person can run and look at. No phase is "implement the backend".

---

## Phase 1 — It runs, it answers, it has a CLI

**Goal:** `freeweight serve` starts a server that renders a page and answers `/api/v1/health` and
`/api/v1/version`; `freeweight health --json` prints the same health from the same code.

**Prerequisites:** `baseaicore>=0.4,<0.5`.

**Work**
* Repository skeleton per [Packaging Standards](../../standards/packaging-and-release-standards.md);
  `src/freeweight/` with `domain/ services/ infrastructure/ web/ cli/ observability/`.
* Typed settings with the documented precedence and startup validation, including the unsafe-binding
  refusals ([Configuration Standards](../../standards/configuration-standards.md)).
* FastAPI app: `/api/v1/health`, `/api/v1/version`, error-envelope handler, request-ID middleware,
  size limits, one HTML page (the shell with a placeholder body).
* Typer CLI: `serve`, `health`, `version`, `config show|validate|init|path`, with lazy imports so
  `--help` stays fast.
* Structured logging with request-ID binding; text/JSON formatter selection.
* `.importlinter` layer contracts; CI workflow.

**Files/subsystems**
```text
src/freeweight/{__main__,__about__,config,bootstrap}.py
src/freeweight/web/{app,routes/system,errors,middleware}.py  src/freeweight/web/templates/base.html
src/freeweight/cli/{main,commands/system,commands/config}.py
src/freeweight/services/health.py   src/freeweight/observability/logging.py
tests/unit/test_config.py  tests/e2e/{test_server_boot,test_cli_basics}.py
```

**Tests**
* Config precedence at each level; unknown key rejected; every unsafe combination refuses to start.
* `/api/v1/health` shape matches the documented schema; unknown API route returns a structured 404;
  no filesystem path is disclosed by a bad static request.
* CLI: exit codes 0/2/3; `--json` output shape; `--help` at 80 columns; `--help` imports neither
  SQLAlchemy nor httpx (asserted on `sys.modules`).
* Request ID generated, echoed and present in logs.

**Acceptance criteria**
1. `python -m freeweight` serves on `127.0.0.1:8765` and renders the shell page.
2. `freeweight health --json` and `GET /api/v1/health` return identical component data.
3. Binding to `0.0.0.0` without tokens refuses to start with exit 3.
4. CI green: format, lint, types, import-linter, tests.

**Known risks:** none material. **Likely failure modes:** business logic creeping into route
handlers from phase one — the layering contract is in place before any is written.
**Gold standards:** zero-configuration start; thin edges; structured logs.
**Deferred:** database, providers, telemetry, benchmarks.

---

## Phase 2 — Storage foundation

**Goal:** the database exists, migrates, and the UI shows a real (empty) machines/models page backed
by real tables.

**Prerequisites:** P1.

**Work**
* SQLAlchemy models for `machines`, `models`, `model_descriptors`, `runtime_profiles`, `settings`,
  `api_tokens` ([Data Model](data-model.md)).
* Engine/session plumbing written **as if it were a package** (it becomes WeightsDB later): pragmas,
  session scope, transaction helper, portable types.
* Alembic: migration `0001`, autogenerate parity check in CI, startup revision check with the
  documented auto/refuse behaviour per dialect.
* Repositories for machines and models; `freeweight db upgrade|status|backup|restore|vacuum`.
* Backup before migration, restore on failure.

**Files/subsystems**
```text
src/freeweight/infrastructure/db/{engine,session,types,base,models,repositories/*}.py
src/freeweight/infrastructure/db/migrations/**   src/freeweight/services/database.py
src/freeweight/cli/commands/db.py
tests/integration/{test_migrations,test_repositories}.py  tests/unit/test_db_types.py
```

**Tests**
* Fresh migration, stepwise, idempotent, downgrade, failure + restore — on SQLite **and** PostgreSQL.
* Autogenerate parity: a deliberately drifted model fails the check.
* Timezone-aware round-trip; naive datetime rejected.
* Model uniqueness: a `name_only` row is upgraded in place when a digest later appears, never
  duplicated.
* `ON DELETE RESTRICT` prevents removing a model that has descriptors.

**Acceptance criteria**
1. `freeweight db upgrade` on an empty directory creates a database at head in ≤ 2 s.
2. A failed migration leaves the original database byte-identical and reports both outcomes.
3. Both dialects green in CI.

**Known risks:** dialect differences. **Likely failure modes:** SQLite-only assumptions; pragmas lost
after a pool reconnect (explicitly tested).
**Gold standards:** safe migrations; no drop-and-recreate path anywhere.
**Deferred:** run/sample tables (Phase 5).

---

## Phase 3 — Model discovery through ModelRack

**Goal:** *FreeWeight can discover Ollama models exclusively through ModelRack and persist canonical
BaseAiCore model identities* — visible on a Models page and via `freeweight models list`.

**Prerequisites:** P2; `modelrack>=0.5,<0.6` (P3 complete).

**Work**
* Provider construction in the composition root only; a `ModelProvider` port in `services/`.
* Discovery service: list → normalize → upsert identity → store descriptor snapshot → record alias
  resolutions.
* Models list and detail pages; `models list|show|refresh` CLI commands with `--json`.
* Health component `provider` with version and model count; degraded when unreachable.

**Files/subsystems**
```text
src/freeweight/services/models.py  src/freeweight/infrastructure/providers/factory.py
src/freeweight/web/routes/models.py  src/freeweight/web/templates/models/*.html
src/freeweight/cli/commands/models.py
tests/unit/test_model_discovery.py  tests/e2e/test_models_flow.py
```

**Tests**
* Discovery against `FakeProvider` with 0, 1 and 20 models; against recorded Ollama fixtures.
* Digest present ⇒ `identity_confidence = digest`; absent ⇒ `name_only`, surfaced in UI and API.
* Re-discovery is idempotent; a changed digest creates a **new** identity and keeps the old one.
* An alias resolution is recorded, not hidden.
* Provider unreachable: page renders with the last known models marked stale; health degraded; no
  crash.
* Boundary test: no `httpx` import anywhere in `freeweight` outside the provider factory.

**Acceptance criteria**
1. With Ollama running, `freeweight models refresh` discovers and persists every model with its
   digest, and the UI lists them with quantization, parameters and context.
2. FreeWeight contains no provider HTTP code (asserted).
3. With Ollama stopped, the page and CLI still work and say why the data is stale.

**Known risks:** identity churn from retags. **Likely failure modes:** silently overwriting a
descriptor and losing history; treating an alias as an identity.
**Gold standards:** one canonical identity; provider isolation; honest staleness.
**Deferred:** benchmarking anything.

---

## Phase 4 — Telemetry and machine profile

**Goal:** the persistent telemetry bar is live on every page, and the machine this instance runs on is
identified and stored.

**Prerequisites:** P2; `sweatmeter>=0.4,<0.5` (P3 complete; shipped as `0.4.0`).

**Work**
* Machine profiling at startup: profile → fingerprint → upsert → `last_seen_at`.
* Telemetry service: a single sampler owned by the application, `latest()` cache, SSE endpoint
  `/api/v1/system/telemetry/stream`.
* Telemetry bar in the shell template with the JS module; `—` for unsupported values with reasons.
* `/api/v1/system/status`; health components `gpu_telemetry` and `machine`.
* Machines page and `freeweight doctor` reporting platform capability.

**Files/subsystems**
```text
src/freeweight/services/telemetry.py  src/freeweight/services/machine.py
src/freeweight/web/routes/system.py   src/freeweight/web/static/js/telemetry.js
src/freeweight/web/templates/partials/telemetry_bar.html
tests/unit/{test_machine_profile,test_telemetry_service}.py  tests/integration/test_telemetry_sse.py
```

**Tests**
* Machine profile stored once per fingerprint; `last_seen_at` updated on restart.
* Telemetry SSE: events flow, heartbeat present, disconnect/reconnect works, 50 concurrent
  subscribers within budget.
* No GPU / no `nvidia-smi`: the bar renders `—` with reasons; health degraded; nothing crashes.
* Sampler stops cleanly on shutdown; no thread leak across repeated app startups.
* Telemetry bar updates cause no layout shift (measured widths asserted).

**Acceptance criteria**
1. The bar shows live CPU, RAM, GPU, VRAM, temperature and power, updating each second without
   layout movement.
2. On a machine with no GPU every GPU field is `—` with a reason, and the application is fully usable.
3. `freeweight doctor` reports the platform's telemetry capability accurately.

**Known risks:** sampler lifecycle bugs under reload. **Likely failure modes:** persisting telemetry
outside runs (forbidden); a stale sample presented as current.
**Gold standards:** never fabricate; never crash; bounded overhead.
**Deferred:** persisting telemetry during runs (Phase 6).

---

## Phase 5 — Run engine on the fake provider

**Goal:** a complete run executes end to end against `FakeProvider` — queued → completed — streaming
progress to the browser, cancellable at any phase, resumable after a kill, with raw samples stored.
No real model involved.

**Prerequisites:** P3, P4; `modelrack` FakeProvider.

**Work**
* Tables and migrations for `benchmark_suites`, `benchmark_tests`, `runs`, `run_tests`, `samples`,
  `metric_values`, `run_events`, `artifacts`.
* Domain: `Benchmark`, `BenchmarkTest`, `Scorer` protocols; the run state machine as an explicit,
  tested transition table.
* Run scheduler thread: claim → prepare → warm → execute → aggregate → complete; one GPU workload at
  a time; queueing beyond that; cancellation checks at every boundary; startup recovery marking
  orphans `interrupted` and enabling resume.
* Event store with gap-free per-run sequences, persistence before publication, SSE endpoint with
  `Last-Event-ID` replay.
* `native.echo` benchmark: a trivial, deterministic suite whose only purpose is to exercise the whole
  machine (and which stays in the product as a self-test).
* Run pages: create, live view, detail with tests and samples. CLI: `run start|list|show|cancel|wait`.

**Files/subsystems**
```text
src/freeweight/domain/{benchmark,run_state,scoring}.py
src/freeweight/services/{runs,events,scheduler}.py
src/freeweight/infrastructure/db/{models_runs.py,repositories/runs.py}
src/freeweight/benchmarks/echo/{manifest.json,benchmark.py}
src/freeweight/web/routes/runs.py  src/freeweight/web/templates/runs/*.html
src/freeweight/cli/commands/runs.py
tests/unit/{test_run_state_machine,test_scheduler,test_event_sequence}.py
tests/integration/{test_run_execution,test_sse_replay,test_recovery}.py
tests/e2e/test_run_journey.py
```

**Tests**
* Every legal transition; every illegal transition rejected; terminal states immutable.
* A deliberately failing sample does not fail the test; a failing test does not fail the run.
* Cancellation in `queued`, `preparing`, `warming` and `running`, each leaving consistent data.
* Kill the process mid-run: on restart the run is `interrupted`, completed tests are retained, and
  resume continues from the right place.
* Event sequence gap-free from 1; replay after disconnect has no gap and no duplicate; sequence
  continues correctly after a restart.
* Second run while one is active is queued, not run concurrently.
* Failed sample stored with `score = NULL` and an error, excluded from aggregates, visible in counts.

**Acceptance criteria**
1. `freeweight run start --suite native.echo` completes, streams progress to the browser and the CLI,
   and stores raw samples.
2. Refreshing the browser mid-run resumes the live view with no missing events.
3. `Ctrl-C` during `run wait` cancels cleanly with exit 6 and consistent data.
4. Killing the server mid-run and restarting yields a resumable `interrupted` run.

**Known risks:** the replay/live handoff and the scheduler are the two subtlest components in the
application. **Likely failure modes:** duplicated events; a run stuck in `cancelling`; partial
aggregates written before samples are durable.
**Gold standards:** raw samples first, aggregates second; safe cancellation everywhere; restart
survival.
**Deferred:** real models, real benchmarks, telemetry persistence.

---

## Phase 6 — First real measurements: performance, token economy, provenance

**Goal:** a real Ollama model is benchmarked for speed and token economy, with telemetry persisted
for the run and a complete reproducibility fingerprint — and the same run can be repeated.

**Prerequisites:** P5; `modelrack` P3 (Ollama); SetSpec P2 payload drafts.

> **Sequencing note.** The reproducibility fingerprint includes a prompt hash, and the prompt library
> was previously scheduled for P7 — so P6 as written required something P7 delivered. The prompt
> **record schema, loader, validator, renderer and hashing** therefore move into this phase, where
> the fingerprint first needs them; P7 keeps the *benchmark* prompt content and the `prompts` CLI.
> `native.performance` and `native.token_economy` use one trivial prompt record each, which is enough
> to exercise the machinery and to make the fingerprint honest from the first real run.

**Work**
* Prompt library: record schema, pack manifest, `StrictUndefined` renderer, canonical hashing,
  `prompt_subset_hash`, startup validation — written as if it were a package, because it becomes one
  at LoadCoach P4 ([ADR-0028](../../adr/0028-prompt-pack-granularity.md)).
* `native.performance` and `native.token_economy` per the [Benchmark Catalog](benchmark-catalog.md).
* Telemetry table split (host rows and per-GPU rows) and `execution.gpu_index` attribution
  ([ADR-0027](../../adr/0027-multi-gpu-semantics.md)); idle-detection outcome per
  [spec §13](spec.md).
* Served context resolved and recorded with its source.
* Warm-up/cooldown, idle detection, cold/warm labelling, repetition handling, `perf_counter_ns`
  timing kept separate from wall-clock timestamps.
* Telemetry persistence for the duration of a run, plus the sampling-overhead calibration test whose
  result is stored on the run.
* Provenance: effective-config resolution through the execution-parameter precedence chain,
  fingerprint document assembly and hashing, per-benchmark `prompt_subset_hash` (the pack hash is
  recorded as provenance but is **not** a fingerprint input).
* `run repeat` with `--check` diffing, and refusal-with-reason when the environment has moved.
* Run detail page: metrics, telemetry charts, fingerprint, sample drill-down.

**Files/subsystems**
```text
src/freeweight/benchmarks/performance/*  src/freeweight/benchmarks/token_economy/*
src/freeweight/domain/{provenance,metrics,aggregation}.py
src/freeweight/services/telemetry_recording.py
tests/unit/{test_metric_formulas,test_provenance,test_aggregation}.py
tests/integration/test_performance_benchmark.py  tests/live/test_real_run.py
```

**Tests**
* Every metric formula with known values, boundaries, zero-division guards and `UNSUPPORTED` inputs.
* Cold and warm samples never combine into one headline metric (asserted on the aggregation output).
* Fingerprint: stable for identical inputs; changes for each input class; the document is stored and
  diffable.
* Telemetry rows written only during the run and cascade-deleted with it; on a two-GPU fixture, host
  fields appear once per sample and GPU fields once per device.
* Editing a prompt the suite does not use leaves its fingerprint unchanged; editing one it does use
  changes it.
* Idle detection: below threshold proceeds; above threshold with `on_idle_timeout = "warn"` records
  `measured_while_busy` with the observed utilization; with `"refuse"` fails with the numbers.
* `run repeat` refuses when the model digest has changed, and explains why; `--force` records the
  divergence.
* Live (marked): a real short run on Ollama produces plausible throughput and TTFT.

**Acceptance criteria**
1. A real model is benchmarked and the run page shows prompt/decode throughput, TTFT, peak VRAM and
   the telemetry series.
2. Overhead outside the provider call is ≤ 10 ms per sample, measured and asserted.
3. The recorded telemetry-sampling overhead is ≤ 1 % and is stored on the run.
4. Two runs of the same subject agree within the documented tolerance.

**Known risks:** measurement contamination from other GPU work; provider timing semantics.
**Likely failure modes:** mixing cold and warm; labelling chunk latency as token latency (guarded by
`token_level_chunks`); wall-clock used for durations.
**Gold standards:** reproducible benchmarks; honest timing; provenance completeness.
**Deferred:** quality benchmarks.

---

## Phase 7 — Deterministic quality suites

**Goal:** instruction following, structured output, tool use, tool recovery and agent behaviour are
measured with entirely deterministic scoring.

**Prerequisites:** P6 (which delivers the prompt library the fingerprint depends on).

**Work**
* Benchmark prompt records and the `prompts list|show|build` CLI
  ([Prompt Standards](../../standards/prompt-management-standards.md)); the loader, renderer and
  hashing already exist from P6.
* `native.instruction_following`, `native.structured_output`, `native.tool_use`,
  `native.tool_recovery`, `native.agent`.
* Mock tool harness over fixture data — no shell, no real filesystem, no network — with the
  containment tests to prove it.
* Scorers: exact, rule, JSON Schema, tool-selection, tool-argument, agent-trajectory.
* Capability gating: a model or provider without tool calling records `skipped` with
  `unsupported_capability`, never a zero.

**Files/subsystems**
```text
src/freeweight/prompts/**  src/freeweight/services/prompts.py
src/freeweight/benchmarks/{instruction_following,structured_output,tool_use,tool_recovery,agent}/*
src/freeweight/domain/scorers/{exact,rule,schema,tools,agent}.py
src/freeweight/benchmarks/fixtures/**
tests/unit/test_scorers_*.py  tests/integration/test_quality_suites.py
tests/security/test_mock_tools_contained.py  tests/unit/test_prompt_pack.py
```

**Tests**
* Each scorer: known-pass, known-fail, boundary, malformed model response, missing data.
* Tool metrics computed correctly for every scenario class, including "no tool required" and
  "hallucinated tool".
* Mock tools cannot read outside their fixture directory or write outside the sandbox directory,
  under adversarial arguments (`../`, absolute paths, symlinks).
* Prompt pack: parses, variables declared and used, renders, manifest current, no inline prompts in
  Python source.
* Capability-gated skip records a reason and contributes no score.

**Acceptance criteria**
1. Five deterministic suites run end to end against a real model and produce interpretable metrics.
2. No LLM is used to score anything in this phase.
3. A model without tool support yields `skipped (unsupported_capability)`, not a low score.
4. Adversarial tool arguments cannot escape the fixture directory.

**Known risks:** fixture design bias. **Likely failure modes:** scoring a refusal as a failure of
capability; tool fixtures leaking real paths.
**Gold standards:** deterministic scoring; honest skips; contained tools.
**Deferred:** judged suites.

---

## Phase 8 — Judgement-dependent suites

**Goal:** auditing, critiquing, judging and long context are measured, with judge bias measured
rather than assumed.

**Prerequisites:** P7.

**Work**
* `native.audit` with the mutation corpus **and clean samples**; precision/recall/F1 plus the
  clean-code false-positive rate; localization scoring.
* `native.critique` with correction uplift and regression rate.
* `native.judge` with position, verbosity, style, repetition, transitivity and self-preference tests.
* `native.long_context` with depth × position × distractor sweeps and `effective_context_tokens`.
* Judge infrastructure: judge model selection, order randomization, blinding, repeated trials,
  agreement measurement, and linkage from any judged score to that judge's own judge-benchmark
  results.

**Files/subsystems**
```text
src/freeweight/benchmarks/{audit,critique,judge,long_context}/*
src/freeweight/domain/scorers/{audit,critique,judge}.py
src/freeweight/domain/judging.py     src/freeweight/benchmarks/corpora/**
tests/unit/{test_audit_metrics,test_critique_metrics,test_judge_bias,test_effective_context}.py
```

**Tests**
* Audit: a model that flags everything scores poorly (a synthetic "flag everything" responder is a
  test case); precision, recall, F1 and the clean-code FP rate computed correctly.
* Critique: regression rate detected when a correct answer is made incorrect.
* Judge: position bias detected on a synthetic order-biased judge; transitivity violations counted;
  self-preference measured with and without anonymization.
* Long context: effective context computed against the configured threshold; a model failing at depth
  is distinguished from one failing everywhere.
* Judged scores carry the judge's identity, prompt version and bias metrics.

**Acceptance criteria**
1. A "flags everything" model scores low on auditing despite perfect recall.
2. Judge bias metrics are produced and displayed alongside any judged score.
3. Effective context differs from advertised context on at least one real model and the difference is
   explained by the depth/position data.

**Known risks:** LLM-judge instability. Mitigated by repetition, agreement measurement, order
randomization and by preferring deterministic suites everywhere else.
**Likely failure modes:** treating judge output as ground truth; averaging biased comparisons.
**Gold standards:** deterministic first; judge trustworthiness always visible.
**Deferred:** external judge benchmarks (Phase 13).

---

## Phase 9 — Memory, energy, reliability and comparison

**Goal:** KV-cache behaviour, energy and reliability are measured, and models, quantizations and
runtime profiles can be compared correctly.

**Prerequisites:** P6, P8.

**Work**
* `native.memory_kv`: theoretical KV from descriptor fields, observed VRAM slope, overhead ratio,
  max-context fit, KV-precision comparison, cache reuse.
* `native.energy`: integration from real sample timestamps, throttle detection, everything labelled
  as an estimate.
* `native.reliability`: dispersion, agreement, pass@k, explicit outlier policy.
* Comparison engine: comparability verdicts, grouping, fingerprint diffing, quantization and runtime
  studies.
* Comparison UI and `results compare` CLI.

**Files/subsystems**
```text
src/freeweight/benchmarks/{memory_kv,energy,reliability}/*
src/freeweight/domain/{comparison,statistics}.py
src/freeweight/services/comparison.py  src/freeweight/web/routes/compare.py
tests/unit/{test_kv_math,test_energy_integration,test_statistics,test_comparison_rules}.py
```

**Tests**
* KV theory against hand-computed values; missing architecture fields ⇒ `unsupported`, not a wrong
  number; hybrid architectures flagged and excluded from the formula.
* VRAM slope fit on synthetic data; OOM path recorded as a measurement (max context), not a failure.
* Energy integrated over irregular intervals using real timestamps.
* Comparison refuses to merge across a "separate" boundary and shows the fingerprint diff.
* Statistics: mean/median/stddev/CV/percentiles with `UNSUPPORTED` inputs excluded and counted; an
  all-unsupported series is `unsupported`, not zero.

**Acceptance criteria**
1. A quantization comparison shows quality, VRAM, speed and token economy side by side, correctly
   separated.
2. Comparing runs from different benchmark versions is refused with a clear explanation.
3. Every statistic reports the sample count it used and how many were excluded.

**Known risks:** VRAM slope noise from other processes. Mitigated by idle detection and by reporting
fit quality alongside the slope.
**Likely failure modes:** averaging across incomparable subjects; treating an OOM as a failed run.
**Gold standards:** honest comparability; measured, not assumed.
**Deferred:** evidence aggregation.

---

## Phase 10 — Dashboard, results experience, data management, exports · **M2 beta**

**Goal:** the product is genuinely usable: a dashboard that answers the four core questions, full
drill-down to raw samples, safe data management and exports.

**Prerequisites:** P9.

**Work**
* Dashboard: filters, summary cards, comparison heatmap, quality-vs-speed, quality-vs-VRAM, token
  economy, context, audit precision/recall, tool behaviour, judge bias, energy.
* Results experience: run detail → test detail → case inspector (prompt, response, tool calls,
  scoring, telemetry) in ≤ 2 interactions from any headline metric.
* Database page: stats, preview-then-confirm deletion, backup, vacuum, retention.
* Exports: JSON, JSONL, CSV; scopes run/model/suite/comparison/all; streaming; SetSpec-wrapped.
* Settings page for runtime-changeable settings only, with security-relevant keys clearly marked as
  config-only.
* Accessibility and UI acceptance checklist pass.

**Files/subsystems**
```text
src/freeweight/web/routes/{dashboard,results,database,settings}.py
src/freeweight/web/templates/{dashboard,results,database,settings}/**
src/freeweight/web/static/js/{dashboard,charts,table}.js
src/freeweight/services/{export,database_admin}.py  src/freeweight/cli/commands/results.py
tests/e2e/{test_dashboard,test_drilldown,test_delete_preview,test_export}.py
tests/accessibility/test_ui_checklist.py  tests/performance/test_dashboard_queries.py
```

**Tests**
* Every dashboard figure matches a value recomputed directly from raw samples (the anti-lie test).
* Deletion preview counts exactly match what deletion removes; models and machines survive.
* Export round-trip: exported JSON re-imported into a viewer reproduces the same metrics.
* Dashboard aggregate over 100 k samples within budget, with the expected query plans.
* UI checklist: themes, `—` rendering, keyboard, contrast, 1280×720, empty/loading/error states.

**Acceptance criteria**
1. Every acceptance item in [UI/UX Standards §13](../../standards/ui-ux-standards.md) passes.
2. No headline number is more than two interactions from its raw source.
3. Deleting results is previewed, confirmed, transactional and backed up.
4. Exports stream a 10 000-sample run within budget.

**Known risks:** dashboard aggregates diverging from raw data. Mitigated by the anti-lie test.
**Likely failure modes:** slow queries at volume; charts that mislead through truncated axes.
**Gold standards:** drillable metrics; safe destructive operations; accessible dense UI.
**Deferred:** evidence export.

---

## Phase 11 — Capability evidence and the LoadCoach contract · **M3 (1.0-rc)**

**Goal:** FreeWeight produces versioned capability evidence with confidence and freshness that another
application can consume with no FreeWeight code and no database access.

**Prerequisites:** P10; SetSpec P4 (frozen schemas).

**Work**
* Capability mapping configuration (benchmark metrics → capabilities, with weights), versioned.
* Aggregation implementing [ADR-0017](../../adr/0017-benchmark-confidence-and-freshness.md):
  sample, consistency, freshness, environment and identity factors; hard separations; policy version
  recorded. Freshness decays from `measured_at` — the latest `completed_at` among the contributing
  runs — never from `computed_at`
  ([ADR-0022 §2](../../adr/0022-capability-evidence-record-contract.md)).
* The full `capability.evidence` field set from
  [ADR-0022 §1](../../adr/0022-capability-evidence-record-contract.md), including
  `vocabulary_version`, `dataset_hashes` and per-benchmark `prompt_subset_hashes`.
* `GET /evidence/export?since=` filtering on `computed_at`, and `complete: true|false` on every
  bundle.
* `capability_evidence` table, recomputation service, staleness detection and badging.
* `GET /api/v1/evidence`, `GET /api/v1/evidence/export`, `freeweight evidence show|export`.
* Contract tests against SetSpec goldens; publish the OpenAPI snapshot.

**Files/subsystems**
```text
src/freeweight/domain/{capability_mapping,confidence}.py
src/freeweight/services/evidence.py  src/freeweight/web/routes/evidence.py
src/freeweight/cli/commands/evidence.py  src/freeweight/config/capability_weights.toml
tests/unit/{test_confidence,test_capability_mapping}.py
tests/contract/{test_evidence_schema,test_evidence_export}.py
```

**Tests**
* Confidence factors individually and combined, against hand-computed values.
* Hard separations enforced: differing benchmark version, dataset hash, prompt version, digest or
  runtime profile never merge.
* A capability with no evidence is **absent**, never scored zero.
* **Re-aggregating unchanged runs does not raise confidence** — the freshness test that makes
  ADR-0017 mean what it says.
* An incremental bundle (`since=`) declares `complete: false`; a full bundle declares `complete: true`.
* A `capability.evidence` payload missing `measured_at`, `policy_version` or `vocabulary_version` is
  rejected by the schema with the field named.
* Export validates against the SetSpec schema and matches the goldens.
* A consumer harness — importing only `setspec` — reads the exported bundle and reconstructs the
  evidence.
* Staleness badging appears when freshness drops below the threshold or drift is detected.

**Acceptance criteria**
1. Every §20 criterion in the [spec](spec.md) is met.
2. A bundle exported by FreeWeight is consumed by a `setspec`-only harness with no FreeWeight import
   and no database access.
3. Evidence records name their contributing benchmarks, weights and sample counts, and the UI can
   explain any score.
4. FreeWeight 1.0-rc is tagged; **LoadCoach development may begin.**

**Known risks:** confidence parameters are judgement calls. Mitigated by making them configuration,
recording the policy version, and revisiting with real data.
**Likely failure modes:** absence of evidence rendered as zero capability; merging across versions.
**Gold standards:** versioned contracts; no cross-application coupling; explainable scores.
**Deferred:** WeightsDB/MirrorWall adoption; external adapters.

---

## Phase 12 — Adopt WeightsDB and MirrorWall

**Goal:** FreeWeight runs on the extracted shared packages with no behaviour change and a smaller
codebase.

**Prerequisites:** `weightsdb>=0.2,<0.3`, `mirrorwall>=0.2,<0.3` and `setspec>=0.4,<0.5` (carrying
`setspec.prompts`) published (LoadCoach P1 and P4);
their adoption checklists.

**Work**
* Replace `freeweight.infrastructure.db` plumbing with WeightsDB (engine, session, transaction, types,
  migration runner, backup, health), keeping FreeWeight's models and migration history untouched.
* Replace local templates, tokens, components and JS with MirrorWall's, keeping FreeWeight's pages and
  navigation.
* Replace `freeweight.services.prompts` with `setspec.prompts`, keeping FreeWeight's own pack and its
  `prompts` CLI commands ([ADR-0028](../../adr/0028-prompt-pack-granularity.md)). Prompt hashes must be
  byte-identical before and after — a golden test over the existing pack is the acceptance criterion.
* Delete the superseded code; keep every existing test passing **unchanged** — that is the proof the
  adoption changed nothing observable.

**Files/subsystems**
```text
(deletions) src/freeweight/infrastructure/db/{engine,session,types}.py
(deletions) src/freeweight/web/static/{css/tokens.css,js/{drawer,dialog,table,theme,sse}.js}
(deletions) src/freeweight/web/templates/{base.html,partials/*}
(edits)     bootstrap.py, web/app.py, templates/** to use MirrorWall macros
```

**Tests**
* The entire pre-existing test suite passes without modification (aside from import paths).
* Migration history is preserved; an existing database upgrades without a new revision.
* Visual snapshots before and after are equivalent apart from intended token differences.

**Acceptance criteria**
1. No behaviour change; no data migration; no test rewritten to accommodate the swap.
2. FreeWeight's line count drops by the extracted amount and the deleted modules are gone.
3. Both applications now run on the same shared packages.

**Known risks:** subtle behavioural differences in session or pragma handling. Mitigated by the
unchanged test suite being the acceptance criterion.
**Likely failure modes:** template regressions; a migration history broken by a changed version table
name.
**Gold standards:** shared infrastructure with zero application coupling.
**Deferred:** nothing.

---

## Phase 13 — External benchmark adapters

**Goal:** established external benchmarks run in isolated environments and their results appear
alongside native results, with sandboxing enforced.

**Prerequisites:** P11.

**Work**
* Adapter framework: environment creation and verification, pinned versions, dataset download and
  hash verification, subprocess invocation with argument lists and timeouts, output normalization,
  error translation.
* Sandbox tiering (container → bwrap → refuse) with the tier recorded on results.
* First adapters: lm-evaluation-harness (MMLU-Pro, GSM8K, ARC, HellaSwag), IFEval, EvalPlus,
  CRUXEval, BFCL, RULER, JudgeBench, LLMBar, CriticBench.
* `freeweight external list|install|verify`; a benchmark-source page crediting each project and its
  licence.

**Files/subsystems**
```text
src/freeweight/external/{framework,environment,sandbox,adapters/*}.py
src/freeweight/cli/commands/external.py  src/freeweight/web/routes/sources.py
tests/unit/test_external_output_parsing.py  tests/integration/test_sandbox_tiers.py
tests/security/test_sandbox_refusal.py
```

**Tests**
* Output parsing from recorded external results, including malformed and partial output.
* Dataset hash mismatch refuses to run and names both hashes.
* Sandbox: container path (skipped when unavailable), bwrap path, and **refusal** when neither exists
  — asserting that nothing executes on the host.
* Subprocess invoked with an argument list and a timeout; a hanging process is killed and reported.
* External results carry their manifest, versions and sandbox tier.

**Acceptance criteria**
1. At least three external adapters produce results that appear alongside native ones with full
   provenance.
2. On a machine with no container runtime, code-execution benchmarks are skipped with
   `sandbox_unavailable` and never run on the host.
3. External environments never contaminate FreeWeight's own environment (verified by importing
   nothing from them).

**Known risks:** external projects changing their CLIs and output formats. Mitigated by pinning,
recorded fixtures and clear failure reporting.
**Likely failure modes:** silently degrading to host execution; unpinned datasets invalidating
comparisons.
**Gold standards:** isolation; pinned versions; refusal over risk.
**Deferred:** SWE-bench, TUA-Bench, real-bug corpora, extreme-context suites.

---

## Phase 14 — Hardening and 1.0 · **M6**

**Goal:** FreeWeight 1.0 — installable, documented, safe, fast enough, and complete against its
acceptance criteria.

**Prerequisites:** P12, P13.

**Work**
* Performance pass against every budget in the [spec](spec.md) §15; fix regressions.
* Security pass: full checklist in [Security Standards §14](../../standards/security-standards.md),
  including `Host` validation, the CSRF token on form routes, and the refusal to start when a
  non-loopback bind lacks `server.allowed_hosts`.
* Documentation: README, quickstart, configuration reference (generated), API docs, benchmark guide,
  troubleshooting, backup/restore, upgrade notes, contributor guide.
* Upgrade testing from every earlier released version; rollback notes.
* Accessibility audit; UI acceptance checklist re-run.
* Publish `freeweight 1.0.0`.

**Files/subsystems**
```text
docs/{quickstart,configuration,benchmarks,troubleshooting,backup-restore,upgrading,security}.md
tests/performance/**  tests/security/**  tests/e2e/test_full_journeys.py
```

**Tests**
* Every performance budget asserted on the reference machine.
* Every security test in the standards checklist.
* Clean-machine install: `pip install freeweight` → `freeweight serve` → run a benchmark → export.
* Upgrade from each released version with real data preserved.

**Acceptance criteria**
1. All 12 acceptance criteria in the [spec](spec.md) §20 pass.
2. All FreeWeight gold standards in [Gold Standards §2](../../standards/gold-standards.md) are met.
3. Documentation complete; `freeweight doctor` diagnoses every failure mode in the troubleshooting
   guide.
4. `freeweight 1.0.0` published and installable from PyPI.

**Known risks:** scope creep at the finish line. Mitigated by the deferred list being explicit and
final for 1.0.
**Likely failure modes:** documentation drift (mitigated by generated references and CI diffs).
**Gold standards:** every FreeWeight gold standard, measured.
**Deferred to post-1.0:** additional external adapters, llama.cpp/vLLM integrations, scheduled
campaigns with regression alerting, multi-machine federation, shareable report bundles, annotation
and tagging, A/B prompt studies.
