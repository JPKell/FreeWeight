"""Comparison rules: what merges, what is separated, and the diff that explains the separation.

Development plan, Phase 9: "Comparison refuses to merge across a 'separate' boundary and shows the
fingerprint diff." That is :class:`TestSeparateBoundariesNeverMerge` and
:class:`TestTheDiffExplainsTheSeparation`.

Acceptance criterion 2 — "comparing runs from different benchmark versions is refused with a clear
explanation" — is
:meth:`TestSeparateBoundariesNeverMerge.test_a_suite_version_change_separates_with_an_explanation`.

The matrix itself is BaseAiCore's and is tested there; what is tested here is the layer FreeWeight
adds — resolving ``indeterminate`` into a named study using the descriptor's family, partitioning
subjects into groups that may be merged, and refusing to pick a winner across a separation.
"""

from __future__ import annotations

import pytest
from baseaicore import Comparability, MeasurementSubject, MetricKind, ModelIdentity, ProviderKind

from freeweight.domain.comparison import (
    ComparisonSubject,
    StudyKind,
    group_subjects,
    metric_kind_for,
    separation_diff,
    verdict_for_pair,
)

_MACHINE = "sha256:" + "a" * 64
_OTHER_MACHINE = "sha256:" + "b" * 64


def _identity(name: str = "qwen3.5:9b-q8_0", digest: str = "sha256:" + "1" * 64) -> ModelIdentity:
    return ModelIdentity(
        provider_kind=ProviderKind.OLLAMA, provider_model_name=name, artifact_digest=digest
    )


def _name_only_identity(name: str = "qwen3.5:9b-q8_0") -> ModelIdentity:
    """Weights identified by name alone — no digest, so nothing pins them across time."""
    return ModelIdentity(
        provider_kind=ProviderKind.OLLAMA, provider_model_name=name, artifact_digest=None
    )


def _subject(
    run_id: str,
    *,
    identity: ModelIdentity | None = None,
    profile_hash: str = "profile-a",
    machine: str = _MACHINE,
    version: str = "1.0.0",
    key: str = "native.memory_kv",
    dataset_hashes: dict[str, str] | None = None,
    family: str | None = "qwen3.5",
    quantization: str | None = "Q8_0",
    document: dict[str, object] | None = None,
) -> ComparisonSubject:
    return ComparisonSubject(
        run_id=run_id,
        subject=MeasurementSubject(
            identity=identity or _identity(),
            runtime_profile_hash=profile_hash,
            machine_fingerprint=machine,
        ),
        benchmark_key=key,
        benchmark_version=version,
        dataset_hashes=dataset_hashes if dataset_hashes is not None else {},
        fingerprint=f"sha256:{run_id}",
        fingerprint_document=document
        if document is not None
        else {
            "benchmark": {"suite_key": key, "suite_version": version},
            "runtime_profile_hash": profile_hash,
            "machine_fingerprint": machine,
        },
        family=family,
        quantization=quantization,
        label=run_id,
    )


class TestMetricKindIsLookedUpNotGuessed:
    """Comparability across machines depends on the kind, so the kind may not be a heuristic."""

    @pytest.mark.parametrize(
        ("key", "kind"),
        [
            ("decode_tokens_per_second", MetricKind.PERFORMANCE),
            ("ttft_ms", MetricKind.PERFORMANCE),
            ("observed_kv_bytes_per_token", MetricKind.MEMORY),
            ("peak_vram_bytes", MetricKind.MEMORY),
            ("gpu_energy_joules", MetricKind.ENERGY),
            ("joules_per_output_token", MetricKind.ENERGY),
            ("answer_correct", MetricKind.QUALITY),
            ("task_success", MetricKind.QUALITY),
        ],
    )
    def test_declared_kinds(self, key: str, kind: MetricKind) -> None:
        assert metric_kind_for(key) is kind

    def test_an_unlisted_key_defaults_to_quality(self) -> None:
        # Quality is the conservative default: it is the only kind that survives a machine change,
        # so an unlisted key gets a caveat rather than a silent merge.
        assert metric_kind_for("some_metric_nobody_declared") is MetricKind.QUALITY


