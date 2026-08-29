"""freeweight.benchmarks.memory_kv.benchmark — ``native.memory_kv``, what a context costs.

Benchmark catalog §3.2. Four tests that *generate*, and one derivation that reads what they left
behind: the descriptor's architecture, the run's telemetry, and which context lengths actually
served a request.

**The split is deliberate.** A scorer sees a case and a string; it cannot see device memory, and a
suite that pretended otherwise would have to smuggle a telemetry handle through the scoring
interface. So the tests here record the *context each sample ran at* and nothing else, and
:func:`derive` — handed the stored telemetry and the stored descriptor by the run engine —
produces the figures the catalog names. Every one of those figures is computed by
:mod:`freeweight.benchmarks.memory_kv.kv`, which is pure arithmetic with no idea where its numbers
came from.

**Cold prefill and reused prefill are two tests, not two cases of one.**
``memory_kv.prefix_first_pass`` declares :attr:`~freeweight.domain.metrics.MeasurementClass.WARM`
and ``memory_kv.prefix_reuse`` declares ``cache_reused``, so
:func:`freeweight.domain.aggregation.aggregate_run` refuses to combine their ``prompt_eval_ms``
into one run-level number rather than relying on anybody remembering that the two describe
different things. ``reuse_speedup`` is then the ratio of the two, computed once, in
:func:`derive`.

**An out-of-memory rejection is a measurement.** ``memory_kv.max_context_fit`` climbs until the
runtime refuses; the refusal is stored as a failed sample and read back here as the boundary. The
run stays successful, because "this model fits 32 768 tokens and not 65 536" is the answer the
test was taken to get — treating the refusal as a failed run would throw it away.

**One device or nothing.** Where more than one GPU is visible and the provider does not report
placement, every figure this module produces is ``unsupported`` with
``multi_gpu_placement_unknown`` (ADR-0027 §3): a VRAM slope read from the wrong device is not an
approximation, it is a fabricated number that reads as "context is free".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baseaicore import UNSUPPORTED, Measurement, is_supported

from freeweight.benchmarks.memory_kv.kv import (
    ContextObservation,
    KvArchitecture,
    cache_reuse_speedup,
    fit_context_slope,
    kv_overhead_ratio,
    max_context_capped_by_configuration,
    max_successful_context_tokens,
    observed_mb_per_1k_context,
    theoretical_kv_bytes_per_token,
)
from freeweight.domain.aggregation import AggregatedMetric
from freeweight.domain.benchmark import BenchmarkCase, BenchmarkManifest, MetricDefinition
from freeweight.domain.metrics import (
    REASON_MULTI_GPU_PLACEMENT_UNKNOWN,
    MeasurementClass,
    MetricResult,
    unavailable,
)
from freeweight.domain.scoring import ScoreMethod, ScoreResult
from freeweight.services.prompts import PromptLibrary, load_pack, prompt_subset_hash

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from datetime import datetime

__all__ = [
    "CONTEXT_TOKENS_DETAIL_KEY",
    "FIT_CONTEXT_TOKENS",
    "MAX_FIT_CONTEXT_TOKENS",
    "PROMPT_ID",
    "SHARED_PREFIX_TOKENS",
    "ContextProbeScorer",
    "MemoryKvBenchmark",
    "MemoryKvTest",
    "SampleWindow",
    "build",
    "derive",
    "load_manifest",
    "stabilized_vram",
]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"

PROMPT_ID = "benchmarks.memory_kv.context_probe"
"""The one prompt record every case in this suite renders."""

CONTEXT_TOKENS_DETAIL_KEY = "context_tokens"
"""Where a sample records the context length it was run at.

The same key :mod:`freeweight.domain.aggregation` reads for effective context, deliberately: two
suites that recorded "the context this sample ran at" under two names would be two facts nobody
could join."""

_CHARS_PER_TOKEN = 4
"""Characters per token used to size filler text. An approximation, and it only decides how much
text to send — every reported context comes from the run's own served context, never from here."""

