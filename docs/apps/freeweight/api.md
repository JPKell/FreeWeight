# FreeWeight — Public API

**Base path:** `/api/v1` · **Conventions:** [API and Contract Standards](../../standards/api-and-contract-standards.md)
**Generated documentation:** `/api/v1/openapi.json`, `/api/v1/docs` (loopback only by default).

Everything here is additive within v1. The committed OpenAPI snapshot is diff-checked in CI.

---

## 1. System

| Endpoint | Purpose |
|---|---|
| `GET /health` | Component health (`database`, `provider`, `gpu_telemetry`, `sandbox`, `external_benchmarks`, `prompts`) |
| `GET /version` | Application version, API versions, SetSpec schema versions. **Never authenticated** ([ADR-0026 §5](../../adr/0026-local-http-hardening.md)) |
| `GET /system/status` | Active run, queue depth, telemetry snapshot, threadpool saturation, disk headroom |
| `GET /system/telemetry/stream` | SSE — `telemetry.sampled` events at the configured interval |

## 2. Machines and models

| Endpoint | Notes |
|---|---|
| `GET /machines` · `GET /machines/{id}` | Static profiles; the current machine is flagged. **Never writes** — machines are recorded when a run is created, so polling this cannot make one look freshly used, and the list is legitimately empty before anything has been measured |
| `GET /models` | Filter by `provider_kind`, `family`, `quantization`, `has_results`; sort by `last_seen_at`, `canonical_id` |
| `POST /models/discover` | Re-discovers through ModelRack; returns added/updated/unchanged/total counts. The counts, not the models: a client that wants the list asks for it, and a discovery that returned every model would bury *what changed* |
| `GET /models/{model_ref}` | Identity, latest descriptor, descriptor history, evidence summary |
| `GET /models/{model_ref}/results` | Paginated results for this model, filterable by suite and runtime profile |
| `GET /models?canonical_id=…` | Lookup by identity; `?provider_kind=&provider_model_name=&artifact_digest=` is the exact-triple form |

`model_ref` is the application-local ULID, or an unambiguous prefix of one; an ambiguous prefix
returns 400 listing the candidates. **The canonical ID is never a path segment** — it contains `/`,
`:` and `@`, and a percent-encoded `/` does not survive common reverse proxies
([ADR-0024](../../adr/0024-canonical-id-and-model-references.md)). Request bodies and CLI arguments
still accept a canonical ID, a bare name or an unambiguous prefix.

## 3. Benchmarks

| Endpoint | Notes |
|---|---|
| `GET /benchmarks` | Installed suites with version, category, runner, requirements, dataset hashes, `headline_metric` and test count. Read from the registry the **run engine executes from**, so a suite listed here is one `POST /runs` accepts — a listing assembled separately would eventually disagree |
| `GET /benchmarks/{key}` | Manifest, tests, metric definitions, prompt references, dataset hashes. `404` names what *is* installed |

## 3a. Goals (user-authored suites)

Full contract: [Subjective Goals](subjective-goals.md).
Decisions: [ADR-0031](../../adr/0031-user-defined-goal-benchmarks.md),
[ADR-0032](../../adr/0032-judge-validity-and-user-capability-namespace.md).

