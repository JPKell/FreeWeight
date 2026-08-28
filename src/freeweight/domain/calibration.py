"""freeweight.domain.calibration — the partition, the validity factor and the gate.

Three decisions, each of which the whole feature rests on:

**The partition.** A seeded, stratified split of the author's graded samples into *anchors* —
embedded in the judge prompt as few-shot exemplars — and a *holdout* the jury is never shown. The
holdout is the only honest estimate of agreement, and leaking it would make the figure
self-congratulatory. Stratified across the grade range so both halves span the scale, and seeded so
two calibration runs of the same goal, jury and grades produce identical partitions.

**The validity factor.** ADR-0032 §2's sixth confidence factor: ``v_c = 1.0`` for any criterion
at rungs 1–4, and ``max(0, kappa_w) × min(1, sqrt(n_holdout / n_holdout_target))`` for a judged
one. The shrinkage is
why six holdout samples at ``kappa_w = 0.71`` yield ``0.55`` and not ``0.71`` — a coefficient over
six observations is not the same claim as a coefficient over sixty, and the arithmetic says so.

**The gate.** Weighted ``kappa_w`` against ``calibration.min_agreement``. Below it the run still
executes and every sample is inspectable, the result is badged ``uncalibrated``, and **no
capability evidence is emitted at all** — not discounted evidence, none. This is the one place in
the suite where a measurement is withheld rather than degraded, and the departure is deliberate:
ADR-0017's floor is a weight for a known quantity, and an uncalibrated rubric has not established a
quantity.

**"Not enough grades" is not "poor agreement".** :class:`CalibrationState` keeps them apart in
code, so the API and the UI cannot conflate them either. One means the author has not done the work
yet; the other means they did it and learned the rubric is not measurable. The remedies are
opposite.

Pure domain: stdlib only.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from freeweight.domain.agreement import weighted_mean

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_N_HOLDOUT_TARGET",
    "POLICY_VERSION",
    "CalibrationState",
    "GateVerdict",
    "Partition",
    "criterion_validity",
    "judge_validity_factor",
    "partition_samples",
    "verdict_for",
]

DEFAULT_N_HOLDOUT_TARGET = 10
"""The shrinkage denominator (ADR-0032 §2). Configuration, recorded with the policy version."""

POLICY_VERSION = "1.0"
"""The calibration-policy version every report this build writes is stamped with.

