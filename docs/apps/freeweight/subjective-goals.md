# FreeWeight — Subjective Goals

**Owner:** FreeWeight. **Status:** Specification.
**Decision records:** ADR-0031 (goal suites and the calibrated-judge instrument), ADR-0032 (judge validity, the gate, the `user.*` namespace).
**Related:** [Spec](spec.md) · [Benchmark Catalog](benchmark-catalog.md) · [Data Model](data-model.md) · [API](api.md)

---

## 1. The mental model, in one paragraph

A goal is a benchmark the user writes. It has **criteria**; each criterion is scored at the highest
rung of the [scoring ladder](benchmark-catalog.md) that can actually check it. Rules check what rules
can check — forbidden phrases, sentence-length distribution, point of view, structure. A jury of
models scores what only judgement can reach — voice, wit, register. Before any judged criterion is
believed, the user grades a dozen examples themselves, the jury is scored against the grades it was
never shown, and the resulting agreement becomes a permanent, visible property of every number the
goal produces. **A judged score without that agreement figure is not a measurement, and FreeWeight
will not export one.**

The user-facing sentence, which the wizard says on its first screen:

> You are building a measuring instrument. Rules are free and exact. A model's judgement is neither,
> so we are going to calibrate it against yours — you will grade about twelve examples, and we will
> tell you how closely the judge agrees with you. If it does not agree closely enough, that is a real
> and useful answer: your rubric is not measurable yet, and we will show you which part.

---

## 2. The goal pack

One directory per goal, JSON throughout (ADR-0019),
hand-editable, git-trackable, portable.

```text
$XDG_CONFIG_HOME/freeweight/goals/<slug>/
├── goal.json                  identity, criteria, weights, judge config, gate
├── tasks/
│   ├── 001.json               one task the candidate model answers
│   └── …
├── calibration/
│   ├── samples/               candidate outputs to be graded (model-produced or pasted in)
│   │   └── 0007.json
│   └── grades.json            the user's grades — the ground truth of the whole feature
├── prompts/
│   ├── judge.rubric.v1.json   the judge prompt record (ADR-0012 schema, no exceptions)
│   └── manifest.json
└── pack.json                  version, hashes, provenance, grader identity
```

### 2.1 `goal.json`

```json
{
  "slug": "noir_tech_voice",
  "name": "Noir-ish tech essay voice",
  "goal_pack_version": "1.2.0",
  "schema_version": "1.0",
  "intent": "Essays that sound like me: dry, concrete, unhurried. Not LinkedIn, not a manual.",
  "contributes_to": "creative_writing",
  "criteria": [
    {
      "key": "no_llm_tells",
      "name": "No LLM tells",
      "rung": "rule",
      "weight": 0.20,
      "gate": true,
      "rule": {"type": "forbidden_phrases", "case_sensitive": false,
               "phrases": ["delve", "leverage", "in today's landscape", "it's worth noting",
                           "navigate the complexities", "tapestry"]}
    },
    {
      "key": "sentence_rhythm",
      "name": "Varied sentence rhythm",
      "rung": "rule",
      "weight": 0.20,
      "rule": {"type": "sentence_length_distribution",
               "mean_words": {"min": 12, "max": 22},
               "cv": {"min": 0.45},
               "score": "proportional"}
    },
    {
      "key": "first_person_past",
      "name": "First person, past tense",
      "rung": "rule",
      "weight": 0.10,
      "rule": {"type": "pov_tense", "person": "first", "tense": "past", "tolerance": 0.15}
    },
    {
      "key": "dry_wit",
      "name": "Dry wit, never winking",
      "rung": "judge",
      "weight": 0.30,
      "scale": {
        "points": 5,
        "descriptors": {
          "5": "Wry and understated. The joke is in the observation, never announced.",
          "3": "Occasional flashes; mostly flat reportage.",
          "1": "Earnest throughout, or nudging the reader to notice it was funny."
        }
      }
    },
    {
      "key": "concrete_over_abstract",
      "name": "Concrete over abstract",
      "rung": "judge",
      "weight": 0.20,
      "scale": {"points": 5, "descriptors": {
        "5": "Claims are anchored to specific things: a number, an object, an incident.",
        "3": "Mixed; abstractions carry more than half the argument.",
        "1": "Abstraction throughout; nothing you could photograph."}}
    }
  ],
  "judge": {
    "jury_size": 3,
    "models": [],
    "repetitions": 3,
    "allow_remote": false,
    "prompt_id": "judge.rubric",
    "prompt_version": "1.0.0"
  },
  "calibration": {
    "target_samples": 12, "min_samples": 8,
    "holdout_fraction": 0.4, "partition_seed": 0,
    "min_agreement": 0.40
  }
}
```

