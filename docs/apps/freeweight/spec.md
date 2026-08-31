# FreeWeight — Specification

**Type:** Application · **Import/distribution name:** `freeweight` · **Default port:** 8765 · **Env prefix:** `FREEWEIGHT_`
**Status:** Specified, not implemented. Corrected 2026-08-21 by the
[final architecture audit](../../reviews/final_architecture_audit.md) (ADR-0022, ADR-0024, ADR-0026–0028).
Extended 2026-08-26 with user-defined goal benchmarks ([ADR-0031](../../adr/0031-user-defined-goal-benchmarks.md),
[ADR-0032](../../adr/0032-judge-validity-and-user-capability-namespace.md)).
**Related:** [Benchmark Catalog](benchmark-catalog.md) · [Subjective Goals](subjective-goals.md) · [API](api.md) · [Data Model](data-model.md) · [Development Plan](development-plan.md) · [Risks](risks.md)

---

## 1. Purpose

Answer, with evidence a user can inspect and reproduce: *how well does this model perform on this
machine, for this capability, under these settings?*

And, for work whose ground truth lives in the user's head rather than in a corpus: *how well does
this model meet **my** stated goal — and how much should I trust that answer?* A goal is measurable
here only once the user has graded enough examples for FreeWeight to characterize the instrument
measuring it ([ADR-0031](../../adr/0031-user-defined-goal-benchmarks.md)).

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
  ([ADR-0017](../../adr/0017-benchmark-confidence-and-freshness.md)).
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
  optimize the target into the instrument ([ADR-0031 §3](../../adr/0031-user-defined-goal-benchmarks.md)).
* **No judged capability evidence without calibration.** A goal below the agreement gate produces a
  full, inspectable result and no evidence
  ([ADR-0032 §3](../../adr/0032-judge-validity-and-user-capability-namespace.md)).
* No comparison of goal results across users who do not share a goal pack — two people's "good tone"
  are two different measurements, separated by `goal_hash`.
* No shared or hosted goal library; goal packs move as files, between machines the user controls.
* No execution of model-generated code outside a sandbox
  ([ADR-0018](../../adr/0018-external-benchmark-isolation.md)).
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
[ADR-0032 §1](../../adr/0032-judge-validity-and-user-capability-namespace.md)), `modelrack`,
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

**Every path above is routable, and a test asserts it.**
`tests/contract/test_declared_surface.py` reads this section out of this document — not a list
maintained beside it, which would drift the same way — and fails when a declared path is not
served. A path a later phase owns is named in that test's `SCHEDULED` map with the phase that owns
it, so "not built yet" stays a decision with an owner rather than an absence nobody notices.

It was written because six paths had been declared and unbuilt since Phase 1: nothing in Phases
1–10A needed the API form of the models or benchmarks surfaces, so no test ever asked for one, and
the gap surfaced when a live end-to-end journey got a 404 at Phase 10A. **The test found three more
than that audit had** — the whole machines API, and `POST /runs/{id}/repeat`, which had a service
function and a CLI command and no route at all. That is the argument for asserting a specification
against its build rather than reading both and comparing them by eye.

### 7.2 CLI

```text
freeweight serve | health | doctor | version
freeweight config show|validate|init|path
freeweight db upgrade|status|backup|restore|vacuum
freeweight models list|show|refresh
freeweight benchmarks list|show                                      (Phase 12)
freeweight run start|list|show|cancel|wait|repeat
freeweight results list|show|compare|export
freeweight evidence show|export                                      (Phase 11)
freeweight external list|install|verify                              (Phase 13)
freeweight goals list|show|init|edit|validate|suggest-rules
freeweight goals calibrate|calibration show|grade|report
freeweight goals export|import|fork-starter|starters
freeweight judges list|validate
freeweight prompts list|show|build
freeweight token create|list|revoke                                  (waits on ADR-0014)
```

`run start` takes `--context-size` to override `[runtime]` for one run
([ADR-0023](../../adr/0023-runtime-profile-resolution.md) §3). A group marked with a phase is
scheduled and deliberately absent until then — a verb that exists and does nothing is worse than one
that does not, because `--help` advertises it. `benchmarks list|show` goes to Phase 12 with the
rest of the CLI surface work; its HTTP form ships now, so the data is reachable in the meantime.

### 7.3 Exports

