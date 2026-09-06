"""The adapter directory reader: what it accepts, what it names, and what it refuses.

Phase 15 / ADR-0061. Three properties carry the weight here and each has its own test:
identity is the artifact's content hash, an unverifiable adapter is *named* rather than omitted,
and a configured directory that does not exist is a configuration error rather than silence.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import IdentityConfidence

from freeweight.infrastructure.adapters import (
    AdapterDirectoryMissing,
    read_directory,
    registrations_from,
)

if TYPE_CHECKING:
    from pathlib import Path

_BASE_DIGEST = "sha256:" + "a1" * 32


def _write_adapter(
    directory: Path,
    name: str,
    *,
    content: bytes = b"lora-weights",
    base_name: str = "qwen2.5-1.5b-instruct.q8_0",
    base_digest: str | None = _BASE_DIGEST,
    declared: tuple[str, ...] = ("instruction_following",),
    recorded_digest: str | None = None,
    write_artifact: bool = True,
) -> str:
    """Write one adapter artifact and its reviewed manifest; return the artifact's real digest."""
    artifact = directory / f"{name}.gguf"
    if write_artifact:
        artifact.write_bytes(content)
    real = "sha256:" + hashlib.sha256(content).hexdigest()
    payload: dict[str, Any] = {
        "name": name,
        "artifact_file": f"{name}.gguf",
        "artifact_sha256": recorded_digest or real,
        "format": "gguf",
        "base": {
            "provider_model_name": base_name,
            "artifact_digest": base_digest,
            "identity_confidence": (
                IdentityConfidence.DIGEST.value
                if base_digest
                else IdentityConfidence.NAME_ONLY.value
            ),
        },
        "declared_capabilities": list(declared),
        "data_classification": "internal",
        "created_at": "2026-09-05T10:00:00.000Z",
    }
    if base_digest is None:
        payload["base"].pop("artifact_digest")
    envelope = {
        "schema": "model.adapter_manifest",
        "schema_version": "1.0",
        "generated_at": "2026-09-05T10:00:00.000Z",
        "generator": {"name": "test", "version": "0"},
        "payload": payload,
    }
    (directory / f"{name}.manifest.json").write_text(json.dumps(envelope), encoding="utf-8")
    return real


