# FreeWeight — Risk and Failure Analysis

Risks specific to FreeWeight. Suite-wide risks are in the
[Risk Register](../../architecture/risk-register.md). Each entry names its likelihood, impact,
mitigation and the signal that tells us it is happening.

---

## 1. Technical risks

| # | Risk | L | I | Mitigation | Early signal |
|---|---|---|---|---|---|
| T1 | **Measurement contamination** — other GPU/CPU work runs during a benchmark and corrupts performance and memory numbers | High | High | One GPU workload at a time; optional idle detection before measuring; telemetry recorded with the run so contamination is visible afterwards; measurement class (`cold`/`warm`) labelled | Wide dispersion across repetitions; telemetry showing utilization before the run starts |
| T2 | **Telemetry sampling distorts the measurement it documents** | Medium | Medium | Overhead budget ≤ 1 %; a calibration test (sampling on vs off) whose result is stored on every run | Calibration delta above budget |
| T3 | **Provider timing semantics misunderstood** — treating chunk latency as token latency, or backend durations as client-observed | Medium | High | Backend and client timings stored separately and never merged; `token_level_chunks` gates the per-token claim; both are shown in the UI | A model reporting implausible tokens/second |
| T4 | **Aggregates diverge from raw data** | Medium | High | The anti-lie test: every dashboard figure is recomputed from raw samples in a test; raw samples always retained | A dashboard number that cannot be reproduced from the case inspector |
| T5 | **Scoring bugs produce confident wrong numbers** | Medium | High | Every formula unit-tested with known values, boundaries and `UNSUPPORTED` inputs; deterministic scoring preferred; scorers reviewed as domain logic | A metric that is suspiciously stable across very different models |
| T6 | **LLM-judge instability** makes judged suites unreproducible | High | Medium | Deterministic scoring wherever possible; repeated trials with agreement measurement; order randomization; judge bias measured and displayed with every judged score | Low repetition agreement; high position bias for the configured judge |
| T7 | **VRAM slope noise** makes KV-cache measurement unreliable | Medium | Medium | Idle detection; stabilized readings before generation; fit quality reported alongside the slope; `unsupported` when telemetry is unavailable | Poor fit quality; slope varying between repeats |
| T8 | **Long runs die** (OOM, driver reset, power) and lose hours of work | Medium | High | Per-sample durability; `interrupted` state; resume from the last completed test; events persisted | Frequent `interrupted` runs |
| T9 | **Database growth** — millions of samples and telemetry rows slow the dashboard | Medium | Medium | Indexes from the first migration; query-plan assertions in tests; configurable telemetry retention; vacuum tooling; PostgreSQL as the escape valve | Dashboard queries exceeding budget at realistic volume |
| T10 | **Fingerprint over-sensitivity** — every run looks incomparable | Medium | Medium | Deliberate exclusions (driver, storage) from the machine fingerprint; drift handled as confidence reduction rather than separation; field-level diff shown | Users unable to compare anything they measured |

## 2. Integration risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| I1 | **Ollama API changes** break discovery, metadata or timings | Medium | High | ModelRack owns the adapter; name-based parsing; `UNSUPPORTED` for missing fields; version-annotated fixtures; live tests catch drift nightly |
| I2 | **Evidence contract mismatch** with LoadCoach | Low | High | SetSpec schemas with goldens; contract tests in both repositories; a consumer harness that imports only `setspec` |
| I3 | **External benchmark CLIs change** | High | Medium | Pinned versions and dataset hashes; recorded output fixtures; adapters fail loudly with the version they expected |
| I4 | **Shared package churn** (BaseAiCore/ModelRack breaking changes) | Medium | Medium | Compatible version ranges; nightly compatibility matrix; pre-1.0 changes coordinated in the PR description |
| I5 | **Descriptor refresh rewrites history** | Low | High | Descriptor snapshots are immutable rows; runs reference the snapshot they used |

## 3. Security risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| S1 | **Generated code executed unsandboxed** | Low | Critical | Tiered sandbox with refusal at the bottom; no host-execution path exists; a test asserts refusal |
| S2 | **Mock tools escape their fixtures** | Low | High | `contained_path` on every access; adversarial-argument tests (`../`, absolute, symlink) |
| S3 | **Malicious benchmark dataset** | Low | High | Pinned hashes verified before use; hardened archive extraction; extraction into a temporary directory |
| S4 | **Accidental LAN exposure** | Medium | High | Loopback default; bind + token + acknowledgement required; startup refusal |
| S5 | **Prompt/response content leaking into logs or exports** | Medium | Medium | Hashes by default; content storage opt-in per run; exports state what they include; redaction filter |
| S6 | **Destructive deletion by mistake** | Medium | Medium | Preview token required, confirmation, transaction, auto-backup; models and machines protected by `RESTRICT` |

