"""freeweight.domain.goals.composite — criteria become one score, gates and all.

[Subjective Goals §4.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s arithmetic, and
the three rules that make the resulting number readable:

**A skipped criterion is excluded, and the exclusion is visible in the weight actually applied.**
Not scored zero, and not silently renormalized behind the user's back. ``applied_weight`` and
``declared_weight`` are both on the result, so a composite computed over 60 % of a rubric says so.

**A hard gate zeroes the composite and names itself.** ``gated_by`` carries the criterion key, so
"this sample scored 0" is never a mystery. A gate that fired is a fact about the sample, not a
degradation of the measurement.

**``score_method_mix`` sits beside the score, always.** A 0.82 that is 80 % rules is a different
kind of number from a 0.82 that is 80 % judgement, and the UI never shows one without the other.
It is computed over the weight that was *actually applied*, because a judged criterion that was
skipped for want of a jury did not contribute judgement to this sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from freeweight.domain.goals.criteria import CriterionOutcome
from freeweight.domain.goals.pack import Rung

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["SampleComposite", "composite_score", "outcome_detail", "score_method_mix"]


def score_method_mix(outcomes: Sequence[CriterionOutcome]) -> dict[str, float]:
    """Return the fraction of *applied* weight contributed by each ladder rung.

    Args:
        outcomes: This sample's criterion outcomes.

    Returns:
        A mapping with all four rungs present, summing to ``1.0`` — or all zeros when nothing
        contributed. Every rung is present even at zero, because a consumer reading
        ``{"rule": 1.0}`` cannot tell whether the judge share was zero or the key was forgotten.
    """
    mix = {rung.value: 0.0 for rung in Rung}
    applied = sum(outcome.weight for outcome in outcomes if outcome.contributes)
    if applied <= 0:
        return mix
    for outcome in outcomes:
        if outcome.contributes:
            mix[outcome.rung.value] += outcome.weight / applied
    return mix


@dataclass(frozen=True, slots=True)
class SampleComposite:
    """One sample's composite score and everything needed to read it.

    Attributes:
        composite: The weighted mean in ``0.0..1.0``, ``0.0`` when a gate fired, or ``None`` when
            no criterion contributed at all — which is an unmeasured sample, not a bad one.
        gated_by: The gate criterion that fired, or ``None``.
        applied_weight: The weight that actually contributed.
        declared_weight: The weight the rubric declares. Different from ``applied_weight``
            whenever something was skipped, and shown next to it for that reason.
        score_method_mix: The rung shares of the applied weight.
        outcomes: The per-criterion outcomes, in declaration order.
    """

    composite: float | None
    gated_by: str | None = None
    applied_weight: float = 0.0
    declared_weight: float = 0.0
    score_method_mix: Mapping[str, float] = field(default_factory=dict)
    outcomes: tuple[CriterionOutcome, ...] = ()

    def as_detail(self) -> dict[str, Any]:
        """Return the evidence a sample stores, ready for ``result_json``."""
        return {
            "composite_score": self.composite,
            "gated_by": self.gated_by,
            "applied_weight": self.applied_weight,
            "declared_weight": self.declared_weight,
            "score_method_mix": dict(self.score_method_mix),
            "criteria": [outcome_detail(outcome) for outcome in self.outcomes],
        }


def outcome_detail(outcome: CriterionOutcome) -> dict[str, Any]:
    """Render one criterion outcome as the entry a sample stores under ``criteria``.

    One definition of the shape, because two things read it: this module's own
    :meth:`SampleComposite.as_detail`, and the run engine, which turns each entry into a
    ``criterion_scores`` row. A goal that defers its judging writes these entries before it has a
    composite and reads them back afterwards, so a second spelling of the same dictionary would be
    a silent way for the two halves of a run to disagree.

    Args:
        outcome: The criterion outcome.

    Returns:
        The entry, JSON-safe.
    """
    return {
        "key": outcome.criterion_key,
        "rung": outcome.rung.value,
        "weight": outcome.weight,
        "raw_score": outcome.raw_score,
        "status": outcome.status.value,
        "gated": outcome.gated,
        "skip_reason": outcome.skip_reason,
        "detail": dict(outcome.detail),
    }


def composite_score(outcomes: Sequence[CriterionOutcome]) -> SampleComposite:
    """Combine one sample's criterion outcomes into one score.

    ``composite = Σ(weight × raw) / Σ(weight)`` over the criteria that produced a score — unless a
    gate criterion failed, in which case the composite is ``0.0`` and the gate is named.

    Args:
        outcomes: This sample's outcomes, in the goal's declaration order.

    Returns:
        The composite. ``composite`` is ``None`` when nothing contributed: every criterion was
        skipped or errored, so there is no measurement to report — as distinct from a measurement
        of zero, which is what a gate produces.
    """
    gated = next((outcome for outcome in outcomes if outcome.gated), None)
    applied = sum(outcome.weight for outcome in outcomes if outcome.contributes)
    declared = sum(outcome.weight for outcome in outcomes)
    mix = score_method_mix(outcomes)
    if gated is not None:
        return SampleComposite(
            composite=0.0,
            gated_by=gated.criterion_key,
            applied_weight=applied,
            declared_weight=declared,
            score_method_mix=mix,
            outcomes=tuple(outcomes),
        )
    if applied <= 0:
        return SampleComposite(
            composite=None,
            applied_weight=0.0,
            declared_weight=declared,
            score_method_mix=mix,
            outcomes=tuple(outcomes),
        )
    total = sum(
        outcome.weight * float(outcome.raw_score)
        for outcome in outcomes
        if outcome.contributes and outcome.raw_score is not None
    )
    return SampleComposite(
        composite=min(1.0, max(0.0, total / applied)),
        applied_weight=applied,
        declared_weight=declared,
        score_method_mix=mix,
        outcomes=tuple(outcomes),
    )
