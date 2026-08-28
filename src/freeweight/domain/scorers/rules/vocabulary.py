"""Rung-2 vocabulary: ``vocabulary_profile``.

[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s vocabulary row:
type-token ratio, rare-word rate, and banned register lists. This is the other half of "not
LinkedIn" — the half that is about *register* rather than about specific phrases, and the half a
:mod:`~freeweight.domain.scorers.rules.phrases` list cannot reach.

**Rarity is a declared proxy, not a frequency table.** This package ships no word-frequency list,
because a criterion measured against one would be measuring that list — its vintage, its corpus,
its idea of English. A word counts as rare when it is at least
:data:`DEFAULT_RARE_WORD_CHARACTERS` characters long, unless the criterion supplies its own
``common_words``, in which case anything outside that list is rare. Both are the user's choice and
both are recorded on every result.

**Type-token ratio depends on length, and this module says so.** A 50-word answer will always have
a higher ratio than a 500-word one; a criterion comparing two responses of very different lengths
on this number is comparing lengths. The word count travels in the detail for exactly that reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    RuleInvalid,
    RuleResult,
    band,
    proportional,
    string_list,
    words,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["DEFAULT_RARE_WORD_CHARACTERS", "vocabulary_profile"]

DEFAULT_RARE_WORD_CHARACTERS = 12
"""How long a word must be to count as rare, absent a supplied ``common_words`` list.

Twelve, which in English prose catches ``notwithstanding``, ``infrastructure`` and
``operationalize`` while leaving ``understand`` and ``information`` alone. It is a proxy and it is
declared as one; a criterion that needs a real frequency threshold supplies its own list."""


def vocabulary_profile(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score a response's vocabulary against ratio bands and a banned register list.

    Parameters:
        ``type_token_ratio``
            A band on distinct words over total words.
        ``rare_word_rate``
            A band on the share of words counting as rare.
        ``banned_register``
            Words whose presence is penalized, matched whole-word and case-insensitively. The
            score contribution is the share of banned words *absent*.
        ``common_words``
            The user's own idea of common. Anything outside it is rare.
        ``rare_word_characters``
            The length threshold, when no ``common_words`` list is supplied.

    The score is the mean of whichever sub-scores the criterion asked for.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict. ``unsupported`` for an empty response, and for a criterion that declares
        neither a band nor a banned list — which would be satisfied by everything.

    Raises:
        RuleInvalid: A bound is not a number, a band's minimum exceeds its maximum,
            ``banned_register`` or ``common_words`` is not a list of strings, or
            ``rare_word_characters`` is not a positive whole number.
    """
    ttr_low, ttr_high = band(parameters, "type_token_ratio")
    rare_low, rare_high = band(parameters, "rare_word_rate")
    banned = string_list(parameters, "banned_register", required=False)
    common = {item.casefold() for item in string_list(parameters, "common_words", required=False)}
    threshold = parameters.get("rare_word_characters", DEFAULT_RARE_WORD_CHARACTERS)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise RuleInvalid(
            f"Rule parameter 'rare_word_characters' must be a positive whole number; got "
            f"{threshold!r}."
        )
    wants_ttr = ttr_low is not None or ttr_high is not None
    wants_rare = rare_low is not None or rare_high is not None
    if not (wants_ttr or wants_rare or banned):
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_BAND)
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)

    tokens = [token.casefold() for token in words(text)]
    if not tokens:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    distinct = len(set(tokens))
    ratio = distinct / len(tokens)
    if common:
        rare_tokens = [token for token in tokens if token not in common]
    else:
        rare_tokens = [token for token in tokens if len(token) >= threshold]
    rare_rate = len(rare_tokens) / len(tokens)
    present = sorted({word for word in banned if word.casefold() in set(tokens)})

    parts: list[float] = []
    if wants_ttr:
        parts.append(proportional(ratio, ttr_low, ttr_high))
    if wants_rare:
        parts.append(proportional(rare_rate, rare_low, rare_high))
    if banned:
        parts.append(1.0 - len(present) / len(banned))
    return RuleResult(
        score=sum(parts) / len(parts),
        detail={
            "word_count": len(tokens),
            "distinct_words": distinct,
            "type_token_ratio": ratio,
            "type_token_ratio_band": [ttr_low, ttr_high],
            "rare_word_rate": rare_rate,
            "rare_word_rate_band": [rare_low, rare_high],
            "rarity_basis": "common_words" if common else f"length>={threshold}",
            "banned_register_present": present,
            "banned_register_checked": len(banned),
            "rare_examples": sorted(set(rare_tokens))[:10],
        },
    )