_FILLER_SENTENCE = "A cache holds what was already computed so that it need not be computed twice. "

FIT_CONTEXT_TOKENS: tuple[int, ...] = (1024, 2048, 4096, 8192, 16384, 32768, 65536)
"""The context lengths the observed slope is fitted over — catalog §3.2's "1K…64K"."""

MAX_FIT_CONTEXT_TOKENS: tuple[int, ...] = (8192, 16384, 32768, 65536, 131072)
"""The ladder the maximum-context-fit test climbs until something refuses."""

SHARED_PREFIX_TOKENS = 4096
"""Tokens in the prefix the cache-reuse test sends once and then re-sends unchanged."""


def _filler(tokens: int) -> str:
    """Return deterministic filler text of approximately ``tokens`` tokens.

    Byte-identical for the same size on every run, so two runs of ``context_slope`` at 8 192
    tokens sent the same prompt and their ``rendered_prompt_hash`` values agree.
    """
    target_chars = tokens * _CHARS_PER_TOKEN
    repeats = max(1, target_chars // len(_FILLER_SENTENCE) + 1)
    return (_FILLER_SENTENCE * repeats)[:target_chars].strip()


@dataclass(frozen=True, slots=True)
class ContextProbeScorer:
    """Records that a generation completed at a known context length. It scores no content.

    ``1.0`` when a non-empty response came back, ``0.0`` when the provider answered with nothing.
    The number that matters is in ``detail``: the context this sample occupied, which is what
    :func:`derive` fits the memory slope against and what the maximum-fit test reads its boundary
    from. A request the runtime refused never reaches a scorer at all — the engine stores it as a
    failed sample, and a failed sample at a given context is exactly the OOM measurement.
    """

    key: str = "response_arrived"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Score one response.

        Args:
            case: The case that produced ``response_text``; its metadata carries the context
                length under test.
            response_text: Exactly what the model returned.

        Returns:
            ``1.0`` for any non-whitespace content and ``0.0`` otherwise, with the case's context
            length in ``detail`` so the sample says what it was measuring.
        """
        context = case.metadata.get(CONTEXT_TOKENS_DETAIL_KEY)
        return ScoreResult(
            score=1.0 if response_text.strip() else 0.0,
            method=self.method,
            detail={
                CONTEXT_TOKENS_DETAIL_KEY: context,
                "case": case.case_id,
                "response_chars": len(response_text),
            },
        )


_PREFILL_METRICS: tuple[MetricDefinition, ...] = (
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
        description=(
            "Time the provider reported evaluating the prompt. Comparable only within one "
            "measurement class: a first pass over a prefix and a reuse of it are different things."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class MemoryKvTest:
    """One test of ``native.memory_kv``: its cases, its metrics and the state it measures.

    Attributes:
        key: Stable test key within the suite.
        name: Human-readable name.
        category: The catalog category the suite contributes to.
        measurement_class: ``warm`` or ``cache_reused`` — declared, never defaulted, because it
            decides what aggregation may combine.
        metrics: What this test's own samples produce. The descriptor- and telemetry-derived
            figures are **not** here; they come from :func:`derive`, which is the only place that
            can compute them honestly.
        cases_spec: ``(case_id, context_tokens, instruction, required_context_tokens)`` per case.
        library: The loaded prompt pack the cases render from.
    """

    key: str
    name: str
    category: str
    measurement_class: str
    metrics: Sequence[MetricDefinition]
    cases_spec: tuple[tuple[str, int, str, int | None], ...]
    library: PromptLibrary

    @property
    def scorer(self) -> ContextProbeScorer:
        """The one scorer every case in this suite uses."""
        return ContextProbeScorer()

    @property
    def streaming(self) -> bool:
        """Never. A memory reading has no first-token moment, and streaming would add one."""
        return False

    @property
    def requires(self) -> Mapping[str, Any]:
        """``token_counts``: without them there is no prompt-evaluation time to compare."""
        return {"provider_capabilities": ["token_counts"], "sandbox": False, "network": False}

    def cases(self) -> Iterator[BenchmarkCase]:
        """Yield this test's cases in declaration order, each rendered from the prompt record."""
        record = self.library.get(PROMPT_ID)
        for ordinal, (case_id, context_tokens, instruction, needed) in enumerate(self.cases_spec):
            rendered = record.render(
                {"passage": _filler(context_tokens), "instruction": instruction}
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
                    "suite": "native.memory_kv",
                    "test": self.key,
                    CONTEXT_TOKENS_DETAIL_KEY: context_tokens,
                },
            )


