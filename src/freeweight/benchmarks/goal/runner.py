"""freeweight.benchmarks.goal.runner — a user's goal pack, as a runnable benchmark.

[ADR-0031 §1](../../../../../docs/adr/0031-user-defined-goal-benchmarks.md) makes a goal suite
first-class in every respect that matters: a manifest, a version, a hash, metric definitions, a
place in the run engine, raw samples that every headline number drills to, and a row in the
comparison UI. This module builds all of that from a loaded pack.

**A goal suite's version carries its hash.** ``benchmark_suites`` is keyed by ``(key, version)``
and a suite version is immutable, so a goal whose author edited a criterion without bumping
``goal_pack_version`` would otherwise reuse the previous version's row and attribute new results to
the old manifest. The version is therefore ``<goal_pack_version>+<first 8 hex of goal_hash>``,
which makes ADR-0032 §4's hard separation structural: a measurement-defining edit *cannot* land in
the previous version's series, because it has a different version.

**The scorer is not pure, and that is the one place in this application where it is not.**
:class:`GoalScorer` holds an optional jury. A criterion at rung 5 is scored by models, which is
exactly what :mod:`freeweight.domain.scoring`'s protocol docstring anticipates when it says "a
scorer that needs a model is a rung-5 judge, and rung 5 has its own machinery and its own
calibration precondition". Everything at rungs 2 and 3 stays a pure function, and a goal with no
judged criterion never touches the jury at all — which is why such a goal runs with the provider
down.

**Skipping is never zero.** A judged criterion with no jury, a rule that could not read the
response, a rule that timed out — each produces a criterion outcome with ``raw_score = NULL``, is
excluded from the composite, and leaves its exclusion visible in ``applied_weight``
([ADR-0016](../../../../../docs/adr/0016-unavailable-is-not-zero.md)).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from freeweight.benchmarks.loading import SuiteBenchmark, SuiteTest
from freeweight.domain.benchmark import BenchmarkCase, BenchmarkManifest, MetricDefinition
from freeweight.domain.goals.composite import composite_score
from freeweight.domain.goals.criteria import (
    DEFAULT_RULE_TIMEOUT_MS,
    CriterionOutcome,
    CriterionStatus,
    SkipReason,
    score_criterion,
)
from freeweight.domain.goals.pack import Rung
from freeweight.domain.scoring import ScoreMethod, ScoreResult
from freeweight.services.prompts import prompt_subset_hash

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.domain.goals.pack import Criterion, GoalPack
    from freeweight.services.goals import LoadedGoal

__all__ = [
    "CATEGORY",
    "GOAL_TEST_KEY",
    "GoalScorer",
    "JudgeCollaborator",
    "build_goal_benchmark",
    "goal_suite_key",
    "goal_suite_version",
]

CATEGORY = "user_defined_goals"
"""The benchmark catalog §2 category every goal suite contributes to."""

GOAL_TEST_KEY = "goal"
"""The one test a goal suite declares.

