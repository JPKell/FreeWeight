"""Subject enumeration: base × compatible adapter, decided by digest and never by name.

Phase 15 / ADR-0058. Two properties carry the weight: an adapter for a different base is not
enumerated at all, and a manifest that names its base without proving one produces a subject that
is flagged everywhere rather than quietly accepted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from baseaicore import DataClassification, IdentityConfidence, ModelIdentity, ProviderKind

from freeweight.domain.subjects import (
    IncompatibleAdapter,
    enumerate_subjects,
    subject_for,
)
from freeweight.infrastructure.adapters import AdapterEntry

_BASE_DIGEST = "sha256:" + "a1" * 32
_OTHER_DIGEST = "sha256:" + "b2" * 32
_TERSE_DIGEST = "sha256:" + "c3" * 32


def _base(digest: str | None = _BASE_DIGEST) -> ModelIdentity:
    return ModelIdentity(
        provider_kind=ProviderKind.LLAMACPP,
        provider_model_name="qwen2.5-1.5b-instruct.q8_0",
        artifact_digest=digest,
    )


def _entry(
    name: str = "terse",
    *,
    artifact_digest: str = _TERSE_DIGEST,
    base_name: str = "qwen2.5-1.5b-instruct.q8_0",
    base_digest: str | None = _BASE_DIGEST,
    available: bool = True,
) -> AdapterEntry:
    return AdapterEntry(
        name=name,
        manifest_path=Path(f"/adapters/{name}.manifest.json"),
        artifact_path=Path(f"/adapters/{name}.gguf"),
        artifact_sha256=artifact_digest,
        source_sha256=None,
        base_model_name=base_name,
        base_artifact_digest=base_digest,
        base_confidence=(
            IdentityConfidence.DIGEST if base_digest else IdentityConfidence.NAME_ONLY
        ),
        declared_capabilities=("instruction_following",),
        data_classification=DataClassification.INTERNAL,
        notes=None,
        available=available,
        unavailable_reason=None if available else "the artifact changed",
    )


class TestEnumerateSubjects:
    """What subjects one base produces."""

    def test_the_bare_base_is_always_first_and_always_present(self) -> None:
        """Adopting adapters never takes away measuring the bare weights."""
        subjects = enumerate_subjects(_base(), [_entry()])

        assert subjects[0].adapter is None
        assert subjects[0].canonical_id == _base().canonical_id

    def test_a_compatible_adapter_becomes_a_subject(self) -> None:
        subjects = enumerate_subjects(_base(), [_entry()])

        assert len(subjects) == 2
        assert subjects[1].adapter_name == "terse"
        assert subjects[1].confidence is IdentityConfidence.DIGEST
        assert not subjects[1].is_name_only

    def test_an_adapter_for_a_different_base_is_not_enumerated(self) -> None:
        """The one thing this module exists to prevent: a LoRA on weights it was not trained on."""
        subjects = enumerate_subjects(_base(), [_entry(base_digest=_OTHER_DIGEST)])

        assert [subject.adapter_name for subject in subjects] == [None]

    def test_an_adapter_naming_a_different_base_is_not_enumerated(self) -> None:
        """A name mismatch is a refusal too, not merely reduced confidence."""
        subjects = enumerate_subjects(_base(), [_entry(base_name="llama-3.2-3b", base_digest=None)])

        assert [subject.adapter_name for subject in subjects] == [None]

    def test_an_unavailable_adapter_is_not_enumerated(self) -> None:
        """An adapter whose artifact no longer matches its manifest is not a candidate."""
        subjects = enumerate_subjects(_base(), [_entry(available=False)])

        assert [subject.adapter_name for subject in subjects] == [None]

    def test_a_name_only_base_enumerates_and_is_flagged(self) -> None:
        """Reduced confidence, not exclusion — and the caveat rides the subject."""
        subjects = enumerate_subjects(_base(), [_entry(base_digest=None)])

        assert len(subjects) == 2
        assert subjects[1].is_name_only
        assert subjects[1].confidence is IdentityConfidence.NAME_ONLY

    def test_no_adapters_configured_enumerates_exactly_the_base(self) -> None:
        """An installation with adapters off behaves as it did before they existed."""
        subjects = enumerate_subjects(_base(), [])

        assert len(subjects) == 1
        assert subjects[0].canonical_id == _base().canonical_id

    def test_three_adapters_on_one_base_are_three_subjects(self) -> None:
        entries = [
            _entry("pirate", artifact_digest="sha256:" + "11" * 32),
            _entry("terse", artifact_digest="sha256:" + "22" * 32),
            _entry("verbose", artifact_digest="sha256:" + "33" * 32),
        ]

        subjects = enumerate_subjects(_base(), entries)

        assert [subject.adapter_name for subject in subjects] == [
            None,
            "pirate",
            "terse",
            "verbose",
        ]
        assert len({subject.canonical_id for subject in subjects}) == 4


class TestCanonicalId:
    """The subject string comes from baseaicore, and I18 depends on that."""

    def test_a_bare_subject_is_byte_for_byte_the_models_canonical_id(self) -> None:
        """ADR-0058's additive claim: adapters change nothing for a subject that has none."""
        base = _base()

        assert enumerate_subjects(base, [])[0].canonical_id == base.canonical_id

    def test_an_adapter_subject_appends_the_libraries_suffix(self) -> None:
        """Never formatted here: two applications agree only while both ask the same library."""
        from baseaicore import AdapterIdentity

        subject = enumerate_subjects(_base(), [_entry()])[1]
        expected = AdapterIdentity(name="terse", artifact_digest=_TERSE_DIGEST).canonical_suffix

        assert subject.canonical_id == f"{_base().canonical_id}{expected}"
        assert subject.canonical_id.startswith(_base().canonical_id)


class TestSubjectFor:
    """`--adapter <name>` resolution, and every way it refuses."""

    def test_no_name_is_the_bare_base(self) -> None:
        assert subject_for(_base(), [_entry()], None).adapter is None

    def test_a_known_compatible_name_resolves(self) -> None:
        subject = subject_for(_base(), [_entry()], "terse")

        assert subject.adapter_name == "terse"
        assert subject.entry is not None

    def test_an_unknown_name_is_refused_and_lists_what_is_registered(self) -> None:
        """ "Not found" without the alternatives is a dead end (spec §13)."""
        with pytest.raises(IncompatibleAdapter) as caught:
            subject_for(_base(), [_entry("terse"), _entry("pirate")], "trse")

        assert "'trse'" in caught.value.message
        assert caught.value.details["registered"] == ["pirate", "terse"]

    def test_an_unavailable_name_is_refused_with_its_reason(self) -> None:
        with pytest.raises(IncompatibleAdapter) as caught:
            subject_for(_base(), [_entry(available=False)], "terse")

        assert "unavailable" in caught.value.message
        assert caught.value.details["reason"] == "the artifact changed"

    def test_an_incompatible_name_is_refused_by_name_with_the_compatible_set(self) -> None:
        """Never a silent fall back to the base — that would attribute the base to the adapter."""
        entries = [_entry("wrong", base_digest=_OTHER_DIGEST), _entry("right")]

        with pytest.raises(IncompatibleAdapter) as caught:
            subject_for(_base(), entries, "wrong")

        assert "'wrong'" in caught.value.message
        assert caught.value.details["compatible"] == ["right"]

    def test_the_refusal_is_a_validation_error_so_the_cli_exits_two(self) -> None:
        """The user named something wrong; that is a usage error, not an outage."""
        with pytest.raises(IncompatibleAdapter) as caught:
            subject_for(_base(), [], "terse")

        assert caught.value.code == "VALIDATION_ERROR"