def _context_slope(library: PromptLibrary) -> MemoryKvTest:
    """The slope test: one short generation at each of the catalog's context lengths."""
    return MemoryKvTest(
        key="memory_kv.context_slope",
        name="Observed context slope",
        category="memory",
        measurement_class=MeasurementClass.WARM.value,
        metrics=_PREFILL_METRICS,
        cases_spec=tuple(
            (
                f"slope-{size}",
                size,
                "Reply with the single word: ok.",
                # Room for the prompt plus a short answer. A case sized to exactly the served
                # context fails on its first output token, which measures the arithmetic.
                size + 256,
            )
            for size in FIT_CONTEXT_TOKENS
        ),
        library=library,
    )


def _max_context_fit(library: PromptLibrary) -> MemoryKvTest:
    """The maximum-fit test: climb until the runtime refuses, and record where it did."""
    return MemoryKvTest(
        key="memory_kv.max_context_fit",
        name="Maximum context fit",
        category="memory",
        measurement_class=MeasurementClass.WARM.value,
        metrics=_PREFILL_METRICS,
        cases_spec=tuple(
            (
                f"fit-{size}",
                size,
                "Reply with the single word: ok.",
                # Deliberately **not** declared as a required context: the whole point of this
                # test is to send a case the run may not be able to serve and record what
                # happened. A declared requirement would skip it before it was tried.
                None,
            )
            for size in MAX_FIT_CONTEXT_TOKENS
        ),
        library=library,
    )


def _prefix_first_pass(library: PromptLibrary) -> MemoryKvTest:
    """The cold half of the cache-reuse pair: a long prefix nothing has seen yet."""
    return MemoryKvTest(
        key="memory_kv.prefix_first_pass",
        name="Shared prefix, first pass",
        category="memory",
        measurement_class=MeasurementClass.WARM.value,
        metrics=_PREFILL_METRICS,
        cases_spec=(
            (
                "prefix-first",
                SHARED_PREFIX_TOKENS,
                "Reply with the single word: ok.",
                SHARED_PREFIX_TOKENS + 256,
            ),
        ),
        library=library,
    )


def _prefix_reuse(library: PromptLibrary) -> MemoryKvTest:
    """The warm half: the identical prefix, several short follow-ups.

    The prefix text is byte-identical to ``prefix-first``'s, which is what gives the runtime
    something to reuse; only the instruction after it differs, so any prefill saving is the cache
    and not a shorter prompt.
    """
    return MemoryKvTest(
        key="memory_kv.prefix_reuse",
        name="Shared prefix, reused",
        category="memory",
        measurement_class=MeasurementClass.CACHE_REUSED.value,
        metrics=_PREFILL_METRICS,
        cases_spec=tuple(
            (
                f"prefix-reuse-{ordinal}",
                SHARED_PREFIX_TOKENS,
                instruction,
                SHARED_PREFIX_TOKENS + 256,
            )
            for ordinal, instruction in enumerate(
                (
                    "Reply with the single word: ok.",
                    "Reply with the single word: yes.",
                    "Reply with the single word: done.",
                )
            )
        ),
        library=library,
    )


@dataclass(frozen=True, slots=True)
class SampleWindow:
    """When one sample ran and at what context, so a telemetry reading can be attributed to it.

    Attributes:
        context_tokens: The context the sample occupied.
        started_at: When the request went out.
        ended_at: When it came back.
        succeeded: Whether the runtime served it. ``False`` is the maximum-fit boundary, and it is
            a measurement.
    """

    context_tokens: int
    started_at: datetime
    ended_at: datetime
    succeeded: bool


