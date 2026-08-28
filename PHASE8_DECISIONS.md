# Phases 8, 8A and 8B — decisions the specification did not make

Everything here was decided while implementing the three phases, because the specification, the
ADRs and the standards did not settle it. Each entry states the decision, the alternatives, and the
reason — so a reader who disagrees can find the argument rather than the code.

Nothing in this file changes a decision the documents *did* make. Where an implemented behaviour
departs from something written down, it is in [`PHASE8_ISSUES.md`](PHASE8_ISSUES.md) instead.

---

## Phase 8 — judgement-dependent suites

### 1. An answer this build cannot read is unscoreable, not a score of zero

`native.audit` and `native.critique` both ask for a declared JSON block — a `findings` list, a
`verdict` field. A response that carries neither returns `score=None` with `AUDIT_UNPARSEABLE` or
`CRITIQUE_UNPARSEABLE`, and the sample stays visible in the counts.

*Alternative considered:* treat an unreadable answer as "found nothing" / "raised no criticism",
which would score it `0.0` and keep it in the aggregate.

*Reason:* reading a defect report or a verdict out of prose needs a judge, and these are rung-2
suites whose entire premise is exact ground truth. A model that described a real bug in a
paragraph has not failed to find it, and recording that it did would be a measurement of
formatting wearing a defect-detection metric's name. The honest reading is that we did not measure
this model on this case (ADR-0016).

### 2. Findings are matched to defects by line, with a two-line tolerance

`MATCH_TOLERANCE_LINES = 2`, and *localization* is scored separately from *detection*.

*Reason:* "did it find the bug" and "did it point at the right line" have different answers. A
mutated comparison is routinely reported against the `if` above it; scoring that as a miss would be
measuring line arithmetic. Two lines is wide enough for that and narrow enough that a finding
cannot drift onto a different defect in a dense function.

### 3. Audit's headline score is per-case F1 on mutated code and silence on clean code

A clean sample scores `1.0` when nothing was reported and `0.0` otherwise; a mutated one scores its
per-case F1.

*Reason:* the catalog's rule is that a model reporting many possible problems must not score well.
On a corpus of nothing but defects, flagging every line is perfect recall, so the rule is
unstateable without the clean half — and the clean half needs a scoring rule of its own, because
recall has no meaning where there is nothing to recall.

### 4. `native.judge` stores its trial record as the sample's response text

A judged case is a *set* of presentations — the same pair both ways round, the same question five
times, one answer with and without attribution. The interaction serializes them as canonical JSON
(`JudgeRecord.as_text`) and the scorer parses that.

*Alternatives considered:* a third scorer protocol taking the interaction's `detail`; computing the
bias figures inside the interaction.

*Reason:* the first would widen [ADR-0033](../docs/adr/0033-benchmark-interaction-protocol.md)'s
deliberate two-protocol split and needs an ADR; the second would put measurement arithmetic in a
conversation driver, where it could not be unit-tested against a synthetic judge. Serializing the
record keeps the scorer a pure function of `(expectation, text)`, which is exactly what makes a
position-biased judge a table-driven test rather than a field report.

### 5. `position_preference_rate` is a *direction*, not a magnitude

Among pairs whose verdict the swap moved, it is the share where the first position won. The
*magnitude* of position bias is `1 − swap_consistency`.

*Reason:* in a two-order swap, an inconsistent verdict means the same *position* won twice by
construction. A judge that always picks the second answer is exactly as biased as one that always
picks the first, and a single rate that called one of them "0.0 preference" would read as unbiased.

### 6. The shipped long-context sweep stops at 32 000 tokens

The catalog's range is 2K–128K "only those supported". This build ships 2K, 4K, 8K, 16K and 32K.

*Reason:* documents are expanded at suite-build time, which is startup and is also every test that
lists the available benchmarks. The shipped sweep costs about 600 KB of generated text and 50 ms;
adding 64K and 128K would multiply both for a figure that is already visible at 32K on the models
this tool is for. The sweep is data — a longer one is a case-file edit and a suite version bump.

### 7. One needle per long-context sweep, and per-needle distractors

Each *test* uses one needle across every point of its sweep, and the distractors that surround a
needle belong to that needle.

*Reason:* rotating needles across a depth sweep varies two things at once, so a dip at 16 000 tokens
would be indistinguishable from a fact the model finds harder to quote. And a distractor about a
different fact distracts from nothing, so a sweep padded with them would report a distractor
sensitivity the model never had the chance to show.

