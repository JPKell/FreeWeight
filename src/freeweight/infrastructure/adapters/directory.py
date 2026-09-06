"""Read the operator's adapter directory: reviewed manifests in, registrations out.

[ADR-0061](../../../docs/adr/0061-the-adapter-registry-is-a-directory-and-a-manifest.md): the
registry is a directory of artifacts plus one reviewed ``model.adapter_manifest`` per adapter, not
a service and not a database. Identity is the **artifact's content hash**, so a rename is
transparent and a content change is a different adapter — and a manifest whose recorded hash no
longer matches its artifact makes that adapter *unavailable*, by name, until it is rescanned
(rule 5). Fail closed: an adapter FreeWeight cannot verify is never offered to a provider, because
applying a LoRA to the wrong base produces plausible, confident, wrong output.

The layout this module reads::

    <directory>/terse.gguf                  the served artifact
    <directory>/terse.manifest.json         the reviewed manifest, a SetSpec envelope

**Nothing here writes.** The directory belongs to the operator; FreeWeight's own record of what it
has *measured* is the ``adapters`` table, which outlives the directory on purpose (spec §10).

This module is also the SetSpec seam. A manifest is validated through
``setspec.model.v1.AdapterManifestIn``, and the conversion to ModelRack's ``AdapterRegistration``
happens **in this application** — ModelRack reads no directory, no environment variable and no
configuration file (ADR-0061 rule 3, ModelRack spec §12). ``.importlinter`` keeps that direction and
:func:`registrations_from` is the seam.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

from baseaicore import ConfigurationError, DataClassification, IdentityConfidence, normalize_digest
from pydantic import ValidationError as PydanticValidationError
from setspec import SchemaVersion, load_envelope
from setspec.model.v1 import AdapterManifestIn

if TYPE_CHECKING:
    from collections.abc import Sequence

    from modelrack.adapters import AdapterRegistration

__all__ = [
    "DRAFT_SUFFIX",
    "MANIFEST_SCHEMA",
    "MANIFEST_SUFFIX",
    "MANIFEST_VERSION",
    "AdapterDirectoryMissing",
    "AdapterEntry",
    "DirectoryReading",
    "read_directory",
    "registrations_from",
    "sha256_of",
]

MANIFEST_SCHEMA: Final = "model.adapter_manifest"
MANIFEST_VERSION: Final = SchemaVersion(1, 0)
MANIFEST_SUFFIX: Final = ".manifest.json"
DRAFT_SUFFIX: Final = ".manifest.draft.json"
ARTIFACT_SUFFIX: Final = ".gguf"

_HASH_CHUNK_BYTES: Final = 1024 * 1024
"""Read size for hashing an artifact. A LoRA GGUF is tens of megabytes rather than gigabytes, but
streaming costs nothing and keeps a large one out of memory whole."""


class AdapterDirectoryMissing(ConfigurationError):
    """``[adapters] directory`` names something that is not a directory.

    A configuration error rather than an empty reading. FreeWeight is a measuring instrument run
    one command at a time: an operator who configures a path and gets silence has no way to tell a
    typo from an empty directory, and would attribute the resulting absence of adapter subjects to
    the adapters rather than to the path. LoadCoach makes the opposite call for a long-running
    service, where ``doctor`` reports it and a restart is expensive; the divergence is deliberate.

    Attributes:
        code: ``"CONFIGURATION_ERROR"``, inherited.
    """

    code: ClassVar[str] = "CONFIGURATION_ERROR"


def sha256_of(path: Path) -> str:
    """Return the normalized ``sha256:`` digest of a file's contents.

    Args:
        path: The file to hash.

    Returns:
        ``"sha256:"`` followed by 64 lowercase hex characters — the form ADR-0024 §2 fixes, and the
        only form any digest in the suite is compared in.

    Raises:
        OSError: The file could not be read. The caller turns that into an unavailable adapter
            carrying the reason, rather than into a crash.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    normalized = normalize_digest(digest.hexdigest())
    # `normalize_digest` returns None only for input it cannot read as a digest; 64 hex characters
    # from hashlib is never that.
    assert normalized is not None  # noqa: S101 — a hashlib digest always normalizes
    return normalized