class TestReadDirectory:
    """Reading a directory of reviewed manifests."""

    def test_a_verified_adapter_is_available(self, tmp_path: Path) -> None:
        """A manifest that parses, whose artifact exists and hashes as recorded, is usable."""
        digest = _write_adapter(tmp_path, "terse")

        reading = read_directory(tmp_path)

        assert [entry.name for entry in reading.entries] == ["terse"]
        entry = reading.entries[0]
        assert entry.available
        assert entry.unavailable_reason is None
        assert entry.artifact_sha256 == digest
        assert entry.base_confidence is IdentityConfidence.DIGEST
        assert entry.declared_capabilities == ("instruction_following",)

    def test_entries_are_in_name_order(self, tmp_path: Path) -> None:
        """Order is the adapter's name, so two readings of one directory agree."""
        for name in ("verbose", "pirate", "terse"):
            _write_adapter(tmp_path, name, content=name.encode())

        assert [entry.name for entry in read_directory(tmp_path).entries] == [
            "pirate",
            "terse",
            "verbose",
        ]

    def test_a_changed_artifact_is_unavailable_and_says_so(self, tmp_path: Path) -> None:
        """Content is identity: different bytes are a different adapter, never a silent re-use."""
        _write_adapter(tmp_path, "terse", content=b"original")
        (tmp_path / "terse.gguf").write_bytes(b"retrained, same filename")

        entry = read_directory(tmp_path).entries[0]

        assert not entry.available
        assert entry.unavailable_reason is not None
        assert "hashes to" in entry.unavailable_reason
        assert "re-attributed" in entry.unavailable_reason
        assert read_directory(tmp_path).available == ()

    def test_a_missing_artifact_is_named_not_omitted(self, tmp_path: Path) -> None:
        """An adapter present but unusable is named, never quietly omitted from the reading."""
        _write_adapter(tmp_path, "terse", write_artifact=False)

        entry = read_directory(tmp_path).entries[0]

        assert not entry.available
        assert entry.unavailable_reason is not None
        assert "terse.gguf" in entry.unavailable_reason

    def test_a_name_only_base_reads_as_name_only(self, tmp_path: Path) -> None:
        """A manifest whose author could not prove a base digest names it rather than proving it."""
        _write_adapter(tmp_path, "terse", base_digest=None)

        entry = read_directory(tmp_path).entries[0]

        assert entry.available
        assert entry.base_artifact_digest is None
        assert entry.base_confidence is IdentityConfidence.NAME_ONLY

    def test_an_unreadable_manifest_is_reported_not_raised(self, tmp_path: Path) -> None:
        """One bad file never stops the directory being read."""
        _write_adapter(tmp_path, "good")
        (tmp_path / "broken.manifest.json").write_text("{not json", encoding="utf-8")

        reading = read_directory(tmp_path)

        assert [entry.name for entry in reading.entries] == ["good"]
        assert len(reading.invalid) == 1
        assert reading.invalid[0][0].name == "broken.manifest.json"

    def test_a_draft_is_never_read_as_a_registration(self, tmp_path: Path) -> None:
        """A draft is never a registration (ADR-0061 rule 4), enforced by the suffix."""
        _write_adapter(tmp_path, "kept")
        (tmp_path / "unreviewed.gguf").write_bytes(b"x")
        (tmp_path / "unreviewed.manifest.draft.json").write_text("{}", encoding="utf-8")

        reading = read_directory(tmp_path)

        assert [entry.name for entry in reading.entries] == ["kept"]
        assert reading.invalid == ()
        assert [path.name for path in reading.drafts] == ["unreviewed.manifest.draft.json"]

    def test_an_artifact_with_no_manifest_is_listed_as_unmanifested(self, tmp_path: Path) -> None:
        """Present and unusable until somebody reviews one — visible, not ignored."""
        _write_adapter(tmp_path, "kept")
        (tmp_path / "orphan.gguf").write_bytes(b"x")

        assert [path.name for path in read_directory(tmp_path).unmanifested] == ["orphan.gguf"]

    def test_an_empty_directory_reads_as_empty(self, tmp_path: Path) -> None:
        """A directory with nothing in it is a legal configuration, not an error."""
        reading = read_directory(tmp_path)

        assert reading.entries == ()
        assert reading.available == ()

    def test_a_directory_that_does_not_exist_is_refused_by_name(self, tmp_path: Path) -> None:
        """A typo in `[adapters] directory` is a message, not an installation that measures nothing.

        The deliberate divergence from LoadCoach, which reads a missing directory as empty because
        it is a long-running service with a `doctor` command. FreeWeight is run one command at a
        time and has no second chance to report it.
        """
        missing = tmp_path / "not-here"

        with pytest.raises(AdapterDirectoryMissing) as caught:
            read_directory(missing)

        assert str(missing) in caught.value.message
        assert caught.value.details["field"] == "adapters.directory"

    def test_a_file_where_a_directory_was_configured_is_refused(self, tmp_path: Path) -> None:
        """Not a directory is refused on the same terms as not present."""
        target = tmp_path / "adapters.txt"
        target.write_text("oops", encoding="utf-8")

        with pytest.raises(AdapterDirectoryMissing):
            read_directory(target)


class TestRegistrationsFrom:
    """The seam between the directory and ModelRack (ADR-0061 rule 3)."""

    def test_only_available_entries_are_offered(self, tmp_path: Path) -> None:
        """A provider cannot tell that it should refuse an adapter, so it is never handed one."""
        _write_adapter(tmp_path, "good")
        _write_adapter(tmp_path, "changed", content=b"a")
        (tmp_path / "changed.gguf").write_bytes(b"b")

        registrations = registrations_from(read_directory(tmp_path).entries)

        assert [registration.name for registration in registrations] == ["good"]

    def test_the_registration_carries_both_halves_of_the_base_claim(self, tmp_path: Path) -> None:
        """Name and digest are two fields: an absent digest can only ever reach NAME_ONLY."""
        _write_adapter(tmp_path, "proved")
        _write_adapter(tmp_path, "named", content=b"other", base_digest=None)

        by_name = {
            registration.name: registration
            for registration in registrations_from(read_directory(tmp_path).entries)
        }

        assert by_name["proved"].base_artifact_digest == _BASE_DIGEST
        assert by_name["named"].base_artifact_digest is None
        assert by_name["named"].base_model_name == "qwen2.5-1.5b-instruct.q8_0"
