"""``native.audit``'s metrics: known-pass, known-fail, boundary, malformed and missing data.

Phase 8's own test list names the case this file exists for: **a model that flags everything
scores poorly**, and the synthetic "flag everything" responder is a test case rather than a field
report. Everything else here is the metric-formula treatment testing standards §5 requires — a
known value, the boundary where a denominator empties, empty input, and the two inputs the scorer
must refuse to score at all.
"""

from __future__ import annotations

import json

import pytest

from freeweight.benchmarks.audit.benchmark import number_lines
from freeweight.domain.benchmark import BenchmarkCase
from freeweight.domain.scorers.audit import (
    MATCH_TOLERANCE_LINES,
    AuditExpectation,
    AuditScorer,
    Defect,
    Finding,
    audit_metrics,
    match_findings,
    parse_findings,
)
from freeweight.domain.scoring import ScoreMethod

_MUTATED = {
    "defects": [
        {"line": 5, "category": "off_by_one", "severity": "medium", "function": "last_n"},
        {"line": 12, "category": "removed_guard", "severity": "high", "function": "average"},
    ],
    "clean": False,
}
_CLEAN: dict[str, object] = {"defects": [], "clean": True}


def _case(expectation: object, case_id: str = "case-1") -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id, ordinal=0, prompt="prompt", expectation={"audit": expectation}
    )


def _findings(*lines: int) -> str:
    return json.dumps(
        {
            "findings": [
                {"line": line, "category": "off_by_one", "function": "last_n"} for line in lines
            ]
        }
    )


def _flag_everything(line_count: int) -> str:
    """A synthetic responder that reports a defect on every line of the file."""
    return _findings(*range(1, line_count + 1))


class TestAFlagEverythingModelScoresPoorly:
    """Acceptance criterion 1: perfect recall must not buy a good score."""

    def test_it_finds_every_defect(self) -> None:
        expectation = AuditExpectation.from_json(_MUTATED)
        findings = parse_findings(_flag_everything(20))
        assert findings is not None
        _score, metrics, _evidence = audit_metrics(expectation, findings)
        assert metrics["recall"] == 1.0

    def test_and_still_scores_close_to_zero(self) -> None:
        expectation = AuditExpectation.from_json(_MUTATED)
        findings = parse_findings(_flag_everything(20))
        assert findings is not None
        score, metrics, _evidence = audit_metrics(expectation, findings)
        assert metrics["precision"] == pytest.approx(2 / 20)
        assert metrics["f1"] == pytest.approx(2 * (2 / 20) * 1.0 / ((2 / 20) + 1.0))
        assert score < 0.2  # noqa: PLR2004 — the whole point of the test is the magnitude

    def test_and_scores_zero_on_every_clean_file(self) -> None:
        verdict = AuditScorer().score(_case(_CLEAN), _flag_everything(20))
        assert verdict.score == 0.0
        assert verdict.detail["clean_code_false_positive_rate"] == 1.0

    def test_while_a_silent_model_scores_one_on_clean_files(self) -> None:
        verdict = AuditScorer().score(_case(_CLEAN), json.dumps({"findings": []}))
        assert verdict.score == 1.0
        assert verdict.detail["clean_code_false_positive_rate"] == 0.0
        # ... and nothing at all on the mutated half, which is the trade being measured.
        missed = AuditScorer().score(_case(_MUTATED), json.dumps({"findings": []}))
        assert missed.score == 0.0
        assert missed.detail["recall"] == 0.0


class TestKnownValues:
    """Precision, recall and F1 against hand-computed answers."""

    def test_one_of_two_defects_found_with_no_false_positives(self) -> None:
        expectation = AuditExpectation.from_json(_MUTATED)
        findings = parse_findings(_findings(5))
        assert findings is not None
        _score, metrics, _evidence = audit_metrics(expectation, findings)
        assert metrics["precision"] == 1.0
        assert metrics["recall"] == 0.5
        assert metrics["f1"] == pytest.approx(2 / 3)

    def test_both_found_with_one_spurious_finding(self) -> None:
        expectation = AuditExpectation.from_json(_MUTATED)
        findings = parse_findings(_findings(5, 12, 40))
        assert findings is not None
        _score, metrics, evidence = audit_metrics(expectation, findings)
        assert metrics["precision"] == pytest.approx(2 / 3)
        assert metrics["recall"] == 1.0
        assert metrics["f1"] == pytest.approx(0.8)
        assert evidence["false_positives"] == 1

    def test_nothing_found_and_nothing_reported_leaves_precision_absent(self) -> None:
        # A rate with an empty denominator is absent, never zero (ADR-0016): the model reported
        # nothing, so "how many of its findings were right" has no answer.
        expectation = AuditExpectation.from_json(_MUTATED)
        _score, metrics, _evidence = audit_metrics(expectation, ())
        assert "precision" not in metrics
        assert metrics["recall"] == 0.0
        assert "f1" not in metrics

    def test_localization_is_scored_apart_from_detection(self) -> None:
        # One line off is still a find (within MATCH_TOLERANCE_LINES) and is *not* localized.
        expectation = AuditExpectation.from_json(_MUTATED)
        findings = parse_findings(_findings(6, 12))
        assert findings is not None
        _score, metrics, _evidence = audit_metrics(expectation, findings)
        assert metrics["recall"] == 1.0
        assert metrics["line_localization_accuracy"] == 0.5
        assert metrics["function_localization_accuracy"] == 0.5


