# Phase 7 — issue register

Raised while implementing FreeWeight Phase 7 (deterministic quality suites) and resolved
immediately afterwards. Kept as a record of *what was decided and where the decision now lives*,
because the resolutions are spread across an ADR, four specification documents and the code, and a
reader who finds one of them should be able to find the rest.

**Status: 10 raised, 10 resolved.** Nothing here is outstanding. Two were resolved by *scheduling*
rather than building — both are marked as such, with the phase that owns them.

The authoritative record of each decision is the document named in its **Resolution** line. This
file is a map, not a source.

---

## 1. The interaction seam had no ADR ✅

**Was:** Three of Phase 7's suites need more than one provider call per sample. The run engine
executed exactly one, and no ADR described how a benchmark drives a conversation — while Phases 8B
(the jury) and 13 (external adapters) will both reach for the same seam.

**Resolution:** [ADR-0033 — Benchmark interactions, the two scorer protocols, and enforced
capability requirements](../docs/adr/0033-benchmark-interaction-protocol.md). It records the
`TurnCaller`/`Interaction` split and what each side owns, the step budget, the rule that control
flow advances on provider state and never on model text, the token-summing rule (and why provider
durations are *not* summed), the second `TrajectoryScorer` protocol and why the type-level split
rather than a widened signature, and where the code lives until a second consumer justifies
extraction. `benchmark-catalog.md §5.2` and the Phase 7 Work list now point at it.

**Noted in the ADR index:** this is the one ADR in the repository written *after* its code, which
is a departure from that directory's own rule. It says so.

## 2. `requires.provider_capabilities` was documented as enforced and was not ✅

**Was:** The `BenchmarkTest.requires` docstring had promised enforcement since Phase 5. Nothing
checked it, so `native.performance` had been declaring `token_counts` for two phases against
providers nobody asked.

**Resolution:** Enforced generically, not just for tool calling
([ADR-0033 §9](../docs/adr/0033-benchmark-interaction-protocol.md), `benchmark-catalog.md §5`,
Phase 7 Work list). The behaviour change to the Phase 6 suites is stated in the ADR's
*Consequences* as a negative, deliberately, rather than left to be discovered against a bare
OpenAI-compatible endpoint at Phase 4/13.

The loose end is closed too: every declared capability name is validated against
`ProviderCapabilities` when the registry is built, so a manifest saying `tool_calls` where it meant
`tool_calling` fails to launch instead of skipping its suite forever with a plausible reason.

## 3. Aggregation could only ever report one number per sample ✅

**Was:** A metric came from provider facts or from the sample's `score`. A suite declaring thirteen
metrics — which `native.tool_use` does — would have reported the headline score thirteen times.

**Resolution:** A third source, documented as a three-row table in
[`benchmark-catalog.md §5.1`](../docs/apps/freeweight/benchmark-catalog.md) and in
`data-model.md`'s `metric_values` section, and as [ADR-0033
§6](../docs/adr/0033-benchmark-interaction-protocol.md). The `SAMPLE_METRICS` docstring and
`_values_for` both describe the order and why the decision is made per *test* rather than per
sample.

## 4. Acceptance criterion 1 said "a real model"; the Tests list had no live entry ✅

**Was:** The plan's criterion and its own Tests list disagreed, so the criterion could not be
demonstrated.

**Resolution:** The Phase 7 Tests list now names the live test, and criterion 1 states how it is
demonstrated: a marked live run of all five suites on real weights, with the same five running
against `FakeProvider` in CI.

## 5. Prompt overrides were specified and unwired ⏭️ *scheduled*

**Was:** `load_pack` takes an `override_root`; nothing passes one, and prompt standards §6's
`--allow-prompt-override` refusal and fingerprint input do not exist.

**Resolution: scheduled at Phase 8A**, which is where user-authored content first reaches the same
loader, and is therefore where the refusal and the fingerprint input have a reason to exist beyond
completeness. The Phase 8A Work list carries the whole requirement; Phase 7's *Deferred* line says
it was left unwired here and why.

**Not built.** A `prompts` CLI that displayed overrides while runs ignored them would describe a
pack no benchmark renders, which is worse than the gap.

## 6. The sandbox root for `write_sandbox_file` was an invented temp directory ✅

**Was:** `toolbox_for` defaulted the write destination to `tempfile.gettempdir()`. No shipped case
writes, so nothing was ever created — but the default was wrong the moment one did, against
security standards §5's "inside the data root where possible, `0700`, cleaned up in a `finally`".

