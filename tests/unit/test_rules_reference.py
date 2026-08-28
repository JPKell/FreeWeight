"""The four rung-3 reference criteria: coverage and faithfulness against user ground truth.

These are where "did it make something up" stops needing a judge, so the case that matters most in
this file is the one where an output invents a number the source never carried.
"""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_GROUND_TRUTH,
    RuleInvalid,
)
from freeweight.domain.scorers.rules.reference import (
    claim_coverage,
    entity_recall,
    no_unsupported_claims,
    reference_similarity,
)

_SOURCE_TEXT = (
    "The racking inspector for this site is Delia Marchetti. Hazardous goods are received at "
    "bay 23 and nowhere else. The duty supervisor's radio callsign is Kestrel."
)
_SOURCE = {
    "text": _SOURCE_TEXT,
    "entities": ["Delia Marchetti", "Kestrel", "bay 23"],
    "claims": [
        {
            "id": "inspector",
            "text": "the racking inspector is Delia Marchetti",
            "any_of": ["Delia Marchetti"],
        },
        {"id": "bay", "text": "hazardous goods are received at bay 23", "any_of": ["bay 23"]},
        {
            "id": "callsign",
            "text": "the duty supervisor's callsign is Kestrel",
            "any_of": ["Kestrel"],
        },
    ],
    "references": [_SOURCE_TEXT],
}


class TestEntityRecall:
    def test_known_pass(self) -> None:
        text = (
            "Delia Marchetti inspects the racking; hazardous goods go to bay 23, callsign Kestrel."
        )
        result = entity_recall(text, {}, source=_SOURCE)
        assert result.score == 1.0
        assert result.detail["missing"] == []

    def test_known_fail_names_what_is_missing(self) -> None:
        result = entity_recall("Delia Marchetti inspects the racking.", {}, source=_SOURCE)
        assert result.score == pytest.approx(1 / 3)
        assert result.detail["missing"] == ["Kestrel", "bay 23"]

    def test_the_obvious_trip_is_a_summary_that_names_nobody(self) -> None:
        assert entity_recall("Some people inspect some things.", {}, source=_SOURCE).score == 0.0

    def test_the_criterion_may_override_the_task_list(self) -> None:
        result = entity_recall("Kestrel", {"entities": ["Kestrel"]}, source=_SOURCE)
        assert result.score == 1.0

    def test_no_ground_truth_is_unsupported(self) -> None:
        result = entity_recall("anything", {}, source=None)
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_NO_GROUND_TRUTH

    def test_empty_input_is_unsupported(self) -> None:
        assert entity_recall("", {}, source=_SOURCE).unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_unicode_entities_match_case_insensitively(self) -> None:
        source = {"entities": ["Größe"]}
        assert entity_recall("die größe", {}, source=source).score == 1.0

    def test_a_non_list_entity_override_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="list of strings"):
            entity_recall("x", {"entities": "Kestrel"}, source=_SOURCE)


class TestClaimCoverage:
    def test_known_pass(self) -> None:
        text = "Delia Marchetti, bay 23, Kestrel."
        assert claim_coverage(text, {}, source=_SOURCE).score == 1.0

    def test_known_fail_names_the_missing_claims(self) -> None:
        result = claim_coverage("Delia Marchetti.", {}, source=_SOURCE)
        assert result.score == pytest.approx(1 / 3)
        assert result.detail["missing"] == ["bay", "callsign"]

    def test_a_claim_without_phrases_falls_back_to_word_overlap(self) -> None:
        source = {"claims": [{"id": "c", "text": "the vault code was changed last year"}]}
        assert (
            claim_coverage("the vault code was changed last year", {}, source=source).score == 1.0
        )
        assert claim_coverage("nothing relevant", {}, source=source).score == 0.0

    def test_the_overlap_threshold_is_configurable(self) -> None:
        source = {"claims": [{"id": "c", "text": "vault code changed last year"}]}
        partial = "the vault code changed"
        assert claim_coverage(partial, {"min_overlap": 0.5}, source=source).score == 1.0
        assert claim_coverage(partial, {"min_overlap": 1.0}, source=source).score == 0.0

    def test_no_claims_is_unsupported(self) -> None:
        assert claim_coverage("x", {}, source={"text": "y"}).unsupported_reason == (
            UNSUPPORTED_NO_GROUND_TRUTH
        )

    def test_empty_input_is_unsupported(self) -> None:
        assert claim_coverage("", {}, source=_SOURCE).unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_a_claim_with_no_text_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="no 'text'"):
            claim_coverage("x", {}, source={"claims": [{"id": "c"}]})

    @pytest.mark.parametrize("overlap", [-0.1, 1.5, "half"])
    def test_a_bad_overlap_is_refused(self, overlap: object) -> None:
        with pytest.raises(RuleInvalid, match="min_overlap"):
            claim_coverage("x", {"min_overlap": overlap}, source=_SOURCE)


