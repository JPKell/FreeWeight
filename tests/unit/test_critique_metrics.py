"""``native.critique``'s metrics, with the phase's named case first: **regression detected**.

A critic that improves nine answers and breaks one must not look like a critic that improves nine
and breaks none. That is what ``regression_rate`` is for, and the first class here is the test the
phase's own list names — a correct answer made incorrect, measured as a regression rather than
absorbed into an average.

The rest is the metric-formula treatment testing standards §5 requires: known values, the
boundaries where a rate's denominator empties, empty input, and the two inputs the scorer refuses
to score.
"""

from __future__ import annotations

import json

import pytest

from freeweight.domain.benchmark import BenchmarkCase
from freeweight.domain.scorers.critique import (
    CritiqueExpectation,
    CritiqueResponse,
    CritiqueScorer,
    critique_metrics,
    parse_critique,
)
from freeweight.domain.scoring import ScoreMethod

_NORMALIZE = ["case", "whitespace", "punctuation"]


def _expectation(*, correct: bool, candidate: str, gold: str = "68") -> CritiqueExpectation:
    return CritiqueExpectation.from_json(
        {
            "answer_is_correct": correct,
            "gold_answers": [gold],
            "candidate_answer": candidate,
            "normalize": _NORMALIZE,
        }
    )


def _case(body: object, case_id: str = "case-1") -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id, ordinal=0, prompt="prompt", expectation={"critique": body}
    )


def _reply(verdict: str, corrected: str | None = None) -> str:
    body: dict[str, object] = {"verdict": verdict, "reason": "because"}
    if corrected is not None:
        body["corrected_answer"] = corrected
    return json.dumps(body)


class TestRegressionIsDetected:
    """The phase's named test: a correct answer made incorrect."""

    def test_flagging_a_correct_answer_and_replacing_it_is_a_regression(self) -> None:
        expectation = _expectation(correct=True, candidate="68")
        response = CritiqueResponse(flagged=True, corrected_answer="65")
        score, metrics, _evidence = critique_metrics(expectation, response)
        assert score == 0.0
        assert metrics["regression_rate"] == 1.0
        assert metrics["correction_uplift"] == -1.0
        assert metrics["hallucinated_criticism_rate"] == 1.0

    def test_flagging_a_correct_answer_without_replacing_it_is_not_a_regression(self) -> None:
        # The criticism was hallucinated, but the answer standing at the end is still right, and
        # conflating the two would report the same number for two different behaviours.
        expectation = _expectation(correct=True, candidate="68")
        response = CritiqueResponse(flagged=True, corrected_answer=None)
        score, metrics, _evidence = critique_metrics(expectation, response)
        assert score == 1.0
        assert metrics["regression_rate"] == 0.0
        assert metrics["hallucinated_criticism_rate"] == 1.0

    def test_leaving_a_correct_answer_alone_is_neither(self) -> None:
        expectation = _expectation(correct=True, candidate="68")
        _score, metrics, _evidence = critique_metrics(expectation, CritiqueResponse(flagged=False))
        assert metrics["regression_rate"] == 0.0
        assert metrics["hallucinated_criticism_rate"] == 0.0
        assert metrics["correction_uplift"] == 0.0

    def test_regression_is_absent_where_the_candidate_was_already_wrong(self) -> None:
        # It has no meaning there, and reporting zero would dilute the rate with cases that
        # could never have contributed to it.
        expectation = _expectation(correct=False, candidate="65")
        _score, metrics, _evidence = critique_metrics(
            expectation, CritiqueResponse(flagged=True, corrected_answer="12")
        )
        assert "regression_rate" not in metrics
        assert "hallucinated_criticism_rate" not in metrics


class TestUpliftIsTheDifferenceOfTheMeans:
    """Measured per case so the ordinary mean aggregation reproduces the catalog's definition."""

    def test_fixing_a_wrong_answer_is_plus_one(self) -> None:
        expectation = _expectation(correct=False, candidate="65")
        _score, metrics, _evidence = critique_metrics(
            expectation, CritiqueResponse(flagged=True, corrected_answer="68")
        )
        assert metrics["correction_uplift"] == 1.0
        assert metrics["valid_correction_rate"] == 1.0
        assert metrics["error_detection_recall"] == 1.0

    def test_missing_a_wrong_answer_is_zero(self) -> None:
        expectation = _expectation(correct=False, candidate="65")
        _score, metrics, _evidence = critique_metrics(expectation, CritiqueResponse(flagged=False))
        assert metrics["correction_uplift"] == 0.0
        assert metrics["error_detection_recall"] == 0.0
        assert "criticism_precision" not in metrics

    def test_the_mean_of_the_per_case_differences_is_the_difference_of_the_means(self) -> None:
        cases = [
            (_expectation(correct=False, candidate="65"), CritiqueResponse(True, "68")),
            (_expectation(correct=False, candidate="65"), CritiqueResponse(False)),
            (_expectation(correct=True, candidate="68"), CritiqueResponse(True, "65")),
            (_expectation(correct=True, candidate="68"), CritiqueResponse(False)),
        ]
        computed = [critique_metrics(expectation, reply) for expectation, reply in cases]
        posts = [metrics["post_correction_accuracy"] for _s, metrics, _e in computed]
        originals = [metrics["original_accuracy"] for _s, metrics, _e in computed]
        uplifts = [metrics["correction_uplift"] for _s, metrics, _e in computed]
        assert sum(uplifts) / len(uplifts) == pytest.approx(
            sum(posts) / len(posts) - sum(originals) / len(originals)
        )


