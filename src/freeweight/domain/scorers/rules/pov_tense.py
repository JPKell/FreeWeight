"""Rung-2 grammatical consistency: ``pov_tense``.

[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s point-of-view
and tense row. "First person, past tense" is one of the most common things a person means by
*voice*, and it is one of the few that a rule can check outright.

**This is a lexical check, and its limits are stated rather than discovered.** Person is decided by
pronouns; tense by auxiliaries, by a short list of irregular past forms, and by the ``-ed`` ending.
It will misread "I read the report" (present or past, identically spelled), and a simple-present
sentence with no auxiliary — "I walk the aisle" — carries no marker it can read at all. That is
the trade the ladder asks for: a deterministic check whose failure mode is *silence* is worth more
than a judged one whose failure mode is a plausible number.

**A sentence with no marker is excluded, not failed.** "Remarkable." carries no pronoun and no
verb. Counting it as a violation would penalize a response for having a short sentence in it, so
the score is the share of *decidable* sentences that conformed, and the count of undecidable ones
travels in the detail where a reader can see how much of the response the rule actually read.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_SENTENCES,
    RuleInvalid,
    RuleResult,
    sentences,
    words,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["PERSONS", "TENSES", "pov_tense"]

PERSONS = ("first", "second", "third")
TENSES = ("past", "present")

UNSUPPORTED_NOTHING_DECIDABLE = "no_decidable_sentences"
"""No sentence carried a pronoun or a verb marker this rule can read."""

_PRONOUNS: dict[str, frozenset[str]] = {
    "first": frozenset({"i", "me", "my", "mine", "myself", "we", "us", "our", "ours", "ourselves"}),
    "second": frozenset({"you", "your", "yours", "yourself", "yourselves"}),
    "third": frozenset(
        {
            "he",
            "him",
            "his",
            "himself",
            "she",
            "her",
            "hers",
            "herself",
            "it",
            "its",
            "itself",
            "they",
            "them",
            "their",
            "theirs",
            "themselves",
            "one",
            "one's",
        }
    ),
}

_PAST_AUXILIARIES = frozenset({"was", "were", "had", "did", "would", "could", "should"})
_PRESENT_AUXILIARIES = frozenset({"am", "is", "are", "have", "has", "do", "does", "will", "can"})

_IRREGULAR_PAST = frozenset(
    {
        "went",
        "came",
        "took",
        "saw",
        "found",
        "wrote",
        "said",
        "made",
        "got",
        "knew",
        "thought",
        "gave",
        "told",
        "left",
        "felt",
        "kept",
        "held",
        "ran",
        "sat",
        "stood",
        "brought",
        "bought",
        "caught",
        "taught",
        "heard",
        "met",
        "paid",
        "sent",
        "spent",
        "built",
        "lost",
        "won",
        "began",
        "drove",
        "chose",
        "spoke",
        "read",
        "put",
        "let",
        "became",
        "broke",
        "drew",
        "fell",
        "flew",
        "forgot",
        "grew",
        "hid",
        "lay",
        "led",
        "rose",
        "sold",
        "shut",
        "slept",
        "sang",
        "swam",
        "threw",
        "understood",
        "wore",
        "woke",
    }
)
"""Common irregular past forms, because ``-ed`` alone would miss half of ordinary narrative.

