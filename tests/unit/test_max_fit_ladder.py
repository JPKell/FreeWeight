"""``native.memory_kv``'s maximum-fit ladder is fitted to a configured ceiling (ADR-0121 §1).

The property: the ceiling changes what the suite measures, so it changes the built manifest's
``dataset_hashes`` — and the shipped ceiling leaves the shipped ladder, and its hash, untouched.
"""

from __future__ import annotations

from freeweight.benchmarks.memory_kv.benchmark import (
    MAX_FIT_CONTEXT_TOKENS,
    MAX_FIT_LADDER_DATASET_KEY,
    build,
    max_fit_ladder,
)


def test_the_ladder_is_truncated_extended_or_capped_at_the_ceiling() -> None:
    assert max_fit_ladder(131_072) == MAX_FIT_CONTEXT_TOKENS
    assert max_fit_ladder(32_768) == (8192, 16384, 32768)
    assert max_fit_ladder(40_000) == (8192, 16384, 32768, 40_000)
    assert max_fit_ladder(262_144) == (*MAX_FIT_CONTEXT_TOKENS, 262_144)
    assert max_fit_ladder(8192) == (8192,)


def test_two_ceilings_are_two_suites_and_the_shipped_one_is_the_default() -> None:
    shipped = build()
    capped = build(max_fit_context_tokens=32_768)
    assert shipped.max_fit_ladder == MAX_FIT_CONTEXT_TOKENS
    assert capped.max_fit_ladder == (8192, 16384, 32768)
    assert (
        shipped.manifest.dataset_hashes[MAX_FIT_LADDER_DATASET_KEY]
        != capped.manifest.dataset_hashes[MAX_FIT_LADDER_DATASET_KEY]
    )
    assert build(max_fit_context_tokens=131_072).manifest == shipped.manifest
    fit = capped.tests[-1]
    assert fit.key == "memory_kv.max_context_fit"
    assert [case[1] for case in fit.cases_spec] == [8192, 16384, 32768]
