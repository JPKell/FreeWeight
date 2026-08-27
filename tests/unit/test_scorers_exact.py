"""The exact-match scorer: known-pass, known-fail, boundary, malformed and missing data.

The phase's own test list for every Phase 7 scorer. "Malformed model response" for an exact-match
scorer is a response that is not the shape the case asked for at all — an empty answer, a wall of
prose around the right token — and "missing data" is a case that never said what right looks like,
which must be unscoreable rather than a failure (ADR-0016).
"""

from __future__ import annotations

import pytest

from freeweight.domain.benchmark import BenchmarkCase
from freeweight.domain.scorers.exact import ExactMatchScorer, Normalization, normalize
from freeweight.domain.scoring import ScoreMethod


def _case(expectation: object, case_id: str = "case-1") -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id, ordinal=0, prompt="prompt", expectation={"exact": expectation}
    )


class TestKnownPassAndFail:
    """The two answers a scorer exists to give."""

    def test_an_exact_answer_scores_one(self) -> None:
        verdict = ExactMatchScorer().score(_case({"any_of": ["42"]}), "42")
        assert verdict.score == 1.0
        assert verdict.method is ScoreMethod.RULE
        assert verdict.detail["matched"] == "42"

    def test_a_wrong_answer_scores_zero_not_none(self) -> None:
        # A measurement, not an absence: the model answered and the answer was wrong.
        verdict = ExactMatchScorer().score(_case({"any_of": ["42"]}), "43")
        assert verdict.score == 0.0
        assert verdict.error_code is None

    def test_any_of_accepts_a_second_spelling(self) -> None:
        case = _case({"any_of": ["7", "seven"], "normalize": ["case"]})
        assert ExactMatchScorer().score(case, "Seven").score == 1.0


class TestBoundary:
    """Normalization is the only latitude, and it is declared per case."""

    def test_without_normalization_a_trailing_stop_fails(self) -> None:
        assert ExactMatchScorer().score(_case({"any_of": ["42"]}), "42.").score == 0.0

    def test_with_punctuation_normalization_it_passes(self) -> None:
        case = _case({"any_of": ["42"], "normalize": ["punctuation", "whitespace"]})
        assert ExactMatchScorer().score(case, " 42. ").score == 1.0

    def test_contains_is_opt_in(self) -> None:
        # Without ``contains`` a longer response fails, which is what keeps a short-answer case
        # from passing on a paragraph that happens to hold the answer and three wrong ones.
        strict = _case({"any_of": ["total_units"]})
        loose = _case({"any_of": ["total_units"], "contains": True})
        response = "It imports total_units from pkg.inventory."
        assert ExactMatchScorer().score(strict, response).score == 0.0
        assert ExactMatchScorer().score(loose, response).score == 1.0

    @pytest.mark.parametrize(
        ("normalizations", "expected"),
        [
            ([], "  Grüße,  Welt.  "),
            ([Normalization.TRAILING], "Grüße,  Welt."),
            ([Normalization.WHITESPACE], "Grüße, Welt."),
            ([Normalization.CASE], "  grüsse,  welt.  "),
            ([Normalization.PUNCTUATION, Normalization.WHITESPACE], "Grüße Welt"),
        ],
    )
    def test_each_normalization_does_exactly_one_thing(
        self, normalizations: list[str], expected: str
    ) -> None:
        assert normalize("  Grüße,  Welt.  ", normalizations) == expected

    def test_an_unknown_normalization_is_ignored_rather_than_loosening(self) -> None:
        # A case asking for a normalization this build does not have compares *more* strictly, not
        # less: the alternative is silently passing answers nobody checked.
        assert normalize("A B", ["stem"]) == "A B"


class TestMalformedAndMissing:
    """An unscoreable case is ``None`` with a reason; a bad answer is ``0.0``."""

    def test_an_empty_response_is_a_failure_not_an_absence(self) -> None:
        verdict = ExactMatchScorer().score(_case({"any_of": ["42"]}), "")
        assert verdict.score == 0.0

    def test_a_case_with_no_expectation_is_unscoreable(self) -> None:
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="p")
        verdict = ExactMatchScorer().score(case, "42")
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"
        assert "c" in (verdict.error_text or "")

    def test_an_empty_any_of_is_unscoreable(self) -> None:
        assert ExactMatchScorer().score(_case({"any_of": []}), "42").score is None

    @pytest.mark.parametrize("shorthand", ["42", ["42", "forty-two"]])
    def test_the_shorthand_forms_mean_the_same_thing(self, shorthand: object) -> None:
        assert ExactMatchScorer().score(_case(shorthand), "42").score == 1.0