---

## Cross-cutting

### 8. `MetricDefinition` gains a declared `source`

`"auto"` (the default, and the behaviour every existing manifest already had), `"detail"` and
`"score"`.

*Reason:* benchmark catalog §5.1's resolution order ends with "the sample's score". That fallback
is safe only for a key no scorer would ever record. A *conditional* rate — `precision`, whose
denominator is empty when a model reports nothing — is absent from every sample the moment the
model reports nothing at all, and would silently become the mean score at exactly the moment its
real value was most interesting. Declaring `source: "detail"` removes the fallback for the metrics
that need it removed, and changes nothing for the ones that do not.

### 9. `effective_context_tokens` is an aggregate-only metric

Computed in `domain/metrics.py` and dispatched from `domain/aggregation.py`, alongside
`output_tokens_per_success`.

*Reason:* it is a property of a *set* of samples — the largest tested context still clearing a
share of the shortest-context baseline. There is no per-sample value for the three-source
resolution order to find, so it belongs with the other per-set formulas rather than in a fourth
mechanism.

---

## Phase 8A — goal packs

### 10. A goal suite's version carries its hash

`benchmark_suites` is keyed by `(key, version)` and a suite version is immutable, so a goal suite
installs as `<goal_pack_version>+<first 8 hex of goal_hash>`.

*Reason:* an author who edits a criterion without bumping `goal_pack_version` would otherwise reuse
the previous version's row, and new results would be attributed to the old manifest. Putting the
hash in the version makes ADR-0032 §4's hard separation *structural*: a measurement-defining edit
cannot land in the previous version's series, because it has a different version.

### 11. Two export formats, for two different readers

`GET /api/v1/goals/{slug}/export` returns the `benchmark.goal_pack` SetSpec envelope.
`freeweight goals export` writes a **bundle**: every file of the pack in one hash-pinned JSON
document, which is what `goals import` and `POST /goals/import` read.

*Reason:* the envelope is the cross-application contract and carries the goal's *definition* —
criteria, weights, rungs, task prompt identities and hashes. That is everything a consumer needs to
decide comparability and nothing an importer could rebuild a runnable pack from: a task's prompt
*text* is not in it, deliberately. ADR-0031 §6's "one hash-pinned bundle" is therefore FreeWeight's
own artifact, and it is a different thing from the envelope rather than a superset of it.

### 12. Three new stable error codes

`GOAL_PATH_UNSAFE`, `GOAL_HASH_MISMATCH` and `PROMPT_OVERRIDE_REFUSED`, extending spec §13's table.

*Reason:* the phase's own test list requires each import refusal to carry *its own* error code, and
spec §13 names three goal codes for five refusals. The first two are the goal-pack analogues of the
existing `DATASET_HASH_MISMATCH`; the third distinguishes "you have an override in place" from
every other conflict, which matters because the remedy is a specific flag. See
[`PHASE8_ISSUES.md`](PHASE8_ISSUES.md) §1 for the documentation change these imply.

### 13. `benchmark_suites.goal_id` gains no foreign key

The column exists (Phase 5 declared it) and is populated; the constraint is not added.

*Reason:* SQLite cannot add a foreign key without recreating the table, and `benchmark_suites` is
referenced by `runs.suite_id` with `ON DELETE RESTRICT`. Recreating a table that a populated `runs`
points at, to gain a nullable constraint the only writer already satisfies, is a materially riskier
operation than the constraint is worth. `services/goals.py` is that writer and holds the goal row
when it writes one. Recorded as an open item in `PHASE8_ISSUES.md` §2.

### 14. The regex dialect refuses unbounded repetition of a group

`(?:a|a?)+`, `(a+)+b`, `(a|ab)*c` and `(?:ab){2,}` are all refused at pack-load time. Bounded
repetition of a group and every quantifier on a character class are unaffected.

*Alternative considered:* the specification's own framing — a linted dialect plus a per-rule timeout
— with the dialect only refusing backreferences and nested unbounded quantifiers.

*Reason:* CPython's regex engine holds the GIL for the whole match, so **no in-process timeout can
interrupt one**. The timeout is a real guard for anything that yields, and worthless for the one
construction that actually runs away. The only effective moment to refuse an exponentially
backtracking pattern is before it is compiled, so that is where this build refuses it. See
`PHASE8_ISSUES.md` §3 for the consequence to the phase's stated test.

