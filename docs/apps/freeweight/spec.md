# FreeWeight — Specification

**Type:** Application · **Import/distribution name:** `freeweight` · **Default port:** 8765 · **Env prefix:** `FREEWEIGHT_`
**Status:** Specified, not implemented. Extended 2026-08-26 with user-defined goal benchmarks
(ADR-0031, ADR-0032).
**Related:** [Subjective Goals](subjective-goals.md) · [API](api.md) · [Development Plan](development-plan.md)

---

## 1. Purpose

Answer, with evidence a user can inspect and reproduce: *how well does this model perform on this
machine, for this capability, under these settings?*

And, for work whose ground truth lives in the user's head rather than in a corpus: *how well does
this model meet **my** stated goal — and how much should I trust that answer?* A goal is measurable
here only once the user has graded enough examples for FreeWeight to characterize the instrument
measuring it (ADR-0031).

FreeWeight measures local open-weight models across capability, efficiency, reliability and resource
use; preserves every raw measurement and enough provenance to reproduce it; presents the results in
a dense, honest UI and CLI; and exports capability evidence that other tools — LoadCoach in
particular — can consume without touching FreeWeight's internals.

## 2. Scope

* Benchmark definitions, fixtures, datasets and manifests (native suites).
* Adapters for established external benchmarks, run as isolated subprocesses.
* **User-defined goal suites**: authoring, deterministic rule criteria, anchored rubric criteria,
  goal packs, versioning and portability ([Subjective Goals](subjective-goals.md)).
* **Judge calibration**: user grading, anchor/holdout partition, agreement measurement against
  user-supplied ground truth, and the gate that decides whether a rubric is measurable at all.
* Benchmark execution: scheduling, repetition, warm-up/cooldown, cancellation, resumption.
* Scoring, with deterministic methods preferred over model-judged ones.
* Result storage: runs, tests, samples, metrics, tool calls, events, telemetry, artifacts.
* Provenance and reproducibility fingerprints.
* Comparison: models, quantizations, runtime profiles, machines, time.
* Capability evidence aggregation with confidence and freshness
  (ADR-0017).
* Export and a read-only evidence API.
* Web UI and CLI over one service layer.

## 3. Explicit non-goals

* **No routing.** FreeWeight never chooses a model for production work; that is LoadCoach's job and
  FreeWeight contains no task profiles and no routing scores.
* **No production orchestration.** No job queue for user workloads, no inference gateway for other
  applications.
* No content workflows.
* No model training, fine-tuning, quantization or conversion.
* No leaderboard publishing, no telemetry upload, no comparison against other people's machines.
* No single universal "model score" as a default.
* **No automatic rewriting of a user's rubric.** FreeWeight diagnoses which criteria a judge
  disagrees with the user on; it never reworks the criterion to make it measurable. That would
  optimize the target into the instrument (ADR-0031 §3).
* **No judged capability evidence without calibration.** A goal below the agreement gate produces a
  full, inspectable result and no evidence
  (ADR-0032 §3).
* No comparison of goal results across users who do not share a goal pack — two people's "good tone"
  are two different measurements, separated by `goal_hash`.
* No shared or hosted goal library; goal packs move as files, between machines the user controls.
* No execution of model-generated code outside a sandbox
  (ADR-0018).
* No writing to another application's database, and no reading of one.

## 4. Responsibilities

| Area | Responsibility |
|---|---|
| Model discovery | Through ModelRack only; persist canonical identities and descriptor snapshots |
| Machine profiling | Through SweatMeter only; persist machine profiles and per-run telemetry |
| Benchmark catalogue | Native suites, external adapters, goal suites, manifests, dataset hashes, version pinning |
| Goal authoring | Guided wizard and CLI interview; rubric linting; goal packs as versioned, portable, hand-editable files |
| Calibration | Collect user grades; partition anchors/holdout; measure judge-vs-user agreement; gate evidence on it; diagnose disagreement |
| Judging | Jury selection, blinding, order randomization, repeated trials, inter-judge agreement, self-judging refusal, remote opt-in |
| Execution | One GPU workload at a time; state machine; repetitions; cancellation; resumption after interruption |
| Scoring | Deterministic first; every formula unit-tested; raw samples always preserved |
| Aggregation | Metrics with dispersion and sample counts; category scores; user-defined weighted profiles |
| Evidence | Capability evidence with confidence and freshness, exported via file or read-only API |
| Provenance | Reproducibility fingerprint and its full input document on every run |
| Presentation | Dense web UI, scriptable CLI, exports (JSON/JSONL/CSV) |
| Data management | Preview-then-confirm deletion, backup, vacuum, retention |

