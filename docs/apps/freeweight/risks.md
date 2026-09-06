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
| T6a | **A judged goal score measures the wrong thing** — the jury is consistent, fast and confidently scoring a criterion it understands differently from the user | High | High | Calibration against the user's own grades is mandatory before any judged criterion contributes evidence; `kappa_w` reported with `n_holdout` beside every number; the gate withholds evidence below 0.40; `judge_validity_factor` caps confidence ([ADR-0031](../../adr/0031-user-defined-goal-benchmarks.md), [ADR-0032](../../adr/0032-judge-validity-and-user-capability-namespace.md)) | High inter-juror agreement with low judge-user agreement — the jury agreeing with itself and not with you |
| T6b | **Calibration statistics are subtly wrong** and produce plausible agreement figures for months | Medium | High | Hand-computed confusion-matrix fixtures; published worked examples; synthetic graders whose true agreement is known by construction (perfect, random, uniformly generous); three statistics that must be able to disagree with each other | `kappa_w` that never approaches 0 or 1 across very different graders |
| T6c | **Holdout leakage** — calibration samples reach the judge prompt, making agreement self-congratulatory | Low | High | The anchor/holdout partition is seeded and recorded; a test scans rendered judge prompts for holdout content hashes rather than reading the code | Implausibly high `kappa_w` on a first calibration |
| T6d | **User-authored content as attack surface** — Jinja2 templates, regex, oversized packs | Medium | Medium | `StrictUndefined` sandbox with no filesystem or network in the environment; linted regex dialect under `rule_timeout_ms`; size caps and containment checks before any write on import | A goal run stalling in a rule scorer |
| T7 | **VRAM slope noise** makes KV-cache measurement unreliable | Medium | Medium | Idle detection; stabilized readings before generation; fit quality reported alongside the slope; `unsupported` when telemetry is unavailable | Poor fit quality; slope varying between repeats |
| T8 | **Long runs die** (OOM, driver reset, power) and lose hours of work | Medium | High | Per-sample durability; `interrupted` state; resume from the last completed test; events persisted | Frequent `interrupted` runs |
| T9 | **Database growth** — millions of samples and telemetry rows slow the dashboard | Medium | Medium | Indexes from the first migration; query-plan assertions in tests; configurable telemetry retention; vacuum tooling; PostgreSQL as the escape valve | Dashboard queries exceeding budget at realistic volume |
| T10 | **Fingerprint over-sensitivity** — every run looks incomparable | Medium | Medium | Deliberate exclusions (driver, storage) from the machine fingerprint; drift handled as confidence reduction rather than separation; field-level diff shown | Users unable to compare anything they measured |
| T11 | **Catastrophic forgetting goes unmeasured** — a LoRA trained for one thing has silently lost another, and the panel that would have caught it was built from what the adapter *claims* | High | High | The panel is declared + a **fixed regression panel** + performance, never declared alone ([catalogue §8](benchmark-catalog.md), [ADR-0059](../../adr/0059-adapter-evidence-is-measured-never-inherited.md)); the regression panel is fixed in the catalogue and versioned with it, not configurable, so two adapters' regression numbers are comparable | An adapter scoring well on every declared capability and below its base on `instruction_following` or `structured_output` |
| T12 | **Adapter evidence attributed to the base** — a join, an inheritance rule or a convenience default lets a measurement taken under a LoRA raise the score of weights nobody measured | Medium | **Critical** | No inheritance in any form, asserted rather than documented ([ADR-0059](../../adr/0059-adapter-evidence-is-measured-never-inherited.md), [ADR-0058 §4](../../adr/0058-the-execution-subject-gains-an-adapter-axis.md)); an unmeasured subject reads `—`, never a number ([ADR-0016](../../adr/0016-unavailable-is-not-zero.md)); the consumer refuses the mis-binding independently, so both ends have to fail before it happens | A subject with no runs showing a score; a base's score moving after an adapter was measured |
| T13 | **Adapter applied to the wrong base** — a manifest names its base by name only, the name matches something else on this machine, and the output is plausible, confident and wrong | Medium | High | Compatibility decided by **digest**, and a mismatch is a refusal rather than an attempt (`verify_adapter_base_compatibility`, fails closed); a name-only base is flagged `NAME_ONLY` everywhere the subject surfaces, including in the exported evidence's confidence | An adapter enumerating against a base whose digest its manifest never recorded |

**What would change the regression panel.** T11's mitigation is a *fixed* panel, and fixing it is a
bet that three suites are enough. The bet is revisited when there is measured forgetting data to
revisit it with — which is [ADR-0059](../../adr/0059-adapter-evidence-is-measured-never-inherited.md)'s
own revisit trigger, and specifically:

* **A regression the panel missed.** An adapter that passes all three regression suites and is then
  found, in use or by a fuller panel, to have lost something material. That is the panel failing at
  its one job, and the lost capability's suite is the candidate fourth row.
