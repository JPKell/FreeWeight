"""freeweight.domain.goals.criteria — one criterion, scored, under a bounded amount of time.

The dispatch table between a goal's declared ``rule.type`` and the seventeen functions of
:mod:`freeweight.domain.scorers.rules`, plus the two things that turn a pure function into
something safe to run against model output on behalf of a user who wrote the rule:

**A timeout, because a rule may be handed an unexpectedly large response.** ``rule_timeout_ms``
bounds every criterion, not only the regex one, so a future rule cannot quietly escape the guard.
It is enforced by running the rule in a worker thread and abandoning the wait, and this docstring
owes an honest statement of what that can and cannot do:

* It **does** bound any rule that yields the GIL or runs as Python bytecode — every rule in the
  library, over any input, is such a rule.
* It **does not** interrupt a match already running inside CPython's regex engine, which holds the
  GIL for the whole match. No in-process mechanism can. That is precisely why
  :func:`~freeweight.domain.scorers.rules.lint_pattern` refuses unbounded repetition of a group
  *before the pattern is ever compiled*: for the one construction that can run away, the guard has
  to be a refusal at authoring time, and the timeout is the backstop for everything else.

Spec §14 names both guards, and they divide the work in exactly that way.

**A refusal that is not a zero.** A rule returning ``unsupported`` becomes a *skipped* criterion
with ``raw_score = NULL``; a rule that raises or times out becomes an *errored* one, also with
``raw_score = NULL``. Neither contributes to the composite, and both stay visible in the applied
weight, which is the mechanism the phase's named failure mode — "a rule that silently scores 0 for
input it cannot parse" — is prevented by
([ADR-0016](../../../../../docs/adr/0016-unavailable-is-not-zero.md)).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

from freeweight.domain.goals.pack import Rung
from freeweight.domain.scorers.rules import RuleInvalid, RuleResult
from freeweight.domain.scorers.rules.length import (
    paragraph_shape,
    sentence_length_distribution,
    word_count,
)
from freeweight.domain.scorers.rules.phrases import forbidden_phrases, required_phrases
from freeweight.domain.scorers.rules.pov_tense import pov_tense
from freeweight.domain.scorers.rules.punctuation import punctuation_profile
from freeweight.domain.scorers.rules.readability import readability
from freeweight.domain.scorers.rules.reference import (
    claim_coverage,
    entity_recall,
    no_unsupported_claims,
    reference_similarity,
)
from freeweight.domain.scorers.rules.regex import regex_match
from freeweight.domain.scorers.rules.repetition import repetition
from freeweight.domain.scorers.rules.structure import json_schema, structure
from freeweight.domain.scorers.rules.vocabulary import vocabulary_profile

if TYPE_CHECKING:
    from collections.abc import Mapping

    from freeweight.domain.goals.pack import Criterion

__all__ = [
    "DEFAULT_RULE_TIMEOUT_MS",
    "GATEABLE_RULE_TYPES",
    "REFERENCE_RULE_TYPES",
    "RULE_RUNGS",
    "RULE_TYPES",
    "CriterionOutcome",
    "CriterionStatus",
    "SkipReason",
    "invoke_rule",
    "score_criterion",
]

logger = logging.getLogger(__name__)

DEFAULT_RULE_TIMEOUT_MS = 250
"""How long one criterion may take on one sample, absent configuration (spec §12, ``[goals]``)."""


class CriterionStatus(StrEnum):
    """What became of one criterion on one sample (data model, ``criterion_scores``)."""

    SCORED = "scored"
    SKIPPED = "skipped"
    ERROR = "error"


class SkipReason(StrEnum):
    """Why a criterion produced no score.

    ``UNSUPPORTED`` is the rule's own honest refusal — an empty response, a band it cannot apply,
    ground truth the task did not supply. ``RULE_TIMEOUT`` and ``RULE_INVALID`` are failures of
    the criterion rather than absences of measurement, and they are recorded as ``error`` rather
    than ``skipped`` so a user can tell "this did not apply" from "this is broken".

    ``JUDGE_DEFERRED`` is **transient and must never survive a completed run**: it marks a judged
    criterion during the generation phase, before the jury has run. It is a different fact from
    ``JUDGE_UNAVAILABLE``, which is permanent and means no jury could be assembled at all — one
    says "not yet", the other says "not ever", and a reader who could not tell them apart would
    read an in-progress run as a degraded one.
    """

    UNSUPPORTED = "unsupported"
    JUDGE_UNAVAILABLE = "judge_unavailable"
    JUDGE_DEFERRED = "judge_deferred"
    HUMAN_GRADE_PENDING = "human_grade_pending"
    RULE_TIMEOUT = "rule_timeout"
    RULE_INVALID = "rule_invalid"
    RULE_ERROR = "rule_error"


class RuleFunction(Protocol):
    """A rung-2 rule: text and parameters in, a :class:`RuleResult` out."""

    def __call__(self, text: str, parameters: Mapping[str, Any]) -> RuleResult:
        """Score one response."""
        ...


class ReferenceFunction(Protocol):
    """A rung-3 rule: as above, plus the task's own ground truth."""

    def __call__(
        self,
        text: str,
        parameters: Mapping[str, Any],
        *,
        source: Mapping[str, Any] | None = None,
    ) -> RuleResult:
        """Score one response against ground truth."""
        ...


