"""freeweight.domain.scorers.rules — the deterministic half of a user's rubric.

Seventeen small functions: the thirteen rung-2 rule types of
[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md) and the four
rung-3 reference types of §3.2. Each is a pure function of ``(text, parameters)`` — plus, for the
reference types, the task's own ground truth — and each returns a :class:`RuleResult`.

**These are measurements, and a wrong one is invisible.** The phase's own risk note says it plainly:
"the rule library is thirteen small formulas and it is tempting to write them quickly; each is a
measurement and a wrong one is invisible". So every function here states in its docstring what it
*refuses*, and every one of them refuses rather than guessing:

* **A rule that cannot parse its input returns ``unsupported``, never ``0.0``.** An empty response,
  a criterion whose parameters name no band, a schema this build's validator does not implement —
  each is an absence of measurement, and scoring it zero would report a model failure that was
  never observed ([ADR-0016](../../../../../docs/adr/0016-unavailable-is-not-zero.md)).
* **A rule that cannot be interpreted raises :class:`RuleInvalid`**, at pack-load time rather than
  mid-run. A malformed criterion is a defect in the goal, not in the model.

**Proportional scoring is one function, not thirteen.** :func:`proportional` decides what "just
outside the band" is worth, once, so two criteria that both declare a range cannot disagree about
how quickly a miss decays. A rule that wants a different curve says so in its own docstring.

Pure domain: stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "MAXIMUM_PATTERN_LENGTH",
    "RuleInvalid",
    "RuleResult",
    "UNSUPPORTED_EMPTY_TEXT",
    "UNSUPPORTED_NO_BAND",
    "UNSUPPORTED_NO_GROUND_TRUTH",
    "UNSUPPORTED_NO_SENTENCES",
    "band",
    "lint_pattern",
    "paragraphs",
    "proportional",
    "sentences",
    "words",
]

UNSUPPORTED_EMPTY_TEXT = "empty_response"
"""There was no text to measure. Not a score of zero — nothing was observed."""

UNSUPPORTED_NO_BAND = "no_band_declared"
"""The criterion declares a range with neither bound, so every response satisfies it."""

UNSUPPORTED_NO_SENTENCES = "no_sentences"
"""The response has no sentence this rule could measure a distribution over."""

UNSUPPORTED_NO_GROUND_TRUTH = "no_ground_truth"
"""A rung-3 criterion was given no annotated source, claim list or reference to compare against."""

MAXIMUM_PATTERN_LENGTH = 200
"""Longest regex a criterion may declare.

The cheap half of the guard on user-supplied patterns (spec §14). The dialect lint below is the
other half, and the per-rule timeout in :mod:`freeweight.domain.goals.criteria` is the backstop
for anything both of them let through."""

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[\s\"')\]]+")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)


class RuleInvalid(ValueError):
    """A criterion this build refuses to interpret.

    Raised where the goal pack is read, never during scoring: a rule that guessed at a malformed
    parameter block would report a model failure caused by a typo in the user's rubric.
    """


@dataclass(frozen=True, slots=True)
class RuleResult:
    """One rule's verdict on one response.

    Attributes:
        score: The score in ``0.0..1.0``, or ``None`` when this response could not be measured.
        detail: What the rule measured — the phrases that matched, the distribution it found, the
            band it compared against. This is what a headline goal number drills to, and a rule
            whose detail is empty has made its number unauditable.
        unsupported_reason: A stable reason when ``score`` is ``None``; ``None`` otherwise.
    """

    score: float | None
    detail: Mapping[str, Any] = field(default_factory=dict)
    unsupported_reason: str | None = None

    def __post_init__(self) -> None:
        """Refuse a result that is internally dishonest.

        Raises:
            ValueError: ``score`` is outside ``0.0..1.0``, a scored result carries a reason, or
                an unscored one carries none. The last is the row ADR-0016 exists to prevent: a
                ``NULL`` with nothing said about why.
        """
        if self.score is not None:
            if not 0.0 <= self.score <= 1.0:
                raise ValueError(f"A rule score must be within 0.0..1.0; got {self.score!r}.")
            if self.unsupported_reason is not None:
                raise ValueError(
                    f"A scored rule result must not carry an unsupported reason; got "
                    f"{self.unsupported_reason!r} alongside score={self.score!r}."
                )
        elif self.unsupported_reason is None:
            raise ValueError(
                "An unmeasured rule result (score=None) must carry a reason; a NULL with no "
                "reason is exactly what ADR-0016 exists to prevent."
            )


def words(text: str) -> list[str]:
    """Return the response's words.

    Letters and apostrophes only: numbers and punctuation are not words, and counting them would
    make a word-count band depend on how many figures a paragraph happens to quote.
    """
    return _WORD.findall(text)


def sentences(text: str) -> list[str]:
    """Return the response's sentences, stripped, blank ones dropped.

    Split on terminal punctuation followed by whitespace. A declared approximation: abbreviations
    and decimals will occasionally split a sentence in two. That is stated rather than hidden
    because the alternative is a sentence tokenizer, and a rubric measured by a tokenizer is
    measuring the tokenizer.
    """
    return [part.strip() for part in _SENTENCE_SPLIT.split(text.strip()) if part.strip()]


def paragraphs(text: str) -> list[str]:
    """Return the response's paragraphs: runs of text separated by a blank line."""
    return [block.strip() for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]


