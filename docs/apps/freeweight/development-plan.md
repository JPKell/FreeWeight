# FreeWeight — Development Plan

**Sequence position:** first application. Begins after BaseAiCore P4, SetSpec P1–P3, ModelRack P2
(FakeProvider) and SweatMeter P1–P2 are available.
**Milestones:** M2 (beta) at P10–P10A · **M3 (FreeWeight 1.0-rc, contract freeze) at P11** · **M6 (FreeWeight 1.0) at P14**.

Every phase ends in something a person can run and look at. No phase is "implement the backend".

**Amended 2026-08-26** by [ADR-0031](../../adr/0031-user-defined-goal-benchmarks.md) and
[ADR-0032](../../adr/0032-judge-validity-and-user-capability-namespace.md), which add user-defined
goal benchmarks: **Phases 8A, 8B and 10A** below, an amendment to Phase 11, and one upstream
prerequisite (§0).

> **Scheduling constraint, and it is hard.** ADR-0032 adds fields to `capability.evidence`, a sixth
> factor to the ADR-0017 confidence formula, and one root to the SetSpec capability vocabulary. **M3
> at Phase 11 is the contract freeze.** Every one of those changes must therefore land *at or before*
> P11. Deferring goal work to "after 1.0" is not a scheduling choice, it is a decision to make a
> breaking change to a frozen cross-application contract later. Model assignment for every step is in
> §Appendix A.

---

## Phase 0 (upstream) — SetSpec capability vocabulary 1.1

Not a FreeWeight phase; a prerequisite in another repository, listed here because P11 cannot pass
without it and it is trivially easy to forget.

**Work (in `py/SetSpec`)**
* Add the reserved root `user` to `setspec.vocabulary.CAPABILITIES`; bump
  `CAPABILITY_VOCABULARY_VERSION` to `"1.1"` (an addition, therefore minor — spec §11.8 rule 8).
* Extend `capability.evidence` with the goal-sourced field group from
  [ADR-0032 §5](../../adr/0032-judge-validity-and-user-capability-namespace.md), all optional, absent
  on non-goal records.
* Add `benchmark.goal_pack` and `benchmark.calibration_report` schemas with goldens.
* `CHANGELOG.md` under `## [Unreleased]`.

**Tests**
* `user.anything_at_all` validates; `user` alone as a bare capability is refused; a *newer-minor*
  payload carrying `user.*` is accepted by a build that predates 1.1 (ADR-0009 forward compatibility)
  — this is the test that proves an older LoadCoach degrades by ignoring goal evidence rather than
  failing on it.
* Golden payloads for both new schemas; round-trip equality; unknown-field preservation.

**Acceptance:** a `setspec`-only script validates a `user.house_voice` evidence payload and rejects
`user` bare. **Must be released before FreeWeight P11 begins.**

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
  `unsupported_capability`, never a zero. A test's whole `requires.provider_capabilities` block is
  enforced, not only tool calling, and a name that is not a `ProviderCapabilities` field fails
  registry construction rather than skipping forever
  ([ADR-0033 §9](../../adr/0033-benchmark-interaction-protocol.md)).
* The **interaction seam** three of these suites need: a benchmark test may declare a bounded
  multi-turn interaction — a tool loop, or a call plus one corrective retry — and the run engine
  executes it, owning the provider, the frozen execution parameters, the step budget and the cost
  accounting ([ADR-0033](../../adr/0033-benchmark-interaction-protocol.md)). Trajectories are stored
  as `tool_calls` rows and scored by a trajectory scorer, never on their final sentence.

**Files/subsystems**
```text
src/freeweight/prompts/**  src/freeweight/services/prompts.py
src/freeweight/benchmarks/{instruction_following,structured_output,tool_use,tool_recovery,agent}/*
src/freeweight/benchmarks/{interaction,loading}.py  src/freeweight/benchmarks/fixtures/**
src/freeweight/domain/scorers/{exact,rule,schema,tools,agent}.py
src/freeweight/cli/commands/prompts.py  src/freeweight/services/runs.py
src/freeweight/domain/aggregation.py
src/freeweight/infrastructure/db/migrations/versions/0004_tool_calls.py
tests/unit/test_scorers_*.py  tests/integration/test_quality_suites.py
tests/security/test_mock_tools_contained.py  tests/unit/test_prompt_pack.py
tests/live/test_real_run.py
```

**Tests**
* Each scorer: known-pass, known-fail, boundary, malformed model response, missing data.
* Tool metrics computed correctly for every scenario class, including "no tool required" and
  "hallucinated tool".
* Mock tools cannot read outside their fixture directory or write outside the sandbox directory,
  under adversarial arguments (`../`, absolute paths, symlinks).
* Prompt pack: parses, variables declared and used, renders, manifest current, no inline prompts in
  Python source.
* Capability-gated skip records a reason and contributes no score, and a manifest naming a
  capability that does not exist fails registry construction.
* A tool trajectory is stored as `tool_calls` rows and cascade-deletes with its sample; a
  hallucinated tool is a row, not a missing one.