A closed list, deliberately short, and named in this module's docstring as part of the lexical
approximation. A sentence carrying none of these, no ``-ed`` word and no auxiliary is *undecidable*
rather than present: guessing tense from the absence of evidence is what a rule must not do."""

_ED = re.compile(r"^[^\W\d_]{3,}ed$", re.UNICODE)


def _person_of(tokens: list[str]) -> str | None:
    """Return the grammatical person a sentence's pronouns indicate, or ``None``.

    ``None`` when the sentence carries no pronoun at all, or carries pronouns of more than one
    person — "I told them" is both, and calling it one would make the rule's answer depend on
    which pronoun happened to come first.
    """
    found = {
        person for person, members in _PRONOUNS.items() if any(token in members for token in tokens)
    }
    return next(iter(found)) if len(found) == 1 else None


def _tense_of(tokens: list[str]) -> str | None:
    """Return the tense a sentence's verb markers indicate, or ``None``.

    Auxiliaries first, because they are unambiguous; then the ``-ed`` ending, which is not. A
    sentence carrying markers of both tenses is undecidable, for the same reason as ``_person_of``.
    """
    past = (
        any(token in _PAST_AUXILIARIES for token in tokens)
        or any(token in _IRREGULAR_PAST for token in tokens)
        or any(_ED.match(token) for token in tokens)
    )
    present = any(token in _PRESENT_AUXILIARIES for token in tokens)
    if past and not present:
        return "past"
    if present and not past:
        return "present"
    return None


def pov_tense(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score how consistently a response holds one person and one tense.

    Parameters:
        ``person``
            One of :data:`PERSONS`, or absent to check tense alone.
        ``tense``
            One of :data:`TENSES`, or absent to check person alone.
        ``tolerance``
            The share of decidable sentences allowed to deviate before the score starts falling.
            ``0.15`` by default: a first-person essay is entitled to quote somebody.

    The score is the conforming share, rescaled so that ``1 − tolerance`` and above is ``1.0`` and
    zero conformance is ``0.0``.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict, with the deviating sentences named in ``detail``. ``unsupported`` for an
        empty response, a response with no sentences, or one where no sentence carried a marker
        this rule can read.

    Raises:
        RuleInvalid: Neither ``person`` nor ``tense`` is declared, one of them is not a known
            value, or ``tolerance`` is outside ``0.0..1.0``.
    """
    person = parameters.get("person")
    tense = parameters.get("tense")
    if person is None and tense is None:
        raise RuleInvalid(
            "A pov_tense criterion must declare 'person', 'tense' or both; one that declares "
            "neither is satisfied by every response."
        )
    if person is not None and str(person) not in PERSONS:
        raise RuleInvalid(
            f"Rule parameter 'person' must be one of {list(PERSONS)}; got {person!r}."
        )
    if tense is not None and str(tense) not in TENSES:
        raise RuleInvalid(f"Rule parameter 'tense' must be one of {list(TENSES)}; got {tense!r}.")
    tolerance = parameters.get("tolerance", 0.15)
    if isinstance(tolerance, bool) or not isinstance(tolerance, int | float):
        raise RuleInvalid(f"Rule parameter 'tolerance' must be a number; got {tolerance!r}.")
    if not 0.0 <= float(tolerance) <= 1.0:
        raise RuleInvalid(f"Rule parameter 'tolerance' must be within 0.0..1.0; got {tolerance!r}.")

    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    lines = sentences(text)
    if not lines:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_SENTENCES)

    decidable = 0
    conforming = 0
    deviating: list[str] = []
    for sentence in lines:
        tokens = [token.casefold() for token in words(sentence)]
        observed_person = _person_of(tokens) if person is not None else None
        observed_tense = _tense_of(tokens) if tense is not None else None
        if person is not None and observed_person is None:
            continue
        if tense is not None and observed_tense is None:
            continue
        decidable += 1
        matches = (person is None or observed_person == str(person)) and (
            tense is None or observed_tense == str(tense)
        )
        if matches:
            conforming += 1
        else:
            deviating.append(sentence[:120])
    if not decidable:
        return RuleResult(
            score=None,
            detail={"sentence_count": len(lines), "decidable_sentences": 0},
            unsupported_reason=UNSUPPORTED_NOTHING_DECIDABLE,
        )
    share = conforming / decidable
    threshold = 1.0 - float(tolerance)
    score = 1.0 if share >= threshold else (share / threshold if threshold > 0 else 0.0)
    return RuleResult(
        score=min(1.0, score),
        detail={
            "person": person,
            "tense": tense,
            "sentence_count": len(lines),
            "decidable_sentences": decidable,
            "conforming_sentences": conforming,
            "conforming_share": share,
            "tolerance": float(tolerance),
            "deviating_sentences": deviating[:5],
        },
    )