**Resolution:** Made unreachable by construction rather than by luck. `MockToolbox.sandbox_root`
is now optional, the default toolbox offers every tool *except* the writing one, and offering
`write_sandbox_file` without a root raises at construction. No default directory is invented; when
a case needs one, the run engine hands out a scoped sandbox and that is a decision with an owner.

## 7. The bounded JSON-Schema validator is the suite's second schema implementation ✅

**Was:** `domain/scorers/schema.py` decides a fixed keyword set and refuses everything else. No
`jsonschema` dependency was added. Whether it should be shared was undecided, and LoadCoach spec
§27 wants the same validation with a corrective retry.

**Resolution:** [ADR-0033 §8](../docs/adr/0033-benchmark-interaction-protocol.md) records the
decision and its trigger: it stays in FreeWeight now (one consumer, below
[ADR-0011](../docs/adr/0011-shared-package-boundaries.md)'s bar), and the extraction rides along
with the `setspec.prompts` work at LoadCoach P4 rather than adding a phase. The *Alternatives*
section records why a dependency was not added.

## 8. "Scoring a refusal as a failure of capability" was guarded but undocumented ✅

**Was:** Nothing on rung 2 can *detect* a refusal, and a scorer that tried would be a judge in a
rule's clothing. Two mechanisms were in place; neither was written down, and one of them —
storing a bounded excerpt of the answer — sat awkwardly beside spec §14's "responses are stored as
hashes by default".

**Resolution:** `spec.md §14` now states the carve-out explicitly and bounds it: scorer evidence in
`samples.result_json`, an answer excerpt capped at 200 characters, and tool results stored as a
hash and a short digest and never in full. The two mechanisms — the pre-run capability skip for
"cannot", the stored trajectory and answer for "will not" — are asserted in
`tests/unit/test_scorers_tools.py::TestARefusalIsNotACapabilityFailure`.

## 9. Smaller notes ✅

* **The shared modules and the tooling exclusions** — `benchmarks/loading.py`,
  `benchmarks/interaction.py`, and the ruff/mypy exclusions for the fixture repository — are now in
  the Phase 7 *Files/subsystems* list, so the deviation is in the plan rather than only in a
  review note.
* **The language constraint is a script check.** `benchmark-catalog.md §3.4` now says so, and says
  why: distinguishing Spanish from Portuguese needs a classifier or a model, and a language
  constraint that needed more than a Unicode script check would be asking for a rung this suite
  does not have.
* **"Parallel independent tools" means order-independent, not concurrent, in 1.0.**
  `benchmark-catalog.md §3.6` now says so and points at the same missing concurrent execution path
  as §3.1's optional concurrency-scaling row. ADR-0033's *Revisit when* names it as a trigger.
* **`SKIP_UNSUPPORTED_CAPABILITY` is exported** from `freeweight.services.runs`.

## 10. `tool_calls` was specified and unbuilt ✅ *(found while resolving the others)*

**Was:** `data-model.md §2` has specified a `tool_calls` table since the freeze — one row per
invocation, with `expected_tool`, `correct_tool`, `correct_arguments` and a `result_hash`. No
migration created it, and Phase 7 shipped three tool suites that stored their trajectories only as
a JSON blob on the sample. The drill-down the table exists for — *which* call named the wrong tool,
with what arguments — was not queryable, and Phase 10's results experience would have had to
backfill it.

**Resolution:** Built, to the column set the data model already specified: migration `0004`, the
`ToolCall` model, `ToolCallRepository`, and rows written in the *same transaction as the sample* so
a trajectory can never be read back shorter than the sample it belongs to. `data-model.md` gained
the rules the columns imply — a hallucinated tool is a row with `status = "unknown_tool"` and not a
missing one; `correct_tool` is `NULL`, never `false`, where the case declares nothing to compare
against; the result is hashed and never stored. The comparison is
`domain.scorers.tools.annotate_calls`, the per-call view of the same greedy pairing the metrics
aggregate, so a stored row and the rate computed over it cannot disagree.

---

## Also fixed, unrelated to the above

`tests/e2e/test_run_journey.py` carried a one-in-many race that surfaced during this work. A run
reaches its terminal status in the database *before* its terminal event is published —
deliberately, so a client can never see a closed stream without a terminal frame — and the test
compared event counts across that window. The test now waits for the stream to go quiet. **The
ordering it was testing is unchanged**; the flake was in the test.
