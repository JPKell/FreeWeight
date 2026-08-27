"""The reproducibility fingerprint: stable for identical inputs, different for each input class.

Development plan, Phase 6: "Fingerprint: stable for identical inputs; changes for each input class;
the document is stored and diffable." The first two are asserted here over the document assembly;
"stored" is asserted in ``tests/integration/test_performance_benchmark.py``, where there is a run
to store it on.

The per-input-class assertion is table-driven and deliberately exhaustive over the document's
sections. A fingerprint that fails to change when the model digest moves is the failure everyone
worries about; a fingerprint that changes when nothing relevant did is the one that trains a user
to ignore the signal, and both are tested.
"""

from __future__ import annotations

from typing import Any

import pytest
from baseaicore import UNSUPPORTED

from freeweight.domain.provenance import (
    Degradation,
    ServedContext,
    ServedContextSource,
    build_fingerprint_document,
    case_selection_hash,
    check_repeatable,
    compute_fingerprint,
    diff_documents,
    divergence_degradation,
    resolve_served_context,
)


def _document(**overrides: Any) -> dict[str, Any]:
    """A complete document, with any section replaced wholesale."""
    sections: dict[str, Any] = {
        "model": {
            "provider_kind": "ollama",
            "provider_model_name": "qwen3.5:9b-q8_0",
            "artifact_digest": "sha256:" + "a" * 64,
            "identity_confidence": "digest",
            "descriptor_hash": "sha256:" + "b" * 64,
        },
        "runtime_profile_hash": "0123456789abcdef",
        "provider": {"kind": "ollama", "version": "0.32.13"},
        "machine_fingerprint": "sha256:" + "c" * 64,
        "environment": {
            "gpu_driver_version": "580.65",
            "cuda_version": "13.0",
            "os_version": "Ubuntu 26.04",
        },
        "benchmark": {
            "suite_key": "native.performance",
            "suite_version": "1.0.0",
            "manifest_hash": "sha256:" + "d" * 64,
            "dataset_hashes": {},
            "prompt_subset_hash": "sha256:" + "e" * 64,
        },
        "execution": {
            "effective_parameters": {"measured_repetitions": 3},
            "repetitions": 3,
            "seed": 0,
            "case_selection_hash": "sha256:" + "f" * 64,
            "served_context": 32768,
            "served_context_source": "assumed",
            "gpu_index": 0,
            "multi_gpu_visible": False,
        },
        "application": {"name": "freeweight", "version": "0.6.0", "git_commit": None},
    }
    sections.update(overrides)
    return build_fingerprint_document(
        model=sections["model"],
        runtime_profile_hash=sections["runtime_profile_hash"],
        provider=sections["provider"],
        machine_fingerprint=sections["machine_fingerprint"],
        environment=sections["environment"],
        benchmark=sections["benchmark"],
        execution=sections["execution"],
        application=sections["application"],
    )


def _with(document: dict[str, Any], section: str, key: str, value: Any) -> dict[str, Any]:
    """Return a copy of ``document`` with one leaf changed."""
    changed = {
        name: dict(body) if isinstance(body, dict) else body for name, body in document.items()
    }
    changed[section][key] = value
    return changed


class TestStability:
    """Identical inputs hash identically, in any order and in any process."""

    def test_the_same_inputs_hash_the_same(self) -> None:
        assert compute_fingerprint(_document()) == compute_fingerprint(_document())

    def test_key_order_does_not_change_the_hash(self) -> None:
        document = _document()
        reordered = dict(reversed(list(document.items())))
        assert compute_fingerprint(reordered) == compute_fingerprint(document)

    def test_the_hash_is_a_prefixed_sha256(self) -> None:
        fingerprint = compute_fingerprint(_document())
        assert fingerprint.startswith("sha256:")
        assert len(fingerprint) == len("sha256:") + 64

    def test_unsupported_serializes_as_a_string_rather_than_null(self) -> None:
        # Machine Identity §4 rule 1. A served context nobody could report must not hash the same
        # as one reported as zero.
        unknown = _with(_document(), "execution", "served_context", UNSUPPORTED)
        zero = _with(_document(), "execution", "served_context", 0)
        assert compute_fingerprint(unknown) != compute_fingerprint(zero)