`benchmark.result`, `benchmark.run_summary`, `capability.evidence`, `benchmark.evidence_bundle`,
`benchmark.goal_pack`, `benchmark.calibration_report` (all SetSpec-versioned), plus flattened CSV
for spreadsheet use, plus `freeweight.export` — the result export, which is FreeWeight's own
document rather than a shared contract
([ADR-0035](../../adr/0035-application-owned-document-schemas.md)).

**Two different artifacts carry a goal pack, and they are not interchangeable:**

| Artifact | Produced by | Contains | Read by |
|---|---|---|---|
| `benchmark.goal_pack` | `GET /api/v1/goals/{slug}/export` | One SetSpec envelope: the pack's *definition* — identity, criteria, weights, judge config, gate, hashes | Anything that wants to read what a goal measures |
| **Goal pack bundle** | `freeweight goals export` | Every file of the pack directory, hash-pinned — `goal.json`, `tasks/`, `prompts/`, `pack.json`, and the user's calibration samples and grades where present | `freeweight goals import` and `POST /api/v1/goals/import` |

The envelope is a description; the bundle is the pack itself, and only the bundle round-trips
([ADR-0031 §6](../../adr/0031-user-defined-goal-benchmarks.md)). Its format is described in
[Subjective Goals §2.3](subjective-goals.md).

### 7.4 A goal run has two phases

A goal run **generates every sample first, then judges them all**, rather than judging each answer
as it arrives.

| Phase | Resident | Does |
|---|---|---|
| Generation | The candidate | Answers every task; scores every rung 1–3 criterion; stores each sample as `awaiting_judgement` |
| — | *nothing* | The candidate is evicted (`provider.unload`) |
| Judging | The jurors | Grades the judged criteria from the **stored response text**, combines both halves into the composite, finishes each sample |

**Nothing about the measurement depends on the two being adjacent.** A jury grades text — the
collaborator's own signature takes a string — so *when* it reads changes nothing about what it
reads, and a two-phase run produces a verdict identical to a one-phase one, asserted directly
rather than assumed.

What the split buys is two things the interleaved form could not give:

* **Peak memory is the larger of the two models, not their sum.** Interleaved, with the provider's
  default `keep_alive`, the candidate stayed resident while each juror loaded — a jury of three
  meant four models at once, on a machine chosen because it had room for one. It also loaded and
  evicted `2N` times for `N` cases instead of `1 + jury_size` times.
* **Telemetry describes the candidate.** The recording window closes and residency is observed
  *before* any juror loads, so a goal run's peak VRAM and energy total are the candidate's rather
  than whichever model happened to be larger.

The judging phase contains its own failures: a jury that cannot be reached fails **that sample**,
with the reason, and the rest of the run proceeds. Aborting would discard a whole run's generation
over one unjudgeable answer. A run interrupted between phases resumes into the judging phase
without regenerating anything — the answers are already stored, which is why goal runs force
response storage on (§12).

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
See [Data Model](data-model.md).

Owns its artifact directory and its exports directory. Reads nothing belonging to another
application.

## 11. Public contracts

1. **Evidence contract.** `capability.evidence` and `benchmark.evidence_bundle` are the supported
   integration surface. LoadCoach consumes them and never queries FreeWeight's tables.
2. **Provenance contract.** Every exported result carries the full provenance set from
   [Machine Identity §6](../../architecture/machine-identity-and-reproducibility.md).
3. **Confidence contract.** FreeWeight computes capability confidence per
   [ADR-0017](../../adr/0017-benchmark-confidence-and-freshness.md); consumers apply it and do not
   recompute it.
4. **Comparability contract.** Exports carry the measurement subject and benchmark version; consumers
   can therefore determine comparability without asking FreeWeight.
5. **API contract.** `/api/v1` per [API Standards](../../standards/api-and-contract-standards.md);
   additive within v1.
6. **Unsupported contract.** Unavailable measurements are `"unsupported"` everywhere — API, export,
   UI, database.
7. **Calibration contract.** A goal's judged criteria emit `capability.evidence` only when weighted
   `kappa_w` reaches `calibration.min_agreement`. Below it the run completes, every sample is
   inspectable, the result is badged `uncalibrated`, and **no evidence is emitted** — not discounted
   evidence, none ([ADR-0032 §3](../../adr/0032-judge-validity-and-user-capability-namespace.md)).
8. **Goal identity contract.** `goal_hash` separates results exactly as a benchmark version does, and
   so does the judge set identity. A different rubric, or a different instrument, is a different
   measurement — never an average.