## 5. Dependencies

**Suite:** `baseaicore`, `setspec` (capability vocabulary **≥ 1.1**, for the `user` root —
ADR-0032 §1), `modelrack`,
`sweatmeter`, `weightsdb` (adopted at Phase 12), `mirrorwall` (adopted at Phase 12).
**Third party:** `fastapi`, `uvicorn[standard]`, `typer`, `pydantic`, `pydantic-settings`,
`sqlalchemy`, `alembic`, `jinja2`.
**External services:** a model provider (Ollama by default). Optional: a container runtime or
`bwrap` for code-execution benchmarks; external benchmark packages the user installs.

**Required at startup:** none. FreeWeight starts, serves its UI and CLI, and reports degraded health
when a provider is unavailable.

## 6. Consumers

* **Users** — web UI, CLI, exported files.
* **LoadCoach** — capability evidence, via `GET /api/v1/evidence` or an exported bundle. Read-only,
  versioned, and the only supported integration point.
* **External tools** — the same public API and exports.

## 7. Public APIs

### 7.1 HTTP (`/api/v1`, full detail in [API](api.md))

```text
GET    /api/v1/health                        GET    /api/v1/version
GET    /api/v1/system/status                 GET    /api/v1/system/telemetry/stream   (SSE)
GET    /api/v1/machines                      GET    /api/v1/machines/{id}
GET    /api/v1/models                        POST   /api/v1/models/discover
GET    /api/v1/models/{model_ref}            GET    /api/v1/models/{model_ref}/results
GET    /api/v1/benchmarks                    GET    /api/v1/benchmarks/{key}
POST   /api/v1/runs                          GET    /api/v1/runs
GET    /api/v1/runs/{id}                     POST   /api/v1/runs/{id}/cancel
GET    /api/v1/runs/{id}/events   (SSE)      POST   /api/v1/runs/{id}/repeat
GET    /api/v1/runs/{id}/tests               GET    /api/v1/runs/{id}/tests/{test_id}/samples
GET    /api/v1/results                       GET    /api/v1/results/compare
GET    /api/v1/results/export                GET    /api/v1/evidence
GET    /api/v1/evidence/export               GET    /api/v1/database/stats
POST   /api/v1/database/delete-preview       DELETE /api/v1/database/results
POST   /api/v1/database/backup               POST   /api/v1/database/vacuum
GET    /api/v1/settings                      PUT    /api/v1/settings

GET    /api/v1/goals                         POST   /api/v1/goals
GET    /api/v1/goals/{slug}                  PUT    /api/v1/goals/{slug}
POST   /api/v1/goals/{slug}/validate         DELETE /api/v1/goals/{slug}
POST   /api/v1/goals/{slug}/suggest-rules    GET    /api/v1/goals/{slug}/tasks
GET    /api/v1/goals/{slug}/calibration      POST   /api/v1/goals/{slug}/calibration/samples
POST   /api/v1/goals/{slug}/calibration/grades
POST   /api/v1/goals/{slug}/calibration/run  GET    /api/v1/goals/{slug}/calibration/report
GET    /api/v1/goals/{slug}/export           POST   /api/v1/goals/import
GET    /api/v1/goals/starters                POST   /api/v1/goals/starters/{key}/fork
GET    /api/v1/judges                        POST   /api/v1/judges/validate
```

### 7.2 CLI

