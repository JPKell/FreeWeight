"""freeweight.benchmarks.memory_kv.kv — the KV-cache arithmetic, as pure functions.

Benchmark catalog §3.2, and nothing else: given a model's architecture fields and a set of VRAM
readings, what does one token of context cost, what did it actually cost, and how far is the
runtime from the theory. No provider, no database, no telemetry collector — the observations
arrive as plain numbers, which is what lets the whole of this module be tested against
hand-computed values and against synthetic slopes with a known answer.

**A missing architecture field produces no number.** Every input is a
:data:`~baseaicore.Measurement`, and :func:`theoretical_kv_bytes_per_token` returns
``UNSUPPORTED`` with :data:`REASON_MISSING_ARCHITECTURE` the moment one of ``layers``,
``kv_heads`` or ``head_dim`` is unreported. The formula would happily produce a plausible number
from two of the three and a guess for the third, and that number would be wrong by exactly the
factor nobody could see (ADR-0016).

**A hybrid architecture is flagged and excluded, not forced through the formula.** ``2 × layers ×
kv_heads × head_dim`` describes a transformer's per-token key/value store. A Mamba or Jamba layer
keeps a fixed-size recurrent state that does not grow with context at all, so applying the
transformer formula to it does not approximate the answer — it invents one, and the invented one
is monotonically wrong in the direction of "this model needs far more memory than it does".

**The slope is reported with its fit quality.** Phase 9's named risk is VRAM slope noise from
other processes, and the mitigation is not to hide it: :func:`fit_context_slope` returns
``r_squared`` and the residual spread beside the slope, so a reader can see that a slope came from
a straight line rather than from a cloud.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baseaicore import UNSUPPORTED, Measurement, is_supported

from freeweight.domain.metrics import MetricResult, unavailable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "BYTES_PER_ELEMENT",
    "HYBRID_ARCHITECTURES",
    "REASON_HYBRID_ARCHITECTURE",
    "REASON_MISSING_ARCHITECTURE",
    "REASON_NO_COLD_PREFILL",
    "REASON_NO_THEORETICAL_BASELINE",
    "REASON_SINGLE_CONTEXT_POINT",
    "REASON_UNKNOWN_KV_PRECISION",
    "REASON_VRAM_DID_NOT_GROW",
    "ContextObservation",
    "KvArchitecture",
    "SlopeFit",
    "bytes_per_element_for",
    "cache_reuse_speedup",
    "max_context_capped_by_configuration",
    "fit_context_slope",
    "is_hybrid",
    "kv_overhead_ratio",
    "max_successful_context_tokens",
    "observed_mb_per_1k_context",
    "theoretical_kv_bytes_per_token",
]

REASON_MISSING_ARCHITECTURE = "missing_architecture_fields"
"""The descriptor did not report every field the theoretical formula needs.

Named for what is missing rather than for the metric, because the fix is a provider that reports
its architecture — not a different benchmark."""

REASON_HYBRID_ARCHITECTURE = "hybrid_architecture_not_modelled"
"""The architecture is not a pure transformer, so the per-token KV formula does not describe it."""

REASON_UNKNOWN_KV_PRECISION = "unknown_kv_precision"
"""The runtime profile's ``kv_cache_precision`` is not one this build knows the element size of."""

REASON_SINGLE_CONTEXT_POINT = "single_context_observation"
"""A slope was asked for from fewer than two context lengths. Two points make a line; one does
not, and a slope of zero from one reading would report that context is free."""

REASON_VRAM_DID_NOT_GROW = "context_axis_has_no_spread"
"""Every observation was taken at the same context length, so the slope's denominator is zero."""

REASON_NO_THEORETICAL_BASELINE = "no_theoretical_baseline"
"""An overhead ratio was asked for with no theoretical figure to divide by."""

REASON_NO_COLD_PREFILL = "no_cold_prefill_measurement"
"""A cache-reuse speed-up was asked for with no cold prefill to compare the warm one against."""