| Endpoint | Notes |
|---|---|
| `GET /goals` | Goals with `goal_hash`, `score_method_mix`, calibration state (`uncalibrated` \| `insufficient` \| `calibrated`), `kappa_w`, `n_holdout`, calibration age, `unforked` |
| `POST /goals` | Create from a goal-pack body. Validates and lints before writing; a lint finding never blocks creation, it is returned |
| `GET /goals/{slug}` | The full pack as loaded, plus lint findings and the current calibration report |
| `PUT /goals/{slug}` | Replace. Returns the **old and new `goal_hash`** and, when they differ, the count of existing runs the change separates — before the change is committed |
| `DELETE /goals/{slug}` | Previewed like every destructive operation; the preview states how many runs it orphans and how many of the user's grades it destroys |
| `POST /goals/{slug}/validate` | Schema, weights, scale descriptors, rule dialect, template rendering. Returns findings with severity |
| `POST /goals/{slug}/suggest-rules` | Given criteria (and calibration samples where present), proposes rung-2 rules with pre-filled parameters. **Proposals only** — never applied automatically |
| `GET /goals/{slug}/tasks` | The task set, flagged `is_starter` |
| `GET /goals/{slug}/calibration` | Samples with partition, grade progress, and what remains to be graded |
| `POST /goals/{slug}/calibration/samples` | Add samples: generate over a model spread, paste text, or promote prior run samples |
| `POST /goals/{slug}/calibration/grades` | Submit grades. Idempotent per `(sample, criterion)`; partial submission is normal and progress survives interruption |
| `POST /goals/{slug}/calibration/run` | Score the **holdout** with the configured jury and compute agreement. Returns a run id; progress streams over the run event SSE like any other run |
| `GET /goals/{slug}/calibration/report` | `kappa_w`, `rho`, `mae`, `bias`, `n_anchor`, `n_holdout`, inter-juror alpha, per criterion and weighted; gate verdict; `judge_validity_factor`; the worst-diverging holdout samples with both rationales |
| `GET /goals/{slug}/export` | `benchmark.goal_pack` — a single SetSpec envelope describing the pack. **Not** the round-trip format: `POST /goals/import` reads the *bundle* that `freeweight goals export` writes ([spec §7.3](spec.md)) |
| `POST /goals/import` | Import a pack. Size-capped, containment-checked, schema-validated, hash-verified before any write; **never overwrites in place** — a colliding slug is rejected with the existing `goal_hash` named |
| `GET /goals/starters` | The four shipped starter packs with their approximate deterministic weight |
| `POST /goals/starters/{key}/fork` | Copy a starter to a new slug. The copy is `unforked` until its criteria or tasks are edited |
| `GET /judges` | Models eligible to serve as jurors, each with its own `native.judge` bias results and eligibility reasons |
| `POST /judges/validate` | Dry-run a jury configuration: assembly, self-judging conflicts, remote permission, structured-output capability |

Two behaviours worth stating at the API level, because a client will otherwise get them wrong:

* **A goal below the gate is a `200`, not an error.** The run completes, results are returned in
  full, `calibration_state` is `"uncalibrated"`, and `GET /evidence` simply contains nothing for that
  capability. `CALIBRATION_INSUFFICIENT` is a `409` and means something different: fewer than
  `min_samples` grades exist, so agreement has never been measured at all.
* **`PUT /goals/{slug}` is a separating change when `goal_hash` moves.** The response says so with a
  count, and a client that applies the change without surfacing it will silently fragment a user's
  measurement history.

## 4. Runs

### `POST /runs`

```json
{
  "model": "ollama/qwen3.5:9b-q8_0",
  "suites": ["native.performance", "native.tool_use"],
  "tests": null,
  "runtime": {"context_size": 32768},
  "gpu_index": 0,
  "execution": {"measured_repetitions": 3, "warmup_repetitions": 1,
                "test_timeout_seconds": 600, "seed": 42, "store_prompts": false},
  "sampling": {"temperature": 0.0, "top_p": 1.0, "max_output_tokens": 1024},
  "label": "q8 baseline"
}
```

Response `201` with the run object, including `reproducibility_fingerprint` and the resolved
`effective_config`. Validation happens before the run is persisted, so a rejected request creates
nothing.

`runtime` overrides the `[runtime]` configuration section **for this run**, field by field — the
fields it omits keep their configured values. It accepts `context_size`, `gpu_layers`, `threads`,
`batch_size` and `keep_alive`; an unrecognised key is a `VALIDATION_ERROR` naming it rather than a
silently ignored one, because a runtime setting that is accepted and not applied produces a run
whose record describes conditions it was never served under. Every field set here is hashed into
`runtime_profile_hash` and therefore separates results
([ADR-0023](../../adr/0023-runtime-profile-resolution.md)).

`POST /runs/{id}/repeat` reuses the **original run's stored profile**, not the current
configuration.