```text
freeweight serve | health | doctor | version
freeweight config show|validate|init|path
freeweight db upgrade|status|backup|restore|vacuum
freeweight models list|show|refresh
freeweight benchmarks list|show
freeweight run start|list|show|cancel|wait|repeat
freeweight results list|show|compare|export
freeweight evidence show|export
freeweight external list|install|verify
freeweight goals list|show|init|edit|validate|suggest-rules
freeweight goals calibrate|calibration show|grade|report
freeweight goals export|import|fork-starter|starters
freeweight judges list|validate
freeweight prompts list|show|build
freeweight token create|list|revoke
```

### 7.3 Exports

`benchmark.result`, `benchmark.run_summary`, `capability.evidence`, `benchmark.evidence_bundle`,
`benchmark.goal_pack`, `benchmark.calibration_report` (all SetSpec-versioned), plus flattened CSV
for spreadsheet use.

## 8. Inputs

Model references, benchmark suite/test selections, execution parameters (repetitions, timeouts,
sampling parameters, context and output series, concurrency), benchmark datasets installed by the
user, prompt packs, configuration, and imported result files (for viewing results produced elsewhere).

For goal suites additionally: goal definitions (criteria, weights, ladder rung per criterion, rule
parameters), the user's own task prompts, calibration samples and **the user's grades of them**, jury
configuration, and imported goal packs. The user's grades are the ground truth of the entire feature;
they are user data, backed up with the database and never transmitted anywhere.

## 9. Outputs

Runs, tests, samples, metrics, tool-call records, telemetry series, events, artifacts (raw responses,
generated code, external benchmark output), aggregate results, capability evidence, comparisons,
exports, and the rendered UI.

For goal suites additionally: per-criterion scores with the ladder rung that produced each, judge
rationales, inter-judge agreement, calibration reports (`kappa_w`, `rho`, `mae`, `bias`, per
criterion, with `n_anchor` and `n_holdout`), disagreement diagnostics naming the criteria and samples
where judge and user diverged most, `score_method_mix`, and exportable goal packs.

## 10. Data ownership

Owns `freeweight.sqlite3` (or its PostgreSQL equivalent) exclusively: machines, models,
model_descriptors, runtime_profiles, benchmark_suites, benchmark_tests, runs, run_tests, samples,
metric_values, tool_calls, telemetry_samples, run_events, artifacts, capability_evidence, settings,
goals, goal_criteria, goal_tasks, calibration_samples, calibration_grades, calibration_reports,
judge_verdicts.
See Data Model.

Owns its artifact directory and its exports directory. Reads nothing belonging to another
application.

## 11. Public contracts

1. **Evidence contract.** `capability.evidence` and `benchmark.evidence_bundle` are the supported
   integration surface. LoadCoach consumes them and never queries FreeWeight's tables.
2. **Provenance contract.** Every exported result carries the full provenance set from
   Machine Identity §6.
3. **Confidence contract.** FreeWeight computes capability confidence per
   ADR-0017; consumers apply it and do not
   recompute it.
4. **Comparability contract.** Exports carry the measurement subject and benchmark version; consumers
   can therefore determine comparability without asking FreeWeight.
5. **API contract.** `/api/v1` per API Standards;
   additive within v1.
6. **Unsupported contract.** Unavailable measurements are `"unsupported"` everywhere — API, export,
   UI, database.
7. **Calibration contract.** A goal's judged criteria emit `capability.evidence` only when weighted
   `kappa_w` reaches `calibration.min_agreement`. Below it the run completes, every sample is
   inspectable, the result is badged `uncalibrated`, and **no evidence is emitted** — not discounted
   evidence, none (ADR-0032 §3).
8. **Goal identity contract.** `goal_hash` separates results exactly as a benchmark version does, and
   so does the judge set identity. A different rubric, or a different instrument, is a different
   measurement — never an average.
9. **Namespace contract.** Goal evidence is emitted under `user.<slug>`, a specialization of the
   reserved `user` root added at SetSpec capability vocabulary 1.1. A goal may *additionally* declare
   a shipped capability it contributes to; it is never emitted **only** as that shipped capability
   (ADR-0032 §1).

## 12. Configuration