BYTES_PER_ELEMENT: Mapping[str, float] = {
    "f32": 4.0,
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 1.0,
    "q8": 1.0,
    "q5_1": 0.625,
    "q5_0": 0.625,
    "q4_0": 0.5,
    "q4": 0.5,
}
"""Bytes one cached key or value element occupies, by KV-cache precision.

The quantized entries carry their block scales: a ``q8_0`` block is 32 values in 32 bytes plus a
2-byte scale, which is 1.06 bytes per element rather than 1.0. The overhead is *deliberately* left
out of the theoretical figure and left visible in :func:`kv_overhead_ratio` instead — block scales,
padding and the runtime's own allocator all land in the same measured gap, and folding one of them
into "theory" would make the ratio look better without making the estimate better.

A precision this table does not name yields ``UNSUPPORTED`` with
:data:`REASON_UNKNOWN_KV_PRECISION`, never the ``f16`` default: guessing two bytes for a cache
that is actually four halves every figure downstream."""

HYBRID_ARCHITECTURES: frozenset[str] = frozenset(
    {"mamba", "mamba2", "jamba", "rwkv", "hybrid", "ssm", "state_space", "griffin", "recurrent"}
)
"""Architecture names whose per-token cost the transformer formula does not describe.

Matched as a whole word against the descriptor's lower-cased ``architecture``, and as a substring
only for the two families whose names are routinely suffixed (``mamba2-…``, ``jamba-…``)."""


def is_hybrid(architecture: str | None) -> bool:
    """Whether an architecture keeps recurrent state rather than a growing KV cache.

    Args:
        architecture: The descriptor's architecture name, or ``None`` when the provider reported
            none.

    Returns:
        ``True`` for a name in :data:`HYBRID_ARCHITECTURES` or one built on ``mamba``/``jamba``.
        ``False`` for ``None`` — an unreported architecture is not evidence of a hybrid one, and
        the missing-field guard already refuses the formula when the *fields* are absent.
    """
    if not architecture:
        return False
    name = architecture.strip().lower().replace("-", "_")
    if name in HYBRID_ARCHITECTURES:
        return True
    return any(name.startswith(prefix) for prefix in ("mamba", "jamba", "rwkv"))


def bytes_per_element_for(kv_cache_precision: str | None) -> MetricResult:
    """Return the byte size of one cached element at a given KV precision.

    Args:
        kv_cache_precision: The runtime profile's setting, e.g. ``"f16"`` or ``"q8_0"``. ``None``
            means the profile did not set one, which is the common case: the runtime then uses its
            own default, and this build refuses to assume what that was.

    Returns:
        The size in bytes, or ``UNSUPPORTED`` with :data:`REASON_UNKNOWN_KV_PRECISION`.
    """
    if not kv_cache_precision:
        return unavailable(REASON_UNKNOWN_KV_PRECISION)
    size = BYTES_PER_ELEMENT.get(kv_cache_precision.strip().lower())
    if size is None:
        return unavailable(REASON_UNKNOWN_KV_PRECISION)
    return MetricResult(size)


@dataclass(frozen=True, slots=True)
class KvArchitecture:
    """The descriptor fields the theoretical KV formula reads, and nothing more.

    A value object rather than the whole :class:`~baseaicore.ModelDescriptor` so that this module
    is testable from four numbers, and so that adding a descriptor field cannot silently change
    what the formula uses.

    Attributes:
        layers: Transformer layer count.
        kv_heads: Key/value head count — equal to the attention-head count without grouped-query
            attention, and the field that makes GQA models cost a fraction of what the head count
            alone would suggest.
        head_dim: Dimension of one attention head.
        architecture: The architecture name, used only to refuse a hybrid model.
        kv_cache_precision: The runtime profile's KV precision, which decides the element size.
    """

    layers: Measurement = UNSUPPORTED
    kv_heads: Measurement = UNSUPPORTED
    head_dim: Measurement = UNSUPPORTED
    architecture: str | None = None
    kv_cache_precision: str | None = None

    @property
    def is_hybrid(self) -> bool:
        """Whether this architecture is excluded from the transformer formula."""
        return is_hybrid(self.architecture)


