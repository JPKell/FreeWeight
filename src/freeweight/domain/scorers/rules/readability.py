"""Rung-2 readability: ``readability``.

[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s readability row.
Three published formulas — Flesch reading ease, Flesch–Kincaid grade level and the Gunning fog
index — each scored against a user-declared band.

**The syllable count is a heuristic, and it says so.** English syllable counting without a
pronouncing dictionary is approximate; the vowel-group rule below is the one every implementation
of these indices uses, and it is stated here rather than hidden so that a user comparing this
number with another tool's knows why they differ by a tenth. Shipping a pronouncing dictionary
would make the criterion a measurement of that dictionary.

**These formulas were fitted to prose.** A response of three words has a grade level, and it
means nothing. :data:`MINIMUM_WORDS` is where this module stops answering: below it the
result is ``unsupported`` rather than a number computed from too little text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    UNSUPPORTED_NO_SENTENCES,
    RuleInvalid,
    RuleResult,
    band,
    proportional,
    sentences,
    words,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["FORMULAS", "MINIMUM_WORDS", "count_syllables", "readability"]

MINIMUM_WORDS = 30
"""Words below which a readability index is not reported.

Thirty, because these indices are ratios of words to sentences and syllables to words, and both
denominators are tiny in a short answer. A two-sentence reply can land anywhere from grade 3 to
grade 18 on a single long word."""

UNSUPPORTED_TOO_SHORT = "too_short_for_readability"
"""The response has fewer than :data:`MINIMUM_WORDS` words."""

_VOWELS = "aeiouy"
_SILENT_E = re.compile(r"[^aeiou]e$")

FORMULAS = ("flesch_reading_ease", "flesch_kincaid_grade", "gunning_fog")
"""The indices this build computes, in the order the docstring introduces them."""


def count_syllables(word: str) -> int:
    """Return an approximate syllable count for one English word.

    The standard vowel-group heuristic: count runs of vowels, drop a silent terminal ``e``, and
    never return less than one. Declared as an approximation in this module's docstring.

    Args:
        word: One word, letters only.

    Returns:
        At least ``1``.
    """
    lowered = word.casefold()
    groups = 0
    previous_was_vowel = False
    for character in lowered:
        is_vowel = character in _VOWELS
        if is_vowel and not previous_was_vowel:
            groups += 1
        previous_was_vowel = is_vowel
    if _SILENT_E.search(lowered) and groups > 1:
        groups -= 1
    return max(1, groups)


def _indices(text: str) -> dict[str, float]:
    """Compute all three indices from one response."""
    tokens = words(text)
    lines = sentences(text)
    word_count = len(tokens)
    sentence_count = max(1, len(lines))
    syllables = sum(count_syllables(token) for token in tokens)
    complex_words = sum(1 for token in tokens if count_syllables(token) >= 3)  # noqa: PLR2004
    words_per_sentence = word_count / sentence_count
    syllables_per_word = syllables / word_count
    return {
        "flesch_reading_ease": 206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word,
        "flesch_kincaid_grade": 0.39 * words_per_sentence + 11.8 * syllables_per_word - 15.59,
        "gunning_fog": 0.4 * (words_per_sentence + 100.0 * complex_words / word_count),
        "words_per_sentence": words_per_sentence,
        "syllables_per_word": syllables_per_word,
        "complex_word_rate": complex_words / word_count,
    }


def readability(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score a response's readability index against a band.

    Parameters:
        ``metric``
            One of :data:`FORMULAS`. ``"flesch_kincaid_grade"`` by default, because a grade level
            is the one of the three a non-specialist can act on.
        ``min`` / ``max``
            The inclusive band. At least one is required.
        ``tolerance``
            How far beyond a bound reaches zero. Defaults to half the band's width.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict, with all three indices in ``detail`` so a reader can see the one they know.
        ``unsupported`` for an empty response, a response shorter than :data:`MINIMUM_WORDS`, a
        response with no sentences, or a criterion declaring neither bound.

    Raises:
        RuleInvalid: ``metric`` is not one of :data:`FORMULAS`, a bound is not a number, or the
            minimum exceeds the maximum.
    """
    metric = str(parameters.get("metric", "flesch_kincaid_grade"))
    if metric not in FORMULAS:
        raise RuleInvalid(
            f"Rule parameter 'metric' must be one of {list(FORMULAS)}; got {metric!r}."
        )
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    low, high = band(parameters, "")
    if low is None and high is None:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_BAND)
    if not sentences(text):
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_SENTENCES)
    tokens = words(text)
    if len(tokens) < MINIMUM_WORDS:
        return RuleResult(
            score=None,
            detail={"word_count": len(tokens), "minimum_words": MINIMUM_WORDS},
            unsupported_reason=UNSUPPORTED_TOO_SHORT,
        )
    computed = _indices(text)
    value = computed[metric]
    tolerance = parameters.get("tolerance")
    if tolerance is not None and (
        isinstance(tolerance, bool) or not isinstance(tolerance, int | float)
    ):
        raise RuleInvalid(f"Rule parameter 'tolerance' must be a number; got {tolerance!r}.")
    return RuleResult(
        score=proportional(
            value, low, high, tolerance=None if tolerance is None else float(tolerance)
        ),
        detail={"metric": metric, "value": value, "min": low, "max": high, **computed},
    )
