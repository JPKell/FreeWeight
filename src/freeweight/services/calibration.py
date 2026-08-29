"""freeweight.services.calibration — collect samples, capture grades, measure the jury, gate.

The service that turns a judged criterion into a *measurement*: the author grades, the jury is
scored against grades it never saw, the agreement is reported with every number, and a rubric that
cannot be measured is refused entry to the evidence contract while still producing a fully
inspectable run
([ADR-0031 §3](../../../../docs/adr/0031-user-defined-goal-benchmarks.md),
[ADR-0032 §3](../../../../docs/adr/0032-judge-validity-and-user-capability-namespace.md)).

**The holdout is never rendered into a judge prompt.** :func:`anchors_for` reads *anchors* and
nothing else, and it is the only function that produces the mapping the jury is built with. That is
the whole mechanism: there is no code path from a holdout sample to a prompt, and the test asserts
it by scanning the rendered prompt for holdout content hashes rather than by reading the code.

**Grading is resumable.** Grades are upserted per ``(sample, criterion)`` and progress is read back
from what has actually been recorded, because grading twelve samples across five criteria is a real
sitting that has to survive being interrupted (Subjective Goals §5.5).

**Three refusals, and they are different things.** A set with no variance has no agreement to
measure and is refused outright. A set concentrated at one end is measurable but unreliable, and is
*warned* about before anything is computed. Too few grades is ``CALIBRATION_INSUFFICIENT`` — the
author has not done the work — which is not the same as having done it and learned the rubric is
not measurable.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from baseaicore import ValidationError, sha256_of, utc_now

from freeweight.domain.agreement import (
    AgreementResult,
    agreement,
    band_for,
    concentrated_grades,
    has_variance,
)
from freeweight.domain.calibration import (
    POLICY_VERSION,
    CalibrationState,
    GateVerdict,
    partition_samples,
    verdict_for,
)
from freeweight.domain.goals.pack import Rung
from freeweight.domain.jury import JuryAssembly
from freeweight.domain.scorers.judged import inter_juror_agreement
from freeweight.infrastructure.db.repositories.calibration import (
    CalibrationGradeRepository,
    CalibrationReportRepository,
    CalibrationSampleRepository,
)
from freeweight.infrastructure.db.repositories.goals import GoalRepository
from freeweight.services.jury import AnchorExemplar

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from baseaicore import Clock

    from freeweight.domain.benchmark import BenchmarkCase
    from freeweight.domain.goals.pack import Criterion, GoalPack
    from freeweight.domain.scorers.judged import JudgedCriterionResult
    from freeweight.services.database import Database
    from freeweight.services.goals import LoadedGoal

__all__ = [
    "ANCHOR",
    "HOLDOUT",
    "CalibrationInsufficient",
    "CalibrationJury",
    "CalibrationOutcome",
    "CriterionAgreement",
    "Disagreement",
    "GradeSubmission",
    "GradingProgress",
    "add_samples",
    "anchors_for",
    "anchors_for_slug",
    "grading_progress",
    "latest_outcome",
    "measure_agreement",
    "record_grades",
    "run_calibration",
    "validity_factor_for_slug",
    "HumanCriterion",
    "RunGradeSubmission",
    "RunGradingSample",
    "RunGradingView",
    "RunNotGradeable",
    "record_run_grades",
    "run_grading_view",
]

ANCHOR = "anchor"
HOLDOUT = "holdout"

_DIAGNOSTIC_SAMPLES = 3
"""How many worst-diverging holdout samples the diagnostics quote back (Subjective Goals §5.6)."""

_EXCERPT_CHARACTERS = 400
"""How much of a diverging sample is quoted in the diagnostics.

Longer than a scorer's 200-character evidence cap and deliberately so: this text is the author's
*own* calibration sample, which is stored in full by design (data model, ``calibration_samples``),
and the point of the diagnostic is that they can recognise it."""


class CalibrationInsufficient(ValidationError):
    """Fewer graded samples than ``calibration.min_samples``.

    Distinct from a failed gate in code, in the API and in the UI copy: this means the author has
    not yet done the work, and the remedy is to grade more samples. A failed gate means they did
    the work and the rubric turned out not to be measurable, and the remedy is to rewrite it.

    Attributes:
        code: ``"CALIBRATION_INSUFFICIENT"``, the stable code spec §13 names.
    """

    code: ClassVar[str] = "CALIBRATION_INSUFFICIENT"


@runtime_checkable
class CalibrationJury(Protocol):
    """What :func:`run_calibration` needs of a jury, and nothing more.

    A protocol rather than :class:`~freeweight.services.jury.JuryService` itself, because the
    phase's own test list asks for "a deterministic fake jury whose bias is configurable" — which
    is how a generous juror and a position-biased juror become test cases rather than field
    reports. Naming the seam here is what makes such a double a first-class citizen instead of a
    cast.
    """

    @property
    def assembly(self) -> JuryAssembly:
        """The jury that was assembled, refusals included."""
        ...

    def with_anchors(self, anchors: Mapping[str, tuple[AnchorExemplar, ...]]) -> CalibrationJury:
        """Return a copy bound to the exemplars the caller's own partition produced."""
        ...

    def grade_all(
        self, criteria: Sequence[Criterion], response_text: str, case: BenchmarkCase
    ) -> list[JudgedCriterionResult]:
        """Grade every criterion for one sample, keeping every verdict."""
        ...

    def judge_prompt_reference(self) -> dict[str, str]:
        """The judge prompt's id, version and hash, for the ``judge_set``."""
        ...


@dataclass(frozen=True, slots=True)
class GradeSubmission:
    """One grade the author is recording.

    Attributes:
        sample_id: The calibration sample.
        criterion_key: Which criterion this grade is on.
        grade: The grade, on that criterion's own scale.
        note: The author's own words about why. What the disagreement diagnostics quote back.
    """

    sample_id: str
    criterion_key: str
    grade: int
    note: str = ""


@dataclass(frozen=True, slots=True)
class GradingProgress:
    """What remains to be graded, so an interrupted sitting can be resumed.

    Attributes:
        samples: How many calibration samples exist.
        judged_criteria: How many criteria need grading.
        expected: ``samples × judged_criteria``.
        recorded: How many grades exist.
        remaining: The ``(sample_id, criterion_key)`` pairs still to do, in a stable order.
        min_samples: The policy minimum.
        target_samples: The policy target.
    """

    samples: int
    judged_criteria: int
    expected: int
    recorded: int
    remaining: tuple[tuple[str, str], ...]
    min_samples: int
    target_samples: int

    @property
    def complete(self) -> bool:
        """Whether every sample has been graded on every judged criterion."""
        return self.expected > 0 and not self.remaining

    def as_json(self) -> dict[str, Any]:
        """Return the progress as the API and the CLI render it."""
        return {
            "samples": self.samples,
            "judged_criteria": self.judged_criteria,
            "expected_grades": self.expected,
            "recorded_grades": self.recorded,
            "remaining": [
                {"sample_id": sample, "criterion": criterion}
                for sample, criterion in self.remaining
            ],
            "complete": self.complete,
            "min_samples": self.min_samples,
            "target_samples": self.target_samples,
        }