def theoretical_kv_bytes_per_token(architecture: KvArchitecture) -> MetricResult:
    """Bytes of KV cache one token of context requires, from the descriptor alone.

    ``2 × layers × kv_heads × head_dim × bytes_per_element`` — the factor of two is the key and
    the value, which are stored separately and are the same shape (benchmark catalog §3.2).

    Args:
        architecture: The four descriptor fields and the runtime's KV precision.

    Returns:
        The per-token cost in bytes. ``UNSUPPORTED`` with :data:`REASON_HYBRID_ARCHITECTURE` for a
        state-space or hybrid model — flagged and excluded rather than forced through a formula
        that does not describe it — and with :data:`REASON_MISSING_ARCHITECTURE` when any of
        ``layers``, ``kv_heads`` or ``head_dim`` is unreported, or
        :data:`REASON_UNKNOWN_KV_PRECISION` when the element size is unknown. Never a number built
        from a guessed field: the guess is invisible in the output and the error it causes is a
        clean multiple.
    """
    if architecture.is_hybrid:
        return unavailable(REASON_HYBRID_ARCHITECTURE)
    fields = (architecture.layers, architecture.kv_heads, architecture.head_dim)
    if not all(is_supported(value) for value in fields):
        return unavailable(REASON_MISSING_ARCHITECTURE)
    element = bytes_per_element_for(architecture.kv_cache_precision)
    if element.numeric_value is None:
        return element
    layers, kv_heads, head_dim = (float(value) for value in fields)
    if layers <= 0 or kv_heads <= 0 or head_dim <= 0:
        return unavailable(REASON_MISSING_ARCHITECTURE)
    return MetricResult(2.0 * layers * kv_heads * head_dim * element.numeric_value)


@dataclass(frozen=True, slots=True)
class ContextObservation:
    """One stabilized VRAM reading at one context length.

    Attributes:
        context_tokens: The context the model was actually serving — the *served* context, never
            the advertised maximum (benchmark catalog §3.2).
        vram_used_bytes: Device memory in use once the reading had stabilized, on the device the
            run attributes its metrics to.
    """

    context_tokens: int
    vram_used_bytes: float


@dataclass(frozen=True, slots=True)
class SlopeFit:
    """A least-squares line through VRAM against context, with how well it fits.

    Attributes:
        slope_bytes_per_token: The fitted gradient — the observed KV cost of one token.
        intercept_bytes: The fitted zero-context intercept, which is approximately the weights
            plus the runtime's fixed allocations. Reported because a slope fitted with a
            nonsensical intercept is a slope nobody should trust.
        r_squared: Coefficient of determination in ``0.0..1.0``. Phase 9's named risk is slope
            noise from other processes, and this is the number that makes the noise visible
            instead of averaging it into the answer.
        residual_stddev_bytes: Spread of the observations around the fitted line.
        sample_count: Observations the fit used.
    """

    slope_bytes_per_token: MetricResult
    intercept_bytes: MetricResult
    r_squared: MetricResult
    residual_stddev_bytes: MetricResult
    sample_count: int


def _unfittable(reason: str, sample_count: int) -> SlopeFit:
    """A fit that could not be computed, with the same reason on every one of its figures."""
    return SlopeFit(
        slope_bytes_per_token=unavailable(reason),
        intercept_bytes=unavailable(reason),
        r_squared=unavailable(reason),
        residual_stddev_bytes=unavailable(reason),
        sample_count=sample_count,
    )


