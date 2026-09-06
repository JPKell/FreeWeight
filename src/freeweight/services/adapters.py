"""The adapter registry as a person and a run see it: the directory, the subjects, the record.

Three questions, one service. What does the operator's directory hold
([ADR-0061](../../../docs/adr/0061-the-adapter-registry-is-a-directory-and-a-manifest.md))? Which
``(base, adapter)`` subjects does that produce
([ADR-0058](../../../docs/adr/0058-the-execution-subject-gains-an-adapter-axis.md))? And which
adapters has this installation actually *measured* under, which is the ``adapters`` table and is
FreeWeight's own — the directory is read, the table is written, and the table outlives the
directory because evidence is keyed on the subject
([ADR-0080](../../../docs/adr/0080-a-persisted-decision-names-the-subject-by-reference-and-by-string.md)).

Nothing here decides a panel or a score. That is
[ADR-0059](../../../docs/adr/0059-adapter-evidence-is-measured-never-inherited.md)'s territory and
lives beside the evidence it governs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import ModelIdentity, ProviderKind, SuiteError, utc_now

from freeweight.domain.panels import Panel, compose_panel
from freeweight.domain.subjects import AdapterSubject, enumerate_subjects, subject_for
from freeweight.infrastructure.adapters import read_directory

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from sqlalchemy.orm import Session

    from freeweight.config import AdapterSettings
    from freeweight.domain.capability_mapping import CapabilityMapping
    from freeweight.infrastructure.adapters import AdapterEntry, DirectoryReading
    from freeweight.services.database import Database

__all__ = [
    "AdapterOverview",
    "AdaptersDisabled",
    "SubjectPanel",
    "adapter_overview",
    "adapter_row_for",
    "measured_scores",
    "panel_for",
    "read_entries",
    "resolve_subject",
    "subjects_for_model",
]


class AdaptersDisabled(SuiteError):
    """``[adapters] directory`` is empty, so the feature is off (ADR-0061 rule 2).

    Empty means off **deliberately**, so this is the honest answer to "show me the adapters" on an
    installation that has configured none — not an empty list, which would look like a directory
    somebody had emptied.

    Attributes:
        code: ``"CONFIGURATION_ERROR"``, stable and part of the CLI's exit-code mapping.
    """

    code: ClassVar[str] = "CONFIGURATION_ERROR"

    def __init__(self) -> None:
        """Say which key turns it on: "nothing happened" is not a diagnosis."""
        super().__init__(
            "adapters are not configured: set [adapters] directory to the directory holding your "
            "adapter artifacts and their reviewed manifests. Empty means off, deliberately.",
            details={"field": "adapters.directory"},
        )


@dataclass(frozen=True, slots=True)
class AdapterOverview:
    """Everything ``freeweight adapters list`` shows.

    Attributes:
        reading: One pass over the directory — entries, unreadable manifests, drafts and
            artifacts nobody has reviewed.
        measured: ``artifact_sha256`` for every adapter this installation has a stored row for.
            An adapter in here but not in :attr:`reading` has measurements and no artifact, which
            is a real state and not a corruption: the table outlives the directory.
    """

    reading: DirectoryReading
    measured: frozenset[str] = frozenset()

    def as_json(self) -> dict[str, Any]:
        """Render for ``--json``."""
        return {
            "directory": str(self.reading.directory),
            "adapters": [
                {**entry.as_json(), "measured": entry.artifact_sha256 in self.measured}
                for entry in self.reading.entries
            ],
            "invalid": [
                {"path": str(path), "problem": problem} for path, problem in self.reading.invalid
            ],
            "drafts": [str(path) for path in self.reading.drafts],
            "unmanifested": [str(path) for path in self.reading.unmanifested],
        }


def read_entries(adapters: AdapterSettings) -> tuple[AdapterEntry, ...]:
    """Return what the operator's directory holds, or ``()`` when adapters are off.

    The one place the rest of the application asks "what adapters exist", so a caller never has to
    remember that an empty ``directory`` means off rather than empty.

    Args:
        adapters: ``settings.adapters``.

    Returns:
        Every entry, available or not. Filtering to the usable ones is the caller's decision and
        depends on what it is doing — a listing shows the unavailable ones by name, and an
        enumeration skips them.

    Raises:
        AdapterDirectoryMissing: The configured directory does not exist.
    """
    directory = adapters.resolved_directory()
    return () if directory is None else read_directory(directory).entries


def adapter_overview(database: Database, adapters: AdapterSettings) -> AdapterOverview:
    """Read the directory and mark which of its adapters this installation has measured under.

    Args:
        database: The application's database handle.
        adapters: ``settings.adapters``.

    Returns:
        The overview.

    Raises:
        AdaptersDisabled: ``[adapters] directory`` is empty.
        AdapterDirectoryMissing: It is set to something that is not a directory.
    """
    directory = adapters.resolved_directory()
    if directory is None:
        raise AdaptersDisabled
    reading = read_directory(directory)
    from freeweight.infrastructure.db.models import Adapter

    with database.read() as session:
        measured = frozenset(
            str(digest) for (digest,) in session.query(Adapter.artifact_sha256).all()
        )
    return AdapterOverview(reading=reading, measured=measured)


def _identity_of(model: Any) -> ModelIdentity:  # noqa: ANN401 — an ORM row
    """Lift a stored ``models`` row into the domain's identity."""
    return ModelIdentity(
        provider_kind=ProviderKind(model.provider_kind),
        provider_model_name=model.provider_model_name,
        artifact_digest=model.artifact_digest,
    )


