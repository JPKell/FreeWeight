"""freeweight.domain.goals.lint — every problem a goal pack has, with a severity each.

``freeweight goals validate`` on a deliberately bad pack must name **every** problem, not the first
one, so nothing here raises: each check appends a :class:`Finding` and the caller decides. A pack
carrying any ``error`` finding is refused at load — which is startup — because a malformed pack is
a startup failure and not a mid-run surprise (Phase 8A's Work list); ``warning`` and ``info``
findings are shown and recorded on ``goal_criteria.lint_json``.

**The lint pushes weight down the ladder, and it does not push back.** Its most valuable check is
the one [ADR-0031 §2](../../../../../docs/adr/0031-user-defined-goal-benchmarks.md) describes: a
rung-5 criterion a rung-2 rule could check is *flagged, with the rule named*, and the user may
overrule it. It is a warning and never a refusal, because the system cannot know that a phrase list
fully covers "avoid corporate hedging", and a false refusal is worse than a warning.

**FreeWeight never rewrites the criterion.** A finding names the problem and, where it can, names
the rule that would help. It does not propose replacement text: a model that reworded the user's
taste until it became measurable would be optimizing the target into the instrument, and the
resulting number would measure nothing (ADR-0031 §3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from freeweight.domain.goals.criteria import (
    GATEABLE_RULE_TYPES,
    REFERENCE_RULE_TYPES,
    RULE_RUNGS,
    RULE_TYPES,
)
from freeweight.domain.goals.pack import Rung
from freeweight.domain.scorers.rules import RuleInvalid

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.domain.goals.pack import Criterion, GoalPack

__all__ = [
    "MECHANIZABLE_HINTS",
    "Finding",
    "Severity",
    "has_errors",
    "lint_pack",
    "suggest_rules",
]

_WEIGHT_SUM_TOLERANCE = 1e-6
_PROBE_TEXT = (
    "The vault stood open. I counted the pallets twice and wrote the number on my hand.\n\n"
    "Nobody had signed the log since Tuesday, which told me more than the log would have."
)
"""A short, ordinary paragraph the lint runs every rule against to surface bad parameters.

Real prose rather than a placeholder, because several rules refuse an empty response and would
report ``unsupported`` instead of the parameter error the lint is looking for. Nothing is scored:
the only thing read is whether the rule raised
:class:`~freeweight.domain.scorers.rules.RuleInvalid`."""


class Severity(StrEnum):
    """How much a finding matters.

    ``ERROR`` refuses the pack. ``WARNING`` is the lint's own judgement and can be overruled by
    the user, who owns the rubric. ``INFO`` is a fact worth stating.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Finding:
    """One problem, or one observation, about a goal pack.

    Attributes:
        code: A stable machine-readable code, so a UI can render a finding it has copy for.
        severity: How much it matters.
        message: One sentence a person can act on.
        criterion_key: Which criterion it concerns, or ``None`` for a pack-level finding.
        suggested_rule: The rule type that would help, when the finding is about mechanizing a
            judged criterion. Never a rewritten criterion — see the module docstring.
    """

    code: str
    severity: Severity
    message: str
    criterion_key: str | None = None
    suggested_rule: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the finding as the API and the CLI render it."""
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "criterion": self.criterion_key,
            "suggested_rule": self.suggested_rule,
        }


MECHANIZABLE_HINTS: Mapping[str, tuple[str, ...]] = {
    "forbidden_phrases": (
        "avoid",
        "never say",
        "no jargon",
        "buzzword",
        "cliche",
        "cliché",
        "banned",
        "forbidden",
        "not linkedin",
        "corporate",
        "hedging",
        "filler",
    ),
    "required_phrases": ("must mention", "must include", "always mention", "must name"),
    "word_count": ("word count", "words long", "length", "too long", "too short", "concise"),
    "sentence_length_distribution": ("sentence", "rhythm", "cadence", "varied sentences"),
    "paragraph_shape": ("paragraph",),
    "readability": ("reading level", "readability", "grade level", "plain english", "jargon-free"),
    "pov_tense": (
        "first person",
        "second person",
        "third person",
        "past tense",
        "present tense",
        "point of view",
    ),
    "vocabulary_profile": ("vocabulary", "register", "word choice", "diction"),
    "punctuation_profile": ("exclamation", "em dash", "em-dash", "semicolon", "punctuation"),
    "structure": ("heading", "bullet", "bulleted", "list", "markdown", "code block", "table"),
    "json_schema": ("valid json", "json schema", "schema"),
    "regex_match": ("format", "pattern"),
    "repetition": ("repeat", "repetition", "repetitive", "restate"),
}
"""Phrases in a judged criterion's own words that a rule could probably check.

