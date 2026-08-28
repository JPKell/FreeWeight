"""freeweight.benchmarks.energy.benchmark — ``native.energy``, an estimate that says it is one.

Benchmark catalog §3.14. Two tests that keep the device busy long enough to be measured, and one
derivation that integrates the power the telemetry sampler recorded while they ran.

**There is no energy sensor here.** Every number this suite produces comes from power *samples*
multiplied by the real time between them, and :mod:`freeweight.benchmarks.energy.energy` is built
so that the label travels with the number: an
:class:`~freeweight.benchmarks.energy.energy.EnergyEstimate` carries ``method`` and
``is_estimate`` on every instance. The catalog's "always labelled a telemetry-derived estimate,
never instrumentation" is therefore a property of the value rather than a caption somebody has to
remember to render.

**The workload is deliberately dull.** Energy is a function of the work, so the suite's job is to
do a *known, repeatable* amount of it — a fixed prompt size and a fixed output length — rather than
anything interesting. A suite whose energy figure moved because the model wrote a longer answer
would be measuring verbosity.

**One device.** Power, temperature and energy are per device, and this suite refuses rather than
guesses when more than one GPU is visible and the provider does not say which holds the model
(ADR-0027 §3). The host's CPU temperature is the one figure here that is not a device figure, and
it is reported under its own key so it can never be mistaken for one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baseaicore import UNSUPPORTED, Measurement

from freeweight.benchmarks.energy.energy import (
    ENERGY_METHOD,
    PowerSample,
    energy_per_output_token,
    energy_per_request,
    energy_per_successful_task,
    integrate_energy_joules,
    merge_windows,
    output_tokens_per_joule,
    peak_power_watts,
    successful_tasks_per_kwh,
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
    "OUTPUT_TOKENS",
    "PROMPT_ID",
    "PROMPT_TOKENS",
    "EnergyBenchmark",
    "EnergyTest",
    "WorkloadScorer",
    "build",
    "derive",
    "load_manifest",
]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"

PROMPT_ID = "benchmarks.energy.workload"
"""The one prompt record every case in this suite renders."""

_CHARS_PER_TOKEN = 4
_FILLER_SENTENCE = "Energy is power integrated over the time the work actually took. "

PROMPT_TOKENS = 2048
"""Prompt size for the workload, in tokens. Large enough that prompt evaluation draws real power."""

OUTPUT_TOKENS = 512
"""Requested output length. Long enough that several power samples fall inside one generation."""


def _filler(tokens: int) -> str:
    """Return deterministic filler text of approximately ``tokens`` tokens."""
    target_chars = tokens * _CHARS_PER_TOKEN
    repeats = max(1, target_chars // len(_FILLER_SENTENCE) + 1)
    return (_FILLER_SENTENCE * repeats)[:target_chars].strip()


@dataclass(frozen=True, slots=True)
class WorkloadScorer:
    """Records that the workload ran. It scores nothing about the answer.

    ``1.0`` for a non-empty response, ``0.0`` for an empty one. The score exists so that
    "successful tasks per kWh" has a denominator that means something: a request that came back
    empty consumed energy and completed no task, and counting it as a success would make a broken
    model look efficient.
    """

    key: str = "response_arrived"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Score one response.

        Args:
            case: The case that produced ``response_text``. Unused: nothing about an energy case
                changes what "the work was done" means.
            response_text: Exactly what the model returned.

        Returns:
            ``1.0`` for any non-whitespace content, ``0.0`` otherwise.
        """
        return ScoreResult(
            score=1.0 if response_text.strip() else 0.0,
            method=self.method,
            detail={"case": case.case_id, "response_chars": len(response_text)},
        )