It exists so a report computed under one set of parameters is never silently compared with one
computed under another — the same role ``policy_version`` plays on ``capability_evidence``."""

_ANCHOR = "anchor"
_HOLDOUT = "holdout"


class CalibrationState(StrEnum):
    """Where a goal stands with respect to its own judged criteria.

    Four states, and the distinction between the middle two is the one spec §13 insists on:
    ``CALIBRATION_INSUFFICIENT`` is raised only when fewer than ``min_samples`` grades exist —
    the author has not yet done the work — as distinct from having done it and learned the rubric
    is not measurable.
    """

    NOT_REQUIRED = "not_required"
    """The goal has no judged criterion. Nothing needs calibrating, so nothing failed to."""

    INSUFFICIENT = "insufficient"
    """Fewer than ``calibration.min_samples`` graded samples. The work is not done yet."""

    UNCALIBRATED = "uncalibrated"
    """Enough grades, and the agreement did not reach the gate. The rubric is not measurable yet."""

    CALIBRATED = "calibrated"
    """The agreement cleared the gate. Evidence may be emitted."""


@dataclass(frozen=True, slots=True)
class Partition:
    """One seeded, stratified split of the author's graded samples.

    Attributes:
        anchors: Sample identifiers embedded in the judge prompt as exemplars.
        holdout: Sample identifiers the jury is never shown.
        seed: The seed that produced the split, recorded so the partition is reproducible and a
            reader can verify the holdout was not chosen to flatter the result.
        strata: How many grades were seen at each grade value, for the report.
    """

    anchors: tuple[str, ...]
    holdout: tuple[str, ...]
    seed: int
    strata: Mapping[int, int] = field(default_factory=dict)

    def label(self, sample_id: str) -> str:
        """Return ``"anchor"`` or ``"holdout"`` for one sample.

        Raises:
            KeyError: The sample is in neither half, which means it was not part of this
                partition and storing it with one of the two labels would be a lie.
        """
        if sample_id in self.anchors:
            return _ANCHOR
        if sample_id in self.holdout:
            return _HOLDOUT
        raise KeyError(f"Sample {sample_id!r} is not part of this partition.")


def partition_samples(
    graded: Mapping[str, int], *, holdout_fraction: float, seed: int
) -> Partition:
    """Split graded samples into anchors and a holdout, seeded and stratified by grade.

    Stratified so that both halves span the scale: a holdout of nothing but 5s would measure the
    jury's agreement at one end of the rubric and report it as agreement about the rubric. Within
    each grade the order is a seeded shuffle, so the split is reproducible without being an
    artefact of insertion order.

    Args:
        graded: ``{sample_id: grade}`` — one representative grade per sample, normally the mean
            of its per-criterion grades rounded to the scale.
        holdout_fraction: The share to withhold, strictly between 0 and 1.
        seed: The recorded partition seed.

    Returns:
        The partition. Both halves are sorted, so the stored order does not depend on dictionary
        iteration order either.

    Raises:
        ValueError: ``holdout_fraction`` is not strictly between 0 and 1. A holdout of everything
            leaves no anchors and one of nothing leaves no honest estimate of agreement; both are
            configuration errors rather than degenerate-but-valid splits.
    """
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError(
            f"holdout_fraction must be above 0 and below 1; got {holdout_fraction}. A holdout of "
            "everything leaves no anchors, and one of nothing leaves no honest estimate of "
            "agreement."
        )
    strata: dict[int, list[str]] = {}
    for sample_id, grade in sorted(graded.items()):
        strata.setdefault(int(grade), []).append(sample_id)

    anchors: list[str] = []
    holdout: list[str] = []
    for grade in sorted(strata):
        members = list(strata[grade])
        random.Random(f"{seed}:{grade}").shuffle(members)  # noqa: S311 — a partition, not crypto
        wanted = round(len(members) * holdout_fraction)
        # At least one from every stratum that has two, so the holdout spans the scale rather
        # than the rounding.
        if wanted == 0 and len(members) > 1:
            wanted = 1
        if wanted == len(members) and len(members) > 1:
            wanted = len(members) - 1
        holdout.extend(members[:wanted])
        anchors.extend(members[wanted:])
    return Partition(
        anchors=tuple(sorted(anchors)),
        holdout=tuple(sorted(holdout)),
        seed=seed,
        strata={grade: len(members) for grade, members in sorted(strata.items())},
    )


def criterion_validity(
    kappa_w: float | None, *, n_holdout: int, n_holdout_target: int = DEFAULT_N_HOLDOUT_TARGET
) -> float:
    """One judged criterion's validity: ``max(0, kappa_w) × min(1, sqrt(n / target))``.

    ADR-0032 §2's formula, written out. The shrinkage term has the same shape as ADR-0017's
    existing ``sample_factor``, so it introduces no new statistical vocabulary for a user to learn.

    Args:
        kappa_w: The criterion's measured agreement, or ``None`` when it could not be measured.
        n_holdout: How many held-out samples produced it.
        n_holdout_target: The shrinkage denominator.

    Returns:
        The factor in ``0.0..1.0``. ``0.0`` for an unmeasured or negative coefficient: a judge that
        agrees with the author worse than chance has established nothing.

    Raises:
        ValueError: ``n_holdout`` is negative or ``n_holdout_target`` is not positive.
    """
    if n_holdout < 0:
        raise ValueError(f"n_holdout cannot be negative; got {n_holdout}.")
    if n_holdout_target <= 0:
        raise ValueError(f"n_holdout_target must be positive; got {n_holdout_target}.")
    if kappa_w is None:
        return 0.0
    shrinkage = min(1.0, math.sqrt(n_holdout / n_holdout_target))
    return max(0.0, kappa_w) * shrinkage


def judge_validity_factor(
    *,
    weights: Mapping[str, float],
    judged: Mapping[str, float | None],
    n_holdout: Mapping[str, int],
    n_holdout_target: int = DEFAULT_N_HOLDOUT_TARGET,
) -> float:
    """The goal-level sixth confidence factor (ADR-0032 §2).

    ``Σ(weight_c × v_c) / Σ(weight_c)`` over every criterion contributing to the score, with
    ``v_c = 1.0`` for anything scored at rungs 1–4 and :func:`criterion_validity` for a judged one.
    Clamped to ``[0.05, 1.0]``, the same clamp ADR-0017 applies to confidence itself.

    Three properties this is chosen for, and each is asserted in the tests:

    * it is **1.0 for a goal with no judged criterion**, so no deterministic result changes value;
    * **mechanizing a criterion raises it**, which is the arithmetic incentive the ladder lacked;
    * **small holdouts are shrunk towards zero rather than trusted**.

    Args:
        weights: Every contributing criterion's weight, by key.
        judged: The measured ``kappa_w`` of each *judged* criterion, by key. A criterion absent
            from this mapping is scored at rungs 1–4 and has validity ``1.0``.
        n_holdout: The holdout size behind each judged criterion's coefficient.
        n_holdout_target: The shrinkage denominator.

    Returns:
        The factor.

    Raises:
        ValueError: ``weights`` is empty, or a judged criterion has no weight. Both would make the
            factor a claim about a rubric this function was not shown.
    """
    if not weights:
        raise ValueError("judge_validity_factor needs at least one weighted criterion.")
    missing = sorted(set(judged) - set(weights))
    if missing:
        raise ValueError(
            f"Judged criteria {missing} have no weight; the factor is a weighted mean and cannot "
            "include a criterion whose share of the composite is unknown."
        )
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("judge_validity_factor needs positive total weight.")
    accumulated = 0.0
    for key, weight in weights.items():
        if key in judged:
            validity = criterion_validity(
                judged[key],
                n_holdout=n_holdout.get(key, 0),
                n_holdout_target=n_holdout_target,
            )
        else:
            validity = 1.0
        accumulated += weight * validity
    return min(1.0, max(0.05, accumulated / total))


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Whether a goal's judged criteria may emit capability evidence, and why.

    Attributes:
        state: Which of the four :class:`CalibrationState` values this goal is in.
        weighted_kappa_w: The figure the gate compared, or ``None`` when there was none.
        min_agreement: The threshold in force, recorded because it is configuration and a reader
            must not assume the default.
        judge_validity_factor: The goal-level factor.
        n_holdout: How many held-out samples the figures rest on.
        n_anchor: How many were embedded in the judge prompt.
        graded_samples: How many graded samples exist at all.
        min_samples: The minimum the policy requires.
        policy_version: The parameters' version.
    """

    state: CalibrationState
    weighted_kappa_w: float | None
    min_agreement: float
    judge_validity_factor: float
    n_holdout: int = 0
    n_anchor: int = 0
    graded_samples: int = 0
    min_samples: int = 0
    policy_version: str = POLICY_VERSION

    @property
    def passed(self) -> bool:
        """Whether evidence may be emitted.

        ``True`` for a goal with no judged criterion: nothing needed calibrating, so nothing
        failed to. ``False`` for both ``insufficient`` and ``uncalibrated`` — different reasons,
        same consequence for the evidence contract, and the state says which.
        """
        return self.state in {CalibrationState.NOT_REQUIRED, CalibrationState.CALIBRATED}

    def as_json(self) -> dict[str, Any]:
        """Return the verdict as the API, the CLI and the report render it."""
        return {
            "calibration_state": self.state.value,
            "passed_gate": self.passed,
            "weighted_kappa_w": self.weighted_kappa_w,
            "min_agreement": self.min_agreement,
            "judge_validity_factor": self.judge_validity_factor,
            "n_holdout": self.n_holdout,
            "n_anchor": self.n_anchor,
            "graded_samples": self.graded_samples,
            "min_samples": self.min_samples,
            "policy_version": self.policy_version,
        }


