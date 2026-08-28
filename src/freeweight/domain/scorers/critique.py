"""freeweight.domain.scorers.critique — rung-2 scoring for ``native.critique``.

Benchmark catalog §3.10. A case supplies a question, a candidate response, and *known
correctness*; the model reviews the response and, where it disagrees, supplies a correction. Every
number here comes from comparing the model's declared verdict and its corrected answer against the
corpus's own ground truth, so nothing in this suite is judged.

**Regression rate is a headline metric, not a footnote.** A critic that improves nine answers and
breaks one is a different tool from a critic that improves nine and breaks none, and a suite
reporting only uplift would present them identically. The catalog says so and this module makes it
structural: ``regression_rate`` is measured on exactly the cases where the candidate answer was
*already correct*, and it is omitted — never zeroed — on the cases where it has no meaning.

**Uplift is measured per case, so it survives averaging.** ``correction_uplift`` is
``post_correction_accuracy − original_accuracy`` for one case, in ``{-1, 0, +1}``. The mean of the
per-case differences *is* the difference of the means, so the run-level figure is the catalog's
definition exactly, computed through the ordinary aggregation path rather than through a special
case that would have to be kept in step with it.

**The critic's verdict is read from a declared field, never from prose.** A response that does not
say ``"verdict"`` has not delivered a verdict, and inferring one from the tone of a paragraph would
make this module a judge sitting on top of the model it is measuring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.exact import normalize
from freeweight.domain.scorers.schema import extract_json
from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from freeweight.domain.benchmark import BenchmarkCase

__all__ = [
    "EXPECTATION_KEY",
    "CritiqueExpectation",
    "CritiqueResponse",
    "CritiqueScorer",
    "critique_metrics",
    "parse_critique",
]

EXPECTATION_KEY = "critique"
"""The key under which a case declares the candidate answer's known correctness."""

ERROR_NO_EXPECTATION = "NO_EXPECTATION"
"""The case declared no critique expectation, so "was the critic right" has no answer."""

ERROR_UNPARSEABLE = "CRITIQUE_UNPARSEABLE"
"""The answer carried no verdict this module can read."""

_VERDICT_CORRECT = "correct"
_VERDICT_INCORRECT = "incorrect"
_EVIDENCE_CHARACTERS = 200
"""How much of the corrected answer is kept beside the score (spec §14's bounded exception)."""


@dataclass(frozen=True, slots=True)
class CritiqueExpectation:
    """What the corpus knows about the candidate answer the model was shown.

    Attributes:
        answer_is_correct: Whether the candidate answer was already right. This is the corpus's
            ground truth and the axis every metric here splits on.
        gold_answers: The accepted answers. At least one; several exist because "3" and "three"
            are the same answer.
        candidate_answer: The answer the model was asked to review, so the scorer can decide the
            final answer when the critic declares the candidate correct and offers no correction.
        normalizations: Which :class:`~freeweight.domain.scorers.exact.Normalization` names to
            apply to both sides before comparing.
    """

    answer_is_correct: bool
    gold_answers: tuple[str, ...]
    candidate_answer: str = ""
    normalizations: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> CritiqueExpectation:
        """Build one expectation from a case declaration.

        Args:
            body: The ``expectation["critique"]`` object.

        Returns:
            The expectation.

        Raises:
            ValueError: ``answer_is_correct`` is missing or is not a boolean, no gold answer is
                declared, or the case says the candidate is correct while the candidate does not
                match any gold answer. The last is a contradiction the corpus must not be able to
                express: a case whose own ground truth disagrees with itself would score every
                critic wrong.
        """
        flag = body.get("answer_is_correct")
        if not isinstance(flag, bool):
            raise ValueError(
                f"A critique case needs a boolean 'answer_is_correct'; got {flag!r}. Without it "
                "there is no ground truth for the critic to be right or wrong about."
            )
        gold = tuple(str(item) for item in body.get("gold_answers", ()) if str(item))
        if not gold:
            raise ValueError("A critique case needs at least one 'gold_answers' entry.")
        normalizations = tuple(str(item) for item in body.get("normalize", ()))
        candidate = str(body.get("candidate_answer", ""))
        expectation = cls(
            answer_is_correct=flag,
            gold_answers=gold,
            candidate_answer=candidate,
            normalizations=normalizations,
        )
        if flag and not expectation.matches_gold(candidate):
            raise ValueError(
                "A critique case declares answer_is_correct=true but its candidate_answer matches "
                "no gold answer. The case's own ground truth contradicts itself, and every critic "
                "would be scored against the contradiction."
            )
        return expectation

    def matches_gold(self, text: str) -> bool:
        """Whether ``text`` is one of the accepted answers after normalization."""
        normalized = normalize(text, self.normalizations)
        return any(normalize(gold, self.normalizations) == normalized for gold in self.gold_answers)


@dataclass(frozen=True, slots=True)
class CritiqueResponse:
    """What the critic declared.

    Attributes:
        flagged: Whether the critic said the candidate answer was wrong.
        corrected_answer: The answer it offered instead, or ``None`` when it offered none.
    """

    flagged: bool
    corrected_answer: str | None = None


