"""``repetition``: n-gram self-repetition, scored against a ceiling."""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import UNSUPPORTED_EMPTY_TEXT, RuleInvalid
from freeweight.domain.scorers.rules.repetition import (
    DEFAULT_N,
    UNSUPPORTED_TOO_SHORT,
    repetition,
)

_VARIED = (
    "the van came at six and we loaded the boxes before dark without any fuss at all "
    "then drove north through empty roads until the depot lights showed over the hedge"
)
_REPEATED = " ".join(["the same clause again and"] * 8)


class TestRepetition:
    def test_known_pass_on_varied_prose(self) -> None:
        result = repetition(_VARIED, {})
        assert result.score == 1.0
        assert result.detail["repetition_rate"] == pytest.approx(0.0)

    def test_the_obvious_trip_is_one_clause_restated(self) -> None:
        result = repetition(_REPEATED, {"max_rate": 0.1})
        assert result.detail["repetition_rate"] > 0.5  # noqa: PLR2004 — the magnitude is the point
        assert result.score == 0.0
        assert result.detail["most_repeated"]

    def test_lower_is_better_so_the_score_inverts(self) -> None:
        clean = repetition(_VARIED, {"max_rate": 0.1}).score
        dirty = repetition(_REPEATED, {"max_rate": 0.1}).score
        assert clean is not None
        assert dirty is not None
        assert clean > dirty

    def test_boundary_exactly_at_the_ceiling(self) -> None:
        # Twelve 3-grams, one repeated: a rate of 1/12, scored against a ceiling of exactly that.
        text = "a b c d e f g h i j a b c"
        result = repetition(text, {"n": 3, "max_rate": 1 / 11})
        assert result.score == 1.0

    def test_a_response_shorter_than_n_is_unsupported(self) -> None:
        result = repetition("two words", {"n": 4})
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_TOO_SHORT

    def test_empty_input_is_unsupported(self) -> None:
        assert repetition("", {}).unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_the_default_n_is_declared(self) -> None:
        assert repetition(_VARIED, {}).detail["n"] == DEFAULT_N

    def test_unicode_words_are_folded_the_same_way(self) -> None:
        text = "Größe Straße Grüße Halle " * 4
        result = repetition(text, {"n": 4, "max_rate": 0.1})
        assert result.detail["repetition_rate"] > 0.1  # noqa: PLR2004 — the ceiling is the point

    @pytest.mark.parametrize("size", [1, 0, -2, "four", True])
    def test_a_bad_n_is_refused(self, size: object) -> None:
        with pytest.raises(RuleInvalid, match="'n'"):
            repetition(_VARIED, {"n": size})

    @pytest.mark.parametrize("ceiling", [-0.1, 1.5, "low"])
    def test_a_bad_max_rate_is_refused(self, ceiling: object) -> None:
        with pytest.raises(RuleInvalid, match="max_rate"):
            repetition(_VARIED, {"max_rate": ceiling})