`~/.config/freeweight/config.toml`, `FREEWEIGHT_*` environment variables, CLI flags, per
Configuration Standards. Principal sections:

```toml
[server]      host = "127.0.0.1"   port = 8765   allow_lan_exposure = false
              allowed_hosts = []     # required when host is not loopback (ADR-0026)
[storage]     database_url = "sqlite:///<data>/freeweight.sqlite3"
              auto_migrate = true on SQLite, false on PostgreSQL (database standards §5.1, §7)
              artifact_dir = "<data>/artifacts"   retention_days = 0     # 0 = keep everything
              backup_retention = 5   # automatic pre-migration backups kept (§7)
              statement_timeout_ms = unset          # PostgreSQL only; also sets lock_timeout
[provider]    kind = "ollama"      base_url = "http://127.0.0.1:11434"  timeout_seconds = 300
[providers]   allow_remote = false
[telemetry]   interval_ms = 1000   persist_during_runs = true   calibrate_overhead = true
[execution]   warmup_repetitions = 1   measured_repetitions = 3   cooldown_seconds = 5
              test_timeout_seconds = 600   run_timeout_seconds = 86400
              randomize_case_order = true   seed = 0
              gpu_index = 0                        # the device metrics are attributed to (ADR-0027)
              idle_gpu_threshold_percent = 10      # 0 disables the check
              idle_required_samples = 3   idle_wait_timeout_seconds = 120
              on_idle_timeout = "warn"             # warn | refuse — see §13
[sandbox]     tier = "auto"        # auto | container | bwrap | none(refuse)
              cpu_limit = 2        memory_limit_mb = 2048   timeout_seconds = 30
[external]    root = "<data>/external"    # per-benchmark environments
[goals]       root = "<config>/goals"              # goal packs; hand-editable JSON
              max_pack_bytes = 5_242_880           # import size cap
              rule_timeout_ms = 250                # per rule, per sample
[judge]       jury_size = 3                        # distinct local models; 1 disables the jury
              models = []                          # empty = auto-select from installed models
              repetitions = 3   randomize_order = true   blind_candidate_identity = true
              refuse_self_judging = true           # a juror never judges its own output
              allow_remote = false                 # requires providers.allow_remote too
              temperature = 0.0
[calibration] target_samples = 12   min_samples = 8
              holdout_fraction = 0.4   partition_seed = 0
              min_agreement = 0.40                 # weighted kappa_w gate for emitting evidence
              n_holdout_target = 10                # shrinkage denominator (ADR-0032 §2)
[logging]     level = "INFO"       include_content = false
```

Goal runs store full response text by default (`store_prompts`/`store_responses` forced on): a
judged score that cannot be re-read by the person who defined the rubric is not auditable, which
defeats the purpose. The privacy default in `[logging]` is unchanged for every other suite.

Benchmark **execution parameters** additionally resolve through the second precedence chain
(application → suite → test → saved settings → run overrides), and the resolved values are frozen
into every run record.

## 13. Error behaviour

Stable error codes (extending the shared set):

```text
PROVIDER_UNAVAILABLE      MODEL_NOT_FOUND           RUN_NOT_FOUND
PROVIDER_TIMEOUT          BENCHMARK_NOT_FOUND       RUN_ALREADY_RUNNING
PROVIDER_PROTOCOL_ERROR   DATASET_MISSING           RUN_NOT_CANCELLABLE
CONTEXT_LIMIT_EXCEEDED    DATASET_HASH_MISMATCH     SANDBOX_UNAVAILABLE
CAPABILITY_UNSUPPORTED    EXTERNAL_BENCHMARK_FAILED SCHEMA_VERSION_UNSUPPORTED
INSUFFICIENT_RESOURCES    PROMPT_INVALID            MIGRATION_REQUIRED
GOAL_NOT_FOUND            GOAL_INVALID              GOAL_PACK_INCOMPATIBLE
CALIBRATION_REQUIRED      CALIBRATION_INSUFFICIENT  JUDGE_UNAVAILABLE
JUDGE_SELF_JUDGING_REFUSED                          REMOTE_JUDGE_NOT_PERMITTED
```

