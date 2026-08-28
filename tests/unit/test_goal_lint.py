"""The rubric lint: every problem at once, with a severity each.

The phase's own test list names two cases, and each has a class here: the lint **fires on a judged
criterion that a ``forbidden_phrases`` rule would cover**, and it **refuses a judged criterion with
no descriptors**. Around them is the ``goals validate`` acceptance criterion — a deliberately bad
pack names *every* problem with a severity, which is why nothing in the lint raises.
"""

from __future__ import annotations

from typing import Any

import pytest

from freeweight.domain.goals.lint import Severity, has_errors, lint_pack, suggest_rules
from freeweight.domain.goals.pack import GoalPackInvalid, GoalTask, parse_pack

_TASK = GoalTask(
    key="warehouse",
    name="Warehouse night",
    prompt_id="goals.noir.warehouse",
    prompt_version="1.0.0",
    prompt_sha256="sha256:" + "ab" * 32,
    rendered_prompt_hash="sha256:" + "cd" * 32,
    prompt_text="Write about the night the inventory did not add up.",
)

_ANCHORED = {
    "points": 5,
    "descriptors": {"5": "Wry and understated.", "3": "Flat reportage.", "1": "Earnest."},
}


def _pack(criteria: list[dict[str, Any]], **changes: Any) -> Any:  # noqa: ANN401 — a GoalPack
    body: dict[str, Any] = {
        "slug": "voice",
        "name": "Voice",
        "goal_pack_version": "1.0.0",
        "criteria": criteria,
        "judge": {"jury_size": 3},
        **changes,
    }
    return parse_pack(body, tasks=changes.pop("tasks", [_TASK]))


def _codes(findings: Any, severity: Severity | None = None) -> set[str]:  # noqa: ANN401
    return {
        finding.code for finding in findings if severity is None or finding.severity is severity
    }


class TestTheLintFiresOnAMechanizableJudgedCriterion:
    """ADR-0031 §2's most valuable check: it names the rule, and it never refuses."""

    def test_a_judged_criterion_a_phrase_list_would_cover(self) -> None:
        criteria = [
            {
                "key": "not_linkedin",
                "name": "No corporate hedging",
                "rung": "judge",
                "weight": 1.0,
                "scale": _ANCHORED,
            }
        ]
        findings = lint_pack(_pack(criteria))
        mechanizable = [
            finding for finding in findings if finding.code == "MECHANIZABLE_JUDGED_CRITERION"
        ]
        assert mechanizable
        assert mechanizable[0].suggested_rule == "forbidden_phrases"
        assert mechanizable[0].criterion_key == "not_linkedin"

    def test_it_is_a_warning_and_never_refuses_the_pack(self) -> None:
        criteria = [
            {
                "key": "not_linkedin",
                "name": "No corporate buzzword jargon",
                "rung": "judge",
                "weight": 1.0,
                "scale": _ANCHORED,
            }
        ]
        findings = lint_pack(_pack(criteria))
        assert not has_errors(findings)
        assert "MECHANIZABLE_JUDGED_CRITERION" in _codes(findings, Severity.WARNING)

    def test_it_does_not_fire_on_a_criterion_no_rule_could_check(self) -> None:
        criteria = [
            {
                "key": "dry_wit",
                "name": "Dry wit",
                "rung": "judge",
                "weight": 1.0,
                "intent": "The joke is in the observation.",
                "scale": _ANCHORED,
            }
        ]
        assert "MECHANIZABLE_JUDGED_CRITERION" not in _codes(lint_pack(_pack(criteria)))

    def test_it_reads_the_scale_descriptors_too(self) -> None:
        criteria = [
            {
                "key": "shape",
                "name": "Shape",
                "rung": "judge",
                "weight": 1.0,
                "scale": {
                    "points": 5,
                    "descriptors": {
                        "5": "Uses headings and a bulleted list.",
                        "3": "Some structure.",
                        "1": "None.",
                    },
                },
            }
        ]
        suggestions = suggest_rules(_pack(criteria).criteria[0])
        assert "structure" in suggestions