@dataclass(frozen=True, slots=True)
class AdapterEntry:
    """One adapter the directory describes, and whether it may be used.

    Attributes:
        name: The manifest's name — the ``--adapter`` value, the display name, and the name half of
            the canonical subject string.
        manifest_path: The reviewed manifest this entry was read from.
        artifact_path: The served artifact, resolved against the directory.
        artifact_sha256: The digest the manifest records. **This is the identity.**
        source_sha256: The training checkpoint's digest, for lineage only.
        base_model_name: The base this adapter declares it was trained against.
        base_artifact_digest: That base's digest, where the manifest's author proved one.
        base_confidence: ``DIGEST`` when a base digest is present, ``NAME_ONLY`` otherwise. The
            existing :class:`~baseaicore.IdentityConfidence` machinery, not a parallel flag
            (ADR-0058 §5).
        declared_capabilities: The vocabulary terms the manifest claims, already validated by
            SetSpec. The claim the panel's declared part puts under test (ADR-0059).
        data_classification: Required by the payload, so always present (ADR-0065 rule 1).
        notes: The reviewer's free text.
        available: Whether this adapter may be registered at all.
        unavailable_reason: Why not, in prose a person can act on. ``None`` when available.
    """

    name: str
    manifest_path: Path
    artifact_path: Path
    artifact_sha256: str
    source_sha256: str | None
    base_model_name: str
    base_artifact_digest: str | None
    base_confidence: IdentityConfidence
    declared_capabilities: tuple[str, ...]
    data_classification: DataClassification
    notes: str | None
    available: bool
    unavailable_reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Render this entry for ``--json`` and for the adapters view."""
        return {
            "name": self.name,
            "artifact_sha256": self.artifact_sha256,
            "artifact_path": str(self.artifact_path),
            "manifest_path": str(self.manifest_path),
            "source_sha256": self.source_sha256,
            "base_model_name": self.base_model_name,
            "base_artifact_digest": self.base_artifact_digest,
            "base_confidence": self.base_confidence.value,
            "declared_capabilities": list(self.declared_capabilities),
            "data_classification": self.data_classification.value,
            "notes": self.notes,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class DirectoryReading:
    """Everything one pass over the directory found.

    Attributes:
        directory: The directory read.
        entries: One per reviewed manifest, available or not, in name order.
        invalid: ``(path, problem)`` per manifest that could not be read at all — bad JSON, the
            wrong schema, a payload the contract refuses. Reported, never guessed at.
        drafts: ``*.manifest.draft.json`` files present. **Nothing trusts a draft**: "the scan
            drafts, a human keeps" (ADR-0061 rule 4) is enforced by the suffix rather than by a
            flag inside the document, so a file nobody renamed cannot be mistaken for one somebody
            reviewed. FreeWeight writes no drafts; they are listed because an operator sharing a
            directory with LoadCoach will have them.
        unmanifested: Artifacts with neither a manifest nor a draft — present, and unusable until
            somebody reviews one.
    """

    directory: Path
    entries: tuple[AdapterEntry, ...] = ()
    invalid: tuple[tuple[Path, str], ...] = ()
    drafts: tuple[Path, ...] = ()
    unmanifested: tuple[Path, ...] = ()

    @property
    def available(self) -> tuple[AdapterEntry, ...]:
        """The entries that may be registered on a provider."""
        return tuple(entry for entry in self.entries if entry.available)

    def by_name(self, name: str) -> AdapterEntry | None:
        """Return the entry called ``name``, or ``None``."""
        return next((entry for entry in self.entries if entry.name == name), None)


def read_directory(directory: Path) -> DirectoryReading:
    """Read every reviewed manifest in ``directory`` and verify each against its artifact.

    An adapter is available only when its manifest parses against ``model.adapter_manifest`` `1.0`,
    its artifact exists, and that artifact's content hashes to the digest the manifest records.
    Each of those failing is a **named** unavailability rather than an omission: "the adapter I
    dropped in is not being used" is the confusion this whole design exists to prevent
    (ADR-0061's consequences).

    Args:
        directory: The configured adapter directory, already expanded.

    Returns:
        The :class:`DirectoryReading`. Never raises for anything the *contents* of the directory can
        do to it — a bad manifest is reported in :attr:`DirectoryReading.invalid` and a mismatched
        artifact in the entry's ``unavailable_reason``.

    Raises:
        AdapterDirectoryMissing: ``directory`` does not exist or is not a directory. Refused rather
            than read as empty, so a typo in ``[adapters] directory`` is a message naming the path
            instead of an installation that quietly measures no adapter subjects.
    """
    if not directory.is_dir():
        raise AdapterDirectoryMissing(
            f"[adapters] directory is {str(directory)!r}, which is not a directory. Create it, "
            "correct the path, or clear the key — an empty [adapters] directory means adapters "
            "are off, and that is a supported configuration.",
            details={"field": "adapters.directory", "value": str(directory)},
        )

    entries: list[AdapterEntry] = []
    invalid: list[tuple[Path, str]] = []
    for manifest_path in sorted(directory.glob(f"*{MANIFEST_SUFFIX}")):
        if manifest_path.name.endswith(DRAFT_SUFFIX):
            continue
        entry, problem = _read_manifest(manifest_path, directory=directory)
        if entry is None:
            invalid.append((manifest_path, problem or "unreadable"))
        else:
            entries.append(entry)

    claimed = {entry.artifact_path for entry in entries}
    drafts = tuple(sorted(directory.glob(f"*{DRAFT_SUFFIX}")))
    drafted_stems = {path.name.removesuffix(DRAFT_SUFFIX) for path in drafts}
    unmanifested = tuple(
        artifact
        for artifact in sorted(directory.glob(f"*{ARTIFACT_SUFFIX}"))
        if artifact.resolve() not in claimed and artifact.stem not in drafted_stems
    )
    return DirectoryReading(
        directory=directory,
        entries=tuple(sorted(entries, key=lambda entry: entry.name)),
        invalid=tuple(invalid),
        drafts=drafts,
        unmanifested=unmanifested,
    )


def _read_manifest(path: Path, *, directory: Path) -> tuple[AdapterEntry | None, str | None]:
    """Read and verify one manifest. Returns ``(entry, problem)``; exactly one is set."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"could not be read: {exc}"
    try:
        envelope = load_envelope(raw, expect=MANIFEST_SCHEMA, supported=[MANIFEST_VERSION])
    except Exception as exc:  # noqa: BLE001 — every parse failure is reported, never raised
        return None, str(exc)
    try:
        manifest = AdapterManifestIn.model_validate(envelope.payload)
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "payload"
        return None, f"{location}: {first['msg']}"

    artifact_path = (directory / manifest.artifact_file).resolve()
    common: dict[str, Any] = {
        "name": manifest.name,
        "manifest_path": path,
        "artifact_path": artifact_path,
        "artifact_sha256": manifest.artifact_sha256,
        "source_sha256": manifest.source_sha256,
        "base_model_name": manifest.base.provider_model_name,
        "base_artifact_digest": manifest.base.artifact_digest,
        "base_confidence": manifest.base.identity_confidence,
        "declared_capabilities": tuple(manifest.declared_capabilities),
        "data_classification": manifest.data_classification,
        "notes": manifest.notes,
    }

    if not artifact_path.is_file():
        return (
            AdapterEntry(
                **common,
                available=False,
                unavailable_reason=(
                    f"the manifest names {manifest.artifact_file!r}, which is not a file in "
                    f"{directory}; this adapter is unavailable until the artifact returns or the "
                    "manifest is rescanned"
                ),
            ),
            None,
        )
    try:
        actual = sha256_of(artifact_path)
    except OSError as exc:
        return (
            AdapterEntry(
                **common,
                available=False,
                unavailable_reason=f"the artifact could not be hashed: {exc}",
            ),
            None,
        )
    if actual != manifest.artifact_sha256:
        return (
            AdapterEntry(
                **common,
                available=False,
                unavailable_reason=(
                    f"{manifest.artifact_file!r} hashes to {actual}, and the manifest records "
                    f"{manifest.artifact_sha256}. The content changed, so these are different "
                    "weights: rescan them rather than editing the digest, or every measurement "
                    "taken under the old hash would be re-attributed to the new ones"
                ),
            ),
            None,
        )
    return AdapterEntry(**common, available=True, unavailable_reason=None), None


def registrations_from(entries: Sequence[AdapterEntry]) -> tuple[AdapterRegistration, ...]:
    """Convert available entries into ModelRack's registration type.

    The conversion lives in the application because **ModelRack never reads the directory**
    (ADR-0061 rule 3): it receives registrations from the application that constructed it,
    validates them, and mounts them.

    Args:
        entries: Entries from :func:`read_directory`. Unavailable ones are **skipped** — an adapter
            whose artifact does not match its manifest must never reach a provider.

    Returns:
        One registration per available entry, in the order given. This is the **complete** set the
        provider will hold: ``register_adapters`` replaces rather than merges, so a name absent
        here is retired at the provider's next idle.

    Raises:
        ValidationError: ModelRack refused a registration the manifest contract accepted. That is a
            real disagreement between two validators over the same fields, and it must surface
            rather than be swallowed into "no adapters found".
    """
    from modelrack.adapters import AdapterRegistration

    return tuple(
        AdapterRegistration(
            name=entry.name,
            artifact_path=entry.artifact_path,
            artifact_sha256=entry.artifact_sha256,
            base_model_name=entry.base_model_name,
            data_classification=entry.data_classification,
            source_sha256=entry.source_sha256,
            base_artifact_digest=entry.base_artifact_digest,
        )
        for entry in entries
        if entry.available
    )
