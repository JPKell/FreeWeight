"""Rung-2 pattern matching: ``regex_match``.

[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s regex row, and
the one rule in the library whose input is *code the user wrote*. Everything else here takes
numbers and word lists; this takes a program, and it runs that program against model output.

**Three guards, in this order.** The pattern is length-capped and dialect-linted at pack-load time
(:func:`~freeweight.domain.scorers.rules.lint_pattern`: no backreferences, no unbounded quantifier
nested inside another), and whatever survives both runs under the per-criterion timeout in
:mod:`freeweight.domain.goals.criteria`. The lint is the cheap guard and the timeout is the
backstop; neither alone is enough, which is why spec §14 names both.

**A pattern that matches nothing is a score of zero, not an absence.** This is the one place the
distinction inverts: elsewhere "the rule could not measure" means unsupported, but a pattern that
ran to completion and did not match *has* measured something. The absence here is a criterion with
no pattern at all.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    RuleInvalid,
    RuleResult,
    lint_pattern,
    proportional,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["FLAGS", "MODES", "regex_match"]

UNSUPPORTED_NO_PATTERN = "no_pattern_declared"
"""The criterion supplied no pattern."""

MODES = ("search", "fullmatch", "count")
"""How a pattern is applied.

``search`` and ``fullmatch`` are boolean; ``count`` scores the number of matches against a band,
which is how "at least three concrete figures" becomes a rule rather than a judgement."""

FLAGS: dict[str, re.RegexFlag] = {
    "ignorecase": re.IGNORECASE,
    "multiline": re.MULTILINE,
    "dotall": re.DOTALL,
}
"""The flags a criterion may set.

Three, and deliberately not ``re.VERBOSE`` or ``re.LOCALE``: the first changes how the pattern
itself is read, which would make the linted dialect mean something different, and the second makes
a result depend on the machine's locale."""


def regex_match(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score a response against a user-supplied pattern.

    Parameters:
        ``pattern``
            The regular expression. Linted (see the module docstring) before it is compiled.
        ``mode``
            One of :data:`MODES`. ``"search"`` by default.
        ``flags``
            Any of :data:`FLAGS`.
        ``min`` / ``max``
            For ``count`` mode, the band on the number of matches.
        ``negate``
            When ``True``, inverts a boolean result: the criterion is met when the pattern does
            *not* match. Declared rather than expressed with a negative lookahead, because a
            lookahead over model output is exactly the construction the dialect lint exists to
            keep out.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict, with the match count and a bounded sample of the matches in ``detail``.
        ``unsupported`` when no pattern is declared or the response is empty.

    Raises:
        RuleInvalid: The pattern is absent, too long, uses a refused construct, does not compile;
            ``mode`` is unknown; a flag is unknown; or ``count`` mode declares no band.
    """
    raw = parameters.get("pattern")
    if raw is None:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_PATTERN)
    mode = str(parameters.get("mode", "search"))
    if mode not in MODES:
        raise RuleInvalid(f"Rule parameter 'mode' must be one of {list(MODES)}; got {mode!r}.")
    declared_flags = parameters.get("flags", ())
    if isinstance(declared_flags, str) or not isinstance(declared_flags, list | tuple):
        raise RuleInvalid(f"Rule parameter 'flags' must be a list; got {declared_flags!r}.")
    flags = re.NOFLAG
    for name in declared_flags:
        if str(name) not in FLAGS:
            raise RuleInvalid(
                f"Rule flag {name!r} is not one of {sorted(FLAGS)}; a flag this build does not "
                "set would make the pattern mean something other than what was written."
            )
        flags |= FLAGS[str(name)]
    compiled = re.compile(lint_pattern(str(raw)).pattern, flags)

    from freeweight.domain.scorers.rules import band

    low, high = band(parameters, "")
    if mode == "count" and low is None and high is None:
        raise RuleInvalid(
            "A regex_match criterion in 'count' mode must declare 'min', 'max' or both; without "
            "a band every match count scores the same."
        )
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)

    matches = compiled.findall(text)
    negate = bool(parameters.get("negate", False))
    if mode == "count":
        score = proportional(len(matches), low, high)
    else:
        matched = (
            compiled.fullmatch(text.strip()) is not None
            if mode == "fullmatch"
            else compiled.search(text) is not None
        )
        score = 1.0 if matched is not negate else 0.0
    return RuleResult(
        score=score,
        detail={
            "pattern": str(raw),
            "mode": mode,
            "flags": sorted(str(name) for name in declared_flags),
            "negate": negate,
            "match_count": len(matches),
            "matches": [str(match)[:60] for match in matches[:5]],
            "band": [low, high],
        },
    )