class TestSeparateBoundariesNeverMerge:
    """Every ``separate`` row of the matrix, with the study FreeWeight names it."""

    def test_a_suite_version_change_separates_with_an_explanation(self) -> None:
        left = _subject("run-a", version="1.0.0")
        right = _subject("run-b", version="1.1.0")
        verdict = verdict_for_pair(left, right, metric_kind=MetricKind.QUALITY)
        assert verdict.comparability is Comparability.SEPARATE
        assert verdict.study is StudyKind.INCOMPARABLE
        assert not verdict.may_merge
        assert "1.0.0" in verdict.reason
        assert "1.1.0" in verdict.reason
        assert "never averaged" in verdict.reason

    def test_a_dataset_hash_change_separates(self) -> None:
        left = _subject("run-a", dataset_hashes={"fixtures": "sha256:aa"})
        right = _subject("run-b", dataset_hashes={"fixtures": "sha256:bb"})
        verdict = verdict_for_pair(left, right, metric_kind=MetricKind.QUALITY)
        assert verdict.comparability is Comparability.SEPARATE
        assert not verdict.may_merge

    def test_a_different_runtime_profile_is_a_runtime_study(self) -> None:
        left = _subject("run-a", profile_hash="profile-f16")
        right = _subject("run-b", profile_hash="profile-q8")
        verdict = verdict_for_pair(left, right, metric_kind=MetricKind.MEMORY)
        assert verdict.comparability is Comparability.SEPARATE
        assert verdict.study is StudyKind.RUNTIME_STUDY
        assert not verdict.may_merge

    def test_a_different_quantization_is_a_quantization_study(self) -> None:
        left = _subject("run-a", identity=_identity("qwen3.5:9b-q8_0"), quantization="Q8_0")
        right = _subject(
            "run-b",
            identity=_identity("qwen3.5:9b-q4_0", digest="sha256:" + "2" * 64),
            quantization="Q4_0",
        )
        verdict = verdict_for_pair(left, right, metric_kind=MetricKind.QUALITY)
        # BaseAiCore alone answers "indeterminate" here; the descriptor's family is what makes
        # this a study rather than two unrelated models.
        assert verdict.comparability is Comparability.INDETERMINATE
        assert verdict.study is StudyKind.QUANTIZATION_STUDY
        assert not verdict.may_merge
        assert "quantization" in verdict.reason

    def test_two_unrelated_models_are_not_a_study(self) -> None:
        left = _subject("run-a", family="qwen3.5")
        right = _subject(
            "run-b",
            identity=_identity("llama4:70b", digest="sha256:" + "3" * 64),
            family="llama4",
        )
        verdict = verdict_for_pair(left, right, metric_kind=MetricKind.QUALITY)
        assert verdict.study is StudyKind.UNRELATED
        assert not verdict.may_merge

    def test_a_different_machine_separates_hardware_metrics_only(self) -> None:
        left = _subject("run-a", machine=_MACHINE)
        right = _subject("run-b", machine=_OTHER_MACHINE)
        for kind in (MetricKind.PERFORMANCE, MetricKind.MEMORY, MetricKind.ENERGY):
            verdict = verdict_for_pair(left, right, metric_kind=kind)
            assert verdict.comparability is Comparability.SEPARATE
            assert verdict.study is StudyKind.MACHINE_STUDY
            assert not verdict.may_merge
        quality = verdict_for_pair(left, right, metric_kind=MetricKind.QUALITY)
        assert quality.comparability is Comparability.WARN
        assert quality.may_merge

    def test_two_different_suites_are_never_one_comparison(self) -> None:
        left = _subject("run-a", key="native.memory_kv")
        right = _subject("run-b", key="native.energy")
        verdict = verdict_for_pair(left, right, metric_kind=MetricKind.QUALITY)
        assert verdict.comparability is Comparability.SEPARATE
        assert "different benchmark suites" in verdict.reason.lower()

    def test_an_identical_subject_is_directly_comparable(self) -> None:
        verdict = verdict_for_pair(
            _subject("run-a"), _subject("run-b"), metric_kind=MetricKind.PERFORMANCE
        )
        assert verdict.comparability is Comparability.COMPARABLE
        assert verdict.study is StudyKind.DIRECT
        assert verdict.may_merge
        assert verdict.diff == ()

    def test_a_name_only_identity_merges_with_a_caveat(self) -> None:
        identity = _name_only_identity()
        verdict = verdict_for_pair(
            _subject("run-a", identity=identity),
            _subject("run-b", identity=identity),
            metric_kind=MetricKind.QUALITY,
        )
        assert verdict.comparability is Comparability.WARN
        assert verdict.may_merge
        assert "name_only" in verdict.reason


