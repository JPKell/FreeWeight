# FreeWeight — Benchmark Catalog

**Owner:** FreeWeight. **Status:** Specification of what is measured and how it is scored.
The 1.0 scope is marked per entry; anything unmarked is a documented future extension.
**Related:** [Subjective Goals](subjective-goals.md) — the user-authored suites in §7.

---

## 1. Scoring ladder

Preference order, always. A lower rung is used only when every higher rung is impossible, and the
rung used is recorded on the result.

```text
1. Executable verification        run the code, run the tests
2. Rule-based verification        exact match, regex, JSON Schema, structural constraints
3. Reference verification         compare against ground truth with a documented metric
4. Human evaluation               explicit, recorded, never silently mixed with automatic scores
5. LLM-as-judge                   last resort; judge model, prompt version and bias controls recorded
```

An LLM never judges what a deterministic method can verify. Where a judge is used, the judge model's
own judge-benchmark results are linked from the result, so a user can see how trustworthy that judge
is.

**Rung 5 has a precondition.** A judge is an *instrument*, not an oracle: it may take a measurement
only when its agreement with user-supplied ground truth has been measured and is reported alongside
every number it produces. An uncalibrated judge is an opinion with a decimal point, and FreeWeight
exports no capability evidence from one
(ADR-0031,
ADR-0032 §3). This is also the line
that reconciles rung 5 with Testing Standards §3: no model
ever decides whether FreeWeight's own code is correct, or FreeWeight's control flow. That ban is
untouched.

---

## 2. Categories

There is **no default universal score**. Category scores exist; a user may define weighted profiles
(for example "coding agent", "low-VRAM assistant", "overnight reasoner") and every profile shows its
weights and drills to raw measurements.

```text
Performance        Memory & KV cache     Token efficiency     Energy efficiency
Reasoning          Instruction following Coding               Code reasoning
Tool use           Agent behaviour       Auditing             Critiquing
Judging            Long context          Reliability          Structured output
User-defined goals (§7)
```

---

## 3. Native suites (1.0 scope)

