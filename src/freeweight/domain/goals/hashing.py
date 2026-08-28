"""freeweight.domain.goals.hashing — ``goal_hash``, over the measurement-defining subset only.

[Subjective Goals §2.2](../../../../../docs/apps/freeweight/subjective-goals.md) states the rule in
one sentence: *renaming a criterion for readability must not separate a year of results; changing
what it checks must*. This module is that sentence as code, and it is the module whose failure
modes are hardest to see, so both are named here:

* **Over-covering fragments history.** A hash that included display names or ``intent`` would
  separate a goal's results the first time its author fixed a typo, leaving two half-populated
  series that can never be merged.
* **Under-covering merges different measurements.** A hash that ignored the jury, or the scale's
  descriptors, would let two genuinely different instruments write into one series — the failure
  ADR-0032 §4 makes a *hard separation* precisely to prevent.

**What is inside**, and nothing else: each criterion's key, rung, weight, gate flag, rule
parameters, scale points and scale descriptors, and its judged mode; each task's key and its
prompt record's hash; and the whole jury configuration including the judge prompt's own hash.

**What is outside**: display names, ``intent``, ``contributes_to``, the grader's identity, the
calibration policy and the calibration grades. The last two are the subtle ones and they are
deliberate — a gate threshold is *policy*, recorded on the report with its policy version, and
re-grading refines the instrument's characterization without changing what is measured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baseaicore import canonical_json, sha256_of

if TYPE_CHECKING:
    from freeweight.domain.goals.pack import Criterion, GoalPack

__all__ = ["compute_goal_hash", "hashable_document"]


def _criterion_document(criterion: Criterion) -> dict[str, Any]:
    """Return the measurement-defining part of one criterion.

    ``name`` and ``intent`` are absent by construction rather than by filtering: a future field
    added to :class:`~freeweight.domain.goals.pack.Criterion` does not silently enter the hash,
    because nothing here enumerates the object's attributes.
    """
    return {
        "key": criterion.key,
        "rung": criterion.rung.value,
        "weight": criterion.weight,
        "gate": criterion.is_gate,
        "rule": dict(criterion.rule) if criterion.rule is not None else None,
        "scale": (
            None
            if criterion.scale is None
            else {
                "points": criterion.scale.points,
                "descriptors": dict(criterion.scale.descriptors),
            }
        ),
        "mode": criterion.mode,
    }


def hashable_document(pack: GoalPack, *, judge_prompt_sha256: str | None = None) -> dict[str, Any]:
    """Return exactly what :func:`compute_goal_hash` hashes.

    Exposed rather than inlined because the UI has to answer "what would this edit change?" before
    the edit is applied, and diffing two of these documents is how it does that (acceptance
    criterion 4). A hash a user cannot see the inputs of is a hash they cannot trust.

    Args:
        pack: The goal.
        judge_prompt_sha256: The judge prompt record's canonical hash, or ``None`` when the goal
            has no jury. A goal with no judged criterion carries no ``judge`` section at all, so
            shipping a judge prompt later cannot move a rules-only goal's hash.

    Returns:
        The document, JSON-safe and in a stable key order.
    """
    return {
        "schema_version": pack.schema_version,
        "criteria": [_criterion_document(criterion) for criterion in pack.criteria],
        "tasks": [
            {
                "key": task.key,
                "prompt_id": task.prompt_id,
                "prompt_version": task.prompt_version,
                "prompt_sha256": task.prompt_sha256,
            }
            for task in pack.tasks
        ],
        "judge": (
            None
            if pack.judge is None
            else {**pack.judge.as_json(), "prompt_sha256": judge_prompt_sha256 or ""}
        ),
    }


def compute_goal_hash(pack: GoalPack, *, judge_prompt_sha256: str | None = None) -> str:
    """Return the ``sha256:``-prefixed hash of a goal's measurement-defining content.

    Over :func:`~baseaicore.canonical_json`, not over the pack's bytes: reformatting ``goal.json``
    or reordering its keys must not separate a goal's results from the ones it produced yesterday,
    and hashing the file would do exactly that.

    Args:
        pack: The goal.
        judge_prompt_sha256: The judge prompt record's hash, or ``None``.

    Returns:
        ``"sha256:"`` followed by 64 lowercase hex characters.
    """
    document = hashable_document(pack, judge_prompt_sha256=judge_prompt_sha256)
    return f"sha256:{sha256_of(canonical_json(document))}"