class TestTheLintRefusesAJudgedCriterionWithNoDescriptors:
    """An unanchored scale reliably produces agreement near zero, so it is refused at authoring."""

    def test_no_descriptors_at_all(self) -> None:
        criteria = [
            {
                "key": "tone",
                "name": "Tone",
                "rung": "judge",
                "weight": 1.0,
                "scale": {"points": 5},
            }
        ]
        findings = lint_pack(_pack(criteria))
        assert "JUDGED_WITHOUT_DESCRIPTORS" in _codes(findings, Severity.ERROR)
        assert has_errors(findings)

    def test_fewer_than_three_descriptors(self) -> None:
        criteria = [
            {
                "key": "tone",
                "name": "Tone",
                "rung": "judge",
                "weight": 1.0,
                "scale": {"points": 5, "descriptors": {"5": "Good", "1": "Bad"}},
            }
        ]
        assert "JUDGED_WITHOUT_DESCRIPTORS" in _codes(lint_pack(_pack(criteria)), Severity.ERROR)

    def test_three_descriptors_pass(self) -> None:
        criteria = [
            {"key": "tone", "name": "Tone", "rung": "judge", "weight": 1.0, "scale": _ANCHORED}
        ]
        assert "JUDGED_WITHOUT_DESCRIPTORS" not in _codes(lint_pack(_pack(criteria)))

    def test_a_judged_criterion_with_no_scale_at_all(self) -> None:
        criteria = [{"key": "tone", "name": "Tone", "rung": "judge", "weight": 1.0}]
        assert "GRADED_WITHOUT_SCALE" in _codes(lint_pack(_pack(criteria)), Severity.ERROR)

    def test_an_even_scale_is_refused_where_the_pack_is_parsed(self) -> None:
        # Four points removes the midpoint, which is where a grader puts "this is fine".
        criteria = [
            {
                "key": "tone",
                "name": "Tone",
                "rung": "judge",
                "weight": 1.0,
                "scale": {"points": 4, "descriptors": _ANCHORED["descriptors"]},
            }
        ]
        with pytest.raises(GoalPackInvalid, match="ordinal scale"):
            _pack(criteria)


class TestValidateNamesEveryProblem:
    """Acceptance criterion 5: a deliberately bad pack names every problem with a severity."""

    _BAD = [
        {"key": "a", "name": "A", "rung": "rule", "weight": 0.3},
        {"key": "b", "name": "B", "rung": "rule", "weight": 0.3, "rule": {"type": "not_a_rule"}},
        {
            "key": "c",
            "name": "C",
            "rung": "judge",
            "weight": 0.3,
            "gate": True,
            "scale": {"points": 5},
        },
    ]

    def test_all_of_them_at_once(self) -> None:
        findings = lint_pack(_pack(self._BAD))
        assert _codes(findings, Severity.ERROR) >= {
            "WEIGHTS_DO_NOT_SUM",
            "DETERMINISTIC_WITHOUT_RULE",
            "RULE_TYPE_UNKNOWN",
            "JUDGED_WITHOUT_DESCRIPTORS",
            "GATE_ON_GRADED_CRITERION",
        }

    def test_every_finding_carries_a_severity_and_a_message(self) -> None:
        for finding in lint_pack(_pack(self._BAD)):
            assert finding.severity in set(Severity)
            assert finding.message
            assert finding.as_json()["code"] == finding.code

    def test_nothing_raises_no_matter_how_bad_the_pack_is(self) -> None:
        assert lint_pack(_pack(self._BAD))


class TestRuleCriterionFindings:
    def test_a_rung_that_does_not_match_its_rule_type(self) -> None:
        criteria = [
            {
                "key": "recall",
                "name": "Recall",
                "rung": "rule",
                "weight": 1.0,
                "rule": {"type": "entity_recall"},
            }
        ]
        assert "RUNG_MISMATCH" in _codes(lint_pack(_pack(criteria)), Severity.ERROR)

    def test_malformed_rule_parameters_are_found_by_a_probe(self) -> None:
        criteria = [
            {
                "key": "bad",
                "name": "Bad",
                "rung": "rule",
                "weight": 1.0,
                "rule": {"type": "forbidden_phrases", "phrases": []},
            }
        ]
        assert "RULE_PARAMETERS_INVALID" in _codes(lint_pack(_pack(criteria)), Severity.ERROR)

    def test_a_catastrophic_pattern_is_refused_by_the_dialect_lint(self) -> None:
        criteria = [
            {
                "key": "shape",
                "name": "Shape",
                "rung": "rule",
                "weight": 1.0,
                "rule": {"type": "regex_match", "pattern": r"(a+)+b"},
            }
        ]
        assert "RULE_PARAMETERS_INVALID" in _codes(lint_pack(_pack(criteria)), Severity.ERROR)

    def test_a_gate_on_a_gradual_rule_is_a_warning(self) -> None:
        criteria = [
            {
                "key": "reading",
                "name": "Reading level",
                "rung": "rule",
                "weight": 1.0,
                "gate": True,
                "rule": {"type": "readability", "min": 6, "max": 9},
            }
        ]
        findings = lint_pack(_pack(criteria))
        assert "GATE_ON_GRADUAL_RULE" in _codes(findings, Severity.WARNING)
        assert not has_errors(findings)

    def test_a_gate_on_a_phrase_list_is_not_flagged(self) -> None:
        criteria = [
            {
                "key": "tells",
                "name": "Tells",
                "rung": "rule",
                "weight": 1.0,
                "gate": True,
                "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
            }
        ]
        assert "GATE_ON_GRADUAL_RULE" not in _codes(lint_pack(_pack(criteria)))


