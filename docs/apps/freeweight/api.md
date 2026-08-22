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
| `GET /machines` · `GET /machines/{id}` | Static profiles; the current machine is flagged |
| `GET /models` | Filter by `provider_kind`, `family`, `quantization`, `has_results`; sort by `last_seen_at`, `canonical_id` |
| `POST /models/discover` | Re-discovers through ModelRack; returns added/updated/unchanged counts and any alias resolutions observed |
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
| `GET /benchmarks` | Installed suites with version, category, runner, requirements, dataset status |
| `GET /benchmarks/{key}` | Manifest, tests, metric definitions, prompt references, dataset hashes |

## 4. Runs

### `POST /runs`

```json
{
  "model": "ollama/qwen3.5:9b-q8_0",
  "suites": ["native.performance", "native.tool_use"],
  "tests": null,
  "runtime_profile": {"context_size": 32768, "kv_cache_precision": "f16"},
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

Notable errors: `MODEL_NOT_FOUND`, `BENCHMARK_NOT_FOUND`, `DATASET_MISSING`,
`DATASET_HASH_MISMATCH`, `PROVIDER_UNAVAILABLE`, `INSUFFICIENT_RESOURCES`, `RUN_ALREADY_RUNNING`
(when a GPU workload is active and queueing is disabled), `SANDBOX_UNAVAILABLE` (only when every
selected test requires a sandbox).

| Endpoint | Notes |
|---|---|
| `GET /runs` | Filter by `status`, `model`, `suite`, `machine`, `label`, date range; cursor pagination |
| `GET /runs/{id}` | Run with tests, aggregate metrics, degradations and the fingerprint document |
| `POST /runs/{id}/cancel` | 202 when accepted; 409 `RUN_NOT_CANCELLABLE` for terminal runs |
| `POST /runs/{id}/repeat` | Creates a new run with the identical effective config; `?check=true` diffs afterwards; refuses with a reason when the environment cannot satisfy it, unless `force=true` |
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
| `GET /results/export` | `?format=json|jsonl|csv&scope=run|model|suite|comparison|all&include_samples=…&include_prompts=…` — streams; SetSpec-wrapped for JSON/JSONL |

The compare endpoint never averages across a boundary marked "separate"; it returns the groups and
the field-level fingerprint diff that separates them.

## 6. Evidence (the LoadCoach integration point)

| Endpoint | Notes |
|---|---|
| `GET /evidence` | Current `capability.evidence` records; filter by capability, model, machine, runtime profile, minimum confidence. A **collection** envelope (`items`/`page`) whose items are SetSpec envelopes |
| `GET /evidence/export` | A complete `benchmark.evidence_bundle` (SetSpec-versioned), optionally filtered; the file form of the same data. A **single** SetSpec envelope, with no collection wrapper |

The two envelopes compose in exactly that order and never the reverse
([ADR-0025 §2](../../adr/0025-envelope-boundaries.md)). Consumers check `schema_version` and reject
unsupported majors. These endpoints are **read-only** and require only the `read` scope when
authentication is enabled.

### `GET /evidence/export` parameters

| Parameter | Meaning |
|---|---|
| `since` | RFC 3339. Returns evidence whose **`computed_at`** is later, on FreeWeight's clock. A client never supplies its own clock: it sends back the `generated_at` of the bundle it received last time, which makes the comparison single-clock and correct across machines |
| `capability`, `model`, `machine`, `runtime_profile`, `min_confidence` | The same filters as `GET /evidence` |

Every bundle declares `complete: true|false`. `since` produces an incremental bundle
(`complete: false`), which can add and update evidence but can never tell a consumer that something
was removed. A consumer observes removals only from a complete bundle, and marks locally-held evidence
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
5. Treat FreeWeight being unreachable as **degraded**: keep the last import, mark it stale, and say so
   in every routing explanation.
