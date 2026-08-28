"""``goal_hash``: both directions asserted, because one direction alone is useless.

The phase's own test list is explicit: the hash **changes** when a criterion's rule parameters,
weight, rung or scale descriptors change, and **does not change** when a display name, ``intent``
or ``contributes_to`` changes. A hash that never changes and a hash that always changes are equally
useless, and only asserting both distinguishes a correct one from either.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from freeweight.domain.goals.hashing import compute_goal_hash, hashable_document
from freeweight.domain.goals.pack import GoalTask, parse_pack

_TASK = GoalTask(
    key="warehouse",
    name="Warehouse night",
    prompt_id="goals.noir.warehouse",
    prompt_version="1.0.0",
    prompt_sha256="sha256:" + "ab" * 32,
    rendered_prompt_hash="sha256:" + "cd" * 32,
    prompt_text="Write about the night the inventory did not add up.",
)

_BODY: dict[str, Any] = {
    "slug": "noir_tech_voice",
    "name": "Noir-ish tech essay voice",
    "goal_pack_version": "1.0.0",
    "schema_version": "1.0",
    "intent": "Dry, concrete, unhurried.",
    "contributes_to": "creative_writing",
    "created_by": "jpk",
    "criteria": [
        {
            "key": "no_llm_tells",
            "name": "No LLM tells",
            "rung": "rule",
            "weight": 0.6,
            "gate": True,
            "rule": {"type": "forbidden_phrases", "phrases": ["delve", "tapestry"]},
        },
        {
            "key": "dry_wit",
            "name": "Dry wit, never winking",
            "rung": "judge",
            "weight": 0.4,
            "scale": {
                "points": 5,
                "descriptors": {"5": "Wry and understated.", "3": "Flat.", "1": "Earnest."},
            },
        },
    ],
    "judge": {"jury_size": 3, "models": [], "repetitions": 3, "allow_remote": False},
    "calibration": {"min_agreement": 0.4},
}


def _hash(**changes: Any) -> str:
    body = {**_BODY, **changes}
    return compute_goal_hash(
        parse_pack(body, tasks=[_TASK]), judge_prompt_sha256="sha256:" + "ef" * 32
    )


def _with_criterion(index: int, **changes: Any) -> str:
    criteria = [dict(entry) for entry in _BODY["criteria"]]
    criteria[index] = {**criteria[index], **changes}
    return _hash(criteria=criteria)


BASELINE = _hash()


class TestTheHashChangesWhenTheMeasurementChanges:
    """Under-covering merges two different instruments into one series."""

    def test_a_rule_parameter(self) -> None:
        changed = _with_criterion(
            0, rule={"type": "forbidden_phrases", "phrases": ["delve", "tapestry", "leverage"]}
        )
        assert changed != BASELINE

    def test_a_weight(self) -> None:
        criteria = [dict(entry) for entry in _BODY["criteria"]]
        criteria[0]["weight"] = 0.7
        criteria[1]["weight"] = 0.3
        assert _hash(criteria=criteria) != BASELINE

    def test_a_rung(self) -> None:
        changed = _with_criterion(
            1,
            rung="rule",
            scale=None,
            rule={"type": "forbidden_phrases", "phrases": ["obviously"]},
        )
        assert changed != BASELINE

    def test_a_scale_descriptor(self) -> None:
        changed = _with_criterion(
            1,
            scale={
                "points": 5,
                "descriptors": {"5": "Wry, and never announced.", "3": "Flat.", "1": "Earnest."},
            },
        )
        assert changed != BASELINE

    def test_the_number_of_scale_points(self) -> None:
        changed = _with_criterion(
            1,
            scale={
                "points": 7,
                "descriptors": {"7": "Wry and understated.", "4": "Flat.", "1": "Earnest."},
            },
        )
        assert changed != BASELINE

    def test_the_gate_flag(self) -> None:
        assert _with_criterion(0, gate=False) != BASELINE

    def test_a_criterion_key(self) -> None:
        assert _with_criterion(0, key="no_tells") != BASELINE

    def test_a_task_s_prompt(self) -> None:
        other = replace(_TASK, prompt_sha256="sha256:" + "99" * 32)
        assert compute_goal_hash(parse_pack(_BODY, tasks=[other])) != BASELINE

    def test_the_jury_configuration(self) -> None:
        assert _hash(judge={**_BODY["judge"], "jury_size": 5}) != BASELINE

    def test_the_judge_prompt(self) -> None:
        pack = parse_pack(_BODY, tasks=[_TASK])
        first = compute_goal_hash(pack, judge_prompt_sha256="sha256:" + "ef" * 32)
        second = compute_goal_hash(pack, judge_prompt_sha256="sha256:" + "11" * 32)
        assert first != second


class TestTheHashDoesNotChangeWhenOnlyTheDescriptionDoes:
    """Over-covering fragments a year of results the first time a typo is fixed."""

    def test_a_criterion_display_name(self) -> None:
        assert _with_criterion(0, name="No LLM giveaways") == BASELINE

    def test_a_criterion_intent_note(self) -> None:
        assert _with_criterion(0, intent="This is the one I care about most.") == BASELINE

    def test_the_goal_display_name(self) -> None:
        assert _hash(name="Something else entirely") == BASELINE

    def test_the_goal_intent(self) -> None:
        assert _hash(intent="Rewritten six months later.") == BASELINE

    def test_contributes_to(self) -> None:
        assert _hash(contributes_to=None) == BASELINE

    def test_the_grader_identity(self) -> None:
        assert _hash(created_by="somebody else") == BASELINE

    def test_the_pack_version(self) -> None:
        # The pack version is metadata about the pack; the hash is over the measurement.
        assert _hash(goal_pack_version="2.0.0") == BASELINE

    def test_the_calibration_policy(self) -> None:
        # Gate parameters are policy, recorded on the report with the policy version. Re-grading
        # refines the instrument's characterization without changing what is measured.
        assert _hash(calibration={"min_agreement": 0.6, "target_samples": 20}) == BASELINE

    def test_a_task_s_display_name(self) -> None:
        renamed = replace(_TASK, name="A different label")
        assert (
            compute_goal_hash(
                parse_pack(_BODY, tasks=[renamed]), judge_prompt_sha256="sha256:" + "ef" * 32
            )
            == BASELINE
        )


class TestTheHashableDocument:
    """Exposed so the UI can say what an edit would change *before* it is applied."""

    def test_it_holds_only_the_measurement_defining_sections(self) -> None:
        document = hashable_document(parse_pack(_BODY, tasks=[_TASK]))
        assert set(document) == {"schema_version", "criteria", "tasks", "judge"}

    def test_no_criterion_carries_its_display_name(self) -> None:
        document = hashable_document(parse_pack(_BODY, tasks=[_TASK]))
        for criterion in document["criteria"]:
            assert "name" not in criterion
            assert "intent" not in criterion

    def test_a_goal_with_no_jury_carries_no_judge_section(self) -> None:
        # Which is why shipping a judge prompt later cannot move a rules-only goal's hash.
        body = {
            **_BODY,
            "criteria": [_BODY["criteria"][0] | {"weight": 1.0}],
            "judge": None,
        }
        document = hashable_document(parse_pack(body, tasks=[_TASK]))
        assert document["judge"] is None
        pack = parse_pack(body, tasks=[_TASK])
        assert compute_goal_hash(pack) == compute_goal_hash(
            pack, judge_prompt_sha256="sha256:" + "ef" * 32
        )

    def test_the_hash_is_stable_across_key_order(self) -> None:
        reordered = {key: _BODY[key] for key in reversed(list(_BODY))}
        assert _hash(**reordered) == BASELINE

    def test_the_hash_is_prefixed_and_hex(self) -> None:
        assert BASELINE.startswith("sha256:")
        assert len(BASELINE) == len("sha256:") + 64  # noqa: PLR2004 — a sha256's own width
        int(BASELINE.removeprefix("sha256:"), 16)


def test_two_identical_packs_hash_identically() -> None:
    assert _hash() == _hash()


@pytest.mark.parametrize("field", ["name", "intent", "created_by"])
def test_no_descriptive_field_moves_the_hash(field: str) -> None:
    assert _hash(**{field: "changed"}) == BASELINE
