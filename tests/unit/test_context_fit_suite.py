"""``native.context_fit`` (ADR-0148): one case per rung, each served at its rung, and a fit derived
from the rungs a run tried — never from the run's own served context."""

from __future__ import annotations

from freeweight.benchmarks.context_fit.benchmark import SERVE_CONTEXT_KEY, build, derive
from freeweight.benchmarks.memory_kv.benchmark import MAX_FIT_LADDER_DATASET_KEY


def _values(attempts: list[tuple[int, bool]]) -> dict[str, float | None]:
    rows = derive(attempts, gpu_index=0, multi_gpu_visible=False)
    return {row.metric_key: row.numeric_value for row in rows}


def test_every_case_is_served_at_its_own_rung_and_requires_nothing() -> None:
    suite = build(max_fit_context_tokens=32_768)
    cases = list(suite.tests[0].cases())

    assert [case.case_id for case in cases] == ["fit-8192", "fit-16384", "fit-32768"]
    assert [case.metadata[SERVE_CONTEXT_KEY] for case in cases] == [8192, 16384, 32768]
    assert all(case.required_context_tokens is None for case in cases)
    shipped = build().manifest.dataset_hashes[MAX_FIT_LADDER_DATASET_KEY]
    assert suite.manifest.dataset_hashes[MAX_FIT_LADDER_DATASET_KEY] != shipped


def test_a_refused_rung_bounds_the_fit_and_is_not_a_cap() -> None:
    assert _values([(8192, True), (16384, True), (32768, False)]) == {
        "max_successful_context_tokens": 16384.0,
        "max_context_capped_by_configuration": 0.0,
    }


def test_the_top_rung_serving_is_a_cap_not_a_limit() -> None:
    assert _values([(8192, True), (16384, True)]) == {
        "max_successful_context_tokens": 16384.0,
        "max_context_capped_by_configuration": 1.0,
    }


def test_nothing_served_is_unavailable_never_zero() -> None:
    rows = derive([(8192, False)], gpu_index=0, multi_gpu_visible=False)

    assert rows
    assert all(row.numeric_value is None and row.unavailable_reason for row in rows)
