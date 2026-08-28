# Phases 8, 8A and 8B — issue register

Raised while implementing the three judgement-dependent phases. Unlike
[`PHASE8_DECISIONS.md`](PHASE8_DECISIONS.md), which records choices the specification left open,
everything here is a **gap between what is written down and what is built** — a document that needs
a change, a specified behaviour that is not implemented, or a specified test that turned out to be
unreachable as written.

**Status: 19 raised, 3 resolved.** Six need a documentation change and nothing else; four are
scheduled work whose phase is named; four are divergences that need a decision; three were defects
found while building these phases and were fixed inside them, and are recorded because each has a
regression test somebody should be able to find the reason for.

---

---

## Status — 2026-08-28

Two passes on 2026-08-28 — a documentation pass, then the decisions it surfaced — closed thirteen
of these. What each one needed is recorded so a reader can tell "fixed" from "written down and
still owed".

| # | Issue | Status |
|---|---|---|
| 1 | Three stable error codes are not in spec §13's table | **Closed.** Spec §13 lists all five missing codes — the three named here plus `COMPARISON_SUBJECT_NOT_FOUND` and `COMPARISON_REFUSED`, found while doing it — each with why the shared set could not describe it. Statuses are in the new [API §11](docs/apps/freeweight/api.md), which is the error table the document never had. |
| 2 | `MetricDefinition.source` is a manifest field the catalog does not document | **Closed.** Catalog §5 names `source` in the field list and the example; §5.1 explains why the fallthrough is unsafe for a conditional rate. |
| 3 | Two export formats need naming apart | **Closed.** Spec §7.3 carries a table separating `benchmark.goal_pack` from the bundle; Subjective Goals gains §2.3 describing the bundle format; API §3a says which endpoint produces which. |
| 4 | The `[goals]`, `[judge]` and `[calibration]` sections have no reference entry | **Partly.** Spec §12 now documents `[runtime]` — which shipped in Phase 10 and was undocumented — and states plainly that the *generated* reference Configuration Standards §8 requires is owed. The generator and its CI check still do not exist. |
| 5 | `native.audit` and `native.long_context` produce fewer metrics than the catalog lists | **Closed.** The eleven unowned figures move to catalogue §3.15, out of 1.0 scope, each with what it is blocked on. §3 now promises only what ships. |
| 6 | The `FreeWeight/docs/` mirror is stale and incomplete | **Closed, structurally.** All seven documents, written by `scripts/sync_docs.py`, with `--check` for CI. The de-linking convention is the script rather than a habit, which is what failed last time. |
| 7 | `capability_evidence` does not exist yet | **Open.** Phase 11. |
| 8 | `contributes_to` is stored and linted but never emitted twice | **Open.** Phase 11. |
| 9 | Rung-4 (`human`) criteria are parsed and skipped | **Closed as a decision: Phase 11 owns it.** Phase 10 was named as owner and shipped the *calibration* half only, which is how it came to belong to no phase. Phase 11 is where a human grade first has somewhere to go — evidence — and its Work list now names the UI and the CLI. |
| 10 | Starter packs, the wizard, and the endpoints that serve them | **Closed.** All shipped in Phase 10A: the starters page, `GET /api/v1/goals/starters`, `POST /goals/starters/{key}/fork`, and `freeweight goals starters|fork-starter`. |
| 11 | The rule timeout cannot interrupt a regex | **Closed.** Spec §14 now says the dialect is the guard and the timeout the backstop, and states why CPython cannot deliver the other order. |
| 12 | `CALIBRATION_REQUIRED` is specified and unimplemented | **Closed: the spec yields.** §13 now says an uncalibrated judged goal runs, emits no evidence, and reports what is missing. ADR-0032 §3's argument applies harder *before* the first calibration than after a failed gate — the author has nothing at all to look at. The code already did this. |
| 13 | `runs.prompt_pack_id`, `prompt_pack_version`, `prompt_pack_hash` are always `NULL` | **Closed.** Populated from the active prompt library at run creation. |
| 14 | `prompt_source = "user_override"` is on the run, not on every record | **Closed: the standard yields.** Prompt standards §6 now says run granularity, because an override applies to every sample of the run that rendered it — a per-sample column would store one identical value ten thousand times and add no fact. |
| 15–17 | | **Closed** in-phase. |
| 18 | Phase 8 acceptance criterion 3 shows the sweep's gap, not the model's | **Closed.** The ceiling is `benchmarks.long_context_max_tokens`, default 32 000: how far a sweep reaches is a property of the machine. The effective ladder is hashed into the suite's `dataset_hashes`, so two ceilings never average. |
| 19 | Deleting a goal removes its pack directory without a backup | **Open.** Needs a decision. |

---

## Needs a documentation change