class TestEveryInputClassChangesIt:
    """One assertion per section of the document Machine Identity §4 specifies."""

    @pytest.mark.parametrize(
        ("section", "key", "value"),
        [
            ("model", "artifact_digest", "sha256:" + "9" * 64),
            ("model", "provider_model_name", "qwen3.5:9b-q4_K_M"),
            ("model", "identity_confidence", "name_only"),
            ("model", "descriptor_hash", "sha256:" + "8" * 64),
            ("provider", "version", "0.33.0"),
            ("environment", "gpu_driver_version", "581.00"),
            ("environment", "cuda_version", "13.1"),
            ("environment", "os_version", "Ubuntu 26.10"),
            ("benchmark", "suite_version", "1.1.0"),
            ("benchmark", "manifest_hash", "sha256:" + "7" * 64),
            ("benchmark", "prompt_subset_hash", "sha256:" + "6" * 64),
            ("benchmark", "dataset_hashes", {"fixtures": "sha256:1234"}),
            ("execution", "seed", 1),
            ("execution", "repetitions", 5),
            ("execution", "case_selection_hash", "sha256:" + "5" * 64),
            ("execution", "served_context", 8192),
            ("execution", "served_context_source", "configured"),
            ("execution", "gpu_index", 1),
            ("execution", "multi_gpu_visible", True),
            ("application", "version", "0.7.0"),
            ("application", "git_commit", "abc1234"),
        ],
    )
    def test_changing_one_input_changes_the_fingerprint(
        self, section: str, key: str, value: Any
    ) -> None:
        baseline = compute_fingerprint(_document())
        assert compute_fingerprint(_with(_document(), section, key, value)) != baseline

    def test_a_changed_runtime_profile_changes_it(self) -> None:
        baseline = compute_fingerprint(_document())
        assert compute_fingerprint(_document(runtime_profile_hash="ffffffffffffffff")) != baseline

    def test_a_changed_machine_changes_it(self) -> None:
        baseline = compute_fingerprint(_document())
        assert compute_fingerprint(_document(machine_fingerprint="sha256:" + "0" * 64)) != baseline

    def test_the_pack_hash_is_not_an_input_because_it_is_not_in_the_document(self) -> None:
        # ADR-0028 §1: the fingerprint takes the per-benchmark subset hash. The pack's identity is
        # recorded on the run as provenance and must not appear here at all.
        assert "prompt_pack_hash" not in _document()["benchmark"]


class TestDocumentsAreDiffable:
    """Two runs that differ are compared field by field, not by two hex strings."""

    def test_identical_documents_diff_to_nothing(self) -> None:
        assert diff_documents(_document(), _document()) == ()

    def test_one_changed_leaf_produces_one_entry_with_its_path(self) -> None:
        changed = _with(_document(), "provider", "version", "0.33.0")
        (entry,) = diff_documents(_document(), changed)
        assert entry.path == "provider.version"
        assert entry.left == "0.32.13"
        assert entry.right == "0.33.0"

    def test_a_field_present_in_only_one_document_is_reported(self) -> None:
        extra = _document()
        extra["application"] = {**extra["application"], "build": "nightly"}
        paths = [entry.path for entry in diff_documents(_document(), extra)]
        assert "application.build" in paths

    def test_entries_are_sorted_by_path(self) -> None:
        changed = _with(_with(_document(), "provider", "version", "x"), "execution", "seed", 9)
        paths = [entry.path for entry in diff_documents(_document(), changed)]
        assert paths == sorted(paths)


class TestCaseSelectionHash:
    """The cases a run executed, hashed so a shuffle does not separate two identical selections."""

    def test_order_does_not_matter(self) -> None:
        assert case_selection_hash(["b", "a"]) == case_selection_hash(["a", "b"])

    def test_a_different_selection_hashes_differently(self) -> None:
        assert case_selection_hash(["a", "b"]) != case_selection_hash(["a", "b", "c"])

    def test_an_empty_selection_is_a_real_stable_value(self) -> None:
        assert case_selection_hash([]).startswith("sha256:")


