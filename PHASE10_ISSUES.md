# Phase 10 & 10A — issues to address

Written at the end of Phase 10 (dashboard, results experience, data management, exports) and
Phase 10A (the goal authoring wizard and the starter packs). Each entry is something a later phase,
a docs change or an upstream package has to resolve. Nothing here blocks either phase's acceptance
criteria; everything here would become a defect if it were forgotten.

The second half of the file records what Phase 9's issues now look like: three are closed, six
remain.

---

---

## Status — 2026-08-28

Two passes on 2026-08-28: a documentation pass that wrote the two owed ADRs and amended eleven
documents, then the seventeen decisions it surfaced, applied. **Nine of these twelve are closed.**
The three that are not are each waiting on something outside this file: Phase 14 owns CSRF, and the
two remaining scope items are recorded at the foot of this file.

A note on how the schema changes landed, because it is a deliberate departure: **no new migration
revisions.** New columns and tables were folded into the revisions that create them, and existing
development databases are dropped. Pre-1.0, with no installation whose data outlives a schema
change, a chain of upgrade steps that will never run is a liability rather than a record — the
schema should read as though it were designed this way on day one, because that is the only state
anyone will ever build.

| # | Issue | Status |
|---|---|---|
| 1 | `benchmark.export` is a schema this build invented, and owes an ADR | **Closed.** [ADR-0035](docs/adr/) gives applications a namespace of their own and amends ADR-0025 §1; the schema is `freeweight.export`, with no compatibility path for the old name (pre-1.0, nothing to keep readable). A contract test scans the source so prose is not the only thing holding the rule. |
| 2 | `setspec.metrics.MetricValueFields` carries no metric key | **Closed.** `metric_key` is required, lower snake case, dot-separable — the last constrained by a real producer: goal suites emit `criterion.<key>`, which a flat pattern rejected. Payload stays at schema 1.0: pre-freeze, and development results are not retained. |
| 3 | The application has no CSRF token, anywhere | **Open.** Phase 14 owns it. |
| 4 | Wizard drafts borrow the `settings` table | **Closed.** `wizard_drafts`, with an `expires_at` every save pushes out, so the clock measures neglect rather than age. Folded into the migration that creates the goal tables rather than added as a new revision. |
| 5 | UI/UX §13's blanket contrast item cannot be satisfied as written | **Closed.** §1 carries the compliant palette with measured ratios for both themes and both grounds, and names the bar each token must clear; §7 and §13 now ask for the pairs the application *renders*, each against its own role. The standard's original palette was the thing that failed. |
| 6 | Subjective Goals §8's pack order and its percentages disagree | **Closed.** §8's table is the reading order, four packs and four figures, 40 → 55 → 70 → 90. |
| 7 | Three endpoints spec §7.1 lists do not exist | **Closed — and there were nine, not three.** The routability test came first and found the whole machines API and `POST /runs/{id}/repeat` on top of the six known. All nine ship; `benchmarks list\|show` (CLI) goes to Phase 12 and the evidence endpoints stay with Phase 11, both named in the test's `SCHEDULED` map. |
| 8 | `GET /results/export` refuses a selection wider than 500 runs | **Closed.** `since` / `until`, half-open so consecutive windows tile — a run on the boundary belongs to the window that starts there, never both and never neither. Every document states the window it covers. |
| 9 | Retention is a selection nothing applies | **Closed by removal.** A measurement does not expire, and what invalidates one — the model leaving the machine, the hardware changing — is not something a clock can detect. `scope=model` is the deletion that was actually wanted, and it already previews and confirms. |
| 10 | `include_prompts` exports prompt identity, not prompt text | **Closed.** `include_prompt_text` adds an appendix of each distinct rendered prompt. Re-rendered rather than read from storage, so it verifies as well as supplies: a prompt edited since the run is absent rather than wrong. |
| 11 | The settings page cannot change a setting the running process already captured | **Closed.** The scheduler re-reads between runs, which is safe by construction — a run's config is frozen at creation — and is what the page already promised. The sampler is still not re-intervalled, deliberately: its interval is a recorded measurement condition. |
| 12 | The dashboard's headline metric per suite is a hand-maintained table | **Closed.** `headline_metric` is a manifest field and all fifteen shipped manifests declare one; the dashboard reads it. |

Phase 9's entries are re-stated in that file's own ledger: **eight closed, one open**.