* Live (marked): the five suites run end to end on a real Ollama model and produce only
  manifest-declared metrics, or skip with `unsupported_capability` where the model lacks the
  capability.

**Acceptance criteria**
1. Five deterministic suites run end to end against a real model and produce interpretable metrics.
   Demonstrated by the marked live test above; the same five run against `FakeProvider` in CI.
2. No LLM is used to score anything in this phase.
3. A model without tool support yields `skipped (unsupported_capability)`, not a low score.
4. Adversarial tool arguments cannot escape the fixture directory.

**Known risks:** fixture design bias. **Likely failure modes:** scoring a refusal as a failure of
capability; tool fixtures leaking real paths.
**Gold standards:** deterministic scoring; honest skips; contained tools.
**Deferred:** judged suites. Prompt overrides
([Prompt Standards §6](../../standards/prompt-management-standards.md)) stay unwired here and are
delivered at P8A, which renders user-authored content through the same loader.

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
**Deferred:** external judge benchmarks (Phase 13); *using* a judge to score against a user's rubric
(Phases 8A–8B) — this phase builds the judge infrastructure and measures judges; it does not yet
point one at a user-authored goal.

---

## Phase 8A — Goal packs and deterministic goal criteria

**Goal:** a user can hand-write a goal pack whose criteria are entirely rules, run it against a
model, and get a scored, drillable result — **with no judge involved anywhere**. This phase proves
the goal machinery on the deterministic half of the ladder before any model judgement enters it.

**Prerequisites:** P7 (prompt library, scorer patterns, run engine). Not P8 — nothing here judges.

**Work**
* Goal pack schema, loader and validator per [Subjective Goals §2](subjective-goals.md); packs load
  once at startup exactly as prompt packs do, and a malformed pack is a startup failure, not a
  mid-run surprise.
* `goals`, `goal_criteria`, `goal_tasks`, `criterion_scores` tables and migration;
  `benchmark_suites.runner` gains `goal`, plus `goal_id` and `goal_hash`.
* `goal_hash`: canonical JSON over the measurement-defining subset only
  ([Subjective Goals §2.2](subjective-goals.md)). Renaming a criterion must not move it; changing what
  it checks must.
* The rule-criterion library — all thirteen types in
  [Subjective Goals §3.1](subjective-goals.md) — each a pure function with a docstring stating what it
  refuses, under `rule_timeout_ms`, with a linted regex dialect (no backreferences, bounded
  repetition).
* Rung-3 reference criteria: `entity_recall`, `claim_coverage`, `no_unsupported_claims`,
  `reference_similarity`.
* Goal runner: composite scoring, weights, hard gates, `score_method_mix`, skipped-criterion handling
  (`raw_score = NULL`, never `0`).
* `freeweight goals list|show|validate|suggest-rules|export|import`, and `goals init` as a terminal
  interview writing a pack.
* The rubric lint: flags a `judge` criterion a rule could check and names the rule; refuses a `judge`
  criterion with no scale descriptors.
* **Prompt overrides, wired** ([Prompt Standards §6](../../standards/prompt-management-standards.md)),
  which arrive here because this is the phase where user-authored content first reaches the same
  loader: `$XDG_CONFIG_HOME/freeweight/prompts/` is loaded at startup and marked
  `prompt_source = "user_override"` on every record that used it; a benchmark run with an overridden
  prompt is **refused** unless `--allow-prompt-override` is passed, and the override becomes a
  fingerprint input when it is. P6 built the loader's `override_root`; P7 deliberately left it
  unwired rather than have the `prompts` CLI describe a pack no benchmark would render.
* `GET|POST|PUT|DELETE /api/v1/goals` and `/goals/{slug}/validate|suggest-rules|export`,
  `POST /goals/import`.

**Files/subsystems**
```text
src/freeweight/domain/goals/{pack,criteria,hashing,lint,composite}.py
src/freeweight/domain/scorers/rules/{phrases,length,readability,pov_tense,vocabulary,
                                     punctuation,structure,regex,repetition,reference}.py
src/freeweight/benchmarks/goal/runner.py
src/freeweight/services/goals.py     src/freeweight/repositories/goals.py
src/freeweight/web/routes/goals.py   src/freeweight/cli/commands/goals.py
migrations/versions/xxxx_goal_tables.py
tests/unit/test_rules_*.py  tests/unit/{test_goal_hash,test_goal_lint,test_composite}.py
tests/integration/test_goal_run_rules_only.py  tests/security/test_goal_pack_import.py
```

**Tests**
* Every rule type: known-pass, known-fail, boundary, empty input, unicode, and a text that trips it
  in an obvious way. Each is a metric formula and gets the full [Testing Standards §5](../../standards/testing-standards.md) treatment.
* `goal_hash` **changes** when a criterion's rule parameters, weight, rung or scale descriptors
  change; **does not change** when a display name, `intent` or `contributes_to` changes. Both
  directions asserted — a hash that never changes and a hash that always changes are equally useless.
* Hard gate zeroes the composite and records which gate fired; a skipped criterion is excluded and
  the applied weight reflects it.
* Catastrophic-backtracking regex fails the criterion within `rule_timeout_ms` and does not stall the
  run; the goal completes with that criterion in `error`.
