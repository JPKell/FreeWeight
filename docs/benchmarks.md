# Benchmark guide

FreeWeight runs three kinds of benchmark, all of which produce the same shape of result — samples,
metrics, provenance — and all of which appear together in the UI and exports.

## Native suites

Ship with the application, need no external download, and are the reason FreeWeight starts with
zero setup. They cover quality (echo, instruction following, structured output, tool use), resource
use (performance, token economy, memory/KV, energy) and reliability. List them:

```bash
freeweight benchmarks list
freeweight benchmarks show native.performance
freeweight run start --model <ref> --suite native.performance
```

The full catalogue — every suite, its cases, its metrics and how each is scored — is the
[benchmark catalogue](apps/freeweight/benchmark-catalog.md).

## External adapters

FreeWeight can drive established external benchmarks — lm-evaluation-harness (MMLU-Pro, GSM8K),
IFEval, EvalPlus, CRUXEval, BFCL, RULER, JudgeBench, LLMBar, CriticBench — as **isolated
subprocesses** (ADR-0018). They are never imported into FreeWeight's own environment, their
versions and datasets are pinned, and their output is parsed as untrusted input. You install them
yourself, because their licences and dataset terms forbid redistribution.

```bash
freeweight external list                       # every adapter, its source, licence and state
freeweight external install external.ifeval    # creates its isolated environment
freeweight external verify external.ifeval     # checks its datasets against their pinned hashes
```

The **Sources** page in the UI credits every external project, its pinned version and commit, and
its licence.

### Code execution and the sandbox

EvalPlus and CRUXEval execute model-generated code to score it. That code **never runs on the
host**: it runs in a tiered sandbox (container → bwrap → refuse; ADR-0018), and on a machine with
no tier those benchmarks are *skipped* with `sandbox_unavailable`, never run. `freeweight doctor`'s
`sandbox` line shows which tier is available. Install Docker (or bubblewrap) to enable them.

Every result records the sandbox tier it used, so a performance comparison across tiers is labelled
and a correctness comparison is not misled.

## User-defined goal suites

When the ground truth lives in your head rather than a corpus — "essays in my voice", "summaries
that stay faithful" — you can define a *goal*: some deterministic rule criteria, some rubric
criteria a jury of local models grades. FreeWeight measures how well a model meets your goal and,
crucially, how much to trust that answer: a goal emits capability evidence only once you have graded
enough examples for it to characterise the instrument. See
[subjective goals](apps/freeweight/subjective-goals.md).

```bash
freeweight goals init my-voice        # a guided interview
freeweight goals validate my-voice    # every lint finding at once
freeweight goals calibrate my-voice   # grade samples, see the agreement report
```

## Comparing and exporting

Every result carries its measurement subject `(model identity, runtime profile hash, machine
fingerprint)` and its benchmark version, so a consumer can tell what is comparable to what without
asking FreeWeight. Export a run, or export capability evidence LoadCoach reads with `setspec`
alone:

```bash
freeweight results export <run-id> --format json
freeweight evidence export > evidence-bundle.json
```
