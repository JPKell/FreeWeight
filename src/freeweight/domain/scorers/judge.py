"""freeweight.domain.scorers.judge — rung-2 scoring for ``native.judge``.

Benchmark catalog §3.11 is a table of seven tests, and every row of it is decided by *counting
verdicts*, never by reading them. That is what makes a suite about model judgement a rung-2
measurement: the model under test supplies opinions, and this module compares those opinions with
each other and with the corpus's gold preference. No model decides anything here.

The seven kinds, and the question each answers:

| Kind | Question | Figure |
|---|---|---|
| ``pairwise`` | Does it pick the better answer? | ``pairwise_accuracy`` |
| ``position`` | Does the swap change the verdict? | ``swap_consistency`` |
| ``repetition`` | Asked three times, same answer? | ``repetition_agreement_rate`` |
| ``verbosity`` | Does length beat correctness? | ``verbosity_preference_rate`` |
| ``style`` | Content constant, does presentation move it? | ``style_preference_rate`` |
| ``transitivity`` | A>B and B>C — still A>C? | ``transitivity_violation_rate`` |
| ``self_preference`` | Does attribution move its verdict? | ``self_preference_delta`` |

**A verdict is matched by subject, never by position.** :meth:`JudgeTrial.chosen_subject` is what
makes a swapped presentation comparable with its original, and every figure below is computed from
subjects. A scorer that compared positions would report a perfectly position-biased judge as
perfectly consistent.

**An unparseable verdict is excluded, not counted as a disagreement.** A judge that answered in
prose has not disagreed with itself; it has not answered. Where every trial in a case is
unparseable the case is unscoreable — ``score=None`` with a reason — and it stays visible in the
sample count instead of contributing a zero
([ADR-0016](../../../../../docs/adr/0016-unavailable-is-not-zero.md)).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from freeweight.domain.judging import (
    JudgeChoice,
    JudgeRecord,
    JudgeTrial,
    agreement_rate,
    majority_choice,
)
from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.domain.benchmark import BenchmarkCase

__all__ = [
    "EXPECTATION_KEY",
    "JudgeExpectation",
    "JudgeScorer",
    "JudgeTestKind",
    "judge_metrics",
]

EXPECTATION_KEY = "judge"
"""The key under which a case declares which bias it measures and what the gold answer is."""

ERROR_NO_EXPECTATION = "NO_EXPECTATION"
"""The case declared no judge expectation, so there is nothing to measure against."""

ERROR_NO_RECORD = "JUDGE_RECORD_MISSING"
"""The interaction produced no readable trial record — a harness failure, not a judge failure."""

ERROR_NO_USABLE_VERDICT = "JUDGE_UNPARSEABLE"
"""Every presentation in this case came back in a form no verdict could be read from."""


class JudgeTestKind(StrEnum):
    """Which of benchmark catalog §3.11's seven tests a case belongs to."""

    PAIRWISE = "pairwise"
    POSITION = "position"
    REPETITION = "repetition"
    VERBOSITY = "verbosity"
    STYLE = "style"
    TRANSITIVITY = "transitivity"
    SELF_PREFERENCE = "self_preference"