def subjects_for_model(
    model: Any,  # noqa: ANN401 — an ORM row
    entries: tuple[AdapterEntry, ...],
) -> tuple[AdapterSubject, ...]:
    """Return every subject one stored model can be measured as.

    The bare base first and always, then one per compatible adapter. Compatibility is decided by
    **digest** inside :func:`~freeweight.domain.subjects.enumerate_subjects`, never here and never
    by name.

    Args:
        model: A stored ``models`` row.
        entries: What the directory holds, from :func:`read_entries`.

    Returns:
        The subjects.
    """
    return enumerate_subjects(_identity_of(model), entries)


def resolve_subject(
    model: Any,  # noqa: ANN401 — an ORM row
    entries: tuple[AdapterEntry, ...],
    adapter_name: str | None,
) -> AdapterSubject:
    """Resolve ``--adapter <name>`` against one stored model.

    Args:
        model: A stored ``models`` row.
        entries: What the directory holds.
        adapter_name: The adapter asked for, or ``None`` for the bare base.

    Returns:
        The subject.

    Raises:
        IncompatibleAdapter: The name is unknown, unavailable, or not applicable to this base.
            Never a silent fall back to the base.
    """
    return subject_for(_identity_of(model), entries, adapter_name)


def adapter_row_for(
    session: Session, subject: AdapterSubject, *, now: datetime | None = None
) -> Any | None:  # noqa: ANN401 — an ORM row
    """Return (creating if needed) the ``adapters`` row this subject's adapter belongs to.

    Called when a run is created, so a measurement always has a subject to belong to. Keyed on
    ``artifact_sha256``, which is the identity: a rename updates the row's label, and different
    bytes make a different row — and therefore a different subject, which is the correct answer
    rather than an inconvenience (ADR-0061 rule 5).

    **Never deletes and never prunes.** A rescan that no longer finds an adapter leaves its row
    alone: evidence is keyed on the subject and must not be orphaned by an operator tidying a
    directory (ADR-0080).

    Args:
        session: An open write session.
        subject: The subject a run is about to measure.
        now: The instant to stamp; injected for deterministic tests.

    Returns:
        The row, or ``None`` when ``subject`` is a bare base — which needs no row, because the
        bare base is the absence of an adapter rather than a special one.
    """
    if subject.adapter is None or subject.entry is None:
        return None
    from freeweight.infrastructure.db.models import Adapter

    stamp = now if now is not None else utc_now()
    entry = subject.entry
    row = (
        session.query(Adapter)
        .filter(Adapter.artifact_sha256 == subject.adapter.artifact_digest)
        .one_or_none()
    )
    if row is None:
        row = Adapter(
            name=entry.name,
            artifact_sha256=entry.artifact_sha256,
            artifact_path=str(entry.artifact_path),
            source_sha256=entry.source_sha256,
            base_model_name=entry.base_model_name,
            base_artifact_digest=entry.base_artifact_digest,
            base_confidence=entry.base_confidence.value,
            declared_capabilities_json=list(entry.declared_capabilities),
            data_classification=entry.data_classification.value,
            notes=entry.notes,
            first_seen_at=stamp,
            last_seen_at=stamp,
        )
        session.add(row)
        session.flush()
        return row
    # Everything but the identity is refreshed: a manifest re-reviewed under a new name or with new
    # declared capabilities describes the same weights, and the row that measurements point at has
    # to keep up with it. `artifact_sha256` is never written here — different bytes are a different
    # row, not an update to this one.
    row.name = entry.name
    row.artifact_path = str(entry.artifact_path)
    row.source_sha256 = entry.source_sha256
    row.base_model_name = entry.base_model_name
    row.base_artifact_digest = entry.base_artifact_digest
    row.base_confidence = entry.base_confidence.value
    row.declared_capabilities_json = list(entry.declared_capabilities)
    row.data_classification = entry.data_classification.value
    row.notes = entry.notes
    row.last_seen_at = stamp
    session.flush()
    return row