* Import: oversize pack, path traversal in the pack, colliding slug, bad hash, malformed JSON — each
  refused before any file is written, each with its own error code.
* Goal templates cannot reach the filesystem or network through the Jinja2 environment.
* Lint fires on a judged criterion that a `forbidden_phrases` rule would cover, and on a judged
  criterion with no descriptors.

**Acceptance criteria**
1. A hand-written goal pack with only rule criteria runs end to end against a real model and produces
   a composite score, per-criterion scores and `score_method_mix = {rule: 1.0}`.
2. That run works with **no judge configured at all**, and its `judge_validity_factor` is 1.0.
3. Every headline goal number drills to the criterion and the sample that produced it in ≤ 2
   interactions, showing which phrases matched and which distributions were measured.
4. Renaming a criterion leaves `goal_hash` unchanged; changing its phrase list changes it, and the
   UI states how many existing runs the change would separate **before** it is applied.
5. `freeweight goals validate` on a deliberately bad pack names every problem with a severity.

**Known risks:** the rule library is thirteen small formulas and it is tempting to write them quickly;
each is a measurement and a wrong one is invisible. **Likely failure modes:** a rule that silently
scores 0 for input it cannot parse instead of returning `unsupported`; `goal_hash` over-covering
(display names) and fragmenting history.
**Gold standards:** deterministic scoring; unsupported is not zero; every formula unit-tested.
**Deferred:** all judging, all calibration, the wizard.

---

## Phase 8B — Calibration, the jury, and the gate

**Goal:** a judged criterion becomes a *measurement*: the user grades, the jury is scored against
grades it never saw, the agreement is reported with every number, and a rubric that cannot be
measured is refused entry to the evidence contract while still producing a fully inspectable run.

**Prerequisites:** P8 (judge infrastructure, blinding, order randomization, bias metrics), P8A.

**Work**
* Jury service on P8's judge infrastructure: assembly from installed models, blinding of candidate
  identity, case and criterion order randomization, repetitions, **self-judging refusal**, and
  `jury_reduced` degradation with a recorded reason.
* Judged-criterion scorer: ordinal absolute mode and pairwise mode; juror combination by **median**;
  Krippendorff's alpha for inter-juror agreement.
* Calibration tables and service: sample collection (generate over a model spread, paste, promote
  prior run samples), the seeded stratified anchor/holdout partition, grade capture with resumable
  progress.
* Anchor injection: the judge prompt record renders the anchors as few-shot exemplars with the user's
  grades and notes. It is a prompt record ([ADR-0012](../../adr/0012-prompt-storage-format.md)) —
  no exception, no f-strings.
* Agreement mathematics: quadratic-weighted Cohen's kappa, Spearman rho, MAE, signed bias, all with
  `n_holdout` inseparable from the coefficient in every representation.
* The gate: weighted `kappa_w` against `calibration.min_agreement`; `judge_validity_factor` with the
  `sqrt(n_holdout / n_holdout_target)` shrinkage
  ([ADR-0032 §2](../../adr/0032-judge-validity-and-user-capability-namespace.md)).
* Disagreement diagnostics: the worst-diverging holdout samples, both rationales, sorted by
  contribution to the disagreement, with the lint's read on the likely cause.
* The grade-distribution check that refuses to compute agreement on a set with no variance.
* Remote-juror opt-in requiring both `providers.allow_remote` and the goal's `judge.allow_remote`,
  recorded in the fingerprint, separating results.
* `freeweight goals calibrate|grade|calibration show|report`, `freeweight judges list|validate`, and
  the calibration API endpoints.

**Files/subsystems**
```text
src/freeweight/domain/{jury,agreement,calibration}.py
src/freeweight/domain/scorers/judged.py
src/freeweight/services/{calibration,jury}.py
src/freeweight/repositories/calibration.py
src/freeweight/prompts/goals/judge.rubric.v1.json
src/freeweight/web/routes/calibration.py  src/freeweight/cli/commands/{goals,judges}.py
migrations/versions/xxxx_calibration_tables.py
tests/unit/{test_agreement,test_kappa_weighted,test_krippendorff,test_partition,
            test_validity_factor,test_gate}.py
tests/integration/{test_calibration_flow,test_jury_assembly}.py
```

**Tests**
* `kappa_w` against hand-computed confusion matrices and published worked examples; a **perfectly
  agreeing** synthetic grader yields 1.0; a **uniformly random** grader yields ≈ 0; a grader that is
  consistently one point generous yields high `rho`, high `kappa_w` and non-zero `bias` — the three
  statistics must be able to disagree with each other, or only one of them is real.
* Krippendorff's alpha against a published worked example; total juror agreement ⇒ 1.0.
* Partition: deterministic under a fixed seed, stratified across the grade range, and **the holdout
  is provably never rendered into a judge prompt** — asserted by scanning the rendered prompt for
  holdout content hashes, not by reading the code.
* `judge_validity_factor`: 1.0 when every criterion is rungs 1–4; shrunk at small `n_holdout`
  (6 samples at `kappa_w` 0.71 ⇒ 0.55, hand-computed); clamped at both ends.