9. **Namespace contract.** Goal evidence is emitted under `user.<slug>`, a specialization of the
   reserved `user` root added at SetSpec capability vocabulary 1.1. A goal may *additionally* declare
   a shipped capability it contributes to; it is never emitted **only** as that shipped capability
   ([ADR-0032 §1](../../adr/0032-judge-validity-and-user-capability-namespace.md)).

## 12. Configuration

`~/.config/freeweight/config.toml`, `FREEWEIGHT_*` environment variables, CLI flags, per
[Configuration Standards](../../standards/configuration-standards.md). Principal sections:

```toml
[server]      host = "127.0.0.1"   port = 8765   allow_lan_exposure = false
              allowed_hosts = []     # required when host is not loopback (ADR-0026)
[storage]     database_url = "sqlite:///<data>/freeweight.sqlite3"
              auto_migrate = true on SQLite, false on PostgreSQL (database standards §5.1, §7)
              artifact_dir = "<data>/artifacts"
              backup_retention = 5   # automatic pre-migration backups kept (§7)
              statement_timeout_ms = unset          # PostgreSQL only; also sets lock_timeout
[provider]    kind = "ollama"      base_url = "http://127.0.0.1:11434"  timeout_seconds = 300
[providers]   allow_remote = false
[runtime]     context_size = unset                 # tokens; unset = let the provider choose
              gpu_layers = unset   threads = unset   batch_size = unset   keep_alive = unset
[benchmarks]  long_context_max_tokens = 32000       # ceiling of native.long_context's depth sweep
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
[evidence]    n_target = 30                        # ADR-0017's confidence parameters, every one
              quality_half_life_days = 90.0   performance_half_life_days = 30.0
              freshness_floor = 0.3   stale_below = 0.5   name_only_identity_factor = 0.6
              performance_drift_factor = 0.7   quality_drift_factor = 0.5
              goal_contribution_weight = 1.0       # a goal's weight inside contributes_to (ADR-0032 §1)
              capability_weights_path = unset      # custom capability_weights.toml; unset = shipped
[logging]     level = "INFO"       include_content = false
```

**`[evidence]` is ADR-0017's policy, and a change to it is a new policy.** Every parameter is
recorded on the evidence it produces beside a `policy_version`; customising any of them, or
pointing `capability_weights_path` at a copy of the shipped
`capability_weights.toml` ([benchmark catalog §6](benchmark-catalog.md)), derives a distinct
version from the content, so evidence computed under two policies coexists as two rows rather
than one row meaning two things ([ADR-0022 §3](../../adr/0022-capability-evidence-record-contract.md)).

Goal runs store full response text by default (`store_prompts`/`store_responses` forced on): a
judged score that cannot be re-read by the person who defined the rubric is not auditable, which
defeats the purpose. The privacy default in `[logging]` is unchanged for every other suite.

**There is no time-based retention, deliberately.** A measurement does not expire: a result taken
six months ago is exactly as true as one taken today. What invalidates it is the model leaving the
machine or the hardware changing — and a clock can detect neither. A `retention_days` setting
therefore measures the wrong thing, and re-running a suite because a timer fired would burn GPU
hours to reproduce a number that was already correct.

The deletion a user actually needs is **by model**: `DELETE /api/v1/database/results` with
`scope=model` removes every run of a model that is no longer installed, previewed and confirmed
like every other destructive operation ([database standards
§8](../../standards/database-standards.md)). `scope=before` remains for a user who wants an age
cut-off, applied deliberately rather than on a schedule. `storage.backup_retention` is unrelated and
unchanged: it rotates automatic pre-migration *backups*, not results.

**`[runtime]` is the default runtime profile, and it is sent to the provider — not merely
recorded.** Every field it sets is hashed into `runtime_profile_hash` and therefore separates
results ([ADR-0023](../../adr/0023-runtime-profile-resolution.md)); a field left unset is *not*
sent, and the served value is then resolved from what the provider reports or, failing that,
assumed. `freeweight run start --context-size` overrides it per run, and `POST /api/v1/runs` takes
the same shape as a `runtime` block. A repeat reuses the original run's stored profile rather than
re-resolving from current configuration, for the same reason it reuses the frozen `ExecutionConfig`.

`context_size` is the one that matters most on a memory-constrained machine, because the provider's
own default may be the model's advertised maximum: a 15.7B model asked for a 112 K slot allocates a
KV cache and compute buffers far larger than the weights, spills to host memory, and measures the
spill rather than the model.

