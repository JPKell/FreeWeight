"""ADR-0017's six factors, individually and combined, against hand-computed values.

Every number here is worked out by hand from the ADR's table first and asserted second, so a test
that fails is a formula that changed rather than a formula that was re-derived to match the code.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from baseaicore import MetricKind

from freeweight.domain.confidence import (
    CONFIDENCE_CEILING,
    CONFIDENCE_FLOOR,
    ConfidencePolicy,
    Environment,
    SeparationKey,
    compute_confidence,
    consistency_factor,
    detect_drift,
    environment_factor,
    freshness_factor,
    identity_factor,
    is_stale,
    sample_factor,
    separations,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ENV = Environment(
    provider_kind="ollama",
    provider_version="0.32.13",
    gpu_driver_version="580.65.06",
    cuda_version="13.0",
    os_version="Ubuntu 26.04 LTS",
)


class TestSampleFactor:
    def test_three_samples_are_worth_about_a_third(self) -> None:
        assert sample_factor(3) == pytest.approx(math.sqrt(3 / 30))
        assert sample_factor(3) == pytest.approx(0.316, abs=1e-3)

    def test_thirty_samples_reach_one(self) -> None:
        assert sample_factor(30) == 1.0

    def test_more_than_the_target_is_capped_not_rewarded(self) -> None:
        assert sample_factor(300) == 1.0

    def test_zero_samples_is_zero(self) -> None:
        assert sample_factor(0) == 0.0

    def test_the_target_is_configuration(self) -> None:
        assert sample_factor(10, n_target=10) == 1.0

    def test_a_negative_count_is_refused(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            sample_factor(-1)


class TestConsistencyFactor:
    def test_tight_dispersion_costs_little(self) -> None:
        assert consistency_factor(0.1) == pytest.approx(0.9)

    def test_the_penalty_is_capped_at_a_half(self) -> None:
        assert consistency_factor(0.8) == 0.5
        assert consistency_factor(5.0) == 0.5

    def test_an_undefined_dispersion_is_not_a_penalty(self) -> None:
        """A single observation is already tiny through the sample factor; not twice."""
        assert consistency_factor(None) == 1.0

    def test_a_negative_dispersion_is_refused(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            consistency_factor(-0.1)


class TestFreshnessFactor:
    def test_fresh_is_one(self) -> None:
        assert freshness_factor(measured_at=NOW, now=NOW, half_life_days=90) == 1.0

    def test_one_half_life_is_a_half(self) -> None:
        measured = NOW - timedelta(days=90)
        assert freshness_factor(measured_at=measured, now=NOW, half_life_days=90) == pytest.approx(
            0.5
        )

    def test_performance_evidence_decays_on_the_short_half_life(self) -> None:
        measured = NOW - timedelta(days=30)
        assert freshness_factor(measured_at=measured, now=NOW, half_life_days=30) == pytest.approx(
            0.5
        )
        assert freshness_factor(measured_at=measured, now=NOW, half_life_days=90) == pytest.approx(
            0.5 ** (30 / 90)
        )

    def test_the_floor_holds_old_evidence_usable(self) -> None:
        measured = NOW - timedelta(days=3650)
        assert freshness_factor(measured_at=measured, now=NOW, half_life_days=30) == 0.3

    def test_a_measurement_from_the_future_is_treated_as_now(self) -> None:
        assert (
            freshness_factor(measured_at=NOW + timedelta(days=5), now=NOW, half_life_days=30) == 1.0
        )

    def test_a_naive_instant_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            freshness_factor(measured_at=datetime(2026, 8, 1), now=NOW, half_life_days=30)  # noqa: DTZ001


class TestEnvironmentFactor:
    def test_no_drift_is_one(self) -> None:
        assert environment_factor(ENV, ENV, kind=MetricKind.PERFORMANCE) == 1.0

    def test_an_unknown_current_environment_drifts_nothing(self) -> None:
        assert environment_factor(ENV, None, kind=MetricKind.PERFORMANCE) == 1.0
        assert detect_drift(ENV, None, kind=MetricKind.QUALITY) == ()

    def test_a_driver_change_discounts_performance_evidence(self) -> None:
        current = replace(ENV, gpu_driver_version="585.10.01")
        assert environment_factor(ENV, current, kind=MetricKind.PERFORMANCE) == pytest.approx(0.7)
        assert environment_factor(ENV, current, kind=MetricKind.MEMORY) == pytest.approx(0.7)

    def test_a_driver_change_leaves_quality_evidence_alone(self) -> None:
        current = replace(ENV, gpu_driver_version="585.10.01")
        assert environment_factor(ENV, current, kind=MetricKind.QUALITY) == 1.0
        assert detect_drift(ENV, current, kind=MetricKind.QUALITY) == ()

    def test_a_provider_minor_change_discounts_performance_not_quality(self) -> None:
        current = replace(ENV, provider_version="0.33.0")
        assert environment_factor(ENV, current, kind=MetricKind.PERFORMANCE) == pytest.approx(0.7)
        assert environment_factor(ENV, current, kind=MetricKind.QUALITY) == 1.0
        assert detect_drift(ENV, current, kind=MetricKind.QUALITY) == ("provider_minor",)

    def test_a_provider_major_change_discounts_quality_by_half(self) -> None:
        current = replace(ENV, provider_version="1.0.0")
        assert environment_factor(ENV, current, kind=MetricKind.QUALITY) == pytest.approx(0.5)
        assert environment_factor(ENV, current, kind=MetricKind.PERFORMANCE) == pytest.approx(0.5)

    def test_a_provider_patch_is_not_drift(self) -> None:
        current = replace(ENV, provider_version="0.32.14")
        assert detect_drift(ENV, current, kind=MetricKind.PERFORMANCE) == ()

    def test_an_os_patch_level_is_never_drift(self) -> None:
        current = replace(ENV, os_version="Ubuntu 26.04.1 LTS")
        assert environment_factor(ENV, current, kind=MetricKind.PERFORMANCE) == 1.0
        assert environment_factor(ENV, current, kind=MetricKind.QUALITY) == 1.0

    def test_two_drifted_dimensions_discount_once_not_twice(self) -> None:
        current = replace(ENV, gpu_driver_version="585.10.01", cuda_version="13.1")
        assert environment_factor(ENV, current, kind=MetricKind.ENERGY) == pytest.approx(0.7)
        assert detect_drift(ENV, current, kind=MetricKind.ENERGY) == (
            "gpu_driver_version",
            "cuda_version",
        )


class TestIdentityFactor:
    def test_a_digest_identity_is_one(self) -> None:
        assert identity_factor("digest") == 1.0

    def test_a_name_only_identity_is_discounted(self) -> None:
        assert identity_factor("name_only") == pytest.approx(0.6)

    def test_the_discount_is_configuration(self) -> None:
        policy = ConfidencePolicy(name_only_identity_factor=0.5)
        assert identity_factor("name_only", policy=policy) == 0.5


class TestStaleness:
    def test_below_about_one_half_life_is_stale(self) -> None:
        assert is_stale(freshness=0.49, drift=()) is True
        assert is_stale(freshness=0.5, drift=()) is False

    def test_any_drift_is_stale_regardless_of_freshness(self) -> None:
        assert is_stale(freshness=1.0, drift=("gpu_driver_version",)) is True


class TestCombined:
    """The product, against a value computed by hand from the ADR's table."""

    def test_the_worked_example(self) -> None:
        # 12 samples: sqrt(12/30) = 0.632; CV 0.2: 0.8; 45 days at 90: 0.5**0.5 = 0.707;
        # no drift: 1.0; name_only: 0.6; validity 1.0 → 0.632 × 0.8 × 0.707 × 0.6 = 0.2147.
        breakdown = compute_confidence(
            sample_count=12,
            dispersion=0.2,
            measured_at=NOW - timedelta(days=45),
            now=NOW,
            kind=MetricKind.QUALITY,
            measured_environment=ENV,
            current_environment=ENV,
            identity_confidence="name_only",
        )
        assert breakdown.sample_factor == pytest.approx(math.sqrt(12 / 30))
        assert breakdown.consistency_factor == pytest.approx(0.8)
        assert breakdown.freshness_factor == pytest.approx(0.5**0.5)
        assert breakdown.environment_factor == 1.0
        assert breakdown.identity_factor == pytest.approx(0.6)
        assert breakdown.judge_validity_factor == 1.0
        assert breakdown.confidence == pytest.approx(0.2147, abs=1e-3)
        assert breakdown.age_days == pytest.approx(45.0)
        assert breakdown.half_life_days == 90.0
        assert breakdown.stale is False

    def test_the_sixth_factor_multiplies_in(self) -> None:
        base = compute_confidence(
            sample_count=30,
            dispersion=None,
            measured_at=NOW,
            now=NOW,
            kind=MetricKind.QUALITY,
            measured_environment=ENV,
            current_environment=ENV,
            identity_confidence="digest",
        )
        judged = compute_confidence(
            sample_count=30,
            dispersion=None,
            measured_at=NOW,
            now=NOW,
            kind=MetricKind.QUALITY,
            measured_environment=ENV,
            current_environment=ENV,
            identity_confidence="digest",
            judge_validity_factor=0.55,
        )
        assert base.confidence == 1.0
        assert judged.confidence == pytest.approx(0.55)

    def test_the_floor_and_the_ceiling(self) -> None:
        low = compute_confidence(
            sample_count=1,
            dispersion=0.9,
            measured_at=NOW - timedelta(days=400),
            now=NOW,
            kind=MetricKind.PERFORMANCE,
            measured_environment=ENV,
            current_environment=replace(ENV, cuda_version="12.0"),
            identity_confidence="name_only",
        )
        assert low.confidence == CONFIDENCE_FLOOR
        assert low.stale is True
        assert low.drift == ("cuda_version",)
        high = compute_confidence(
            sample_count=1000,
            dispersion=0.0,
            measured_at=NOW,
            now=NOW,
            kind=MetricKind.QUALITY,
            measured_environment=ENV,
            current_environment=ENV,
            identity_confidence="digest",
        )
        assert high.confidence == CONFIDENCE_CEILING

    def test_freshness_decays_from_measured_at_not_from_now(self) -> None:
        """Recomputing later with the same measured_at yields a *lower* number, never higher."""
        first = compute_confidence(
            sample_count=30,
            dispersion=None,
            measured_at=NOW - timedelta(days=10),
            now=NOW,
            kind=MetricKind.QUALITY,
            measured_environment=ENV,
            current_environment=ENV,
            identity_confidence="digest",
        )
        later = compute_confidence(
            sample_count=30,
            dispersion=None,
            measured_at=NOW - timedelta(days=10),
            now=NOW + timedelta(days=80),
            kind=MetricKind.QUALITY,
            measured_environment=ENV,
            current_environment=ENV,
            identity_confidence="digest",
        )
        assert later.confidence < first.confidence

    def test_a_validity_factor_outside_the_unit_interval_is_refused(self) -> None:
        with pytest.raises(ValueError, match="judge_validity_factor"):
            compute_confidence(
                sample_count=1,
                dispersion=None,
                measured_at=NOW,
                now=NOW,
                kind=MetricKind.QUALITY,
                measured_environment=ENV,
                current_environment=None,
                identity_confidence="digest",
                judge_validity_factor=1.5,
            )


