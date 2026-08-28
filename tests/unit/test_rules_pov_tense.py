"""``pov_tense``: a lexical consistency check whose limits are stated rather than discovered."""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import UNSUPPORTED_EMPTY_TEXT, RuleInvalid
from freeweight.domain.scorers.rules.pov_tense import UNSUPPORTED_NOTHING_DECIDABLE, pov_tense

_FIRST_PAST = {"person": "first", "tense": "past", "tolerance": 0.15}


class TestPovTense:
    def test_known_pass(self) -> None:
        text = (
            "I counted the pallets twice. I wrote the number on my hand. "
            "We walked the north aisle and found nothing."
        )
        result = pov_tense(text, _FIRST_PAST)
        assert result.score == 1.0
        assert result.detail["conforming_share"] == 1.0

    def test_known_fail_wrong_person(self) -> None:
        text = "You counted the pallets twice. You wrote the number down. You went home."
        assert pov_tense(text, _FIRST_PAST).score == 0.0

    def test_the_obvious_trip_is_drifting_into_the_present(self) -> None:
        text = (
            "I counted the pallets twice. I wrote the number down. "
            "I am counting once more. I do the count slowly. I can wait."
        )
        result = pov_tense(text, _FIRST_PAST)
        assert result.detail["decidable_sentences"] == 5  # noqa: PLR2004 — the count is the point
        assert result.score is not None
        assert result.score < 1.0
        assert result.detail["deviating_sentences"]

    def test_boundary_the_tolerance_forgives_one_quotation(self) -> None:
        text = (
            "I counted the pallets twice. I wrote the number down. I walked the aisle. "
            "I checked the log. I went home. I did not sleep. I came back early. "
            "I counted again. I found nothing. I told nobody. I kept the note. "
            "I filed the report. You are always wrong."
        )
        result = pov_tense(text, _FIRST_PAST)
        # One of thirteen decidable sentences deviates, which is inside a 0.15 tolerance.
        assert result.detail["decidable_sentences"] == 13  # noqa: PLR2004 — the count is the point
        assert result.score == 1.0

    def test_a_sentence_with_no_marker_is_excluded_rather_than_failed(self) -> None:
        # "Remarkable." carries no pronoun and no verb; counting it as a violation would penalize
        # a response for having a short sentence in it.
        text = "I counted the pallets. Remarkable. I wrote the number down."
        result = pov_tense(text, _FIRST_PAST)
        assert result.detail["sentence_count"] == 3  # noqa: PLR2004 — the count is the assertion
        assert result.detail["decidable_sentences"] == 2  # noqa: PLR2004 — two carry markers
        assert result.score == 1.0

    def test_nothing_decidable_is_unsupported_not_zero(self) -> None:
        result = pov_tense("Remarkable. Extraordinary. Quite so.", _FIRST_PAST)
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_NOTHING_DECIDABLE

    def test_person_alone_can_be_checked(self) -> None:
        text = "I walk the aisle. I count the pallets."
        assert pov_tense(text, {"person": "first"}).score == 1.0

    def test_empty_input_is_unsupported(self) -> None:
        assert pov_tense("", _FIRST_PAST).unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_unicode_sentences_are_split_and_measured(self) -> None:
        result = pov_tense("Ich zählte. I counted the boxes.", {"person": "first"})
        assert result.detail["decidable_sentences"] >= 1

    def test_a_criterion_that_declares_neither_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="'person', 'tense' or both"):
            pov_tense("I counted.", {})

    @pytest.mark.parametrize(
        "parameters",
        [{"person": "fourth"}, {"tense": "future"}, {"person": "first", "tolerance": 2.0}],
    )
    def test_bad_parameters_are_refused(self, parameters: dict[str, object]) -> None:
        with pytest.raises(RuleInvalid):
            pov_tense("I counted.", parameters)