@dataclass(frozen=True, slots=True)
class JudgeExpectation:
    """What one judge case declares about the comparison it presents.

    Attributes:
        kind: Which test this case belongs to.
        gold: The subject the corpus says should win, for :attr:`JudgeTestKind.PAIRWISE`.
        disfavoured: The subject a *biased* judge would pick — the verbose-but-weaker answer, or
            the flashily-styled one. Named for what it measures rather than for the test, because
            verbosity and style bias differ only in what was varied.
        own: The subject that is the judge's own answer, for
            :attr:`JudgeTestKind.SELF_PREFERENCE`.
        ordering: For :attr:`JudgeTestKind.TRANSITIVITY`, the three subjects in the order the
            corpus claims, best first. Recorded so the violation test knows which direction the
            transitive chain runs.
    """

    kind: JudgeTestKind
    gold: str = ""
    disfavoured: str = ""
    own: str = ""
    ordering: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> JudgeExpectation:
        """Build one expectation from a case declaration.

        Args:
            body: The ``expectation["judge"]`` object.

        Returns:
            The expectation.

        Raises:
            ValueError: ``kind`` is missing or unknown, or the kind's own required field is
                absent. Each kind needs exactly one thing to compare against and a case missing it
                would score every judge identically, which is the failure that is hardest to see
                from the outside.
        """
        raw = str(body.get("kind", ""))
        try:
            kind = JudgeTestKind(raw)
        except ValueError as exc:
            raise ValueError(
                f"Unknown judge test kind {raw!r}; this build measures "
                f"{[member.value for member in JudgeTestKind]}."
            ) from exc
        expectation = cls(
            kind=kind,
            gold=str(body.get("gold", "")),
            disfavoured=str(body.get("disfavoured", "")),
            own=str(body.get("own", "")),
            ordering=tuple(str(item) for item in body.get("ordering", ())),
        )
        required = {
            JudgeTestKind.PAIRWISE: ("gold", expectation.gold),
            JudgeTestKind.VERBOSITY: ("disfavoured", expectation.disfavoured),
            JudgeTestKind.STYLE: ("disfavoured", expectation.disfavoured),
            JudgeTestKind.SELF_PREFERENCE: ("own", expectation.own),
        }.get(kind)
        if required is not None and not required[1]:
            raise ValueError(
                f"A {kind.value!r} judge case must declare {required[0]!r}; without it the case "
                "cannot distinguish a biased judge from an unbiased one."
            )
        if kind is JudgeTestKind.TRANSITIVITY and len(expectation.ordering) != 3:
            raise ValueError(
                f"A transitivity case declares three subjects best-first; got "
                f"{list(expectation.ordering)}."
            )
        return expectation


def _grouped(trials: Sequence[JudgeTrial]) -> dict[str, list[JudgeTrial]]:
    """Index trials by their group tag, preserving order within each group."""
    groups: dict[str, list[JudgeTrial]] = {}
    for trial in trials:
        groups.setdefault(trial.group, []).append(trial)
    return groups


def _preference_rate(trials: Sequence[JudgeTrial], subject: str) -> float | None:
    """Return the share of usable trials that preferred ``subject``.

    ``None`` when no trial produced a usable verdict: a preference rate over nothing is not zero
    preference, it is no measurement.
    """
    usable = [trial for trial in trials if trial.choice is not JudgeChoice.UNPARSEABLE]
    if not usable:
        return None
    return sum(1.0 for trial in usable if trial.chosen_subject == subject) / len(usable)