### 1. Three stable error codes are not in spec §13's table

`GOAL_PATH_UNSAFE`, `GOAL_HASH_MISMATCH` and `PROMPT_OVERRIDE_REFUSED` are implemented, returned
and tested; §13's list does not have them.

Phase 8A's own test list requires each import refusal — oversize, path traversal, colliding slug,
bad hash, malformed JSON — to carry "its own error code", and §13 names three goal codes for five
refusals. `PAYLOAD_TOO_LARGE` and `CONFLICT` cover two from the shared set; the remaining two are
new. `PROMPT_OVERRIDE_REFUSED` is the third, and it exists because "you have an override in place"
has a specific remedy that a generic `CONFLICT` cannot suggest.

**Needs:** four rows added to spec §13, and their HTTP statuses recorded in
[api.md §4](../docs/apps/freeweight/api.md). The mapping this build uses is
`GOAL_PATH_UNSAFE` → 400, `GOAL_HASH_MISMATCH` → 400, `PROMPT_OVERRIDE_REFUSED` → 409.

### 2. `MetricDefinition.source` is a manifest field the catalog does not document

Benchmark catalog §5's manifest example lists `key`, `unit`, `higher_is_better` and `aggregation`
for a metric. This build adds an optional `source` (`auto` | `detail` | `score`), defaulting to
`auto`, which is exactly the behaviour every existing manifest already had.

The reason is in `PHASE8_DECISIONS.md` §8: §5.1's three-source resolution order ends in a fallback
that is unsafe for a *conditional* rate. **Needs:** a paragraph in §5.1 and a row in §5's field
list.

### 3. Two export formats need naming apart

Spec §7.3 lists `benchmark.goal_pack` as an export. This build produces two things: that SetSpec
envelope from `GET /api/v1/goals/{slug}/export`, and a *bundle* — every file of the pack, hash-pinned
— from `freeweight goals export`, which is what `goals import` and `POST /goals/import` read.

They are different artifacts for different readers, and ADR-0031 §6's "exports as one hash-pinned
bundle" describes the second while §7.3 names the first. **Needs:** §7.3 and
[api.md §Goals](../docs/apps/freeweight/api.md) to say which endpoint produces which, and
subjective-goals §2 to describe the bundle format.

### 4. The `[goals]`, `[judge]` and `[calibration]` sections have no reference entry

They are implemented to spec §12 exactly and appear in `config init`'s example file. Configuration
Standards §8 asks for a generated configuration reference; the suite does not have one yet, so this
is noted rather than actioned.

### 5. `native.audit` and `native.long_context` produce fewer metrics than the catalog lists

Implemented, because Phase 8's Work list names them: precision, recall, F1, the clean-code
false-positive rate and localization for `native.audit`; the depth/position/distractor sweeps and
`effective_context_tokens` for `native.long_context`.

Not implemented, because the Work list does not name them and several need a sandbox:

* audit — bug-category accuracy, severity accuracy, explanation correctness, suggested-fix
  correctness, patch compile rate, patch test-pass rate, regression rate;
* long context — accuracy AUC across context, and latency / VRAM / prompt throughput by context;
* judge — the *variance* half of catalog §3.11's "agreement rate, variance" row.