class TestServedContextResolution:
    """The three sources, in strict preference order, and the case where there is none."""

    def test_configured_needs_both_a_request_and_the_capability(self) -> None:
        served = resolve_served_context(requested_context=8192, context_configurable=True)
        assert served == ServedContext(8192, ServedContextSource.CONFIGURED)

    def test_a_request_a_provider_cannot_honour_is_not_configured(self) -> None:
        # A provider that accepts the setting and ignores it would produce a run whose recorded
        # context never happened, so the capability gates the branch.
        served = resolve_served_context(
            requested_context=8192, context_configurable=False, advertised_max_context=32768
        )
        assert served.source is ServedContextSource.ASSUMED
        assert served.tokens == 32768

    def test_a_reported_context_beats_an_advertised_maximum(self) -> None:
        served = resolve_served_context(
            requested_context=None,
            context_configurable=True,
            reported_context=4096,
            advertised_max_context=32768,
        )
        assert served == ServedContext(4096, ServedContextSource.REPORTED)

    def test_nothing_reported_is_unsupported_and_still_says_assumed(self) -> None:
        served = resolve_served_context(requested_context=None, context_configurable=False)
        assert served.numeric_tokens is None
        assert served.source is ServedContextSource.ASSUMED


class TestRepeatBlockers:
    """Machine Identity §7's four refusals, each naming what moved."""

    def test_an_unchanged_environment_blocks_nothing(self) -> None:
        assert check_repeatable(_document(), _document()) == ()

    def test_a_changed_model_digest_is_refused_and_explained(self) -> None:
        moved = _with(_document(), "model", "artifact_digest", "sha256:" + "9" * 64)
        (blocker,) = check_repeatable(_document(), moved)
        assert blocker.reason == "model_digest_changed"
        assert blocker.field_path == "model.artifact_digest"
        assert "different model" in blocker.explanation
        assert blocker.recorded != blocker.observed

    def test_a_missing_digest_is_the_same_refusal(self) -> None:
        absent = _with(_document(), "model", "artifact_digest", None)
        (blocker,) = check_repeatable(_document(), absent)
        assert blocker.reason == "model_digest_changed"

    @pytest.mark.parametrize(
        ("section", "key", "value", "reason"),
        [
            ("provider", "version", "0.33.0", "provider_version_changed"),
            ("benchmark", "dataset_hashes", {"fixtures": "sha256:9"}, "dataset_hash_changed"),
        ],
    )
    def test_the_other_field_level_refusals(
        self, section: str, key: str, value: Any, reason: str
    ) -> None:
        (blocker,) = check_repeatable(_document(), _with(_document(), section, key, value))
        assert blocker.reason == reason

    def test_a_different_machine_is_refused(self) -> None:
        (blocker,) = check_repeatable(
            _document(), _document(machine_fingerprint="sha256:" + "0" * 64)
        )
        assert blocker.reason == "machine_changed"

    def test_several_changes_are_all_reported(self) -> None:
        moved = _with(
            _with(_document(), "model", "artifact_digest", "sha256:" + "9" * 64),
            "provider",
            "version",
            "0.33.0",
        )
        assert len(check_repeatable(_document(), moved)) == 2

    def test_a_forced_repeat_records_every_divergence(self) -> None:
        blockers = check_repeatable(
            _document(), _with(_document(), "provider", "version", "0.33.0")
        )
        degradation = divergence_degradation(blockers)
        assert degradation.kind == "repeat_forced"
        assert degradation.detail["divergences"][0]["field"] == "provider.version"
        assert degradation.as_json()["kind"] == "repeat_forced"


class TestDegradation:
    """A degradation is a recorded condition, and it renders as the JSON storage keeps."""

    def test_it_renders_its_detail(self) -> None:
        degradation = Degradation(kind="measured_while_busy", detail={"cpu_percent": 91.0})
        assert degradation.as_json() == {
            "kind": "measured_while_busy",
            "detail": {"cpu_percent": 91.0},
        }