RULE_TYPES: Mapping[str, RuleFunction | ReferenceFunction] = {
    # Rung 2 — Subjective Goals §3.1, in the order the table lists them.
    "forbidden_phrases": forbidden_phrases,
    "required_phrases": required_phrases,
    "word_count": word_count,
    "sentence_length_distribution": sentence_length_distribution,
    "paragraph_shape": paragraph_shape,
    "readability": readability,
    "pov_tense": pov_tense,
    "vocabulary_profile": vocabulary_profile,
    "punctuation_profile": punctuation_profile,
    "structure": structure,
    "json_schema": json_schema,
    "regex_match": regex_match,
    "repetition": repetition,
    # Rung 3 — Subjective Goals §3.2.
    "entity_recall": entity_recall,
    "claim_coverage": claim_coverage,
    "no_unsupported_claims": no_unsupported_claims,
    "reference_similarity": reference_similarity,
}
"""Every check a criterion may declare, by ``rule.type``.

One table, so that the lint, the CLI's ``suggest-rules`` and the runner cannot disagree about
which types exist. A criterion naming a type absent from here is refused at pack-load time rather
than skipped forever with a plausible reason."""

REFERENCE_RULE_TYPES: frozenset[str] = frozenset(
    {"entity_recall", "claim_coverage", "no_unsupported_claims", "reference_similarity"}
)
"""The rung-3 types, which need the task's annotated source."""

RULE_RUNGS: Mapping[str, Rung] = {
    name: (Rung.REFERENCE if name in REFERENCE_RULE_TYPES else Rung.RULE) for name in RULE_TYPES
}
"""Which rung each type belongs to, so a criterion declaring the wrong one is a lint finding."""

GATEABLE_RULE_TYPES: frozenset[str] = frozenset(
    {"forbidden_phrases", "required_phrases", "json_schema", "regex_match"}
)
"""Types whose clean answer is exactly ``1.0``, and which therefore make sensible hard gates.

A gate is for a disqualifying property, not a gradual one (Subjective Goals §3.1). Putting one on
``readability`` would zero every sample whose grade level was a tenth outside the band, which is
not what anybody means by disqualifying — so the lint warns about it rather than the runner
silently doing it."""


@dataclass(frozen=True, slots=True)
class CriterionOutcome:
    """One criterion's verdict on one sample — a ``criterion_scores`` row, before it is stored.

    Attributes:
        criterion_key: Which criterion this is.
        rung: The ladder rung that produced it, recorded rather than implied.
        weight: The criterion's declared weight.
        raw_score: ``0.0..1.0``, or ``None`` when nothing was measured. Never ``0.0`` for "could
            not measure".
        status: ``scored``, ``skipped`` or ``error``.
        gated: Whether this criterion is a hard gate and it failed.
        skip_reason: Why there is no score, or ``None`` when there is one.
        detail: What the rule measured — the phrases that matched, the distribution it found.
            This is what a headline goal number drills to.
    """

    criterion_key: str
    rung: Rung
    weight: float
    raw_score: float | None = None
    status: CriterionStatus = CriterionStatus.SCORED
    gated: bool = False
    skip_reason: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def contributes(self) -> bool:
        """Whether this outcome enters the composite's weighted mean."""
        return self.status is CriterionStatus.SCORED and self.raw_score is not None


def invoke_rule(
    criterion: Criterion, text: str, source: Mapping[str, Any] | None = None
) -> RuleResult:
    """Run one criterion's rule directly, with no timeout and no error containment.

    The lint calls this against a fixed probe paragraph so that a malformed parameter block is a
    finding rather than a surprise mid-run; :func:`score_criterion` calls it inside the timeout.
    It raises whatever the rule raises, which is the point — the containment belongs to the caller
    that is scoring a real sample, not to the one that is validating a rubric.

    Args:
        criterion: The criterion, whose ``rule.type`` must be in :data:`RULE_TYPES`.
        text: The text to score.
        source: The task's annotated source, for a rung-3 rule.

    Returns:
        The rule's result.

    Raises:
        KeyError: The criterion names a rule type this build does not have. Callers check
            membership first; this is the assertion that they did.
        RuleInvalid: The criterion's parameters are malformed.
    """
    rule_type = criterion.rule_type or ""
    function = RULE_TYPES[rule_type]
    parameters = criterion.rule_parameters
    if rule_type in REFERENCE_RULE_TYPES:
        # The cast is what :data:`REFERENCE_RULE_TYPES` is for: membership in that set *is* the
        # statement that this entry takes a ``source``, and the two are declared side by side so
        # they cannot drift apart.
        return cast("ReferenceFunction", function)(text, parameters, source=source)
    return function(text, parameters)