class TestTheDiffExplainsTheSeparation:
    """Machine Identity §4 rule 3: never merged silently, and the field-level diff is shown."""

    def test_a_separated_pair_carries_the_field_that_moved(self) -> None:
        left = _subject("run-a", version="1.0.0")
        right = _subject("run-b", version="1.1.0")
        verdict = verdict_for_pair(left, right, metric_kind=MetricKind.QUALITY)
        paths = {entry.path for entry in verdict.diff}
        assert "benchmark.suite_version" in paths
        moved = next(entry for entry in verdict.diff if entry.path == "benchmark.suite_version")
        assert (moved.left, moved.right) == ("1.0.0", "1.1.0")

    def test_a_direct_comparison_carries_no_diff(self) -> None:
        verdict = verdict_for_pair(
            _subject("run-a"), _subject("run-b"), metric_kind=MetricKind.QUALITY
        )
        assert verdict.diff == ()

    def test_a_missing_document_yields_an_empty_diff_rather_than_an_invented_one(self) -> None:
        left = _subject("run-a", version="1.0.0", document={})
        right = _subject("run-b", version="1.1.0")
        verdict = verdict_for_pair(left, right, metric_kind=MetricKind.QUALITY)
        assert verdict.comparability is Comparability.SEPARATE
        assert verdict.diff == ()
        # The reason still names the dimension; only the field-level detail is unavailable.
        assert "1.1.0" in verdict.reason

    def test_separation_diff_is_symmetric_in_content(self) -> None:
        left = _subject("run-a", profile_hash="profile-f16")
        right = _subject("run-b", profile_hash="profile-q8")
        forward = separation_diff(left, right)
        backward = separation_diff(right, left)
        assert {entry.path for entry in forward} == {entry.path for entry in backward}


class TestGrouping:
    """A group is a set that may be merged; anything else is a separate column."""

    def test_identical_subjects_form_one_group(self) -> None:
        groups, _ = group_subjects(
            [_subject("run-a"), _subject("run-b"), _subject("run-c")],
            metric_kind=MetricKind.QUALITY,
        )
        assert len(groups) == 1
        assert groups[0].members == ("run-a", "run-b", "run-c")
        assert groups[0].study is StudyKind.DIRECT

    def test_a_separated_subject_gets_its_own_group_with_the_reason(self) -> None:
        groups, verdicts = group_subjects(
            [_subject("run-a"), _subject("run-b", version="2.0.0")],
            metric_kind=MetricKind.QUALITY,
        )
        assert [group.members for group in groups] == [("run-a",), ("run-b",)]
        assert groups[1].study is StudyKind.INCOMPARABLE
        assert "2.0.0" in groups[1].reason
        assert len(verdicts) == 1

    def test_the_same_subjects_group_differently_per_metric_kind(self) -> None:
        subjects = [_subject("run-a", machine=_MACHINE), _subject("run-b", machine=_OTHER_MACHINE)]
        quality, _ = group_subjects(subjects, metric_kind=MetricKind.QUALITY)
        memory, _ = group_subjects(subjects, metric_kind=MetricKind.MEMORY)
        assert len(quality) == 1
        assert len(memory) == 2

    def test_a_subject_joins_a_group_only_if_it_clears_every_member(self) -> None:
        # run-c is comparable with run-a but separated from run-b; joining on first match would
        # put it in a group it was never cleared against.
        subjects = [
            _subject("run-a", profile_hash="p1"),
            _subject("run-b", profile_hash="p2"),
            _subject("run-c", profile_hash="p1"),
        ]
        groups, _ = group_subjects(subjects, metric_kind=MetricKind.MEMORY)
        assert [group.members for group in groups] == [("run-a", "run-c"), ("run-b",)]