Notable errors: `MODEL_NOT_FOUND`, `BENCHMARK_NOT_FOUND`, `DATASET_MISSING`,
`DATASET_HASH_MISMATCH`, `PROVIDER_UNAVAILABLE`, `INSUFFICIENT_RESOURCES`, `RUN_ALREADY_RUNNING`
(when a GPU workload is active and queueing is disabled), `SANDBOX_UNAVAILABLE` (only when every
selected test requires a sandbox).

| Endpoint | Notes |
|---|---|
| `GET /runs` | Filter by `status`, `model`, `suite`, `machine`, `label`, date range; cursor pagination |
| `GET /runs/{id}` | Run with tests, aggregate metrics, degradations and the fingerprint document. A metric row names its key `metric_key`, as every other surface does (§11) |
| `POST /runs/{id}/cancel` | 202 when accepted; 409 `RUN_NOT_CANCELLABLE` for terminal runs |
| `POST /runs/{id}/repeat` | Creates a new run with the identical effective config, reusing the original's frozen `ExecutionConfig` and runtime profile rather than re-resolving them; `?force=true` proceeds past a blocker and records the divergence; `?label=` names the new run |
| `GET /runs/{id}/events` | SSE with `Last-Event-ID` replay |
| `GET /runs/{id}/tests` · `GET /runs/{id}/tests/{test_id}/samples` | Drill-down; samples are cursor-paginated |

### Run events

```text
run.started        test.started      sample.started      telemetry.sampled
run.progress       test.progress     sample.completed    run.degraded
run.completed      test.completed    sample.failed       run.cancelled
run.failed         test.skipped                          run.interrupted
```

## 5. Results and comparison

| Endpoint | Notes |
|---|---|
| `GET /results` | Metric-level query: filter by model, suite, metric key, machine, runtime profile, date |
| `GET /results/compare` | `?subjects=a,b,c&suite=…` — aligned metrics with comparability verdicts and, where a comparison is not permitted, the reason |
| `GET /results/export` | `?format=json|jsonl|csv&scope=run|model|suite|comparison|all&include_samples=…&include_prompts=…&include_prompt_text=…&since=…&until=…` — streams; JSON/JSONL are wrapped in a `freeweight.export` envelope (§12) |

The compare endpoint never averages across a boundary marked "separate"; it returns the groups and
the field-level fingerprint diff that separates them.

**A comparison of one model at three or more served contexts carries a `context_sweep`.** Not
requested — derived: a user who has run the same model at several `context_size` values has already
produced the measurement, and this is the surface that notices. It differences each run's
`model_vram_bytes` into a cost function, `weights_bytes + bytes_per_token × context`, with the `r²`
of the fit beside it so a sweep taken on a busy GPU shows rather than quietly biasing the slope.
`null` for every other comparison, which is the ordinary case.

This is a **study across runs**, not a benchmark result, and it cannot be either the other way
round: `size_vram` scales with the context a model was *loaded* at, so a sweep of prompt lengths
inside one run measures KV fill rather than KV cost, and a benchmark is one run under one profile
([ADR-0034 §6](../../adr/0034-run-level-derived-metrics.md)).

`subjects` accepts **either** a run reference or a model reference. A run subject is guarded by
`suite`: naming a run of a different suite is refused by name. A *model* subject requires `suite`,
and resolves to that model's latest completed run of it — naming a model with no `suite` is
`VALIDATION_ERROR`, because "latest" would otherwise mean something different per subject.

`GET /results/export` refuses a selection wider than **500 runs** rather than truncating one. A
truncated export that did not say it was truncated would be a lie about what was measured, and the
document has no pagination because it is a document, not a page. The refusal names the count and
points at the window.

**`since` and `until` bound the export by run creation time, and the window is half-open** —
`[since, until)`. That is what makes windowing *complete* rather than merely smaller: a run created
exactly at the boundary belongs to the window that starts there and not to the one that ends there,
so consecutive windows tile without duplicating a run or dropping one between them. A history
larger than one document is therefore exported as several that reassemble exactly. Every document
states the window it covers, so a reader can tell a slice from a whole.

