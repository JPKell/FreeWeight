# Phase 9 — issues to address

Written at the end of Phase 9 (memory, energy, reliability and comparison). Each entry is
something a later phase, or a docs change, has to resolve. Nothing here blocks Phase 9's
acceptance criteria; everything here would become a defect if it were forgotten.

---

---

## Status — 2026-08-28

| # | Issue | Status |
|---|---|---|
| 1 | The suite-derived metrics seam is undocumented in the ADRs | **Closed.** [ADR-0034](docs/adr/) states the seam, its constraints and — the part that mattered — the boundary that a derived metric is a function of **one run**. Catalog §5.1 gains it as the fourth metric source. |
| 2 | `native.memory_kv` cannot separate model VRAM from the harness's own | **Closed, elsewhere.** The isolation exists as a *study*: `results compare` fits `model_vram_bytes` — the model's own residency, not the device total — across runs at different contexts, and reports `residual_stddev_bytes` beside `r²`. The in-run slope stays as it is and stays indicative; ADR-0034 §6 says why it cannot be more. |
| 3 | Sample time windows are reconstructed, not recorded | **Closed.** `samples.started_at` records when the request went out, with a check constraint against `created_at`. Folded into the migration that creates the table rather than added as a new revision. |
| 4 | Energy is attributed to the whole run, not the requests inside it | **Closed.** The power series is clipped to the union of the sample windows — clipped, not filtered, so a reading inside a request cannot carry the idle gap that follows it. "Joules per output token" is now absolute rather than merely consistent. |
| 5 | `results list\|show\|export` do not exist | **Closed** in Phase 10. |
| 6 | `native.reliability`'s cases are shipped facts, not a versioned dataset | **Closed.** The questions live in `cases.json` and are hashed into `dataset_hashes`, verified at build time — the same shape `native.long_context` already used. |
| 7 | `max_successful_context_tokens` reports the ceiling, not the model's limit | **Closed.** `max_context_capped_by_configuration` is emitted beside it. |
| 8 | CLI help examples are absent across the application | **Closed** in Phase 10. |
| 9 | The `suite` query parameter is a guard, not a selector | **Closed** in Phase 10 — it is now both, and API §5 documents which. |

---

## 1. The suite-derived metrics seam is undocumented in the ADRs

**What.** Three suites produce run-level figures no scorer can see: `native.memory_kv` needs the
descriptor's architecture fields and the run's VRAM series, `native.energy` needs the power series,
`native.reliability` needs every stored repetition of every case. The run engine now dispatches to
a `derive()` in each benchmark package from `_aggregate_run`
(`src/freeweight/services/runs.py`, `_suite_derived_metrics`).

**Why it matters.** [ADR-0033](docs/adr/) specifies the *interaction* protocol — how a benchmark
drives a conversation — and is explicit that a benchmark never touches the provider, the database
or the clock. It says nothing about a benchmark that needs the run's *telemetry* and *descriptor*
at aggregation time, which is a second, differently-shaped seam. It is currently a three-suite
allowlist (`_SUITES_WITH_DERIVED_METRICS`) precisely so it cannot become an unreviewed extension
point, but the constraint lives in a comment rather than in a decision record.

**Action.** Write an ADR that either blesses the narrow dispatch as it stands or replaces it with a
declared protocol on `Benchmark`. CLAUDE.md's rule applies: "if an architectural decision seems
missing, that is a defect in the docs — close it with a new ADR before writing code." This one was
found *while* writing the code, and the ADR is owed.

---

## 2. `native.memory_kv` cannot separate model VRAM from the harness's own

**What.** The observed context slope is fitted against `vram_used_bytes` for the target device,
sliced to each sample's window. That reading is the **device's** used memory, not the model's — it
includes anything else resident on the GPU.

**Why it matters.** The *slope* is mostly immune (a constant other process cancels out of a
gradient), but the intercept is not, and a second process whose own usage grows during the sweep
biases the slope directly. Phase 9's stated mitigation is idle detection plus reporting fit
quality, and both are in place: the run refuses or records `measured_while_busy`, and
`kv_slope_fit_r_squared` is emitted beside the slope. That is mitigation, not isolation. The
residual spread is computed (`SlopeFit.residual_stddev_bytes`) but is not currently emitted as a
metric row — worth adding, since it is the figure that shows *how* noisy, not merely that it was.

