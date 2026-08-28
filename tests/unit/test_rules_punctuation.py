"""``punctuation_profile``: marks per thousand words, against declared bands."""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    RuleInvalid,
)
from freeweight.domain.scorers.rules.punctuation import MARKS, punctuation_profile

_HUNDRED_WORDS = " ".join("word" for _ in range(100))


class TestPunctuationProfile:
    def test_known_pass_within_the_band(self) -> None:
        text = f"{_HUNDRED_WORDS} — one dash only"
        result = punctuation_profile(text, {"em_dash_per_1000_words": {"min": 5, "max": 15}})
        assert result.score == 1.0

    def test_rates_are_per_thousand_words_not_counts(self) -> None:
        # One dash in a hundred words is ten per thousand; a rule that counted marks would call
        # a paragraph and an essay with the same habit different.
        result = punctuation_profile(
            f"{_HUNDRED_WORDS} — x", {"em_dash_per_1000_words": {"max": 20}}
        )
        assert result.detail["rates_per_1000_words"]["em_dash"] == pytest.approx(1000 / 101)

    def test_the_obvious_trip_is_an_exclamation_habit(self) -> None:
        text = "Great! Excellent! Wonderful! Superb! Marvellous!"
        result = punctuation_profile(text, {"exclamation_per_1000_words": {"max": 20}})
        assert result.detail["counts"]["exclamation"] == 5  # noqa: PLR2004 — the count is the point
        assert result.score == 0.0

    def test_every_marks_rate_travels_even_the_unbanded_ones(self) -> None:
        result = punctuation_profile(
            "One; two — three! four? five…", {"semicolon_per_1000_words": {"max": 500}}
        )
        for mark in MARKS:
            assert mark in result.detail["rates_per_1000_words"]

    def test_a_double_hyphen_is_not_an_em_dash(self) -> None:
        # Counted literally: a rubric that means either says so with two bands.
        result = punctuation_profile(
            f"{_HUNDRED_WORDS} -- not a dash", {"em_dash_per_1000_words": {"max": 0}}
        )
        assert result.detail["counts"]["em_dash"] == 0
        assert result.score == 1.0

    def test_boundary_at_zero(self) -> None:
        assert (
            punctuation_profile(_HUNDRED_WORDS, {"exclamation_per_1000_words": {"max": 0}}).score
            == 1.0
        )

    def test_empty_input_is_unsupported(self) -> None:
        result = punctuation_profile("  ", {"em_dash_per_1000_words": {"max": 5}})
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_no_band_is_unsupported(self) -> None:
        assert punctuation_profile(_HUNDRED_WORDS, {}).unsupported_reason == UNSUPPORTED_NO_BAND

    def test_unicode_punctuation_is_counted(self) -> None:
        result = punctuation_profile("Größe… Straße…", {"ellipsis_per_1000_words": {"max": 1000}})
        assert result.detail["counts"]["ellipsis"] == 2  # noqa: PLR2004 — the count is the point

    def test_an_unknown_mark_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="punctuation this build does not count"):
            punctuation_profile(_HUNDRED_WORDS, {"interrobang_per_1000_words": {"max": 1}})