One test, one case per task. A goal's criteria all apply to every task, so splitting tasks across
tests would give each of them its own aggregate rows and no goal-level number — which is the
opposite of what a composite is for."""

ERROR_NO_CRITERION_SCORED = "GOAL_UNMEASURED"
"""Every criterion was skipped or errored, so this sample carries no measurement."""

_HASH_PREFIX = len("sha256:")
_VERSION_HASH_CHARACTERS = 8


@runtime_checkable
class JudgeCollaborator(Protocol):
    """Scores a goal's judged criteria. Supplied by the service layer, absent at Phase 8A.

    The seam that keeps :mod:`freeweight.domain.goals` free of providers: everything about *how* a
    jury is assembled, blinded, ordered and polled lives behind this one call.
    """

    def score_judged(
        self,
        *,
        criteria: Sequence[Criterion],
        response_text: str,
        case: BenchmarkCase,
    ) -> Sequence[CriterionOutcome]:
        """Return one outcome per judged criterion, in the order given.

        Never raises: a provider failure is a skipped criterion with a recorded reason, because a
        jury that could not be reached must not discard the rule criteria that scored fine.
        """
        ...


def goal_suite_key(slug: str) -> str:
    """Return the ``benchmark_suites.key`` a goal is installed under."""
    return f"goal.{slug}"


def goal_suite_version(goal_pack_version: str, goal_hash: str) -> str:
    """Return the suite version for one goal pack, hash included.

    Args:
        goal_pack_version: The pack's own semantic version.
        goal_hash: Its measurement-defining hash.

    Returns:
        ``"<version>+<8 hex>"``. See the module docstring for why the hash is in the version.
    """
    digest = goal_hash[_HASH_PREFIX:] if goal_hash.startswith("sha256:") else goal_hash
    return f"{goal_pack_version}+{digest[:_VERSION_HASH_CHARACTERS]}"


def _metrics(pack: GoalPack) -> list[dict[str, Any]]:
    """Declare the metrics a goal suite produces (benchmark catalog §7.4).

    ``composite_score`` is the headline and comes from the sample's score. Everything else is
    ``source = "detail"``: a rate the scorer did not measure for any sample must be *absent*
    rather than quietly reported as the composite under another name.
    """
    declared: list[dict[str, Any]] = [
        {
            "key": "composite_score",
            "unit": "ratio",
            "higher_is_better": True,
            "aggregation": "mean",
            "source": "score",
            "description": "The weighted composite across this goal's criteria, gates applied.",
        },
        {
            "key": "gated_sample_rate",
            "unit": "ratio",
            "higher_is_better": False,
            "aggregation": "mean",
            "source": "detail",
            "description": "Share of samples zeroed by a hard gate. The gate that fired is named "
            "on the sample.",
        },
        {
            "key": "applied_weight_share",
            "unit": "ratio",
            "higher_is_better": True,
            "aggregation": "mean",
            "source": "detail",
            "description": "Share of the rubric's declared weight that actually contributed. "
            "Below 1.0 means criteria were skipped, and the sample says which.",
        },
        {
            "key": "judge_validity_factor",
            "unit": "ratio",
            "higher_is_better": True,
            "aggregation": "mean",
            "source": "detail",
            "description": "The sixth confidence factor (ADR-0032 §2). 1.0 for a goal scored "
            "entirely at rungs 1-4.",
        },
    ]
    declared.extend(
        {
            "key": f"score_method_mix_{rung.value}",
            "unit": "ratio",
            "higher_is_better": True,
            "aggregation": "mean",
            "source": "detail",
            "description": f"Share of applied weight scored at rung {rung.value!r}. Shown beside "
            "the score, never instead of it.",
        }
        for rung in Rung
    )
    declared.extend(
        {
            "key": f"criterion.{criterion.key}",
            "unit": "ratio",
            "higher_is_better": True,
            "aggregation": "mean",
            "source": "detail",
            "description": f"{criterion.name} — scored at rung {criterion.rung.value!r}, weight "
            f"{criterion.weight}.",
        }
        for criterion in pack.criteria
    )
    return declared


def _manifest(
    goal: LoadedGoal, *, prompt_hash: str, references: Sequence[Mapping[str, str]]
) -> BenchmarkManifest:
    """Build the manifest a goal suite installs under."""
    pack = goal.pack
    capabilities = [pack.capability_id]
    if pack.contributes_to:
        capabilities.append(pack.contributes_to)
    body: dict[str, Any] = {
        "key": goal_suite_key(pack.slug),
        "name": pack.name,
        "version": goal_suite_version(pack.goal_pack_version, goal.goal_hash),
        "category": CATEGORY,
        "runner": "goal",
        "scorer": "goal_composite",
        "capabilities": capabilities,
        "requires": {"provider_capabilities": [], "sandbox": False, "network": False},
        "dataset_hashes": {},
        "prompt_ids": [dict(reference) for reference in references],
        "prompt_subset_hash": prompt_hash,
        "target_device": "any",
        "metrics": _metrics(pack),
        "license": "user",
        "goal_slug": pack.slug,
        "goal_hash": goal.goal_hash,
        "goal_pack_version": pack.goal_pack_version,
        "unforked": pack.unforked,
        "description": pack.intent or f"User-authored goal {pack.slug!r}.",
    }
    return BenchmarkManifest.from_json(body)


@dataclass(frozen=True, slots=True)
class GoalScorer:
    """Scores one response against a goal's criteria and combines them into a composite.

    Args:
        pack: The goal.
        rule_timeout_ms: How long each deterministic criterion may take on each sample.
        judge: The jury, or ``None``. Absent, judged criteria are skipped with
            ``judge_unavailable`` and the rule criteria still score — spec §13's rule, which is
            what lets a goal run at all when no provider can serve a jury.
        judge_validity_factor: The goal's calibrated validity factor, written onto every sample so
            a result carries the factor that will multiply into its confidence. ``1.0`` for a goal
            with no judged criterion, which is every goal at Phase 8A.
    """

    pack: GoalPack
    rule_timeout_ms: int = DEFAULT_RULE_TIMEOUT_MS
    judge: JudgeCollaborator | None = None
    judge_validity_factor: float = 1.0
    key: str = "goal_composite"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Score every criterion, combine them, and report the mix beside the number.

        Args:
            case: The case, whose ``metadata`` carries the task's annotated source.
            response_text: Exactly what the model returned.

        Returns:
            ``score`` is the composite. ``detail`` carries every criterion's own score, the gate
            that fired, the applied and declared weights, and ``score_method_mix``.
            ``score=None`` with :data:`ERROR_NO_CRITERION_SCORED` when nothing could be measured —
            an unmeasured sample, not a bad one.
        """
        source = case.metadata.get("goal_source")
        outcomes: list[CriterionOutcome] = []
        judged: list[Criterion] = []
        for criterion in self.pack.criteria:
            if criterion.rung.is_deterministic:
                outcomes.append(
                    score_criterion(
                        criterion,
                        response_text,
                        source=source if isinstance(source, dict) else None,
                        timeout_ms=self.rule_timeout_ms,
                    )
                )
            elif criterion.rung is Rung.JUDGE:
                judged.append(criterion)
                outcomes.append(_unscored(criterion, SkipReason.JUDGE_UNAVAILABLE))
            else:
                outcomes.append(_unscored(criterion, SkipReason.HUMAN_GRADE_PENDING))
        if judged and self.judge is not None:
            scored = {
                outcome.criterion_key: outcome
                for outcome in self.judge.score_judged(
                    criteria=judged, response_text=response_text, case=case
                )
            }
            outcomes = [scored.get(outcome.criterion_key, outcome) for outcome in outcomes]

        composite = composite_score(outcomes)
        detail: dict[str, Any] = {
            "case": case.case_id,
            **composite.as_detail(),
            "gated_sample_rate": 1.0 if composite.gated_by is not None else 0.0,
            "applied_weight_share": (
                composite.applied_weight / composite.declared_weight
                if composite.declared_weight
                else 0.0
            ),
            "judge_validity_factor": self.judge_validity_factor,
            **{
                f"score_method_mix_{rung}": share
                for rung, share in composite.score_method_mix.items()
            },
            **{
                f"criterion.{outcome.criterion_key}": float(outcome.raw_score)
                for outcome in outcomes
                if outcome.raw_score is not None
            },
        }
        if composite.composite is None:
            return ScoreResult(
                score=None,
                method=self.method,
                detail=detail,
                error_code=ERROR_NO_CRITERION_SCORED,
                error_text=(
                    f"No criterion of goal {self.pack.slug!r} could be measured on case "
                    f"{case.case_id!r}; every one was skipped or errored."
                ),
            )
        return ScoreResult(
            score=composite.composite,
            method=_dominant_method(composite.score_method_mix),
            detail=detail,
        )


