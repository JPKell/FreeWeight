"""``readability``: three published indices against a declared band."""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    RuleInvalid,
)
from freeweight.domain.scorers.rules.readability import (
    MINIMUM_WORDS,
    UNSUPPORTED_TOO_SHORT,
    count_syllables,
    readability,
)

_PLAIN = (
    "The van came at six. We loaded it in the yard and drove north. "
    "The road was clear and the load was light. We got there before dark. "
    "The crew took the boxes off and we went home. It was a good day and "
    "nobody had to work late for once."
)
_DENSE = (
    "Notwithstanding the aforementioned considerations regarding operational "
    "infrastructure, the organisation's implementation methodology necessitates "
    "comprehensive reconsideration of intercontinental distribution architecture, "
    "particularly insofar as the associated administrative responsibilities "
    "demonstrably constitute an impediment to organisational effectiveness."
)


class TestReadability:
    def test_known_pass_plain_prose_is_low_grade(self) -> None:
        result = readability(_PLAIN, {"metric": "flesch_kincaid_grade", "min": 0, "max": 8})
        assert result.score == 1.0
        assert result.detail["value"] < 8  # noqa: PLR2004 — the band's own bound

    def test_the_obvious_trip_is_dense_prose(self) -> None:
        result = readability(_DENSE, {"metric": "flesch_kincaid_grade", "min": 0, "max": 8})
        assert result.score == 0.0
        assert result.detail["value"] > 12  # noqa: PLR2004 — well outside the band

    def test_known_fail_is_reported_with_all_three_indices(self) -> None:
        result = readability(_DENSE, {"min": 0, "max": 8})
        for name in ("flesch_reading_ease", "flesch_kincaid_grade", "gunning_fog"):
            assert name in result.detail

    def test_gunning_fog_can_be_chosen(self) -> None:
        result = readability(_PLAIN, {"metric": "gunning_fog", "min": 0, "max": 10})
        assert result.detail["metric"] == "gunning_fog"
        assert result.score == 1.0

    def test_boundary_a_response_one_word_short_is_unsupported(self) -> None:
        # These indices are ratios with tiny denominators in a short answer, so this module stops
        # answering rather than reporting a grade level computed from two sentences.
        short = " ".join("word" for _ in range(MINIMUM_WORDS - 1)) + "."
        result = readability(short, {"min": 0, "max": 8})
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_TOO_SHORT

    def test_boundary_at_exactly_the_minimum_is_measured(self) -> None:
        exact = " ".join("word" for _ in range(MINIMUM_WORDS)) + "."
        assert readability(exact, {"min": 0, "max": 40}).score == 1.0

    def test_empty_input_is_unsupported(self) -> None:
        assert readability("", {"min": 0, "max": 8}).unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_no_band_is_unsupported(self) -> None:
        assert readability(_PLAIN, {}).unsupported_reason == UNSUPPORTED_NO_BAND

    def test_unicode_prose_is_measured_without_raising(self) -> None:
        text = " ".join(["Die Größe der Straße war für die Fahrer völlig überraschend."] * 6)
        result = readability(text, {"min": 0, "max": 40})
        assert result.score is not None

    def test_an_unknown_metric_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="metric"):
            readability(_PLAIN, {"metric": "smog", "min": 0, "max": 8})

    def test_a_non_numeric_tolerance_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="tolerance"):
            readability(_PLAIN, {"min": 0, "max": 8, "tolerance": "wide"})


class TestSyllableHeuristic:
    """Declared as an approximation in the module docstring, and asserted as one here."""

    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("cat", 1),
            ("hidden", 2),
            ("inventory", 4),
            ("make", 1),
            ("the", 1),
            # The heuristic counts vowel *groups*, so "idea" is two rather than three. Named
            # here because the module declares itself an approximation and this is where the
            # approximation is visible.
            ("idea", 2),
            ("", 1),
        ],
    )
    def test_known_values(self, word: str, expected: int) -> None:
        assert count_syllables(word) == expected

    def test_never_returns_zero(self) -> None:
        assert count_syllables("rhythm") >= 1