def fit_context_slope(observations: Sequence[ContextObservation]) -> SlopeFit:
    """Fit VRAM against context length by ordinary least squares.

    Args:
        observations: Stabilized readings at two or more distinct context lengths. Order is
            irrelevant; repeated readings at one length are averaged into that length's point by
            the fit itself, since least squares already weights them that way.

    Returns:
        The fit. ``UNSUPPORTED`` with :data:`REASON_SINGLE_CONTEXT_POINT` for fewer than two
        observations and :data:`REASON_VRAM_DID_NOT_GROW` when every observation sits at one
        context length — a vertical line has no gradient, and returning ``0`` would report that
        context costs nothing.

        ``r_squared`` is ``1.0`` for an exact fit, including the two-point case, where it is
        exact by construction rather than by agreement — the ``sample_count`` beside it is what
        says so.
    """
    if len(observations) < 2:
        return _unfittable(REASON_SINGLE_CONTEXT_POINT, len(observations))
    xs = [float(item.context_tokens) for item in observations]
    ys = [float(item.vram_used_bytes) for item in observations]
    mean_x = math.fsum(xs) / len(xs)
    mean_y = math.fsum(ys) / len(ys)
    variance_x = math.fsum((x - mean_x) ** 2 for x in xs)
    if variance_x <= 0:
        return _unfittable(REASON_VRAM_DID_NOT_GROW, len(observations))
    covariance = math.fsum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = covariance / variance_x
    intercept = mean_y - slope * mean_x
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys, strict=True)]
    residual_sum = math.fsum(value**2 for value in residuals)
    total_sum = math.fsum((y - mean_y) ** 2 for y in ys)
    r_squared = 1.0 if total_sum <= 0 else 1.0 - residual_sum / total_sum
    spread = (
        MetricResult(math.sqrt(residual_sum / (len(observations) - 2)))
        if len(observations) > 2
        else unavailable(REASON_SINGLE_CONTEXT_POINT)
    )
    return SlopeFit(
        slope_bytes_per_token=MetricResult(slope),
        intercept_bytes=MetricResult(intercept),
        r_squared=MetricResult(r_squared),
        residual_stddev_bytes=spread,
        sample_count=len(observations),
    )


def observed_mb_per_1k_context(slope_bytes_per_token: MetricResult) -> MetricResult:
    """Restate an observed slope in the unit the catalog reports it in.

    Mebibytes per 1 024 tokens of context — the same measurement, in the unit a person reads on a
    VRAM budget. Unavailable in, unavailable out, with the reason carried through unchanged.
    """
    if slope_bytes_per_token.numeric_value is None:
        return slope_bytes_per_token
    return MetricResult(slope_bytes_per_token.numeric_value * 1024.0 / (1024.0 * 1024.0))


def kv_overhead_ratio(observed: MetricResult, theoretical: MetricResult) -> MetricResult:
    """Observed bytes per token divided by theoretical bytes per token.

    A **runtime efficiency** figure and never a quality one (benchmark catalog §3.2): a ratio of
    1.3 says this runtime spends 30 % more than the architecture requires on block scales, padding
    and allocator granularity. It says nothing whatever about the model's answers.

    Args:
        observed: The measured slope, from :func:`fit_context_slope`.
        theoretical: The architectural figure, from :func:`theoretical_kv_bytes_per_token`.

    Returns:
        The ratio, or ``UNSUPPORTED`` carrying the reason whichever input was missing, and
        :data:`REASON_NO_THEORETICAL_BASELINE` where the theoretical figure is zero or negative.
    """
    if observed.numeric_value is None:
        return observed
    if theoretical.numeric_value is None:
        return theoretical
    if theoretical.numeric_value <= 0:
        return unavailable(REASON_NO_THEORETICAL_BASELINE)
    return MetricResult(observed.numeric_value / theoretical.numeric_value)