class TestNoUnsupportedClaims:
    def test_known_pass_everything_traces(self) -> None:
        text = "Delia Marchetti inspects the racking and hazardous goods go to bay 23."
        assert no_unsupported_claims(text, {}, source=_SOURCE).score == 1.0

    def test_the_obvious_trip_is_an_invented_number(self) -> None:
        text = "Hazardous goods are received at bay 23, and 41 pallets were rejected."
        result = no_unsupported_claims(text, {}, source=_SOURCE)
        assert result.score is not None
        assert result.score < 1.0
        assert "41" in result.detail["unsupported"]

    def test_an_invented_name_is_caught_too(self) -> None:
        result = no_unsupported_claims(
            "Rowan Beck inspects the racking.", {"check": ["entities"]}, source=_SOURCE
        )
        assert "Rowan Beck" in result.detail["unsupported"]

    def test_thousands_separators_are_normalized_before_tracing(self) -> None:
        source = {"text": "The site handled 1200 pallets."}
        assert (
            no_unsupported_claims(
                "It handled 1,200 pallets.", {"check": ["numbers"]}, source=source
            ).score
            == 1.0
        )

    def test_an_allow_list_covers_the_task_s_own_framing(self) -> None:
        result = no_unsupported_claims(
            "Summary of the site.", {"check": ["entities"], "allow": ["Summary"]}, source=_SOURCE
        )
        assert result.score == 1.0

    def test_a_response_that_asserts_nothing_is_unsupported_not_perfect(self) -> None:
        # It has fabricated nothing, and it has also not been measured for faithfulness.
        result = no_unsupported_claims("it depends.", {}, source=_SOURCE)
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_NO_GROUND_TRUTH

    def test_no_source_text_is_unsupported(self) -> None:
        assert no_unsupported_claims("Kestrel", {}, source={"entities": []}).unsupported_reason == (
            UNSUPPORTED_NO_GROUND_TRUTH
        )

    def test_empty_input_is_unsupported(self) -> None:
        assert no_unsupported_claims("", {}, source=_SOURCE).unsupported_reason == (
            UNSUPPORTED_EMPTY_TEXT
        )

    def test_an_unknown_class_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="does not trace"):
            no_unsupported_claims("x", {"check": ["dates"]}, source=_SOURCE)


class TestReferenceSimilarity:
    def test_known_pass_identical_text(self) -> None:
        assert reference_similarity(_SOURCE_TEXT, {}, source=_SOURCE).score == 1.0

    def test_known_fail_unrelated_text(self) -> None:
        result = reference_similarity("Completely different words entirely.", {}, source=_SOURCE)
        assert result.score is not None
        assert result.score < 0.2  # noqa: PLR2004 — the magnitude is the assertion

    def test_the_best_reference_wins(self) -> None:
        source = {"references": ["totally unrelated", _SOURCE_TEXT]}
        assert reference_similarity(_SOURCE_TEXT, {}, source=source).score == 1.0

    def test_sequence_ratio_is_available(self) -> None:
        result = reference_similarity(_SOURCE_TEXT, {"metric": "sequence_ratio"}, source=_SOURCE)
        assert result.detail["metric"] == "sequence_ratio"
        assert result.score == 1.0

    def test_a_band_turns_similarity_into_a_target_rather_than_a_maximum(self) -> None:
        # "Cover the same ground without copying it": too close is as wrong as too far.
        result = reference_similarity(_SOURCE_TEXT, {"min": 0.3, "max": 0.6}, source=_SOURCE)
        assert result.score is not None
        assert result.score < 1.0

    def test_no_references_is_unsupported(self) -> None:
        assert reference_similarity("x", {}, source={"text": "y"}).unsupported_reason == (
            UNSUPPORTED_NO_GROUND_TRUTH
        )

    def test_empty_input_is_unsupported(self) -> None:
        assert reference_similarity("", {}, source=_SOURCE).unsupported_reason == (
            UNSUPPORTED_EMPTY_TEXT
        )

    def test_unicode_is_compared_normally(self) -> None:
        source = {"references": ["Die Größe der Straße"]}
        assert reference_similarity("Die Größe der Straße", {}, source=source).score == 1.0

    def test_an_unknown_metric_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="metric"):
            reference_similarity("x", {"metric": "cosine"}, source=_SOURCE)
