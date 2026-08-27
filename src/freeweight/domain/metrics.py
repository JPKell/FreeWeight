"""freeweight.domain.metrics — every metric formula, as pure functions over one sample.

Pure domain: stdlib and :mod:`baseaicore` only. No database, no provider, no clock. That is what
lets every formula here be tested against known values, boundaries, division guards and
``UNSUPPORTED`` inputs without a fixture (spec §18).

**Three rules run through all of it.**

1. *A number that cannot be computed is :data:`~baseaicore.UNSUPPORTED` with a reason, never
   ``0`` and never ``None``* ([ADR-0016](../../../../docs/adr/0016-unavailable-is-not-zero.md)).
   Every formula returns a :class:`MetricResult`, which carries exactly one of "here is the value"
   and "here is why there isn't one".
2. *The provider's account of its own work is never mixed with what this process observed.*
   ``prompt_tokens_per_second`` divides the provider's token count by the provider's
   ``prompt_eval_ms``; ``total_ms`` is this process's monotonic observation and says so in its
   description. There is no formula here that divides one by the other.
3. *Chunk latency is not token latency.* :func:`inter_token_ms_mean` returns ``UNSUPPORTED`` with
   reason ``chunks_are_not_tokens`` unless the provider declared ``token_level_chunks`` for the
   call that produced the sample. It is a separate metric key from ``inter_chunk_ms_mean``, which
   is always available, so a per-token claim can never be made by relabelling a per-chunk number
   — the failure mode Phase 6 names.

Durations arriving here were measured with :func:`time.perf_counter_ns` at the call site; wall
clock never enters a duration (spec §15).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from baseaicore import UNSUPPORTED, Measurement, is_supported

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "AGGREGATE_METRIC_KEYS",
    "SAMPLE_METRICS",
    "MeasurementClass",
    "MetricResult",
    "SampleFacts",
    "chunk_latencies_ms",
    "decode_tokens_per_second",
    "inter_chunk_ms_mean",
    "inter_chunk_ms_p50",
    "inter_chunk_ms_p95",
    "inter_token_ms_mean",
    "output_tokens_per_success",
    "percentile",
    "prompt_tokens_per_second",
    "quality_per_1k_output_tokens",
    "rate_per_second",
    "successes_per_million_output_tokens",
    "total_tokens_per_success",
    "unavailable",
]

REASON_NOT_REPORTED = "not_reported"
"""The provider did not report an input this formula needs."""

REASON_ZERO_DURATION = "zero_duration"
"""The duration this rate would divide by is zero or negative — a rate would be meaningless."""

REASON_NO_SUCCESSES = "no_successful_samples"
"""A per-success figure was asked for and nothing succeeded. Zero successes is not zero cost."""

REASON_NO_OUTPUT_TOKENS = "no_output_tokens"
"""A per-token figure was asked for and no output tokens were produced or reported."""

REASON_CHUNKS_ARE_NOT_TOKENS = "chunks_are_not_tokens"
"""The provider does not emit one token per streamed delta, so no per-token latency exists."""

REASON_NOT_STREAMED = "not_streamed"
"""The sample came from a non-streaming call, which has no first-token or inter-chunk moment."""

REASON_MULTI_GPU_PLACEMENT_UNKNOWN = "multi_gpu_placement_unknown"
"""More than one GPU is visible and the provider does not say which holds the model.

ADR-0027 §3: a VRAM slope measured against the wrong device reads as zero bytes per token, which
is a fabricated measurement rather than an approximate one. Memory, KV and energy figures take
this reason rather than a number.
"""


class MeasurementClass(StrEnum):
    """What state the model was in when a measurement was taken (benchmark catalog §3.1).

    Stored on ``run_tests.measurement_class`` and carried through aggregation, because cold and
    warm numbers describe different things and averaging them produces a number that describes
    neither.
    """

    COLD = "cold"
    WARM = "warm"
    CACHE_REUSED = "cache_reused"
    NOT_APPLICABLE = "n/a"


@dataclass(frozen=True, slots=True)
class MetricResult:
    """One computed metric: a value, or an honest refusal with the reason for it.

    Attributes:
        value: The number, or :data:`~baseaicore.UNSUPPORTED`.
        unavailable_reason: Why there is no number. Non-``None`` exactly when ``value`` is
            ``UNSUPPORTED`` — the pairing is enforced, because a ``NULL`` metric with no reason is
            the row ADR-0016 exists to prevent, and a number with a reason beside it is a row
            nobody can interpret.

    Raises:
        ValueError: The value and the reason disagree.
    """

    value: Measurement
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Refuse a result whose value and reason contradict each other."""
        if is_supported(self.value) and self.unavailable_reason is not None:
            raise ValueError(
                f"A computed metric must not carry an unavailable reason; got "
                f"{self.unavailable_reason!r} alongside {self.value!r}."
            )
        if not is_supported(self.value) and self.unavailable_reason is None:
            raise ValueError(
                "An unavailable metric must say why; a NULL value with no reason is exactly the "
                "row ADR-0016 exists to prevent."
            )

    @property
    def numeric_value(self) -> float | None:
        """The value as the plain ``float`` storage takes, or ``None`` when unavailable."""
        return float(self.value) if is_supported(self.value) else None