def _winner(trials: Sequence[JudgeTrial]) -> str | None:
    """Return the subject a group of trials preferred, or ``None`` for a tie or no verdict."""
    usable = [trial for trial in trials if trial.choice is not JudgeChoice.UNPARSEABLE]
    if not usable:
        return None
    subjects = [trial.chosen_subject for trial in usable if trial.chosen_subject is not None]
    if not subjects:
        return None
    tally: dict[str, int] = {}
    for subject in subjects:
        tally[subject] = tally.get(subject, 0) + 1
    ranked = sorted(tally.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def judge_metrics(  # noqa: PLR0912 — one branch per catalog §3.11 row, which is the point
    expectation: JudgeExpectation, record: JudgeRecord
) -> tuple[float | None, dict[str, float], dict[str, Any]]:
    """Compute one judge case's figures from its trial record.

    Args:
        expectation: What the case declared.
        record: Every presentation the interaction made, in order.

    Returns:
        ``(score, metrics, evidence)``. ``score`` is ``None`` when no usable verdict came back;
        otherwise it is the case's contribution to "how good is this model at judging", where
        higher is always better — so a bias figure enters the score as ``1 − rate``.
    """
    trials = record.trials
    usable = [trial for trial in trials if trial.choice is not JudgeChoice.UNPARSEABLE]
    metrics: dict[str, float] = {}
    evidence: dict[str, Any] = {
        "kind": expectation.kind.value,
        "trials": len(trials),
        "usable_trials": len(usable),
        "verdicts": [
            {"group": trial.group, "order": list(trial.order), "choice": trial.choice.value}
            for trial in trials
        ],
    }
    if not usable:
        return None, metrics, evidence

    score: float | None = None
    match expectation.kind:
        case JudgeTestKind.PAIRWISE:
            rate = _preference_rate(trials, expectation.gold)
            if rate is not None:
                metrics["pairwise_accuracy"] = rate
                score = rate
        case JudgeTestKind.POSITION:
            score = _position(trials, metrics)
        case JudgeTestKind.REPETITION:
            rate = agreement_rate(trial.choice for trial in trials)
            if rate is not None:
                metrics["repetition_agreement_rate"] = rate
                score = rate
            evidence["majority_verdict"] = majority_choice(trial.choice for trial in trials).value
        case JudgeTestKind.VERBOSITY:
            rate = _preference_rate(trials, expectation.disfavoured)
            if rate is not None:
                metrics["verbosity_preference_rate"] = rate
                score = 1.0 - rate
        case JudgeTestKind.STYLE:
            rate = _preference_rate(trials, expectation.disfavoured)
            if rate is not None:
                metrics["style_preference_rate"] = rate
                score = 1.0 - rate
        case JudgeTestKind.TRANSITIVITY:
            score = _transitivity(trials, expectation, metrics, evidence)
        case _:  # JudgeTestKind.SELF_PREFERENCE
            score = _self_preference(trials, expectation, metrics)
    return score, metrics, evidence


def _position(trials: Sequence[JudgeTrial], metrics: dict[str, float]) -> float | None:
    """Score a position-bias case: the same pair, presented both ways round.

    ``swap_consistency`` is ``1.0`` when both presentations reached the same verdict about the
    same *subject* — or when both declared a tie, which is a verdict the swap did not move either.
    It is the figure that says *whether* the judge is position-biased, and it is the one that
    enters the score.

    ``position_preference_rate`` says *which way*, and is emitted **only when the pair was
    inconsistent**. An inconsistent swap means the same position won twice, so ``1.0`` is a
    preference for the first position and ``0.0`` a preference for the second; neither is better
    than the other, and a consistent pair has no direction to report at all. A rate whose
    denominator is empty for this case is absent, not zero (ADR-0016), which is what keeps a
    consistent judge out of the denominator rather than recorded as unbiased-towards-second.
    """
    usable = [trial for trial in trials if trial.choice is not JudgeChoice.UNPARSEABLE]
    if len(usable) < 2:  # noqa: PLR2004 — a swap needs both of its two presentations
        return None
    first, second = usable[0], usable[1]
    both_tied = first.choice is JudgeChoice.TIE and second.choice is JudgeChoice.TIE
    consistent = both_tied or (
        first.chosen_subject is not None and first.chosen_subject == second.chosen_subject
    )
    metrics["swap_consistency"] = 1.0 if consistent else 0.0
    if not consistent:
        chose_first_both = first.choice is JudgeChoice.FIRST and second.choice is JudgeChoice.FIRST
        metrics["position_preference_rate"] = 1.0 if chose_first_both else 0.0
    return metrics["swap_consistency"]


def _transitivity(
    trials: Sequence[JudgeTrial],
    expectation: JudgeExpectation,
    metrics: dict[str, float],
    evidence: dict[str, Any],
) -> float | None:
    """Score a transitivity case: A>B and B>C, so A>C — or a violation is recorded.

    A violation is only *countable* when all three sub-comparisons produced a verdict and the
    first two agree with the corpus's chain. A judge that tied on ``B`` versus ``C`` has not
    violated transitivity; it has declined to build the chain, and counting that as a violation
    would report indecision as incoherence.
    """
    groups = _grouped(trials)
    winners = {name: _winner(group) for name, group in groups.items()}
    evidence["chain"] = {name: winner for name, winner in sorted(winners.items())}
    best, middle, worst = expectation.ordering
    ab, bc, ac = winners.get("ab"), winners.get("bc"), winners.get("ac")
    if ab is None or bc is None or ac is None:
        return None
    if ab == best and bc == middle:
        violated = ac != best
        metrics["transitivity_violation_rate"] = 1.0 if violated else 0.0
        return 0.0 if violated else 1.0
    # The judge's own chain, whatever it is, must still be internally consistent: if it said B
    # beats A and C beats B, it must not then say A beats C.
    implied = {ab, bc}
    if implied == {middle, worst} and ac == best:
        metrics["transitivity_violation_rate"] = 1.0
        return 0.0
    metrics["transitivity_violation_rate"] = 0.0
    return 1.0


def _self_preference(
    trials: Sequence[JudgeTrial], expectation: JudgeExpectation, metrics: dict[str, float]
) -> float | None:
    """Score a self-preference case: the same pair, anonymized and attributed.

    The delta is what the catalog asks for — ``attributed − anonymized`` — because a judge that
    prefers one answer in both conditions may simply be right. What is a bias is preferring it
    *more* once it is told the answer is its own.
    """
    groups = _grouped(trials)
    anonymized = _preference_rate(groups.get("anonymized", ()), expectation.own)
    attributed = _preference_rate(groups.get("attributed", ()), expectation.own)
    if anonymized is None or attributed is None:
        return None
    metrics["self_preference_anonymized"] = anonymized
    metrics["self_preference_attributed"] = attributed
    metrics["self_preference_delta"] = attributed - anonymized
    return 1.0 - max(0.0, attributed - anonymized)


@dataclass(frozen=True, slots=True)
class JudgeScorer:
    """Scores one judge case from the trial record its interaction produced.

    The record arrives as the sample's response text — canonical JSON written by
    :meth:`~freeweight.domain.judging.JudgeRecord.as_text` — because a judged case is a *set* of
    presentations and its final turn alone would describe none of them. That keeps this scorer a
    pure function of ``(expectation, text)``, which is what lets a position-biased judge be a
    table-driven unit test rather than a field report.
    """

    key: str = "judge_bias"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Compute this case's bias figures.

        Args:
            case: The case, carrying ``expectation["judge"]``.
            response_text: The serialized :class:`~freeweight.domain.judging.JudgeRecord`.

        Returns:
            The verdict, with every measured figure in ``detail``. ``score=None`` when the case
            declares nothing to measure, when no record could be read, or when no presentation
            produced a usable verdict.
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
                    "there is no bias for this case to measure."
                ),
            )
        try:
            expectation = JudgeExpectation.from_json(declared)
        except ValueError as exc:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_NO_EXPECTATION,
                error_text=(
                    f"Case {case.case_id!r} declares a judge expectation it cannot use: {exc}"
                ),
            )
        record = JudgeRecord.from_text(response_text)
        if record is None:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id, "kind": expectation.kind.value},
                error_code=ERROR_NO_RECORD,
                error_text=(
                    f"Case {case.case_id!r} produced no readable trial record. That is a harness "
                    "failure rather than a judge failure, and it is not scored as one."
                ),
            )
        score, metrics, evidence = judge_metrics(expectation, record)
        if score is None:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id, **metrics, **evidence},
                error_code=ERROR_NO_USABLE_VERDICT,
                error_text=(
                    f"Case {case.case_id!r}: no presentation produced a verdict this build can "
                    "read, so there is nothing to compare."
                ),
            )
        return ScoreResult(
            score=score,
            method=self.method,
            detail={"case": case.case_id, **metrics, **evidence},
        )