def score_criterion(
    criterion: Criterion,
    text: str,
    *,
    source: Mapping[str, Any] | None = None,
    timeout_ms: int = DEFAULT_RULE_TIMEOUT_MS,
) -> CriterionOutcome:
    """Score one deterministic criterion against one response.

    Args:
        criterion: The criterion. Its rung must be ``rule`` or ``reference``; a judged or human
            criterion has no deterministic score and is the caller's business.
        text: The candidate's response.
        source: The task's annotated source, for a rung-3 criterion.
        timeout_ms: How long this criterion may take. See the module docstring for what "may"
            means and why.

    Returns:
        The outcome. Never raises for a bad rule: an unknown type, a malformed parameter block, a
        timeout and an unexpected exception all become ``error`` outcomes with ``raw_score =
        NULL``, because one broken criterion must not discard a sample's other measurements.

    Raises:
        ValueError: ``criterion`` is a judged or human criterion. That is a programming error in
            the caller rather than a defect in the goal, and returning an outcome for it would
            silently score a judged criterion as a rule.
    """
    if not criterion.rung.is_deterministic:
        raise ValueError(
            f"Criterion {criterion.key!r} is scored at rung {criterion.rung.value!r} and has no "
            "deterministic score; score_criterion is for rungs 2 and 3."
        )
    rule_type = criterion.rule_type or ""
    if rule_type not in RULE_TYPES:
        return CriterionOutcome(
            criterion_key=criterion.key,
            rung=criterion.rung,
            weight=criterion.weight,
            status=CriterionStatus.ERROR,
            skip_reason=SkipReason.RULE_INVALID.value,
            detail={
                "rule_type": rule_type,
                "error": f"Unknown rule type {rule_type!r}; this build runs {sorted(RULE_TYPES)}.",
            },
        )
    # A fresh executor per criterion, so an abandoned computation cannot occupy a worker the next
    # criterion needs. The pool is shut down without waiting: the point of the timeout is that the
    # run continues, and blocking on the thread we just gave up on would defeat it exactly.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fw-rule")
    try:
        future = executor.submit(invoke_rule, criterion, text, source)
        try:
            result = future.result(timeout=max(timeout_ms, 1) / 1000.0)
        except FutureTimeout:
            return CriterionOutcome(
                criterion_key=criterion.key,
                rung=criterion.rung,
                weight=criterion.weight,
                status=CriterionStatus.ERROR,
                skip_reason=SkipReason.RULE_TIMEOUT.value,
                detail={"rule_type": rule_type, "timeout_ms": timeout_ms},
            )
        except RuleInvalid as exc:
            return CriterionOutcome(
                criterion_key=criterion.key,
                rung=criterion.rung,
                weight=criterion.weight,
                status=CriterionStatus.ERROR,
                skip_reason=SkipReason.RULE_INVALID.value,
                detail={"rule_type": rule_type, "error": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 — one broken rule never discards a sample
            logger.warning(
                "goal.rule_error",
                extra={"criterion": criterion.key, "rule_type": rule_type},
                exc_info=exc,
            )
            return CriterionOutcome(
                criterion_key=criterion.key,
                rung=criterion.rung,
                weight=criterion.weight,
                status=CriterionStatus.ERROR,
                skip_reason=SkipReason.RULE_ERROR.value,
                detail={"rule_type": rule_type, "error": str(exc)},
            )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if result.score is None:
        return CriterionOutcome(
            criterion_key=criterion.key,
            rung=criterion.rung,
            weight=criterion.weight,
            status=CriterionStatus.SKIPPED,
            skip_reason=SkipReason.UNSUPPORTED.value,
            detail={
                "rule_type": rule_type,
                "unsupported_reason": result.unsupported_reason,
                **dict(result.detail),
            },
        )
    return CriterionOutcome(
        criterion_key=criterion.key,
        rung=criterion.rung,
        weight=criterion.weight,
        raw_score=result.score,
        status=CriterionStatus.SCORED,
        gated=criterion.is_gate and result.score < 1.0,
        detail={"rule_type": rule_type, **dict(result.detail)},
    )