@dataclass(frozen=True, slots=True)
class EnergyTest:
    """One test of ``native.energy``: a fixed amount of work, repeated.

    Attributes:
        key: Stable test key within the suite.
        name: Human-readable name.
        category: The catalog category the suite contributes to.
        measurement_class: ``warm`` for both tests — a cold load draws a very different power
            profile, and mixing the two would produce a joules-per-token figure that describes
            neither.
        metrics: The per-sample metrics; the energy figures come from :func:`derive`.
        cases_spec: ``(case_id, prompt_tokens, instruction, required_context_tokens)`` per case.
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
    def scorer(self) -> WorkloadScorer:
        """The one scorer every case in this suite uses."""
        return WorkloadScorer()

    @property
    def streaming(self) -> bool:
        """Never: streaming changes the decode profile, and this suite measures decode power."""
        return False

    @property
    def requires(self) -> Mapping[str, Any]:
        """``token_counts``: joules per token has no denominator without them."""
        return {"provider_capabilities": ["token_counts"], "sandbox": False, "network": False}

    def cases(self) -> Iterator[BenchmarkCase]:
        """Yield this test's cases in declaration order, each rendered from the prompt record."""
        record = self.library.get(PROMPT_ID)
        for ordinal, (case_id, prompt_tokens, instruction, needed) in enumerate(self.cases_spec):
            rendered = record.render(
                {"passage": _filler(prompt_tokens), "instruction": instruction}
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
                    "suite": "native.energy",
                    "test": self.key,
                    "prompt_tokens_requested": prompt_tokens,
                },
            )


_WORK_METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        key="output_tokens",
        unit="count",
        higher_is_better=True,
        aggregation="mean",
        description="Generated tokens the provider reported — the work the energy was spent on.",
    ),
    MetricDefinition(
        key="decode_ms",
        unit="ms",
        higher_is_better=False,
        aggregation="mean",
        description="Time the provider reported generating output.",
    ),
)


def _sustained_generation(library: PromptLibrary) -> EnergyTest:
    """The main workload: three identical long generations, so the estimate has a spread."""
    return EnergyTest(
        key="energy.sustained_generation",
        name="Sustained generation",
        category="energy",
        measurement_class=MeasurementClass.WARM.value,
        metrics=_WORK_METRICS,
        cases_spec=tuple(
            (
                f"sustained-{ordinal}",
                PROMPT_TOKENS,
                f"Continue the passage above for about {OUTPUT_TOKENS} tokens. Do not stop early.",
                PROMPT_TOKENS + OUTPUT_TOKENS + 256,
            )
            for ordinal in range(3)
        ),
        library=library,
    )


def _prompt_dominated(library: PromptLibrary) -> EnergyTest:
    """A prompt-heavy, output-light case, so prefill energy is separable from decode energy."""
    return EnergyTest(
        key="energy.prompt_dominated",
        name="Prompt-dominated request",
        category="energy",
        measurement_class=MeasurementClass.WARM.value,
        metrics=_WORK_METRICS,
        cases_spec=(
            (
                "prompt-dominated",
                PROMPT_TOKENS * 2,
                "Reply with the single word: ok.",
                PROMPT_TOKENS * 2 + 256,
            ),
        ),
        library=library,
    )


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
    ("gpu_energy_joules", "J", False, "sum"),
    ("joules_per_request", "J", False, "mean"),
    ("joules_per_output_token", "J", False, "mean"),
    ("joules_per_successful_task", "J", False, "mean"),
    ("output_tokens_per_joule", "tokens/J", True, "mean"),
    ("successful_tasks_per_kwh", "tasks/kWh", True, "mean"),
    ("peak_gpu_power_watts", "W", False, "max"),
    ("max_cpu_temperature_c", "°C", False, "max"),
    ("throttling_suspected", "ratio", False, "max"),
)
"""``(metric_key, unit, higher_is_better, aggregation)`` for every figure :func:`derive` emits.

``mean_gpu_power_watts`` and ``max_gpu_temperature_c`` are deliberately absent: the run engine
already writes both from :func:`~freeweight.services.telemetry_recording.summarize_gpu_telemetry`
for **every** suite, and a second row under the same key from a second code path is how two
numbers for one fact get into a database."""