class TestCriticismPrecision:
    """Only defined on the cases the critic actually flagged."""

    def test_flagging_a_wrong_answer_is_precise(self) -> None:
        expectation = _expectation(correct=False, candidate="65")
        _score, metrics, _evidence = critique_metrics(
            expectation, CritiqueResponse(flagged=True, corrected_answer="68")
        )
        assert metrics["criticism_precision"] == 1.0

    def test_flagging_a_right_answer_is_not(self) -> None:
        expectation = _expectation(correct=True, candidate="68")
        _score, metrics, _evidence = critique_metrics(
            expectation, CritiqueResponse(flagged=True, corrected_answer="65")
        )
        assert metrics["criticism_precision"] == 0.0

    def test_it_is_absent_when_nothing_was_flagged(self) -> None:
        expectation = _expectation(correct=True, candidate="68")
        _score, metrics, _evidence = critique_metrics(expectation, CritiqueResponse(flagged=False))
        assert "criticism_precision" not in metrics


class TestBoundaryAndNormalization:
    """A correction that is right in different words is still right."""

    def test_normalization_is_applied_to_both_sides(self) -> None:
        expectation = CritiqueExpectation.from_json(
            {
                "answer_is_correct": False,
                "gold_answers": ["12 February 2021"],
                "candidate_answer": "3 March 2021",
                "normalize": _NORMALIZE,
            }
        )
        _score, metrics, _evidence = critique_metrics(
            expectation, CritiqueResponse(True, " 12 february 2021. ")
        )
        assert metrics["post_correction_accuracy"] == 1.0

    def test_an_empty_correction_string_is_no_correction(self) -> None:
        assert parse_critique(_reply("incorrect", "")) == CritiqueResponse(
            flagged=True, corrected_answer=None
        )


class TestMalformedAndMissingData:
    """What the scorer refuses, and why refusing is not a zero."""

    def test_prose_is_unscoreable(self) -> None:
        verdict = CritiqueScorer().score(
            _case({"answer_is_correct": False, "gold_answers": ["68"], "candidate_answer": "65"}),
            "That looks wrong to me; it should be sixty-eight.",
        )
        assert verdict.score is None
        assert verdict.error_code == "CRITIQUE_UNPARSEABLE"
        assert verdict.method is ScoreMethod.RULE

    def test_an_empty_answer_is_unscoreable(self) -> None:
        verdict = CritiqueScorer().score(
            _case({"answer_is_correct": False, "gold_answers": ["68"], "candidate_answer": "65"}),
            "",
        )
        assert verdict.score is None
        assert verdict.error_code == "CRITIQUE_UNPARSEABLE"

    def test_an_unknown_verdict_word_is_unscoreable(self) -> None:
        assert parse_critique(json.dumps({"verdict": "maybe"})) is None

    def test_a_case_with_no_expectation_is_unscoreable(self) -> None:
        verdict = CritiqueScorer().score(
            BenchmarkCase(case_id="c", ordinal=0, prompt="p"), _reply("correct")
        )
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"

    def test_a_case_whose_ground_truth_contradicts_itself_is_refused(self) -> None:
        with pytest.raises(ValueError, match="contradicts itself"):
            CritiqueExpectation.from_json(
                {
                    "answer_is_correct": True,
                    "gold_answers": ["68"],
                    "candidate_answer": "65",
                }
            )

    def test_a_case_with_no_gold_answer_is_refused(self) -> None:
        with pytest.raises(ValueError, match="gold_answers"):
            CritiqueExpectation.from_json({"answer_is_correct": False, "candidate_answer": "65"})

    def test_a_case_with_no_correctness_flag_is_refused(self) -> None:
        with pytest.raises(ValueError, match="answer_is_correct"):
            CritiqueExpectation.from_json({"gold_answers": ["68"], "candidate_answer": "65"})


class TestTheScorerEndToEnd:
    """The scorer's own contract, over the shapes a model actually returns."""

    def test_a_fenced_json_answer_is_read(self) -> None:
        verdict = CritiqueScorer().score(
            _case(
                {
                    "answer_is_correct": False,
                    "gold_answers": ["68"],
                    "candidate_answer": "65",
                    "normalize": _NORMALIZE,
                }
            ),
            "```json\n" + _reply("incorrect", "68") + "\n```",
        )
        assert verdict.score == 1.0
        assert verdict.detail["correction_uplift"] == 1.0
        assert verdict.detail["critic_flagged"] is True
