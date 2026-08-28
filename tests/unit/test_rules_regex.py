"""``regex_match``: the one rule whose input is code the user wrote.

The dialect lint is the primary guard and is asserted here; the per-criterion timeout is the
backstop and is asserted in ``test_composite.py``, where the machinery that applies it lives.
"""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import (
    MAXIMUM_PATTERN_LENGTH,
    UNSUPPORTED_EMPTY_TEXT,
    RuleInvalid,
    lint_pattern,
)
from freeweight.domain.scorers.rules.regex import UNSUPPORTED_NO_PATTERN, regex_match


class TestRegexMatch:
    def test_known_pass_in_search_mode(self) -> None:
        result = regex_match("bay 23 is closed", {"pattern": r"bay \d+"})
        assert result.score == 1.0
        assert result.detail["match_count"] == 1

    def test_known_fail(self) -> None:
        # A pattern that ran and did not match has measured something: this is the one place the
        # unsupported/zero distinction inverts.
        result = regex_match("no bays here", {"pattern": r"bay \d+"})
        assert result.score == 0.0
        assert result.unsupported_reason is None

    def test_negate_inverts_a_boolean_result(self) -> None:
        parameters = {"pattern": r"\bTODO\b", "negate": True}
        assert regex_match("clean text", parameters).score == 1.0
        assert regex_match("TODO: fix", parameters).score == 0.0

    def test_count_mode_scores_against_a_band(self) -> None:
        parameters = {"pattern": r"\d+", "mode": "count", "min": 3}
        assert regex_match("1 2 3", parameters).score == 1.0
        thin = regex_match("1", parameters).score
        assert thin is not None
        assert thin < 1.0

    def test_fullmatch_mode_is_strict(self) -> None:
        parameters = {"pattern": r"[A-Z]{3}-\d{4}", "mode": "fullmatch"}
        assert regex_match("ABC-1234", parameters).score == 1.0
        assert regex_match("see ABC-1234 for detail", parameters).score == 0.0

    def test_flags_are_declared_not_embedded(self) -> None:
        parameters = {"pattern": r"^bay", "flags": ["ignorecase", "multiline"]}
        assert regex_match("closed\nBay 23", parameters).score == 1.0

    def test_the_obvious_trip_is_a_pattern_that_matches_nothing_it_should(self) -> None:
        assert regex_match("Bay 23", {"pattern": r"bay \d+"}).score == 0.0

    def test_empty_input_is_unsupported(self) -> None:
        result = regex_match("", {"pattern": "x"})
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_no_pattern_is_unsupported(self) -> None:
        assert regex_match("text", {}).unsupported_reason == UNSUPPORTED_NO_PATTERN

    def test_unicode_patterns_and_text(self) -> None:
        assert regex_match("Die Größe stimmt", {"pattern": r"Größe"}).score == 1.0

    def test_count_mode_without_a_band_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="'count' mode"):
            regex_match("1 2 3", {"pattern": r"\d", "mode": "count"})

    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="mode"):
            regex_match("x", {"pattern": "x", "mode": "fuzzy"})

    def test_an_unknown_flag_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="flag"):
            regex_match("x", {"pattern": "x", "flags": ["verbose"]})


class TestTheDialectLint:
    """Spec §14's linted dialect: the cheap guard that makes the timeout rarely needed."""

    def test_an_ordinary_pattern_compiles(self) -> None:
        assert lint_pattern(r"bay \d{1,3}").pattern == r"bay \d{1,3}"

    @pytest.mark.parametrize("pattern", [r"(\w+)\1", r"(?P<x>a)(?P=x)"])
    def test_a_backreference_is_refused(self, pattern: str) -> None:
        with pytest.raises(RuleInvalid, match="backreference"):
            lint_pattern(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [r"(a+)+b", r"(x*)*y", r"(?:a|a?)+", r"(?:a|aa)+", r"(a|ab)*c", r"(?:ab){2,}"],
    )
    def test_unbounded_repetition_of_a_group_is_refused(self, pattern: str) -> None:
        # This is where catastrophic backtracking lives, and a running match cannot be
        # interrupted — so it is refused before it can ever run rather than timed out afterwards.
        with pytest.raises(RuleInvalid, match="repeats a group without an upper bound"):
            lint_pattern(pattern)

    @pytest.mark.parametrize("pattern", [r"(?:ab){1,5}", r"\d+", r"[a-z]*", r".{0,80}"])
    def test_bounded_repetition_and_class_quantifiers_are_accepted(self, pattern: str) -> None:
        # Their work is linear in the input, so nothing here can run away.
        assert lint_pattern(pattern) is not None

    def test_an_over_long_pattern_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="the limit is"):
            lint_pattern("a" * (MAXIMUM_PATTERN_LENGTH + 1))

    def test_a_pattern_that_does_not_compile_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="does not compile"):
            lint_pattern("(unclosed")

    def test_a_pattern_at_exactly_the_length_cap_is_accepted(self) -> None:
        assert lint_pattern("a" * MAXIMUM_PATTERN_LENGTH) is not None