Field rules mirror Prompt Standards §2.1: `slug`
is stable and never renamed (a rename is a new goal); `goal_pack_version` is semantic, with a
**major** bump for any change to criteria, weights, rung, rule parameters, scales or the judge
configuration — that is, anything inside `goal_hash`.

### 2.2 What `goal_hash` covers

`goal_hash` is the SHA-256 over the canonical JSON of the **measurement-defining** content only:
criteria (keys, rungs, weights, rule parameters, scale descriptors), task IDs and their rendered
prompt hashes, the judge prompt record hash, and the jury configuration.

It excludes display names, `intent`, `contributes_to`, grader identity and the calibration grades.
Renaming a criterion for readability must not separate a year of results; changing what it checks
must.

---

### 2.3 The bundle — the portable form of a pack

A pack is a directory, and a directory does not travel. `freeweight goals export` writes the whole
pack as **one hash-pinned JSON document**, and it is what `freeweight goals import` and
`POST /api/v1/goals/import` read.

```json
{
  "bundle_version": "1.0",
  "slug": "house_voice",
  "goal_hash": "sha256:…",
  "goal_pack_version": "1.2.0",
  "created_by": "the name the author gave, free text",
  "files": {"goal.json": "…", "tasks/001.json": "…", "prompts/judge.rubric.v1.json": "…"},
  "bundle_sha256": "sha256:…"
}
```

`files` maps each pack-relative path to its literal text — every file under the pack directory,
including the calibration samples and the author's grades where they exist. `bundle_sha256` covers
the file map, so a modified bundle is refused rather than imported.

An import validates **everything before it writes anything**: the document's shape, its total size
against `goals.max_pack_bytes`, every member's name for path containment, the bundle hash, and the
slug's availability. Only then is a temporary directory populated, parsed and linted; only then is
it moved into place. An import **never overwrites in place** — a colliding slug is refused with the
existing `goal_hash` named, and the caller renames.

**This is not `benchmark.goal_pack`, and the difference is load-bearing.** The SetSpec envelope at
`GET /api/v1/goals/{slug}/export` carries the goal's *definition* — criteria, weights, scales, judge
config, gate, hashes, and each task's prompt by id and hash — which is what another application
needs to understand what a `user.*` capability measured. It is not enough to rebuild a pack from:
an id and a hash are not the prompt text. The bundle is the pack; the envelope describes it.

| | Bundle | `benchmark.goal_pack` |
|---|---|---|
| Written by | `freeweight goals export` | `GET /api/v1/goals/{slug}/export` |
| Contains | Every file, verbatim | The definition, prompts by id + hash |
| Round-trips | Yes — this is what `goals import` reads | No |
| Audience | The author, and another FreeWeight | Any consumer of the contract |
| Versioned by | `bundle_version` | `schema_version` |

---

## 3. Criteria and the rule catalogue

Every criterion declares a rung. The lint in §7 flags a `judge` criterion a rule could check.

### 3.1 Rung 2 — rule criteria (deterministic, no model, no cost)

| `rule.type` | Checks | Scores |
|---|---|---|
| `forbidden_phrases` | Phrase/regex blacklist | 1.0 clean, else `1 − hits/max_hits`, or hard fail when `gate` |
| `required_phrases` | Must-appear terms, with `min_occurrences` | Fraction present |
| `word_count` | Total length band | In-band 1.0; proportional decay outside |
| `sentence_length_distribution` | Mean and coefficient of variation bands | Proportional to distance from band |
| `paragraph_shape` | Paragraph count and length bands | Proportional |
| `readability` | Flesch–Kincaid / Gunning fog band | Proportional |
| `pov_tense` | Grammatical person and tense consistency | Fraction of sentences conforming, against `tolerance` |
| `vocabulary_profile` | Type-token ratio, rare-word rate, banned register lists | Proportional |
| `punctuation_profile` | Em-dash, semicolon, exclamation rates per 1 000 words | Proportional |
| `structure` | Heading depth, list usage, code-block presence, Markdown shape | Boolean or fraction |
| `json_schema` | Output validates against a supplied schema | Boolean |
| `regex_match` | User pattern, linted dialect, `rule_timeout_ms` bounded | Boolean or count-scaled |
| `repetition` | N-gram self-repetition rate | Proportional, lower is better |