**`include_prompts=true` exports prompt *identity*** — each sample's `prompt_id`,
`prompt_version`, `prompt_hash` and `rendered_prompt_hash`. That is the right default: a database of
measurements should not become a second copy of the prompt pack, and [prompt standards
§4](../../standards/prompt-management-standards.md) makes the identity sufficient to re-render —
*on the machine that has the pack*. A reader elsewhere does not, which is the difference between an
export that is auditable and one that is merely referential.

**`include_prompt_text=true` closes that gap** with a **prompt appendix**: each distinct rendered
prompt once, keyed by its `rendered_prompt_hash`, under `payload.prompt_appendix`. Cheap, because
prompts repeat across thousands of samples. It is built by **re-rendering** from the installed
suites rather than by reading stored text — prompt text is not stored — so it also *verifies*: a
prompt offered under a given hash is one whose current text produces that hash. A prompt edited
since the run simply does not appear, and the reader gets no text rather than the wrong text.

## 6. Evidence (the LoadCoach integration point)

| Endpoint | Notes |
|---|---|
| `GET /evidence` | Current `capability.evidence` records; filter by capability, model, machine, runtime profile, minimum confidence. A **collection** envelope (`items`/`page`) whose items are SetSpec envelopes. `user.*` records carry `goal_hash`, `score_method_mix`, `judge_set`, `calibration` and `judge_validity_factor` ([ADR-0032 §5](../../adr/0032-judge-validity-and-user-capability-namespace.md)) |
| `GET /evidence/export` | A complete `benchmark.evidence_bundle` (SetSpec-versioned), optionally filtered; the file form of the same data. A **single** SetSpec envelope, with no collection wrapper |

The two envelopes compose in exactly that order and never the reverse
([ADR-0025 §2](../../adr/0025-envelope-boundaries.md)). Consumers check `schema_version` and reject
unsupported majors. These endpoints are **read-only** and require only the `read` scope when
authentication is enabled.

### `GET /evidence` parameters

| Parameter | Meaning |
|---|---|
| `capability` | Exact capability ID, e.g. `tool_use` or `user.house_voice` |
| `model` | Model canonical ID, ULID or unambiguous prefix — the same four forms every surface accepts |
| `machine` | Machine fingerprint |
| `runtime_profile` | Runtime profile hash |
| `min_confidence` | Records at or above this confidence |
| `limit`, `cursor` | Cursor pagination over the total order `(capability_id, id)`; `limit` defaults to 50 and clamps to 500 |

A capability with no evidence is **absent** from the collection, never present with a score of
zero, and a goal below its calibration gate has no record at all
([ADR-0032 §3](../../adr/0032-judge-validity-and-user-capability-namespace.md)). The page form,
`/evidence`, shows the same records with ADR-0017's staleness badge and, one interaction away, the
six confidence factors and the contributing metrics that explain each score.

### `GET /evidence/export` parameters

| Parameter | Meaning |
|---|---|
| `since` | RFC 3339. Returns evidence whose **`computed_at`** is later, on FreeWeight's clock. A client never supplies its own clock: it sends back the `generated_at` of the bundle it received last time, which makes the comparison single-clock and correct across machines |
| `capability`, `model`, `machine`, `runtime_profile`, `min_confidence` | The same filters as `GET /evidence` |

Every bundle declares `complete: true|false`. `since` — or any filter — produces an incremental
bundle (`complete: false`), which can add and update evidence but can never tell a consumer that
something was removed; only a bundle nothing narrowed is complete. A consumer observes removals only from a complete bundle, and marks locally-held evidence
absent from one as `superseded` rather than deleting it
([ADR-0022 §5](../../adr/0022-capability-evidence-record-contract.md)). A consumer that has never
imported from this source pulls complete.

## 7. Database management