def derive(  # noqa: PLR0913 — every argument is a documented measurement input
    samples: Sequence[PowerSample],
    *,
    requests: int,
    successes: int,
    output_tokens: Measurement = UNSUPPORTED,
    max_cpu_temperature_c: Measurement = UNSUPPORTED,
    throttling_suspected: bool | None = None,
    gpu_index: int = 0,
    multi_gpu_visible: bool = False,
    placement_known: bool = False,
    request_windows: Sequence[tuple[datetime, datetime]] | None = None,
) -> tuple[AggregatedMetric, ...]:
    """Turn one run's power series into ``native.energy``'s run-level metrics.

    Args:
        samples: The device's power readings for the run's window, in any order.
        requests: Provider requests made inside that window.
        successes: Requests that completed with a non-empty answer.
        output_tokens: Total generated tokens the provider reported, or ``UNSUPPORTED``.
        max_cpu_temperature_c: The host's peak CPU temperature, reported under its own key so it
            is never mistaken for a device figure.
        throttling_suspected: The collector's verdict, or ``None`` where it could not tell.
            ``None`` is *not* ``False``: "the driver does not expose throttle reasons and the
            clocks were unreadable" is a different fact from "the device did not throttle".
        gpu_index: The device every figure is attributed to.
        multi_gpu_visible: Whether more than one GPU was visible during the run.
        placement_known: Whether the provider reported which device holds the model.
        request_windows: ``(started_at, completed_at)`` per stored sample. Given, every figure
            here describes the energy spent **on requests**: the integration is clipped to their
            union and the peak is taken from readings inside one. Without them the figures cover
            the run's whole wall-clock span — which includes the idle settle wait, the warm-ups
            and the cooldowns — and "joules per output token" is inflated by however much of the
            run was spent doing nothing.

    Returns:
        One row per figure in :data:`_DERIVED`, in that order. Where more than one GPU was visible
        and placement is unknown, every row carries ``multi_gpu_placement_unknown`` and no value.
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
                excluded_count=len(samples),
                gpu_index=gpu_index,
            )
            for key, unit, higher_is_better, aggregation in _DERIVED
        )

    bounds = merge_windows(request_windows) if request_windows else ()
    estimate = integrate_energy_joules(samples, gpu_index=gpu_index, windows=bounds or None)
    # The peak follows the same rule as the total: a peak drawn from a warm-up generation is not
    # this suite's subject, and reporting one figure over requests beside another over the whole
    # run would be two answers to one question.
    peak_from = (
        [
            sample
            for sample in samples
            if any(begin <= sample.timestamp <= end for begin, end in bounds)
        ]
        if bounds
        else list(samples)
    )
    throttle = (
        unavailable("throttle_state_unknown")
        if throttling_suspected is None
        else MetricResult(1.0 if throttling_suspected else 0.0)
    )
    results: Mapping[str, MetricResult] = {
        "gpu_energy_joules": estimate.joules,
        "joules_per_request": energy_per_request(estimate, requests=requests),
        "joules_per_output_token": energy_per_output_token(estimate, output_tokens=output_tokens),
        "joules_per_successful_task": energy_per_successful_task(estimate, successes=successes),
        "output_tokens_per_joule": output_tokens_per_joule(estimate, output_tokens=output_tokens),
        "successful_tasks_per_kwh": successful_tasks_per_kwh(estimate, successes=successes),
        "peak_gpu_power_watts": peak_power_watts(peak_from),
        "max_cpu_temperature_c": (
            MetricResult(max_cpu_temperature_c)
            if max_cpu_temperature_c is not UNSUPPORTED
            else unavailable("no_cpu_temperature")
        ),
        "throttling_suspected": throttle,
    }
    return tuple(
        _row(
            key,
            results[key],
            unit=unit,
            higher_is_better=higher_is_better,
            aggregation=aggregation,
            sample_count=estimate.interval_count,
            excluded_count=estimate.excluded_count,
            gpu_index=estimate.gpu_index,
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
class EnergyBenchmark:
    """The ``native.energy`` suite: its manifest, its prompt pack and its two tests.

    Attributes:
        manifest: The suite's manifest.
        library: The prompt pack its cases render from.
        method: How every joule this suite reports was arrived at, restated on the benchmark so
            the label reaches an export that only ever sees the suite.
    """

    manifest: BenchmarkManifest
    library: PromptLibrary
    method: str = ENERGY_METHOD

    @property
    def tests(self) -> Sequence[EnergyTest]:
        """The two tests: sustained generation first, then the prompt-dominated request."""
        return (_sustained_generation(self.library), _prompt_dominated(self.library))


def build(library: PromptLibrary | None = None) -> EnergyBenchmark:
    """Build the suite, verifying that the manifest describes the installed prompts.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one.

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` does not match the installed pack.
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
    return EnergyBenchmark(manifest=manifest, library=pack)