* The gate: below threshold ⇒ run completes, result badged `uncalibrated`, **`capability_evidence`
  has no row** — the absence is asserted directly, because "we emitted it quietly at the floor" is
  precisely the failure the gate exists to prevent.
* `CALIBRATION_INSUFFICIENT` (too few grades) is distinguished from a failed gate (enough grades,
  poor agreement) in code, in the API and in the UI copy.
* Self-judging is refused and recorded, not silently discounted; a jury reduced below `jury_size`
  records `jury_reduced` and still scores.
* Zero eligible jurors ⇒ judged criteria `skipped (judge_unavailable)`, rule criteria still score,
  the partial result says so.
* Grade-distribution check fires on an all-4-and-5 calibration set.
* Everything above runs against `FakeProvider` with **no GPU, no Ollama, no network** — including a
  deterministic fake jury whose bias is configurable, which is how a "generous juror" and a
  "position-biased juror" become test cases rather than field reports.

**Acceptance criteria**
1. A goal with judged criteria, calibrated on 12 graded samples, reports `kappa_w`, `rho`, `mae`,
   `bias` and `n_holdout` per criterion, and a weighted gate verdict.
2. A deliberately unmeasurable rubric ("make it good") completes its run, is badged `uncalibrated`,
   emits **no** evidence, and names the criteria and the specific samples where the jury diverged
   from the user.
3. Moving one criterion from `judge` to `rule` measurably raises `judge_validity_factor`, and both
   values are shown.
4. A juror is never asked to judge its own output, and the refusal appears in the run record.
5. Two calibration runs of the same goal, jury and grades produce identical partitions and identical
   agreement figures.

**Known risks:** the agreement statistics are the intellectual core of the feature and a subtly wrong
`kappa_w` would be invisible for months — it would simply produce plausible numbers. Mitigated by
hand-computed fixtures, published worked examples, and the synthetic graders whose true agreement is
known by construction.
**Likely failure modes:** holdout leaking into the judge prompt; averaging jurors instead of taking
the median and losing the dispersion that *is* the error bar; conflating "not enough grades" with
"poor agreement"; presenting `kappa_w` without `n_holdout`.
**Gold standards:** deterministic first; judge trustworthiness always visible; unsupported is not
zero; the default suite needs no GPU, no Ollama, no network.
**Deferred:** the wizard, starter packs, evidence export.

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

## Phase 10A — The goal authoring wizard and starter packs · **completes M2 beta**

**Goal:** a user who has never read the documentation can define a subjective goal, be taught what
makes it measurable, grade a calibration set, and see how well the instrument agrees with them —
entirely from the UI, and walk away owning an editable file.

**Prerequisites:** P8B, P10 (dashboard, drill-down and the results experience the wizard reuses).

**Work**
* The seven-step wizard of [Subjective Goals §7](subjective-goals.md): intent → criteria → proposed
  rules → tasks → generate and grade → agreement → save. Server-rendered with progressive
  enhancement ([ADR-0020](../../adr/0020-ui-rendering-strategy.md)); no SPA.
* **Step 2 is the part that earns the feature**: for each criterion the wizard asks whether two people
  reading the description would grade the same text the same way, and whether it is one quality or
  two fused together. Splitting "not LinkedIn" into a vocabulary criterion and a register criterion is
  what makes both measurable, and the wizard has to make that move visible rather than performing it.
* Rule proposal UI: each proposed rule shown with pre-filled parameters, accept/edit/skip, and the
  running statement of how much weight has moved off the judge and onto rules.
* Blinded inline grading: shuffled, model identity hidden, notes per grade, continuous save,
  resumable across sessions and browser refreshes. Grading twelve samples across five criteria is a
  real sitting.
* The calibration report screen: the band language from
  [Subjective Goals §5.5](subjective-goals.md), never a bare coefficient; the disagreement samples
  side by side with the user's own note and the jury's rationale.
* The four starter packs, complete with tasks, criteria, proposed rules and worked graded calibration
  sets; `unforked` badging wherever a starter's results appear.
* Goal results in the dashboard, comparison and export paths: `score_method_mix` beside every score,
  `goal_hash` separation in comparison, `benchmark.goal_pack` and `benchmark.calibration_report`
  exports.
* Wizard copy that states the grading cost **before** the user invests in it, and that frames a failed
  gate as a useful answer rather than a rejection.

**Files/subsystems**
```text
src/freeweight/web/routes/wizard.py
src/freeweight/web/templates/goals/{wizard_*.html,grade.html,report.html,starters.html}
src/freeweight/web/templates/results/goal_detail.html
src/freeweight/goals/starters/{creative_voice,brand_voice,summary_faithfulness,
                               technical_explanation}/**
src/freeweight/services/wizard.py
tests/e2e/{test_goal_wizard_journey,test_grading_resumable}.py
tests/integration/test_starter_packs.py
```

**Tests**
* Full E2E journey through HTTP **and** CLI: define → propose rules → tasks → grade → calibrate →
  run → compare → export, with no file editing at any point.