class TestPackLevelFindings:
    _GOOD = [
        {
            "key": "tells",
            "name": "Tells",
            "rung": "rule",
            "weight": 1.0,
            "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
        }
    ]

    def test_no_tasks_is_an_error(self) -> None:
        assert "NO_TASKS" in _codes(lint_pack(_pack(self._GOOD, tasks=[])), Severity.ERROR)

    def test_judged_criteria_with_no_jury_configuration(self) -> None:
        body = {
            "slug": "voice",
            "name": "Voice",
            "goal_pack_version": "1.0.0",
            "criteria": [
                {"key": "wit", "name": "Wit", "rung": "judge", "weight": 1.0, "scale": _ANCHORED}
            ],
        }
        findings = lint_pack(parse_pack(body, tasks=[_TASK]))
        assert "JUDGED_WITHOUT_JUDGE_CONFIG" in _codes(findings, Severity.ERROR)

    def test_a_reference_criterion_with_no_annotated_source_is_a_warning(self) -> None:
        criteria = [
            {
                "key": "entities",
                "name": "Entities",
                "rung": "reference",
                "weight": 1.0,
                "rule": {"type": "entity_recall"},
            }
        ]
        findings = lint_pack(_pack(criteria))
        assert "REFERENCE_WITHOUT_GROUND_TRUTH" in _codes(findings, Severity.WARNING)
        assert not has_errors(findings)

    def test_the_deterministic_share_is_always_reported(self) -> None:
        findings = lint_pack(_pack(self._GOOD))
        info = next(finding for finding in findings if finding.code == "DETERMINISTIC_WEIGHT_SHARE")
        assert "100%" in info.message
        assert info.severity is Severity.INFO

    def test_an_unforked_starter_is_badged(self) -> None:
        findings = lint_pack(_pack(self._GOOD, unforked=True))
        assert "UNFORKED_STARTER" in _codes(findings, Severity.WARNING)

    def test_a_clean_pack_carries_only_the_informational_finding(self) -> None:
        findings = lint_pack(_pack(self._GOOD))
        assert _codes(findings) == {"DETERMINISTIC_WEIGHT_SHARE"}


