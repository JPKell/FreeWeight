"""freeweight.benchmarks.context_fit.benchmark — ``native.context_fit``, how much context fits.

ADR-0148. One test, ``context_fit.max_context``, climbing the maximum-fit ladder
(:func:`~freeweight.benchmarks.memory_kv.benchmark.max_fit_ladder`, fitted to
``benchmarks.max_fit_context_tokens``). **Each rung is its own launch**: a case declares
``serve_context_tokens`` and the run engine sends it under the run's profile with ``context_size``
replaced by that value, so the server is started at the rung rather than at the run's context. A
launch the card refuses is a failed sample, and the largest rung with a completed sample is the
answer.

Every other suite of a model waits for this one (ADR-0148 §5) and then runs at its answer (§6).
The arithmetic is ``native.memory_kv``'s own — this module adds cases, not formulas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.benchmarks.memory_kv import benchmark as memory_kv
from freeweight.benchmarks.memory_kv.kv import KvArchitecture
from freeweight.domain.benchmark import BenchmarkCase, BenchmarkManifest
from freeweight.domain.metrics import MeasurementClass
from freeweight.services.prompts import PromptLibrary, load_pack, prompt_subset_hash

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from freeweight.domain.aggregation import AggregatedMetric

__all__ = [
    "REFINE_STEP_TOKENS",
    "SERVE_CONTEXT_KEY",
    "SUITE_KEY",
    "TEST_KEY",
    "ContextFitBenchmark",
    "ContextFitTest",
    "build",
    "derive",
    "usable_context",
]

SUITE_KEY = "native.context_fit"
TEST_KEY = "context_fit.max_context"

SERVE_CONTEXT_KEY = "serve_context_tokens"
"""Case metadata naming the context this case is served at, replacing the run's own (ADR-0148 §1).

The run engine reads it for any suite, so the exception to "one run is one launch" is declared by
the case that needs it and never inferred from a suite key."""

PROMPT_HEADROOM_TOKENS = 512
"""How far below its rung a case's prompt is sized. The filler is a character estimate, and a
prompt that overshoots its own context would be refused by the server's context check — which
measures the arithmetic, not the card."""

REFINE_STEP_TOKENS = 4096
"""The resolution the fit is refined to between two ladder rungs (ADR-0151). Refined rungs are
multiples of it, so three launches take a 32 768-token gap to one step."""


def usable_context(fit_tokens: int, margin_tokens: int = REFINE_STEP_TOKENS) -> int:
    """The context a model is used at: its measured fit less a margin (ADR-0152, ADR-0153).

    A fit launched and served once, on a card whose other tenants take a varying share of its
    memory; the margin is KV cache left for that variation. It is
    ``benchmarks.context_fit_margin_tokens``, one refinement step by default.

    Args:
        fit_tokens: ``max_successful_context_tokens``.
        margin_tokens: How many tokens to hold back; ``0`` uses the fit as measured.

    Returns:
        ``fit_tokens - margin_tokens``, never below one refinement step unless the fit itself is.
    """
    return max(fit_tokens - margin_tokens, min(fit_tokens, REFINE_STEP_TOKENS))


_DERIVED_KEYS = frozenset({"max_successful_context_tokens", "max_context_capped_by_configuration"})
_MANIFEST_PATH = Path(__file__).parent / "manifest.json"


@dataclass(frozen=True, slots=True)
class ContextFitTest:
    """The one test: a case per ladder rung, each served at its rung.

    Attributes:
        ladder: The rungs this installation climbs, ascending.
        library: The loaded prompt pack the cases render from.
    """

    ladder: tuple[int, ...]
    library: PromptLibrary
    key: str = TEST_KEY
    name: str = "Maximum context fit"
    category: str = "memory"
    measurement_class: str = MeasurementClass.WARM.value

    @property
    def metrics(self) -> Sequence[Any]:
        """The prompt figures each sample produces; the fit itself comes from :func:`derive`."""
        return memory_kv._PREFILL_METRICS  # noqa: SLF001 — the same two figures, one definition

    @property
    def scorer(self) -> memory_kv.ContextProbeScorer:
        """``native.memory_kv``'s scorer: a response arrived, at a recorded context."""
        return memory_kv.ContextProbeScorer()

    @property
    def streaming(self) -> bool:
        """Never: a fit has no first-token moment."""
        return False

    @property
    def requires(self) -> Mapping[str, Any]:
        """Nothing beyond a provider that answers; a refusal is itself the measurement."""
        return {"provider_capabilities": [], "sandbox": False, "network": False}

    def cases(self) -> Iterator[BenchmarkCase]:
        """Yield one case per rung, each declaring the context it is served at.

        Deliberately no ``required_context_tokens``: a declared requirement would skip a rung
        before it was tried, and trying it is the test.
        """
        for ordinal, rung in enumerate(self.ladder):
            yield self._case(rung, ordinal)

    def next_cases(self, outcomes: Mapping[str, bool | None]) -> Sequence[BenchmarkCase]:
        """The rung halfway between the largest that served and the smallest refused above it.

        ADR-0151. The ladder doubles, so after it the fit is only known to lie between two rungs;
        this halves that gap, one launch at a time, on multiples of :data:`REFINE_STEP_TOKENS`,
        until it is one step.

        Args:
            outcomes: Case id → ``True`` served, ``False`` refused, ``None`` skipped, for every
                case tried so far.

        Returns:
            One case; or none when nothing served, nothing above the largest served rung was
            refused, or the gap is already within one step.
        """
        tried = {
            int(case_id.removeprefix("fit-")): served
            for case_id, served in outcomes.items()
            if served is not None and case_id.removeprefix("fit-").isdigit()
        }
        served = [rung for rung, ok in tried.items() if ok]
        if not served:
            return ()
        low = max(served)
        refused = [rung for rung, ok in tried.items() if not ok and rung > low]
        if not refused:
            return ()
        high = min(refused)
        middle = low + (high - low) // 2 // REFINE_STEP_TOKENS * REFINE_STEP_TOKENS
        if high - low <= REFINE_STEP_TOKENS or middle <= low:
            return ()
        return (self._case(middle, len(outcomes)),)

    def _case(self, rung: int, ordinal: int) -> BenchmarkCase:
        record = self.library.get(memory_kv.PROMPT_ID)
        rendered = record.render(
            {
                "passage": memory_kv._filler(max(rung - PROMPT_HEADROOM_TOKENS, 1)),  # noqa: SLF001
                "instruction": "Reply with the single word: ok.",
            }
        )
        return BenchmarkCase(
            case_id=f"fit-{rung}",
            ordinal=ordinal,
            prompt=rendered.user,
            system_prompt=rendered.system,
            prompt_id=rendered.prompt_id,
            prompt_version=rendered.version,
            expectation={},
            metadata={
                "suite": SUITE_KEY,
                "test": self.key,
                memory_kv.CONTEXT_TOKENS_DETAIL_KEY: rung,
                SERVE_CONTEXT_KEY: rung,
            },
        )