* Grading survives a browser refresh, a server restart mid-session, and an out-of-order submission;
  no grade is lost and none is double-counted.
* Every starter pack parses, validates, lints clean, and its worked calibration set reproduces its
  documented agreement figures under a fixed seed and the fake jury.
* A starter forked but unedited is badged `unforked` in the UI, in results and in exports.
* The wizard never writes a goal the CLI cannot then load, and a goal written by hand is editable in
  the wizard without loss — asserted by a byte-level round-trip through both surfaces.
* Accessibility: the grading UI is fully keyboard-operable and screen-reader labelled; it is the one
  screen a user spends twenty unbroken minutes in.
* The rule proposer never applies a rule automatically, asserted on the persisted pack.

**Acceptance criteria**
1. Spec §20 criterion 13 is met in full: a user with no prior setup completes the whole flow from the
   UI, reads no documentation, edits no file — and the artifact they end up owning is a JSON pack
   they can open in an editor and diff in git.
2. Spec §20 criteria 14–16 are demonstrable on a live machine, not merely covered by tests.
3. Each of the four starter packs runs end to end on a fresh install and shows its deterministic
   weight fraction; read in the documented order they demonstrate rising deterministic weight.
4. The calibration report is legible to someone who does not know what a kappa is — the band and the
   consequence are stated in words, with the coefficient and `n_holdout` beside them.

**Known risks:** this is where the feature succeeds or fails as a *product*. The mathematics can be
perfect and the feature still dead if the grading step feels like unpaid work with an unclear payoff.
**Likely failure modes:** a wizard that produces a pack the user cannot understand when they open it;
grading progress lost to a refresh; the report screen showing a coefficient and no meaning; starter
packs quietly becoming defaults because forking is more effort than not forking.
**Gold standards:** zero-configuration start; every headline metric drills to its samples; honest
skips; server-rendered with progressive enhancement.
**Deferred:** goal evidence export (Phase 11).

---

## Phase 11 — Capability evidence and the LoadCoach contract · **M3 (1.0-rc)**

**Goal:** FreeWeight produces versioned capability evidence with confidence and freshness that another
application can consume with no FreeWeight code and no database access.

**Prerequisites:** P10, **P10A**; SetSpec P4 (frozen schemas) **and §0 above (vocabulary 1.1)**.

**Work**
* Capability mapping configuration (benchmark metrics → capabilities, with weights), versioned.
* Aggregation implementing [ADR-0017](../../adr/0017-benchmark-confidence-and-freshness.md):
  sample, consistency, freshness, environment, identity **and `judge_validity_factor`** — six factors,
  the sixth being 1.0 for every rung 1–4 measurement so no previously specified result changes value
  ([ADR-0032 §2](../../adr/0032-judge-validity-and-user-capability-namespace.md)); hard separations
  **including `goal_hash` and judge set identity**; policy version recorded. Freshness decays from `measured_at` — the latest `completed_at` among the contributing
  runs — never from `computed_at`
  ([ADR-0022 §2](../../adr/0022-capability-evidence-record-contract.md)).
* The full `capability.evidence` field set from
  [ADR-0022 §1](../../adr/0022-capability-evidence-record-contract.md), including
  `vocabulary_version`, `dataset_hashes` and per-benchmark `prompt_subset_hashes`.
* Goal evidence: emitted under `user.<slug>`; the goal-sourced field group from
  [ADR-0032 §5](../../adr/0032-judge-validity-and-user-capability-namespace.md); the optional
  secondary emission into a declared `contributes_to` capability as one weighted source among
  others — **never as that capability alone**.
* The gate wired into aggregation: a goal below `calibration.min_agreement` produces **no evidence
  row**, and the aggregation service says so in its report rather than skipping silently.
* `GET /evidence/export?since=` filtering on `computed_at`, and `complete: true|false` on every
  bundle.
* `capability_evidence` table, recomputation service, staleness detection and badging.
* `GET /api/v1/evidence`, `GET /api/v1/evidence/export`, `freeweight evidence show|export`.
* Contract tests against SetSpec goldens; publish the OpenAPI snapshot.
* **The generated configuration reference** ([Configuration Standards
  §8](../../standards/configuration-standards.md)): `docs/configuration.md`, produced from the
  settings model so it cannot drift, listing per field its key path, environment variable, type,
  default, valid range, whether it is runtime-changeable, its security implications and an example.
  A CI job fails when the committed file differs from the generated one.

  It lands here because the write-or-check pattern now exists in the repository —
  `scripts/sync_docs.py` is the same shape — so the second generator is much cheaper than the first
  was, and pydantic already holds every field, type, default and constraint. Spec §12 is the
  hand-maintained authority until then, and it has drifted twice: `[goals]`, `[judge]`,
  `[calibration]` and `[runtime]` were all added without it.
* **The blinded grading UI for rung-4 (`human`) criteria**, and the CLI equivalent. A
  `rung: "human"` criterion already validates, hashes, lints and appears in `score_method_mix`, and
  then skips every sample with `human_grade_pending` — so a goal that declares one measures less of
  itself than it says. The grading *machinery* exists: Phase 10A's calibration grading writes real
  `calibration_grades` rows through the same blinded presentation
  ([Subjective Goals §3.3, §5.2](subjective-goals.md)). What is missing is the second entry point,
  over an ordinary run's samples rather than a calibration set. It lands here because this is the
  phase where a human grade would first have somewhere to go: evidence. Phase 10 was named as the
  owner and shipped the calibration half only, which is how it came to belong to no phase.