@dataclass(frozen=True, slots=True)
class SubjectPanel:
    """One subject's panel, and the evidence it actually has.

    Attributes:
        subject: The subject.
        panel: What
            [ADR-0059](../../../docs/adr/0059-adapter-evidence-is-measured-never-inherited.md)
            says to run against it.
        measured: ``{capability_id: score}`` **measured on this subject**, and on nothing else.
            Empty for a subject nobody has benchmarked, which is the state every adapter subject
            starts in and is rendered ``—`` rather than a number.
    """

    subject: AdapterSubject
    panel: Panel
    measured: Mapping[str, float]

    @property
    def has_evidence(self) -> bool:
        """Whether anything has been measured on this subject."""
        return bool(self.measured)

    def as_json(self) -> dict[str, Any]:
        """Render for ``--json`` and for the comparison view."""
        return {
            "subject": self.subject.canonical_id,
            "adapter": self.subject.adapter_name,
            "identity_confidence": self.subject.confidence.value,
            "name_only": self.subject.is_name_only,
            "panel": self.panel.as_json(),
            "measured": dict(self.measured),
            "has_evidence": self.has_evidence,
        }


def measured_scores(database: Database, subject: AdapterSubject) -> dict[str, float]:
    """Return the capability scores measured **on this subject**, keyed by capability.

    Filtered on ``subject_canonical_id``, which names the subject exactly — the base and each of
    its adapter subjects are different strings. Filtering on ``model_id`` instead would span all of
    them, and that is precisely the join
    [ADR-0059](../../../docs/adr/0059-adapter-evidence-is-measured-never-inherited.md) forbids: it
    would publish the base's strengths as an adapter's claims while leaving the adapter's damage
    unmeasured, so the fabricated numbers would be exactly the ones that win routing.

    Args:
        database: The application's database handle.
        subject: The subject to read.

    Returns:
        ``{capability_id: score}``. **Empty** for a subject with no measurements — never the base's
        scores, never a discounted copy of them, never a prior. Absent is absent (ADR-0016).
    """
    from freeweight.infrastructure.db.repositories.evidence import EvidenceRepository

    with database.read() as session:
        rows = EvidenceRepository().list_all(session, subject_canonical_id=subject.canonical_id)
        return {str(row.capability_id): float(row.score) for row in rows}


def panel_for(
    database: Database,
    subject: AdapterSubject,
    *,
    mapping: CapabilityMapping,
    available_suites: Sequence[str] | None = None,
) -> SubjectPanel:
    """Compose one subject's panel and read the evidence it already has.

    The base's scores are read **only** to choose the regression panel's third row (catalogue
    §8.2). They decide which suite to *run*; they never become this subject's evidence.

    Args:
        database: The application's database handle.
        subject: The subject.
        mapping: The capability mapping.
        available_suites: What the registry can run, so a panel cannot name a suite that would
            fail at run time instead of at composition.

    Returns:
        The subject's panel and its own measured scores.
    """
    declared = () if subject.entry is None else subject.entry.declared_capabilities
    base = AdapterSubject(base=subject.base, confidence=subject.base.identity_confidence)
    panel = compose_panel(
        mapping,
        declared_capabilities=declared,
        base_scores=measured_scores(database, base),
        available_suites=available_suites,
    )
    return SubjectPanel(subject=subject, panel=panel, measured=measured_scores(database, subject))
