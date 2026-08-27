"""The rule scorer: every constraint kind, the three accuracies, and the violation classes.

Benchmark catalog §3.4. The assertions that matter most are the ones about *separation*: strict
and loose accuracy must be able to disagree, instruction-level accuracy must distinguish one
broken rule from five, and a malformed constraint must be unscoreable rather than a model failure —
otherwise a typo in a fixture is reported as a model that cannot follow instructions.
"""

from __future__ import annotations

import pytest

from freeweight.domain.benchmark import BenchmarkCase
from freeweight.domain.scorers.rule import (
    Constraint,
    ConstraintInvalid,
    ConstraintKind,
    RuleScorer,
    ViolationClass,
    loose_text,
)


def _case(*constraints: dict[str, object]) -> BenchmarkCase:
    return BenchmarkCase(
        case_id="case-1", ordinal=0, prompt="prompt", expectation={"constraints": list(constraints)}
    )


class TestEachConstraintKind:
    """One passing and one failing response per kind, all decided by counting or matching."""

    @pytest.mark.parametrize(
        ("declaration", "passing", "failing"),
        [
            ({"kind": "required_phrase", "value": "cache"}, "the cache is warm", "it is warm"),
            ({"kind": "forbidden_phrase", "value": "GPU"}, "a graphics card", "a GPU"),
            ({"kind": "word_count_range", "minimum": 2, "maximum": 3}, "two three words", "one"),
            ({"kind": "line_count", "minimum": 2, "maximum": 2}, "a\nb", "a\nb\nc"),
            (
                {"kind": "list_length", "value": "^-\\s+", "minimum": 2, "maximum": 2},
                "- a\n- b",
                "- a",
            ),
            ({"kind": "starts_with", "value": "RESULT:"}, "RESULT: ok", "ok RESULT:"),
            ({"kind": "ends_with", "value": "END"}, "done END", "END done"),
            ({"kind": "matches", "value": "[0-9]+\\.[0-9]+"}, "version 2.4", "version two"),
            ({"kind": "every_line_matches", "value": "^[0-9]+\\. "}, "1. a\n2. b", "1. a\nb"),
            ({"kind": "script", "value": "GREEK"}, "καλημέρα", "good morning"),
        ],
    )
    def test_pass_and_fail(
        self, declaration: dict[str, object], passing: str, failing: str
    ) -> None:
        case = _case(declaration)
        assert RuleScorer().score(case, passing).score == 1.0
        assert RuleScorer().score(case, failing).score == 0.0

    def test_a_response_with_no_letters_fails_a_script_constraint(self) -> None:
        # Boundary: "every letter is Greek" is vacuously true of a response with no letters, and
        # a scorer that returned 1.0 there would pass an empty answer.
        assert RuleScorer().score(_case({"kind": "script", "value": "GREEK"}), "123").score == 0.0