class TestPolicy:
    def test_the_defaults_are_the_adr_table(self) -> None:
        policy = ConfidencePolicy()
        assert policy.n_target == 30
        assert policy.quality_half_life_days == 90.0
        assert policy.performance_half_life_days == 30.0
        assert policy.freshness_floor == 0.3
        assert policy.stale_below == 0.5
        assert policy.name_only_identity_factor == 0.6
        assert policy.performance_drift_factor == 0.7
        assert policy.quality_drift_factor == 0.5
        assert policy.is_default

    def test_the_shipped_policy_version_is_the_mapping_version(self) -> None:
        assert ConfidencePolicy().policy_version(mapping_version="1.0") == "1.0"

    def test_a_customised_policy_derives_its_own_version(self) -> None:
        custom = ConfidencePolicy(n_target=20)
        version = custom.policy_version(mapping_version="1.0")
        assert version.startswith("1.0+")
        assert version != ConfidencePolicy(n_target=25).policy_version(mapping_version="1.0")

    def test_a_meaningless_parameter_is_refused(self) -> None:
        with pytest.raises(ValueError, match="n_target"):
            ConfidencePolicy(n_target=0)
        with pytest.raises(ValueError, match="stale_below"):
            ConfidencePolicy(stale_below=1.5)


class TestHardSeparations:
    """Never a discount: the dimensions that differ are named, so a caller partitions."""

    def test_identical_keys_may_merge(self) -> None:
        key = SeparationKey(suite_version="1.0.0", artifact_digest="sha256:aa")
        assert separations(key, key) == ()

    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            ({"suite_version": "1.1.0"}, "suite_version"),
            ({"dataset_hashes": {"d": "2"}}, "dataset_hashes"),
            ({"prompt_subset_hash": "sha256:other"}, "prompt_subset_hash"),
            ({"artifact_digest": "sha256:bb"}, "artifact_digest"),
            ({"runtime_profile_hash": "other"}, "runtime_profile_hash"),
            ({"goal_hash": "sha256:rubric2"}, "goal_hash"),
            ({"judge_set": "jury2"}, "judge_set"),
        ],
    )
    def test_each_dimension_separates(self, change: dict[str, Any], expected: str) -> None:
        left = SeparationKey(
            suite_version="1.0.0",
            dataset_hashes={"d": "1"},
            prompt_subset_hash="sha256:p",
            artifact_digest="sha256:aa",
            runtime_profile_hash="p1",
            goal_hash="sha256:rubric1",
            judge_set="jury1",
        )
        right = replace(left, **change)
        assert separations(left, right) == (expected,)

    def test_the_machine_separates_performance_and_only_badges_quality(self) -> None:
        left = SeparationKey(suite_version="1.0.0", machine_fingerprint="m1")
        right = SeparationKey(suite_version="1.0.0", machine_fingerprint="m2")
        assert separations(left, right, kind=MetricKind.PERFORMANCE) == ("machine_fingerprint",)
        assert separations(left, right, kind=MetricKind.QUALITY) == ()