`gate: true` makes a rule criterion a **hard gate**: failing it zeroes the sample's composite score
and records `gated_by: <criterion_key>`. Use it for things that are disqualifying rather than
gradual — a forbidden phrase, invalid JSON.

### 3.2 Rung 3 — reference criteria (deterministic, needs user ground truth)

| `rule.type` | Checks | Needs |
|---|---|---|
| `entity_recall` | Named entities from the source appear in the output | An annotated source |
| `claim_coverage` | User-listed claims are covered | A claim list per task |
| `no_unsupported_claims` | Output entities/numbers all trace to the source | An annotated source |
| `reference_similarity` | Similarity to user reference outputs (recorded metric, no model) | Reference outputs |

These carry the summarization-faithfulness starter pack, where "did it make something up" is
answerable without judgement far more often than it first appears.

### 3.3 Rung 4 — human criteria

`rung: "human"` queues the sample for the user to grade in a blinded UI, recorded with
`score_method = "human"`. Validity is 1.0 by definition; throughput is one person.

**Status: the criterion validates, hashes, lints and is counted in `score_method_mix`; the run-time
grading UI arrives at [Phase 11](development-plan.md).** Until it does, a rung-4 criterion skips
every sample with `human_grade_pending` and contributes no score, and the applied weight shows the
shortfall rather than hiding it — a goal that declares one measures less of itself than it says,
visibly.

The *calibration* grading UI is a different surface and it does exist: §5.2's blinded grading of
calibration samples ships, in the wizard and as `freeweight goals grade`. What Phase 11 adds is the
second entry point, over an ordinary run's samples — and it lands there because that is the phase
where a human grade first has somewhere to go, which is evidence.

### 3.4 Rung 5 — judged criteria

An ordinal scale (3, 5 or 7 points) with descriptors for at least the top, middle and bottom points.
**A judged criterion with no descriptors fails validation** — "rate the tone 1–5" gives the jury
nothing to anchor on and reliably produces `kappa_w` near zero.

A judged criterion may instead declare `"mode": "pairwise"` with a reference set, in which case the
jury compares the candidate against a reference in both orders and the criterion scores a win rate.
This reuses `native.judge`'s bias controls directly and is the better choice when the user has
exemplars but no vocabulary for why they are good.

---

## 4. Scoring

### 4.1 Per sample

```text
for each criterion c:
    raw_c    in [0, 1]        rule/reference: computed; judge: (median juror score − 1)/(points − 1)
                              human: (grade − 1)/(points − 1)
composite = Σ(weight_c × raw_c) / Σ(weight_c)          … unless a gate criterion failed,
                                                          in which case composite = 0.0
```

Jurors are combined by **median**, not mean: a single juror misreading a rubric should not drag the
score, and the median is what the inter-juror agreement figure is reported against.

### 4.2 Per goal, per model

Composite mean with the full dispersion set from `native.reliability` — sd, CV, p50/p95 — plus, as
first-class headline metrics:

```text
score_method_mix          {rule: 0.50, reference: 0.00, human: 0.00, judge: 0.50}
inter_juror_agreement     Krippendorff's alpha across jurors, per judged criterion
judge_validity_factor     from calibration (§5.4)
gated_sample_rate         fraction of samples zeroed by a hard gate, with which gate
```

`score_method_mix` sits beside the score everywhere the score appears. A 0.82 that is 80 % rules is
a different kind of number from a 0.82 that is 80 % judgement, and the UI never shows one without
the other.

---

## 5. Calibration

### 5.1 Collecting samples

Calibration samples are candidate outputs for the goal's tasks. They come from any of:
running one or more models over the tasks (the wizard's default, and it deliberately uses a **spread**
of models so the set contains work of genuinely different quality), pasting in text the user already
has, or importing prior run samples.

A calibration set that is all excellent or all terrible cannot produce a meaningful agreement
figure — there is no variance to agree about. The wizard checks the user's grade distribution and
**says so** before computing anything: *"You graded eleven of twelve samples 4 or 5. Agreement
measured on this set will be unreliable. Add some weaker examples."*

### 5.2 Grading

The user grades each sample on each judged criterion, on that criterion's scale, in a blinded UI —
model identity hidden, sample order randomized. Free-text notes per grade are stored and are what the
disagreement diagnostics quote back.

Minimum 8, target 12. Below 8, `CALIBRATION_INSUFFICIENT`.

### 5.3 Partition

A seeded, recorded partition splits grades into **anchors** (default 60 %) and **holdout** (40 %),
stratified across the grade range so both halves span the scale.