**Files/subsystems**
```text
src/freeweight/domain/{capability_mapping,confidence}.py
src/freeweight/services/evidence.py  src/freeweight/web/routes/evidence.py
src/freeweight/cli/commands/evidence.py  src/freeweight/config/capability_weights.toml
src/freeweight/web/routes/grading.py  src/freeweight/web/templates/grading/
scripts/generate_config_reference.py  docs/configuration.md
tests/unit/{test_confidence,test_capability_mapping}.py
tests/contract/{test_evidence_schema,test_evidence_export}.py
tests/integration/test_human_grading.py
```

**Tests**
* Confidence factors individually and combined, against hand-computed values — six of them now.
* `judge_validity_factor` is exactly 1.0 for every native and external suite: a regression test over
  the whole catalogue, so this ADR provably changed no existing number.
* A goal below the gate contributes **no** evidence row for `user.<slug>` **and none** for its
  `contributes_to` capability — the second half is the one that will be forgotten.
* A goal declaring `contributes_to` appears in both places, with the `user.*` record retaining the
  goal's identity that the blended record loses.
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
  evidence, **including a `user.*` record with its calibration block intact**.
* A `setspec` build predating vocabulary 1.1 accepts a 1.1 bundle and ignores its `user.*` records
  rather than failing — the forward-compatibility degradation ADR-0009 promises, asserted end to end
  rather than assumed.
* Staleness badging appears when freshness drops below the threshold or drift is detected.

**Acceptance criteria**
1. Every §20 criterion in the [spec](spec.md) is met.
2. A bundle exported by FreeWeight is consumed by a `setspec`-only harness with no FreeWeight import
   and no database access.
3. Evidence records name their contributing benchmarks, weights and sample counts, and the UI can
   explain any score.
4. A calibrated goal exports as `user.<slug>` evidence whose confidence visibly carries the validity
   factor, and an uncalibrated one exports nothing at all.
5. FreeWeight 1.0-rc is tagged; **LoadCoach development may begin.** The evidence contract is frozen
   *with* the goal field group in it — the whole reason Phases 8A–10A precede this one.

**Known risks:** confidence parameters are judgement calls. Mitigated by making them configuration,
recording the policy version, and revisiting with real data.
**Likely failure modes:** absence of evidence rendered as zero capability; merging across versions;
emitting uncalibrated goal evidence at the confidence floor because "degrade, never discard" was
applied without reading [ADR-0032 §3](../../adr/0032-judge-validity-and-user-capability-namespace.md),
which deliberately departs from it here.
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
1. All 18 acceptance criteria in the [spec](spec.md) §20 pass.
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


---

# Appendix A — Model assignment

Two different questions get asked with the same words, so both are answered, separately:
which model **builds** FreeWeight (A.1, A.2), and which model FreeWeight **uses as a juror** at run
time (A.3).

**Relationship to the suite-wide guide.** [`roadmap/model-assignment.md`](../../roadmap/model-assignment.md)
answers the same build-time question at one row per phase for all nine repositories, and its own
§2.7 covers FreeWeight. This appendix is the deeper, FreeWeight-specific cut — per-step rather than
per-phase, plus the runtime-juror question the suite-wide guide does not address. Where the two
disagree on a phase's difficulty, this appendix is authoritative for FreeWeight; the suite-wide
guide should be updated to match rather than treated as a second, silently diverging opinion.

## A.1 Development models — one line per phase

Reasoning level is the effort setting, not a second model choice: `medium` for well-trodden
patterns, `high` where a wrong answer is expensive, `xhigh` where a wrong answer is expensive *and*
hard to detect, `max` where a wrong answer is invisible.

**The governing rule:** escalate for work where being subtly wrong produces a *plausible* result.
A broken migration announces itself; a `kappa_w` that is wrong by a factor stays quiet for months.