**Needs:** either a phase named for them (the patch metrics need Phase 9's sandbox at the earliest)
or the catalog rows marking them as a later scope. Leaving the catalog promising metrics no phase
owns is how §6's `creative_writing` row became a dangling contract in the first place.

### 6. The `FreeWeight/docs/` mirror is stale and incomplete

It carries four of the seven FreeWeight documents (`api`, `development-plan`, `spec`,
`subjective-goals`) and none of the ADRs or standards, and the four it has differ from the
canonical copies under `AiSuite/`. This phase read from the canonical copies throughout.

**Needs:** a decision about what the mirror is for. Either it is complete enough to work from
standalone — in which case it needs `benchmark-catalog.md`, `data-model.md`, `risks.md` and the
ADRs the code references — or it is not, and the repository should say so rather than carrying a
partial copy that reads as authoritative.

---

## Scheduled elsewhere, named here so the gap is visible

### 7. `capability_evidence` does not exist yet, so the gate's "no row" is asserted conditionally

ADR-0032 §3's rule is that a goal below the gate emits **no** evidence, and Phase 8B's test list
says the absence must be "asserted directly, because *we emitted it quietly at the floor* is
precisely the failure the gate exists to prevent".

The table is Phase 11's. `tests/integration/test_calibration_flow.py::TestTheGate` asserts the
absence the only way it currently can: the table has no rows if it exists, and does not exist
otherwise. **Phase 11 must keep that assertion true**, and should replace the conditional with a
direct one the moment the table is created.

### 8. `contributes_to` is stored and linted but never emitted twice

ADR-0032 §1 requires a goal that declares `contributes_to` to be emitted **twice** — once as
`user.<slug>` and once as a weighted source inside the shipped capability. The field is parsed,
validated against SetSpec's vocabulary and carried into the suite's `capabilities`, but nothing
emits evidence yet. **Phase 11.**

### 9. Rung-4 (`human`) criteria are parsed and skipped

A `rung: "human"` criterion validates, hashes, lints and appears in `score_method_mix` — and then
skips every sample with `human_grade_pending`, because there is no blinded grading UI to produce a
grade. Subjective Goals §3.3 describes the UI; Phase 10 owns it. Until then a goal that declares one
measures less of itself than it says, and the applied weight shows that.

### 10. Starter packs, the wizard, and the endpoints that serve them

`GET /api/v1/goals/starters`, `POST /api/v1/goals/starters/{key}/fork`, `freeweight goals
starters|fork-starter` and the web authoring flow are all absent. They are Phase 8B's and Phase 10A's
*Deferred* lines respectively, and spec §7.1/§7.2 list them without saying so. The `unforked` flag
they set is implemented and linted, so the machinery is ready for them.

---

## Divergences and defects that need a decision

### 11. The rule timeout cannot interrupt a regex, so the dialect refuses the pattern instead

Phase 8A's test list says: *"Catastrophic-backtracking regex fails the criterion within
`rule_timeout_ms` and does not stall the run; the goal completes with that criterion in `error`."*

**That test is unreachable in CPython as written.** The regex engine holds the GIL for the whole
match, so a worker thread running one cannot be interrupted by anything in the same process; the
timeout's own `future.result(timeout=…)` cannot even wake up. Measured: a 50 ms budget against
`^(?:a|a?)+$` and 24 characters returned after 3.1 seconds.

What this build does instead is *stronger*: `lint_pattern` refuses unbounded repetition of a group
outright, at pack-load time, so the pattern never runs at all
(`tests/unit/test_rules_regex.py::TestTheDialectLint`). The timeout remains as the backstop for
every rule that yields — which is every rule in the library — and
`tests/unit/test_composite.py::TestTheRuleTimeout` asserts the mechanism against an injected slow
rule.

**Needs:** the phase's test line and spec §14's "user regex runs under `rule_timeout_ms` with a
linted dialect (no backreferences, bounded repetition) so a catastrophic-backtracking pattern fails
the criterion rather than the process" rewritten to say that the dialect is the guard and the
timeout is the backstop. As written, §14 implies the timeout does work the timeout cannot do.

**Residual:** a rule that *does* time out leaves a worker thread running to completion, and
`concurrent.futures` joins it at interpreter exit. In a server that is invisible; in the CLI it
would delay the process's exit by however long the rule takes. With the dialect in place no shipped
rule can reach that state, but the mechanism is worth knowing about.

### 12. `CALIBRATION_REQUIRED` is specified and unimplemented

Spec §13: *"`CALIBRATION_REQUIRED` is raised at run start when a goal has rung-5 criteria and no
calibration record at all; the error names the number of samples still to grade."*

Phase 8B's Work list does not name it, and this build does not raise it: a judged goal with no
calibration record runs, its judged criteria are graded by whatever jury can be assembled, and the
result carries `judge_validity_factor` and — since the gate has nothing to compare — emits no
evidence.

There is a real tension to settle, which is why this is raised rather than built:

* §13 says such a run is **refused**;
* ADR-0032 §3 says a run whose *gate* fails still executes, because "the diagnostic data is exactly
  what the user needs to fix the rubric, and it costs one GPU-bound run to obtain";
* the same argument applies at least as strongly *before* the first calibration, where the author
  has nothing at all to look at.

**Needs:** a decision between the two, in the spec. If §13 wins, the check belongs in `create_run`
beside the prompt-override refusal, and the error should name the sample count from
`grading_progress`.

### 13. `runs.prompt_pack_id`, `prompt_pack_version` and `prompt_pack_hash` are always `NULL`

Pre-existing, from Phase 6. `services/runs.py::_benchmark_library` reads `benchmark.library`, and no
benchmark object has that attribute — `SuiteBenchmark` is a manifest and its tests — so the three
provenance columns have never been populated.

Phase 8A makes this more visible rather than causing it: the reproducibility fingerprint now records
which prompts an *override* replaced, so a reader can see that a run used an overridden prompt while
the columns that would say which pack it came from are empty.

**Needs:** either the library threaded into `create_run` (the pack is already loaded by
`active_prompt_library`, so this is a small change) or the columns removed from the data model.

### 14. `prompt_source = "user_override"` is on the run, not on every record

Prompt Standards §6: *"Overridden prompts are marked in the UI and in every record that used them
(`prompt_source: "user_override"`)."*

This build records the overridden prompt ids twice on the run — inside the reproducibility
fingerprint document, and as a `prompt_overridden` degradation carrying `prompt_source:
"user_override"` — and `freeweight prompts list` marks the record itself. It does **not** put a
`prompt_source` column on `samples`, because that is a schema change the phase's Work list does not
name and the data model does not specify.

**Needs:** either a `prompt_source` column on `samples` in the data model, or §6 amended to say the
marking is at run granularity. A run is the right granularity in practice — an override applies to
every sample of the run that rendered it — but the standard says "every record".


### 18. Phase 8's acceptance criterion 3 is demonstrable, but the gap it shows is the sweep's, not the model's

Run live against `ollama/hk:latest` (a 9.4B Qwen 3.5 at Q8_0, advertised 262 144 tokens):

```text
advertised                      262144
effective_context_tokens         32025
longest_tested_context_tokens    32025
retrieval_accuracy                 1.0   (10/10 across the depth and position sweeps)
```

The two numbers differ by a factor of eight, so the criterion — *"effective context differs from
advertised context on at least one real model and the difference is explained by the depth/position
data"* — is demonstrable. But the depth data explains it as **the edge of the measurement**: the
model answered correctly at every length the sweep probed, so 32 025 is a floor rather than a
limit.

That is a consequence of `PHASE8_DECISIONS.md` §6 (the shipped sweep stops at 32 000 tokens), and
this build now reports `longest_tested_context_tokens` beside the effective figure so a reader
cannot take one for the other. It is still not what the criterion is reaching for.

**Needs:** a decision about the sweep's ceiling. Either the shipped sweep grows — which costs
startup time and memory for every process that lists benchmarks, and needs the documents expanded
lazily rather than at suite-build time — or the criterion is reworded to ask that effective context
be *measured and reported beside* advertised context, which is what the suite actually delivers.

### 19. Deleting a goal removes its pack directory without a backup

Database Standards §8: *"Destructive operations preview, confirm, transact and back up."*
`DELETE /api/v1/goals/{slug}` previews (a bare `DELETE` is the preview), names what it would orphan
and how many grades it would destroy, and performs the row deletion in a transaction. It then
removes the pack directory from disk with no backup.

The mitigation is real but partial: the pack is the user's own hand-editable, git-trackable JSON,
and this build says so in `goals init`'s own output. It is still the one destructive operation in
the application that takes a backup of nothing.

**Needs:** either the pack directory copied into the backup directory before removal — the
mechanism `infrastructure/db/backup.py` already has for the database — or §8 amended to say that
files the user owns outside the data root are their own to keep.

---

## Found and fixed inside these phases

Recorded rather than dropped: each one is now asserted against, and a reader who finds the
assertion should be able to find the reason for it.

### 15. An unpartitioned calibration sample could be rendered as a judge-prompt exemplar ✅

`add_samples` defaulted a new sample's `partition` to `anchor`, and `anchors_for` reads the anchor
half. A caller that assembled the jury *before* the partition had been computed — which is what
`freeweight goals calibrate` does — therefore handed the jury every sample as an exemplar,
including the ones the partition was about to hold out.

**Fixed** two ways, because one guard here is not enough: a new sample now defaults to `holdout`,
and `run_calibration` rebinds the jury to the anchors *its own* partition produced rather than
trusting whatever it was handed. Asserted by
`tests/integration/test_calibration_flow.py::TestTheHoldoutIsNeverShownToTheJury`, which scans the
prompts the jury was actually sent.

### 16. Re-syncing a goal deleted the author's grades ✅

`GoalRepository.sync` deleted and recreated `goal_criteria` on every load, and
`calibration_grades` cascades from it. Every CLI command and every API request that touches a goal
calls `sync_goals`, so a full grading sitting was destroyed by the next command the author typed.

**Fixed:** criteria and tasks the pack still declares are now updated in place, matched by key, and
only a criterion the pack has removed is deleted — where losing its grades is correct, because the
measurement they graded no longer exists. Asserted by
`TestTheAuthorsGradesSurviveEverything`.

### 17. Importing the calibration service alone could not write through it ✅

`models_goals.py` declares three foreign keys into `samples` and `models`, which live in sibling
model modules. SQLAlchemy resolves a foreign key's target by *name* at flush time, so a process
that imported only `freeweight.services.calibration` failed its first write with
`NoReferencedTableError` — invisible under pytest, which imports everything, and immediate from the
CLI.

**Fixed:** `models_goals` imports its two siblings for their registration side effect, so the
dependency is the module's own rather than every caller's. Asserted in a subprocess by
`test_the_goal_models_register_the_tables_their_foreign_keys_point_at`.