* **Anchors** are embedded in the judge prompt as few-shot exemplars, with the user's grade and note.
* **Holdout is never shown to the jury, ever.** It is the only honest estimate of agreement, and
  leaking it would make the whole figure self-congratulatory.

The partition seed is recorded in the calibration report so the split is reproducible.

### 5.4 Agreement

The jury — the exact configuration the goal will run with — scores the holdout. Per judged criterion:

```text
kappa_w = 1 − Σ(w_ij × O_ij) / Σ(w_ij × E_ij)        w_ij = (i − j)² / (k − 1)²
```

quadratic-weighted Cohen's kappa between the user's grades and the jury median, over a `k`-point
scale: ordinal-aware (a 4-vs-5 disagreement counts far less than 1-vs-5) and chance-corrected.
Reported alongside it, always:

| | |
|---|---|
| `rho` | Spearman rank correlation — does the jury rank as the user ranks |
| `mae` | Mean absolute error in scale points — how far off, in units the user thinks in |
| `bias` | Mean signed error — does the jury run generous or harsh |
| `n_holdout` | **Never omitted.** `kappa_w` without its n is a number pretending to be a fact |

The gate, and the confidence factor:

```text
gate:      Σ(weight_c × kappa_w,c) / Σ(weight_c)  ≥  calibration.min_agreement   (default 0.40)
                over judged criteria only

validity:  v_c = 1.0                                                    rungs 1–4
           v_c = max(0, kappa_w,c) × min(1, sqrt(n_holdout / 10))       rung 5
           judge_validity_factor = Σ(weight_c × v_c) / Σ(weight_c)      clamped [0.05, 1.0]
```

Both are defined in ADR-0032 §2–3.
The shrinkage term is why six holdout samples at `kappa_w = 0.71` yield 0.55, not 0.71.

### 5.5 Interpretation shown to the user

The UI shows a band, not a bare coefficient, and states the consequence:

```text
kappa_w  ≥ 0.75    Strong. The judge tracks your grading closely.
         0.60–0.75 Good. Usable; expect the occasional sample you would score differently.
         0.40–0.60 Fair. Evidence is emitted, but confidence is reduced substantially.
         < 0.40    Not measurable yet. Results run and are inspectable; no evidence is emitted.
```

### 5.6 When it fails

The run still executes. The result is badged **UNCALIBRATED**. The diagnostics show, per criterion,
sorted by contribution to the disagreement:

* the criterion, its `kappa_w`, its `bias`, and how much of the goal's weight it carries;
* the three holdout samples where jury and user diverged most, side by side, with the user's own
  note and the jury's rationale;
* the lint's read on *why* — most commonly one of: no descriptors on the scale, descriptors that
  describe a topic rather than a quality, two qualities fused into one criterion, or a criterion
  a rule should have been checking all along.

**FreeWeight proposes no rewritten criterion text.** It names the problem and shows the evidence.
The rubric is the user's, and a model that edited it until it could measure it would be measuring
its own edit (ADR-0031 §3).

---

## 6. The jury

* Default 3 distinct local models, `temperature = 0.0`, `repetitions = 3`, case and criterion order
  randomized, candidate model identity hidden from the jury.
