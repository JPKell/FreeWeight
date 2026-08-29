"""freeweight.benchmarks.performance.benchmark — ``native.performance``, the first real measurement.

Benchmark catalog §3.1. Five tests, and every one of them measures *how fast*, never *how good*:
the scorer records only that a response arrived, because a speed benchmark that also judged content
would report one number for two different properties.

**The hygiene rules are structural here, not advisory.**

* *Warm-up is excluded from headline numbers.* The run engine's warm phase throws its generations
  away; nothing in this suite ever sees them.
* *Cold and warm are never mixed.* ``performance.cold_load`` declares
  :attr:`~freeweight.domain.metrics.MeasurementClass.COLD` and every other test declares ``warm``,
  so :func:`freeweight.domain.aggregation.aggregate_run` refuses to combine them into a run-level
  figure rather than relying on anybody remembering not to.
* *Chunk latency is not token latency.* ``performance.streaming_latency`` declares both
  ``inter_chunk_ms_*`` and ``inter_token_ms_mean``; the latter is available only where the provider
  declared ``token_level_chunks``, and is ``UNSUPPORTED`` with a reason otherwise
  (:func:`freeweight.domain.metrics.inter_token_ms_mean`).
* *Every prompt-size case declares the context it needs.* A case larger than the run's served
  context is skipped with a recorded reason instead of being sent and failing — which is benchmark
  catalog §3.1's "only those the model supports", decided per case because one suite runs against
  models with different contexts.

**Concurrency scaling is not implemented.** The catalog marks that row optional, and the four
figures it would add (aggregate throughput, per-request throughput, latency percentiles under load,
peak VRAM under load) need a concurrent execution path the run engine does not have and that Phase
6 does not ask for. It is absent rather than stubbed.

The prompts come from the pack (:mod:`freeweight.services.prompts`), so this suite's
``prompt_subset_hash`` is real from its first run and its results separate when — and only when —
one of *its* prompts changes (ADR-0028 §1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.domain.benchmark import BenchmarkCase, BenchmarkManifest, MetricDefinition
from freeweight.domain.metrics import MeasurementClass
from freeweight.domain.scoring import ScoreMethod, ScoreResult
from freeweight.services.prompts import PromptLibrary, load_pack, prompt_subset_hash

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "PROMPT_ID",
    "PerformanceBenchmark",
    "PerformanceTest",
    "ResponseArrivedScorer",
    "build",
    "load_manifest",
]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"

PROMPT_ID = "benchmarks.performance.probe"
"""The one prompt record every case in this suite renders."""

_CHARS_PER_TOKEN = 4
"""Characters per token used to size filler text.

An approximation, and it does not have to be better than that: the *measured* prompt length is
whatever the provider reports in ``prompt_tokens``, and that is the number every throughput figure
divides by. This constant only decides how much text to send, never what the result says.
"""

_FILLER_SENTENCE = (
    "A benchmark harness sends a prompt, waits, and writes down exactly what came back. "
)

PROMPT_SIZES: tuple[int, ...] = (128, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
"""The prompt lengths benchmark catalog §3.1 names, in tokens."""

OUTPUT_LENGTHS: tuple[int, ...] = (32, 128, 256, 512, 1024)
"""The fixed output lengths benchmark catalog §3.1 names, in tokens."""


def _filler(tokens: int) -> str:
    """Return deterministic filler text of approximately ``tokens`` tokens.

    Deterministic because a benchmark whose prompt differs between runs is not a benchmark: the
    same size always produces byte-identical text, so two runs of ``prompt_processing`` at 4 096
    tokens sent the same bytes and their ``rendered_prompt_hash`` values agree.
    """
    target_chars = tokens * _CHARS_PER_TOKEN
    repeats = max(1, target_chars // len(_FILLER_SENTENCE) + 1)
    return (_FILLER_SENTENCE * repeats)[:target_chars].strip()


@dataclass(frozen=True, slots=True)
class ResponseArrivedScorer:
    """Records that the request completed. It scores nothing about the answer.

    ``1.0`` when a non-empty response came back, ``0.0`` when the provider answered with nothing.
    A call that never returned does not reach a scorer at all — the engine stores it as a failed
    sample with the provider's error, and a failed sample is excluded from every aggregate rather
    than scored zero (ADR-0016).

    This suite's numbers are timings, and its score exists so that a case which silently returned
    an empty string is visible as a completed-but-empty measurement rather than as a very fast one.
    """

    key: str = "response_arrived"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Score one response.

        Args:
            case: The case that produced ``response_text``. Unused: nothing about a speed case
                changes what "an answer arrived" means.
            response_text: Exactly what the model returned.

        Returns:
            ``1.0`` for any non-whitespace content, ``0.0`` otherwise, with the response's size in
            ``detail`` so the sample drill-down shows what was measured.
        """
        stripped = response_text.strip()
        return ScoreResult(
            score=1.0 if stripped else 0.0,
            method=self.method,
            detail={"response_chars": len(response_text), "case": case.case_id},
        )