def stabilized_vram(
    window: SampleWindow,
    timestamps: Sequence[datetime],
    vram_used_bytes: Sequence[float | None],
) -> Measurement:
    """The device memory in use while one sample was running.

    Takes the **maximum** reading inside the sample's window rather than the mean: the KV cache
    grows through a generation and is at its largest at the end, and a mean would report a model
    as needing less memory than it actually asked the allocator for. That is the direction that
    causes an out-of-memory the estimate said could not happen.

    Args:
        window: The sample's interval.
        timestamps: The run's telemetry timestamps, ascending.
        vram_used_bytes: Used device memory at each of those instants; ``None`` where the reading
            was unavailable.

    Returns:
        The peak reading inside the window, or :data:`~baseaicore.UNSUPPORTED` when no telemetry
        sample fell inside it — a common, honest outcome for a request faster than the sampling
        interval, and one that must not become a zero.
    """
    inside = [
        value
        for stamp, value in zip(timestamps, vram_used_bytes, strict=True)
        if value is not None and window.started_at <= stamp <= window.ended_at
    ]
    return max(inside) if inside else UNSUPPORTED


def _row(
    key: str,
    result: MetricResult,
    *,
    unit: str,
    higher_is_better: bool,
    aggregation: str,
    sample_count: int,
    excluded_count: int,
    gpu_index: int,
) -> AggregatedMetric:
    """Assemble one run-level derived row, always naming its device."""
    return AggregatedMetric(
        metric_key=key,
        run_test_id=None,
        numeric_value=result.numeric_value,
        unavailable_reason=result.unavailable_reason,
        unit=unit,
        aggregation=aggregation,
        higher_is_better=higher_is_better,
        sample_count=sample_count,
        excluded_count=excluded_count,
        stddev=None,
        coefficient_of_variation=None,
        measurement_class=MeasurementClass.NOT_APPLICABLE,
        gpu_index=gpu_index,
    )


_DERIVED: tuple[tuple[str, str, bool, str], ...] = (
    ("theoretical_kv_bytes_per_token", "bytes", False, "max"),
    ("observed_kv_bytes_per_token", "bytes", False, "max"),
    ("observed_mb_per_1k_context", "MiB", False, "max"),
    ("kv_slope_fit_r_squared", "ratio", True, "max"),
    ("kv_overhead_ratio", "ratio", False, "max"),
    ("max_successful_context_tokens", "count", True, "max"),
    ("max_context_capped_by_configuration", "ratio", False, "max"),
    ("reuse_speedup", "ratio", True, "max"),
)
"""``(metric_key, unit, higher_is_better, aggregation)`` for every figure :func:`derive` emits."""