A keyword table and nothing cleverer, deliberately. The alternative — asking a model which
criteria are mechanizable — would put a model in the authoring loop, and this is a lint whose
whole value is that it is boring, fast and free. It over-fires by design: the finding is a warning
naming a rule, and a user who reads it and disagrees has lost ten seconds."""


def _criterion_text(criterion: Criterion) -> str:
    """Return everything the author wrote about one criterion, casefolded, for the hint match."""
    parts = [criterion.name, criterion.intent, criterion.key.replace("_", " ")]
    if criterion.scale is not None:
        parts.extend(criterion.scale.descriptors.values())
    return " ".join(parts).casefold()


def suggest_rules(criterion: Criterion) -> tuple[str, ...]:
    """Return the rule types that might carry part of one criterion, best guess first.

    Args:
        criterion: The criterion, judged or otherwise.

    Returns:
        Rule type names, in :data:`MECHANIZABLE_HINTS` order. Empty when nothing matched, which is
        the common and correct answer for a criterion about wit or register.
    """
    text = _criterion_text(criterion)
    return tuple(
        rule_type
        for rule_type, hints in MECHANIZABLE_HINTS.items()
        if any(re.search(rf"\b{re.escape(hint)}", text) for hint in hints)
    )


def _check_weights(pack: GoalPack, findings: list[Finding]) -> None:
    """The composite is a weighted mean, so unaccounted weight changes every criterion's share."""
    total = pack.total_weight
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        findings.append(
            Finding(
                code="WEIGHTS_DO_NOT_SUM",
                severity=Severity.ERROR,
                message=(
                    f"Criterion weights sum to {total:.4f}, not 1. The composite is a weighted "
                    "mean, so weight that is unaccounted for changes every criterion's real "
                    "share rather than only the total."
                ),
            )
        )


def _check_rule_criterion(criterion: Criterion, findings: list[Finding]) -> None:
    """Every way a deterministic criterion can be undeclarable."""
    if criterion.rule is None:
        findings.append(
            Finding(
                code="DETERMINISTIC_WITHOUT_RULE",
                severity=Severity.ERROR,
                message=(
                    f"Criterion {criterion.key!r} is scored at rung "
                    f"{criterion.rung.value!r} but declares no rule. A deterministic criterion is "
                    "defined by the check it runs."
                ),
                criterion_key=criterion.key,
            )
        )
        return
    rule_type = criterion.rule_type or ""
    if rule_type not in RULE_TYPES:
        findings.append(
            Finding(
                code="RULE_TYPE_UNKNOWN",
                severity=Severity.ERROR,
                message=(
                    f"Criterion {criterion.key!r} declares rule type {rule_type!r}; this build "
                    f"runs {sorted(RULE_TYPES)}."
                ),
                criterion_key=criterion.key,
            )
        )
        return
    expected = RULE_RUNGS[rule_type]
    if criterion.rung is not expected:
        findings.append(
            Finding(
                code="RUNG_MISMATCH",
                severity=Severity.ERROR,
                message=(
                    f"Criterion {criterion.key!r} declares rung {criterion.rung.value!r} for rule "
                    f"type {rule_type!r}, which is scored at rung {expected.value!r}. The rung is "
                    "what score_method_mix reports, so a wrong one misdescribes the number."
                ),
                criterion_key=criterion.key,
            )
        )
    if criterion.is_gate and rule_type not in GATEABLE_RULE_TYPES:
        findings.append(
            Finding(
                code="GATE_ON_GRADUAL_RULE",
                severity=Severity.WARNING,
                message=(
                    f"Criterion {criterion.key!r} is a hard gate on {rule_type!r}, which scores "
                    "proportionally. A gate zeroes the whole sample the moment the score falls "
                    "below 1.0, so a response a tenth outside the band would be disqualified. "
                    f"Gates suit {sorted(GATEABLE_RULE_TYPES)}."
                ),
                criterion_key=criterion.key,
            )
        )
    _probe_parameters(criterion, findings)