The four M2 readiness items at the foot of this file: the packaging standard's undated dependency
example is **closed** (§4 now dates the block, names the failure it caused, and points at the
roadmap's version table). FreeWeight's version for M2 is **closed** (`0.9-beta`, recorded in the roadmap's §6
trajectory, which now has application rows at all). I1–I3 are **closed**: roadmap §4's milestones
are `pytest -m I1` / `I2` / `I3`, so a reviewer can point at one thing.

**The PyPI names are nearly closed by circumstance:** `baseaicore`, `modelrack` and `sweatmeter`
are already published under this project's own name, at the versions this workspace holds.
`setspec` is the one gap, and it is what `install-check` needs.

---

## 1. `benchmark.export` is a schema this build invented, and owes an ADR

**What.** `GET /api/v1/results/export` and `freeweight results export` emit a SetSpec envelope
whose `schema` is `benchmark.export` (`src/freeweight/services/export.py`). No ADR declares it and
no SetSpec module defines it; the payload's shape lives only in FreeWeight.

**Why it matters.** Spec §7.3 lists FreeWeight's exports as `benchmark.result`,
`benchmark.run_summary`, `capability.evidence`, `benchmark.evidence_bundle`,
`benchmark.goal_pack` and `benchmark.calibration_report`, plus flattened CSV. None of them can
carry what api.md §5 documents this endpoint as producing:

* **`benchmark.run_summary` cannot name its own metrics.** Its `aggregate_metrics` is a
  `WireSequence[MetricValueFields]`, and `MetricValueFields` has no key field (issue 2). An export
  of run summaries alone would produce a document whose metrics cannot be told apart, which fails
  the phase's own round-trip test for a reason that is not FreeWeight's to fix.
* **No SetSpec schema has a slot for raw samples**, and `include_samples=true` is a documented
  parameter of this endpoint.

So the export is FreeWeight's own document, it *embeds* a `benchmark.run_summary` payload per run
under `summary` — built and validated through `BenchmarkRunSummaryOut`, so the contract is really
exercised — and it puts the keyed metric rows and the optional samples beside it where they are
addressable.

**Action.** Write an ADR that either blesses `benchmark.export` as an application-owned schema or
moves it into SetSpec. CLAUDE.md's rule applies: "if an architectural decision seems missing, that
is a defect in the docs — close it with a new ADR before writing code." This one was found while
writing the code, and the ADR is owed. api.md §5 should say which schema the endpoint emits either
way; today it says only "SetSpec-wrapped".

---

## 2. `setspec.metrics.MetricValueFields` carries no metric key

**What.** `MetricValueFields` declares `value`, `unit`, `aggregation`, `higher_is_better`,
`sample_count` and `dispersion` — and nothing that says *which metric* it is. Both
`BenchmarkResultFields.metrics` and `BenchmarkRunSummaryFields.aggregate_metrics` are sequences of
it.

**Why it matters.** A consumer receiving `benchmark.run_summary` gets a list of numbers it cannot
attribute. It cannot chart them, cannot compare them across runs, and cannot check whether the
producer emitted the metric it was looking for. This is not a FreeWeight bug and no FreeWeight
change can fix it: adding a key to a payload that forbids extra fields is an upstream change.

**Action.** Add `metric_key: str` to `MetricValueFields` in SetSpec. It is **additive**, so it is a
minor bump (`1.0` → `1.1`) under API standards §7 rule 1, and readers accepting any minor within a
supported major take it for free (rule 2). Once it lands, `benchmark.export`'s own `metrics` block
can become the SetSpec one and issue 1 gets simpler.

---

## 3. The application has no CSRF token, anywhere

**What.** Security standards §2.1 requires every HTML form route to carry a double-submit token: a
`__Host-`-prefixed `SameSite=Strict` cookie plus a hidden field, compared with
`hmac.compare_digest`, with a mismatch or absence returning `403 CSRF_FAILED`. No such mechanism
exists in this codebase. `grep -ri csrf src tests` returns nothing.

**Why it matters.** It predates this phase — `POST /models/discover`, `POST /runs` and
`POST /runs/{id}/cancel` have shipped without one since Phase 3 — but Phase 10 added the most
destructive form in the application (`POST /database/delete`) and Phase 10A added a dozen more.

The deletion route is defended specifically, and deliberately: it requires a **preview token** that
only a same-origin preview response carries (a cross-origin attacker can issue the request but
cannot read the response that would give them the token) **and** a typed confirmation. That is a
real defence for that route and not a substitute for the framework.

**Action.** **Phase 14 owns this** — its Work list names "the CSRF token on form routes" inside
the security pass — so it is scheduled rather than unowned. Recorded here because Phase 10 and 10A
between them roughly tripled the number of form routes that will need it, and because the gap is
live in every build until M6. It is one middleware, one hidden field in a base template, and a test
per form.

---

## 4. Wizard drafts borrow the `settings` table

**What.** Steps 1–4 of the wizard are pre-pack state and are stored as a JSON value under
`settings.key = "wizard.draft.<id>"` (`src/freeweight/services/wizard.py`).

**Why it matters.** The `settings` table is for settings. A wizard draft is user data with a
lifecycle — it is created, edited over minutes or days, and deleted when its pack is written — and
none of that is expressed: there is no expiry, no owner, no index, and `db status` counts drafts as
settings. The grading half of the wizard does *not* have this problem: grades are real
`calibration_samples` and `calibration_grades` rows, which is exactly why grading survives a
restart.

**Action.** A `wizard_drafts` table with `created_at`, `updated_at` and an expiry, plus a
migration. It was not done here because a migration is outside this phase's file list, and shipping
half a schema change is worse than shipping none.

---

## 5. UI/UX standards §13's blanket contrast item cannot be satisfied as written

**What.** §13 requires "contrast checks pass for every token pair in both themes". Two of the
standard's own tokens make that impossible:

| Pair | Ratio | Required |
|---|---:|---|
| `--mw-text-subtle` `#94A3B8` on `--mw-surface` `#FFFFFF` | 2.58:1 | 4.5:1 for text |
| `--mw-accent` `#2F80ED` on `--mw-surface` `#FFFFFF` | 3.87:1 | 4.5:1 for link text |

The second is the FreeWeight brand accent, and §1 names it explicitly.

**Why it matters.** §7 calls WCAG 2.1 AA "non-negotiable", so the palette and the accessibility
rule contradict each other. Taken literally, no build can pass §13.

**What this phase did.** Added `--mw-accent-text` (a darkened brand blue at 5.75:1 on the page
background) for link text, keeping `--mw-accent` for fills, focus rings and chart series where the
3:1 UI-boundary rule applies; and split `--mw-border` (decorative table rules, no contrast
requirement) from `--mw-border-strong` (the boundary of an interactive control, 3.58:1). The
contrast test in `tests/accessibility/test_ui_checklist.py` asserts over the pairs the application
actually renders, and that list is in the test where it can be reviewed.

**Action.** Amend UI/UX standards §1 and §13: name the text-safe accent, state that
`--mw-text-subtle` is for non-text decoration only, and rewrite the §13 item as "over the token
pairs the application renders" rather than over the cross product.

---

## 6. Subjective Goals §8's pack order and its "40 % → 70 % → ~90 %" sentence disagree

**What.** §8 lists four starter packs in the order `creative_voice`, `brand_voice`,
`summary_faithfulness`, `technical_explanation`, then says "read in the order above, the packs go
from 40 % to 70 % to ~90 % deterministic weight" — three figures for four packs, with the fourth
described as "Mixed".

**Why it matters.** Development plan Phase 10A acceptance criterion 3 says the packs "read in the
documented order … demonstrate rising deterministic weight". With four packs and three figures,
"the documented order" is ambiguous.

**What this phase did.** `freeweight.goals.starters.READING_ORDER` declares the order that carries
the lesson — `creative_voice` (40 %), `technical_explanation` (55 %), `brand_voice` (70 %),
`summary_faithfulness` (90 %) — which is strictly rising, keeps each pack's documented character,
and is asserted by `tests/integration/test_starter_packs.py`. It differs from the table's row order.

**Action.** Reorder §8's table to match, or add the fourth figure to the sentence.

---

## 7. Three endpoints spec §7.1 lists do not exist

**What.** `GET /api/v1/benchmarks`, `GET /api/v1/benchmarks/{key}` and
`GET /api/v1/models/{model_ref}/results` are in spec §7.1 and are not implemented. `freeweight
benchmarks list|show` and `freeweight token create|list|revoke` are in §7.2 and are not
implemented either.

**Why it matters.** None of them is Phase 10's, and none was stubbed — a verb that exists and does
nothing is worse than one that does not, because `--help` advertises it. Recorded so the gap stays
a decision. The token commands wait on ADR-0014's authentication work; the benchmark listings are
small and have no obvious owner phase.

**Action.** Assign `benchmarks list|show` to a phase, or move it to spec §21.

---

## 8. `GET /api/v1/results/export` refuses a selection wider than 500 runs

**What.** `MAX_EXPORT_RUNS = 500`. A `scope=all` on a database with more runs than that is refused
by name.

**Why it matters.** It is the right refusal today — a truncated export that did not say it was
truncated would be a lie about what was measured, and the endpoint has no pagination because an
export is a document rather than a page. But a user with two years of measurements cannot export
them in one call, and the error tells them to narrow rather than offering a way through.

**Action.** Either a documented `since`/`until` window on the export (the shape
`GET /evidence/export` already uses), or a multi-document form where each file declares
`complete: false` and its own window. The second matches ADR-0022 §5's incremental-bundle
reasoning and is probably the right answer.

---

## 9. Retention is a selection nothing applies

**What.** `storage.retention_days` is configuration, `retention_selection()` turns it into a
deletion selection, and the database page shows the current value — but nothing ever *runs* it. A
user who sets 90 days gets no deletion.

**Why it matters.** A retention setting that silently does nothing is worse than not having one: it
reads as a promise about disk usage that the application is not keeping.

**Action.** Decide who applies it. It must not be applied without a preview and a confirmation
(database standards §8), so a background job that deleted on a timer would violate the standard;
the honest shape is probably a banner on the database page — "retention would remove 412 rows;
preview it" — plus a `freeweight db prune --yes` for scripts. Both need a decision about whether an
unattended install may ever delete without a human.

---

## 10. `include_prompts` exports prompt identity, not prompt text

**What.** `GET /api/v1/results/export?include_samples=true&include_prompts=true` includes each
sample's `prompt_id`, `prompt_version`, `prompt_hash` and `rendered_prompt_hash`, and not the
rendered text.

**Why it matters.** It is the right default — a database of measurements should not become a copy
of the prompt pack, and prompt standards §4 makes the identity sufficient to re-render — but a
consumer *outside* this machine cannot re-render anything, because they do not have the pack. An
evidence reader deciding comparability is fine; a reader auditing what was asked is not.

**Action.** Decide whether the export should carry a prompt appendix (each distinct
`rendered_prompt_hash` once, with its text) under a third flag. It is cheap — prompts repeat across
thousands of samples — and it is the difference between an auditable export and a referential one.

---

## 11. The settings page cannot change a setting the running process already captured

**What.** `update_settings` stores a value and says it "applies to work started from now on". The
telemetry sampler's interval, in particular, is read once in the application lifespan; changing it
from the settings page does not re-interval the running sampler.

**Why it matters.** The copy on the page is honest about it, and the alternative — changing a
measurement's conditions while it is being measured — is worse. The application lifespan now calls
`apply_stored()` before it builds the sampler and the scheduler, so a stored value is in force from
the next start; what it does not do is re-interval a sampler that is already running, or re-read
execution defaults between two runs of a serving process.

**Action.** Decide whether the scheduler should re-read execution settings between runs. It safely
could — a run's effective config is frozen at creation, so re-reading *between* runs cannot change
a measurement underneath itself — and that is what "applies to work started from now on" actually
promises. The sampler is harder and probably not worth it: its interval is recorded on every run as
a measurement condition.

---

## 12. The dashboard's headline metric per suite is a hand-maintained table

**What.** `HEADLINE_METRICS` in `src/freeweight/services/results.py` maps each shipped suite to the
one figure that stands for it in the comparison heatmap. A suite absent from it has no heatmap
column; it still appears in the panels.

**Why it matters.** Choosing which of eleven `native.judge` metrics represents "how good a judge is
this" is an editorial act and should not be inferred — an inferred "first metric" would silently
change when a suite gained one. But the table is in a service module rather than beside the suite
that owns the decision, so a new suite is added in one place and forgotten in another.

**Action.** Move the declaration onto `BenchmarkManifest` as a `headline_metric` field, so a suite
declares its own headline and the dashboard reads it. That is a manifest schema change and a
migration of the eleven shipped manifests.

---

## Phase 9's issues, revisited

| # | Issue | Status |
|---|---|---|
| 1 | The suite-derived metrics seam is undocumented in the ADRs | **Open.** Still owed. |
| 2 | `native.memory_kv` cannot separate model VRAM from the harness's own | **Open.** `SlopeFit.residual_stddev_bytes` is still computed and not emitted; both fixes are inside `benchmarks/memory_kv`, outside this phase's file list. |
| 3 | Sample time windows are reconstructed, not recorded | **Open**, and now visible to users: the case inspector reconstructs the same window to show a sample's telemetry, and says so on the page. A `samples.started_at` column would fix both. |
| 4 | Energy is attributed to the whole run, not to the requests inside it | **Open.** Waits on issue 3. |
| 5 | `results list\|show\|export` do not exist | **Closed.** All three ship in Phase 10, and `freeweight results` now has the four verbs spec §7.2 lists. |
| 6 | `native.reliability`'s cases are shipped facts, not a versioned dataset | **Open.** |
| 7 | `max_successful_context_tokens` reports the ceiling, not the model's limit | **Open.** The dashboard's context panel shows the figure without the distinction, so the ambiguity is now on a page rather than in a database. |
| 8 | CLI help examples are absent across the application | **Closed.** Every command in `cli/commands/*` now carries one realistic `Example:`, done in one pass as the issue recommended. |
| 9 | The `suite` query parameter is a guard, not a selector | **Closed.** It is now both: a run subject is still guarded by it, and a *model* subject resolves to that model's latest completed run of it. Naming a model with no suite is refused. The resolver lives in `services/results.py` beside the dashboard's, so "latest" cannot come to mean two things. |

---

## Appendix — the M2 readiness audit

Run against [master roadmap](../docs/roadmap/master-roadmap.md) §1 (M2's exit condition), §4
(integration milestones), §5 (stabilization) and §8 (the documentation consistency review that
precedes every milestone). Recorded here rather than in a separate file because everything it
found was either fixed in the same pass or is already an entry above.

**Phases 1–10A are structurally complete.** Every path declared in every `Files/subsystems` block
from Phase 1 to Phase 10A exists and is not a stub. Every remaining `TODO: implement per…` module
belongs to Phase 11 (`domain/{capability_mapping,confidence}.py`, `services/evidence.py`,
`web/routes/evidence.py`, `cli/commands/evidence.py`, the two `tests/contract/test_evidence_*.py`),
Phase 13 (`external/**`, `web/routes/sources.py`, the sandbox tests) or Phase 14
(`tests/e2e/test_full_journeys.py`).

**Fixed in this pass**, all of them found by the audit rather than by a failing test:

| Found | Was | Now |
|---|---|---|
| The `contracts` CI job selected zero tests and exited `5` | Red on every push since the workflow was copied in | Ten contract tests over the three schemas this build emits |
| The default suite took 4:55 | Testing standards §10 allows three minutes | 2:15 — the idle-detection wait was 2.2 s of every run and is tested in its own right |
| `setspec>=0.3,<0.4` | SetSpec is 0.2 until M3; the distribution could not resolve | `>=0.2,<0.3`, moving with the freeze |
| Four `dev` pins a major behind the verified toolchain | CI installed pytest 8 / mypy 1; the gate is run with pytest 9 / mypy 2 | Pinned to the verified majors |
| No nightly job | `-m live` and `-m performance` ran nowhere | `.github/workflows/nightly.yml` |
| `diff-cover` absent | Named as a required pre-merge gate | A pull-request job |
| The `docs/` mirror carried 4 of 7 documents | Links to the missing three had been stripped from the four that were there | All seven, one de-linking convention, no dangling links |
| The live suite stopped at Phase 8A | M2's exit is a demonstration on a real model | Phase 9's suites, Phase 10's drill-down/comparison/export, and a goal authored, calibrated against a real jury and scored |

**Found by the new live tests**, which is the argument for having written them:

* **`GET /api/v1/models`, `GET /api/v1/models/{model_ref}` and `POST /api/v1/models/discover` do
  not exist.** All three are in spec §7.1; the models surface is HTML-only
  (`web/routes/models.py` registers on the page router with no `/api/v1` prefix). Nothing in
  Phases 1–10A depended on the API form, so no test had ever asked for it — the live goal
  journey did, and got a 404. This belongs with the other unimplemented §7.1 endpoints in §7
  above, and is the third instance of the same pattern, which is worth reading as a signal: spec
  §7.1's endpoint list has no test asserting that every path in it is routable. One test that
  walks the list and reports the gaps would have caught all of them at Phase 3.

### ~~A run cannot pin its served context~~ — FIXED, and the root cause was deeper than it looked

**Severity: this one took a machine down.** Found by running the new live suite; fixed in the same
pass.

**What it looked like.** :func:`~freeweight.services.runs.create_run` built
``runtime_profile = RuntimeProfile()`` as a literal, so ``resolve_served_context`` always received
``requested_context=None`` and its ``CONFIGURED`` branch was unreachable.

**What it actually was.** Worse: :func:`_build_request` constructed every ``GenerationRequest``
**without a runtime profile at all**. So the profile was not merely unset — it was stored, hashed
into the reproducibility fingerprint, and *never sent to the provider*. Every run in this
application's history was served at whatever the provider chose while its record named a profile it
had never been asked for. ADR-0023's premise that "FreeWeight takes this seriously: a run names its
runtime profile, stores it, and hashes it into the reproducibility fingerprint" was true of the
record and false of the request.

**What it cost.** A modern local model advertises 128K-262K context. With nothing sent, Ollama
served the model's maximum: on a 30 GiB / 16 GB machine a 15.7B model at a 112K slot asked for
21.9 GiB of CPU KV cache, 7.7 GiB of VRAM KV cache and a 5.4 GiB compute buffer, fell back to nine
of its twenty-eight layers on the GPU, ran at 3.9 tokens/second, and took the display driver and
then the kernel with it. No OOM killer fired, because most of that allocation was mmap-backed.
Quietly, and worse: **every performance number measured that way was dominated by KV spill rather
than by the model.**

**The fix.** A ``[runtime]`` configuration section (ADR-0023 §1 already named it, for LoadCoach),
``freeweight run start --context-size`` (ADR-0023 §3 already named the flag), a ``runtime`` block on
``POST /api/v1/runs``, and the profile threaded through ``create_run`` → ``_RunContext`` →
``_build_request`` → the provider. ``_warm`` warms under the same profile, so warming no longer
loads at one context and measures at another. ``repeat_run`` repeats the *original's* stored
profile rather than re-resolving from current configuration, for the same reason it reuses the
frozen ``ExecutionConfig``.

Verified against a live Ollama, three runs of one model:

```text
--context-size 2048   -> ollama served 2048   model_vram_bytes 5,274,117,078
--context-size 8192   -> ollama served 8192   model_vram_bytes 6,295,440,588
--context-size 16384  -> ollama served 16384  model_vram_bytes 7,520,177,356
served_context_source = "configured" on all three, three distinct fingerprints
```

**Follow-ups, all now closed:**

* **The jury carried no profile either.** ``JuryService`` built its own ``GenerationRequest``
  without one, so a juror was served at the provider's choice — which is exactly the path that took
  a machine down, since the crashing model was the *juror*, not the candidate. A goal run's jury now
  serves under the candidate's own profile (judging at a different context than the answers were
  generated at would be a second, unrecorded variable), and both calibration juries serve under
  ``[runtime]``.
* **ModelRack now captures the served context.** ``/api/ps`` reports ``context_length`` beside
  ``size_vram`` and the adapter was discarding it; ``ResidentModel.context_length`` carries it,
  which is what makes "reported" distinguishable from "assumed" at all.
* **A run records what it was actually served.** ``served_context_observed`` is emitted as a
  metric, and where it contradicts a context the run merely *assumed*, the run carries a
  ``served_context_assumed_incorrectly`` degradation naming both numbers. The frozen fingerprint is
  never rewritten — provenance that changes after the fact is not provenance — so the disagreement
  is surfaced the way every other "the conditions were not what the record implies" fact is.
* **``PHASE9_ISSUES.md`` §7 is closed.** ``max_context_capped_by_configuration`` is emitted beside
  ``max_successful_context_tokens``, so "the model refused at the next rung" and "the sweep stopped
  where it was told to" no longer report an identical number with no way to tell them apart.

**One item genuinely remains, and it is a design question rather than a patch:**

``native.memory_kv`` sweeps context by **prompt filler** at a fixed served context, and fits its
VRAM slope against *device-wide* telemetry. Neither is now the best available measurement, and the
reason is a fact learned while fixing the above: **``size_vram`` scales with the context the model
was *loaded* at, not with the prompt length fed into it** — llama.cpp allocates the KV cache for the
whole slot up front. So an in-run sweep of prompt lengths measures KV *fill*, not KV *cost*, and the
device-wide slope it fits is contaminated by every other process besides.

The honest measurement is now available and is a different shape: several runs at different
``context_size`` values, differencing ``model_vram_bytes``. On the reference machine that yields
``qwen3:8b ≈ 4.95 GiB + 157 KB/token``, exact to about 1 %, from three runs and no regression at all.
But a benchmark is one run under one profile, so this cannot be a suite — it is a *study* across
runs, like the quantization and runtime-profile comparisons ``results compare`` already supports.

**Action.** Decide where a multi-run study lives. Until then ``native.memory_kv``'s slope should be
read as indicative and its ``kv_slope_fit_r_squared`` as the honesty check it was always meant to
be; ``PHASE9_ISSUES.md`` §2 ("cannot separate model VRAM from the harness's own") stays open behind
that decision, though ``model_vram_bytes`` now gives the per-model figure §2 asked for.

### ~~The jury picks jurors by sort order~~ — CLOSED, by removing the constraint instead

**Resolved 2026-08-28, and not the way this entry proposed.** The question was framed as "should
jury assembly refuse a juror the machine cannot hold beside the candidate?" — which took *beside*
as a given. It was not: judging happened inside the per-sample loop only because that is where
scoring happened, and a jury grades stored text rather than a live model.

So the run has two phases: generate every sample with the candidate, evict it, then judge them all
with the jurors ([spec §7.4](docs/apps/freeweight/spec.md)). Peak memory is now the larger of the
two models rather than their sum, which is a bigger improvement than the refusal would have been
and removes the case the refusal existed for. A juror that cannot be served *alone* still cannot be
served, and that is an ordinary `JUDGE_UNAVAILABLE` on a jury of zero, which already existed.

Two things fell out that were not the point and are worth more than an entry each: a goal run's
telemetry now describes the candidate rather than whichever model happened to be larger, and a
failing jury costs one sample rather than the run.

<details><summary>The original entry, as written</summary>

### ~~The jury picks jurors by sort order~~ — narrowed, not closed

**What.** :func:`~freeweight.services.jury.build_jury` selects jurors from every installed model,
sorted, minus the candidate. Size, advertised context and whether the machine can hold two models
at once are not inputs.

**What changed.** The dangerous half is fixed: a juror is now served under an explicit runtime
profile, so the crash mode — a juror loaded at its advertised 164K context — is gone whatever model
is chosen. What remains is a *fit* question rather than a safety one.

**What remains.** A user with one large model installed alongside small ones can still get it as a
juror by where it sorts, on a machine that cannot hold it beside the candidate.

**Action.** Decide whether jury assembly should consider servability, or whether the honest answer
is that it should not — a jury is an instrument, and silently swapping it for a smaller model
because memory is tight would change the measurement without saying so. If the latter, the refusal
needs to be explicit: "this machine cannot serve model X beside the candidate" is a legitimate
``JUDGE_UNAVAILABLE`` and far better than a hang.

</details>

**Still open, and each needs a decision rather than a patch:**

1. **The packaging standards' dependency example is undated.** §4's block literally contains
   `setspec>=0.3,<0.4`, which is the post-M3 state; it was copied verbatim into this repository and
   made the distribution unresolvable. The example should say which milestone it describes, or use
   a placeholder.
2. **FreeWeight has no version for M2.** The roadmap's §6 trajectory table starts FreeWeight at
   M3 (`1.0-rc`); LoadCoach's beta gets `0.9-beta`. This build is `0.1.0`. Whether a FreeWeight
   beta is tagged, and as what, is undecided in the documentation.
3. **`pip install freeweight` cannot work until the suite packages are published.** The
   `install-check` CI job installs the built wheel, which resolves `baseaicore`, `setspec`,
   `modelrack` and `sweatmeter` from PyPI. Roadmap §9 action 3 ("claim the distribution names on
   PyPI, or record the fallback naming decision in ADR-0015") is unfinished, and until it is, that
   job cannot pass on a clean runner.
4. **Integration milestones I1–I3 have no dedicated verification artefact.** Roadmap §4 says none
   is "considered complete on the basis of a code review". I1 (no provider HTTP code in FreeWeight)
   is asserted by `.importlinter`; I2 (telemetry bar live, no-GPU path) and I3 (exports validate
   against schemas and goldens) are covered by tests but are not labelled as the integration
   verification, so a reviewer cannot point at one thing and say "this is I3".