**Every metric in this section is implemented and shipping.** Figures that were declared here and
owned by no phase have moved to [§3.15](#315-deferred-to-a-later-release), because this section is
titled "1.0 scope" and a metric with no owner does not belong in it. An unqualified metric list
reads as a contract: §6's `creative_writing` row was a dangling capability for exactly as long as
nothing said which figures were owed and by when.

Native suites have no external dependency, ship with the application, and are the reason FreeWeight
is useful on a fresh install.

### 3.1 `native.performance` — Performance

| Test | Method | Key metrics |
|---|---|---|
| Cold model load | Force unload where supported, then load | `load_ms`, peak RAM/VRAM during load, disk read |
| Prompt processing | Prompt sizes 128, 512, 1K, 2K, 4K, 8K, 16K, 32K, 64K (only those the model supports) | `prompt_tokens`, `prompt_eval_ms`, `prompt_tokens_per_second` |
| Decode throughput | Fixed output lengths 32, 128, 256, 512, 1024 | `output_tokens`, `decode_ms`, `decode_tokens_per_second` |
| Combined request | Realistic prompt + generation | `total_ms`, `ttft_ms`, prompt and decode throughput |
| Streaming latency | Measure inter-chunk timing | `ttft_ms`, mean/p50/p95 inter-chunk ms — labelled *chunk* latency unless `token_level_chunks` is true |
| Concurrency scaling (optional) | 1, 2, 4, 8 simultaneous requests | Aggregate and per-request throughput, p50/p95/p99 latency, peak VRAM/RAM |

Hygiene rules: warm-up repetitions excluded from headline numbers; cold and warm results never mixed;
optional idle-detection before measurement (GPU and CPU below threshold for N samples); every
measurement labelled `cold` | `warm` | `cache_reused`.

### 3.2 `native.memory_kv` — Memory and KV cache

| Test | Method | Key metrics |
|---|---|---|
| Theoretical KV requirement | From descriptor architecture fields: `2 × layers × kv_heads × head_dim × bytes_per_element` | `theoretical_kv_bytes_per_token` (or `unsupported` when fields are missing) |
| Observed context slope | Equivalent generations at 1K…64K context; stabilized VRAM before generation | `observed_kv_bytes_per_token`, `observed_mb_per_1k_context`, fit quality |
| Runtime overhead ratio | observed ÷ theoretical | `kv_overhead_ratio` — a *runtime efficiency* figure, never a quality figure |
| Maximum context fit | Increase context until OOM, rejection or the configured limit | `max_successful_context_tokens` |
| KV precision comparison | Where the runtime supports f16/q8/q4 | VRAM, max context, throughput, quality delta per precision |
| Cache reuse | Long shared prefix, several short follow-ups | Cold vs warm prefill ms, `reuse_speedup`, cached-token count where exposed |

Hybrid/state-space architectures are flagged and excluded from the transformer formula rather than
forced through it. Every metric here is `unsupported` when telemetry is unavailable.

Every metric in this suite is attributed to `execution.gpu_index` and carries it. Where more than one
GPU is visible and the provider does not report placement, the whole suite is **skipped** with
`multi_gpu_placement_unknown` — a VRAM slope measured against the wrong device reads as zero bytes per
token, which is a fabricated measurement, not an approximate one
(ADR-0027 §3). The context axis of every test is the
**served** context, recorded with its source, not the advertised maximum.

### 3.3 `native.token_economy` — Token efficiency

Collected automatically on **every** benchmark, not only here: input/output/thinking/tool tokens,
characters, words, bytes, turns, tool calls.

Derived: `output_tokens_per_success`, `total_tokens_per_success`,
`quality_per_1k_output_tokens`, `successes_per_million_output_tokens`.

Because tokenizers differ, token counts are never compared across models without the character and
byte counts alongside them — the UI shows both, and cross-tokenizer comparisons are labelled.

### 3.4 `native.instruction_following`

Deterministic constraint checks: exact JSON structure, exact list length, required phrase, forbidden
phrase, word-count range, specific opening/closing text, multiple simultaneous constraints, language
constraint, formatting constraint.

The **language constraint is a script check**: every cased letter must belong to a named Unicode
script ("answer in Greek"). Distinguishing Spanish from Portuguese needs a classifier or a model, and
neither is available on rung 2 — a language constraint that needed more than this would be asking for
a rung this suite does not have.

Metrics: strict prompt accuracy, loose prompt accuracy, instruction-level accuracy, and violation
counts by class (format, length, keyword, structure, language).

### 3.5 `native.structured_output`

JSON-mode and JSON-Schema conformance: valid JSON rate, schema-conformance rate, required-field
presence, type correctness, enum adherence, nesting correctness, and recovery rate after one
corrective retry. Capability-gated: a provider without structured output records `unsupported`, not a
failure.

### 3.6 `native.tool_use`

Mock tools over deterministic fixtures — calculator, `read_file`, `list_directory`, `search_text`,
`search_symbol`, `lookup_record`, `database_query`, `get_inventory`, `write_sandbox_file`,
`run_mock_test`. No shell, no real filesystem, no network.

Scenarios: one correct tool; several similar tools; no tool required; tool required; sequential
tools; parallel independent tools; invalid argument; tool failure; empty result; ambiguous result;
tool unavailable.

**"Parallel independent tools" means order-independent, not concurrent, in 1.0.** The run engine has
one synchronous execution path (ADR-0003); the scenario
is scored with ordering accuracy switched off, so a model that answers both halves in either order is
correct. Genuine concurrency is the same gap as the optional concurrency-scaling row in §3.1 and
would need its own decision about what a concurrent trajectory's timings mean
(ADR-0033, "revisit when").

Metrics: tool-selection accuracy, argument schema validity, argument semantic correctness,
unnecessary-call rate, missed-tool rate, hallucinated-tool rate, multi-tool sequence accuracy,
ordering accuracy, calls per success, redundant-call rate, repeated-identical-call rate, task success.

### 3.7 `native.tool_recovery`

Deliberate tool failures — file not found, invalid argument, empty search, permission denied, tool
timeout, ambiguous result. Metrics: recovery success rate, retry count, repeated-error count,
recovery tool count, recovery latency, recovery token count.

### 3.8 `native.agent`

Multi-step goals over deterministic tools: find a symbol → read its definition → locate callers →
answer; diagnose a failing test from logs and source; locate a misconfiguration and propose a change;
combine results from several mock tables.

Metrics: task success, steps to completion, tool calls, wrong turns, retries, tokens, wall time,
recovery rate, unnecessary actions.

### 3.9 `native.audit`

Mutation-based corpus over known-correct, unit-tested code. Mutations with exact ground truth:
`<`→`<=`, `>`→`>=`, `==`→`!=`, wrong variable, wrong constant, off-by-one, removed guard, missing
return, wrong branch, swapped arguments, removed exception handling, wrong boolean operator, wrong
loop boundary, unreachable code, wrong function call, removed resource cleanup, transaction/lock
misuse. **Clean samples are included deliberately.**

Metrics: precision, recall, F1, **clean-code false-positive rate**, clean-code silence rate,
defect-detection score, file/function/line localization. Seven further figures are deferred
([§3.15](#315-deferred-to-a-later-release)).

A model that reports many possible problems must not score well. Precision and the false-positive
rate are shown next to recall everywhere.

### 3.10 `native.critique`

Input: question, candidate response, known correctness. Metrics: error-detection recall, criticism
precision, hallucinated-criticism rate, error localization, explanation accuracy, valid-correction
rate, **correction uplift** (post-correction accuracy − original accuracy) and **regression rate**
(correct answers made incorrect by the critic). Regression rate is a headline metric, not a footnote.

### 3.11 `native.judge`

| Test | Method | Metrics |
|---|---|---|
| Pairwise correctness | Question, answer A, answer B, gold preference | Pairwise accuracy |
| Position bias | Same pair in both orders | Swap consistency, position preference |
| Repetition stability | Same comparison repeated | Agreement rate |
| Verbosity bias | Concise correct vs verbose weaker | Verbosity preference rate |
| Style bias | Content held constant, style varied | Style preference rate |
| Transitivity | A>B, B>C ⇒ A>C | Transitivity violation rate |
| Self-preference | Own answer vs another's, anonymized and not | Self-preference delta |

### 3.12 `native.long_context`

Context depths 2K…128K (only those supported). Retrieval at 10/25/50/75/90 % depth; distractor
sensitivity (0K…64K of irrelevant context); distributed reasoning across distant facts.

Metrics: accuracy by context length, by information position and by distractor volume;
`effective_context_tokens` (largest tested context where accuracy ≥ a configurable fraction — default
80 % — of the short-context baseline, with the threshold recorded); `longest_tested_context_tokens`.
Four further figures are deferred ([§3.15](#315-deferred-to-a-later-release)).

Advertised context and effective context are always displayed as separate numbers — and so is
`longest_tested_context_tokens`, because the two are indistinguishable without it. A model that
answers correctly at every length the sweep probes has an effective context *at least* that large;
reporting the sweep's own ceiling as the model's limit would turn the edge of the measurement into a
property of the model.

The depth sweep's ladder doubles to a ceiling of **32 000 tokens by default**, and the ceiling is
configuration (`benchmarks.long_context_max_tokens`) rather than a constant: how far a sweep can
reach is a property of the machine, and a card that can serve 128 000 tokens should be able to
measure them. The **effective** ladder is hashed into this suite's `dataset_hashes`, so two
ceilings are two measurements and are never averaged. Only the depth sweep stretches — the position,
distractor and distributed-reasoning tests hold context constant on purpose, and stretching them
would change what they measure rather than how far they reach.

### 3.13 `native.reliability`

Cross-cutting: every benchmark runner supports repeated trials and stores **all** repetitions.
Reported: mean, median, min, max, standard deviation, coefficient of variation, p50/p95/p99 where
meaningful; `pass@1` and `pass@k` where applicable; answer/tool-call/judge agreement for repeated
stochastic tests.

Outliers are never silently discarded. Any exclusion is explicit, reasoned and preserved in the raw
data.

### 3.14 `native.energy` (telemetry-derived estimate)

From persisted power samples for `execution.gpu_index`: `energy_joules ≈ Σ(power_watts × dt_seconds)`
using real sample timestamps, **integrated only over the requests**. Each interval between two
readings is clipped to the union of the run's own sample windows — `[samples.started_at,
samples.created_at]` — so the settle wait, the warm-up generations and the inter-test cooldowns are
excluded. Clipping rather than filtering is what makes it correct: a reading taken inside a request
whose *next* reading falls after it would otherwise carry the whole idle gap at the request's power
level, which is the largest error the naive version makes. "Joules per output token" is therefore an
absolute figure rather than one that is merely consistent between two runs of the same suite.
`peak_gpu_power_watts` follows the same rule, because a peak drawn from a warm-up is not this
suite's subject and reporting one figure over requests beside another over the whole run would be
two answers to one question. There is no machine-wide GPU energy figure; per-device series are
reported separately and never summed into one number that no device produced. Metrics: joules per request, per output token, per successful task; tokens per joule;
successful tasks per kWh; mean and peak GPU power; max GPU/CPU temperature; suspected throttling.

Always labelled a telemetry-derived estimate, never instrumentation. `unsupported` when power
readings are unavailable.

---

### 3.15 Deferred to a later release

Eleven figures were specified in this catalogue before anything measured them, and no phase of the
[development plan](development-plan.md) ever took them. They are recorded here rather than deleted,
because *what was considered and not built* is worth as much to the next reader as what was — but
they are **out of 1.0 scope**, and nothing above promises them.

| Suite | Deferred | Blocked on |
|---|---|---|
| `native.audit` | bug-category accuracy, severity accuracy, explanation correctness, suggested-fix correctness | A graded defect taxonomy the mutation corpus does not carry: each mutation would need a category and a severity that a human agreed to, and inventing them at generation time would measure the generator |
| `native.audit` | patch compile rate, patch test-pass rate, regression rate | A sandbox that applies a patch and runs a test suite. The sandbox exists (ADR-0018) and nothing drives it this way; the missing part is a corpus whose projects build and test inside it |
| `native.long_context` | accuracy AUC across context | Nothing technical — it is a summary of the depth sweep this suite already reports, and a single number that hides the shape of the curve is worth less than the curve |
| `native.long_context` | latency, VRAM and prompt throughput by context | A per-context resource series, which is a *study across runs* now that context is configuration ([spec §12](spec.md)) rather than a sweep inside one — see `results compare`'s context sweep, which measures exactly this for VRAM |
| `native.judge` | repetition-stability variance | The agreement rate ships; the variance beside it needs the per-repetition spread retained at aggregation, which `judge_verdicts` already stores and no metric reads |

**The trigger to revisit is a second consumer, not a spare afternoon.** The patch metrics become
worth building when something else needs the sandbox to run a project's tests — Phase 13's external
adapters are the candidate. The long-context resource series is already partly answered by the
context sweep, and finishing it means deciding that the sweep belongs to more than memory.


## 4. External benchmark adapters

Run as isolated subprocesses (ADR-0018), installed
by the user, pinned by version and dataset hash. FreeWeight never redistributes their datasets.

| Adapter | Area | 1.0 scope | Notes |
|---|---|---|---|
| lm-evaluation-harness | General capability | Yes | Default subset: MMLU-Pro, GSM8K, ARC-Challenge, HellaSwag |
| IFEval | Instruction following | Yes | Objectively verifiable instruction types |
| EvalPlus | Coding (executable) | Yes | HumanEval(+), MBPP(+); `fragility = base − plus` |
| CRUXEval | Code reasoning | Yes | Predict output / predict input |
| BFCL | Function calling | Yes | The primary external tool-use reference |
| RULER | Long context | Yes | The primary external effective-context reference |
| JudgeBench | Judging | Yes | Objective correctness-based preference labels |
| LLMBar | Judge robustness | Yes | Includes adversarial subsets |
| CriticBench | Critiquing | Yes | Generation, critique and correction |
| RepoBench | Repository coding | Future | Cross-file completion |
| SWE-bench Verified | Software engineering | Future | Container-only; heavy |
| TUA-Bench | Terminal agents | Future | Container-only; heavy |
| BugsInPy / Defects4J | Real bug corpora | Future | Real-fault auditing |
| LongBench v2 / InfiniteBench | Long context | Future | Realistic and extreme context |
| LiveBench | Contamination-resistant | Future | Refreshed question sets |

Deprecated and explicitly not used as a primary framework: OpenAI `simple-evals` (useful only as a
reference implementation of individual evaluations).

---

## 5. Benchmark manifests

Every benchmark — native or external — has a manifest, and its hash is part of the run's
reproducibility fingerprint.

```json
{
  "key": "native.tool_use",
  "name": "Native Tool Use",
  "version": "1.0.0",
  "category": "tool_use",
  "runner": "native",
  "scorer": "tool_use",
  "capabilities": ["tool_use"],
  "requires": {"provider_capabilities": ["tool_calling"], "sandbox": false, "network": false},
  "dataset_hashes": {"fixtures": "sha256:…"},
  "prompt_ids": [{"prompt_id": "benchmarks.tool_use.system", "version": "1.0.0",
                  "sha256": "9f2c…"}],
  "prompt_subset_hash": "sha256:…",
  "target_device": "gpu",
  "metrics": [
    {"metric_key": "task_success", "unit": "ratio", "higher_is_better": true,
     "aggregation": "mean"},
    {"metric_key": "tool_selection_accuracy", "unit": "ratio", "higher_is_better": true,
     "aggregation": "mean"},
    {"metric_key": "unnecessary_tool_call_rate", "unit": "ratio", "higher_is_better": false,
     "aggregation": "mean", "source": "detail"}
  ],
  "license": "project",
  "manifest_hash": "sha256:…"
}
```

**A suite declares its own `headline_metric`** — the one figure that stands for it where a single
number is needed, which is the dashboard's comparison heatmap. Choosing which of eleven
`native.judge` metrics means "how good a judge is this" is an editorial act that must not be
inferred: an inferred "first metric" would silently change the day the suite gained one. So it is
declared by the suite that owns the judgement, and the dashboard reads it. A suite that declares
none gets no heatmap column — the honest outcome of nobody having decided — and still appears in
every panel. Goal suites resolve to `composite_score` without declaring anything, because every one
of them has it.

A metric declares `metric_key`, `unit`, `higher_is_better` and `aggregation`, and may declare
**`source`**
— `auto` (the default), `detail` or `score` — which pins where its value comes from rather than
letting resolution fall through §5.1's order. Every manifest written before this field existed
behaves identically under `auto`.

External manifests additionally record: source repository, release tag, commit, licence, install
command, dataset paths and hashes, required executables, container requirement, network requirement.

Rules: benchmark versions are pinned; a dataset never updates silently between comparison runs;
results from different suite versions are separated, never averaged.

**`metric_key` is spelled the same everywhere** — in this manifest, on every API surface that
reports a value, and in `metric_values`. Declaring a metric and reporting a value for one are
different acts, and naming them differently meant a reader moving between a manifest and a result
had to translate; a contract test now fails the build if either side drifts.

`dataset_hashes` covers whatever installed content the suite's results depend on — a corpus, a case
file, or a ladder the machine's configuration fits (§3.12). It is the general mechanism for "this
suite's content can drift, and a drift must separate rather than silently re-scale". A suite whose
questions live in Python rather than in a hashed file has only its *version* to separate its
results, which depends on whoever edits them remembering to bump it.

`prompt_subset_hash` covers **only the prompts this manifest declares**, and it — not the pack hash —
is the reproducibility-fingerprint input and the evidence-separation input. A change to a prompt this
benchmark uses forces this suite's version bump and separates its results; a change elsewhere in the
application's pack separates nothing
(ADR-0028).

`requires.provider_capabilities` names
`ProviderCapabilities` fields and is **enforced before the test
runs**: an unmet requirement skips the test with `unsupported_capability` and contributes no score,
and a name that is not a capability field is treated as unmet and fails registry construction rather
than skipping the suite forever (ADR-0033 §9).

### 5.1 Where a metric's value comes from

A manifest declares metric *keys*; a run resolves each one from the first of three sources that has
it, in this order.

| Order | Source | Scope | Used when | Example |
|---|---|---|---|---|
| 1 | Sample facts | One sample | The key is a provider-reported count or duration, or is derived from one | `decode_tokens_per_second`, `ttft_ms` |
| 2 | **Scorer detail** | One test | Any completed sample in the test carries a number under that key in its `ScoreResult.detail` | `tool_selection_accuracy`, `instruction_level_accuracy` |
| 3 | The sample's score | One test | Neither of the above | `harness_roundtrip_success` |
| 4 | **Run derivation** | One run | The figure exists in no sample at all: it needs the descriptor, the telemetry series or every stored repetition (ADR-0034) | `observed_kv_bytes_per_token`, `gpu_energy_joules`, `coefficient_of_variation` |

The scorer-detail source exists because a single scorer often measures several things at once — a
tool trajectory yields a dozen figures, and a suite without this source would report its headline
score under a dozen different names. The decision is made per *test*, not per sample: a sample that
measured no value for a key is excluded from that metric with `not_measured_for_this_case` and is
counted in `excluded_count`, never contributing a zero
(ADR-0016, ADR-0033 §6).

This is what makes a rate with an empty denominator *absent* rather than zero: ordering accuracy for
a case that requires one tool call, calls-per-success for a case that failed, recovery rate for a
trajectory in which nothing failed.

**`source` pins the resolution when the fallthrough would be wrong.** The order above ends in *the
sample's score*, which is a safe last resort for a figure the scorer measured directly and an unsafe
one for a **conditional rate**: a recovery rate whose denominator was empty must be *absent*, and a
fallthrough to the score would report the sample's own pass/fail under a recovery-rate key. A metric
that declares `"source": "detail"` is resolved from scorer detail or not at all. `auto` is the
default and is what every manifest means when it says nothing.

**Run-derived metrics (source 4) are ordinary metrics.** They declare unit, aggregation and
direction in the manifest like any other, are stored run-level in `metric_values`, and are queried,
compared, exported and drilled by the same paths. What is special is only where the *inputs* come
from — and the boundary that goes with it: a derived metric is a function of **one run**. A figure
that needs two runs is a study over results, not a benchmark result, and its home is the comparison
surface (ADR-0034 §6).

### 5.2 Suites that need more than one call per sample

A test may declare an **interaction** — a bounded tool loop, or a call plus one corrective retry —
and the run engine executes it instead of making a single call
(ADR-0033). The benchmark decides what to say
next; the engine owns the provider, the frozen execution parameters, the step budget and the cost
accounting. Token counts are summed across the turns; provider-reported durations are not, because a
sum across calls is a figure no provider produced.

A trajectory is scored by a **trajectory scorer**, which reads the whole transcript rather than the
final response text. There is no fallback to text scoring: a suite measuring tool selection must not
silently report an exact-match figure under a tool-selection key.

---

## 6. Capability mapping

How benchmark metrics become the `capability.evidence` LoadCoach consumes. Weights are configuration,
shipped with defaults, versioned with the evidence.

| Capability | Primary sources |
|---|---|
| `reasoning` | lm-eval (MMLU-Pro, GSM8K, ARC), `native.agent` reasoning steps |
| `coding` | EvalPlus pass@1 (plus variants weighted higher), `fragility` as a penalty |
| `code_review` / `auditing` | `native.audit` F1 with the clean-code false-positive rate as a penalty |
| `debugging` | `native.agent` diagnosis tasks, CRUXEval, future real-bug corpora |
| `instruction_following` | `native.instruction_following`, IFEval |
| `structured_output` | `native.structured_output` |
| `tool_use` | `native.tool_use`, `native.tool_recovery`, BFCL |
| `agentic` | `native.agent` |
| `summarization` / `creative_writing` | User-authored goal suites (§7), judged criteria calibrated against the user's own grades, with judge trustworthiness and agreement linked. **No shipped suite scores these** — a house voice has no corpus ground truth, so the ground truth is the user's (ADR-0031) |
| `user.<slug>` | The goal suite of that name, emitted under the reserved `user` root (ADR-0032 §1). Opt-in for routing; never weighted unless a task profile names it |
| `judging` | `native.judge`, JudgeBench, LLMBar (bias metrics reduce the score) |
| `critiquing` | `native.critique` (regression rate reduces the score) |
| `long_context` | `native.long_context` effective context, RULER |
| `speed` / `latency` | `native.performance` decode throughput, TTFT |
| `memory_efficiency` | `native.memory_kv` observed bytes/token, max context fit |
| `token_efficiency` | `native.token_economy` |
| `energy_efficiency` | `native.energy` |
| `reliability` | `native.reliability` dispersion and agreement across every suite |

Every capability score records which benchmarks contributed, with what weight and how many samples —
so a user can always answer "why is this model's coding score 0.71?"

---

## 7. Goal suites — user-authored (1.0 scope)

Full contract: [Subjective Goals](subjective-goals.md). Decisions:
ADR-0031,
ADR-0032.

A **goal suite** is a benchmark the user writes, for work whose ground truth lives in their head
rather than in a corpus: *"essays in my voice"*, *"our house style"*, *"summaries that invent
nothing"*. It is a third `runner` kind alongside `native` and `external`, and is first-class in every
respect that matters — manifest, version, hash, run engine, raw samples, comparison, export.

### 7.1 How a goal is scored

Criteria, each declaring the highest ladder rung that can actually check it:

```text
rung 2  rule        forbidden/required phrases, word and sentence-length distributions,
                    paragraph shape, readability band, POV and tense, vocabulary and
                    punctuation profiles, structure, JSON Schema, linted regex, repetition
rung 3  reference   entity recall, claim coverage, unsupported-claim detection, reference
                    similarity — deterministic, against user-supplied ground truth
rung 4  human       the user grades, blinded, in the UI
rung 5  judge       the irreducible remainder: voice, wit, register, cohesion
```

`freeweight goals validate` **flags a rung-5 criterion a rung-2 rule could check**, and names the
rule. It is a lint, not a refusal — the system cannot know that a phrase list fully covers "avoid
corporate hedging", and a false refusal is worse than a warning. Every goal result reports
`score_method_mix`, the fraction of its scored weight by rung, beside the score itself.

Hard gates: a rule criterion with `gate: true` zeroes the sample's composite when it fails, and
records which gate did it. For disqualifying properties, not gradual ones.

### 7.2 Calibration is the price of a judged criterion

| Step | |
|---|---|
| Grade | The user grades 8–12 samples (target 12) on their own criteria, blinded, with notes |
| Partition | Seeded, stratified, recorded: 60 % **anchors** into the judge prompt as exemplars, 40 % **holdout** never shown to the jury |
| Measure | The jury scores the holdout; agreement with the user is `kappa_w` (quadratic-weighted Cohen's kappa), reported with `rho`, `mae`, `bias` and — never omitted — `n_holdout` |
| Gate | Weighted `kappa_w` below `calibration.min_agreement` (default 0.40) ⇒ the run completes, every sample is inspectable, the result is badged **UNCALIBRATED**, and **no capability evidence is emitted** |
| Diagnose | The criteria and the specific samples where jury and user diverged most, with both rationales |

FreeWeight never rewrites the user's criterion. It names the problem and shows the evidence.

### 7.3 The jury

3 distinct local models by default, `temperature = 0.0`, repeated trials, case and criterion order
randomized, candidate identity hidden. Inter-juror agreement (Krippendorff's alpha) is a headline
metric. A juror never judges its own output — refused and recorded, not discounted. A remote frontier
juror is opt-in twice over, recorded in the fingerprint, and **separates** results from locally-judged
ones. Changing the jury changes `goal_hash`: a new instrument is a new measurement.

### 7.4 Metrics

```text
composite_score           weighted, gates applied
per-criterion scores      with the rung that produced each
score_method_mix          {rule, reference, human, judge} fractions of scored weight
inter_juror_agreement     Krippendorff's alpha per judged criterion
judge_validity_factor     Σ(weight × v) / Σ(weight);  v = 1.0 for rungs 1–4,
                          max(0, kappa_w) × min(1, sqrt(n_holdout / 10)) for rung 5
gated_sample_rate         with the gate that fired
calibration               kappa_w, rho, mae, bias, n_anchor, n_holdout, graded_by, measured_at
```

`judge_validity_factor` multiplies into ADR-0017 confidence as a sixth factor, and is **1.0 for every
measurement in §3 and §4** — no existing result changes value.

### 7.5 Starter packs

Shipped, forkable, and explicitly not defaults; a goal running unedited starter criteria and tasks is
badged `unforked`.

| Key | Goal | Approx. deterministic weight |
|---|---|---|
| `starter.creative_voice` | Style and tone in creative non-fiction | ~40 % |
| `starter.brand_voice` | A defined persona or house style guide | ~70 % |
| `starter.summary_faithfulness` | Coverage without fabrication | ~90 % |
| `starter.technical_explanation` | Correct, well-pitched technical prose | ~55 % |

Read in that order they teach the point: the better you understand what you want, the less of it
needs a judge.

### 7.6 Goal manifests

A goal suite's manifest follows §5 with three additions and one changed field:

```json
{
  "key": "goal.noir_tech_voice",
  "runner": "goal",
  "goal_hash": "sha256:…",
  "goal_pack_version": "1.2.0",
  "calibration_ref": {"kappa_w": 0.71, "n_holdout": 6, "measured_at": "2026-08-14T09:00:00Z"},
  "judge_set": {"jurors": ["ollama/qwen3:14b@sha256:…", "ollama/gemma3:12b@sha256:…"],
                "prompt_id": "judge.rubric", "prompt_version": "1.0.0", "remote": false}
}
```

`goal_hash` covers criteria, weights, rungs, rule parameters, scale descriptors, task prompt hashes,
the judge prompt hash and the jury configuration — and excludes display names, `intent` and the
grades. Renaming a criterion must not separate a year of results; changing what it checks must.