def _probe_parameters(criterion: Criterion, findings: list[Finding]) -> None:
    """Run the rule once against a fixed probe so a bad parameter block is a finding, not a run.

    The only thing read is whether :class:`~freeweight.domain.scorers.rules.RuleInvalid` was
    raised. The probe's *score* is meaningless and is discarded — a lint that reported it would be
    grading a paragraph nobody wrote.
    """
    from freeweight.domain.goals.criteria import invoke_rule

    try:
        invoke_rule(criterion, _PROBE_TEXT, _PROBE_SOURCE)
    except RuleInvalid as exc:
        findings.append(
            Finding(
                code="RULE_PARAMETERS_INVALID",
                severity=Severity.ERROR,
                message=(
                    f"Criterion {criterion.key!r} declares parameters this build refuses: {exc}"
                ),
                criterion_key=criterion.key,
            )
        )
    except Exception as exc:  # noqa: BLE001 — the lint reports, it never fails the pack load
        findings.append(
            Finding(
                code="RULE_PROBE_FAILED",
                severity=Severity.ERROR,
                message=(
                    f"Criterion {criterion.key!r} raised {type(exc).__name__} on a probe "
                    f"paragraph: {exc}. A rule that cannot survive ordinary prose cannot score a "
                    "run."
                ),
                criterion_key=criterion.key,
            )
        )


_PROBE_SOURCE: Mapping[str, Any] = {
    "text": _PROBE_TEXT,
    "entities": ["Tuesday"],
    "claims": [{"id": "probe", "text": "the pallets were counted twice"}],
    "references": [_PROBE_TEXT],
}
"""Ground truth for the probe, so a rung-3 criterion's parameters are checked too.

Without it every reference rule would return ``unsupported`` on the probe and its parameter block
would go unvalidated until the first real run."""


def _check_graded_criterion(criterion: Criterion, findings: list[Finding]) -> None:
    """Every way a judged or human criterion can be ungradeable."""
    if criterion.rule is not None:
        findings.append(
            Finding(
                code="GRADED_WITH_RULE",
                severity=Severity.ERROR,
                message=(
                    f"Criterion {criterion.key!r} is graded at rung {criterion.rung.value!r} but "
                    "declares a rule. If a rule can check it, it belongs at rung 'rule'; "
                    "declaring both would make its ladder position ambiguous."
                ),
                criterion_key=criterion.key,
            )
        )
    if criterion.scale is None:
        findings.append(
            Finding(
                code="GRADED_WITHOUT_SCALE",
                severity=Severity.ERROR,
                message=(
                    f"Criterion {criterion.key!r} is graded at rung {criterion.rung.value!r} but "
                    "declares no scale. A grade with no scale cannot be compared with another "
                    "grader's."
                ),
                criterion_key=criterion.key,
            )
        )
    elif criterion.rung is Rung.JUDGE and not criterion.scale.anchored:
        findings.append(
            Finding(
                code="JUDGED_WITHOUT_DESCRIPTORS",
                severity=Severity.ERROR,
                message=(
                    f"Criterion {criterion.key!r} is judged but its {criterion.scale.points}-point "
                    "scale describes fewer than three of its points. An unanchored scale gives a "
                    "jury nothing to calibrate against and reliably produces agreement near zero, "
                    "so it is refused now rather than discovered after twelve samples have been "
                    "graded."
                ),
                criterion_key=criterion.key,
            )
        )
    if criterion.is_gate:
        findings.append(
            Finding(
                code="GATE_ON_GRADED_CRITERION",
                severity=Severity.ERROR,
                message=(
                    f"Criterion {criterion.key!r} is a hard gate at rung "
                    f"{criterion.rung.value!r}. A gate zeroes a sample outright, and a judgement "
                    "is not a disqualification: it would let one juror's reading of one rubric "
                    "line discard the whole measurement."
                ),
                criterion_key=criterion.key,
            )
        )
    if criterion.rung is Rung.JUDGE:
        for rule_type in suggest_rules(criterion):
            findings.append(
                Finding(
                    code="MECHANIZABLE_JUDGED_CRITERION",
                    severity=Severity.WARNING,
                    message=(
                        f"Criterion {criterion.key!r} is judged, and a {rule_type!r} rule could "
                        "carry part of it. Rules are free, exact, and never disagree with you; "
                        "moving weight onto one raises this goal's judge_validity_factor "
                        "arithmetically. This is a suggestion — you own the rubric."
                    ),
                    criterion_key=criterion.key,
                    suggested_rule=rule_type,
                )
            )