def unavailable(reason: str) -> MetricResult:
    """Return an unavailable result carrying ``reason``."""
    return MetricResult(UNSUPPORTED, reason)


@dataclass(frozen=True, slots=True)
class SampleFacts:
    """Everything one stored sample offers a formula, in the units its column names declare.

    Every quantity is a :data:`~baseaicore.Measurement`, so "the provider did not report this"
    arrives here as ``UNSUPPORTED`` rather than as a zero that a rate would happily divide.

    Attributes:
        status: The sample's stored status. Only ``completed`` samples contribute to a metric.
        input_tokens: Prompt tokens the provider reported.
        output_tokens: Generated tokens the provider reported, thinking and tool syntax included.
        thinking_tokens: Reasoning tokens, already inside ``output_tokens``.
        tool_tokens: Tool-syntax tokens, already inside ``output_tokens``.
        output_chars: Characters generated.
        output_words: Whitespace-delimited words generated.
        output_bytes: UTF-8 bytes generated.
        client_wall_ms: What this process observed for the whole call.
        client_ttft_ms: What this process observed before the first delta. Streamed calls only.
        backend_load_ms: What the provider reported loading the model.
        backend_prompt_eval_ms: What the provider reported evaluating the prompt.
        backend_decode_ms: What the provider reported generating output.
        backend_total_ms: The provider's own total.
        score: The sample's score, or ``UNSUPPORTED`` when it was not scored.
        detail: The sample's ``result_json`` — where a streamed sample keeps its inter-chunk
            timings and whether those chunks were tokens.
    """

    status: str = "completed"
    input_tokens: Measurement = UNSUPPORTED
    output_tokens: Measurement = UNSUPPORTED
    thinking_tokens: Measurement = UNSUPPORTED
    tool_tokens: Measurement = UNSUPPORTED
    output_chars: Measurement = UNSUPPORTED
    output_words: Measurement = UNSUPPORTED
    output_bytes: Measurement = UNSUPPORTED
    client_wall_ms: Measurement = UNSUPPORTED
    client_ttft_ms: Measurement = UNSUPPORTED
    backend_load_ms: Measurement = UNSUPPORTED
    backend_prompt_eval_ms: Measurement = UNSUPPORTED
    backend_decode_ms: Measurement = UNSUPPORTED
    backend_total_ms: Measurement = UNSUPPORTED
    score: Measurement = UNSUPPORTED
    detail: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> SampleFacts:
        """Build from one ``samples`` row, mapping a ``NULL`` column to ``UNSUPPORTED``.

        The storage layer writes ``NULL`` for "the provider did not report this" (data model §2
        keeps plain nullable columns on ``samples``); this is the one place that ``NULL`` becomes
        the sentinel the formulas refuse to do arithmetic on.
        """

        def quantity(name: str) -> Measurement:
            value = row.get(name)
            return UNSUPPORTED if value is None else float(value)

        detail = row.get("result_json")
        return cls(
            status=str(row.get("status", "completed")),
            input_tokens=quantity("input_tokens"),
            output_tokens=quantity("output_tokens"),
            thinking_tokens=quantity("thinking_tokens"),
            tool_tokens=quantity("tool_tokens"),
            output_chars=quantity("output_chars"),
            output_words=quantity("output_words"),
            output_bytes=quantity("output_bytes"),
            client_wall_ms=quantity("client_wall_ms"),
            client_ttft_ms=quantity("client_ttft_ms"),
            backend_load_ms=quantity("backend_load_ms"),
            backend_prompt_eval_ms=quantity("backend_prompt_eval_ms"),
            backend_decode_ms=quantity("backend_decode_ms"),
            backend_total_ms=quantity("backend_total_ms"),
            score=quantity("score"),
            detail=detail if isinstance(detail, dict) else {},
        )


