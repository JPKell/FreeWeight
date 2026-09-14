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


def test_refinement_halves_the_gap_until_one_step_is_left() -> None:
    """ADR-0151: 32 768 served and 65 536 refused leaves a gap three launches take to 4 096."""
    test = build(max_fit_context_tokens=131_072).tests[0]
    tried: dict[str, bool | None] = {
        "fit-8192": True,
        "fit-16384": True,
        "fit-32768": True,
        "fit-65536": False,
        "fit-131072": False,
    }
    chosen = []
    for served in (False, True, False):
        (case,) = test.next_cases(tried)
        assert case.ordinal == len(tried)
        chosen.append(case.metadata[SERVE_CONTEXT_KEY])
        tried[case.case_id] = served

    assert chosen == [49152, 40960, 45056]
    assert test.next_cases(tried) == ()


def test_nothing_to_refine_without_a_served_rung_and_a_refused_one_above_it() -> None:
    test = build(max_fit_context_tokens=32_768).tests[0]

    assert test.next_cases({"fit-8192": True, "fit-16384": True, "fit-32768": True}) == ()
    assert test.next_cases({"fit-8192": False, "fit-16384": False}) == ()
    assert test.next_cases({"fit-8192": True, "fit-16384": None}) == ()