def verdict_for(  # noqa: PLR0913 — the gate is a function of exactly these facts
    *,
    weights: Mapping[str, float],
    judged_kappa: Mapping[str, float | None],
    n_holdout: Mapping[str, int],
    graded_samples: int,
    min_samples: int,
    min_agreement: float,
    n_anchor: int = 0,
    n_holdout_target: int = DEFAULT_N_HOLDOUT_TARGET,
) -> GateVerdict:
    """Decide whether a goal's judged criteria may emit evidence.

    Args:
        weights: Every criterion's weight, by key.
        judged_kappa: The measured agreement of each judged criterion. Empty for a goal with no
            judged criterion.
        n_holdout: The holdout size behind each judged criterion.
        graded_samples: How many calibration samples the author has graded.
        min_samples: ``calibration.min_samples``.
        min_agreement: ``calibration.min_agreement``.
        n_anchor: How many samples were embedded as exemplars.
        n_holdout_target: The shrinkage denominator.

    Returns:
        The verdict, in one of four states.
    """
    if not judged_kappa:
        return GateVerdict(
            state=CalibrationState.NOT_REQUIRED,
            weighted_kappa_w=None,
            min_agreement=min_agreement,
            judge_validity_factor=1.0,
            graded_samples=graded_samples,
            min_samples=min_samples,
        )
    factor = judge_validity_factor(
        weights=weights,
        judged=judged_kappa,
        n_holdout=n_holdout,
        n_holdout_target=n_holdout_target,
    )
    holdout = max(n_holdout.values(), default=0)
    if graded_samples < min_samples:
        # The author has not done the work yet. Distinct from having done it and learned the
        # rubric is not measurable — spec §13 keeps the two apart, and so does this branch.
        return GateVerdict(
            state=CalibrationState.INSUFFICIENT,
            weighted_kappa_w=None,
            min_agreement=min_agreement,
            judge_validity_factor=factor,
            n_holdout=holdout,
            n_anchor=n_anchor,
            graded_samples=graded_samples,
            min_samples=min_samples,
        )
    weighted = weighted_mean(judged_kappa, weights)
    state = (
        CalibrationState.CALIBRATED
        if weighted is not None and weighted >= min_agreement
        else CalibrationState.UNCALIBRATED
    )
    return GateVerdict(
        state=state,
        weighted_kappa_w=weighted,
        min_agreement=min_agreement,
        judge_validity_factor=factor,
        n_holdout=holdout,
        n_anchor=n_anchor,
        graded_samples=graded_samples,
        min_samples=min_samples,
    )
