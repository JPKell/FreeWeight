"""The KV cost function, fitted across runs rather than inside one.

This is the study ADR-0034 §6 sends to the comparison surface. The reason it cannot be a benchmark
is a fact about the runtime rather than a preference: ``size_vram`` scales with the context a model
was *loaded* at, because llama.cpp allocates the whole KV slot up front. So a sweep of prompt
lengths inside one run measures KV **fill**; differencing ``model_vram_bytes`` across runs at
different ``context_size`` measures KV **cost**, and a benchmark is one run under one profile.

What is asserted here is mostly what the sweep *refuses* to fit, because a fitted line is a number
whether or not it means anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from freeweight.services.comparison import (
    KV_SWEEP_METRIC,
    MINIMUM_SWEEP_POINTS,
    Comparison,
    MetricCell,
    MetricRow,
    RunColumn,
    _context_sweep,
)

GIB = 1024**3


def _column(run_id: str, *, context: int | None, model: str = "ollama/qwen3:8b@sha256:abc") -> Any:
    """One comparison column, varying only what a sweep cares about."""
    return RunColumn(
        run_id=run_id,
        label=run_id,
        model_canonical_id=model,
        suite_key="native.memory_kv",
        suite_version="1.0.0",
        quantization="q8_0",
        family="qwen3",
        runtime_profile_hash=f"hash-{context}",
        kv_cache_precision="f16",
        context_size=context,
        machine_fingerprint="machine-1",
        machine_hostname="bench",
        gpu_index=0,
        started_at_rfc3339=None,
        fingerprint=f"fp-{run_id}",
        identity_confidence="digest",
    )


def _row(values: Mapping[str, float | None], *, metric_key: str = KV_SWEEP_METRIC) -> MetricRow:
    """One metric row carrying each run's residency."""
    from baseaicore import MetricKind

    return MetricRow(
        metric_key=metric_key,
        unit="bytes",
        higher_is_better=False,
        kind=MetricKind.MEMORY,
        cells=tuple(
            MetricCell(
                run_id=run_id,
                numeric_value=value,
                unavailable_reason=None if value is not None else "not_measured",
                unit="bytes",
                sample_count=1,
                excluded_count=0,
                stddev=None,
                coefficient_of_variation=None,
                gpu_index=0,
                group_index=0,
            )
            for run_id, value in values.items()
        ),
        groups=(),
        verdicts=(),
    )


# A real measurement from the reference machine: qwen3:8b, three served contexts.
REAL = {4_096: 5_578_204_118.0, 16_384: 7_520_177_356.0, 40_960: 11_194_387_660.0}


def _sweep_from(points: dict[int, float], **kwargs: object) -> Any:
    columns = [
        _column(f"run-{index}", context=context, **kwargs)  # type: ignore[arg-type]
        for index, context in enumerate(points)
    ]
    values = {column.run_id: points[column.context_size] for column in columns}
    return _context_sweep(columns, [_row(values)])


class TestTheFitItself:
    def test_it_recovers_the_cost_function_from_real_measurements(self) -> None:
        """Three runs on the reference machine: 4.64 GiB of weights, 148 KiB per token.

        Least squares over all three points, which is not the same estimator as differencing
        adjacent pairs — that gives ≈4.95 GiB + 157 KiB/token from the same data. The fit is the
        one to report because it uses every point and says how well they agree.
        """
        sweep = _sweep_from(REAL)

        assert sweep is not None
        assert sweep.weights_bytes / GIB == pytest.approx(4.64, abs=0.02)
        assert sweep.bytes_per_token / 1024 == pytest.approx(148.4, abs=0.5)
        assert sweep.r_squared > 0.999

    def test_it_predicts_what_a_scheduler_actually_asks(self) -> None:
        """The question LoadCoach has: can this machine serve that model at that context?"""
        sweep = _sweep_from(REAL)

        assert sweep is not None
        assert sweep.predict_bytes(16_384) == pytest.approx(REAL[16_384], rel=0.01)

    def test_the_points_are_ascending_by_context(self) -> None:
        sweep = _sweep_from(REAL)

        assert sweep is not None
        assert [context for context, _ in sweep.points] == sorted(REAL)

    def test_a_noisy_sweep_says_so_rather_than_hiding_it(self) -> None:
        """r² is the honesty check: a sweep taken on a busy GPU must not look clean."""
        noisy = {4_096: 5.0 * GIB, 16_384: 9.0 * GIB, 40_960: 6.0 * GIB}
        sweep = _sweep_from(noisy)

        assert sweep is not None
        assert sweep.r_squared < 0.5


class TestWhatItRefusesToFit:
    def test_two_points_are_not_a_sweep(self) -> None:
        """Two points fit exactly, so r² would be 1.0 by construction and mean nothing."""
        assert MINIMUM_SWEEP_POINTS == 3
        assert _sweep_from({4_096: 5.0 * GIB, 16_384: 7.0 * GIB}) is None

    def test_two_models_are_not_a_sweep(self) -> None:
        """Differencing VRAM across models measures the models, not the cost of context."""
        columns = [
            _column("a", context=4_096, model="ollama/qwen3:8b@sha256:abc"),
            _column("b", context=16_384, model="ollama/qwen3:8b@sha256:abc"),
            _column("c", context=40_960, model="ollama/llama3:8b@sha256:def"),
        ]
        values = {"a": 5.0 * GIB, "b": 7.0 * GIB, "c": 11.0 * GIB}

        assert _context_sweep(columns, [_row(values)]) is None

    def test_two_machines_are_not_a_sweep(self) -> None:
        columns = [
            _column("a", context=4_096),
            _column("b", context=16_384),
            _column("c", context=40_960),
        ]
        columns[2] = replace(columns[2], machine_fingerprint="machine-2")
        values = {"a": 5.0 * GIB, "b": 7.0 * GIB, "c": 11.0 * GIB}

        assert _context_sweep(columns, [_row(values)]) is None

    def test_repeat_runs_at_one_context_are_not_extra_points(self) -> None:
        """They are a repeatability check. Averaging them into the line would hide a disagreement,
        and counting them twice would pretend the sweep is wider than it is."""
        columns = [
            _column("a", context=4_096),
            _column("b", context=4_096),
            _column("c", context=16_384),
        ]
        values = {"a": 5.0 * GIB, "b": 5.1 * GIB, "c": 7.0 * GIB}

        assert _context_sweep(columns, [_row(values)]) is None

    def test_a_run_without_a_served_context_is_not_a_point(self) -> None:
        """Nothing to plot it against: an assumed context is not a number the run chose."""
        columns = [
            _column("a", context=4_096),
            _column("b", context=None),
            _column("c", context=40_960),
        ]
        values = {"a": 5.0 * GIB, "b": 7.0 * GIB, "c": 11.0 * GIB}

        assert _context_sweep(columns, [_row(values)]) is None

    def test_without_the_residency_metric_there_is_no_sweep(self) -> None:
        """It fits the *model's* reported residency, never the device total — which is what makes
        it an isolation rather than the mitigation ``native.memory_kv``'s in-run slope is."""
        columns = [
            _column("a", context=4_096),
            _column("b", context=16_384),
            _column("c", context=40_960),
        ]
        values = {"a": 5.0 * GIB, "b": 7.0 * GIB, "c": 11.0 * GIB}

        assert _context_sweep(columns, [_row(values, metric_key="peak_vram_bytes")]) is None

    def test_an_ordinary_comparison_carries_no_sweep(self) -> None:
        """The usual answer. It is derived, not requested, so most comparisons say nothing."""
        assert Comparison(columns=(), rows=()).context_sweep is None