## 4. Portability risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| P1 | Windows/macOS users find telemetry-dependent benchmarks unavailable | High | Low | Documented per platform; benchmarks **skipped with a reason**, never silently wrong; `doctor` explains |
| P2 | Non-NVIDIA GPUs unsupported at 1.0 | High | Medium | `GpuReader` interface ready; degradation is explicit; AMD listed as a future extension |
| P3 | No container runtime on the reference machine | Certain | Medium | bwrap tier; refusal below it; documented recommendation |

## 5. Performance risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| F1 | Per-sample overhead grows as features are added | Medium | Medium | Budget asserted in a performance test on every phase that touches the path |
| F2 | Export of a large run exhausts memory | Medium | Medium | Streaming exports; JSONL; artifact references instead of inline blobs |
| F3 | SSE fan-out under many dashboard tabs | Low | Medium | Bounded queues; drop-and-replay; multiplexed telemetry stream |

## 6. Model and provider risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| M1 | **Benchmark contamination** — models trained on public benchmark data | High | High | Native suites are FreeWeight-authored and unpublished; contamination noted per external suite; LiveBench-style refreshed sets listed as a future extension; users told plainly which suites are public |
| M2 | Models refuse or moralize instead of performing a task | Medium | Medium | Refusals classified as a distinct outcome, never scored as incapacity |
| M3 | Provider silently truncates output at a default limit | Medium | High | `finish_reason` recorded on every sample; length-truncated samples flagged and excluded from quality aggregates |
| M4 | Retagged models invalidate history | Medium | Medium | Digest identity; alias resolutions recorded; `name_only` results flagged permanently |

## 7. Migration and maintenance risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| G1 | Metric definitions change and silently reinterpret old data | Medium | High | A changed definition gets a **new metric key**; old keys retained |
| G2 | Benchmark suite edits invalidate comparisons | High | Medium | Suite versioning; results separated by version; a prompt change forces a suite bump |
| G3 | Adoption of WeightsDB/MirrorWall regresses behaviour | Medium | Medium | The unchanged test suite is the acceptance criterion for Phase 12 |
| G4 | Benchmark catalogue outgrows one person's ability to maintain it | Medium | Medium | Manifest-driven design; external suites isolated; native suites deliberately small and deterministic |

---

## 8. Deliberate trade-offs

* **One GPU workload at a time** — throughput sacrificed for measurement validity.
* **Raw samples retained forever by default** — disk used generously so provenance is never lost.
* **Deterministic scoring preferred** — some qualities (prose quality) are measured less precisely
  rather than measured badly by a judge.
* **No universal score** — harder to skim, honest about what was measured.
* **Sandbox refusal over host execution** — some benchmarks are unavailable rather than risky.
* **Provenance-heavy records** — more storage and more required fields, in exchange for reproducible
  comparison.
* **Local only** — no shared leaderboard, no cross-machine ranking, no upload.

## 9. Explicit non-goals (restated as risk control)

Routing, production orchestration, content workflows, training/fine-tuning, publishing a leaderboard,
scoring models on someone else's hardware. Each of these has been requested of benchmark tools before
and each would compromise FreeWeight's single responsibility.

## 10. Premature optimizations to avoid

* Caching aggregates before a query-plan test shows a real problem.
* A background worker pool before one scheduler thread is proven insufficient.
* A plugin system for benchmarks before the third external adapter exists.
* A custom time-series store for telemetry before SQLite is measured as inadequate.
* Client-side rendering of the dashboard before server-rendered pages miss their budget.

## 11. Architectural traps

* Letting a benchmark suite reach into the database directly instead of returning samples.
* Adding a "task profile" concept — that is LoadCoach's, and its arrival here would be the first step
  toward FreeWeight becoming a router.
* Storing a routing score on a model row.
* Letting the UI compute a metric the domain does not define.
* Allowing external adapter code to be imported rather than subprocessed.
