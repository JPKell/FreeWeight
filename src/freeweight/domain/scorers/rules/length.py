"""Rung-2 length and shape rules: ``word_count``, ``sentence_length_distribution``,
``paragraph_shape``.

[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s three
distribution rows. These are the rules that catch *rhythm* — the thing a reader notices before
they notice anything else, and the thing most people describe as "voice" without realising it is
partly arithmetic.

**A distribution needs something to be a distribution over.** A response with no sentence, or one
whose whole content is a single word, returns ``unsupported`` rather than a coefficient of
variation computed from one observation. One sentence has no spread, and reporting ``0.0`` would
claim perfect uniformity from a single sample — the same rule
:mod:`freeweight.domain.aggregation` applies to dispersion everywhere else.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    UNSUPPORTED_NO_SENTENCES,
    RuleResult,
    band,
    paragraphs,
    proportional,
    sentences,
    words,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["paragraph_shape", "sentence_length_distribution", "word_count"]

_MINIMUM_FOR_SPREAD = 2
"""Sentences needed before a coefficient of variation means anything."""


def _tolerance(parameters: Mapping[str, Any], name: str = "tolerance") -> float | None:
    """Read an optional decay tolerance, or ``None`` for the shared default."""
    value = parameters.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        from freeweight.domain.scorers.rules import RuleInvalid

        raise RuleInvalid(f"Rule parameter {name!r} must be a number; got {value!r}.")
    return float(value)


def word_count(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score a response's total length against a band.

    Parameters:
        ``min`` / ``max``
            The inclusive word-count band. At least one is required.
        ``tolerance``
            How many words beyond a bound reach a score of zero. Defaults to half the band's
            width (:func:`~freeweight.domain.scorers.rules.proportional`).

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict. ``unsupported`` for an empty response, and for a criterion declaring neither
        bound — a band satisfied by every response measures nothing, and reporting ``1.0`` would
        put a criterion into the composite that can never move it.

    Raises:
        RuleInvalid: A bound is not a number, the minimum exceeds the maximum, or ``tolerance``
            is not positive.
    """
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    low, high = band(parameters, "")
    if low is None and high is None:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_BAND)
    count = len(words(text))
    return RuleResult(
        score=proportional(count, low, high, tolerance=_tolerance(parameters)),
        detail={"word_count": count, "min": low, "max": high},
    )


def _mean_and_cv(lengths: Sequence[int]) -> tuple[float, float | None]:
    """Return the mean sentence length and its coefficient of variation.

    The coefficient is ``None`` for a single sentence: one observation has no spread, and a zero
    would claim perfect uniformity the response never demonstrated.
    """
    mean = sum(lengths) / len(lengths)
    if len(lengths) < _MINIMUM_FOR_SPREAD or mean == 0:
        return mean, None
    variance = sum((length - mean) ** 2 for length in lengths) / (len(lengths) - 1)
    return mean, math.sqrt(variance) / mean


def sentence_length_distribution(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score the mean and the variability of a response's sentence lengths.

    Parameters:
        ``mean_words``
            A ``{"min": …, "max": …}`` band on the mean words per sentence.
        ``cv``
            A band on the coefficient of variation — the measure of *rhythm*. A high mean with a
            low coefficient is a wall of identical medium sentences, which is what a reader means
            by "it sounds like a manual".
        ``tolerance`` / ``cv_tolerance``
            Optional decay reaches for each band.

    The score is the mean of whichever sub-scores could be computed.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict. ``unsupported`` when the response has no sentences, or when neither band is
        declared. A response of one sentence scores its mean and reports the coefficient as
        unmeasured rather than as zero.

    Raises:
        RuleInvalid: A bound is not a number, a band's minimum exceeds its maximum, or a tolerance
            is not positive.
    """
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    mean_low, mean_high = band(parameters, "mean_words")
    cv_low, cv_high = band(parameters, "cv")
    if mean_low is None and mean_high is None and cv_low is None and cv_high is None:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_BAND)
    lengths = [len(words(sentence)) for sentence in sentences(text)]
    lengths = [length for length in lengths if length]
    if not lengths:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_SENTENCES)
    mean, cv = _mean_and_cv(lengths)

    parts: list[float] = []
    if mean_low is not None or mean_high is not None:
        parts.append(proportional(mean, mean_low, mean_high, tolerance=_tolerance(parameters)))
    if (cv_low is not None or cv_high is not None) and cv is not None:
        parts.append(
            proportional(cv, cv_low, cv_high, tolerance=_tolerance(parameters, "cv_tolerance"))
        )
    detail = {
        "sentence_count": len(lengths),
        "mean_words": mean,
        "coefficient_of_variation": cv,
        "mean_words_band": [mean_low, mean_high],
        "cv_band": [cv_low, cv_high],
        "cv_unmeasured": cv is None,
    }
    if not parts:
        return RuleResult(score=None, detail=detail, unsupported_reason=UNSUPPORTED_NO_SENTENCES)
    return RuleResult(score=sum(parts) / len(parts), detail=detail)


def paragraph_shape(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score a response's paragraph count and paragraph length.

    Parameters:
        ``paragraphs``
            A band on how many paragraphs the response has.
        ``words_per_paragraph``
            A band on the mean words per paragraph.
        ``tolerance`` / ``words_tolerance``
            Optional decay reaches for each band.

    The score is the mean of whichever sub-scores could be computed.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict. ``unsupported`` for an empty response or a criterion declaring neither band.

    Raises:
        RuleInvalid: A bound is not a number, a band's minimum exceeds its maximum, or a tolerance
            is not positive.
    """
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    count_low, count_high = band(parameters, "paragraphs")
    length_low, length_high = band(parameters, "words_per_paragraph")
    if all(bound is None for bound in (count_low, count_high, length_low, length_high)):
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_BAND)
    blocks = paragraphs(text)
    lengths = [len(words(block)) for block in blocks]
    mean_length = sum(lengths) / len(lengths) if lengths else 0.0

    parts: list[float] = []
    if count_low is not None or count_high is not None:
        parts.append(
            proportional(len(blocks), count_low, count_high, tolerance=_tolerance(parameters))
        )
    if length_low is not None or length_high is not None:
        parts.append(
            proportional(
                mean_length,
                length_low,
                length_high,
                tolerance=_tolerance(parameters, "words_tolerance"),
            )
        )
    return RuleResult(
        score=sum(parts) / len(parts),
        detail={
            "paragraph_count": len(blocks),
            "mean_words_per_paragraph": mean_length,
            "paragraphs_band": [count_low, count_high],
            "words_per_paragraph_band": [length_low, length_high],
        },
    )