def band(parameters: Mapping[str, Any], name: str) -> tuple[float | None, float | None]:
    """Read one ``{"min": …, "max": …}`` block from a criterion's parameters.

    Args:
        parameters: The criterion's ``rule`` block.
        name: The key holding the band, or ``""`` to read ``min``/``max`` from ``parameters``
            itself.

    Returns:
        ``(minimum, maximum)``, either of which may be ``None``.

    Raises:
        RuleInvalid: A bound is present and is not a number, or the minimum exceeds the maximum.
            A band that can never be satisfied is a defect in the rubric, and every response
            failing it would be reported as a model failure.
    """
    source = parameters if not name else parameters.get(name, {})
    if not isinstance(source, dict):
        raise RuleInvalid(f"Rule parameter {name!r} must be an object with 'min' and/or 'max'.")
    bounds: list[float | None] = []
    for key in ("min", "max"):
        value = source.get(key)
        if value is None:
            bounds.append(None)
        elif isinstance(value, bool) or not isinstance(value, int | float):
            raise RuleInvalid(
                f"Rule bound {name + '.' if name else ''}{key} must be a number; got {value!r}."
            )
        else:
            bounds.append(float(value))
    low, high = bounds[0], bounds[1]
    if low is not None and high is not None and low > high:
        raise RuleInvalid(
            f"Rule band {name or 'range'} has min {low} above max {high}; no response can "
            "satisfy it, so every one of them would be reported as a model failure."
        )
    return low, high


def proportional(
    value: float, low: float | None, high: float | None, *, tolerance: float | None = None
) -> float:
    """Score one measurement against one band, decaying linearly outside it.

    The single decay curve the whole rule library shares. Inside the band the score is ``1.0``;
    outside, it falls linearly to ``0.0`` at ``tolerance`` beyond the bound that was missed.

    ``tolerance`` defaults to half the band's width, and to half the bound itself for a one-sided
    band. That default is a choice, and this is the one place it is made: a criterion asking for
    12–22 words a sentence scores ``0.0`` at 27 words, which is roughly where a reader stops
    calling it "a bit long" and starts calling it "not what I asked for".

    Args:
        value: What was measured.
        low: The inclusive lower bound, or ``None``.
        high: The inclusive upper bound, or ``None``.
        tolerance: How far beyond a bound reaches zero. Must be positive when given.

    Returns:
        A score in ``0.0..1.0``. ``1.0`` when neither bound exists — a band with no bounds is
        satisfied by everything, and the caller decides whether that is a defect.

    Raises:
        RuleInvalid: ``tolerance`` is given and is not positive. A zero tolerance would make every
            miss score zero regardless of size, which is a step function wearing a gradient's name.
    """
    if tolerance is not None and tolerance <= 0:
        raise RuleInvalid(f"A rule tolerance must be positive; got {tolerance!r}.")
    if low is None and high is None:
        return 1.0
    if (low is None or value >= low) and (high is None or value <= high):
        return 1.0
    if low is not None and high is not None:
        default = max((high - low) / 2.0, 1e-9)
    else:
        bound = low if low is not None else high
        default = max(abs(float(bound or 0.0)) / 2.0, 1e-9)
    reach = tolerance if tolerance is not None else default
    distance = (low - value) if (low is not None and value < low) else (value - (high or 0.0))
    return max(0.0, 1.0 - distance / reach)