def _unscored(criterion: Criterion, reason: SkipReason) -> CriterionOutcome:
    """Return the outcome of a criterion nothing scored."""
    return CriterionOutcome(
        criterion_key=criterion.key,
        rung=criterion.rung,
        weight=criterion.weight,
        status=CriterionStatus.SKIPPED,
        skip_reason=reason.value,
        detail={},
    )


_TIE_BREAK: Mapping[str, int] = {
    Rung.JUDGE.value: 0,
    Rung.HUMAN.value: 1,
    Rung.REFERENCE.value: 2,
    Rung.RULE.value: 3,
}
"""How ``score_method`` breaks a tie: towards the *lower* rung.

A sample split evenly between rules and judgement is labelled ``judge``. Deliberately, and not
alphabetically: the single stored value is a summary, and a summary that understated how much
judgement went into a number would be the wrong direction to round in. The whole blend is on the
same row as ``score_method_mix``, which is what the UI actually shows."""


def _dominant_method(mix: Mapping[str, float]) -> ScoreMethod:
    """Return the ladder rung that carried most of this sample's applied weight.

    ``samples.score_method`` holds one value, and a composite is by construction a blend. The
    honest single value is the rung that carried most of it; the *whole* blend is on the same row
    in ``result_json`` as ``score_method_mix``, which is what the UI shows and what
    :mod:`freeweight.domain.aggregation` reports as its own metrics. Ties go to the lower rung
    (:data:`_TIE_BREAK`).
    """
    ranked = sorted(mix.items(), key=lambda item: (-item[1], _TIE_BREAK.get(item[0], 9)))
    if not ranked or ranked[0][1] <= 0:
        return ScoreMethod.RULE
    return ScoreMethod(ranked[0][0])