**Action.** When a provider reports per-model residency (ADR-0027's "revisit when" names vLLM),
prefer it over the device total and record which source was used.

---

## 3. Sample time windows are reconstructed, not recorded

**What.** `_stored_samples` derives a sample's start as `created_at - client_wall_ms`, because
`samples` stores only `created_at` (written when the call returns) and the observed wall time.

**Why it matters.** It is used only to decide which telemetry observations fell inside a request,
and it is honest about its limits — a sample with no recorded wall time collapses to a zero-length
window that no observation falls inside, so it contributes nothing rather than contributing a
guess. But it is an approximation of a boundary, and on a machine where the sampler interval is
close to the request duration it can attribute a reading to the wrong request.

**Action.** Consider a `started_at` column on `samples` (data-model change, needs a migration and
probably an ADR note). Cheap, and it removes an approximation from a measurement path.

---

## 4. Energy is attributed to the whole run, not to the requests inside it

**What.** `native.energy` integrates the power series over the run's window and divides by the
request, token and success counts. The window therefore includes the settle wait, the warm-up
generations and the inter-test cooldown.

**Why it matters.** "Joules per output token" is inflated by however much idle time the run
contained. It is consistent between two runs of the same suite with the same settings — which is
what a comparison needs — but it is **not** an absolute figure, and nothing currently says so on
the number itself.

**Action.** Either slice the power series to the union of the sample windows (which needs issue 3
first), or record the idle fraction of the window alongside the estimate so a reader can see how
much of it was work.

---

## 5. `results list|show|export` do not exist yet

**What.** `freeweight results` currently has one verb: `compare`. Spec §7.2 lists
`results list|show|export`.

**Why it matters.** It is Phase 10's work and is deliberately absent rather than stubbed — a verb
that exists and does nothing is worse than one that does not, because `--help` advertises it. Noted
so the gap is a decision rather than an oversight.

**Action.** Phase 10.

---

## 6. `native.reliability`'s cases are shipped facts, not a versioned dataset

**What.** The six questions live as a tuple in
`src/freeweight/benchmarks/reliability/benchmark.py`, and the manifest declares an empty
`dataset_hashes`.

**Why it matters.** Editing a question changes what the suite measures, and today only the suite
*version* separates those results — which is correct, but it depends on whoever edits the tuple
remembering to bump the version. Every other suite whose content can drift has a dataset hash that
makes the separation structural.

**Action.** Either hash the case tuple into `dataset_hashes` at build time (as `native.long_context`
does with `cases.json`), or move the cases into a `cases.json` beside the manifest and hash the
file. The second is more consistent with the rest of the suite set.

---

## 7. `max_successful_context_tokens` reports the ceiling, not the model's limit

**What.** `memory_kv.max_context_fit` climbs 8K → 128K, and the metric is capped at the run's
configured `served_context`.

**Why it matters.** On a run whose served context is 32 768, the answer is always 32 768 as long as
the model serves it — which is true and is what was demonstrated, but it is the *configuration's*
limit rather than the model's. Nothing currently distinguishes "capped by configuration" from
"refused at the next rung".

**Action.** Emit a companion boolean (`max_context_capped_by_configuration`) or a reason on the
metric, so the two cases read differently in the UI.

---

## 8. CLI help examples are absent across the application

**What.** CLI standards §2 requires every command's help to show "at least one realistic example".
`freeweight results compare` now has one; no other command in the application does.

**Why it matters.** It is a standards gap that predates this phase and is application-wide, so
fixing it only here would make `results compare` inconsistent with thirty other commands.

**Action.** A sweep across `src/freeweight/cli/commands/*` adding one example to each docstring,
best done in one pass rather than a command at a time.

---

## 9. The `suite` query parameter is a guard, not a selector

**What.** API §5 documents `GET /results/compare?subjects=a,b,c&suite=…`. This implementation
treats `subjects` as **run** references and `suite` as a guard: every subject must be a run of that
suite, and one that is not is refused by name.

**Why it matters.** The parameter shape is satisfied and the behaviour is safe, but a reader of
api.md might reasonably expect `subjects` to accept *model* references with `suite` selecting which
suite's latest result to compare. That reading needs a "latest completed run of suite S for model
M" query the repositories do not have.

**Action.** Decide which reading api.md means, and either amend the document or add the resolver.
Phase 10's results experience is the natural place, since it needs the same query.