def rate_per_second(count: Measurement, duration_ms: Measurement) -> MetricResult:
    """Return ``count`` per second, or an honest refusal.

    The one division guard in this module; every throughput formula routes through it so there is
    exactly one place that decides what happens when a duration is zero.

    Args:
        count: The numerator — tokens, requests, anything counted.
        duration_ms: The elapsed milliseconds the count was produced in.

    Returns:
        ``count / (duration_ms / 1000)`` when both are reported and the duration is positive;
        ``UNSUPPORTED`` with :data:`REASON_NOT_REPORTED` when either input is unreported; and
        ``UNSUPPORTED`` with :data:`REASON_ZERO_DURATION` when the duration is zero or negative.
        A zero duration is never treated as "infinitely fast" and never as one millisecond.
    """
    if not is_supported(count) or not is_supported(duration_ms):
        return unavailable(REASON_NOT_REPORTED)
    if duration_ms <= 0:
        return unavailable(REASON_ZERO_DURATION)
    return MetricResult(float(count) / (float(duration_ms) / 1000.0))


def _reported(value: Measurement) -> MetricResult:
    """Return ``value`` as a result, or an unavailable one when the provider reported nothing."""
    return MetricResult(value) if is_supported(value) else unavailable(REASON_NOT_REPORTED)


def prompt_tokens_per_second(facts: SampleFacts) -> MetricResult:
    """Prompt-evaluation throughput, from the provider's own token count and its own duration."""
    return rate_per_second(facts.input_tokens, facts.backend_prompt_eval_ms)


def decode_tokens_per_second(facts: SampleFacts) -> MetricResult:
    """Decode throughput, from the provider's own token count and its own decode duration."""
    return rate_per_second(facts.output_tokens, facts.backend_decode_ms)


def chunk_latencies_ms(facts: SampleFacts) -> tuple[float, ...]:
    """Return the inter-chunk gaps a streamed sample recorded, in declaration order.

    Empty for a sample that was not streamed, or that produced fewer than two deltas — one delta
    has no gap after it, and inventing one would be a fabricated measurement.
    """
    raw = facts.detail.get("inter_chunk_ms")
    if not isinstance(raw, list):
        return ()
    return tuple(float(item) for item in raw if isinstance(item, int | float))


def _chunk_statistic(
    facts: SampleFacts, reducer: Callable[[Sequence[float]], float]
) -> MetricResult:
    """Apply ``reducer`` to a sample's inter-chunk gaps, refusing when there are none."""
    gaps = chunk_latencies_ms(facts)
    if not gaps:
        return unavailable(REASON_NOT_STREAMED)
    return MetricResult(reducer(gaps))


def percentile(values: Sequence[float], fraction: float) -> float:
    """Return the linearly interpolated percentile of ``values``.

    Args:
        values: At least one value; order is irrelevant, they are sorted here.
        fraction: ``0.0`` through ``1.0`` — ``0.5`` for p50, ``0.95`` for p95.

    Returns:
        The value at ``fraction`` of the way through the sorted sequence, interpolating between
        the two neighbouring samples. A single value is its own every percentile.

    Raises:
        ValueError: ``values`` is empty, or ``fraction`` is outside ``0.0..1.0``. Both are caller
            defects: there is no percentile of nothing, and returning ``0.0`` for one would be a
            fabricated number.
    """
    if not values:
        raise ValueError("percentile() needs at least one value; there is no percentile of none.")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError(f"percentile() fraction must be within 0.0..1.0; got {fraction!r}.")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def inter_chunk_ms_mean(facts: SampleFacts) -> MetricResult:
    """Mean gap between streamed deltas. **Chunk** latency, whatever a chunk turned out to be."""
    return _chunk_statistic(facts, lambda gaps: sum(gaps) / len(gaps))


def inter_chunk_ms_p50(facts: SampleFacts) -> MetricResult:
    """Median gap between streamed deltas."""
    return _chunk_statistic(facts, lambda gaps: percentile(gaps, 0.5))


def inter_chunk_ms_p95(facts: SampleFacts) -> MetricResult:
    """95th-percentile gap between streamed deltas."""
    return _chunk_statistic(facts, lambda gaps: percentile(gaps, 0.95))


def inter_token_ms_mean(facts: SampleFacts) -> MetricResult:
    """Mean per-**token** latency — available only where a delta really is one token.

    Returns:
        The same arithmetic as :func:`inter_chunk_ms_mean` when the sample recorded
        ``token_level_chunks: true``, and ``UNSUPPORTED`` with
        :data:`REASON_CHUNKS_ARE_NOT_TOKENS` otherwise. This is the guard, and it is a *separate
        metric key* rather than a caveat on the chunk metric, because a caveat is something a
        chart can drop and a missing number is not (ModelRack spec §11.4).
    """
    if not bool(facts.detail.get("token_level_chunks", False)):
        return unavailable(REASON_CHUNKS_ARE_NOT_TOKENS)
    return inter_chunk_ms_mean(facts)