class TestThePackParserRefuses:
    """Shape errors, raised where the pack is read rather than collected as findings.

    A pack that cannot be *parsed* cannot be linted either, so these are the one class of problem
    that raises. Everything a reader could still act on is a finding with a severity; everything
    here would have left the parser with nothing to describe.
    """

    def _body(self, **changes: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "slug": "voice",
            "name": "Voice",
            "goal_pack_version": "1.0.0",
            "criteria": [
                {
                    "key": "tells",
                    "name": "Tells",
                    "rung": "rule",
                    "weight": 1.0,
                    "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
                }
            ],
        }
        body.update(changes)
        return body

    @pytest.mark.parametrize(
        ("slug", "message"),
        [
            ("", "must match"),
            ("Voice", "must match"),
            ("1voice", "must match"),
            ("voice-name", "must match"),
            ("../etc", "must match"),
            ("reasoning", "shipped capability root"),
        ],
    )
    def test_an_unusable_slug(self, slug: str, message: str) -> None:
        with pytest.raises(GoalPackInvalid, match=message):
            parse_pack(self._body(slug=slug), tasks=[_TASK])

    def test_a_schema_version_this_build_does_not_speak(self) -> None:
        with pytest.raises(GoalPackInvalid, match="this build speaks"):
            parse_pack(self._body(schema_version="2.0"), tasks=[_TASK])

    def test_no_criteria_at_all(self) -> None:
        with pytest.raises(GoalPackInvalid, match="not a benchmark"):
            parse_pack(self._body(criteria=[]), tasks=[_TASK])

    def test_a_criterion_that_is_not_an_object(self) -> None:
        with pytest.raises(GoalPackInvalid, match="not an object"):
            parse_pack(self._body(criteria=["tells"]), tasks=[_TASK])

    def test_two_criteria_sharing_a_key(self) -> None:
        criterion = self._body()["criteria"][0]
        with pytest.raises(GoalPackInvalid, match="more than once"):
            parse_pack(
                self._body(criteria=[criterion, {**criterion, "weight": 0.5}]), tasks=[_TASK]
            )

    def test_a_criterion_with_no_key(self) -> None:
        with pytest.raises(GoalPackInvalid, match="anonymous"):
            parse_pack(self._body(criteria=[{"rung": "rule", "weight": 1.0}]), tasks=[_TASK])

    def test_a_criterion_with_an_unknown_rung(self) -> None:
        with pytest.raises(GoalPackInvalid, match="the ladder's rungs"):
            parse_pack(
                self._body(criteria=[{"key": "a", "rung": "vibes", "weight": 1.0}]), tasks=[_TASK]
            )

    @pytest.mark.parametrize("weight", ["heavy", 0.0, -0.5, 1.5, True])
    def test_a_criterion_with_an_unusable_weight(self, weight: object) -> None:
        with pytest.raises(GoalPackInvalid):
            parse_pack(
                self._body(criteria=[{"key": "a", "rung": "rule", "weight": weight}]),
                tasks=[_TASK],
            )

    def test_a_rule_with_no_type(self) -> None:
        with pytest.raises(GoalPackInvalid, match="no 'type'"):
            parse_pack(
                self._body(
                    criteria=[{"key": "a", "rung": "rule", "weight": 1.0, "rule": {"x": 1}}]
                ),
                tasks=[_TASK],
            )

    def test_an_unknown_judged_mode(self) -> None:
        with pytest.raises(GoalPackInvalid, match="'absolute' or 'pairwise'"):
            parse_pack(
                self._body(
                    criteria=[
                        {
                            "key": "a",
                            "rung": "judge",
                            "weight": 1.0,
                            "mode": "vibes",
                            "scale": _ANCHORED,
                        }
                    ]
                ),
                tasks=[_TASK],
            )

    def test_a_non_object_descriptor_block(self) -> None:
        with pytest.raises(GoalPackInvalid, match="non-object scale.descriptors"):
            parse_pack(
                self._body(
                    criteria=[
                        {
                            "key": "a",
                            "rung": "judge",
                            "weight": 1.0,
                            "scale": {"points": 5, "descriptors": ["top", "middle", "bottom"]},
                        }
                    ]
                ),
                tasks=[_TASK],
            )

    def test_a_created_at_that_is_not_a_timestamp(self) -> None:
        with pytest.raises(GoalPackInvalid, match="RFC 3339"):
            parse_pack(self._body(created_at="last Tuesday"), tasks=[_TASK])

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            ({"jury_size": 0}, "positive whole number"),
            ({"repetitions": "three"}, "positive whole number"),
            ({"models": "qwen"}, "list of canonical model IDs"),
        ],
    )
    def test_a_malformed_judge_block(self, block: dict[str, Any], message: str) -> None:
        with pytest.raises(GoalPackInvalid, match=message):
            parse_pack(self._body(judge=block), tasks=[_TASK])

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            ({"target_samples": 0}, "positive whole number"),
            ({"min_samples": 20, "target_samples": 12}, "is above"),
            ({"holdout_fraction": 1.0}, "above 0 and below 1"),
            ({"holdout_fraction": "half"}, "must be a number"),
            ({"partition_seed": "zero"}, "whole number"),
            ({"min_agreement": 2.0}, "within -1..1"),
            ({"min_agreement": "high"}, "must be a number"),
        ],
    )
    def test_a_malformed_calibration_block(self, block: dict[str, Any], message: str) -> None:
        with pytest.raises(GoalPackInvalid, match=message):
            parse_pack(self._body(calibration=block), tasks=[_TASK])

    def test_an_unknown_contributes_to_is_a_finding_rather_than_a_refusal(self) -> None:
        # It is a pack the parser can read; naming it as an error finding is what lets
        # ``goals validate`` report it alongside everything else.
        pack = parse_pack(self._body(contributes_to="vibes"), tasks=[_TASK])
        assert "CONTRIBUTES_TO_UNKNOWN" in _codes(lint_pack(pack), Severity.ERROR)

    def test_a_known_contributes_to_passes(self) -> None:
        pack = parse_pack(self._body(contributes_to="creative_writing"), tasks=[_TASK])
        assert "CONTRIBUTES_TO_UNKNOWN" not in _codes(lint_pack(pack))
