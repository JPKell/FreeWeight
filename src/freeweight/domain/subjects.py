"""Enumerating measurement subjects: a base, and the adapters that may be applied to it.

Phase 15. [ADR-0058](../../../docs/adr/0058-the-execution-subject-gains-an-adapter-axis.md) makes
the measurement subject ``(model, adapter?, runtime profile, machine)``; this module answers the
first half of that — which ``(base, adapter)`` pairs exist at all on this installation.

**Compatibility is decided by digest, never by name.** ``baseaicore``'s
:func:`~baseaicore.verify_adapter_base_compatibility` is the whole decision, and it fails closed:
applying a LoRA to the wrong base produces plausible, confident, wrong output, which is the worst
failure available in a measuring instrument. Where a manifest names its base without proving it,
the pair is enumerated at ``NAME_ONLY`` confidence — the existing machinery, not a parallel flag —
and that caveat is carried on the subject so every surface can show it.

Pure: no database, no provider, no filesystem. It takes identities and entries and returns
subjects, which is what makes the rules testable without a GPU.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from baseaicore import (
    AdapterIdentity,
    IdentityConfidence,
    ValidationError,
    verify_adapter_base_compatibility,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from baseaicore import ModelIdentity

    from freeweight.infrastructure.adapters import AdapterEntry

__all__ = [
    "AdapterSubject",
    "IncompatibleAdapter",
    "compatible_entries",
    "enumerate_subjects",
    "subject_for",
]


class IncompatibleAdapter(ValidationError):
    """An adapter was named for a base it cannot be applied to.

    Raised rather than silently falling back to the bare base: a caller that asked for an adapter
    and got the base would attribute the base's behaviour to the adapter, which is the
    mis-attribution the whole adapter axis exists to prevent (ADR-0058 §4).
    """


@dataclass(frozen=True, slots=True)
class AdapterSubject:
    """One ``(base, adapter?)`` pair this installation can measure.

    Attributes:
        base: The model identity the weights are served under.
        adapter: The LoRA applied, or ``None`` for the bare base.
        confidence: How well the pairing is known. ``DIGEST`` when the adapter's manifest proved
            the base's digest and it matched; ``NAME_ONLY`` when the manifest named the base
            without proving it. For a bare base this is the base's own identity confidence.
        entry: The directory entry this subject came from, or ``None`` for a bare base.
    """

    base: ModelIdentity
    adapter: AdapterIdentity | None = None
    confidence: IdentityConfidence = IdentityConfidence.DIGEST
    entry: AdapterEntry | None = None

    @property
    def canonical_id(self) -> str:
        """The canonical subject string, from ``baseaicore`` rather than formatted here.

        With no adapter this is **byte-for-byte** the base's canonical ID, which is the additive
        claim ADR-0058 rests on. The format is never re-implemented in this application: I18's
        claim is that two applications agree on a subject with no shared code, and that is only
        true while both ask the same library.
        """
        if self.adapter is None:
            return self.base.canonical_id
        return f"{self.base.canonical_id}{self.adapter.canonical_suffix}"

    @property
    def is_name_only(self) -> bool:
        """Whether this pairing rests on a name rather than a digest.

        A permanent caveat that must be flagged **everywhere the subject surfaces** — the listing,
        the run's provenance, the comparison view and the exported evidence's confidence — not only
        where it was first computed.
        """
        return self.confidence is IdentityConfidence.NAME_ONLY

    @property
    def adapter_name(self) -> str | None:
        """The adapter's name, or ``None`` for a bare base."""
        return None if self.adapter is None else self.adapter.name


def _identity_of(entry: AdapterEntry) -> AdapterIdentity:
    """Lift one directory entry into the domain's adapter identity."""
    return AdapterIdentity(
        name=entry.name,
        artifact_digest=entry.artifact_sha256,
        source_digest=entry.source_sha256,
    )