| # | Phase | Model | Level | Reasoning |
|---|---|---|---|---|
| 0 | SetSpec vocabulary 1.1 (upstream) | Sonnet 5 | medium | One frozenset entry, one version string, two schemas. Fully specified by ADR-0032 §1 and §5; nothing to decide |
| 1 | It runs, it answers, it has a CLI | Sonnet 5 | medium | Scaffolding against established standards. **Escalate to high** for the config precedence chain and the unsafe-binding refusals — a refusal that does not fire is a security defect that ships silently |
| 2 | Storage foundation | Sonnet 5 | medium | Standard SQLAlchemy/Alembic work. **Escalate to Opus 5 / high** for the first migration and the dialect abstraction: the baseline migration is permanent, and a SQLite-only assumption baked in here surfaces in Phase 12 |
| 3 | Model discovery through ModelRack | Sonnet 5 | high | Identity reconciliation is the subtle part — retags, digest vs `name_only`, never overwriting a descriptor snapshot (ADR-0008, ADR-0024). Wrong identity handling corrupts every downstream comparison |
| 4 | Telemetry and machine profile | Sonnet 5 | high | Sampler lifecycle under reload, and `UNSUPPORTED` propagation. A sensor that returns 0 instead of unsupported is exactly the failure ADR-0016 exists to prevent, and it looks like data |
| 5 | Run engine on the fake provider | **Opus 5** | high | The plan's own note: the scheduler and the replay/live SSE handoff are the two subtlest components in the application. State machine, cancellation consistency at every phase, gap-free event sequences |
| 6 | First real measurements + provenance | **Opus 5** | high | Where "confident wrong numbers" are born: backend vs client timing kept separate, cold/warm never mixed, the reproducibility fingerprint's input set. The fingerprint's contents are a judgement call with years of comparability riding on them |
| 7 | Deterministic quality suites | Sonnet 5 | high | Scorers are many small formulas against clear contracts. **Escalate to Opus 5 / high** for mock-tool containment (a security boundary) and for capability gating — scoring a refusal as a capability failure is the classic error here |
| 8 | Judgement-dependent suites | **Opus 5** | xhigh | Judge bias mathematics, audit precision with the clean-code false-positive rate, correction uplift vs regression rate, effective context. A "flags everything" model must score badly, and getting that wrong produces a leaderboard that rewards noise |
| 8A | Goal packs and deterministic goal criteria | Sonnet 5 | medium | Thirteen small pure rule functions, tables, CRUD, CLI — volume, not subtlety. **Escalate to Opus 5 / high** for the `goal_hash` boundary, the rubric lint heuristics, and the regex/import hardening |
| 8B | **Calibration, the jury, and the gate** | **Opus 5** | **max** | The intellectual core of the subjective-goal feature. `kappa_w`, Krippendorff's alpha, the shrinkage term, the anchor/holdout partition and its leak guarantee. A subtly wrong agreement statistic produces entirely plausible numbers and stays invisible for months — this is the definition of `max` |
| 9 | Memory, energy, reliability, comparison | **Opus 5** | high | KV theory from architecture fields, VRAM slope fitting, energy integrated over irregular timestamps, and the comparability rules that decide what may never be averaged. Each is a formula whose wrong answer looks reasonable |
| 10 | Dashboard, results, data management, exports | Sonnet 5 | medium | Server-rendered UI against established patterns. **Escalate to Opus 5 / high** for the anti-lie test — every dashboard figure recomputed from raw samples — which is the one thing standing between the UI and quiet divergence |
| 10A | Goal authoring wizard and starter packs | **Opus 5** | high | The teaching *is* the product. Step 2's two questions, the calibration report copy, and the four starter packs' deliberate deterministic-weight progression. **Sonnet 5 / medium** for the templates, forms and grading UI once the flow is decided |
| 11 | Capability evidence and the LoadCoach contract | **Opus 5** | xhigh | **M3 contract freeze.** Six-factor confidence, hard separations, and the gate's "no row at all" rule — which a careful implementer gets wrong precisely *by* being careful and emitting at the floor. Everything decided here is expensive to change afterwards |
| 12 | Adopt WeightsDB and MirrorWall | Sonnet 5 | high | A refactor whose success criterion is *no behaviour change*. Bounded, but session and pragma handling differ subtly between the inline code and the extracted package |
| 13 | External benchmark adapters | Sonnet 5 | high | Adapter-per-benchmark is repetitive work. **Escalate to Opus 5 / high** for the subprocess isolation boundary and for parsing external output as untrusted input (ADR-0018) |
| 14 | Hardening and 1.0 | Sonnet 5 | high | Performance budgets, security checklist, docs, upgrade paths — enumerable. **Escalate to Opus 5 / xhigh** for the final audit of all 18 spec §20 acceptance criteria and the gold standards, where the job is to find what everyone stopped seeing |

Three phases carry the risk for the whole application: **5** (the engine everything runs on),
**8B** (the instrument the subjective feature depends on) and **11** (the contract that cannot be
changed later). If effort is rationed anywhere, ration it elsewhere.

## A.2 Development models — which model writes which step

A.1 assigns a phase; this assigns the steps *within* the goal phases, where the mix inside one
phase varies more than the phase-level line can express.

The house rule first, because it overrides the table: **the docstring-first step is always Opus.**
This project defines behaviour, then writes the Google-style docstring including what the function
*refuses*, then tests, then implements. Deciding what a function refuses is a design act. Where the
table below assigns implementation to Sonnet, it means "implement against a contract Opus already
wrote", not "hand Sonnet the module name".