def max_successful_context_tokens(
    attempts: Sequence[tuple[int, bool]], *, configured_limit: int | None = None
) -> MetricResult:
    """The largest context that actually served a generation.

    **An out-of-memory rejection is the measurement, not a failure of it.** The maximum-context-fit
    test climbs until the runtime says no; the length it said no at is the answer this metric
    exists to report, so an OOM attempt contributes a ``False`` here and the run stays successful.
    Treating it as a failed run would throw away the one number the test was taken to get.

    Args:
        attempts: ``(context_tokens, succeeded)`` per attempt, in any order.
        configured_limit: The ceiling the run was configured not to climb past, or ``None``. When
            the largest success *is* the ceiling, the true maximum is unknown but not unmeasured —
            the value is still the ceiling, since that is what was demonstrated to work.

    Returns:
        The largest context with a successful attempt. ``UNSUPPORTED`` with
        :data:`REASON_SINGLE_CONTEXT_POINT` when nothing succeeded at all — a model that failed at
        every length has no maximum, and reporting the smallest length tried would claim it worked
        there.

        :func:`max_context_capped_by_configuration` is the companion that says which of the two
        this number is — the model's limit, or the ceiling the sweep was told not to pass.
    """
    successes = [tokens for tokens, ok in attempts if ok]
    if not successes:
        return unavailable(REASON_SINGLE_CONTEXT_POINT)
    largest = max(successes)
    capped = configured_limit is not None and largest >= configured_limit
    if configured_limit is not None:
        largest = min(largest, configured_limit)
    del capped
    return MetricResult(float(largest))


def max_context_capped_by_configuration(
    attempts: Sequence[tuple[int, bool]], *, configured_limit: int | None = None
) -> MetricResult:
    """Whether :func:`max_successful_context_tokens` reported a ceiling rather than a limit.

    ``1.0`` means the sweep succeeded at the highest context it was *allowed* to try, so the
    model's real maximum is at or above the reported number and was never established. ``0.0``
    means the model itself refused at the next rung, which is the measurement the sibling metric
    exists to take.

    The two cases report the identical number and are not the same fact — the first is the
    configuration's limit and the second is the model's — and a reader comparing two models cannot
    tell them apart without this (``PHASE9_ISSUES.md`` §7). ``UNSUPPORTED`` when nothing succeeded,
    because there is then no maximum for the question to be about.

    Args:
        attempts: ``(context_tokens, succeeded)`` per attempt, in any order.
        configured_limit: The ceiling the run was configured not to climb past, or ``None``.

    Returns:
        ``1.0``, ``0.0``, or ``UNSUPPORTED`` with :data:`REASON_SINGLE_CONTEXT_POINT`.
    """
    successes = [tokens for tokens, ok in attempts if ok]
    if not successes:
        return unavailable(REASON_SINGLE_CONTEXT_POINT)
    if configured_limit is None:
        return MetricResult(0.0)
    return MetricResult(1.0 if max(successes) >= configured_limit else 0.0)


def cache_reuse_speedup(cold_prefill_ms: Measurement, warm_prefill_ms: Measurement) -> MetricResult:
    """How many times faster a reused prefix was than the same prefix computed cold.

    Args:
        cold_prefill_ms: Prompt-evaluation time for the first request over a long shared prefix.
        warm_prefill_ms: Prompt-evaluation time for a follow-up that could reuse it.

    Returns:
        ``cold / warm``. Greater than one means the cache helped. ``UNSUPPORTED`` with
        :data:`REASON_NO_COLD_PREFILL` when either duration is unreported or the warm duration is
        zero or negative — a division that would report an infinite speed-up from a rounding
        artefact.
    """
    if not is_supported(cold_prefill_ms) or not is_supported(warm_prefill_ms):
        return unavailable(REASON_NO_COLD_PREFILL)
    if float(warm_prefill_ms) <= 0 or float(cold_prefill_ms) <= 0:
        return unavailable(REASON_NO_COLD_PREFILL)
    return MetricResult(float(cold_prefill_ms) / float(warm_prefill_ms))
