"""freeweight.domain.scorers.judged — a jury's verdicts become one criterion score.

Rung 5's arithmetic, with the provider already out of the picture: this module is handed the
grades a jury produced and turns them into a criterion outcome. Two modes, both from
[Subjective Goals §3.4 and §4.1](../../../../../docs/apps/freeweight/subjective-goals.md):

* **absolute** — each juror grades on the criterion's ordinal scale; the criterion's raw score is
  ``(median − 1) / (points − 1)``;
* **pairwise** — each juror compares the candidate against a reference in both orders; the
  criterion scores the candidate's win rate.

**Jurors are combined by median, never by mean.** A single juror misreading a rubric should not
drag the score, and the median is what the inter-juror agreement figure is reported *against*. A
mean would also destroy the property the whole design rests on: the jury's dispersion is the
measurement's error bar, and it is reported beside the number rather than folded into it.

**Every verdict is retained.** :class:`JudgedCriterionResult` carries them all, because
``judge_verdicts`` stores one row per juror per repetition and a scorer that returned only the
median would leave that table with nothing to write.

**A refusal is not a low grade.** A juror that refused — self-judging, a protocol error, a
timeout — contributes no grade at all and is recorded with its reason. Where *no* juror produced a
usable grade the criterion is skipped with ``raw_score = NULL``, never scored zero.

Pure domain: stdlib and this package's own modules.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from freeweight.domain.agreement import krippendorff_alpha
from freeweight.domain.goals.criteria import CriterionOutcome, CriterionStatus, SkipReason
from freeweight.domain.jury import ERROR_JUDGE_UNAVAILABLE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from freeweight.domain.goals.pack import Criterion

__all__ = [
    "PAIRWISE",
    "JudgedCriterionResult",
    "JurorVerdict",
    "combine_verdicts",
    "inter_juror_agreement",
    "normalize_grade",
    "pairwise_win_rate",
]

PAIRWISE = "pairwise"
"""The criterion mode that compares against a reference instead of grading on a scale."""

_MINIMUM_FOR_ALPHA = 2


@dataclass(frozen=True, slots=True)
class JurorVerdict:
    """One juror's answer for one criterion on one sample, for one repetition.

    Attributes:
        juror_canonical_id: Which model answered.
        juror_ordinal: Its position in the jury, so a stored row keys uniquely.
        repetition: Which repetition this was.
        grade: The grade on the criterion's scale, or ``None`` for a pairwise verdict or a
            refusal.
        pairwise_choice: ``"candidate"``, ``"reference"`` or ``"tie"``, or ``None``.
        presentation_order: The order the two answers were shown in. Recorded, because order bias
            is measured rather than assumed.
        rationale: The juror's one-sentence reason, bounded by the caller.
        refused_reason: Why there is no verdict — ``self_judging``, ``protocol_error``,
            ``timeout`` — or ``None``.
        latency_ms: How long the call took.
        input_tokens: What the juror's own call cost, as the provider reported it, or ``None``
            where it reported none. A jury is not free, and ``judge_verdicts`` carries the cost so
            a user can see what a judged goal spends.
        output_tokens: As above.
        remote: Whether this juror ran off the measuring machine. Recorded per verdict as well as
            on the jury, because a mixed jury is a thing a reader has to be able to see.
    """

    juror_canonical_id: str
    juror_ordinal: int
    repetition: int
    grade: int | None = None
    pairwise_choice: str | None = None
    presentation_order: str = "candidate_first"
    rationale: str | None = None
    refused_reason: str | None = None
    latency_ms: float | None = None
    input_tokens: float | None = None
    output_tokens: float | None = None
    remote: bool = False

    @property
    def usable(self) -> bool:
        """Whether this verdict contributes to the criterion's score."""
        return self.refused_reason is None and (
            self.grade is not None or self.pairwise_choice is not None
        )


@dataclass(frozen=True, slots=True)
class JudgedCriterionResult:
    """One judged criterion's outcome on one sample, with the jury's dispersion beside it.

    Attributes:
        outcome: The criterion outcome the composite consumes.
        verdicts: Every verdict, retained in full for ``judge_verdicts``.
        median_grade: The jury median on the criterion's scale, or ``None`` for pairwise/refused.
        inter_juror_alpha: Krippendorff's alpha across jurors, or ``None`` for a single-juror jury
            — where the quantity does not exist rather than being zero.
    """

    outcome: CriterionOutcome
    verdicts: tuple[JurorVerdict, ...] = ()
    median_grade: float | None = None
    inter_juror_alpha: float | None = None


