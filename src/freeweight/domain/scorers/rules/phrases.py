"""Rung-2 phrase rules: ``forbidden_phrases`` and ``required_phrases``.

[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s first two rows,
and the two that carry most of the weight a well-written rubric moves off its judge. "Not
LinkedIn" is partly a phrase list, and a phrase list is free, exact, and never disagrees with the
person who wrote it.

**Both count occurrences, not lines.** A response that says ``delve`` four times has used it four
times, and a rule that scored presence alone could not tell a slip from a habit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    RuleInvalid,
    RuleResult,
    coverage,
    string_list,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["forbidden_phrases", "required_phrases"]


def _occurrences(haystack: str, needle: str) -> int:
    """Count non-overlapping occurrences of ``needle`` in ``haystack``."""
    return haystack.count(needle) if needle else 0


def forbidden_phrases(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score a response against a blacklist of phrases.

    Parameters:
        ``phrases``
            The blacklist. Required and non-empty.
        ``case_sensitive``
            Whether matching respects case. ``False`` by default, because "avoid *delve*" is
            almost never a claim about capitalisation.
        ``max_hits``
            How many occurrences take the score to zero. Defaults to the number of phrases
            declared, which makes one slip per banned term the point at which the criterion is
            entirely failed.

    Scores ``1.0`` for a clean response, and ``1 − hits / max_hits`` otherwise, floored at zero.
    A criterion carrying ``gate: true`` turns any hit into a hard gate; that decision belongs to
    the composite and not here, so this function reports the hits either way.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict. ``unsupported`` for an empty response — a rule cannot observe the absence of
        a phrase in text that does not exist, and scoring it ``1.0`` would reward a model for
        answering nothing.

    Raises:
        RuleInvalid: ``phrases`` is absent, empty or not a list of strings, or ``max_hits`` is not
            a positive whole number.
    """
    declared = string_list(parameters, "phrases")
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    case_sensitive = bool(parameters.get("case_sensitive", False))
    subject = text if case_sensitive else text.casefold()
    maximum = parameters.get("max_hits", len(declared))
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise RuleInvalid(
            f"Rule parameter 'max_hits' must be a positive whole number; got {maximum!r}."
        )
    hits: dict[str, int] = {}
    for phrase in declared:
        needle = phrase if case_sensitive else phrase.casefold()
        count = _occurrences(subject, needle)
        if count:
            hits[phrase] = count
    total = sum(hits.values())
    return RuleResult(
        score=max(0.0, 1.0 - total / maximum),
        detail={
            "matched_phrases": dict(sorted(hits.items())),
            "hits": total,
            "max_hits": maximum,
            "phrases_checked": len(declared),
            "case_sensitive": case_sensitive,
        },
    )


def required_phrases(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score a response on how many required terms it contains.

    Parameters:
        ``phrases``
            The terms that must appear. Required and non-empty.
        ``min_occurrences``
            How many times each must appear to count as present. ``1`` by default.
        ``case_sensitive``
            Whether matching respects case. ``False`` by default.

    Scores the fraction of required phrases present.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict. ``unsupported`` for an empty response.

    Raises:
        RuleInvalid: ``phrases`` is absent, empty or not a list of strings, or
            ``min_occurrences`` is not a positive whole number.
    """
    declared = string_list(parameters, "phrases")
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    minimum = parameters.get("min_occurrences", 1)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise RuleInvalid(
            f"Rule parameter 'min_occurrences' must be a positive whole number; got {minimum!r}."
        )
    case_sensitive = bool(parameters.get("case_sensitive", False))
    subject = text if case_sensitive else text.casefold()
    counts = {
        phrase: _occurrences(subject, phrase if case_sensitive else phrase.casefold())
        for phrase in declared
    }
    present = [count >= minimum for count in counts.values()]
    return RuleResult(
        score=coverage(present),
        detail={
            "occurrences": dict(sorted(counts.items())),
            "present": sorted(phrase for phrase, count in counts.items() if count >= minimum),
            "missing": sorted(phrase for phrase, count in counts.items() if count < minimum),
            "min_occurrences": minimum,
            "case_sensitive": case_sensitive,
        },
    )
