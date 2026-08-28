"""Rung-3 reference criteria: coverage and faithfulness against user-supplied ground truth.

[Subjective Goals §3.2](../../../../../docs/apps/freeweight/subjective-goals.md)'s four rows.
These sit one rung above the rules because they need something the response alone cannot supply —
an annotated source, a list of claims, a set of reference outputs — and one rung below a judge
because, given that ground truth, they are decided by counting.

**This is where "did it make something up" stops needing a judge.** The summarization-faithfulness
starter pack is built almost entirely from these: an entity in the output that is not in the source
is a fabrication, and finding it is a set operation. The catalog's claim that faithfulness is
"answerable without judgement far more often than it first appears" is this module.

**A criterion with no ground truth is unsupported, never zero.** A task that supplied no annotated
source has not been measured for faithfulness; scoring it ``0.0`` would report a fabrication that
was never looked for, and scoring it ``1.0`` would certify one that was never checked.
"""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_GROUND_TRUTH,
    RuleInvalid,
    RuleResult,
    band,
    coverage,
    proportional,
    string_list,
    words,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "SIMILARITY_METRICS",
    "claim_coverage",
    "entity_recall",
    "no_unsupported_claims",
    "reference_similarity",
]

_DEFAULT_CLAIM_OVERLAP = 0.6
"""Share of a claim's content words that must appear for the claim to count as covered.

Used only when a claim declares no ``any_of`` phrases. A claim that means something specific
should say which phrases carry it; this is the fallback, and it is deliberately generous, because
the failure it guards against — marking a genuinely covered claim as missing — is the one that
makes a faithfulness score untrustworthy."""

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_ENTITY = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "with",
    }
)

SIMILARITY_METRICS = ("token_jaccard", "sequence_ratio")
"""How ``reference_similarity`` compares two texts.

``token_jaccard`` is bag-of-words overlap, which ignores order; ``sequence_ratio`` is
:class:`difflib.SequenceMatcher`, which does not. Both are recorded metrics with no model in them,
which is the whole point of rung 3."""


def _ground_truth(source: Mapping[str, Any] | None, key: str) -> Any:  # noqa: ANN401 — a JSON value
    """Return one field of a task's annotated source, or ``None``."""
    return None if source is None else source.get(key)


def entity_recall(
    text: str, parameters: Mapping[str, Any], *, source: Mapping[str, Any] | None = None
) -> RuleResult:
    """Score how many of the source's named entities appear in the output.

    Parameters:
        ``entities``
            The entities to look for, overriding the task's own list. Optional.
        ``case_sensitive``
            Whether matching respects case. ``False`` by default.

    Ground truth: the task's ``source.entities``.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.
        source: The task's annotated source.

    Returns:
        The verdict, naming the entities found and missing. ``unsupported`` when neither the
        criterion nor the task supplies an entity list, and for an empty response.

    Raises:
        RuleInvalid: ``entities`` is present and is not a list of strings.
    """
    declared = string_list(parameters, "entities", required=False)
    entities = declared or tuple(
        str(item) for item in (_ground_truth(source, "entities") or ()) if str(item)
    )
    if not entities:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_GROUND_TRUTH)
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    case_sensitive = bool(parameters.get("case_sensitive", False))
    subject = text if case_sensitive else text.casefold()
    found = {
        entity: (entity if case_sensitive else entity.casefold()) in subject for entity in entities
    }
    return RuleResult(
        score=coverage(list(found.values())),
        detail={
            "entities_checked": len(entities),
            "found": sorted(name for name, present in found.items() if present),
            "missing": sorted(name for name, present in found.items() if not present),
            "case_sensitive": case_sensitive,
        },
    )


def _content_words(text: str) -> set[str]:
    """Return a text's content words, casefolded, stopwords dropped."""
    return {token.casefold() for token in words(text) if token.casefold() not in _STOPWORDS}


def claim_coverage(
    text: str, parameters: Mapping[str, Any], *, source: Mapping[str, Any] | None = None
) -> RuleResult:
    """Score how many of the task's listed claims the output covers.

    Parameters:
        ``min_overlap``
            The share of a claim's content words that must appear when the claim declares no
            ``any_of`` phrases. :data:`_DEFAULT_CLAIM_OVERLAP` by default.

    Ground truth: the task's ``source.claims``, each an object with ``id``, ``text`` and
    optionally ``any_of`` — the phrases that would carry the claim.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.
        source: The task's annotated source.

    Returns:
        The verdict, naming which claims were covered. ``unsupported`` when the task lists no
        claims, and for an empty response.

    Raises:
        RuleInvalid: ``min_overlap`` is outside ``0.0..1.0``, or a claim is not an object with
            text.
    """
    minimum = parameters.get("min_overlap", _DEFAULT_CLAIM_OVERLAP)
    if isinstance(minimum, bool) or not isinstance(minimum, int | float):
        raise RuleInvalid(f"Rule parameter 'min_overlap' must be a number; got {minimum!r}.")
    if not 0.0 <= float(minimum) <= 1.0:
        raise RuleInvalid(f"Rule parameter 'min_overlap' must be within 0.0..1.0; got {minimum!r}.")
    claims = _ground_truth(source, "claims") or ()
    if not claims:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_GROUND_TRUTH)
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)

    subject = text.casefold()
    present = _content_words(text)
    covered: list[str] = []
    missing: list[str] = []
    for index, entry in enumerate(claims):
        if not isinstance(entry, dict) or not str(entry.get("text", "")):
            raise RuleInvalid(
                f"Claim {index} has no 'text'; a claim nobody can read cannot be checked for."
            )
        identifier = str(entry.get("id", f"claim_{index + 1}"))
        phrases = [str(item) for item in entry.get("any_of", ()) if str(item)]
        if phrases:
            hit = any(phrase.casefold() in subject for phrase in phrases)
        else:
            wanted = _content_words(str(entry["text"]))
            hit = bool(wanted) and len(wanted & present) / len(wanted) >= float(minimum)
        (covered if hit else missing).append(identifier)
    return RuleResult(
        score=coverage([True] * len(covered) + [False] * len(missing)),
        detail={
            "claims_checked": len(claims),
            "covered": covered,
            "missing": missing,
            "min_overlap": float(minimum),
        },
    )