class TestTheThreeAccuracies:
    """Strict, loose and instruction-level answer three different questions."""

    def test_strict_and_loose_disagree_about_a_fenced_answer(self) -> None:
        case = _case({"kind": "starts_with", "value": "RESULT:", "case_sensitive": True})
        verdict = RuleScorer().score(case, "```\nRESULT: ok\n```")
        assert verdict.detail["strict_prompt_accuracy"] == 0.0
        assert verdict.detail["loose_prompt_accuracy"] == 1.0
        assert verdict.score == 0.0, "the headline is the strict figure, never the loose one"

    def test_a_conversational_lead_in_is_loose_only(self) -> None:
        case = _case({"kind": "starts_with", "value": "SUMMARY:", "case_sensitive": True})
        verdict = RuleScorer().score(case, "Sure, here you go:\nSUMMARY: it hashes inputs")
        assert (
            verdict.detail["strict_prompt_accuracy"],
            verdict.detail["loose_prompt_accuracy"],
        ) == (
            0.0,
            1.0,
        )

    def test_instruction_level_accuracy_separates_one_miss_from_all(self) -> None:
        case = _case(
            {"kind": "required_phrase", "value": "hash"},
            {"kind": "required_phrase", "value": "model"},
            {"kind": "required_phrase", "value": "machine"},
            {"kind": "required_phrase", "value": "prompt"},
        )
        one_miss = RuleScorer().score(case, "hash model machine")
        all_missed = RuleScorer().score(case, "nothing relevant")
        assert one_miss.detail["instruction_level_accuracy"] == 0.75
        assert all_missed.detail["instruction_level_accuracy"] == 0.0
        assert one_miss.score == all_missed.score == 0.0

    def test_loose_text_strips_exactly_three_wrappers(self) -> None:
        assert loose_text('```json\n{"a": 1}\n```') == '{"a": 1}'
        assert loose_text("Certainly:\nanswer") == "answer"
        assert loose_text('"answer"') == "answer"
        assert loose_text("a wrapper it must not strip: (answer)") == (
            "a wrapper it must not strip: (answer)"
        )


class TestViolationClasses:
    """Benchmark catalog §3.4 counts violations by class, and each kind declares its own."""

    def test_each_kind_maps_to_its_declared_class(self) -> None:
        assert Constraint(ConstraintKind.REQUIRED_PHRASE, "x").violation_class is (
            ViolationClass.KEYWORD
        )
        assert Constraint(ConstraintKind.WORD_COUNT_RANGE, minimum=1).violation_class is (
            ViolationClass.LENGTH
        )
        assert Constraint(ConstraintKind.SCRIPT, "GREEK").violation_class is ViolationClass.LANGUAGE

    def test_counts_are_reported_per_class(self) -> None:
        case = _case(
            {"kind": "required_phrase", "value": "hash"},
            {"kind": "starts_with", "value": "RESULT:"},
            {"kind": "word_count_range", "minimum": 50, "maximum": 60},
        )
        detail = RuleScorer().score(case, "nope").detail
        assert detail["violations_keyword"] == 1
        assert detail["violations_format"] == 1
        assert detail["violations_length"] == 1
        assert detail["violations_structure"] == 0
        assert [item["kind"] for item in detail["violations"]] == [
            "required_phrase",
            "starts_with",
            "word_count_range",
        ]


class TestMalformedAndMissing:
    """A defect in the case is never reported as a defect in the model."""

    def test_a_case_with_no_constraints_is_unscoreable(self) -> None:
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="p")
        verdict = RuleScorer().score(case, "anything")
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"

    def test_an_unknown_constraint_kind_is_unscoreable(self) -> None:
        verdict = RuleScorer().score(_case({"kind": "vibes", "value": "good"}), "anything")
        assert verdict.score is None
        assert verdict.error_code == "CONSTRAINT_INVALID"

    def test_an_empty_response_is_a_failure_not_an_absence(self) -> None:
        assert RuleScorer().score(_case({"kind": "required_phrase", "value": "x"}), "").score == 0.0

    @pytest.mark.parametrize(
        "declaration",
        [
            {"kind": "required_phrase"},
            {"kind": "word_count_range"},
            {"kind": "word_count_range", "minimum": "ten", "value": None},
            {"kind": "matches", "value": "(a+)+b"},
            {"kind": "matches", "value": "(a)\\1"},
            {"kind": "matches", "value": "x" * 201},
            {"kind": "matches", "value": "([unclosed"},
        ],
    )
    def test_the_declaration_is_refused_where_it_is_built(
        self, declaration: dict[str, object]
    ) -> None:
        # Refused at case-build time — which is startup — rather than mid-run. The two regex cases
        # are spec §14's linted dialect: a backreference and an unbounded nested quantifier are
        # what make catastrophic backtracking reachable from fixture data.
        with pytest.raises(ConstraintInvalid):
            Constraint.from_json(declaration)