def _check_pack_level(pack: GoalPack, findings: list[Finding]) -> None:
    """Findings about the goal as a whole rather than about one criterion."""
    if not pack.tasks:
        findings.append(
            Finding(
                code="NO_TASKS",
                severity=Severity.ERROR,
                message=(
                    f"Goal {pack.slug!r} declares no tasks. Criteria score outputs; with no task "
                    "there is no output to score."
                ),
            )
        )
    judged = pack.judged_criteria
    if judged and pack.judge is None:
        findings.append(
            Finding(
                code="JUDGED_WITHOUT_JUDGE_CONFIG",
                severity=Severity.ERROR,
                message=(
                    f"Goal {pack.slug!r} has judged criteria "
                    f"{[criterion.key for criterion in judged]} but no judge configuration. A "
                    "judged score is a property of the jury that produced it; without the jury's "
                    "identity the result cannot be separated from one produced by a different "
                    "instrument."
                ),
            )
        )
    needs_source = [
        criterion.key for criterion in pack.criteria if criterion.rule_type in REFERENCE_RULE_TYPES
    ]
    if needs_source and not any(task.source for task in pack.tasks):
        findings.append(
            Finding(
                code="REFERENCE_WITHOUT_GROUND_TRUTH",
                severity=Severity.WARNING,
                message=(
                    f"Criteria {needs_source} compare against ground truth, and no task supplies "
                    "any. They will report 'unsupported' on every sample rather than scoring "
                    "zero, so the goal will run and measure less of itself than it declares."
                ),
            )
        )
    if pack.contributes_to is not None:
        from setspec.vocabulary import CAPABILITIES, RESERVED_ROOTS

        root = pack.contributes_to.split(".", 1)[0]
        if root not in CAPABILITIES or root in RESERVED_ROOTS:
            findings.append(
                Finding(
                    code="CONTRIBUTES_TO_UNKNOWN",
                    severity=Severity.ERROR,
                    message=(
                        f"Goal {pack.slug!r} declares contributes_to "
                        f"{pack.contributes_to!r}, which is not a shipped capability. A goal may "
                        "additionally feed an existing capability, but the term has to be one "
                        f"other components already understand; the roots are "
                        f"{sorted(CAPABILITIES - RESERVED_ROOTS)}."
                    ),
                )
            )
    deterministic = sum(
        criterion.weight for criterion in pack.criteria if criterion.rung.is_deterministic
    )
    total = pack.total_weight or 1.0
    findings.append(
        Finding(
            code="DETERMINISTIC_WEIGHT_SHARE",
            severity=Severity.INFO,
            message=(
                f"{deterministic / total:.0%} of this goal's weight is scored deterministically. "
                "The better you understand what you want, the less of it needs a judge."
            ),
        )
    )
    if pack.unforked:
        findings.append(
            Finding(
                code="UNFORKED_STARTER",
                severity=Severity.WARNING,
                message=(
                    f"Goal {pack.slug!r} is running unedited starter content. A voice measured on "
                    "someone else's prompts is not your voice; the result is badged 'unforked' "
                    "until the criteria or tasks are edited."
                ),
            )
        )


def lint_pack(pack: GoalPack) -> tuple[Finding, ...]:
    """Return every finding about one goal pack, in a stable order.

    Args:
        pack: The parsed pack.

    Returns:
        Pack-level findings first, then per-criterion findings in declaration order. Nothing here
        raises: ``goals validate`` on a deliberately bad pack must name every problem with a
        severity, and an exception would name exactly one.
    """
    findings: list[Finding] = []
    _check_weights(pack, findings)
    _check_pack_level(pack, findings)
    for criterion in pack.criteria:
        if criterion.rung.is_deterministic:
            _check_rule_criterion(criterion, findings)
        else:
            _check_graded_criterion(criterion, findings)
    return tuple(findings)


def has_errors(findings: Sequence[Finding]) -> bool:
    """Whether any finding is severe enough to refuse the pack."""
    return any(finding.severity is Severity.ERROR for finding in findings)