Fields the provider configures at **server startup** rather than per request — Ollama's KV cache
precision and flash-attention setting among them — are deliberately **not** in `[runtime]`. A
configuration key that silently does nothing is worse than an absent one.

**`[benchmarks]` holds limits a machine decides, not a suite author.** A shipped suite's content is
fixed and hashed — that is what makes two runs of it comparable — but how far a sweep can reach
before the machine cannot serve the context is a property of the hardware.
`long_context_max_tokens` fits `native.long_context`'s doubling ladder to the ceiling: truncated
below it, extended by doubling above it, with the ceiling itself as the final rung. Raising it on a
machine that can serve more turns `effective_context_tokens` from a floor into a measurement;
lowering it on a small card keeps the suite runnable instead of failing every rung.

**It separates results**, and structurally: the effective ladder is hashed into that suite's own
`dataset_hashes`, so it reaches the reproducibility fingerprint by the same path every other
content-identity fact does. A 32 000-token sweep and a 128 000-token sweep are two measurements and
are never averaged — a sweep that stopped earlier reports a smaller effective context for reasons
that have nothing to do with the model.

**The generated configuration reference is the field-level authority.** `docs/configuration.md`
in the FreeWeight repository is produced from the settings model by
`scripts/generate_config_reference.py` — key path, environment variable, type, default, range,
runtime-changeability, security implications and an example per field — and a CI job fails when
the committed file differs from what the model generates ([Configuration Standards
§8](../../standards/configuration-standards.md)). This section is the summary; where the two
disagree, the generated document is right and this one is stale.

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
GOAL_PATH_UNSAFE          GOAL_HASH_MISMATCH        PROMPT_OVERRIDE_REFUSED
CALIBRATION_REQUIRED      CALIBRATION_INSUFFICIENT  JUDGE_UNAVAILABLE
JUDGE_SELF_JUDGING_REFUSED                          REMOTE_JUDGE_NOT_PERMITTED
COMPARISON_SUBJECT_NOT_FOUND                        COMPARISON_REFUSED
```

Five of these name refusals that the shared set cannot describe usefully, and each exists because
its remedy is specific:

| Code | Raised when | Why not a shared code |
|---|---|---|
| `GOAL_PATH_UNSAFE` | An imported pack contains a path that escapes its own directory | `VALIDATION_ERROR` would not tell the user their file is hostile |
| `GOAL_HASH_MISMATCH` | A pack's declared hash does not match its contents | Distinct from a malformed pack: this one was *modified* |
| `PROMPT_OVERRIDE_REFUSED` | A run would render an overridden prompt without `--allow-prompt-override` | The remedy is a flag; `CONFLICT` cannot suggest one |
| `COMPARISON_SUBJECT_NOT_FOUND` | A named comparison subject resolves to nothing | Names *which* subject, of several |
| `COMPARISON_REFUSED` | The subjects are separated by a fingerprint boundary | Not a missing thing — a comparison that must not be averaged |

Their HTTP statuses are in [API §11](api.md).

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
  latency tests run normally ([ADR-0027](../../adr/0027-multi-gpu-semantics.md)).
* **A goal below the agreement gate is not an error.** The run completes normally; the result is
  badged `uncalibrated` and emits no evidence. `CALIBRATION_INSUFFICIENT` is raised only when fewer
  than `calibration.min_samples` grades exist — that is, when the user has not yet done the work,
  as distinct from having done it and learned the rubric is not measurable.
* **An uncalibrated judged goal runs; it does not refuse.** A goal with rung-5 criteria and no
  calibration record at all executes normally, emits **no** evidence, and reports
  `judge_validity_factor` and the grading progress so the author can see what is missing. This
  supersedes an earlier reading of this section, which raised `CALIBRATION_REQUIRED` at run start.
  [ADR-0032 §3](../../adr/0032-judge-validity-and-user-capability-namespace.md)'s argument — that
  the diagnostic data is exactly what the user needs to fix the rubric, and it costs one GPU-bound
  run to obtain — applies *more* strongly before the first calibration than after a failed gate:
  the author has nothing at all to look at, and refusing denies them the only thing that would tell
  them what to fix. `CALIBRATION_REQUIRED` is therefore reserved for a caller that explicitly asks
  for evidence from an ungraded goal; it is not a run-start refusal.
* A jury that cannot be assembled (fewer than `judge.jury_size` eligible models, or the only
  eligible juror is the candidate itself) degrades to the largest eligible jury and records
  `jury_reduced` with the reason. A jury of zero is `JUDGE_UNAVAILABLE`; judged criteria are
  `skipped`, rule criteria still score, and the partial result says so.
* **A jury that fails during the judging phase fails that sample, not the run.** The sample is
  finished as `failed` with the reason and the phase continues: judging runs after every answer has
  been generated (§7.4), so aborting would discard a whole run's work over one unjudgeable answer.
  `judge_deferred` — the marker a criterion carries *between* the phases — is transient and never
  survives a completed run; it is a different fact from `judge_unavailable`, which is permanent.
* Rule criteria never depend on a provider. A goal whose criteria are entirely rungs 1–3 runs with
  no model judging at all, and is fully available when the provider is down.
* Cancellation is honoured at every phase and leaves consistent data.
* Full degradation matrix: [Graceful Degradation](../../architecture/graceful-degradation.md).

## 14. Security considerations

* Loopback by default; non-loopback requires tokens, the exposure acknowledgement and
  `server.allowed_hosts`. The `Host` header is validated on every request before routing
  ([ADR-0026](../../adr/0026-local-http-hardening.md)).
* **Model-generated code is never executed on the host.** Tiered sandbox, refusal at the bottom tier.
* Native tool benchmarks expose only mock tools over fixture data — no shell, no unrestricted
  filesystem, no network, no real database. A tool runs only when the case put it on the model's
  allowlist and its arguments validate against the tool's own schema; every path is proved contained
  after symlink resolution, and reads and writes have separate roots. A containment refusal names the
  path that was requested and never the path it resolved to — an error message is input to the next
  prompt exactly as a result is ([ADR-0033 §7](../../adr/0033-benchmark-interaction-protocol.md)).
* External benchmark adapters run as subprocesses with an argument list, a timeout and captured
  output; their results are parsed as untrusted input.
* Datasets are verified against pinned hashes before use; archive extraction is hardened.
* Prompts and responses are stored as hashes by default; full text only when the run explicitly
  requests it. **A scorer's own evidence is the one bounded exception**: `samples.result_json` holds
  what the scorer measured against — the matched phrase, the failing JSON path, the tool call and its
  arguments, and an excerpt of the answer capped at 200 characters — because a headline metric that
  drills only to a float is not auditable. The excerpt is a fixed cap, not the response; a tool
  result is stored as a hash and a short digest, never in full.
* Artifact paths are containment-checked; artifact files are `0600`.
* **User-authored goal content is untrusted input to FreeWeight's own renderer.** Goal templates
  render through the same `setspec.prompts` loader with `StrictUndefined` and no filesystem or
  network access in the Jinja2 environment. **User regex is guarded by the dialect, not by the
  timeout.** A pattern is linted at pack-load time — no backreferences, no unbounded repetition of a
  group — and a pattern that fails the lint is refused before it ever runs, so a
  catastrophic-backtracking pattern fails *validation* rather than the criterion. `rule_timeout_ms`
  remains as the backstop for every rule that yields, which is every rule in the library. The order
  matters, because CPython cannot deliver the other one: the regex engine holds the GIL for the
  whole match, so a worker thread running a pathological pattern cannot be interrupted by a timeout
  in the same process — measured at 3.1 s against a 50 ms budget. A specification that promised the
  timeout would catch it would be promising something no implementation can deliver.
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
[Cross-Platform Standards](../../standards/cross-platform-standards.md).

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
  ([ADR-0028](../../adr/0028-prompt-pack-granularity.md)).
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
   ([ADR-0022](../../adr/0022-capability-evidence-record-contract.md)).
7. Cold and warm measurements are never mixed in one headline number.
8. An evidence bundle exported by FreeWeight is imported by LoadCoach with no FreeWeight code or
   database access.
9. Code-execution benchmarks refuse to run when no sandbox tier is available.
10. The full test suite passes with no GPU, no Ollama and no network; coverage ≥ 85 % overall and
    ≥ 95 % in `domain/`.
11. Deleting results never deletes model or machine history, and always previews first.
12. All gold standards for FreeWeight in [Gold Standards §2](../../standards/gold-standards.md) are met.
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
  exists to fit it ([ADR-0032](../../adr/0032-judge-validity-and-user-capability-namespace.md)).
* Multi-grader goals: several people grade one calibration set, with inter-grader agreement measured
  alongside judge agreement — a house style is a shared instrument, not one person's.
