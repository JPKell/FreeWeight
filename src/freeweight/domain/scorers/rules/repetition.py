"""Rung-2 self-repetition: ``repetition``.

[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s last rule row.
An n-gram self-repetition rate: what share of a response's n-grams have appeared earlier in the
same response. It catches the specific failure of a model that has run out of things to say and is
restating its opening in three different registers.

**Lower is better, so the score inverts.** The rate itself is in ``detail``; the score is the
proportional distance from the declared ceiling, so a criterion reads the same way as every other
one in the library — higher is better, everywhere.

**A response too short to repeat is not a repetitive response.** Fewer than ``n`` words yields no
n-gram at all, and a rate over an empty denominator is ``unsupported``, never zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    RuleInvalid,
    RuleResult,
    proportional,
    words,
)
from freeweight.domain.scorers.rules import (
    RuleResult as _RuleResult,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["DEFAULT_N", "repetition"]

DEFAULT_N = 4
"""The n-gram length, when a criterion does not say.

Four words, which is long enough that ordinary English collocations ("on the other hand") do not
dominate the count and short enough to catch a restated clause."""

UNSUPPORTED_TOO_SHORT = "too_short_for_ngrams"
"""The response has fewer words than the n-gram length, so there is no rate to compute."""


def repetition(text: str, parameters: Mapping[str, Any]) -> _RuleResult:
    """Score a response's n-gram self-repetition rate against a ceiling.

    Parameters:
        ``n``
            The n-gram length. :data:`DEFAULT_N` by default.
        ``max_rate``
            The share of repeated n-grams at which the score is still ``1.0``. ``0.1`` by default:
            some repetition is how English works.
        ``tolerance``
            How far above ``max_rate`` reaches zero. Defaults to half of ``max_rate``.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict, with the rate and the most-repeated n-grams in ``detail``. ``unsupported``
        for an empty response or one shorter than ``n`` words.

    Raises:
        RuleInvalid: ``n`` is not a whole number of at least 2, or ``max_rate`` is outside
            ``0.0..1.0``.
    """
    size = parameters.get("n", DEFAULT_N)
    if isinstance(size, bool) or not isinstance(size, int) or size < 2:  # noqa: PLR2004 — a bigram is the shortest n-gram
        raise RuleInvalid(f"Rule parameter 'n' must be a whole number of at least 2; got {size!r}.")
    ceiling = parameters.get("max_rate", 0.1)
    if isinstance(ceiling, bool) or not isinstance(ceiling, int | float):
        raise RuleInvalid(f"Rule parameter 'max_rate' must be a number; got {ceiling!r}.")
    if not 0.0 <= float(ceiling) <= 1.0:
        raise RuleInvalid(f"Rule parameter 'max_rate' must be within 0.0..1.0; got {ceiling!r}.")
    tolerance = parameters.get("tolerance")
    if tolerance is not None and (
        isinstance(tolerance, bool) or not isinstance(tolerance, int | float)
    ):
        raise RuleInvalid(f"Rule parameter 'tolerance' must be a number; got {tolerance!r}.")

    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    tokens = [token.casefold() for token in words(text)]
    if len(tokens) < size:
        return RuleResult(
            score=None,
            detail={"word_count": len(tokens), "n": size},
            unsupported_reason=UNSUPPORTED_TOO_SHORT,
        )

    counts: dict[tuple[str, ...], int] = {}
    for start in range(len(tokens) - size + 1):
        gram = tuple(tokens[start : start + size])
        counts[gram] = counts.get(gram, 0) + 1
    total = sum(counts.values())
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    rate = repeated / total
    reach = float(tolerance) if tolerance is not None else max(float(ceiling) / 2.0, 1e-9)
    worst = sorted(
        ((gram, count) for gram, count in counts.items() if count > 1),
        key=lambda item: (-item[1], item[0]),
    )[:5]
    return RuleResult(
        score=proportional(rate, None, float(ceiling), tolerance=reach),
        detail={
            "n": size,
            "ngram_count": total,
            "distinct_ngrams": len(counts),
            "repeated_ngrams": repeated,
            "repetition_rate": rate,
            "max_rate": float(ceiling),
            "most_repeated": [{"ngram": " ".join(gram), "count": count} for gram, count in worst],
        },
    )