class TestBoundary:
    """The matching tolerance, and the greedy one-finding-per-defect pairing."""

    def test_a_finding_at_the_tolerance_edge_matches(self) -> None:
        matched, spurious, missed = match_findings(
            [Defect(line=10)], [Finding(line=10 + MATCH_TOLERANCE_LINES)]
        )
        assert len(matched) == 1
        assert not spurious
        assert not missed

    def test_a_finding_one_past_it_does_not(self) -> None:
        matched, spurious, missed = match_findings(
            [Defect(line=10)], [Finding(line=11 + MATCH_TOLERANCE_LINES)]
        )
        assert not matched
        assert len(spurious) == 1
        assert len(missed) == 1

    def test_repeating_the_same_line_buys_one_true_positive_and_four_false_ones(self) -> None:
        matched, spurious, _missed = match_findings(
            [Defect(line=10)], [Finding(line=10) for _ in range(5)]
        )
        assert len(matched) == 1
        assert len(spurious) == 4  # noqa: PLR2004 — the count is the assertion

    def test_the_nearest_finding_wins_the_defect(self) -> None:
        matched, _spurious, _missed = match_findings(
            [Defect(line=10)], [Finding(line=12), Finding(line=10)]
        )
        assert matched[0][1].line == 10  # noqa: PLR2004 — the exact line is the assertion

    def test_a_finding_with_no_line_is_a_false_positive(self) -> None:
        _matched, spurious, missed = match_findings([Defect(line=10)], [Finding(line=None)])
        assert len(spurious) == 1
        assert len(missed) == 1


class TestMalformedAndMissingData:
    """What the scorer refuses to score, and why refusing is not a zero."""

    def test_prose_is_unscoreable_rather_than_a_detection_failure(self) -> None:
        verdict = AuditScorer().score(_case(_MUTATED), "I think line five looks wrong.")
        assert verdict.score is None
        assert verdict.error_code == "AUDIT_UNPARSEABLE"
        assert verdict.method is ScoreMethod.RULE

    def test_an_empty_answer_is_unscoreable(self) -> None:
        verdict = AuditScorer().score(_case(_MUTATED), "")
        assert verdict.score is None
        assert verdict.error_code == "AUDIT_UNPARSEABLE"

    def test_a_case_with_no_expectation_is_unscoreable(self) -> None:
        verdict = AuditScorer().score(
            BenchmarkCase(case_id="c", ordinal=0, prompt="p"), _findings(5)
        )
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"

    def test_a_clean_case_that_also_declares_a_defect_is_refused(self) -> None:
        contradiction = {"clean": True, "defects": [{"line": 3}]}
        with pytest.raises(ValueError, match="clean=true"):
            AuditExpectation.from_json(contradiction)
        verdict = AuditScorer().score(_case(contradiction), _findings(3))
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"

    def test_a_defect_with_no_line_is_refused_where_the_case_is_read(self) -> None:
        with pytest.raises(ValueError, match="positive integer 'line'"):
            Defect.from_json({"category": "off_by_one"})

    def test_a_bare_list_of_findings_is_accepted(self) -> None:
        # The model was asked for an object; a bare array is the same report and refusing it
        # would measure formatting rather than auditing.
        findings = parse_findings('[{"line": 5}]')
        assert findings is not None
        assert findings[0].line == 5  # noqa: PLR2004 — the parsed line is the assertion

    def test_an_explicitly_empty_report_is_a_real_answer(self) -> None:
        assert parse_findings('{"findings": []}') == ()


class TestTheNumberedGutter:
    """The model and the corpus have to mean the same thing by "line 5"."""

    def test_lines_are_numbered_from_one(self) -> None:
        rendered = number_lines("alpha\nbeta\ngamma\n")
        assert rendered.splitlines()[0].endswith("| alpha")
        assert rendered.splitlines()[0].strip().startswith("1")
        assert rendered.splitlines()[2].strip().startswith("3")

    def test_a_trailing_newline_does_not_add_a_line(self) -> None:
        assert len(number_lines("alpha\n").splitlines()) == 1