### 15. The Jinja2 environment is sandboxed

`services/prompts.py` now renders through `jinja2.sandbox.SandboxedEnvironment`.

*Reason:* spec §14 calls user-authored goal content "untrusted input to FreeWeight's own renderer"
and asks for no filesystem or network access in the environment. `StrictUndefined` and a missing
loader deliver neither on their own: `{{ ''.__class__.__mro__ }}` resolves in a stock environment
and is the first step of every published Jinja2 escape to `open`. An imported goal pack is somebody
else's file, so the sandbox is the thing §14 was actually asking for.

### 16. A task prompt carries its goal-task facts under `metadata.goal_task`

Its key, display name, render variables, annotated source and `is_starter` flag.

*Reason:* ADR-0031 §6 requires a task prompt to be a prompt record "in full". Keeping the
goal-specific fields inside `metadata` means the file loads through the same validator, renders in
the same sandbox, and hashes the same way — and it makes the annotated source a `goal_hash` input,
which it must be, because a rung-3 criterion scored against a different source is a different
measurement.

### 17. A slug is validated before any path is built from it

`import_bundle` and `write_pack` check `SLUG_PATTERN` before `mkdtemp`, and the staging directory's
name does not contain the slug.

*Reason:* found by the security test. The staging directory is created before the pack is parsed,
so waiting for `parse_pack` to reject a slug would already have passed it to the filesystem.
Security standards §4: an identifier used in a path is checked against the allowlist *before* any
filesystem call.

### 18. `GoalScorer` holds the jury, and is therefore not pure

Every other scorer in the application is a pure function.

*Reason:* `domain/scoring.py`'s own docstring anticipates this — "a scorer that needs a model is a
rung-5 judge, and rung 5 has its own machinery". Everything at rungs 2 and 3 stays pure, and a goal
with no judged criterion never touches the jury at all, which is why such a goal runs with the
provider down.

### 19. `score_method` breaks a tie towards the lower rung

A sample split evenly between rules and judgement is stored as `judge`.

*Reason:* `samples.score_method` holds one value and a composite is a blend. The whole blend is on
the same row as `score_method_mix`; the single value is a summary, and a summary that *understated*
how much judgement went into a number is the wrong direction to round in.

### 20. Rule-library choices that the specification names but does not fix

| Rule | Decision | Reason |
|---|---|---|
| every band | Outside the band, the score decays linearly to zero at **half the band's width** | The specification says "proportional" and stops. One curve, defined once, so two criteria that both declare a range cannot disagree about how quickly a miss decays |
| `vocabulary_profile` | "Rare" means **at least 12 characters**, unless the criterion supplies its own `common_words` | Shipping a frequency list would make the criterion a measurement of that list — its vintage, its corpus, its idea of English |
| `pov_tense` | Tense is read from auxiliaries, a short closed list of irregular pasts, and `-ed`. A simple-present sentence with no auxiliary is **undecidable and excluded** | Guessing tense from the absence of evidence is what a rule must not do. The share is over *decidable* sentences, and the count of undecidable ones is in the detail |
| `readability` | Refuses a response under **30 words** | These indices are ratios with tiny denominators in a short answer; a two-sentence reply lands anywhere from grade 3 to grade 18 on one long word |
| `punctuation_profile` | Marks are counted **literally**: a double hyphen is not an em dash | A rubric that means either says so with two bands |
| `repetition` | Default n-gram length **4** | Long enough that ordinary collocations do not dominate, short enough to catch a restated clause |
| `no_unsupported_claims` | A response containing no numbers and no capitalised entities is **`unsupported`**, not perfect | It has fabricated nothing, and it has also not been measured for faithfulness |
| every criterion | A `gate` fails when its raw score is below `1.0`; the lint *warns* when a gate sits on a rule that scores proportionally | A gate is for a disqualifying property, and putting one on `readability` would zero a sample a tenth outside the band |

---

## Phase 8B — calibration, the jury and the gate

### 21. Two judge prompt records, not one

`goals.judge.rubric` (ordinal) and `goals.judge.pairwise`.

*Reason:* the two modes have different response contracts — a grade on a scale, and a choice
between two labels. A single record could only carry both by putting the contract itself in a
variable, which would leave the prompt unable to describe its own output.

### 22. Inter-juror agreement is measured across samples, not within one