def build_goal_benchmark(
    goal: LoadedGoal,
    *,
    rule_timeout_ms: int = DEFAULT_RULE_TIMEOUT_MS,
    judge: JudgeCollaborator | None = None,
    judge_validity_factor: float = 1.0,
) -> SuiteBenchmark:
    """Turn one loaded goal pack into a runnable benchmark.

    Args:
        goal: The loaded pack.
        rule_timeout_ms: ``goals.rule_timeout_ms``.
        judge: The jury, or ``None`` for a run with no judging in it.
        judge_validity_factor: The goal's calibrated validity factor.

    Returns:
        The benchmark: one test, one case per task.

    Raises:
        ValueError: The pack declares no tasks. A goal with no task produces no output to score,
            and installing it would create a suite whose every run measured nothing.
    """
    pack = goal.pack
    if not pack.tasks:
        raise ValueError(
            f"Goal {pack.slug!r} declares no tasks; there would be no output to score."
        )
    references = [
        {
            "prompt_id": task.prompt_id,
            "version": task.prompt_version,
            "sha256": task.prompt_sha256,
        }
        for task in pack.tasks
    ]
    if goal.judge_prompt is not None:
        references.append(goal.judge_prompt.reference.as_json())
    from freeweight.services.prompts import PromptReference

    prompt_hash = prompt_subset_hash(
        PromptReference(
            prompt_id=str(entry["prompt_id"]),
            version=str(entry["version"]),
            sha256=str(entry["sha256"]),
        )
        for entry in references
    )
    manifest = _manifest(goal, prompt_hash=prompt_hash, references=references)
    scorer = GoalScorer(
        pack=pack,
        rule_timeout_ms=rule_timeout_ms,
        judge=judge,
        judge_validity_factor=judge_validity_factor,
    )
    cases = tuple(
        BenchmarkCase(
            case_id=task.key,
            ordinal=ordinal,
            prompt=task.prompt_text,
            system_prompt=task.system_prompt,
            prompt_id=task.prompt_id,
            prompt_version=task.prompt_version,
            expectation={},
            metadata={
                "goal": pack.slug,
                "task": task.key,
                "is_starter": task.is_starter,
                "goal_source": dict(task.source) if task.source else None,
            },
        )
        for ordinal, task in enumerate(pack.tasks)
    )
    definitions = tuple(
        MetricDefinition(
            key=str(entry["key"]),
            unit=str(entry["unit"]),
            higher_is_better=bool(entry["higher_is_better"]),
            aggregation=str(entry["aggregation"]),
            description=str(entry.get("description", "")),
            source=str(entry.get("source", "auto")),
        )
        for entry in manifest.body["metrics"]
    )
    test = SuiteTest(
        key=GOAL_TEST_KEY,
        name=pack.name,
        category=CATEGORY,
        measurement_class="warm",
        streaming=False,
        metrics=definitions,
        requires=dict(manifest.requires),
        scorer=scorer,
        interaction=None,
        declared_cases=cases,
    )
    return SuiteBenchmark(manifest=manifest, tests=(test,))