def normalize_grade(grade: float, *, points: int) -> float:
    """Map an ordinal grade onto ``0.0..1.0``.

    ``(grade − 1) / (points − 1)`` — Subjective Goals §4.1 exactly. The bottom of the scale is
    ``0.0`` rather than ``1/points``, because a grader who gave the lowest grade meant the lowest
    score and not "a fifth of the way up".

    Args:
        grade: The grade, ``1..points``. May be fractional: a jury median of four grades can be.
        points: The scale's size.

    Returns:
        The normalized score.

    Raises:
        ValueError: ``points`` is below 2, or ``grade`` is outside ``1..points``.
    """
    if points < _MINIMUM_FOR_ALPHA:
        raise ValueError(f"An ordinal scale needs at least 2 points; got {points}.")
    if not 1 <= grade <= points:
        raise ValueError(f"A grade of {grade} is outside 1..{points}.")
    return (grade - 1) / (points - 1)


def pairwise_win_rate(verdicts: Sequence[JurorVerdict]) -> float | None:
    """Return the candidate's share of the usable pairwise verdicts, ties counting as a half.

    Ties as a half rather than as a loss: a jury that could not separate the candidate from the
    reference has said they are equivalent, and scoring that as a defeat would make "as good as
    the reference" worse than it is.

    Args:
        verdicts: Every pairwise verdict for one criterion.

    Returns:
        The win rate, or ``None`` when no verdict was usable.
    """
    usable = [verdict for verdict in verdicts if verdict.usable and verdict.pairwise_choice]
    if not usable:
        return None
    wins = sum(_pairwise_points(verdict.pairwise_choice) for verdict in usable)
    return wins / len(usable)


def _pairwise_points(choice: str | None) -> float:
    """Return one pairwise verdict's contribution to the win rate."""
    if choice == "candidate":
        return 1.0
    return 0.5 if choice == "tie" else 0.0


def _alpha_across_jurors(verdicts: Sequence[JurorVerdict]) -> float | None:
    """Krippendorff's alpha across jurors on one sample, one unit per repetition.

    ``None`` in two cases, and neither is zero:

    * **one juror** — there is no inter-juror agreement, only self-consistency, and a biased juror
      exhibits that perfectly (ADR-0031 §4). Reporting ``1.0`` would be the most flattering
      possible lie;
    * **one repetition** — alpha over a single unit is degenerate and evaluates to ``0.0`` no
      matter how well the jurors agreed, which would report unanimity as chance.

    The figure that appears beside a goal's score is computed across *samples* by
    :func:`inter_juror_agreement`, where there are enough units for it to mean something.
    """
    by_repetition: dict[int, dict[int, float]] = {}
    jurors: set[int] = set()
    for verdict in verdicts:
        if not verdict.usable or verdict.grade is None:
            continue
        jurors.add(verdict.juror_ordinal)
        by_repetition.setdefault(verdict.repetition, {})[verdict.juror_ordinal] = float(
            verdict.grade
        )
    if len(jurors) < _MINIMUM_FOR_ALPHA or len(by_repetition) < _MINIMUM_FOR_ALPHA:
        return None
    ordered = sorted(jurors)
    units = [
        [grades.get(juror) for juror in ordered]
        for _repetition, grades in sorted(by_repetition.items())
    ]
    return krippendorff_alpha(units)


def inter_juror_agreement(results: Sequence[JudgedCriterionResult]) -> float | None:
    """Krippendorff's alpha across jurors for one criterion, over every sample it scored.

    This is benchmark catalog §7.4's ``inter_juror_agreement``: a headline metric, not a footnote.
    One unit per ``(sample, repetition)``, one rating per juror, so the figure describes whether
    the jurors agree *with each other* — which is what distinguishes jury bias from jury noise
    when it is read beside ``kappa_w``. High alpha with low ``kappa_w`` means the jurors agree with
    each other and not with the author.

    Args:
        results: One entry per sample this criterion was scored on.

    Returns:
        The coefficient, or ``None`` for a single-juror jury or fewer than two units.
    """
    jurors: set[int] = set()
    units: list[list[float | None]] = []
    by_unit: dict[tuple[int, int], dict[int, float]] = {}
    for index, result in enumerate(results):
        for verdict in result.verdicts:
            if not verdict.usable or verdict.grade is None:
                continue
            jurors.add(verdict.juror_ordinal)
            by_unit.setdefault((index, verdict.repetition), {})[verdict.juror_ordinal] = float(
                verdict.grade
            )
    if len(jurors) < _MINIMUM_FOR_ALPHA or len(by_unit) < _MINIMUM_FOR_ALPHA:
        return None
    ordered = sorted(jurors)
    units = [[grades.get(juror) for juror in ordered] for _key, grades in sorted(by_unit.items())]
    return krippendorff_alpha(units)