* Inter-juror agreement (Krippendorff's alpha) is a headline metric and feeds `consistency_factor`.
* **A juror never judges its own output.** Refused, recorded, not discounted.
* Every judged score links to each juror's own `native.judge` results — position bias, verbosity
  bias, self-preference — so "how good is this instrument" is one click away.
* A remote frontier juror requires `providers.allow_remote = true` **and** the goal's
  `judge.allow_remote = true`, is recorded in the fingerprint, and **separates** results from
  locally-judged ones.
* Changing the jury changes `goal_hash` and therefore separates results. This is not incidental: a
  new jury is a new instrument, and the old numbers were produced by the old one.

**The jury runs in its own phase, after every answer has been generated** ([spec
§7.4](spec.md)). The candidate is evicted first, so the jurors have the machine to themselves —
interleaved, a jury of three held four models resident at once on a machine chosen because it had
room for one. It costs the measurement nothing: a jury grades *stored text*, so when it reads
changes nothing about what it reads, and the verdict is identical either way.

It also makes a goal run's own resource figures mean something. Judging happens after the telemetry
window has closed, so peak VRAM and energy describe the candidate rather than whichever model
happened to be larger.

---

## 7. Authoring: what the application actually shows the user

`freeweight goals init` runs this as a terminal interview; the web wizard runs it as steps. Both
produce the same goal pack, and neither is required — a goal pack written by hand in an editor and
validated with `freeweight goals validate` is equally first-class.

**Step 1 — What are you trying to get?** Free text. *"Essays that sound like me: dry, concrete,
unhurried. Not LinkedIn, not a manual."* Nothing is inferred from this; it is stored as `intent` so
the goal is legible in six months.

**Step 2 — Break it into criteria.** The user lists qualities. The wizard asks two questions of each,
and these two questions are most of the value the wizard adds:

> *Could two people who both read your description grade the same essay the same way?*
> *Is this one quality, or two stuck together?*

"Not LinkedIn" is two things — a vocabulary problem and a register problem — and splitting it is
what makes both measurable.

**Step 3 — The application proposes rules.** For each criterion it offers the rule criteria that
could carry part of it, with the parameters pre-filled from the user's calibration samples where
they exist:

```text
"Not LinkedIn"
  → forbidden_phrases   suggested list of 14 terms, editable          [accept] [edit] [skip]
  → vocabulary_profile  banned register list                          [accept] [edit] [skip]
  Accepting both moves 20% of this goal's weight off the judge and onto rules.
  Rules are free, exact, and never disagree with you.
```

**Step 4 — Your tasks.** The user supplies prompts from their real work. Starter tasks are offered
and are explicitly labelled as things to replace: a voice measured on someone else's prompts is not
the user's voice. A goal still running entirely on unedited starter tasks is badged as such.

**Step 5 — Generate and grade.** FreeWeight runs a spread of models over the tasks, presents the
outputs blinded and shuffled, and the user grades them on their own criteria with notes. Progress is
saved continuously; grading twelve samples across five criteria is a real sitting and must survive
being interrupted.

**Step 6 — See the agreement.** The jury scores the holdout. The user sees §5.4's figures, §5.5's
band, and — this is the part that teaches — the samples where the jury disagreed with them, so the
number is attached to a felt experience rather than being an abstraction.

**Step 7 — Save.** The pack is written to disk. The wizard shows the path and the `goal_hash`, and
says plainly that the file is editable, diffable and portable.

---

## 8. Starter packs

Four ship with the application, complete with tasks, criteria, proposed rules and a worked
calibration set of graded samples. They make the feature demonstrable on a fresh install, and they
teach the shape of a good rubric by being read. **They are starters, not defaults**: a goal whose
criteria and tasks are unedited is badged `unforked` in the UI and in its results.

They are **read in this order**, and the order is the lesson:

| # | Key | Goal | Deterministic weight | Carries |
|---|---|---|---:|---|
| 1 | `starter.creative_voice` | Style and tone in creative non-fiction | ~40 % | The hardest case. Demonstrates that even "voice" partly mechanizes |
| 2 | `starter.technical_explanation` | Correct, well-pitched technical prose | ~55 % | `readability` band and `structure` as rules; correctness and audience-fit judged |
| 3 | `starter.brand_voice` | Compliance with a defined persona or house style guide | ~70 % | Banned terms, register, structure, reading level. The judged remainder is small |
| 4 | `starter.summary_faithfulness` | Coverage without fabrication | ~90 % | Mostly rung 3: `claim_coverage` and `no_unsupported_claims` against annotated sources. Shows that "did it make things up" is usually deterministic |

The pedagogy is deliberate. Read in this order the packs go **40 % → 55 % → 70 % → 90 %**
deterministic weight, strictly rising, which is the single most useful thing a user can internalize
about writing a measurable rubric: **the better you understand what you want, the less of it needs a
judge.**

The order is declared once, in `freeweight.goals.starters.READING_ORDER`, and both the CLI and the
starters page render from it — a reading order that is prose in one place and an implicit list
somewhere else is a reading order that drifts.

---

## 9. Failure modes this design accepts

| Mode | What happens |
|---|---|
| User grades everything 4–5 | Wizard refuses to compute agreement and says why (§5.1) |
| Rubric genuinely not measurable | Result runs, `uncalibrated`, no evidence, diagnostics name the criteria (§5.6) |
| User's taste drifts over months | Calibration record ages like evidence; `<app> health` surfaces it; re-grading is a normal act |
| Jury too small (few models installed) | `jury_reduced` recorded with reason; single-juror goals lose inter-juror agreement and say so |
| No provider at all | Rule criteria score normally; judged criteria `skipped`; partial result labelled |
| User games their own goal | Not defended against, and out of scope — the user is measuring for themselves |
| Two users compare "tone" scores | Separated by `goal_hash`; the UI refuses to merge and explains |