def derive(  # noqa: PLR0913 — every argument is a documented measurement input
    *,
    architecture: KvArchitecture,
    observations: Sequence[ContextObservation],
    attempts: Sequence[tuple[int, bool]],
    cold_prefill_ms: Measurement = UNSUPPORTED,
    warm_prefill_ms: Measurement = UNSUPPORTED,
    gpu_index: int = 0,
    multi_gpu_visible: bool = False,
    placement_known: bool = False,
    configured_limit: int | None = None,
) -> tuple[AggregatedMetric, ...]:
    """Turn one run's stored evidence into ``native.memory_kv``'s run-level metrics.

    Args:
        architecture: The descriptor fields plus the run's KV precision.
        observations: One stabilized VRAM reading per context length that ran.
        attempts: ``(context_tokens, succeeded)`` from the maximum-fit test, refusals included.
        cold_prefill_ms: Mean prompt-evaluation time over the first pass at the shared prefix.
        warm_prefill_ms: Mean prompt-evaluation time over the reuses of it.
        gpu_index: The device every figure is attributed to.
        multi_gpu_visible: Whether more than one GPU was visible during the run.
        placement_known: Whether the provider reported which device holds the model.
        configured_limit: The context ceiling the run was configured not to climb past.

    Returns:
        One row per figure in :data:`_DERIVED`, in that order, each naming ``gpu_index``. Where
        more than one GPU was visible and placement is unknown, **every** row carries
        ``multi_gpu_placement_unknown`` and no value: catalog §3.2 skips the whole suite in that
        case, and a partially-populated suite would be worse than none, because the populated half
        would look trustworthy.
    """
    if multi_gpu_visible and not placement_known:
        refusal = unavailable(REASON_MULTI_GPU_PLACEMENT_UNKNOWN)
        return tuple(
            _row(
                key,
                refusal,
                unit=unit,
                higher_is_better=higher_is_better,
                aggregation=aggregation,
                sample_count=0,
                excluded_count=len(observations) + len(attempts),
                gpu_index=gpu_index,
            )
            for key, unit, higher_is_better, aggregation in _DERIVED
        )

    theoretical = theoretical_kv_bytes_per_token(architecture)
    fit = fit_context_slope(observations)
    results: dict[str, tuple[MetricResult, int, int]] = {
        "theoretical_kv_bytes_per_token": (theoretical, 1 if theoretical.numeric_value else 0, 0),
        "observed_kv_bytes_per_token": (fit.slope_bytes_per_token, fit.sample_count, 0),
        "observed_mb_per_1k_context": (
            observed_mb_per_1k_context(fit.slope_bytes_per_token),
            fit.sample_count,
            0,
        ),
        "kv_slope_fit_r_squared": (fit.r_squared, fit.sample_count, 0),
        "kv_overhead_ratio": (
            kv_overhead_ratio(fit.slope_bytes_per_token, theoretical),
            fit.sample_count,
            0,
        ),
        "max_context_capped_by_configuration": (
            max_context_capped_by_configuration(attempts, configured_limit=configured_limit),
            sum(1 for _, ok in attempts if ok),
            sum(1 for _, ok in attempts if not ok),
        ),
        "max_successful_context_tokens": (
            max_successful_context_tokens(attempts, configured_limit=configured_limit),
            sum(1 for _, ok in attempts if ok),
            sum(1 for _, ok in attempts if not ok),
        ),
        "reuse_speedup": (
            cache_reuse_speedup(cold_prefill_ms, warm_prefill_ms),
            sum(1 for value in (cold_prefill_ms, warm_prefill_ms) if is_supported(value)),
            0,
        ),
    }
    return tuple(
        _row(
            key,
            results[key][0],
            unit=unit,
            higher_is_better=higher_is_better,
            aggregation=aggregation,
            sample_count=results[key][1],
            excluded_count=results[key][2],
            gpu_index=gpu_index,
        )
        for key, unit, higher_is_better, aggregation in _DERIVED
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
class MemoryKvBenchmark:
    """The ``native.memory_kv`` suite: its manifest, its prompt pack and its four tests."""

    manifest: BenchmarkManifest
    library: PromptLibrary

    @property
    def tests(self) -> Sequence[MemoryKvTest]:
        """The four tests, in the order they must run.

        The slope first, because it is the measurement the suite exists for and it should run
        against an uncontaminated allocator; the maximum-fit climb last of the three that grow the
        cache, because an out-of-memory rejection may leave the runtime in a state the earlier
        tests would rather not inherit. The cache-reuse pair sits between them, first pass before
        reuse — the order *is* the measurement there.
        """
        return (
            _context_slope(self.library),
            _prefix_first_pass(self.library),
            _prefix_reuse(self.library),
            _max_context_fit(self.library),
        )


def build(library: PromptLibrary | None = None) -> MemoryKvBenchmark:
    """Build the suite, verifying that the manifest describes the installed prompts.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one.

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` does not match the prompts the installed
            pack holds. Refused rather than recomputed: the subset hash is a fingerprint input, so
            correcting it here would let a stale manifest produce runs whose provenance describes
            prompts that were never rendered.
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
    return MemoryKvBenchmark(manifest=manifest, library=pack)
