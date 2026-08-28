"""``vocabulary_profile``: type-token ratio, rare-word rate and a banned register list."""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    RuleInvalid,
)
from freeweight.domain.scorers.rules.vocabulary import (
    DEFAULT_RARE_WORD_CHARACTERS,
    vocabulary_profile,
)

_VARIED = "the van came at six and we loaded boxes into it before dark without any fuss"
_REPETITIVE = "the the the the the the the the the the"


class TestVocabularyProfile:
    def test_known_pass_on_the_ratio_band(self) -> None:
        result = vocabulary_profile(_VARIED, {"type_token_ratio": {"min": 0.8}})
        assert result.score == 1.0
        assert result.detail["type_token_ratio"] > 0.8  # noqa: PLR2004 — the band's own bound

    def test_the_obvious_trip_is_one_word_repeated(self) -> None:
        result = vocabulary_profile(_REPETITIVE, {"type_token_ratio": {"min": 0.8}})
        assert result.detail["type_token_ratio"] == pytest.approx(0.1)
        assert result.score == 0.0

    def test_a_banned_register_word_costs_its_share(self) -> None:
        parameters = {"banned_register": ["leverage", "synergy", "ideate", "operationalize"]}
        clean = vocabulary_profile(_VARIED, parameters)
        assert clean.score == 1.0
        dirty = vocabulary_profile(f"{_VARIED} leverage synergy", parameters)
        assert dirty.score == pytest.approx(0.5)
        assert dirty.detail["banned_register_present"] == ["leverage", "synergy"]

    def test_rarity_defaults_to_a_declared_length_proxy(self) -> None:
        text = "the incomprehensibility of it all was notwithstanding a small thing"
        result = vocabulary_profile(text, {"rare_word_rate": {"max": 0.05}})
        assert result.detail["rarity_basis"] == f"length>={DEFAULT_RARE_WORD_CHARACTERS}"
        assert result.detail["rare_word_rate"] > 0.05  # noqa: PLR2004 — the band's own bound
        assert result.score is not None
        assert result.score < 1.0

    def test_a_supplied_common_word_list_replaces_the_proxy(self) -> None:
        result = vocabulary_profile(
            "van box crate pallet",
            {"rare_word_rate": {"max": 0.5}, "common_words": ["van", "box"]},
        )
        assert result.detail["rarity_basis"] == "common_words"
        assert result.detail["rare_word_rate"] == pytest.approx(0.5)
        assert result.score == 1.0

    def test_the_word_count_travels_because_the_ratio_depends_on_it(self) -> None:
        short = vocabulary_profile("one two three", {"type_token_ratio": {"min": 0.5}})
        assert short.detail["word_count"] == 3  # noqa: PLR2004 — the count is the assertion

    def test_boundary_at_the_band_edge(self) -> None:
        assert vocabulary_profile(_REPETITIVE, {"type_token_ratio": {"min": 0.1}}).score == 1.0

    def test_empty_input_is_unsupported(self) -> None:
        result = vocabulary_profile("   ", {"type_token_ratio": {"min": 0.5}})
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_a_criterion_asking_for_nothing_is_unsupported(self) -> None:
        assert vocabulary_profile(_VARIED, {}).unsupported_reason == UNSUPPORTED_NO_BAND

    def test_unicode_words_are_counted(self) -> None:
        result = vocabulary_profile("Größe Straße Grüße", {"type_token_ratio": {"min": 0.9}})
        assert result.detail["distinct_words"] == 3  # noqa: PLR2004 — the count is the assertion
        assert result.score == 1.0

    @pytest.mark.parametrize("threshold", [0, -3, 1.5, True])
    def test_a_bad_rare_word_threshold_is_refused(self, threshold: object) -> None:
        with pytest.raises(RuleInvalid, match="rare_word_characters"):
            vocabulary_profile(_VARIED, {"rare_word_characters": threshold})

    def test_a_non_list_banned_register_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="list of strings"):
            vocabulary_profile(_VARIED, {"banned_register": "leverage"})