@dataclass(frozen=True, slots=True)
class ContextFitBenchmark:
    """The ``native.context_fit`` suite: its manifest, its prompt pack and its one test."""

    manifest: BenchmarkManifest
    library: PromptLibrary
    ladder: tuple[int, ...] = memory_kv.MAX_FIT_CONTEXT_TOKENS

    @property
    def tests(self) -> Sequence[ContextFitTest]:
        """The one test."""
        return (ContextFitTest(ladder=self.ladder, library=self.library),)


def derive(
    attempts: Sequence[tuple[int, bool]], *, gpu_index: int, multi_gpu_visible: bool
) -> tuple[AggregatedMetric, ...]:
    """Turn the rungs a run tried into ``max_successful_context_tokens`` and its capped flag.

    ``native.memory_kv``'s :func:`~freeweight.benchmarks.memory_kv.benchmark.derive`, fed no
    telemetry and filtered to the two fit figures. The configured limit is the **top rung this run
    tried** — a rung skipped past the model's trained context is not a rung it was allowed — never
    the run's own served context, which is what clamped ``memory_kv``'s figure to configuration.

    Args:
        attempts: ``(rung, succeeded)`` per sample that was sent. Skipped rungs are not attempts.
        gpu_index: The device the figures are attributed to.
        multi_gpu_visible: Whether more than one GPU was visible during the run.

    Returns:
        Two rows. ``max_successful_context_tokens`` is ``UNSUPPORTED`` when no rung served.
    """
    rows = memory_kv.derive(
        architecture=KvArchitecture(),
        observations=(),
        attempts=attempts,
        gpu_index=gpu_index,
        multi_gpu_visible=multi_gpu_visible,
        configured_limit=max((rung for rung, _ in attempts), default=None),
    )
    return tuple(row for row in rows if row.metric_key in _DERIVED_KEYS)


def build(
    library: PromptLibrary | None = None,
    *,
    max_fit_context_tokens: int = memory_kv.MAX_FIT_CONTEXT_TOKENS[-1],
) -> ContextFitBenchmark:
    """Build the suite, verifying that the manifest describes the installed prompts.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one.
        max_fit_context_tokens: ``benchmarks.max_fit_context_tokens``; the effective ladder is
            hashed into ``dataset_hashes`` exactly as ``native.memory_kv`` hashes its own
            (ADR-0121 §1).

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` does not match the installed pack.
        PromptNotFound: The manifest declares a prompt the installed pack does not have.
    """
    pack = library if library is not None else load_pack()
    manifest = BenchmarkManifest.from_json(json.loads(_MANIFEST_PATH.read_text(encoding="utf-8")))
    references = pack.references(
        (entry["prompt_id"], entry.get("version")) for entry in manifest.prompt_ids
    )
    actual = prompt_subset_hash(references)
    if manifest.prompt_subset_hash != actual:
        raise ValueError(
            f"Benchmark {manifest.key!r} declares prompt_subset_hash "
            f"{manifest.prompt_subset_hash!r}; the installed pack's declared prompts hash to "
            f"{actual!r}. Rebuild the manifest and bump the suite version."
        )
    ladder = memory_kv.max_fit_ladder(max_fit_context_tokens)
    resolved = replace(
        manifest,
        dataset_hashes={
            **manifest.dataset_hashes,
            memory_kv.MAX_FIT_LADDER_DATASET_KEY: memory_kv.max_fit_ladder_hash(ladder),
        },
    )
    return ContextFitBenchmark(manifest=resolved, library=pack, ladder=ladder)