def compatible_entries(
    base: ModelIdentity, entries: Iterable[AdapterEntry]
) -> tuple[tuple[AdapterEntry, IdentityConfidence], ...]:
    """Return the entries that may be applied to ``base``, each with its pairing confidence.

    Args:
        base: The model identity actually served — the digest that was hashed, not the one a
            manifest hoped for.
        entries: Directory entries. **Unavailable ones are skipped**: an adapter whose artifact no
            longer matches its manifest is not a candidate for anything.

    Returns:
        ``(entry, confidence)`` for every compatible entry, in the order given. An incompatible
        entry is **absent**, not present-and-flagged: enumerating it would put a subject in front
        of a person that cannot be measured.
    """
    compatible: list[tuple[AdapterEntry, IdentityConfidence]] = []
    for entry in entries:
        if not entry.available:
            continue
        try:
            confidence = verify_adapter_base_compatibility(
                base,
                declared_base_name=entry.base_model_name,
                declared_base_digest=entry.base_artifact_digest,
            )
        except ValidationError:
            continue
        compatible.append((entry, confidence))
    return tuple(compatible)


def enumerate_subjects(
    base: ModelIdentity, entries: Iterable[AdapterEntry]
) -> tuple[AdapterSubject, ...]:
    """Return every subject ``base`` can be measured as: itself, plus each compatible adapter.

    The bare base is **always** first and always present, including when adapters are configured:
    measuring bare weights is not something adopting adapters takes away, and the base's own
    evidence is what the regression panel's third row is chosen from
    ([benchmark catalogue §8.2](../../../docs/apps/freeweight/benchmark-catalog.md)).

    Args:
        base: The model identity actually served.
        entries: What the operator's directory holds. Empty when adapters are off, in which case
            the answer is exactly the bare base — which is what it was before adapters existed.

    Returns:
        The bare base, then one subject per compatible adapter in the order given.
    """
    subjects = [AdapterSubject(base=base, confidence=base.identity_confidence)]
    subjects.extend(
        AdapterSubject(
            base=base,
            adapter=_identity_of(entry),
            confidence=confidence,
            entry=entry,
        )
        for entry, confidence in compatible_entries(base, entries)
    )
    return tuple(subjects)


def subject_for(
    base: ModelIdentity, entries: Sequence[AdapterEntry], adapter_name: str | None
) -> AdapterSubject:
    """Resolve ``--adapter <name>`` against what this installation can serve.

    Args:
        base: The model identity actually served.
        entries: What the operator's directory holds.
        adapter_name: The adapter asked for, or ``None`` for the bare base.

    Returns:
        The subject. ``None`` returns the bare base, which is every pre-1.1 run's subject.

    Raises:
        IncompatibleAdapter: ``adapter_name`` names no adapter in the directory, names one that is
            unavailable, or names one that cannot be applied to ``base``. Each refusal names the
            adapter **and lists the registered set**, because "not found" without the alternatives
            is a dead end (spec §13). Never a silent fall back to the bare base: a caller that
            asked for an adapter and got the base would attribute the base's behaviour to it.
    """
    if adapter_name is None:
        return AdapterSubject(base=base, confidence=base.identity_confidence)

    registered = sorted(entry.name for entry in entries)
    named = next((entry for entry in entries if entry.name == adapter_name), None)
    if named is None:
        raise IncompatibleAdapter(
            f"No adapter named {adapter_name!r} is registered. "
            f"Registered adapters: {registered or ['(none)']}. "
            "Adapters are read from [adapters] directory; an artifact with no reviewed manifest "
            "is not registered.",
            details={"adapter": adapter_name, "registered": registered},
        )
    if not named.available:
        raise IncompatibleAdapter(
            f"Adapter {adapter_name!r} is registered but unavailable: {named.unavailable_reason}",
            details={
                "adapter": adapter_name,
                "reason": named.unavailable_reason,
                "registered": registered,
            },
        )
    try:
        confidence = verify_adapter_base_compatibility(
            base,
            declared_base_name=named.base_model_name,
            declared_base_digest=named.base_artifact_digest,
        )
    except ValidationError as exc:
        compatible = sorted(entry.name for entry, _ in compatible_entries(base, entries))
        raise IncompatibleAdapter(
            f"Adapter {adapter_name!r} cannot be applied to {base.canonical_id}: {exc.message} "
            f"Adapters compatible with this base: {compatible or ['(none)']}.",
            details={
                "adapter": adapter_name,
                "base": base.canonical_id,
                "compatible": compatible,
                "registered": registered,
            },
        ) from exc
    return AdapterSubject(
        base=base, adapter=_identity_of(named), confidence=confidence, entry=named
    )
