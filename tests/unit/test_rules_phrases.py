"""``forbidden_phrases`` and ``required_phrases``.

Every rule type gets the full [Testing Standards §5](../../docs/standards/testing-standards.md)
metric-formula treatment, which the phase's own test list spells out: known-pass, known-fail,
boundary, empty input, unicode, and a text that trips it in an obvious way.
"""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import UNSUPPORTED_EMPTY_TEXT, RuleInvalid
from freeweight.domain.scorers.rules.phrases import forbidden_phrases, required_phrases

_BANNED = {"phrases": ["delve", "tapestry", "in today's landscape"]}


class TestForbiddenPhrases:
    def test_known_pass_scores_one(self) -> None:
        result = forbidden_phrases("I counted the pallets twice.", _BANNED)
        assert result.score == 1.0
        assert result.detail["matched_phrases"] == {}

    def test_known_fail_scores_below_one(self) -> None:
        result = forbidden_phrases("Let us delve into the tapestry.", _BANNED)
        assert result.score == pytest.approx(1 / 3)
        assert result.detail["matched_phrases"] == {"delve": 1, "tapestry": 1}

    def test_the_obvious_trip_is_repetition_of_one_phrase(self) -> None:
        # Three uses of one banned word is three hits, not one: a rule that scored presence could
        # not tell a slip from a habit.
        result = forbidden_phrases("delve, delve, delve", _BANNED)
        assert result.detail["hits"] == 3
        assert result.score == 0.0

    def test_boundary_at_max_hits(self) -> None:
        parameters = {**_BANNED, "max_hits": 2}
        assert forbidden_phrases("delve tapestry", parameters).score == 0.0
        assert forbidden_phrases("delve", parameters).score == 0.5

    def test_matching_is_case_insensitive_by_default(self) -> None:
        assert forbidden_phrases("Delve", _BANNED).score != 1.0
        assert forbidden_phrases("Delve", {**_BANNED, "case_sensitive": True}).score == 1.0

    def test_empty_input_is_unsupported_not_a_clean_response(self) -> None:
        # Scoring 1.0 would reward a model for answering nothing.
        result = forbidden_phrases("   \n ", _BANNED)
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_unicode_phrases_match(self) -> None:
        result = forbidden_phrases("Grüße aus dem Lager.", {"phrases": ["grüße"]})
        assert result.score == 0.0

    def test_an_empty_phrase_list_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="measures nothing"):
            forbidden_phrases("anything", {"phrases": []})

    def test_a_non_list_phrase_block_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="list of strings"):
            forbidden_phrases("anything", {"phrases": "delve"})

    @pytest.mark.parametrize("maximum", [0, -1, 1.5, True])
    def test_a_bad_max_hits_is_refused(self, maximum: object) -> None:
        with pytest.raises(RuleInvalid, match="max_hits"):
            forbidden_phrases("anything", {**_BANNED, "max_hits": maximum})


class TestRequiredPhrases:
    _REQUIRED = {"phrases": ["migration", "rollback", "downtime"]}

    def test_known_pass_scores_one(self) -> None:
        text = "The migration ran with no downtime and a tested rollback."
        assert required_phrases(text, self._REQUIRED).score == 1.0

    def test_known_fail_scores_the_fraction_present(self) -> None:
        result = required_phrases("The migration ran.", self._REQUIRED)
        assert result.score == pytest.approx(1 / 3)
        assert result.detail["missing"] == ["downtime", "rollback"]

    def test_the_obvious_trip_is_none_of_them(self) -> None:
        assert required_phrases("It went fine.", self._REQUIRED).score == 0.0

    def test_boundary_at_min_occurrences(self) -> None:
        parameters = {"phrases": ["rollback"], "min_occurrences": 2}
        assert required_phrases("rollback", parameters).score == 0.0
        assert required_phrases("rollback and rollback", parameters).score == 1.0

    def test_empty_input_is_unsupported(self) -> None:
        result = required_phrases("", self._REQUIRED)
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_unicode_matching_uses_casefolding_not_lower(self) -> None:
        # ``ß`` casefolds to ``ss``, so "GRÖSSE" and "Größe" are the same word. ``lower()`` would
        # not have found it, which is why the rules casefold.
        assert required_phrases("Größe des Lagers", {"phrases": ["GRÖSSE"]}).score == 1.0
        assert required_phrases("Größe des Lagers", {"phrases": ["größe"]}).score == 1.0
        assert required_phrases("Große Halle", {"phrases": ["größe"]}).score == 0.0

    @pytest.mark.parametrize("minimum", [0, -1, 2.5, True])
    def test_a_bad_min_occurrences_is_refused(self, minimum: object) -> None:
        with pytest.raises(RuleInvalid, match="min_occurrences"):
            required_phrases("anything", {**self._REQUIRED, "min_occurrences": minimum})
