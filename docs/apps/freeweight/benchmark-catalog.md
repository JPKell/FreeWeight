# FreeWeight — Benchmark Catalog

**Owner:** FreeWeight. **Status:** Specification of what is measured and how it is scored.
The 1.0 scope is marked per entry; anything unmarked is a documented future extension.

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
```

---

## 3. Native suites (1.0 scope)

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
([ADR-0027 §3](../../adr/0027-multi-gpu-semantics.md)). The context axis of every test is the
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

Metrics: precision, recall, F1, **clean-code false-positive rate**, bug-category accuracy, severity
accuracy, file/function/line localization, explanation correctness, suggested-fix correctness, patch
compile rate, patch test-pass rate, regression rate.

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
| Repetition stability | Same comparison repeated | Agreement rate, variance |
| Verbosity bias | Concise correct vs verbose weaker | Verbosity preference rate |
| Style bias | Content held constant, style varied | Style preference rate |
| Transitivity | A>B, B>C ⇒ A>C | Transitivity violation rate |
| Self-preference | Own answer vs another's, anonymized and not | Self-preference delta |

### 3.12 `native.long_context`

Context depths 2K…128K (only those supported). Retrieval at 10/25/50/75/90 % depth; distractor
sensitivity (0K…64K of irrelevant context); distributed reasoning across distant facts.

Metrics: accuracy by context length, by information position and by distractor volume;
`effective_context_tokens` (largest tested context where accuracy ≥ a configurable fraction — default
80 % — of the short-context baseline, with the threshold recorded); accuracy AUC across context;
latency, VRAM and prompt throughput by context.

Advertised context and effective context are always displayed as separate numbers.

### 3.13 `native.reliability`

Cross-cutting: every benchmark runner supports repeated trials and stores **all** repetitions.
Reported: mean, median, min, max, standard deviation, coefficient of variation, p50/p95/p99 where
meaningful; `pass@1` and `pass@k` where applicable; answer/tool-call/judge agreement for repeated
stochastic tests.

Outliers are never silently discarded. Any exclusion is explicit, reasoned and preserved in the raw
data.

### 3.14 `native.energy` (telemetry-derived estimate)

From persisted power samples for `execution.gpu_index`: `energy_joules ≈ Σ(power_watts × dt_seconds)`
using real sample timestamps. There is no machine-wide GPU energy figure; per-device series are
reported separately and never summed into one number that no device produced. Metrics: joules per request, per output token, per successful task; tokens per joule;
successful tasks per kWh; mean and peak GPU power; max GPU/CPU temperature; suspected throttling.

Always labelled a telemetry-derived estimate, never instrumentation. `unsupported` when power
readings are unavailable.

---

## 4. External benchmark adapters

Run as isolated subprocesses ([ADR-0018](../../adr/0018-external-benchmark-isolation.md)), installed
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
    {"key": "task_success", "unit": "ratio", "higher_is_better": true, "aggregation": "mean"},
    {"key": "tool_selection_accuracy", "unit": "ratio", "higher_is_better": true, "aggregation": "mean"},
    {"key": "unnecessary_tool_call_rate", "unit": "ratio", "higher_is_better": false, "aggregation": "mean"}
  ],
  "license": "project",
  "manifest_hash": "sha256:…"
}
```

External manifests additionally record: source repository, release tag, commit, licence, install
command, dataset paths and hashes, required executables, container requirement, network requirement.

Rules: benchmark versions are pinned; a dataset never updates silently between comparison runs;
results from different suite versions are separated, never averaged.

`prompt_subset_hash` covers **only the prompts this manifest declares**, and it — not the pack hash —
is the reproducibility-fingerprint input and the evidence-separation input. A change to a prompt this
benchmark uses forces this suite's version bump and separates its results; a change elsewhere in the
application's pack separates nothing
([ADR-0028](../../adr/0028-prompt-pack-granularity.md)).

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
| `summarization` / `creative_writing` | Judge-scored suites, with judge trustworthiness linked |
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
