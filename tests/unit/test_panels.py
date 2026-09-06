"""The A-2 panel's composition: three parts, a fixed regression panel, and no inheritance.

Phase 15 / ADR-0059 §2, [benchmark catalogue §8](../../docs/apps/freeweight/benchmark-catalog.md).
The panel is what decides what is known about an adapter subject, so the two things asserted
hardest here are that the regression panel does not vary with the manifest, and that the base's
scores choose a *suite to run* rather than becoming the subject's evidence.
"""

from __future__ import annotations

from freeweight.domain.capability_mapping import parse_mapping
from freeweight.domain.panels import (
    FIXED_REGRESSION_SUITES,
    PERFORMANCE_SUITE,
    REGRESSION_PANEL_VERSION,
    compose_panel,
    strongest_capability,
)


def _mapping() -> object:
    """A small mapping in the shipped file's shape, with the suites the panel names."""
    return parse_mapping(
        {
            "version": "test-1",
            "capabilities": {
                "instruction_following": {
                    "sources": [
                        {
                            "suite": "native.instruction_following",
                            "metric_key": "compliance_rate",
                            "weight": 1.0,
                        }
                    ]
                },
                "structured_output": {
                    "sources": [
                        {
                            "suite": "native.structured_output",
                            "metric_key": "valid_rate",
                            "weight": 1.0,
                        }
                    ]
                },
                "speed": {
                    "sources": [
                        {"suite": "native.performance", "metric_key": "decode_tps", "weight": 1.0}
                    ]
                },
                "coding": {
                    "sources": [{"suite": "native.agent", "metric_key": "pass_rate", "weight": 1.0}]
                },
            },
        }
    )


class TestTheThreeParts:
    """Declared + regression + performance, and nothing else."""

    def test_a_panel_always_has_exactly_the_three_parts(self) -> None:
        panel = compose_panel(_mapping())  # type: ignore[arg-type]

        assert [part.name for part in panel.parts] == ["declared", "regression", "performance"]

    def test_the_declared_part_is_the_manifests_claim(self) -> None:
        panel = compose_panel(_mapping(), declared_capabilities=("coding",))  # type: ignore[arg-type]

        assert panel.part("declared").suites == ("native.agent",)

    def test_a_manifest_declaring_nothing_has_an_empty_declared_part(self) -> None:
        """A fact about the manifest, not a gap in the panel — the part is still there."""
        panel = compose_panel(_mapping())  # type: ignore[arg-type]

        assert panel.part("declared").suites == ()

    def test_performance_is_always_in_the_panel(self) -> None:
        """A LoRA is extra work per token, so the base's throughput is not this subject's."""
        panel = compose_panel(_mapping())  # type: ignore[arg-type]

        assert panel.part("performance").suites == (PERFORMANCE_SUITE,)

    def test_a_declared_term_no_suite_measures_is_reported_not_dropped(self) -> None:
        """A manifest claiming something unmeasurable is a fact about the claim."""
        panel = compose_panel(_mapping(), declared_capabilities=("coding", "telepathy"))  # type: ignore[arg-type]

        assert panel.unmapped_capabilities == ("telepathy",)
        assert panel.part("declared").suites == ("native.agent",)

    def test_a_goal_capability_needs_no_special_case(self) -> None:
        """A house-voice LoRA scored by a calibrated house-voice goal is the intended pairing."""
        panel = compose_panel(_mapping(), declared_capabilities=("user.house_voice",))  # type: ignore[arg-type]

        assert panel.part("declared").suites == ("goal.house_voice",)


class TestTheFixedRegressionPanel:
    """The part nobody would think to run, and the part that must not vary."""

    def test_the_first_two_rows_are_the_same_whatever_the_manifest_declares(self) -> None:
        """Two adapters' regression numbers are only comparable if the panel is one thing."""
        first = compose_panel(_mapping(), declared_capabilities=("coding",))  # type: ignore[arg-type]
        second = compose_panel(_mapping(), declared_capabilities=("user.house_voice",))  # type: ignore[arg-type]

        for panel in (first, second):
            assert panel.part("regression").suites[:2] == FIXED_REGRESSION_SUITES

    def test_the_third_row_is_the_bases_strongest_measured_capability(self) -> None:
        panel = compose_panel(
            _mapping(),  # type: ignore[arg-type]
            base_scores={"coding": 0.81, "speed": 0.40},
        )

        assert panel.regression_third_row == "native.agent"
        assert panel.part("regression").suites == (*FIXED_REGRESSION_SUITES, "native.agent")

    def test_an_unmeasured_base_leaves_the_third_row_absent_and_says_why(self) -> None:
        """The rule cannot invent a strongest capability; ADR-0016 is why it does not try."""
        panel = compose_panel(_mapping(), base_scores={})  # type: ignore[arg-type]

        assert panel.regression_third_row is None
        assert panel.part("regression").suites == FIXED_REGRESSION_SUITES
        assert "measure the base first" in panel.part("regression").reason

    def test_the_third_row_is_not_duplicated_when_it_is_already_fixed(self) -> None:
        panel = compose_panel(
            _mapping(),  # type: ignore[arg-type]
            base_scores={"instruction_following": 0.9},
        )

        assert panel.part("regression").suites == FIXED_REGRESSION_SUITES
        assert len(set(panel.suites)) == len(panel.suites)

    def test_the_panel_records_its_version(self) -> None:
        """So two subjects' regression numbers are only compared under the same panel."""
        assert compose_panel(_mapping()).panel_version == REGRESSION_PANEL_VERSION  # type: ignore[arg-type]

    def test_a_suite_the_registry_cannot_run_is_left_out_at_composition(self) -> None:
        """Better a short panel a person can see than a run that fails halfway."""
        panel = compose_panel(
            _mapping(),  # type: ignore[arg-type]
            available_suites=["native.instruction_following", PERFORMANCE_SUITE],
        )

        assert panel.part("regression").suites == ("native.instruction_following",)
        assert panel.suites == ("native.instruction_following", PERFORMANCE_SUITE)


class TestStrongestCapability:
    """The one place the base's evidence is read, and it chooses a suite, not a score."""

    def test_the_highest_score_wins(self) -> None:
        assert strongest_capability({"coding": 0.4, "speed": 0.9}) == "speed"

    def test_a_tie_breaks_on_the_capability_id_so_the_choice_is_deterministic(self) -> None:
        assert strongest_capability({"speed": 0.9, "coding": 0.9}) == "coding"

    def test_an_unmeasured_base_has_no_strongest_capability(self) -> None:
        assert strongest_capability({}) is None