def output_tokens_per_success(output_tokens: Measurement, successes: int) -> MetricResult:
    """Output tokens spent per successful sample (benchmark catalog §3.3).

    Returns:
        ``UNSUPPORTED`` with :data:`REASON_NO_SUCCESSES` when nothing succeeded — a suite that
        failed every case did not achieve its results cheaply, and dividing by zero successes to
        get "0 tokens per success" would say exactly that.
    """
    if not is_supported(output_tokens):
        return unavailable(REASON_NOT_REPORTED)
    if successes <= 0:
        return unavailable(REASON_NO_SUCCESSES)
    return MetricResult(float(output_tokens) / successes)


def total_tokens_per_success(
    input_tokens: Measurement, output_tokens: Measurement, successes: int
) -> MetricResult:
    """Input plus output tokens spent per successful sample (benchmark catalog §3.3)."""
    if not is_supported(input_tokens) or not is_supported(output_tokens):
        return unavailable(REASON_NOT_REPORTED)
    if successes <= 0:
        return unavailable(REASON_NO_SUCCESSES)
    return MetricResult((float(input_tokens) + float(output_tokens)) / successes)


def quality_per_1k_output_tokens(
    mean_score: Measurement, output_tokens: Measurement
) -> MetricResult:
    """Mean score obtained per thousand output tokens (benchmark catalog §3.3)."""
    if not is_supported(mean_score) or not is_supported(output_tokens):
        return unavailable(REASON_NOT_REPORTED)
    if output_tokens <= 0:
        return unavailable(REASON_NO_OUTPUT_TOKENS)
    return MetricResult(float(mean_score) / (float(output_tokens) / 1000.0))


def successes_per_million_output_tokens(successes: int, output_tokens: Measurement) -> MetricResult:
    """Successful samples obtained per million output tokens (benchmark catalog §3.3)."""
    if not is_supported(output_tokens):
        return unavailable(REASON_NOT_REPORTED)
    if output_tokens <= 0:
        return unavailable(REASON_NO_OUTPUT_TOKENS)
    return MetricResult(successes / (float(output_tokens) / 1_000_000.0))


SAMPLE_METRICS: Mapping[str, Callable[[SampleFacts], MetricResult]] = {
    # Counts and durations the provider reported, passed through so they aggregate like any other
    # metric rather than being read straight off a column by three different callers.
    "prompt_tokens": lambda facts: _reported(facts.input_tokens),
    "output_tokens": lambda facts: _reported(facts.output_tokens),
    "thinking_tokens": lambda facts: _reported(facts.thinking_tokens),
    "tool_tokens": lambda facts: _reported(facts.tool_tokens),
    "output_chars": lambda facts: _reported(facts.output_chars),
    "output_words": lambda facts: _reported(facts.output_words),
    "output_bytes": lambda facts: _reported(facts.output_bytes),
    "prompt_eval_ms": lambda facts: _reported(facts.backend_prompt_eval_ms),
    "decode_ms": lambda facts: _reported(facts.backend_decode_ms),
    "load_ms": lambda facts: _reported(facts.backend_load_ms),
    "total_ms": lambda facts: _reported(facts.client_wall_ms),
    "ttft_ms": lambda facts: (
        _reported(facts.client_ttft_ms)
        if is_supported(facts.client_ttft_ms)
        else unavailable(REASON_NOT_STREAMED)
    ),
    # Derived.
    "prompt_tokens_per_second": prompt_tokens_per_second,
    "decode_tokens_per_second": decode_tokens_per_second,
    "inter_chunk_ms_mean": inter_chunk_ms_mean,
    "inter_chunk_ms_p50": inter_chunk_ms_p50,
    "inter_chunk_ms_p95": inter_chunk_ms_p95,
    "inter_token_ms_mean": inter_token_ms_mean,
}
"""Per-sample derivations, by metric key.

A metric key that appears here is computed from the sample's own facts; one that does not is
either an aggregate-only key (:data:`AGGREGATE_METRIC_KEYS`) or a score-derived metric, and
:mod:`freeweight.domain.aggregation` decides which. The registry is what lets a benchmark declare
its metrics in a manifest without also shipping code to extract each one.
"""

AGGREGATE_METRIC_KEYS: frozenset[str] = frozenset(
    {
        "output_tokens_per_success",
        "total_tokens_per_success",
        "quality_per_1k_output_tokens",
        "successes_per_million_output_tokens",
    }
)
"""Metric keys that exist only over a *set* of samples (benchmark catalog §3.3).

"Tokens per success" has no per-sample meaning — one sample either succeeded or did not — so these
are computed once per group by :mod:`freeweight.domain.aggregation` rather than sample by sample.
"""
