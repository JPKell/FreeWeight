"""freeweight.domain.scorers.exact — rung-2 exact matching, with declared normalization.

The simplest scorer in the application, and the one whose only real decision is *what counts as
the same string*. That decision is declared per case rather than baked in here: a case that means
"the answer is 42" and a case that means "the answer is exactly `SELECT id FROM users`" want
different normalization, and a scorer that always lowercased would quietly pass the second one for
the wrong text.

Nothing here is a judgement. A comparison this module cannot make — a case with no expected answer
at all — returns ``score=None`` with a reason rather than ``0.0``, because "we never said what
right looks like" is a defect in the case, not a failure by the model (ADR-0016).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from freeweight.domain.benchmark import BenchmarkCase

__all__ = ["EXPECTATION_KEY", "ExactMatchScorer", "Normalization", "normalize"]

EXPECTATION_KEY = "exact"
"""The key under which a case declares what this scorer compares against."""

ERROR_NO_EXPECTATION = "NO_EXPECTATION"
"""No expected answer was declared, so there is nothing to compare the response with."""

_EVIDENCE_CHARACTERS = 200
"""How much of the answer is kept beside the score.

Bounded on purpose. ``ScoreResult.detail`` exists so a headline metric drills to "the expected and
actual values" a person can read, and a sample that kept the whole answer would be storing the
response by default — which spec §14 reserves for a run that asked for it."""


class Normalization(StrEnum):
    """One transformation applied to both sides before comparison.

    Declared per case and recorded in the score's ``detail``, so a passing result names the
    latitude it was given rather than leaving a reader to guess whether case mattered.
    """

    CASE = "case"
    """Casefold both sides. Unicode casefolding, not ``lower()``: ``ß`` and ``ss`` compare equal."""

    WHITESPACE = "whitespace"
    """Collapse every run of whitespace to one space and strip the ends."""

    PUNCTUATION = "punctuation"
    """Drop Unicode punctuation, so a trailing full stop is not a failure."""

    TRAILING = "trailing"
    """Strip leading and trailing whitespace only, leaving interior spacing alone."""


def normalize(text: str, normalizations: Sequence[str]) -> str:
    """Apply ``normalizations`` to ``text``, in a fixed order.

    The order is fixed here rather than taken from the case: applying ``punctuation`` before
    ``whitespace`` and applying it after produce different strings for ``"a . b"``, and a case
    that could choose would make two identically-declared cases score differently.

    Args:
        text: The string to normalize.
        normalizations: The names to apply; unknown names are ignored, because a case declaring a
            normalization this build does not have should not silently compare *less* strictly
            than it asked for — it compares more strictly, and the mismatch is visible in the
            detail.

    Returns:
        The normalized string.
    """
    wanted = set(normalizations)
    result = text
    if Normalization.CASE in wanted:
        result = result.casefold()
    if Normalization.PUNCTUATION in wanted:
        result = "".join(
            character for character in result if not unicodedata.category(character).startswith("P")
        )
    if Normalization.WHITESPACE in wanted:
        result = " ".join(result.split())
    elif Normalization.TRAILING in wanted:
        result = result.strip()
    return result


@dataclass(frozen=True, slots=True)
class ExactMatchScorer:
    """Scores ``1.0`` when the response equals one declared answer after normalization.

    The case declares, under ``expectation["exact"]``:

    ``any_of``
        The accepted answers. One is enough; several exist because "3" and "three" are the same
        answer and neither is more correct.
    ``normalize``
        The :class:`Normalization` names to apply to both sides. Absent means byte equality.
    ``contains``
        When ``True``, an accepted answer appearing *anywhere* in the response passes. This is the
        form the agent suite uses, where the final turn is prose that has to contain the answer;
        it is opt-in because for a short-answer case it would pass a response that also contained
        four wrong answers.
    """

    key: str = "exact_match"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Compare ``response_text`` with the case's declared answers.

        Args:
            case: The case, carrying its expectation.
            response_text: Exactly what the model returned.

        Returns:
            ``1.0`` for a match, ``0.0`` for a mismatch, and ``score=None`` with
            :data:`ERROR_NO_EXPECTATION` when the case declared no answer to compare against —
            an unmeasurable case, not a failed one.
        """
        expectation = case.expectation.get(EXPECTATION_KEY)
        accepted = _accepted(expectation)
        if not accepted:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_NO_EXPECTATION,
                error_text=(
                    f"Case {case.case_id!r} declares no expectation[{EXPECTATION_KEY!r}].any_of, "
                    "so there is no answer to compare the response with."
                ),
            )
        declared = expectation if isinstance(expectation, dict) else {}
        normalizations = [str(name) for name in declared.get("normalize", ())]
        contains = bool(declared.get("contains", False))
        candidate = normalize(response_text, normalizations)
        matched = next(
            (
                answer
                for answer in accepted
                if _matches(candidate, normalize(answer, normalizations), contains=contains)
            ),
            None,
        )
        return ScoreResult(
            score=1.0 if matched is not None else 0.0,
            method=self.method,
            detail={
                "case": case.case_id,
                "matched": matched,
                "accepted": list(accepted),
                "normalize": normalizations,
                "contains": contains,
                "response_chars": len(response_text),
                "response": candidate[:_EVIDENCE_CHARACTERS],
            },
        )


def _matches(candidate: str, answer: str, *, contains: bool) -> bool:
    """Return whether one normalized answer matches the normalized response."""
    return answer in candidate if contains else candidate == answer


def _accepted(expectation: Any) -> tuple[str, ...]:  # noqa: ANN401 — a stored JSON value
    """Return the declared answers, tolerating a bare string or a bare list.

    Tolerated because a case is data: ``{"exact": "42"}`` and ``{"exact": {"any_of": ["42"]}}``
    mean the same thing to the person writing the case, and refusing the short form would trade a
    real scoring failure for a formatting one.
    """
    if isinstance(expectation, str):
        return (expectation,)
    if isinstance(expectation, list | tuple):
        return tuple(str(item) for item in expectation)
    if isinstance(expectation, dict):
        declared = expectation.get("any_of", expectation.get("answer"))
        if isinstance(declared, str):
            return (declared,)
        if isinstance(declared, list | tuple):
            return tuple(str(item) for item in declared)
    return ()