| Endpoint | Notes |
|---|---|
| `GET /database/stats` | Row counts, size, revision, last backup, integrity status |
| `POST /database/delete-preview` | Body describes the selection; returns exactly what would be removed, by table |
| `DELETE /database/results` | Requires the preview token returned above; transactional; auto-backup above threshold |
| `POST /database/backup` · `POST /database/vacuum` | Both return an outcome record |

Models and machines are never removed by a result deletion.

## 8. Settings

| Endpoint | Notes |
|---|---|
| `GET /settings` · `PUT /settings` | Runtime-changeable settings only. Attempts to change a security-relevant setting return 403 `FORBIDDEN` naming the config-only key |

**"Applies to work started from now on" is exact, and narrower than it sounds.** A stored value is
read when the application builds the sampler and the scheduler, so it is in force from the next
start. It does **not** re-interval a telemetry sampler that is already running, and it does not
re-read execution defaults between two runs of a serving process.

That is the safe direction to be wrong in: changing a measurement's conditions while it is being
measured is worse than a setting that takes effect later. A run's effective configuration is frozen
at creation and recorded, so a reader can always see which values a given run was measured under —
including the sampler interval.

## 9. Authentication

Loopback with no configured tokens: open. Otherwise `Authorization: Bearer <token>` with scopes
`read` / `write` / `admin` ([ADR-0014](../../adr/0014-authentication-strategy.md)). Read-only
endpoints — including `/evidence` — need only `read`.

## 10. Client guidance for LoadCoach

1. `GET /api/v1/version` (no credential needed); verify the API major and the
   `benchmark.evidence_bundle` schema versions.
2. `GET /api/v1/evidence/export?since=<the previous bundle's `generated_at`>` for an incremental
   bundle. Never send your own clock. Pull complete on first contact and whenever you need to observe
   removals.
3. Validate the envelope; reject an unsupported major with both versions named.
4. Store evidence keyed by measurement subject — identity, `runtime_profile_hash`,
   `machine_fingerprint`, capability and `policy_version` — and never merge across differing benchmark
   versions, dataset hashes or prompt subset hashes.
4a. Evidence for a model you have not discovered is normal, not an error: retain it, mark it
   unmatched, and bind it when discovery produces a match
   ([ADR-0022 §4](../../adr/0022-capability-evidence-record-contract.md)).
4b. Take freshness from `measured_at`, never from `computed_at`.
4c. Use evidence only for an execution whose resolved runtime profile hash matches the evidence's
   ([ADR-0023](../../adr/0023-runtime-profile-resolution.md)).
4d. Never merge across differing `goal_hash` or judge set identity — both are hard separations
   ([ADR-0032 §4](../../adr/0032-judge-validity-and-user-capability-namespace.md)).
4e. **`user.*` capabilities are opt-in.** Do not weight one unless a task profile names it
   explicitly. A capability that one person's taste defines must not acquire routing influence
   merely by existing.
4f. When a routing decision used a `user.*` capability, the explanation names the goal, its
   agreement (`kappa_w`) and `n_holdout` — in words, not just a confidence number. "Chose qwen3:14b
   partly on `user.house_voice` 0.74, judge agreement 0.71 over 6 samples you graded on 2026-08-14"
   is auditable; "confidence 0.31" is not.
5. Treat FreeWeight being unreachable as **degraded**: keep the last import, mark it stale, and say so
   in every routing explanation.

## 11. Error codes and HTTP statuses

Every non-2xx response uses the one error envelope from
[API Standards §4](../../standards/api-and-contract-standards.md) — `{"error": {"code", "message",
"details", "request_id", "timestamp"}}` — never wrapped in a SetSpec envelope, because an error
describes one request rather than a document that outlives it
([ADR-0025](../../adr/0025-envelope-boundaries.md)).

The codes are listed in [spec §13](spec.md); this is the status each one carries.

