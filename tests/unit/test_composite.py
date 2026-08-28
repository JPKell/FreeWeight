"""Criteria become one score: gates, skips, the applied weight, and the rule timeout.

The phase's own test list names three cases here, and each has a class:

* a **hard gate zeroes the composite and records which gate fired**;
* a **skipped criterion is excluded and the applied weight reflects it** — never scored zero;
* a **catastrophic-backtracking regex fails the criterion within ``rule_timeout_ms``** and does not
  stall the run, leaving that criterion in ``error``.

The third is the one that needs a clock: it asserts that ``score_criterion`` *returns* inside its
budget, which is the property the run depends on. The abandoned computation finishes on its own —
Python cannot interrupt a regex engine mid-backtrack — which is why the dialect lint is the primary
guard and this is the backstop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from freeweight.domain.goals.composite import composite_score, score_method_mix
from freeweight.domain.goals.criteria import (
    CriterionOutcome,
    CriterionStatus,
    SkipReason,
    score_criterion,
)
from freeweight.domain.goals.pack import Criterion, Rung
from freeweight.domain.scorers.rules import RuleResult

_TEXT = "I counted the pallets twice and wrote the number on my hand."


def _criterion(key: str, weight: float, rule: dict[str, Any], *, gate: bool = False) -> Criterion:
    return Criterion(key=key, name=key, rung=Rung.RULE, weight=weight, is_gate=gate, rule=rule)


def _scored(key: str, weight: float, score: float, *, gated: bool = False) -> CriterionOutcome:
    return CriterionOutcome(
        criterion_key=key, rung=Rung.RULE, weight=weight, raw_score=score, gated=gated
    )


def _skipped(key: str, weight: float, rung: Rung = Rung.JUDGE) -> CriterionOutcome:
    return CriterionOutcome(
        criterion_key=key,
        rung=rung,
        weight=weight,
        status=CriterionStatus.SKIPPED,
        skip_reason=SkipReason.JUDGE_UNAVAILABLE.value,
    )


class TestAHardGateZeroesTheComposite:
    def test_and_names_the_gate_that_fired(self) -> None:
        result = composite_score(
            [_scored("tells", 0.4, 0.5, gated=True), _scored("rhythm", 0.6, 1.0)]
        )
        assert result.composite == 0.0
        assert result.gated_by == "tells"

    def test_a_gate_that_passed_leaves_the_weighted_mean_alone(self) -> None:
        result = composite_score([_scored("tells", 0.4, 1.0), _scored("rhythm", 0.6, 0.5)])
        assert result.composite == pytest.approx(0.7)
        assert result.gated_by is None

    def test_a_gate_fires_on_anything_below_one(self) -> None:
        criterion = _criterion(
            "tells",
            1.0,
            {"type": "forbidden_phrases", "phrases": ["pallets", "lorries"]},
            gate=True,
        )
        outcome = score_criterion(criterion, _TEXT)
        assert outcome.raw_score == 0.5
        assert outcome.gated is True

    def test_a_clean_gate_criterion_is_not_gated(self) -> None:
        criterion = _criterion(
            "tells", 1.0, {"type": "forbidden_phrases", "phrases": ["delve"]}, gate=True
        )
        assert score_criterion(criterion, _TEXT).gated is False

    def test_a_skipped_gate_never_fires(self) -> None:
        # A criterion that measured nothing cannot have failed.
        criterion = _criterion(
            "tells", 1.0, {"type": "forbidden_phrases", "phrases": ["delve"]}, gate=True
        )
        outcome = score_criterion(criterion, "   ")
        assert outcome.status is CriterionStatus.SKIPPED
        assert outcome.gated is False


class TestASkippedCriterionIsExcluded:
    def test_the_applied_weight_shows_the_exclusion(self) -> None:
        result = composite_score([_scored("tells", 0.6, 1.0), _skipped("wit", 0.4)])
        assert result.applied_weight == pytest.approx(0.6)
        assert result.declared_weight == pytest.approx(1.0)

    def test_and_the_composite_is_over_what_actually_contributed(self) -> None:
        # Not 0.6: a skipped criterion is excluded, not scored zero, so the one criterion that
        # did measure carries the whole composite.
        result = composite_score([_scored("tells", 0.6, 1.0), _skipped("wit", 0.4)])
        assert result.composite == 1.0

    def test_the_score_method_mix_is_over_the_applied_weight(self) -> None:
        # The judged criterion was skipped, so this sample contributed no judgement at all.
        result = composite_score([_scored("tells", 0.6, 1.0), _skipped("wit", 0.4)])
        assert result.score_method_mix == {
            "rule": 1.0,
            "reference": 0.0,
            "human": 0.0,
            "judge": 0.0,
        }

    def test_every_rung_is_present_even_at_zero(self) -> None:
        # A consumer reading {"rule": 1.0} cannot tell whether the judge share was zero or the
        # key was forgotten.
        assert set(score_method_mix([_scored("a", 1.0, 1.0)])) == {
            "rule",
            "reference",
            "human",
            "judge",
        }

    def test_everything_skipped_is_no_measurement_rather_than_zero(self) -> None:
        result = composite_score([_skipped("wit", 0.5), _skipped("register", 0.5)])
        assert result.composite is None
        assert result.applied_weight == 0.0

    def test_a_rule_that_could_not_read_the_response_skips_rather_than_scores_zero(self) -> None:
        criterion = _criterion("length", 1.0, {"type": "word_count", "min": 5})
        outcome = score_criterion(criterion, "")
        assert outcome.status is CriterionStatus.SKIPPED
        assert outcome.raw_score is None
        assert outcome.skip_reason == SkipReason.UNSUPPORTED.value
        assert outcome.detail["unsupported_reason"] == "empty_response"

    def test_the_evidence_names_every_criterion_including_the_skipped_ones(self) -> None:
        detail = composite_score([_scored("tells", 0.6, 1.0), _skipped("wit", 0.4)]).as_detail()
        assert [entry["key"] for entry in detail["criteria"]] == ["tells", "wit"]
        assert detail["criteria"][1]["raw_score"] is None
        assert detail["criteria"][1]["skip_reason"] == SkipReason.JUDGE_UNAVAILABLE.value


class TestTheRuleTimeout:
    """A rule that runs long fails *its criterion*, inside the budget, without stalling the run.

    The phase's named case is a catastrophic-backtracking regex, and this build stops one
    **earlier**: :func:`~freeweight.domain.scorers.rules.lint_pattern` refuses unbounded repetition
    of a group before the pattern is ever compiled, because CPython's regex engine holds the GIL
    for the whole match and no in-process timeout can interrupt one. That refusal is asserted in
    ``test_rules_regex.py``. What is asserted *here* is the backstop the timeout actually
    provides — for a rule that yields, which is every rule in the library — using an injected slow
    rule so the mechanism is measured rather than a particular pattern's runtime.
    """

    @pytest.fixture
    def slow_rule(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Replace one rule with a blocking one, and release it at teardown.

        Released rather than left sleeping: an abandoned worker thread would delay the
        interpreter's own exit, and a test suite that hangs for thirty seconds after its last
        assertion is a test suite people stop running.
        """
        from freeweight.domain.goals import criteria as module

        released = threading.Event()

        def rule(text: str, parameters: dict[str, Any]) -> RuleResult:
            del text, parameters
            released.wait(30)
            return RuleResult(score=1.0)

        monkeypatch.setitem(module.RULE_TYPES, "forbidden_phrases", rule)
        yield
        released.set()

    def test_the_criterion_returns_inside_its_budget(self, slow_rule: None) -> None:
        del slow_rule
        criterion = _criterion("tells", 1.0, {"type": "forbidden_phrases", "phrases": ["delve"]})
        started = time.monotonic()
        outcome = score_criterion(criterion, _TEXT, timeout_ms=50)
        elapsed_ms = (time.monotonic() - started) * 1000
        assert elapsed_ms < 2000  # noqa: PLR2004 — generous, and far below the rule's own 30 s
        assert outcome.status is CriterionStatus.ERROR
        assert outcome.skip_reason == SkipReason.RULE_TIMEOUT.value
        assert outcome.raw_score is None
        assert outcome.detail["timeout_ms"] == 50  # noqa: PLR2004 — the budget is recorded

    def test_and_the_goal_still_completes_with_the_other_criteria_scored(
        self, slow_rule: None
    ) -> None:
        del slow_rule
        poisoned = _criterion("tells", 0.5, {"type": "forbidden_phrases", "phrases": ["delve"]})
        sound = _criterion("length", 0.5, {"type": "word_count", "min": 5, "max": 40})
        outcomes = [
            score_criterion(poisoned, _TEXT, timeout_ms=50),
            score_criterion(sound, _TEXT),
        ]
        result = composite_score(outcomes)
        assert result.composite == 1.0
        assert result.applied_weight == pytest.approx(0.5)
        assert result.outcomes[0].status is CriterionStatus.ERROR

    def test_a_rule_that_finishes_is_not_timed_out(self) -> None:
        criterion = _criterion("shape", 1.0, {"type": "regex_match", "pattern": r"pallets"})
        outcome = score_criterion(criterion, _TEXT, timeout_ms=50)
        assert outcome.status is CriterionStatus.SCORED
        assert outcome.raw_score == 1.0


