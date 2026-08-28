"""Rung-2 punctuation: ``punctuation_profile``.

[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s punctuation row:
em-dash, semicolon and exclamation rates per thousand words. Small, and more useful than it looks —
a great deal of what people recognise as a house style, or as an LLM's default register, lives in
punctuation habits rather than in word choice.

**Rates, not counts.** A count per response is a measurement of length. Every figure here is per
thousand words, so a paragraph and an essay are comparable, and the raw counts travel alongside so
a reader can check the arithmetic.

**Every mark is counted literally.** An em dash is U+2014; a double hyphen is not one, and this
module does not normalize them together. A rubric that means either says so with two bands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    RuleResult,
    band,
    proportional,
    words,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["MARKS", "punctuation_profile"]

_PER = 1000.0

MARKS: dict[str, str] = {
    "em_dash": "—",
    "en_dash": "–",
    "semicolon": ";",
    "colon": ":",
    "exclamation": "!",
    "question": "?",
    "ellipsis": "…",
    "parenthesis": "(",
}
"""The marks a criterion may band, and the character each counts.

Named rather than inferred from the band key so a rubric cannot ask for a mark this build silently
ignores: a band naming something absent from this table is refused at pack-load time."""


def punctuation_profile(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score a response's punctuation rates against per-thousand-word bands.

    Parameters:
        ``<mark>_per_1000_words``
            A ``{"min": …, "max": …}`` band, for any ``<mark>`` in :data:`MARKS`. At least one is
            required.

    The score is the mean of the declared bands' sub-scores.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict, with every mark's rate in ``detail`` — including the ones the criterion did
        not band, because a user looking at why a response felt wrong benefits from seeing the
        whole profile.

        ``unsupported`` for an empty response, and for a criterion declaring no band at all.

    Raises:
        RuleInvalid: A band names a mark :data:`MARKS` does not carry, a bound is not a number, or
            a band's minimum exceeds its maximum.
    """
    from freeweight.domain.scorers.rules import RuleInvalid

    declared: dict[str, tuple[float | None, float | None]] = {}
    for key in parameters:
        if not key.endswith("_per_1000_words"):
            continue
        mark = key[: -len("_per_1000_words")]
        if mark not in MARKS:
            raise RuleInvalid(
                f"Rule parameter {key!r} names punctuation this build does not count; the known "
                f"marks are {sorted(MARKS)}."
            )
        declared[mark] = band(parameters, key)
    if not declared:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_BAND)
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    word_count = len(words(text))
    if not word_count:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)

    counts = {mark: text.count(character) for mark, character in MARKS.items()}
    rates = {mark: count * _PER / word_count for mark, count in counts.items()}
    parts = [proportional(rates[mark], low, high) for mark, (low, high) in sorted(declared.items())]
    return RuleResult(
        score=sum(parts) / len(parts),
        detail={
            "word_count": word_count,
            "counts": counts,
            "rates_per_1000_words": rates,
            "bands": {mark: list(bounds) for mark, bounds in sorted(declared.items())},
        },
    )