Krippendorff's alpha for a criterion is computed over every `(sample, repetition)` unit;
`JudgedCriterionResult.inter_juror_alpha` is `None` for a single-repetition sample.

*Reason:* alpha over a single unit is degenerate — it evaluates to `0.0` however well the jurors
agreed — so reporting it per sample would render unanimity as chance. Benchmark catalog §7.4's
figure is per *criterion*, which is where there are enough units for it to mean something.

### 23. The partition is stratified on one representative grade per sample

The median across that sample's per-criterion grades, rounded.

*Reason:* the partition has to span the scale, and the scale it has to span is "how good is this
sample overall". Stratifying per criterion would produce a different split per criterion, and the
holdout has to be one set.

### 24. `run_calibration` rebinds the jury to its own partition's anchors

Whatever anchors the caller assembled the jury with are replaced by the ones the partition just
produced, through `CalibrationJury.with_anchors`.

*Reason:* found by the leak test. The exemplars and the holdout are two halves of one computation;
if they can come from two, the way they disagree is by showing the jury a sample it is about to be
measured on. A newly added calibration sample also now defaults to `holdout`, so an unpartitioned
sample can never be rendered as an exemplar.

### 25. A rules-only goal returns `not_required` rather than raising `CALIBRATION_INSUFFICIENT`

*Reason:* nothing needs calibrating, so nothing can fail to — and nothing needs grading either.
Raising the insufficiency error would tell an author with a perfectly good deterministic rubric
that they have twelve samples to grade.

### 26. An uncalibrated goal reports `judge_validity_factor` as `1.0` on the run

*Reason:* an uncalibrated goal emits no evidence at all (ADR-0032 §3), so the factor never
multiplies into a confidence anybody reads. Reporting the *stored* factor keeps the number meaning
"this is what would multiply in", rather than encoding the gate a second time in a field that is
not the gate.

### 27. A calibration report is replaced, not appended

*Reason:* a report describes the instrument *as it now is*, and two reports for one goal would
leave every reader deciding which is current. The history that matters — what a particular run was
measured under — is on the run's own record.

### 28. `CalibrationJury` is a protocol

`run_calibration` takes the protocol, not `JuryService`.

*Reason:* the phase's own test list asks for "a deterministic fake jury whose bias is
configurable". Naming the seam makes such a double a first-class citizen rather than a cast, and
it is the seam that makes a generous juror and a scattered juror into test cases.

---

## Layout deviations from the phase's Files lists

| The plan says | This build uses | Why |
|---|---|---|
| `src/freeweight/repositories/goals.py` | `src/freeweight/infrastructure/db/repositories/goals.py` | Every other repository in the application lives there; a tenth in a new top-level package would be the only one |
| `src/freeweight/repositories/calibration.py` | `src/freeweight/infrastructure/db/repositories/calibration.py` | As above |
| `migrations/versions/xxxx_goal_tables.py` | `src/freeweight/infrastructure/db/migrations/versions/0005_goal_tables.py` | The repository's actual migration location |
| — | `src/freeweight/infrastructure/db/models_goals.py` | The ORM models the migrations and repositories need; the plan's Files list names the migration and the repository but not the mapped classes between them |
| `src/freeweight/prompts/goals/judge.rubric.v1.json` | that, plus `judge.pairwise.v1.json` | See §21 |
| — | `src/freeweight/benchmarks/goal/__init__.py`, `benchmarks/{audit,critique,judge,long_context}/__init__.py` | Package initialisers |
| — | `src/freeweight/benchmarks/long_context/{haystack,scoring}.py` | Inside the phase's own `benchmarks/long_context/*` glob |
| — | `src/freeweight/benchmarks/judge/session.py` | Inside the phase's own `benchmarks/judge/*` glob |
| — | `tests/e2e/test_goals_api.py` | Testing Standards §5 requires HTTP-route coverage — success, validation shape, not-found, error envelope, request-ID propagation — of every route, and the phase's Tests list names no API test |
| — | three `@pytest.mark.live` entries in `tests/live/test_real_run.py` | Phase 8's criterion 3 and Phase 8A's criteria 1–2 say "on a real model", and neither phase's Tests list names a live entry. Phase 7 resolved the same mismatch the same way ([`PHASE7_ISSUES.md`](PHASE7_ISSUES.md) §4). All three were run against a real 9.4B model and pass |