* **A regression suite that never moves.** If, across every adapter measured on this machine, one of
  the three never separates a good adapter from a damaged one, it is costing GPU time for no
  information and should be replaced rather than kept for symmetry.
* **Row 3 resolving to the same suite everywhere.** The "base's strongest measured capability" rule
  exists to make the panel targeted. If in practice it always lands on the same suite, the rule is
  decoration and should become a fourth fixed row that says so plainly.
* **Cost, honestly measured.** If the three-suite panel is cheap enough that operators run a fuller
  one anyway, the panel is too small; if it is expensive enough that they skip measuring adapters,
  it is too big. Both are observable from run history rather than from opinion.

Making the panel **configurable** is not on that list, and would need its own ADR: it overturns the
comparability the whole design assumes ([catalogue §8.2](benchmark-catalog.md)).

### First run against real adapters, 2026-09-06 (row H5)

The panel had only ever been *composed*; the LA3 journey measured adapter subjects with
`native.echo` for speed, so rows 1 and 2 had never met a LoRA. They have now
(`FreeWeight/tests/live/test_a2_regression_panel.py`), on the reference machine against
`Qwen2.5-1.5B-Instruct.Q8_0` and its three trained adapters:

| Subject | `instruction_following` | `structured_output` |
|---|---|---|
| bare base | 0.727 (n=11) | 1.000 (n=3) |
| `+terse` | 0.818 (n=11) | 1.000 (n=3) |
| `+pirate` | 0.818 (n=11) | 1.000 (n=3) |
| `+verbose` | 0.818 (n=11) | 1.000 (n=3) |

**No forgetting was detected, and that is the weaker half of the finding.** All three adapters
moved `instruction_following` by exactly `+0.091` — one case in eleven — and none moved
`structured_output` at all. Three adapters trained for three different voices scoring identically
on both rows is not evidence that all three are undamaged; it is evidence that **at n=11 and n=3 the
panel resolves nothing finer than gross forgetting**. The adapters are certainly live: the same
provider, prompted identically, answers as the base, as a pirate, tersely and verbosely.

This is the **second** revisit trigger above — "a regression suite that never moves" — arriving in
its quietest form, and it is recorded rather than acted on: one base, one machine, three adapters
none of which is damaged. What would settle it is a deliberately damaged adapter. Until then the
honest statement is that the panel *ran*, produced each subject's own numbers, and inherited
nothing — and that its sample sizes are too small to be read as a clean bill of health for any
particular LoRA.

## 2. Integration risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| I1 | **Ollama API changes** break discovery, metadata or timings | Medium | High | ModelRack owns the adapter; name-based parsing; `UNSUPPORTED` for missing fields; version-annotated fixtures; live tests catch drift nightly |
| I2 | **Evidence contract mismatch** with LoadCoach | Low | High | SetSpec schemas with goldens; contract tests in both repositories; a consumer harness that imports only `setspec` |
| I3 | **External benchmark CLIs change** | High | Medium | Pinned versions and dataset hashes; recorded output fixtures; adapters fail loudly with the version they expected |
| I4 | **Shared package churn** (BaseAiCore/ModelRack breaking changes) | Medium | Medium | Compatible version ranges; nightly compatibility matrix; pre-1.0 changes coordinated in the PR description |
| I5 | **Descriptor refresh rewrites history** | Low | High | Descriptor snapshots are immutable rows; runs reference the snapshot they used |
| I6 | **The two applications disagree about a subject string** — FreeWeight exports evidence keyed on a subject LoadCoach spells differently, and the bundle imports as `unmatched` for ever | Low | High | Both derive the string from `baseaicore`'s `MeasurementSubject.canonical_subject_id` / `AdapterIdentity.canonical_suffix`, and neither re-implements the format; integration verification I18 asserts the agreement across a real file with no shared code and no shared database | Adapter-bearing records importing as `unmatched` while their base's records bind |
| I7 | **The operator's adapter directory drifts from what was measured** — an artifact is replaced in place, and old measurements re-attach to new weights | Medium | High | Identity is the artifact digest, not the path or the name; a manifest whose recorded hash no longer matches its artifact makes that adapter *unavailable* and named, never silently re-used ([ADR-0061](../../adr/0061-the-adapter-registry-is-a-directory-and-a-manifest.md) rule 5); FreeWeight's `adapters` table outlives the directory, so a deleted artifact leaves a subject with history and no availability rather than an orphan | An adapter reported unavailable with a digest mismatch after a re-conversion |

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
* **Subjective goals cost the user real work** — roughly twelve graded samples before a rubric
  produces evidence. The grading *is* the ground truth, so the cost is the feature and not an
  onboarding defect to be optimized away. Some users will not pay it, and their goals will run and
  display without ever emitting evidence.
* **A rubric that cannot be measured is told so** — a failed calibration gate withholds evidence
  rather than emitting a discounted number. The one place in the suite where a measurement is
  withheld rather than degraded, and deliberately so.
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