def combine_verdicts(
    criterion: Criterion, verdicts: Sequence[JurorVerdict]
) -> JudgedCriterionResult:
    """Turn one criterion's jury verdicts into the outcome the composite consumes.

    Args:
        criterion: The judged criterion, carrying its scale and its mode.
        verdicts: Every verdict, refusals included.

    Returns:
        The result. When no verdict was usable the outcome is *skipped* with
        ``raw_score = NULL`` and the reason ``judge_unavailable`` — never a score of zero, which
        would record a judgement nobody made.

    Raises:
        ValueError: The criterion is not judged, or it is an absolute criterion with no scale.
            Both are authoring defects the lint refuses first; reaching here with one would mean
            scoring a criterion whose units are unknown.
    """
    from freeweight.domain.goals.pack import Rung

    if criterion.rung is not Rung.JUDGE:
        raise ValueError(
            f"Criterion {criterion.key!r} is scored at rung {criterion.rung.value!r}; "
            "combine_verdicts is for rung 5."
        )
    detail: dict[str, Any] = {
        "mode": criterion.mode or "absolute",
        "verdicts": len(verdicts),
        "usable_verdicts": sum(1 for verdict in verdicts if verdict.usable),
        "refusals": sorted(
            {verdict.refused_reason for verdict in verdicts if verdict.refused_reason}
        ),
        "jurors": sorted({verdict.juror_canonical_id for verdict in verdicts}),
    }

    if criterion.mode == PAIRWISE:
        rate = pairwise_win_rate(verdicts)
        if rate is None:
            return JudgedCriterionResult(
                outcome=_skipped(criterion, detail), verdicts=tuple(verdicts)
            )
        detail["win_rate"] = rate
        return JudgedCriterionResult(
            outcome=CriterionOutcome(
                criterion_key=criterion.key,
                rung=criterion.rung,
                weight=criterion.weight,
                raw_score=rate,
                status=CriterionStatus.SCORED,
                detail=detail,
            ),
            verdicts=tuple(verdicts),
        )

    if criterion.scale is None:
        raise ValueError(
            f"Criterion {criterion.key!r} is graded on an absolute scale but declares none; the "
            "lint refuses this at authoring time, so reaching here means a pack bypassed it."
        )
    grades = [
        float(verdict.grade) for verdict in verdicts if verdict.usable and verdict.grade is not None
    ]
    if not grades:
        return JudgedCriterionResult(outcome=_skipped(criterion, detail), verdicts=tuple(verdicts))
    median = statistics.median(grades)
    alpha = _alpha_across_jurors(verdicts)
    detail.update(
        {
            "median_grade": median,
            "grades": grades,
            "scale_points": criterion.scale.points,
            "inter_juror_agreement": alpha,
            "grade_spread": max(grades) - min(grades),
        }
    )
    return JudgedCriterionResult(
        outcome=CriterionOutcome(
            criterion_key=criterion.key,
            rung=criterion.rung,
            weight=criterion.weight,
            raw_score=normalize_grade(median, points=criterion.scale.points),
            status=CriterionStatus.SCORED,
            detail=detail,
        ),
        verdicts=tuple(verdicts),
        median_grade=median,
        inter_juror_alpha=alpha,
    )


def _skipped(criterion: Criterion, detail: dict[str, Any]) -> CriterionOutcome:
    """Return the outcome of a criterion no juror could score."""
    return CriterionOutcome(
        criterion_key=criterion.key,
        rung=criterion.rung,
        weight=criterion.weight,
        status=CriterionStatus.SKIPPED,
        skip_reason=SkipReason.JUDGE_UNAVAILABLE.value,
        detail={**detail, "error_code": ERROR_JUDGE_UNAVAILABLE},
    )