Behavioural rules:
* A failed sample never becomes a zero score; it is stored with its error and excluded from
  aggregates, with the exclusion visible in the sample count.
* A skipped test records *why* it was skipped (`unsupported_capability`, `sandbox_unavailable`,
  `dataset_missing`, `insufficient_vram`).
* A run that dies mid-flight is `interrupted`, not `failed`; completed tests are retained and the run
  is resumable.
* **Idle detection has a defined outcome.** When `idle_gpu_threshold_percent > 0`, the run waits up to
  `idle_wait_timeout_seconds` for the GPU and CPU to fall below the threshold for
  `idle_required_samples` consecutive samples. If they do not, `on_idle_timeout` decides:
  `warn` (default) proceeds and records the degradation `measured_while_busy` with the observed
  utilization on the run, so contamination is visible in the provenance rather than invisible;
  `refuse` fails the run with `INSUFFICIENT_RESOURCES` and the observed numbers. Silently proceeding
  with no record was the previously unspecified third option, and it is the one that produces
  unexplained dispersion months later. This is the mechanism that makes "FreeWeight and LoadCoach on
  one GPU" honest rather than merely documented.
* On a machine where more than one GPU is visible and the provider does not report placement, memory,
  KV and energy tests are **skipped** with `multi_gpu_placement_unknown`; quality, throughput and
  latency tests run normally (ADR-0027).
* **A goal below the agreement gate is not an error.** The run completes normally; the result is
  badged `uncalibrated` and emits no evidence. `CALIBRATION_INSUFFICIENT` is raised only when fewer
  than `calibration.min_samples` grades exist — that is, when the user has not yet done the work,
  as distinct from having done it and learned the rubric is not measurable.
* `CALIBRATION_REQUIRED` is raised at run start when a goal has rung-5 criteria and no calibration
  record at all; the error names the number of samples still to grade.
* A jury that cannot be assembled (fewer than `judge.jury_size` eligible models, or the only
  eligible juror is the candidate itself) degrades to the largest eligible jury and records
  `jury_reduced` with the reason. A jury of zero is `JUDGE_UNAVAILABLE`; judged criteria are
  `skipped`, rule criteria still score, and the partial result says so.
* Rule criteria never depend on a provider. A goal whose criteria are entirely rungs 1–3 runs with
  no model judging at all, and is fully available when the provider is down.
* Cancellation is honoured at every phase and leaves consistent data.
* Full degradation matrix: Graceful Degradation.

## 14. Security considerations

* Loopback by default; non-loopback requires tokens, the exposure acknowledgement and
  `server.allowed_hosts`. The `Host` header is validated on every request before routing
  (ADR-0026).
* **Model-generated code is never executed on the host.** Tiered sandbox, refusal at the bottom tier.
* Native tool benchmarks expose only mock tools over fixture data — no shell, no unrestricted
  filesystem, no network, no real database.
* External benchmark adapters run as subprocesses with an argument list, a timeout and captured
  output; their results are parsed as untrusted input.
* Datasets are verified against pinned hashes before use; archive extraction is hardened.
* Prompts and responses are stored as hashes by default; full text only when the run explicitly
  requests it.
* Artifact paths are containment-checked; artifact files are `0600`.
* **User-authored goal content is untrusted input to FreeWeight's own renderer.** Goal templates
  render through the same `setspec.prompts` loader with `StrictUndefined` and no filesystem or
  network access in the Jinja2 environment; user regex runs under `rule_timeout_ms` with a linted
  dialect (no backreferences, bounded repetition) so a catastrophic-backtracking pattern fails the
  criterion rather than the process.
* Imported goal packs are size-capped, path-containment-checked, schema-validated and hash-verified
  before a single file is written; an import never overwrites an existing goal in place.
* A goal pack carries the grader's identity as free text the user supplied, never a system account
  or an email harvested from the environment.
* Destructive database operations preview, confirm, transact and back up.

## 15. Performance considerations

FreeWeight's own overhead must be small enough not to distort what it measures:

| Measure | Target |
|---|---|
| Per-sample overhead outside the provider call | ≤ 10 ms |
| Overhead as a share of a 2 s inference | ≤ 0.5 % |
| Telemetry sampling effect on measured throughput | ≤ 1 %, measured and recorded per run |
| Run start (validate → persist → first call) | ≤ 500 ms |
| Aggregation of a 10 000-sample run | ≤ 5 s |
| Dashboard aggregate over 100 k samples | ≤ 200 ms |
| Export of a 10 000-sample run | ≤ 10 s |
| Rule-criterion scoring, per sample, all rules | ≤ 50 ms |
| Calibration agreement computation, 20 samples × 8 criteria × 3 jurors | ≤ 1 s |
| Goal pack validate + lint | ≤ 500 ms |

Timing uses `time.perf_counter_ns()`; wall-clock timestamps are separate. Cold and warm measurements
are never mixed. A calibration test records the sampling overhead on each run so the distortion is
part of the provenance rather than an assumption.

## 16. Cross-platform considerations

Linux tier 1. On Windows/macOS: the application, database, discovery, quality benchmarks and exports
work; host telemetry is `unsupported` (GPU telemetry works where `nvidia-smi` is present); memory,
KV-cache and energy benchmarks are **skipped with a recorded reason**; code-execution benchmarks
require a container runtime. Goal suites are fully supported on every platform: rule criteria need
nothing but Python, and judged criteria need only a provider. See
Cross-Platform Standards.

## 17. Observability

* Structured logs with `request_id`, `run_id`, `run_test_id`, `sample_id`, `model_canonical_id`,
  and for goal runs `goal_slug`, `goal_hash`, `criterion_key`, `juror_model_id`.
* Persisted run events (SetSpec `event.envelope`) with gap-free sequences and SSE replay.
* Health components: `database`, `provider`, `gpu_telemetry`, `sandbox`, `external_benchmarks`,
  `prompts`, `goals` (packs parse and validate), `judges` (a jury can be assembled).
* `<app> health` reports, per goal, the calibration agreement, the holdout size and the age of the
  calibration record — a goal whose calibration has aged past its half-life is surfaced the same way
  stale evidence is.
* `GET /api/v1/system/status`: active run, queue depth, telemetry snapshot, threadpool saturation,
  disk headroom.
* Every headline metric drills to its samples in at most two interactions.

## 18. Test strategy

| Layer | Coverage |
|---|---|
| Unit | Every metric formula (known values, boundaries, division guards, `UNSUPPORTED` inputs); every scorer (known-pass, known-fail, boundary, malformed response, missing data); state machines; provenance assembly; aggregation with excluded samples |
| Contract | SetSpec exports validate and match goldens; evidence bundle consumable with no FreeWeight code; OpenAPI snapshot; error codes |
| Integration | Migrations both dialects; repositories; event persistence and replay; run execution end to end against `FakeProvider` |
| E2E | Full journeys through HTTP **and** CLI: discover → run → watch → cancel → compare → export → delete |
| Failure-path | Provider absent/timeout/malformed; GPU absent; sandbox absent; dataset missing/hash mismatch; disk full; kill mid-run then restart and resume |
| Performance | Every budget in §15 |
| Security | Sandbox refusal; traversal; oversize; no secret in logs; mock tools cannot escape fixtures |
| Goal & calibration | Rule scorers against known text; `kappa_w`/`rho`/`mae`/`bias` against hand-computed values and published worked examples; partition determinism under a fixed seed; a synthetic **perfectly-agreeing** grader yields `kappa_w = 1.0` and a synthetic **random** grader yields ≈ 0; the gate refuses evidence and still emits the result; jury assembly, blinding, self-judging refusal, `jury_reduced` degradation; goal pack round-trips byte-identically through export/import; `goal_hash` changes when a criterion changes and does not change when a task's display name does |
| Live (marked) | Real Ollama: a short real benchmark run producing plausible metrics; one goal run with a real jury producing plausible agreement |

The default suite runs with **no GPU, no Ollama, no network**.