_THROUGHPUT_METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        metric_key="prompt_tokens",
        unit="count",
        higher_is_better=True,
        aggregation="mean",
        description="Prompt tokens the provider reported for this case.",
    ),
    MetricDefinition(
        metric_key="prompt_eval_ms",
        unit="ms",
        higher_is_better=False,
        aggregation="mean",
        description="Time the provider reported evaluating the prompt.",
    ),
    MetricDefinition(
        metric_key="prompt_tokens_per_second",
        unit="tokens/s",
        higher_is_better=True,
        aggregation="mean",
        description=(
            "Prompt-evaluation throughput: the provider's token count divided by the provider's "
            "own prompt-evaluation time. Never mixed with client-observed wall time."
        ),
    ),
)

_DECODE_METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        metric_key="output_tokens",
        unit="count",
        higher_is_better=True,
        aggregation="mean",
        description="Generated tokens the provider reported, thinking and tool syntax included.",
    ),
    MetricDefinition(
        metric_key="decode_ms",
        unit="ms",
        higher_is_better=False,
        aggregation="mean",
        description="Time the provider reported generating output tokens.",
    ),
    MetricDefinition(
        metric_key="decode_tokens_per_second",
        unit="tokens/s",
        higher_is_better=True,
        aggregation="mean",
        description=(
            "Decode throughput: the provider's output-token count divided by its own decode time."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class PerformanceTest:
    """One test of ``native.performance``: its cases, its metrics and how it is executed.

    Attributes:
        key: Stable test key within the suite.
        name: Human-readable name.
        category: The catalog category the suite contributes to.
        measurement_class: ``cold`` or ``warm`` — declared, never defaulted, because it decides
            what aggregation may combine.
        streaming: Whether cases run through :meth:`~modelrack.Provider.stream`. Only a streamed
            call has a first-token moment or inter-chunk gaps to observe.
        metrics: What this test produces.
        cases_spec: ``(case_id, passage_tokens, instruction, required_context_tokens)`` per case.
        library: The loaded prompt pack the cases render from.
    """

    key: str
    name: str
    category: str
    measurement_class: str
    streaming: bool
    metrics: Sequence[MetricDefinition]
    cases_spec: tuple[tuple[str, int, str, int | None], ...]
    library: PromptLibrary

    @property
    def scorer(self) -> ResponseArrivedScorer:
        """The one scorer every case in this suite uses."""
        return ResponseArrivedScorer()

    @property
    def requires(self) -> Mapping[str, Any]:
        """What a provider must offer for this test to mean anything.

        ``token_counts`` for every test: without reported token counts there is no honest
        throughput figure at all, only a wall-clock duration (ModelRack spec §11.3). ``streaming``
        additionally for the streamed tests.
        """
        capabilities = ["token_counts"]
        if self.streaming:
            capabilities.append("streaming")
        return {"provider_capabilities": capabilities, "sandbox": False, "network": False}

    def cases(self) -> Iterator[BenchmarkCase]:
        """Yield this test's cases in declaration order, each rendered from the prompt record."""
        record = self.library.get(PROMPT_ID)
        for ordinal, (case_id, passage_tokens, instruction, needed) in enumerate(self.cases_spec):
            rendered = record.render(
                {"passage": _filler(passage_tokens), "instruction": instruction}
            )
            yield BenchmarkCase(
                case_id=case_id,
                ordinal=ordinal,
                prompt=rendered.user,
                system_prompt=rendered.system,
                prompt_id=rendered.prompt_id,
                prompt_version=rendered.version,
                required_context_tokens=needed,
                expectation={},
                metadata={
                    "suite": "native.performance",
                    "test": self.key,
                    "passage_tokens": passage_tokens,
                },
            )


def _prompt_processing(library: PromptLibrary) -> PerformanceTest:
    """Build the prompt-processing test: one case per catalog prompt size."""
    return PerformanceTest(
        key="performance.prompt_processing",
        name="Prompt processing",
        category="performance",
        measurement_class=MeasurementClass.WARM.value,
        streaming=False,
        metrics=_THROUGHPUT_METRICS,
        cases_spec=tuple(
            (
                f"prompt-{size}",
                size,
                "Reply with the single word: ok.",
                # The case needs its own prompt plus a little room to answer in; a case sized to
                # exactly the served context would fail on the first output token, which measures
                # the arithmetic rather than the model.
                size + 256,
            )
            for size in PROMPT_SIZES
        ),
        library=library,
    )


def _decode_throughput(library: PromptLibrary) -> PerformanceTest:
    """Build the decode-throughput test: one case per catalog output length."""
    return PerformanceTest(
        key="performance.decode_throughput",
        name="Decode throughput",
        category="performance",
        measurement_class=MeasurementClass.WARM.value,
        streaming=False,
        metrics=_DECODE_METRICS,
        cases_spec=tuple(
            (
                f"decode-{length}",
                64,
                f"Continue the passage above for about {length} tokens. Do not stop early.",
                length + 512,
            )
            for length in OUTPUT_LENGTHS
        ),
        library=library,
    )


def _combined_request(library: PromptLibrary) -> PerformanceTest:
    """Build the combined-request test: a realistic prompt and a realistic generation, streamed."""
    return PerformanceTest(
        key="performance.combined_request",
        name="Combined request",
        category="performance",
        measurement_class=MeasurementClass.WARM.value,
        streaming=True,
        metrics=(
            MetricDefinition(
                metric_key="total_ms",
                unit="ms",
                higher_is_better=False,
                aggregation="mean",
                description=(
                    "Total time this process observed for the call, measured with a monotonic "
                    "counter. The client's account, never merged with the provider's."
                ),
            ),
            MetricDefinition(
                metric_key="ttft_ms",
                unit="ms",
                higher_is_better=False,
                aggregation="mean",
                description="Time this process observed before the first streamed delta arrived.",
            ),
            *_THROUGHPUT_METRICS[2:],
            *_DECODE_METRICS[2:],
        ),
        cases_spec=(
            (
                "combined-short",
                512,
                "Summarise the passage above in three sentences.",
                1024,
            ),
            (
                "combined-long",
                2048,
                "Summarise the passage above, then list three questions it leaves open.",
                4096,
            ),
        ),
        library=library,
    )


def _streaming_latency(library: PromptLibrary) -> PerformanceTest:
    """Build the streaming-latency test: inter-delta timing, honestly labelled."""
    return PerformanceTest(
        key="performance.streaming_latency",
        name="Streaming latency",
        category="performance",
        measurement_class=MeasurementClass.WARM.value,
        streaming=True,
        metrics=(
            MetricDefinition(
                metric_key="ttft_ms",
                unit="ms",
                higher_is_better=False,
                aggregation="mean",
                description="Time observed before the first streamed delta arrived.",
            ),
            MetricDefinition(
                metric_key="inter_chunk_ms_mean",
                unit="ms",
                higher_is_better=False,
                aggregation="mean",
                description=(
                    "Mean gap between streamed deltas. A **chunk** figure: one delta is one "
                    "token only where the provider declares token_level_chunks."
                ),
            ),
            MetricDefinition(
                metric_key="inter_chunk_ms_p50",
                unit="ms",
                higher_is_better=False,
                aggregation="mean",
                description="Median gap between streamed deltas, per sample.",
            ),
            MetricDefinition(
                metric_key="inter_chunk_ms_p95",
                unit="ms",
                higher_is_better=False,
                aggregation="mean",
                description="95th-percentile gap between streamed deltas, per sample.",
            ),
            MetricDefinition(
                metric_key="inter_token_ms_mean",
                unit="ms",
                higher_is_better=False,
                aggregation="mean",
                description=(
                    "Mean per-token latency. Available only where the provider declares that one "
                    "streamed delta is one token; unsupported with a reason otherwise, never "
                    "borrowed from the chunk figure."
                ),
            ),
        ),
        cases_spec=(("stream-latency", 256, "Write a short paragraph about caching.", 1024),),
        library=library,
    )


def _cold_load(library: PromptLibrary) -> PerformanceTest:
    """Build the cold-load test — the one test in this suite that is *not* warm.

    Its ``measurement_class`` is what stops a cold ``load_ms`` from ever being averaged with a
    warm one: the run-level aggregate for a metric whose contributing tests disagree about the
    class is emitted with no value and the reason ``cold_and_warm_not_comparable``.
    """
    return PerformanceTest(
        key="performance.cold_load",
        name="Cold model load",
        category="performance",
        measurement_class=MeasurementClass.COLD.value,
        streaming=False,
        metrics=(
            MetricDefinition(
                metric_key="load_ms",
                unit="ms",
                higher_is_better=False,
                aggregation="mean",
                description=(
                    "Time the provider reported loading the model. Unsupported, never zero, on a "
                    "provider that does not report it."
                ),
            ),
            MetricDefinition(
                metric_key="total_ms",
                unit="ms",
                higher_is_better=False,
                aggregation="mean",
                description="Total observed time for the first call against a cold model.",
            ),
        ),
        cases_spec=(("cold-load", 64, "Reply with the single word: ok.", 512),),
        library=library,
    )


def load_manifest() -> BenchmarkManifest:
    """Load ``manifest.json`` from beside this module.

    Returns:
        The parsed manifest.

    Raises:
        ValueError: The shipped manifest is missing a required field — a packaging defect.
    """
    body = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return BenchmarkManifest.from_json(body)


@dataclass(frozen=True, slots=True)
class PerformanceBenchmark:
    """The ``native.performance`` suite: its manifest, its prompt pack and its five tests."""

    manifest: BenchmarkManifest
    library: PromptLibrary

    @property
    def tests(self) -> Sequence[PerformanceTest]:
        """The five tests, cold load last so the warm tests run against a warmed model."""
        return (
            _prompt_processing(self.library),
            _decode_throughput(self.library),
            _combined_request(self.library),
            _streaming_latency(self.library),
            _cold_load(self.library),
        )


def build(library: PromptLibrary | None = None) -> PerformanceBenchmark:
    """Build the suite, verifying that the manifest describes the installed prompts.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one. Injected so a test can
            build the suite over a pack in a temporary directory.

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` does not match the prompts the installed
            pack actually holds. Refused rather than recomputed: the subset hash is a fingerprint
            input, so silently correcting it here would let a stale manifest produce runs whose
            provenance describes prompts that were never rendered.
        PromptNotFound: The manifest declares a prompt the installed pack does not have.
    """
    pack = library if library is not None else load_pack()
    manifest = load_manifest()
    references = pack.references(
        (entry["prompt_id"], entry.get("version")) for entry in manifest.prompt_ids
    )
    actual = prompt_subset_hash(references)
    if manifest.prompt_subset_hash != actual:
        raise ValueError(
            f"Benchmark {manifest.key!r} declares prompt_subset_hash "
            f"{manifest.prompt_subset_hash!r}; the installed pack's declared prompts hash to "
            f"{actual!r}. Rebuild the manifest and bump the suite version — a prompt this suite "
            "uses has changed, which separates its results."
        )
    return PerformanceBenchmark(manifest=manifest, library=pack)