| Status | Codes |
|---|---|
| 400 | `VALIDATION_ERROR`, `SCHEMA_VERSION_UNSUPPORTED`, `GOAL_INVALID`, `GOAL_PACK_INCOMPATIBLE`, `GOAL_PATH_UNSAFE`, `GOAL_HASH_MISMATCH`, `COMPARISON_REFUSED` |
| 401 | `UNAUTHENTICATED` |
| 403 | `FORBIDDEN`, `REMOTE_JUDGE_NOT_PERMITTED` |
| 404 | `NOT_FOUND`, `MODEL_NOT_FOUND`, `RUN_NOT_FOUND`, `BENCHMARK_NOT_FOUND`, `GOAL_NOT_FOUND`, `COMPARISON_SUBJECT_NOT_FOUND` |
| 405 | `METHOD_NOT_ALLOWED` |
| 409 | `CONFLICT`, `RUN_NOT_CANCELLABLE`, `RUN_ALREADY_RUNNING`, `CALIBRATION_REQUIRED`, `CALIBRATION_INSUFFICIENT`, `JUDGE_SELF_JUDGING_REFUSED`, `PROMPT_OVERRIDE_REFUSED` |
| 413 | `PAYLOAD_TOO_LARGE` |
| 415 | `UNSUPPORTED_MEDIA_TYPE` |
| 421 | `MISDIRECTED_REQUEST` |
| 500 | `CONFIGURATION_ERROR`, `INTERNAL_ERROR` |
| 502 | `PROVIDER_PROTOCOL_ERROR` |
| 503 | `DEPENDENCY_UNAVAILABLE`, `PROVIDER_UNAVAILABLE`, `JUDGE_UNAVAILABLE` |
| 504 | `PROVIDER_TIMEOUT` |

Three of these are worth a client's attention because the obvious reading is wrong:

* **`403 FORBIDDEN` from `PUT /settings`** is not an authentication failure. It means the key is
  config-only — a security-relevant setting that a running process may not change — and the response
  names the key. Re-authenticating will not help; editing the configuration file will.
* **`409 CALIBRATION_INSUFFICIENT` is not the gate.** It means fewer than `min_samples` grades exist,
  so agreement has never been measured. A goal that *failed* the gate returns `200`.
* **`503 JUDGE_UNAVAILABLE` is a run-level outcome, not an outage.** No jury could be assembled;
  rule criteria still scored and the partial result says so.

## 11a. One name per concept

A metric value's key is **`metric_key`** on every surface that reports one — `GET /results`,
`GET /results/export`, `GET /runs/{id}`, `GET /models/{ref}/results` and `freeweight run show
--json`. It was briefly `key` on two of them, which is exactly the drift CLAUDE.md's "same concept,
same name" rule exists to prevent; a contract test now fails the build if a surface spells it the
old way.

A benchmark **manifest** spells it the same way: `metrics: [{"metric_key": …, "unit": …}]`
([benchmark catalogue](benchmark-catalog.md) §5). Declaring a metric and reporting a value for one
are different acts, and they name the thing identically — so a reader moving between a manifest and
a result never has to translate.

## 12. Exported document schemas

| Schema | Owner | Emitted by |
|---|---|---|
| `benchmark.result`, `benchmark.run_summary` | SetSpec | Embedded in exports and evidence |
| `capability.evidence`, `benchmark.evidence_bundle` | SetSpec | `GET /evidence`, `GET /evidence/export` |
| `benchmark.goal_pack`, `benchmark.calibration_report` | SetSpec | `GET /goals/{slug}/export`, `GET /goals/{slug}/calibration/report` |
| **`freeweight.export`** | **FreeWeight** | `GET /results/export`, `freeweight results export` |

`freeweight.export` is an **application-owned document**, in FreeWeight's own namespace because
SetSpec does not describe it and must not have to: its shape follows this endpoint's query model
(`scope`, `include_samples`, `include_prompts`), and no shared schema can carry keyed metric rows or
raw samples ([ADR-0035](../../adr/0035-application-owned-document-schemas.md)). It **embeds** a real
`benchmark.run_summary` per run under `summary`, so the shared contract is exercised rather than
paraphrased.

It is not a cross-application contract. A consumer integrating with FreeWeight uses the evidence
bundle (§6, §10), which is versioned for exactly that purpose.