## 19. Compatibility and versioning

* Application semantic versioning; API `v1`; SetSpec schema versions independent of both.
* Benchmark suites carry their own versions; a suite version change separates results rather than
  invalidating them.
* A benchmark's provenance carries the `prompt_subset_hash` of **the prompts that benchmark declares**,
  not the whole pack's hash — so editing an unrelated prompt separates nothing
  (ADR-0028).
* Database migrations are forward-only with tested upgrade paths from every released version.
* Result data is never silently reinterpreted by an upgrade: if a metric definition changes, the
  metric gets a new key and the old key is retained.

## 20. Acceptance criteria

1. `pip install freeweight && freeweight serve` works with only Ollama running; no configuration.
2. Models are discovered exclusively through ModelRack, persisted as canonical BaseAiCore identities
   with digests where available.
3. A benchmark run executes, streams progress, survives a browser refresh, and can be cancelled
   safely at any phase.
4. Every headline metric drills to the raw sample that produced it in ≤ 2 interactions.
5. Two runs of the same subject with the same fingerprint produce metrics within the documented
   tolerance; differing fingerprints are shown with a field-level diff.
6. Unsupported measurements appear as `—` in the UI and `"unsupported"` in exports — never `0`.
6a. Recomputing capability evidence over unchanged runs does not raise its confidence: freshness comes
   from `measured_at`, the latest completed run that contributed
   (ADR-0022).
7. Cold and warm measurements are never mixed in one headline number.
8. An evidence bundle exported by FreeWeight is imported by LoadCoach with no FreeWeight code or
   database access.
9. Code-execution benchmarks refuse to run when no sandbox tier is available.
10. The full test suite passes with no GPU, no Ollama and no network; coverage ≥ 85 % overall and
    ≥ 95 % in `domain/`.
11. Deleting results never deletes model or machine history, and always previews first.
12. All gold standards for FreeWeight in Gold Standards §2 are met.
13. A user with no prior setup can, from the UI alone, define a goal ("essays in my voice"), be shown
    which of their criteria a deterministic rule can check, supply their own tasks, grade twelve
    samples inline, and see a calibration report — without reading documentation and without editing
    a file. The wizard's output is a JSON goal pack they can then open in an editor and diff in git.
14. A deliberately unmeasurable rubric (criteria such as "make it good") produces a completed,
    fully-inspectable run badged `uncalibrated`, emits no capability evidence, and names the criteria
    the judge disagreed with the user on most — with the specific samples.
15. A goal whose criteria are entirely deterministic rules runs, scores and exports evidence with no
    judge involved and `judge_validity_factor = 1.0`.
16. Moving one criterion from a judged rung to a rule raises the goal's `judge_validity_factor`, and
    the UI shows `score_method_mix` before and after — the ladder's incentive is visible, not merely
    documented.
17. A goal pack exported on one machine, imported on another and re-run over the same model produces
    the same `goal_hash`; changing the jury separates the results rather than averaging them.
18. LoadCoach ignores a `user.*` capability unless a task profile names it, and any routing
    explanation that used one states the goal, the agreement and the holdout size in words.

## 21. Future extensions

* Additional external benchmark adapters (SWE-bench, TUA-Bench, BugsInPy, Defects4J, LongBench,
  InfiniteBench).
* llama.cpp (`llama-bench`) and vLLM (metrics endpoint) integrations once ModelRack supports them.
* Scheduled/unattended benchmark campaigns with regression alerting.
* Multi-machine result federation (import from other machines and compare with machine badges).
* Public shareable report bundles (opt-in, redacted).
* Result annotation and tagging for experiment tracking.
* A/B prompt studies as a first-class feature.
* Active-learning calibration: the application proposes which sample would most improve agreement if
  graded next, rather than a fixed holdout fraction.
* Bayesian judge-reliability modelling behind the same interface, once enough calibration data
  exists to fit it (ADR-0032).
* Multi-grader goals: several people grade one calibration set, with inter-grader agreement measured
  alongside judge agreement — a house style is a shared instrument, not one person's.
