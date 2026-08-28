"""``word_count``, ``sentence_length_distribution`` and ``paragraph_shape``."""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    RuleInvalid,
    proportional,
)
from freeweight.domain.scorers.rules.length import (
    paragraph_shape,
    sentence_length_distribution,
    word_count,
)

_TEN_WORDS = "one two three four five six seven eight nine ten"


class TestWordCount:
    def test_known_pass_in_band(self) -> None:
        assert word_count(_TEN_WORDS, {"min": 5, "max": 15}).score == 1.0

    def test_known_fail_just_outside_the_band_decays_rather_than_snapping(self) -> None:
        # Two words short of a band eight wide: the default tolerance is half the width, so the
        # score is half. A step function here would make "nearly right" and "wildly wrong" the
        # same number.
        result = word_count(_TEN_WORDS, {"min": 12, "max": 20})
        assert result.score == pytest.approx(0.5)
        assert result.detail["word_count"] == 10  # noqa: PLR2004 — the count is the assertion

    def test_the_obvious_trip_is_far_outside(self) -> None:
        assert word_count("one", {"min": 200, "max": 400}).score == 0.0

    def test_boundary_is_inclusive(self) -> None:
        assert word_count(_TEN_WORDS, {"min": 10, "max": 10}).score == 1.0

    def test_empty_input_is_unsupported(self) -> None:
        result = word_count("", {"min": 5})
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_no_band_is_unsupported_rather_than_a_free_one(self) -> None:
        # A criterion satisfied by every response measures nothing, and reporting 1.0 would put a
        # criterion into the composite that can never move it.
        result = word_count(_TEN_WORDS, {})
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_NO_BAND

    def test_unicode_words_count_once_each(self) -> None:
        assert word_count("Grüße Größe Straße", {"min": 3, "max": 3}).score == 1.0

    def test_numbers_and_punctuation_are_not_words(self) -> None:
        assert word_count("two words 12 34 -- !!", {"min": 2, "max": 2}).score == 1.0

    def test_an_inverted_band_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="no response can satisfy"):
            word_count(_TEN_WORDS, {"min": 30, "max": 10})

    def test_a_non_numeric_bound_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="must be a number"):
            word_count(_TEN_WORDS, {"min": "five"})


class TestProportionalCurve:
    """The one decay curve the whole library shares."""

    def test_in_band_is_one(self) -> None:
        assert proportional(15, 10, 20) == 1.0

    def test_zero_at_the_tolerance_edge(self) -> None:
        assert proportional(25, 10, 20, tolerance=5) == 0.0

    def test_half_way_is_half(self) -> None:
        assert proportional(22.5, 10, 20, tolerance=5) == pytest.approx(0.5)

    def test_never_negative(self) -> None:
        assert proportional(1000, 10, 20, tolerance=5) == 0.0

    def test_a_band_with_no_bounds_is_satisfied(self) -> None:
        assert proportional(5, None, None) == 1.0

    def test_a_zero_tolerance_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="must be positive"):
            proportional(25, 10, 20, tolerance=0)


class TestSentenceLengthDistribution:
    _BAND = {"mean_words": {"min": 5, "max": 15}, "cv": {"min": 0.2}}

    def test_known_pass(self) -> None:
        text = (
            "I counted the pallets twice and wrote the number on my hand. "
            "Nobody had signed. "
            "The second count matched the first, which was the whole problem that night."
        )
        assert sentence_length_distribution(text, self._BAND).score == 1.0

    def test_the_obvious_trip_is_a_wall_of_identical_sentences(self) -> None:
        # Same length every time: the mean is in band and the coefficient of variation is zero.
        text = " ".join("one two three four five six seven." for _ in range(6))
        result = sentence_length_distribution(text, self._BAND)
        assert result.detail["coefficient_of_variation"] == pytest.approx(0.0)
        assert result.score is not None
        assert result.score < 1.0

    def test_a_single_sentence_leaves_the_spread_unmeasured(self) -> None:
        # One observation has no spread; reporting 0.0 would claim perfect uniformity.
        result = sentence_length_distribution("One two three four five six.", self._BAND)
        assert result.detail["cv_unmeasured"] is True
        assert result.detail["coefficient_of_variation"] is None
        assert result.score == 1.0

    def test_empty_input_is_unsupported(self) -> None:
        assert sentence_length_distribution("", self._BAND).unsupported_reason == (
            UNSUPPORTED_EMPTY_TEXT
        )

    def test_no_band_is_unsupported(self) -> None:
        assert (
            sentence_length_distribution("One two three.", {}).unsupported_reason
            == UNSUPPORTED_NO_BAND
        )

    def test_unicode_prose_is_measured_normally(self) -> None:
        text = "Die Größe war falsch. Ich habe zweimal gezählt und nichts gefunden, gar nichts."
        result = sentence_length_distribution(text, {"mean_words": {"min": 3, "max": 20}})
        assert result.score == 1.0

    def test_an_inverted_band_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="no response can satisfy"):
            sentence_length_distribution("One two.", {"mean_words": {"min": 20, "max": 5}})


class TestParagraphShape:
    _BAND = {"paragraphs": {"min": 2, "max": 4}, "words_per_paragraph": {"min": 5, "max": 40}}

    def test_known_pass(self) -> None:
        text = f"{_TEN_WORDS}\n\n{_TEN_WORDS}\n\n{_TEN_WORDS}"
        assert paragraph_shape(text, self._BAND).score == 1.0

    def test_the_obvious_trip_is_one_long_block(self) -> None:
        result = paragraph_shape(" ".join([_TEN_WORDS] * 12), self._BAND)
        assert result.detail["paragraph_count"] == 1
        assert result.score == 0.0

    def test_boundary_counts_a_blank_line_as_the_separator(self) -> None:
        assert (
            paragraph_shape(f"{_TEN_WORDS}\n{_TEN_WORDS}", self._BAND).detail["paragraph_count"]
            == 1
        )

    def test_empty_input_is_unsupported(self) -> None:
        assert paragraph_shape("\n\n", self._BAND).unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_no_band_is_unsupported(self) -> None:
        assert paragraph_shape(_TEN_WORDS, {}).unsupported_reason == UNSUPPORTED_NO_BAND

    def test_unicode_paragraphs(self) -> None:
        text = "Größe war falsch heute morgen früh.\n\nZweimal gezählt und nichts gefunden hier."
        assert paragraph_shape(text, {"paragraphs": {"min": 2, "max": 2}}).score == 1.0
