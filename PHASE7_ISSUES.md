# Phase 7 — issues that need to be addressed

Written while implementing FreeWeight Phase 7 (deterministic quality suites). Everything here is
either a **defect in the documentation** that the phase forced a decision on, or a **deliberate
gap** a later phase or a follow-up must close. Nothing here blocks the phase's acceptance
criteria; each item names what was decided and why, so the decision can be ratified or reversed.

Ordering is by how much a wrong answer would cost.

---

## 1. The development plan's own file list is incomplete for its Work list — no ADR covers the seam

**What happened.** Phase 7 asks for tool-use, tool-recovery and agent suites. All three need more
than one provider call per sample: ask, run the tools the model asked for, hand the results back,
ask again. The run engine as of Phase 6 executes one `generate`/`stream` call per case and scores
`result.text`. Nothing in the plan's "Files/subsystems" list mentions `services/runs.py`, and no
ADR describes how a benchmark drives a multi-turn conversation.

**What was decided.** A narrow seam, named explicitly in the diff:

* `freeweight/benchmarks/interaction.py` (new) defines `TurnCaller` ("here is a conversation, give
  me the next assistant turn"), `Interaction` and `InteractionOutcome`, plus the two drivers
  (`ToolSession`, `StructuredOutputSession`).
* `services/runs.py` gained `_run_interactive_case`, which supplies the caller under the run's
  frozen execution config, sums token counts across the turns and stores one sample.
* A benchmark test declares `interaction`; the engine dispatches on its presence rather than
  inferring it from the scorer.

**Why it needs an ADR.** CLAUDE.md's rule is explicit: *"If an architectural decision seems
missing, that is a defect in the docs — close it with a new ADR before writing code."* This is a
new extension point on the run engine that Phases 8, 8A/8B (goal suites, judges) and 13 (external
adapters) will all reach for. It should be ADR'd before those phases build on it, and
`spec.md §7`'s public-API section should name it.

**Also affected:** `domain/scoring.py`'s `Scorer` protocol is `score(case, response_text)`, which
cannot express a trajectory. Rather than widen it (and touch every existing scorer), a sibling
`TrajectoryScorer` protocol with `score_trajectory(case, transcript)` lives in
`domain/scorers/tools.py`. Two scorer protocols is a decision an ADR should record, not a detail
to be rediscovered at Phase 8.

---

## 2. `requires.provider_capabilities` was documented as enforced and was not

**What happened.** `domain/benchmark.py`'s `BenchmarkTest.requires` docstring has said since
Phase 5 that requirements are *"checked before the test runs; an unmet requirement skips the test
with a recorded reason"*. Nothing checked them. `native.performance` has been declaring
`token_counts` and `streaming` for two phases against providers that were never asked.

**What was decided.** Phase 7's capability-gating Work item is implemented generically, not just
for tool calling: `_unmet_capabilities` matches the names in `requires["provider_capabilities"]`
against `ProviderCapabilities` field names, and an unmet requirement makes the test `skipped` with
`skip_reason = "unsupported_capability"` before it runs.

**What needs attention.** This changes the behaviour of the *Phase 6* suites too. On a provider
without `token_counts`, `native.performance` and `native.token_economy` now skip rather than
producing `UNSUPPORTED`-heavy results. That is almost certainly the right behaviour and it is what
the docstring always promised — but it is a behaviour change to shipped suites made under a
Phase 7 heading, and someone should confirm it is wanted rather than discover it against a bare
OpenAI-compatible endpoint at Phase 4/13.

An unrecognised capability name is treated as **unmet**, not as satisfied. That is the honest
reading of "I cannot tell", but it means a typo in a manifest silently skips a suite. A
`build_registry`-time validation that every declared capability name exists on
`ProviderCapabilities` would turn that into a startup error and is worth adding.

---

## 3. Aggregation could only ever report one number per sample

**What happened.** `domain/aggregation._values_for` derived a metric either from
`SAMPLE_METRICS` (provider counts and timings) or from the sample's `score`. A suite declaring
thirteen metrics — which `native.tool_use` does, per benchmark catalog §3.6 — would have reported
the same headline score under all thirteen keys.

**What was decided.** A third source, between the two: if any completed sample in a group carries
a number under the metric's key in its scorer detail, the metric is *detail-derived* for the whole
group, and a sample carrying none is excluded with `not_measured_for_this_case`. The
group-not-sample decision is deliberate — a per-sample fallback would let a missing key silently
resolve to the headline score.

**What needs attention.** `data-model.md §2`'s `metric_values` description and
`benchmark-catalog.md §5`'s manifest section should say that a suite's metrics may come from its
scorer's detail, and `domain/metrics.py`'s `SAMPLE_METRICS` docstring should point at the third
source. As written, the docs describe two sources and the code has three.

---

## 4. Acceptance criterion 1 says "against a real model"; the phase's Tests list has no live entry

**What happened.** Phase 6's Tests list ends with an explicit live entry ("Live (marked): a real
short run on Ollama…"). Phase 7's does not, yet its acceptance criterion 1 reads *"Five
deterministic suites run end to end against a real model and produce interpretable metrics."*

**What was decided.** `tests/live/test_real_run.py` — an existing file, not one Phase 7 lists —
gained `test_the_five_quality_suites_run_end_to_end_on_a_real_model`, marked `live` and therefore
excluded from CI. It asserts only that each suite completes and that every metric it produced is
under a key its manifest declares; it asserts nothing about the values, because what a particular
local model scores is the thing being measured.

**What needs attention.** Either the plan's Tests list should name that live test, or acceptance
criterion 1 should say "against the fake provider, with a live smoke test". Right now the two
disagree, and the criterion cannot be demonstrated by the default suite.

---

## 5. Prompt standards §6 (overrides) is specified and unwired

**What happened.** `services/prompts.load_pack` takes an `override_root` and marks overridden
records `source="user_override"`. Nothing passes it: `shipped_prompt_library()` loads the shipped
pack, and so does `freeweight prompts list|show`. Prompt standards §6 further requires that
*"FreeWeight refuses to run a benchmark with an overridden prompt unless `--allow-prompt-override`
is passed, and records the override in the reproducibility fingerprint when it is."* Neither the
flag nor the refusal exists.

**What was decided.** The `prompts` CLI deliberately reads the *shipped* pack, so what it prints
is what a run would actually render. Wiring overrides into the CLI while runs ignored them would
have made the CLI describe a pack no benchmark uses.

**What needs attention.** This is a real gap in a shipped standard, and it is a provenance gap:
today a user can drop a file into `$XDG_CONFIG_HOME/freeweight/prompts/` and nothing loads it, but
the moment something does, results become incomparable with no flag guarding it and no fingerprint
input recording it. It belongs in whichever phase wires configuration to the pack root — plausibly
alongside the goal-pack work at Phase 8A, which renders user-authored templates through the same
loader.

---

## 6. `native.tool_use` has no `write_sandbox_file` case, and the sandbox root is a temp directory

**What happened.** The mock toolbox implements `write_sandbox_file` (benchmark catalog §3.6 lists
it), and the containment tests exercise it thoroughly. No shipped *case* calls it, because none of
the catalog's eleven scenarios needs a write.

**What was decided.** `toolbox_for` defaults the sandbox root to `tempfile.gettempdir() /
"freeweight-tools"`, created only on first write — which no shipped case triggers.

**What needs attention.** The moment a case does write, that default is wrong: security standards
§5 wants temporary files *inside the data root where possible*, `0700`, and *"always cleaned up in
a `finally`"*. The right fix is for the run engine to hand each run a scoped sandbox directory and
delete it when the run ends, which is a change to `services/runs.py` this phase's Work list does
not cover. Until then, no case writes and nothing is left behind.

---

## 7. The bounded JSON-Schema validator is FreeWeight's second schema implementation

**What happened.** `domain/scorers/schema.py` implements a JSON-Schema subset — `type`,
`properties`, `required`, `additionalProperties`, `items`, `enum`, `const`, the numeric and length
bounds, `pattern` — and **refuses** anything else rather than ignoring it. No `jsonschema`
dependency was added.

**Why.** Adding a runtime dependency is outside the phase's Work list, and a validator that
silently skipped `oneOf` would report a conformance rate for a check it never performed — worse
than having no rate. The refusal is asserted in `tests/unit/test_scorers_schema.py`.

**What needs attention.** ADR-0009 puts pydantic and generated JSON Schema at the centre of
SetSpec, and §5 of the prompt standards moves the prompt loader into `setspec.prompts`. If a
schema *validator* is also going to be shared — LoadCoach's spec §27 wants "JSON, JSON Schema,
required fields, regex, length" validation with corrective retry, which is the same code — it
should be decided now where it lives, before there are two of it. The same module is already
reused by the mock toolbox to validate tool arguments.

---

## 8. "Scoring a refusal as a failure of capability" is guarded by evidence, not by detection

**What happened.** The phase names this as a likely failure mode. Nothing on rung 2 can *detect* a
refusal — telling "I would rather not" apart from "I have no idea" is a judgement, and a scorer
that tried would be a rung-5 judge wearing a rule's clothes.

**What was decided.** Two mechanisms, neither of them a heuristic:

* A model that *cannot* call tools never reaches a scorer at all — the capability gate skips the
  test before any sample exists, so "cannot" and "will not" are structurally different outcomes.
* For "will not", the evidence is kept: every tool sample stores its whole trajectory *and* a
  bounded excerpt of the final answer, so two zero-scoring samples are told apart by a person
  reading one sample. Asserted in `tests/unit/test_scorers_tools.py::TestARefusalIsNotACapability
  Failure`.

**What needs attention.** The answer excerpt is capped at 200 characters, which
`ScoreResult.detail`'s own contract ("the expected and actual values") sanctions and spec §14's
"responses are stored as hashes by default" arguably does not. The two documents should be
reconciled: either §14 should carve out bounded scorer evidence, or the excerpt should be gated on
the run's `store_responses` — in which case a run without it loses the only thing distinguishing a
refusal from confusion, and the failure mode comes back.

---

## 9. Smaller notes

* **`benchmarks/loading.py` and `benchmarks/interaction.py` are new shared modules** under
  `src/freeweight/benchmarks/`, which the plan's file list writes as
  `benchmarks/{instruction_following,…}/*`. Five copies of "load the manifest, verify the subset
  hash, render the cases" would have been five places for a suite to drift in how it attributes a
  prompt. Named here so the deviation is visible.
* **`pyproject.toml` gained two exclusions** (`ruff` `extend-exclude`, `mypy` `exclude`) for
  `src/freeweight/benchmarks/fixtures/data/`. The mock-tool fixture repository is a real directory
  on disk — the containment tests have to defeat real symlinks — and a formatter pass over it would
  rewrite the bytes the tools read.
* **The `language` constraint is a *script* check.** Benchmark catalog §3.4 lists a "language
  constraint"; deciding Spanish from Portuguese needs a classifier or a model, and neither is
  available on rung 2. `ConstraintKind.SCRIPT` asserts that every cased letter belongs to a named
  Unicode script, which is deterministic and is what "answer in Greek" can honestly mean. The
  catalog should say so.
* **Concurrency and `native.tool_use`'s parallel scenario.** The catalog's "parallel independent
  tools" scenario is modelled as *order does not matter*, not as *calls issued concurrently*: the
  run engine has no concurrent execution path (noted at Phase 6 for the same reason). If the
  catalog means genuine concurrency, the scenario is not yet implemented.
* **`SKIP_UNSUPPORTED_CAPABILITY` is not exported** from `freeweight.services.runs.__all__`. The
  web and CLI surfaces read `skip_reason` as a string today; when they start branching on it, it
  should be a shared constant rather than a literal in three places.