@dataclass(frozen=True, slots=True)
class Disagreement:
    """One holdout sample where the jury and the author diverged, with both rationales.

    Attributes:
        sample_id: Which sample.
        criterion_key: Which criterion.
        author_grade: What the author gave it.
        jury_grade: What the jury's median was.
        divergence: The absolute difference, which is what the list is sorted by.
        author_note: The author's own note. The most useful sentence in the whole report.
        jury_rationale: The jury's reason.
        excerpt: A bounded excerpt of the sample, so the author can recognise it.
    """

    sample_id: str
    criterion_key: str
    author_grade: int
    jury_grade: float
    divergence: float
    author_note: str = ""
    jury_rationale: str = ""
    excerpt: str = ""

    def as_json(self) -> dict[str, Any]:
        """Return the diagnostic as the report renders it."""
        return {
            "sample_id": self.sample_id,
            "criterion": self.criterion_key,
            "author_grade": self.author_grade,
            "jury_grade": self.jury_grade,
            "divergence": self.divergence,
            "author_note": self.author_note,
            "jury_rationale": self.jury_rationale,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True, slots=True)
class CriterionAgreement:
    """One judged criterion's measured agreement and everything read beside it.

    Attributes:
        criterion_key: Which criterion.
        weight: Its share of the composite, so a reader can weight the figures as the score did.
        result: The four statistics and the ``n`` they were computed over.
        inter_juror_alpha: Krippendorff's alpha across jurors, or ``None`` for one juror.
        validity: This criterion's contribution to the goal's validity factor.
        band: The interpretation band, so the number arrives with its consequence.
        lint: The lint's read on *why*, when agreement is poor.
        disagreements: The worst-diverging holdout samples for this criterion.
    """

    criterion_key: str
    weight: float
    result: AgreementResult
    inter_juror_alpha: float | None = None
    validity: float = 0.0
    band: str = ""
    lint: str = ""
    disagreements: tuple[Disagreement, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the criterion's agreement as the API, the CLI and the report render it."""
        return {
            "criterion": self.criterion_key,
            "weight": self.weight,
            **self.result.as_json(),
            "inter_juror_alpha": self.inter_juror_alpha,
            "judge_validity_factor": self.validity,
            "band": self.band,
            "lint": self.lint,
            "disagreements": [item.as_json() for item in self.disagreements],
        }


@dataclass(frozen=True, slots=True)
class CalibrationOutcome:
    """One calibration measurement of one goal.

    Attributes:
        goal_slug: Which goal.
        goal_hash: The exact rubric the agreement was measured against.
        verdict: The gate verdict, including the state and the validity factor.
        criteria: Per-criterion agreement, judged criteria only.
        judge_set: The jury the agreement was measured for.
        partition_seed: The seed that produced the split.
        graded_by: Who graded.
        measured_at: When. Ages like evidence.
        warnings: Things the author should know before reading the numbers — a concentrated grade
            distribution, a jury smaller than configured.
    """

    goal_slug: str
    goal_hash: str
    verdict: GateVerdict
    criteria: tuple[CriterionAgreement, ...] = ()
    judge_set: Mapping[str, Any] = field(default_factory=dict)
    partition_seed: int = 0
    graded_by: str = "unknown"
    measured_at: datetime | None = None
    warnings: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the whole outcome as the report endpoint and ``goals report`` render it."""
        from baseaicore.timeutil import to_rfc3339

        return {
            "goal_slug": self.goal_slug,
            "goal_hash": self.goal_hash,
            **self.verdict.as_json(),
            "band": band_for(self.verdict.weighted_kappa_w),
            "criteria": [item.as_json() for item in self.criteria],
            "judge_set": dict(self.judge_set),
            "partition_seed": self.partition_seed,
            "graded_by": self.graded_by,
            "measured_at": to_rfc3339(self.measured_at) if self.measured_at else None,
            "warnings": list(self.warnings),
        }


def add_samples(
    database: Database,
    goal: LoadedGoal,
    *,
    contents: Sequence[Mapping[str, Any]],
    clock: Clock = utc_now,
) -> list[str]:
    """Add candidate outputs for the author to grade.

    Args:
        database: The database handle.
        goal: The loaded goal.
        contents: One entry per sample: ``content`` plus optionally ``origin``
            (``generated`` | ``pasted`` | ``imported_run_sample``), ``goal_task_key``,
            ``model_id`` and ``source_sample_id``.
        clock: Injected for deterministic tests.

    Returns:
        The ids of the samples that were added. A sample whose content is already present is
        **skipped**: two identical samples would be graded twice and counted twice in a figure
        that assumes independent observations.

    Raises:
        GoalNotFound: The goal is not stored. Sync it first — the pack on disk is the source of
            truth, but the grades hang off the projected row.
    """
    from freeweight.services.goals import GoalNotFound

    now = clock()
    with database.write() as session:
        row = GoalRepository().get_by_slug(session, goal.pack.slug)
        if row is None:
            raise GoalNotFound(
                f"Goal {goal.pack.slug!r} is not stored; load it before adding samples.",
                details={"slug": goal.pack.slug},
            )
        tasks = {task.key: task.id for task in GoalRepository().tasks(session, row.id)}
        seen = CalibrationSampleRepository().existing_hashes(session, row.id)
        pending: list[dict[str, Any]] = []
        for entry in contents:
            text = str(entry["content"])
            digest = f"sha256:{sha256_of(text)}"
            if digest in seen:
                continue
            seen.add(digest)
            pending.append(
                {
                    "goal_id": row.id,
                    "goal_task_id": tasks.get(str(entry.get("goal_task_key", ""))),
                    "origin": str(entry.get("origin", "pasted")),
                    "model_id": entry.get("model_id"),
                    "source_sample_id": entry.get("source_sample_id"),
                    "content": text,
                    "content_sha256": digest,
                    # Held out until the seeded partition says otherwise. The default is
                    # deliberately the *safe* half: an unpartitioned sample must never be
                    # renderable as a judge-prompt exemplar, and defaulting to ``anchor`` would
                    # make every newly added sample one until the next calibration run.
                    "partition": HOLDOUT,
                    "partition_seed": goal.pack.calibration.partition_seed,
                    "created_at": now,
                }
            )
        created = CalibrationSampleRepository().insert_many(session, pending)
        return [sample.id for sample in created]


def record_grades(
    database: Database,
    goal: LoadedGoal,
    submissions: Sequence[GradeSubmission],
    *,
    graded_by: str,
    clock: Clock = utc_now,
) -> int:
    """Record the author's grades, replacing any previous grade for the same pair.

    Args:
        database: The database handle.
        goal: The loaded goal.
        submissions: The grades.
        graded_by: Free text the author supplied. Never harvested from the environment (spec §14).
        clock: Injected for deterministic tests.

    Returns:
        How many grades were recorded.

    Raises:
        ValidationError: A submission names a criterion the goal does not have, a criterion that
            is not graded, or a grade outside that criterion's own scale. Each would put a number
            into the ground truth that the agreement mathematics would then treat as real.
        GoalNotFound: The goal is not stored.
    """
    from freeweight.services.goals import GoalNotFound

    now = clock()
    with database.write() as session:
        row = GoalRepository().get_by_slug(session, goal.pack.slug)
        if row is None:
            raise GoalNotFound(
                f"Goal {goal.pack.slug!r} is not stored.", details={"slug": goal.pack.slug}
            )
        criterion_ids = GoalRepository().criterion_ids(session, row.id)
        for submission in submissions:
            criterion = goal.pack.criterion(submission.criterion_key)
            if criterion is None or submission.criterion_key not in criterion_ids:
                raise ValidationError(
                    f"Goal {goal.pack.slug!r} has no criterion {submission.criterion_key!r}.",
                    details={"criterion": submission.criterion_key},
                )
            if criterion.scale is None:
                raise ValidationError(
                    f"Criterion {criterion.key!r} is scored at rung {criterion.rung.value!r} and "
                    "has no scale to grade on.",
                    details={"criterion": criterion.key},
                )
            if not 1 <= submission.grade <= criterion.scale.points:
                raise ValidationError(
                    f"Grade {submission.grade} is outside criterion {criterion.key!r}'s "
                    f"1..{criterion.scale.points} scale.",
                    details={"criterion": criterion.key, "grade": submission.grade},
                )
            CalibrationGradeRepository().upsert(
                session,
                calibration_sample_id=submission.sample_id,
                goal_criterion_id=criterion_ids[submission.criterion_key],
                grade=submission.grade,
                note=submission.note or None,
                graded_by=graded_by,
                graded_at=now,
            )
        return len(submissions)


def _graded_criteria(goal: LoadedGoal) -> tuple[Criterion, ...]:
    """The criteria the author grades: judged ones, plus human ones, graded the same way."""
    return tuple(
        criterion for criterion in goal.pack.criteria if criterion.rung in {Rung.JUDGE, Rung.HUMAN}
    )


def grading_progress(database: Database, goal: LoadedGoal) -> GradingProgress:
    """Return what remains to be graded, so an interrupted sitting can be resumed."""
    criteria = _graded_criteria(goal)
    with database.read() as session:
        row = GoalRepository().get_by_slug(session, goal.pack.slug)
        if row is None:
            return GradingProgress(
                samples=0,
                judged_criteria=len(criteria),
                expected=0,
                recorded=0,
                remaining=(),
                min_samples=goal.pack.calibration.min_samples,
                target_samples=goal.pack.calibration.target_samples,
            )
        samples = CalibrationSampleRepository().list_for_goal(session, row.id)
        criterion_ids = GoalRepository().criterion_ids(session, row.id)
        by_id = {value: key for key, value in criterion_ids.items()}
        grades = CalibrationGradeRepository().list_for_goal(session, row.id)
    recorded = {
        (grade.calibration_sample_id, by_id.get(grade.goal_criterion_id, "")) for grade in grades
    }
    remaining = tuple(
        (sample.id, criterion.key)
        for sample in samples
        for criterion in criteria
        if (sample.id, criterion.key) not in recorded
    )
    return GradingProgress(
        samples=len(samples),
        judged_criteria=len(criteria),
        expected=len(samples) * len(criteria),
        recorded=len(recorded),
        remaining=remaining,
        min_samples=goal.pack.calibration.min_samples,
        target_samples=goal.pack.calibration.target_samples,
    )


def _representative_grade(grades: Sequence[int]) -> int:
    """One grade per sample, for stratifying the partition.

    The median across the sample's per-criterion grades, rounded: the partition has to span the
    scale, and the scale it has to span is "how good is this sample overall".
    """
    return int(round(statistics.median(grades))) if grades else 1


def anchors_for(database: Database, goal: LoadedGoal) -> dict[str, tuple[AnchorExemplar, ...]]:
    """Return the author's *anchor* examples, by criterion key.

    The only function that produces the mapping a jury is built with, and it reads
    ``partition = 'anchor'`` and nothing else. There is no code path from a holdout sample to a
    judge prompt, which is what makes "the holdout is never shown to the jury" a property of the
    program rather than a promise about it.

    Args:
        database: The database handle.
        goal: The loaded goal.

    Returns:
        ``{criterion_key: (exemplar, …)}``, ordered by grade so a juror sees the scale's range.
    """
    with database.read() as session:
        row = GoalRepository().get_by_slug(session, goal.pack.slug)
        if row is None:
            return {}
        anchors = {
            sample.id: sample
            for sample in CalibrationSampleRepository().list_for_goal(
                session, row.id, partition=ANCHOR
            )
        }
        criterion_ids = GoalRepository().criterion_ids(session, row.id)
        by_id = {value: key for key, value in criterion_ids.items()}
        grades = CalibrationGradeRepository().list_for_goal(session, row.id)
    exemplars: dict[str, list[AnchorExemplar]] = {}
    for grade in grades:
        sample = anchors.get(grade.calibration_sample_id)
        if sample is None:
            continue
        key = by_id.get(grade.goal_criterion_id, "")
        exemplars.setdefault(key, []).append(
            AnchorExemplar(content=sample.content, grade=grade.grade, note=grade.note or "")
        )
    return {
        key: tuple(sorted(items, key=lambda item: -item.grade)) for key, items in exemplars.items()
    }


def _lint_for(result: AgreementResult, criterion: Criterion) -> str:
    """The lint's read on why agreement is poor (Subjective Goals §5.6).

    Names the problem and shows the evidence; it never proposes replacement text. A model that
    reworded the author's taste until it became measurable would be optimizing the target into the
    instrument, and the resulting number would measure nothing (ADR-0031 §3).
    """
    if result.kappa_w is None:
        return (
            "Agreement could not be computed: your grades for this criterion do not vary, so "
            "there is nothing for the jury to agree or disagree about. Grade some weaker examples."
        )
    if result.kappa_w >= 0.6:  # noqa: PLR2004 — §5.5's own band boundary
        return ""
    if abs(result.bias) >= 1.0:
        direction = "more generously than" if result.bias > 0 else "harsher than"
        return (
            f"The jury grades {direction} you by {abs(result.bias):.1f} points on average. That "
            "is a systematic offset rather than noise, which usually means your descriptors "
            "describe a different level from the one you actually grade at."
        )
    if result.rho is not None and result.rho >= 0.6:  # noqa: PLR2004 — §5.5's own band boundary
        return (
            "The jury ranks these samples much as you do but does not land on the same grades. "
            "That is usually one criterion carrying two qualities: splitting it often fixes both."
        )
    described = 0 if criterion.scale is None else len(criterion.scale.descriptors)
    points = 0 if criterion.scale is None else criterion.scale.points
    if points and described * 2 < points:
        return (
            f"Only {described} of this criterion's {points} scale points carry a descriptor. A "
            "juror grading an undescribed point is guessing at what you meant by it."
        )
    return (
        "The jury does not rank these samples as you do. Most often this criterion describes a "
        "topic rather than a quality, or it is a quality a rule could check outright — run "
        "`freeweight goals suggest-rules` and see."
    )


def measure_agreement(  # noqa: PLR0913 — an agreement figure is a function of exactly these
    goal: LoadedGoal,
    *,
    author_grades: Mapping[str, Mapping[str, int]],
    jury_grades: Mapping[str, Mapping[str, float]],
    jury_rationales: Mapping[str, Mapping[str, str]] | None = None,
    author_notes: Mapping[str, Mapping[str, str]] | None = None,
    excerpts: Mapping[str, str] | None = None,
    alphas: Mapping[str, float | None] | None = None,
    n_holdout_target: int = 10,
) -> tuple[CriterionAgreement, ...]:
    """Compute per-criterion agreement between the author and the jury on the holdout.

    Args:
        goal: The loaded goal.
        author_grades: ``{sample_id: {criterion_key: grade}}`` — the held-out grades.
        jury_grades: ``{sample_id: {criterion_key: median}}`` — what the jury said about the same
            samples, having never seen them before.
        jury_rationales: The jury's reasons, for the diagnostics.
        author_notes: The author's own notes, which are what the diagnostics quote back.
        excerpts: A bounded excerpt of each sample, so the author can recognise it.
        alphas: Inter-juror agreement per criterion.
        n_holdout_target: The shrinkage denominator.

    Returns:
        One entry per judged criterion that had at least one paired grade, in the goal's
        declaration order. A criterion with none is omitted — there is nothing to report about it,
        and reporting a zero would be a claim about a measurement nobody took.
    """
    from freeweight.domain.calibration import criterion_validity

    results: list[CriterionAgreement] = []
    for criterion in goal.pack.judged_criteria:
        if criterion.scale is None:
            continue
        pairs = [
            (sample_id, author_grades[sample_id][criterion.key], jury[criterion.key])
            for sample_id, jury in sorted(jury_grades.items())
            if criterion.key in jury and criterion.key in author_grades.get(sample_id, {})
        ]
        if not pairs:
            continue
        author = [grade for _sample, grade, _jury in pairs]
        jury_medians = [int(round(value)) for _sample, _grade, value in pairs]
        result = agreement(author, jury_medians, scale_points=criterion.scale.points)
        divergences = sorted(
            (
                Disagreement(
                    sample_id=sample_id,
                    criterion_key=criterion.key,
                    author_grade=grade,
                    jury_grade=value,
                    divergence=abs(value - grade),
                    author_note=(author_notes or {}).get(sample_id, {}).get(criterion.key, ""),
                    jury_rationale=(jury_rationales or {})
                    .get(sample_id, {})
                    .get(criterion.key, ""),
                    excerpt=(excerpts or {}).get(sample_id, "")[:_EXCERPT_CHARACTERS],
                )
                for sample_id, grade, value in pairs
            ),
            key=lambda item: (-item.divergence, item.sample_id),
        )
        results.append(
            CriterionAgreement(
                criterion_key=criterion.key,
                weight=criterion.weight,
                result=result,
                inter_juror_alpha=(alphas or {}).get(criterion.key),
                validity=criterion_validity(
                    result.kappa_w, n_holdout=result.n, n_holdout_target=n_holdout_target
                ),
                band=band_for(result.kappa_w),
                lint=_lint_for(result, criterion),
                disagreements=tuple(
                    item for item in divergences[:_DIAGNOSTIC_SAMPLES] if item.divergence > 0
                ),
            )
        )
    return tuple(results)


def run_calibration(  # noqa: PLR0913 — a calibration run needs all of its collaborators
    database: Database,
    goal: LoadedGoal,
    *,
    jury: CalibrationJury,
    n_holdout_target: int = 10,
    graded_by: str = "unknown",
    clock: Clock = utc_now,
) -> CalibrationOutcome:
    """Partition the graded samples, score the holdout with the jury, and gate.

    In order: read the grades, refuse a set with no variance, warn about one bunched at an end,
    compute the seeded stratified partition and *record* it, score the holdout with the jury the
    goal will actually run with, measure agreement, and write the report.

    Args:
        database: The database handle.
        goal: The loaded goal.
        jury: The assembled jury — the exact configuration the goal will run with, because
            agreement measured against a different instrument describes a different instrument.
        n_holdout_target: The shrinkage denominator.
        graded_by: Who graded, recorded on the report.
        clock: Injected for deterministic tests.

    Returns:
        The outcome, already persisted.

    Raises:
        CalibrationInsufficient: Fewer than ``calibration.min_samples`` graded samples.
        ValidationError: The author's grades for every judged criterion are identical, so there is
            no variance to measure agreement over.
        GoalNotFound: The goal is not stored.
    """
    from freeweight.services.goals import GoalNotFound

    now = clock()
    policy = goal.pack.calibration
    if not goal.pack.judged_criteria:
        # Nothing to calibrate, so nothing can fail to calibrate — and nothing needs grading
        # either. Returning ``NOT_REQUIRED`` rather than raising ``CALIBRATION_INSUFFICIENT`` is
        # the difference between "you have work to do" and "you have none".
        return CalibrationOutcome(
            goal_slug=goal.pack.slug,
            goal_hash=goal.goal_hash,
            verdict=verdict_for(
                weights={criterion.key: criterion.weight for criterion in goal.pack.criteria},
                judged_kappa={},
                n_holdout={},
                graded_samples=0,
                min_samples=policy.min_samples,
                min_agreement=policy.min_agreement,
            ),
            graded_by=graded_by,
            measured_at=now,
        )
    with database.read() as session:
        row = GoalRepository().get_by_slug(session, goal.pack.slug)
        if row is None:
            raise GoalNotFound(
                f"Goal {goal.pack.slug!r} is not stored.", details={"slug": goal.pack.slug}
            )
        goal_id = row.id
        samples = {
            sample.id: sample
            for sample in CalibrationSampleRepository().list_for_goal(session, goal_id)
        }
        criterion_ids = GoalRepository().criterion_ids(session, goal_id)
        by_id = {value: key for key, value in criterion_ids.items()}
        grade_rows = CalibrationGradeRepository().list_for_goal(session, goal_id)

    author: dict[str, dict[str, int]] = {}
    notes: dict[str, dict[str, str]] = {}
    for grade in grade_rows:
        key = by_id.get(grade.goal_criterion_id, "")
        author.setdefault(grade.calibration_sample_id, {})[key] = grade.grade
        notes.setdefault(grade.calibration_sample_id, {})[key] = grade.note or ""
    graded_sample_ids = sorted(author)
    if len(graded_sample_ids) < policy.min_samples:
        raise CalibrationInsufficient(
            f"Goal {goal.pack.slug!r} has {len(graded_sample_ids)} graded samples; "
            f"{policy.min_samples} are needed. That is work still to do, not a rubric that failed "
            "to measure.",
            details={
                "slug": goal.pack.slug,
                "graded": len(graded_sample_ids),
                "min_samples": policy.min_samples,
                "remaining": policy.min_samples - len(graded_sample_ids),
            },
        )

    warnings: list[str] = []
    for criterion in goal.pack.judged_criteria:
        values = [
            author[sample_id][criterion.key]
            for sample_id in graded_sample_ids
            if criterion.key in author[sample_id]
        ]
        if values and not has_variance(values):
            raise ValidationError(
                f"Every grade you gave for {criterion.key!r} is the same. Agreement is a "
                "chance-corrected statistic: with no variance there is nothing to agree about, "
                "and any coefficient computed here would be a division nobody should perform. "
                "Grade some samples you feel differently about.",
                details={"slug": goal.pack.slug, "criterion": criterion.key},
            )
        if (
            values
            and criterion.scale is not None
            and concentrated_grades(values, scale_points=criterion.scale.points)
        ):
            warnings.append(
                f"You graded most of the set at one end of {criterion.key!r}'s scale. Agreement "
                "measured on this set will be unreliable. Add some weaker examples."
            )

    representative = {
        sample_id: _representative_grade(list(author[sample_id].values()))
        for sample_id in graded_sample_ids
    }
    partition = partition_samples(
        representative, holdout_fraction=policy.holdout_fraction, seed=policy.partition_seed
    )
    with database.write() as session:
        repository = CalibrationSampleRepository()
        for sample_id in graded_sample_ids:
            repository.set_partition(
                session,
                sample_id=sample_id,
                partition=partition.label(sample_id),
                seed=policy.partition_seed,
            )

    # Rebound to the anchors this partition just produced, never to whatever the caller happened
    # to pass. The holdout is defined by the split computed three lines above, so the exemplars
    # have to come from the same computation or the two can disagree — and the way they disagree
    # is by showing the jury a sample it is about to be measured on.
    jury = jury.with_anchors(
        _exemplars_from(partition.anchors, samples=samples, author=author, notes=notes)
    )

    jury_grades: dict[str, dict[str, float]] = {}
    rationales: dict[str, dict[str, str]] = {}
    excerpts: dict[str, str] = {}
    results_by_criterion: dict[str, list[Any]] = {}
    for sample_id in partition.holdout:
        sample = samples[sample_id]
        excerpts[sample_id] = sample.content[:_EXCERPT_CHARACTERS]
        case = _calibration_case(goal, sample_id)
        for result in jury.grade_all(list(goal.pack.judged_criteria), sample.content, case):
            key = result.outcome.criterion_key
            results_by_criterion.setdefault(key, []).append(result)
            if result.median_grade is None:
                continue
            jury_grades.setdefault(sample_id, {})[key] = result.median_grade
            reasons = [verdict.rationale for verdict in result.verdicts if verdict.rationale]
            if reasons:
                rationales.setdefault(sample_id, {})[key] = reasons[0]

    alphas = {key: inter_juror_agreement(results) for key, results in results_by_criterion.items()}
    criteria = measure_agreement(
        goal,
        author_grades=author,
        jury_grades=jury_grades,
        jury_rationales=rationales,
        author_notes=notes,
        excerpts=excerpts,
        alphas=alphas,
        n_holdout_target=n_holdout_target,
    )
    # Every judged criterion the goal *declares*, not only the ones that produced a coefficient.
    # A criterion the jury could not grade at all has ``kappa_w = None``, which is excluded from
    # the weighted figure and scores zero validity — and, crucially, keeps the goal in
    # ``uncalibrated`` rather than letting an empty mapping read as "nothing needed calibrating".
    measured = {item.criterion_key: item for item in criteria}
    judged_kappa: dict[str, float | None] = {
        criterion.key: (
            measured[criterion.key].result.kappa_w if criterion.key in measured else None
        )
        for criterion in goal.pack.judged_criteria
    }
    holdout_sizes = {
        criterion.key: (measured[criterion.key].result.n if criterion.key in measured else 0)
        for criterion in goal.pack.judged_criteria
    }
    verdict = verdict_for(
        weights={criterion.key: criterion.weight for criterion in goal.pack.criteria},
        judged_kappa=judged_kappa,
        n_holdout=holdout_sizes,
        graded_samples=len(graded_sample_ids),
        min_samples=policy.min_samples,
        min_agreement=policy.min_agreement,
        n_anchor=len(partition.anchors),
        n_holdout_target=n_holdout_target,
    )
    if jury.assembly.reduced:
        warnings.append(
            f"The jury is {len(jury.assembly.jurors)} model(s), not the "
            f"{jury.assembly.requested_size} this goal asks for. Inter-juror agreement is weaker "
            "or absent, and the result says so."
        )
    judge_set = _judge_set(jury)
    outcome = CalibrationOutcome(
        goal_slug=goal.pack.slug,
        goal_hash=goal.goal_hash,
        verdict=verdict,
        criteria=criteria,
        judge_set=judge_set,
        partition_seed=policy.partition_seed,
        graded_by=graded_by,
        measured_at=now,
        warnings=tuple(warnings),
    )
    _persist(database, goal_id=goal_id, criterion_ids=criterion_ids, outcome=outcome)
    return outcome


def _exemplars_from(
    anchor_ids: Sequence[str],
    *,
    samples: Mapping[str, Any],
    author: Mapping[str, Mapping[str, int]],
    notes: Mapping[str, Mapping[str, str]],
) -> dict[str, tuple[AnchorExemplar, ...]]:
    """Build the judge prompt's exemplars from the anchor half of one partition.

    Takes the anchor ids as an argument rather than reading a stored label, so the exemplars and
    the holdout are two halves of *one* computation and cannot drift apart.
    """
    exemplars: dict[str, list[AnchorExemplar]] = {}
    for sample_id in anchor_ids:
        sample = samples.get(sample_id)
        if sample is None:  # pragma: no cover — the ids come from these samples
            continue
        for criterion_key, grade in author.get(sample_id, {}).items():
            exemplars.setdefault(criterion_key, []).append(
                AnchorExemplar(
                    content=sample.content,
                    grade=grade,
                    note=notes.get(sample_id, {}).get(criterion_key, ""),
                )
            )
    return {
        key: tuple(sorted(items, key=lambda item: -item.grade)) for key, items in exemplars.items()
    }


def _judge_set(jury: CalibrationJury) -> dict[str, Any]:
    """Return the jury's identity, prompt included — a hard-separation input (ADR-0032 §4)."""
    from freeweight.domain.jury import judge_set_identity

    reference = jury.judge_prompt_reference()
    identity = judge_set_identity(
        jury.assembly,
        prompt_id=reference["prompt_id"],
        prompt_version=reference["prompt_version"],
        prompt_sha256=reference["prompt_sha256"],
    )
    return {**identity, **jury.assembly.as_json()}


def _calibration_case(goal: LoadedGoal, sample_id: str) -> BenchmarkCase:
    """Build the minimal case a jury needs to grade one calibration sample.

    The jury is given the *task* the writer was given, so it grades the answer against what was
    asked rather than against nothing. Where a goal has several tasks the first is used: a
    calibration sample records which task produced it, and a sample pasted in by hand records
    none, so the task shown is the goal's own opening question either way.
    """
    from freeweight.domain.benchmark import BenchmarkCase

    task_text = goal.pack.tasks[0].prompt_text if goal.pack.tasks else ""
    return BenchmarkCase(
        case_id=sample_id,
        ordinal=0,
        prompt=task_text,
        metadata={"task_text": task_text, "goal": goal.pack.slug},
    )


def _persist(
    database: Database,
    *,
    goal_id: str,
    criterion_ids: Mapping[str, str],
    outcome: CalibrationOutcome,
) -> None:
    """Write the calibration report: one row per criterion, plus the goal-level row."""
    rows: list[dict[str, Any]] = [
        {
            "goal_criterion_id": None,
            "goal_hash": outcome.goal_hash,
            "judge_set_json": dict(outcome.judge_set),
            "kappa_w": outcome.verdict.weighted_kappa_w,
            "rho": None,
            "mae": None,
            "bias": None,
            "n_anchor": outcome.verdict.n_anchor,
            "n_holdout": outcome.verdict.n_holdout,
            "inter_juror_alpha": None,
            "passed_gate": outcome.verdict.passed,
            "min_agreement": outcome.verdict.min_agreement,
            "judge_validity_factor": outcome.verdict.judge_validity_factor,
            "disagreement_json": {"warnings": list(outcome.warnings)},
            "graded_by": outcome.graded_by,
            "measured_at": outcome.measured_at,
            "policy_version": POLICY_VERSION,
        }
    ]
    rows.extend(
        {
            "goal_criterion_id": criterion_ids.get(item.criterion_key),
            "goal_hash": outcome.goal_hash,
            "judge_set_json": dict(outcome.judge_set),
            "kappa_w": item.result.kappa_w,
            "rho": item.result.rho,
            "mae": item.result.mae,
            "bias": item.result.bias,
            "n_anchor": outcome.verdict.n_anchor,
            "n_holdout": item.result.n,
            "inter_juror_alpha": item.inter_juror_alpha,
            "passed_gate": outcome.verdict.passed,
            "min_agreement": outcome.verdict.min_agreement,
            "judge_validity_factor": item.validity,
            "disagreement_json": {
                "band": item.band,
                "lint": item.lint,
                "samples": [entry.as_json() for entry in item.disagreements],
            },
            "graded_by": outcome.graded_by,
            "measured_at": outcome.measured_at,
            "policy_version": POLICY_VERSION,
        }
        for item in outcome.criteria
    )
    with database.write() as session:
        CalibrationReportRepository().replace_for_goal(session, goal_id, rows)


def latest_outcome(database: Database, goal: LoadedGoal) -> CalibrationOutcome | None:
    """Read back the stored calibration report for one goal.

    Args:
        database: The database handle.
        goal: The loaded goal.

    Returns:
        The outcome, or ``None`` when this goal has never been calibrated. A goal whose rubric has
        changed since it was calibrated still returns its stored report — with the ``goal_hash``
        it was measured against — because a stale report is information and hiding it would leave
        the reader with nothing.
    """
    with database.read() as session:
        row = GoalRepository().get_by_slug(session, goal.pack.slug)
        if row is None:
            return None
        reports = CalibrationReportRepository().list_for_goal(session, row.id)
        criterion_ids = GoalRepository().criterion_ids(session, row.id)
    if not reports:
        return None
    by_id = {value: key for key, value in criterion_ids.items()}
    goal_level = next((report for report in reports if report.goal_criterion_id is None), None)
    if goal_level is None:  # pragma: no cover — written together, read together
        return None
    weights = {criterion.key: criterion.weight for criterion in goal.pack.criteria}
    criteria: list[CriterionAgreement] = []
    for report in reports:
        if report.goal_criterion_id is None:
            continue
        key = by_id.get(report.goal_criterion_id, "")
        detail = _mapping(report.disagreement_json)
        criteria.append(
            CriterionAgreement(
                criterion_key=key,
                weight=weights.get(key, 0.0),
                result=AgreementResult(
                    kappa_w=report.kappa_w,
                    rho=report.rho,
                    mae=report.mae or 0.0,
                    bias=report.bias or 0.0,
                    n=report.n_holdout,
                    scale_points=_scale_points(goal, key),
                ),
                inter_juror_alpha=report.inter_juror_alpha,
                validity=report.judge_validity_factor,
                band=str(detail.get("band", "")),
                lint=str(detail.get("lint", "")),
                disagreements=tuple(
                    Disagreement(
                        sample_id=str(entry.get("sample_id", "")),
                        criterion_key=key,
                        author_grade=int(entry.get("author_grade", 0)),
                        jury_grade=float(entry.get("jury_grade", 0.0)),
                        divergence=float(entry.get("divergence", 0.0)),
                        author_note=str(entry.get("author_note", "")),
                        jury_rationale=str(entry.get("jury_rationale", "")),
                        excerpt=str(entry.get("excerpt", "")),
                    )
                    for entry in detail.get("samples", ())
                ),
            )
        )
    state = (
        CalibrationState.NOT_REQUIRED
        if not goal.pack.judged_criteria
        else CalibrationState.CALIBRATED
        if goal_level.passed_gate
        else CalibrationState.UNCALIBRATED
    )
    verdict = GateVerdict(
        state=state,
        weighted_kappa_w=goal_level.kappa_w,
        min_agreement=goal_level.min_agreement,
        judge_validity_factor=goal_level.judge_validity_factor,
        n_holdout=goal_level.n_holdout,
        n_anchor=goal_level.n_anchor,
        graded_samples=0,
        min_samples=goal.pack.calibration.min_samples,
        policy_version=goal_level.policy_version,
    )
    warnings = tuple(
        str(item) for item in _mapping(goal_level.disagreement_json).get("warnings", ())
    )
    return CalibrationOutcome(
        goal_slug=goal.pack.slug,
        goal_hash=goal_level.goal_hash,
        verdict=verdict,
        criteria=tuple(criteria),
        judge_set=_mapping(goal_level.judge_set_json),
        partition_seed=goal.pack.calibration.partition_seed,
        graded_by=goal_level.graded_by or "unknown",
        measured_at=goal_level.measured_at,
        warnings=warnings,
    )


def _mapping(value: object) -> dict[str, Any]:
    """Read a ``PortableJSON`` column back as a mapping, tolerating ``NULL``.

    The column is typed ``object`` on the ORM model, and a stored report written by an older
    build could legitimately hold something other than an object. A report that cannot be read
    back is worth less than an empty one, so this narrows rather than raising.
    """
    return dict(value) if isinstance(value, dict) else {}


def anchors_for_slug(database: Database, pack: GoalPack) -> dict[str, tuple[AnchorExemplar, ...]]:
    """Return the anchor exemplars for one goal, keyed by criterion, from its pack alone.

    The run engine holds the goal's :class:`~freeweight.domain.goals.pack.GoalPack` (it is on the
    scorer) but not the :class:`~freeweight.services.goals.LoadedGoal` around it, and re-reading
    the pack from disk mid-run would let a file edited during a run change what the jury was
    shown. This reads the *database* projection instead, which is what the run was prepared
    against.

    Reads ``partition = 'anchor'`` and nothing else — see :func:`anchors_for`.
    """
    with database.read() as session:
        row = GoalRepository().get_by_slug(session, pack.slug)
        if row is None:
            return {}
        anchors = {
            sample.id: sample
            for sample in CalibrationSampleRepository().list_for_goal(
                session, row.id, partition=ANCHOR
            )
        }
        by_id = {
            value: key for key, value in GoalRepository().criterion_ids(session, row.id).items()
        }
        grades = CalibrationGradeRepository().list_for_goal(session, row.id)
    exemplars: dict[str, list[AnchorExemplar]] = {}
    for grade in grades:
        sample = anchors.get(grade.calibration_sample_id)
        if sample is None:
            continue
        exemplars.setdefault(by_id.get(grade.goal_criterion_id, ""), []).append(
            AnchorExemplar(content=sample.content, grade=grade.grade, note=grade.note or "")
        )
    return {
        key: tuple(sorted(items, key=lambda item: -item.grade)) for key, items in exemplars.items()
    }


def validity_factor_for_slug(database: Database, pack: GoalPack) -> float:
    """Return one goal's stored ``judge_validity_factor``, or ``1.0`` when it has none.

    ``1.0`` for an uncalibrated goal is deliberate and safe: an uncalibrated goal emits **no
    evidence at all** (ADR-0032 §3), so the factor never multiplies into a confidence anybody
    reads. Reporting it as 1.0 on the run keeps the number meaning "this is what would multiply
    in" rather than encoding the gate twice.
    """
    with database.read() as session:
        row = GoalRepository().get_by_slug(session, pack.slug)
        if row is None:
            return 1.0
        report = CalibrationReportRepository().goal_level(session, row.id)
    return report.judge_validity_factor if report is not None else 1.0


def _scale_points(goal: LoadedGoal, criterion_key: str) -> int:
    """Return one criterion's scale size, or 5 when it has none."""
    criterion = goal.pack.criterion(criterion_key)
    return criterion.scale.points if criterion is not None and criterion.scale else 5


# ---------------------------------------------------------------------------------------------
# Rung-4 (`human`) grading over an ordinary run's samples (Phase 11)
#
# Subjective Goals §3.3: a `human` criterion queues the sample for the user to grade in a blinded
# UI, recorded with `score_method = "human"`, validity 1.0 by definition. The grading *machinery*
# above already exists for calibration samples; this is the second entry point, over a completed
# run's samples rather than a calibration set. It lands here because this is where a human grade
# first has somewhere to go — evidence — and because one grading vocabulary (blinded, shuffled,
# saved on submit, upserted by (sample, criterion)) is one fewer thing for the two screens to
# disagree about.
# ---------------------------------------------------------------------------------------------


class RunNotGradeable(ValidationError):
    """This run's samples cannot be graded by hand.

    Raised when the run is not a completed goal run, when its goal declares no ``human``
    criterion, or when the goal's rubric has changed since the run — a grade against a different
    rubric would be attributed to a measurement it was never part of.
    """

    code: ClassVar[str] = "RUN_NOT_GRADEABLE"


@dataclass(frozen=True, slots=True)
class RunGradeSubmission:
    """One grade for one of a run's samples on one ``human`` criterion.

    Attributes:
        sample_id: The run's sample.
        criterion_key: Which human criterion.
        grade: The grade on that criterion's own scale.
        note: The grader's own words, kept beside the grade.
    """

    sample_id: str
    criterion_key: str
    grade: int
    note: str = ""


@dataclass(frozen=True, slots=True)
class HumanCriterion:
    """One rung-4 criterion as the grading screen presents it.

    Attributes:
        key: The criterion key.
        name: Its display name.
        weight: Its share of the composite.
        scale_points: The ordinal scale's size.
        descriptors: What the scale points mean, by point.
    """

    key: str
    name: str
    weight: float
    scale_points: int
    descriptors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunGradingSample:
    """One completed sample of the run, blinded: the text and the grades it has, nothing else.

    Attributes:
        sample_id: The sample.
        case_id: The task it answered, so a grader can see what was asked.
        response_text: What the model wrote. Always stored for a goal run (spec §12).
        grades: ``{criterion_key: {"grade": int, "note": str}}`` for the grades recorded so far.
    """

    sample_id: str
    case_id: str
    response_text: str
    grades: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunGradingView:
    """Everything the grading screen and ``goals grade --run`` need, and nothing that unblinds.

    The model that produced the run is deliberately **not** read: blinding is enforced by not
    fetching the identity rather than by not rendering it, so a template change cannot leak it.

    Attributes:
        run_id: The run.
        goal_slug: The goal.
        goal_name: Its display name.
        criteria: The human criteria to grade on.
        samples: The completed samples, in a seeded order that is not the order they were produced
            in and is stable across reloads.
        expected: ``samples × criteria``.
        recorded: How many grades exist.
    """

    run_id: str
    goal_slug: str
    goal_name: str
    criteria: tuple[HumanCriterion, ...]
    samples: tuple[RunGradingSample, ...]
    expected: int
    recorded: int

    @property
    def complete(self) -> bool:
        """Whether every sample has been graded on every human criterion."""
        return self.expected > 0 and self.recorded >= self.expected

    def as_json(self) -> dict[str, Any]:
        """Return the view as ``goals grade --run --json`` prints it."""
        return {
            "run_id": self.run_id,
            "goal_slug": self.goal_slug,
            "criteria": [
                {"key": c.key, "name": c.name, "scale_points": c.scale_points}
                for c in self.criteria
            ],
            "samples": [
                {"sample_id": s.sample_id, "case_id": s.case_id, "grades": dict(s.grades)}
                for s in self.samples
            ],
            "expected_grades": self.expected,
            "recorded_grades": self.recorded,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class _RunGoal:
    """The rows a run's grading reads and writes through."""

    run: Any
    suite: Any
    goal: Any
    criteria: tuple[Any, ...]
    human: tuple[HumanCriterion, ...]
    criterion_ids: Mapping[str, str]


def _run_goal(session: Any, run_id: str) -> _RunGoal:  # noqa: ANN401 — a Session
    """Load the run, its goal suite and the goal's criteria, refusing what cannot be graded."""
    from freeweight.infrastructure.db.models_goals import Goal
    from freeweight.infrastructure.db.models_runs import BenchmarkSuite
    from freeweight.infrastructure.db.repositories.runs import RunRepository
    from freeweight.services.runs import RunNotFound

    run = RunRepository().get_by_id(session, run_id)
    if run is None:
        raise RunNotFound(f"No run matches {run_id!r}.", details={"run": run_id})
    suite = session.get(BenchmarkSuite, run.suite_id)
    if suite is None or suite.runner != "goal" or not suite.goal_id:
        raise RunNotGradeable(
            f"Run {run_id!r} is not a goal run; only a goal's human criteria are graded by hand.",
            details={"run": run_id},
        )
    if run.status != "completed":
        raise RunNotGradeable(
            f"Run {run_id!r} is {run.status!r}; its samples are graded once it has completed.",
            details={"run": run_id, "status": run.status},
        )
    goal = session.get(Goal, suite.goal_id)
    if goal is None:
        raise RunNotGradeable(
            f"Run {run_id!r} measured a goal that is no longer stored.", details={"run": run_id}
        )
    if suite.goal_hash and goal.goal_hash != suite.goal_hash:
        raise RunNotGradeable(
            f"Goal {goal.slug!r} has changed since run {run_id!r} measured it (rubric "
            f"{str(suite.goal_hash)[:16]} then, {str(goal.goal_hash)[:16]} now). A grade against "
            "the current rubric would be attributed to a measurement it was never part of; "
            "re-run the goal and grade that run.",
            details={"run": run_id, "goal": goal.slug},
        )
    criteria = tuple(GoalRepository().criteria(session, goal.id))
    human = tuple(
        HumanCriterion(
            key=str(row.key),
            name=str(row.name),
            weight=float(row.weight),
            scale_points=int(row.scale_points or 5),
            descriptors=(
                {str(k): str(v) for k, v in dict(row.scale_descriptors_json).items()}
                if isinstance(row.scale_descriptors_json, dict)
                else {}
            ),
        )
        for row in criteria
        if row.rung == Rung.HUMAN.value
    )
    if not human:
        raise RunNotGradeable(
            f"Goal {goal.slug!r} declares no human criterion; there is nothing to grade by hand.",
            details={"run": run_id, "goal": goal.slug},
        )
    return _RunGoal(
        run=run,
        suite=suite,
        goal=goal,
        criteria=criteria,
        human=human,
        criterion_ids=GoalRepository().criterion_ids(session, goal.id),
    )


def _run_samples(session: Any, run_id: str) -> list[Any]:  # noqa: ANN401 — a Session
    """The run's completed samples with stored text, in declaration order."""
    from sqlalchemy import select

    from freeweight.infrastructure.db.models_runs import RunTest, Sample

    return list(
        session.scalars(
            select(Sample)
            .join(RunTest, RunTest.id == Sample.run_test_id)
            .where(
                RunTest.run_id == run_id,
                Sample.status == "completed",
                Sample.response_text.is_not(None),
            )
            .order_by(Sample.run_test_id.asc(), Sample.ordinal.asc(), Sample.repetition.asc())
        ).all()
    )


def run_grading_view(database: Database, run_id: str) -> RunGradingView:
    """Read what the grading screen shows for one run: blinded, shuffled, with progress.

    Args:
        database: The database handle.
        run_id: The completed goal run.

    Returns:
        The view.

    Raises:
        RunNotFound: No run has this id.
        RunNotGradeable: The run is not a completed goal run with human criteria, or its goal's
            rubric has changed since.
    """
    from freeweight.domain.judging import randomized_order
    from freeweight.infrastructure.db.repositories.goals import CriterionScoreRepository

    with database.read() as session:
        context = _run_goal(session, run_id)
        human_keys = {criterion.key for criterion in context.human}
        samples: list[RunGradingSample] = []
        recorded = 0
        for sample in _run_samples(session, run_id):
            grades: dict[str, dict[str, Any]] = {}
            for score in CriterionScoreRepository().list_for_sample(session, sample.id):
                if score.criterion_key not in human_keys or score.status != "scored":
                    continue
                detail = dict(score.detail_json) if isinstance(score.detail_json, dict) else {}
                grades[str(score.criterion_key)] = {
                    "grade": int(detail.get("human_grade", 0)),
                    "note": str(detail.get("note", "")),
                }
                recorded += 1
            samples.append(
                RunGradingSample(
                    sample_id=sample.id,
                    case_id=str(sample.case_id),
                    response_text=str(sample.response_text),
                    grades=grades,
                )
            )
    ordered = randomized_order(samples, seed_material=f"grading:{run_id}")
    return RunGradingView(
        run_id=run_id,
        goal_slug=str(context.goal.slug),
        goal_name=str(context.goal.name),
        criteria=context.human,
        samples=tuple(ordered),
        expected=len(samples) * len(context.human),
        recorded=recorded,
    )


def record_run_grades(  # noqa: PLR0913 — a grade needs the run, the grader and the re-aggregation
    database: Database,
    run_id: str,
    submissions: Sequence[RunGradeSubmission],
    *,
    graded_by: str,
    registry: Any,  # noqa: ANN401 — a BenchmarkRegistry; importing it here would pull runs in
    evidence_settings: Any = None,  # noqa: ANN401 — an EvidenceSettings, or None for defaults
    clock: Clock = utc_now,
) -> int:
    """Record grades on a run's samples, finish their composites, and refresh the evidence.

    Each grade lands on the sample's existing ``criterion_scores`` row for that criterion — the
    row the run wrote as ``skipped`` with ``human_grade_pending`` — turning it into ``scored``
    with ``raw = (grade − 1) / (points − 1)``. The sample's composite is then recomputed through
    the same function the run engine uses, the run's aggregate metrics are rewritten from its
    samples, and the subject's capability evidence is recomputed, so a grade recorded a week later
    reaches the evidence bundle by the same path a rule's score did during the run.

    Args:
        database: The database handle.
        run_id: The completed goal run.
        submissions: The grades.
        graded_by: Free text the grader supplied. Never harvested from the environment.
        registry: The benchmark registry, for the run's metric definitions.
        evidence_settings: The ``[evidence]`` section, or ``None`` for the shipped defaults.
        clock: Injected for deterministic tests.

    Returns:
        How many grades were recorded.

    Raises:
        RunNotFound: No run has this id.
        RunNotGradeable: See :func:`run_grading_view`.
        ValidationError: A submission names a sample outside the run, a criterion that is not a
            human one, or a grade outside the scale.
    """
    from freeweight.benchmarks.goal.runner import verdict_from_outcomes
    from freeweight.domain.goals.criteria import CriterionOutcome, CriterionStatus
    from freeweight.infrastructure.db.models_goals import CriterionScore
    from freeweight.infrastructure.db.models_runs import Sample
    from freeweight.infrastructure.db.repositories.goals import CriterionScoreRepository
    from freeweight.services.evidence import recompute_for_run
    from freeweight.services.runs import reaggregate_run

    now = clock()
    touched: list[str] = []
    with database.write() as session:
        context = _run_goal(session, run_id)
        by_key = {criterion.key: criterion for criterion in context.human}
        sample_ids = {sample.id for sample in _run_samples(session, run_id)}
        for submission in submissions:
            criterion = by_key.get(submission.criterion_key)
            if criterion is None:
                raise ValidationError(
                    f"Goal {context.goal.slug!r} has no human criterion "
                    f"{submission.criterion_key!r}.",
                    details={"criterion": submission.criterion_key},
                )
            if submission.sample_id not in sample_ids:
                raise ValidationError(
                    f"Sample {submission.sample_id!r} is not a completed sample of run {run_id!r}.",
                    details={"sample_id": submission.sample_id, "run": run_id},
                )
            if not 1 <= submission.grade <= criterion.scale_points:
                raise ValidationError(
                    f"Grade {submission.grade} is outside criterion {criterion.key!r}'s "
                    f"1..{criterion.scale_points} scale.",
                    details={"criterion": criterion.key, "grade": submission.grade},
                )
            row = next(
                (
                    score
                    for score in CriterionScoreRepository().list_for_sample(
                        session, submission.sample_id
                    )
                    if score.criterion_key == criterion.key
                ),
                None,
            )
            if row is None:
                raise ValidationError(
                    f"Sample {submission.sample_id!r} carries no criterion row for "
                    f"{criterion.key!r}; it was not scored by this rubric.",
                    details={"sample_id": submission.sample_id, "criterion": criterion.key},
                )
            points = criterion.scale_points
            row.raw_score = (submission.grade - 1) / (points - 1) if points > 1 else 1.0
            row.status = CriterionStatus.SCORED.value
            row.skip_reason = None
            row.detail_json = {
                "human_grade": submission.grade,
                "scale_points": points,
                "note": submission.note,
                "graded_by": graded_by,
                "graded_at": now.isoformat(),
            }
            if submission.sample_id not in touched:
                touched.append(submission.sample_id)
        session.flush()

        ordinal = {str(row.key): int(row.ordinal) for row in context.criteria}
        for sample_id in touched:
            sample = session.get(Sample, sample_id)
            if sample is None:  # pragma: no cover — checked against the run moments ago
                continue
            rows = sorted(
                CriterionScoreRepository().list_for_sample(session, sample_id),
                key=lambda score: ordinal.get(str(score.criterion_key), 0),
            )
            outcomes = [
                CriterionOutcome(
                    criterion_key=str(score.criterion_key),
                    rung=Rung(str(score.rung)),
                    weight=float(score.weight),
                    raw_score=score.raw_score,
                    status=CriterionStatus(str(score.status)),
                    gated=bool(score.gated),
                    skip_reason=score.skip_reason,
                    detail=dict(score.detail_json) if isinstance(score.detail_json, dict) else {},
                )
                for score in rows
                if isinstance(score, CriterionScore)
            ]
            stored = dict(sample.result_json) if isinstance(sample.result_json, dict) else {}
            verdict = verdict_from_outcomes(
                slug=str(context.goal.slug),
                case_id=str(sample.case_id),
                outcomes=outcomes,
                judge_validity_factor=float(stored.get("judge_validity_factor", 1.0)),
            )
            sample.score = verdict.score
            sample.score_method = verdict.method.value
            sample.result_json = _json_safe(dict(verdict.detail))
        session.flush()

    reaggregate_run(database, run_id, registry=registry, clock=clock)
    recompute_for_run(database, run_id, settings=evidence_settings, clock=clock)
    return len(submissions)


def _json_safe(value: Any) -> Any:  # noqa: ANN401 — a JSON value has no narrower type
    """Round-trip a value through JSON so nothing un-storable reaches ``PortableJSON``."""
    import json

    return json.loads(json.dumps(value, default=str))