| Phase | Step | Model | Why |
|---|---|---|---|
| §0 | SetSpec vocabulary root, schema additions, forward-compat tests | **Sonnet 5** | Small, mechanical, tightly specified by ADR-0032 §1 and §5 |
| 8A | Goal pack schema, `goal_hash` boundary (what it covers and excludes) | **Opus 5** | The exclusion list is a judgement call with a year of comparability riding on it |
| 8A | The thirteen rule scorers | **Sonnet 5** | Each is a small pure function against a written contract; volume, not subtlety |
| 8A | Rule scorer *contracts* — what each refuses, what returns `unsupported` vs `0` | **Opus 5** | This is where "unsupported is not zero" is either honoured or quietly lost |
| 8A | Tables, migrations, repositories, CRUD routes, CLI commands | **Sonnet 5** | Well-trodden patterns already established in P2–P7 |
| 8A | The rubric lint heuristics | **Opus 5** | Deciding when a rule *could* cover a judged criterion is genuinely hard and wrong answers train users to ignore the lint |
| 8A | Regex dialect linting, import hardening, traversal defences | **Opus 5** | Security boundary on user-supplied input |
| 8B | `kappa_w`, Krippendorff's alpha, Spearman, shrinkage, the validity factor | **Opus 5** | The intellectual core. A subtly wrong agreement statistic produces *plausible* numbers and stays invisible for months |
| 8B | Partition semantics and the holdout-leak guarantee | **Opus 5** | The single assumption the whole feature's honesty rests on |
| 8B | Hand-computed test fixtures and synthetic graders | **Opus 5** | The fixtures are the specification of correctness here; generating them carelessly defeats the tests that depend on them |
| 8B | Jury assembly, blinding, order randomization, self-judging refusal | **Sonnet 5** | Mechanical once the rules are stated, and P8 already built the infrastructure |
| 8B | The judge prompt record (anchors as few-shot) | **Opus 5** | Prompt design determines the agreement figure; it is the instrument's calibration curve |
| 8B | Gate wiring, error-code separation, degradation paths | **Sonnet 5** | Specified precisely in ADR-0032 §3 and spec §13 |
| 10A | Wizard flow, and especially Step 2's two questions | **Opus 5** | The teaching is the product; generic wizard copy would waste the entire feature |
| 10A | Calibration report copy — bands, consequences, framing a failed gate | **Opus 5** | A user who reads "kappa 0.31" and nothing else concludes the feature is broken |
| 10A | Wizard templates, forms, grading UI, progressive enhancement | **Sonnet 5** | Standard server-rendered UI work against MirrorWall components |
| 10A | Starter pack content — tasks, criteria, worked graded sets | **Opus 5** drafts, **Sonnet 5** expands | The four packs teach by example and their deterministic-weight progression is deliberate; the bulk JSON around each decision is not |
| 10A | E2E journey tests, accessibility tests | **Sonnet 5** | Enumerable against acceptance criteria |
| 11 | Six-factor confidence, gate-in-aggregation, dual emission | **Opus 5** | Frozen contract; the "no row at all" rule is the one a careful implementer will get wrong by being careful |
| 11 | Export plumbing, consumer harness, OpenAPI snapshot | **Sonnet 5** | Contract already frozen by then |
| any | Test parametrization tables, fixture expansion, docstring scaffolding from these specs, CHANGELOG entries | **Haiku 4.5** | Bulk transformation of an existing decision into an existing shape |

Rule of thumb, stated once: **Opus for anything where being subtly wrong produces a plausible
number.** That is most of 8B and the wizard copy, and almost nothing in 8A's rule library.

## A.3 Runtime models — which models FreeWeight should use as jurors

Distinct from A.1–A.2: this is what the *application* picks at run time, and what the wizard should
default to.

**Selection criteria, in order.** Jury quality on subjective criteria depends far less on parameter
count than on three things: reliable structured output (a juror that returns prose where a grade was
asked for is noise), instruction-following (the rubric is a long instruction), and **error
decorrelation** between jurors.

* **Three distinct model families, not three sizes of one family.** Two checkpoints from the same
  lineage share their biases, so their agreement is inflated and the jury's dispersion understates
  the real error. Family diversity is the reason the jury exists.
* **Rank candidates by their own `native.judge` results** — position bias, verbosity bias,
  self-preference, transitivity violations — which Phase 8 already measures for every installed
  model. `GET /judges` surfaces exactly this, and the wizard's default jury is the top three
  distinct families by those metrics. **This is the correct mechanism and it should be preferred over
  any hard-coded recommendation**, including one written into this plan: it uses measurements from
  the user's own machine rather than someone else's opinion, and it stays correct as models change.
* `temperature = 0.0`, `repetitions = 3`. Repetitions at temperature zero still vary — batching and
  kernel non-determinism see to that — and that residual variance is worth measuring rather than
  assuming away.
* A juror must be excluded when it is the candidate. Enforced, not advisory.

**For the remote opt-in**, a frontier model is a materially better instrument on style and tone, and
the current Claude models (`claude-opus-5`, `claude-sonnet-5`) are the natural choice where the user
has enabled it. It is off by default, requires two separate opt-ins, is recorded in the fingerprint,
and separates results — see [ADR-0031 §4](../../adr/0031-user-defined-goal-benchmarks.md). The
default install measures offline, and should.

**For the calibration sample spread** (§5.1 of [Subjective Goals](subjective-goals.md)), deliberately
include a model expected to do *badly* at the goal. A calibration set with no weak examples has no
variance for the agreement statistic to work with, and the wizard's grade-distribution check will
refuse to compute on it.