def no_unsupported_claims(
    text: str, parameters: Mapping[str, Any], *, source: Mapping[str, Any] | None = None
) -> RuleResult:
    """Score what share of the output's specifics trace back to the source.

    Parameters:
        ``check``
            Which classes to trace: ``"numbers"``, ``"entities"`` or both. Both by default.
        ``allow``
            Strings that count as supported wherever they appear — the task's own framing words,
            a date the user is content to see restated.

    Ground truth: the task's ``source.text``.

    Numbers are matched after stripping thousands separators, so ``1,200`` in the output traces to
    ``1200`` in the source. Entities are capitalised runs; the first word of a sentence is *not*
    excluded, which makes this rule mildly conservative — it will occasionally count "The" as an
    entity to trace, and the source almost always contains it.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.
        source: The task's annotated source.

    Returns:
        The verdict, naming exactly which specifics did not trace. ``unsupported`` when the task
        supplies no source text, for an empty response, and — importantly — when the response
        contains no specifics at all: a response that asserted nothing has fabricated nothing, and
        it has also not been measured for faithfulness.

    Raises:
        RuleInvalid: ``check`` names a class this rule does not trace, or ``allow`` is not a list
            of strings.
    """
    wanted = parameters.get("check", ["numbers", "entities"])
    if isinstance(wanted, str) or not isinstance(wanted, list | tuple):
        raise RuleInvalid(f"Rule parameter 'check' must be a list; got {wanted!r}.")
    classes = {str(item) for item in wanted}
    unknown = sorted(classes - {"numbers", "entities"})
    if unknown:
        raise RuleInvalid(
            f"Rule parameter 'check' names {unknown}, which this rule does not trace; the classes "
            "are ['numbers', 'entities']."
        )
    allowed = {item.casefold() for item in string_list(parameters, "allow", required=False)}
    document = _ground_truth(source, "text")
    if not isinstance(document, str) or not document.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_GROUND_TRUTH)
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)

    haystack = document.casefold()
    haystack_numbers = {value.replace(",", "") for value in _NUMBER.findall(document)}
    specifics: list[tuple[str, str]] = []
    if "numbers" in classes:
        specifics.extend(("number", value) for value in _NUMBER.findall(text))
    if "entities" in classes:
        specifics.extend(("entity", value) for value in _ENTITY.findall(text))
    if not specifics:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_GROUND_TRUTH)

    supported: list[str] = []
    unsupported: list[str] = []
    for kind, value in specifics:
        if value.casefold() in allowed:
            supported.append(value)
            continue
        traced = (
            value.replace(",", "") in haystack_numbers
            if kind == "number"
            else value.casefold() in haystack
        )
        (supported if traced else unsupported).append(value)
    return RuleResult(
        score=len(supported) / len(specifics),
        detail={
            "specifics_checked": len(specifics),
            "classes": sorted(classes),
            "unsupported": sorted(set(unsupported))[:20],
            "unsupported_count": len(unsupported),
        },
    )


def _similarity(candidate: str, reference: str, metric: str) -> float:
    """Return one similarity figure between two texts."""
    if metric == "sequence_ratio":
        return difflib.SequenceMatcher(None, candidate, reference).ratio()
    left, right = _content_words(candidate), _content_words(reference)
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def reference_similarity(
    text: str, parameters: Mapping[str, Any], *, source: Mapping[str, Any] | None = None
) -> RuleResult:
    """Score a response's similarity to the closest of the task's reference outputs.

    Parameters:
        ``metric``
            One of :data:`SIMILARITY_METRICS`. ``"token_jaccard"`` by default, because word
            overlap is what a person means by "covers the same ground", while character-level
            similarity mostly measures sentence length.
        ``min`` / ``max``
            An optional band. Without one the similarity *is* the score, which is what the
            catalog means by a recorded metric; with one, the score is the proportional distance
            into the band — the form to use when a response that is *too* close to the reference
            is as wrong as one that is too far.

    Ground truth: the task's ``source.references``.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.
        source: The task's annotated source.

    Returns:
        The verdict, with every reference's similarity in ``detail``. ``unsupported`` when the
        task supplies no references, and for an empty response.

    Raises:
        RuleInvalid: ``metric`` is not one of :data:`SIMILARITY_METRICS`, a bound is not a number,
            or the minimum exceeds the maximum.
    """
    metric = str(parameters.get("metric", "token_jaccard"))
    if metric not in SIMILARITY_METRICS:
        raise RuleInvalid(
            f"Rule parameter 'metric' must be one of {list(SIMILARITY_METRICS)}; got {metric!r}."
        )
    references: Sequence[str] = [
        str(item) for item in (_ground_truth(source, "references") or ()) if str(item)
    ]
    if not references:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_GROUND_TRUTH)
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    low, high = band(parameters, "")
    similarities = [_similarity(text, reference, metric) for reference in references]
    best = max(similarities)
    score = best if low is None and high is None else proportional(best, low, high)
    return RuleResult(
        score=score,
        detail={
            "metric": metric,
            "best_similarity": best,
            "similarities": similarities,
            "references_checked": len(references),
            "band": [low, high],
        },
    )