def parse_critique(text: str) -> CritiqueResponse | None:
    """Read the critic's verdict block, or refuse to.

    The accepted shape is a JSON object carrying ``verdict`` — ``"correct"`` or ``"incorrect"`` —
    and optionally ``corrected_answer``. Anything else yields ``None``.

    Args:
        text: Exactly what the model returned.

    Returns:
        The parsed verdict, or ``None`` when none could be read.
    """
    document, error = extract_json(text)
    if error is not None or not isinstance(document, dict):
        return None
    verdict = str(document.get("verdict", "")).strip().casefold()
    if verdict not in {_VERDICT_CORRECT, _VERDICT_INCORRECT}:
        return None
    corrected = document.get("corrected_answer")
    return CritiqueResponse(
        flagged=verdict == _VERDICT_INCORRECT,
        corrected_answer=str(corrected) if isinstance(corrected, str) and corrected else None,
    )


def _final_answer(expectation: CritiqueExpectation, response: CritiqueResponse) -> str:
    """Return the answer that stands after the critique.

    The candidate's own answer when the critic endorsed it or offered no replacement; the
    correction otherwise. This is what makes "the critic broke a correct answer" observable: the
    only way a regression happens is a critic that flagged a right answer *and* replaced it with a
    wrong one, and a scorer that always used the candidate answer could never see it.
    """
    if response.flagged and response.corrected_answer is not None:
        return response.corrected_answer
    return expectation.candidate_answer


def critique_metrics(
    expectation: CritiqueExpectation, response: CritiqueResponse
) -> tuple[float, dict[str, float], dict[str, Any]]:
    """Compute one case's critique figures.

    Every figure whose denominator is empty for this case is **omitted** rather than zeroed:
    ``error_detection_recall`` has no meaning on a case whose candidate answer was already right,
    ``hallucinated_criticism_rate`` and ``regression_rate`` have none on a case whose candidate
    answer was wrong, and ``criticism_precision`` has none on a case the critic did not flag.

    Args:
        expectation: The corpus's ground truth.
        response: What the critic declared.

    Returns:
        ``(score, metrics, evidence)``. ``score`` is post-correction accuracy: ``1.0`` when the
        answer standing after the critique is right.
    """
    final = _final_answer(expectation, response)
    post = 1.0 if expectation.matches_gold(final) else 0.0
    original = 1.0 if expectation.answer_is_correct else 0.0

    metrics: dict[str, float] = {
        "post_correction_accuracy": post,
        "original_accuracy": original,
        "correction_uplift": post - original,
    }
    if not expectation.answer_is_correct:
        metrics["error_detection_recall"] = 1.0 if response.flagged else 0.0
    else:
        metrics["hallucinated_criticism_rate"] = 1.0 if response.flagged else 0.0
        metrics["regression_rate"] = 1.0 if post < original else 0.0
    if response.flagged:
        metrics["criticism_precision"] = 0.0 if expectation.answer_is_correct else 1.0
    if response.flagged and response.corrected_answer is not None:
        metrics["valid_correction_rate"] = 1.0 if expectation.matches_gold(final) else 0.0

    evidence: dict[str, Any] = {
        "candidate_was_correct": expectation.answer_is_correct,
        "critic_flagged": response.flagged,
        "offered_correction": response.corrected_answer is not None,
        "final_answer_excerpt": final[:_EVIDENCE_CHARACTERS],
    }
    return post, metrics, evidence


@dataclass(frozen=True, slots=True)
class CritiqueScorer:
    """Scores one critique against the corpus's known correctness.

    The headline ``score`` is post-correction accuracy. Uplift, regression rate, detection recall,
    criticism precision and the hallucinated-criticism rate travel in ``detail`` as their own
    metrics: a critic is not one number, and the pair that matters most — uplift and regression —
    can only be read together.
    """

    key: str = "critique"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Compare the critic's verdict and correction with the case's ground truth.

        Args:
            case: The case, carrying ``expectation["critique"]``.
            response_text: Exactly what the model returned.

        Returns:
            The verdict. ``score=None`` with :data:`ERROR_NO_EXPECTATION` when the case declares
            no ground truth, and with :data:`ERROR_UNPARSEABLE` when the answer carries no
            verdict — a model that answered in prose has not been measured, and scoring it
            ``0.0`` would record a critique failure that was never observed.
        """
        declared = case.expectation.get(EXPECTATION_KEY)
        if not isinstance(declared, dict):
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_NO_EXPECTATION,
                error_text=(
                    f"Case {case.case_id!r} declares no expectation[{EXPECTATION_KEY!r}], so "
                    "there is no known correctness for the critic to be measured against."
                ),
            )
        try:
            expectation = CritiqueExpectation.from_json(declared)
        except ValueError as exc:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_NO_EXPECTATION,
                error_text=(
                    f"Case {case.case_id!r} declares a critique expectation it cannot use: {exc}"
                ),
            )
        parsed = parse_critique(response_text)
        if parsed is None:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_UNPARSEABLE,
                error_text=(
                    f"Case {case.case_id!r}: the answer carries no 'verdict' field. Inferring a "
                    "verdict from prose would make this scorer a judge of the model it measures."
                ),
            )
        score, metrics, evidence = critique_metrics(expectation, parsed)
        return ScoreResult(
            score=score,
            method=self.method,
            detail={"case": case.case_id, **metrics, **evidence},
        )