_GROUP_REPEAT = re.compile(r"(?<!\\)\)\s*(?:[+*]|\{\s*\d*\s*,\s*\})")
"""An unbounded quantifier applied to a group: ``(...)+``, ``(?:...)*``, ``(...){2,}``.

This is where catastrophic backtracking actually lives. ``(?:a|a?)+`` and ``(a+)+b`` are both
exponential and neither contains anything else a lint could object to; refusing the *construction*
is what makes them unreachable, and it is the only guard that works, because CPython's regex engine
holds the GIL for the whole match and no in-process timeout can interrupt one
(:func:`freeweight.domain.goals.criteria.score_criterion` says so too).

Bounded repetition of a group — ``(?:ab){1,5}`` — is untouched: its work is linear in the bound."""


def lint_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a user-supplied regex, or refuse the dialect.

    Spec §14's linted dialect: **no backreferences, no unbounded repetition of a group**, and a
    length cap. Together they make catastrophic backtracking *unreachable* rather than merely
    survivable, which matters more than it looks: a pattern that backtracks exponentially cannot be
    interrupted once it starts, so the only effective moment to refuse it is before it runs.

    The rejected constructions are the ones that make one input match a group in exponentially
    many ways. Bounded repetition of a group is allowed, and so is every quantifier on a
    character class, because their work is linear in the input.

    Args:
        pattern: The user's pattern.

    Returns:
        The compiled pattern.

    Raises:
        RuleInvalid: The pattern is too long, does not compile, uses a backreference, or applies
            an unbounded quantifier to a group. The message names the bounded form that would be
            accepted instead, because the user is trying to express something and refusing without
            an alternative is not help.
    """
    if len(pattern) > MAXIMUM_PATTERN_LENGTH:
        raise RuleInvalid(
            f"A criterion pattern is {len(pattern)} characters; the limit is "
            f"{MAXIMUM_PATTERN_LENGTH}."
        )
    if re.search(r"\\[1-9]|\(\?P=", pattern):
        raise RuleInvalid(
            f"Criterion pattern {pattern!r} uses a backreference, which this dialect refuses: "
            "backreferences are what make catastrophic backtracking reachable."
        )
    if _GROUP_REPEAT.search(pattern):
        raise RuleInvalid(
            f"Criterion pattern {pattern!r} repeats a group without an upper bound. That is where "
            "catastrophic backtracking lives, and a running match cannot be interrupted — so it "
            "is refused here rather than timed out later. Bound it explicitly, as in "
            "'(?:ab){1,5}', or quantify a character class instead."
        )
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RuleInvalid(f"Criterion pattern {pattern!r} does not compile: {exc}") from exc


def string_list(
    parameters: Mapping[str, Any], name: str, *, required: bool = True
) -> tuple[str, ...]:
    """Read a list-of-strings parameter.

    Args:
        parameters: The criterion's ``rule`` block.
        name: The key holding the list.
        required: Whether an empty or absent list is a defect.

    Returns:
        The strings, in declaration order.

    Raises:
        RuleInvalid: The value is not a list of strings, or it is empty and ``required``.
    """
    value = parameters.get(name, ())
    if isinstance(value, str) or not isinstance(value, list | tuple):
        raise RuleInvalid(f"Rule parameter {name!r} must be a list of strings; got {value!r}.")
    items = tuple(str(item) for item in value if str(item))
    if required and not items:
        raise RuleInvalid(
            f"Rule parameter {name!r} is empty; a list nothing can violate measures nothing."
        )
    return items


def coverage(found: Sequence[bool]) -> float:
    """Return the share of ``found`` that is ``True``, or ``1.0`` for an empty sequence.

    ``1.0`` rather than ``0.0`` for nothing to find: a criterion that asked for no terms has had
    every term it asked for supplied. The caller decides whether asking for none is a defect, and
    :func:`string_list` normally refuses it first.
    """
    return sum(1.0 for item in found if item) / len(found) if found else 1.0