class TestCriterionDispatch:
    def test_an_unknown_rule_type_errors_rather_than_scoring(self) -> None:
        criterion = _criterion("mystery", 1.0, {"type": "vibes"})
        outcome = score_criterion(criterion, _TEXT)
        assert outcome.status is CriterionStatus.ERROR
        assert outcome.skip_reason == SkipReason.RULE_INVALID.value

    def test_a_malformed_parameter_block_errors_rather_than_raising(self) -> None:
        criterion = _criterion("tells", 1.0, {"type": "forbidden_phrases", "phrases": []})
        outcome = score_criterion(criterion, _TEXT)
        assert outcome.status is CriterionStatus.ERROR
        assert outcome.skip_reason == SkipReason.RULE_INVALID.value
        assert "error" in outcome.detail

    def test_a_reference_rule_is_handed_the_task_s_source(self) -> None:
        criterion = Criterion(
            key="entities",
            name="Entities",
            rung=Rung.REFERENCE,
            weight=1.0,
            rule={"type": "entity_recall"},
        )
        outcome = score_criterion(
            criterion, "Kestrel and Delia Marchetti", source={"entities": ["Kestrel"]}
        )
        assert outcome.raw_score == 1.0
        assert outcome.rung is Rung.REFERENCE

    def test_a_judged_criterion_is_not_this_function_s_business(self) -> None:
        criterion = Criterion(key="wit", name="Wit", rung=Rung.JUDGE, weight=1.0)
        with pytest.raises(ValueError, match="rungs 2 and 3"):
            score_criterion(criterion, _TEXT)

    def test_the_rung_travels_on_the_outcome(self) -> None:
        criterion = _criterion("tells", 1.0, {"type": "forbidden_phrases", "phrases": ["delve"]})
        assert score_criterion(criterion, _TEXT).rung is Rung.RULE


class TestTheCompositeItself:
    def test_the_weighted_mean(self) -> None:
        result = composite_score(
            [_scored("a", 0.5, 1.0), _scored("b", 0.3, 0.5), _scored("c", 0.2, 0.0)]
        )
        assert result.composite == pytest.approx(0.65)

    def test_an_empty_set_is_no_measurement(self) -> None:
        assert composite_score([]).composite is None

    def test_the_score_is_clamped_into_range(self) -> None:
        result = composite_score([_scored("a", 1.0, 1.0)])
        assert 0.0 <= (result.composite or 0.0) <= 1.0
