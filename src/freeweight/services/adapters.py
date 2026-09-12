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
    "adapter_catalog",
    "can_serve_adapters",
    "adapter_overview",
    "adapter_row_for",
    "measured_scores",
    "panel_for",
    "read_entries",
    "resolve_subject",
    "serving_mode",
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


def serving_mode(provider: Any, entries: Sequence[AdapterEntry]) -> bool | None:
    """Report the ``RuntimeProfile.adapters_registered`` this run is actually served under.

    ADR-0074 rule 3: the constructing application sets the field, because it is the actor that
    also supplied the registration set. FreeWeight offers the directory's **available** entries to
    the provider at construction
    (:func:`~freeweight.infrastructure.providers.factory.register_adapters_with`), so this function
    answers the same question that call did, from the same two inputs.

    Not ``list_adapters()``: a snapshot moves while a restart is pending, and ADR-0074's rejected
    alternative is explicit that the profile is what the caller asked for rather than something a
    provider stamps on afterwards.

    Getting this wrong is not cosmetic and is not caught by anything local. ``None`` "disagrees
    with nothing and is always served", so a run that registers three adapters and records ``None``
    is served happily, exports evidence under a profile hash that never happened, and a consumer
    resolving the honest ``True`` excludes that evidence as ``evidence_profile_mismatch`` — silently
    and correctly. Row H5's I18 found exactly that.

    Args:
        provider: The constructed provider this run will be served by.
        entries: What the operator's adapter directory holds, from :func:`read_entries` — every
            entry, available or not.

    Returns:
        ``True`` when this provider can serve adapters and at least one available adapter was
        offered to it; ``False`` when it can and none was; ``None`` when the provider has no
        concept of adapters at all, which is every profile written before this field existed.
    """
    if not provider.capabilities().adapter_hot_swap:
        return None
    return any(entry.available for entry in entries)


def can_serve_adapters(provider: Any) -> bool:
    """Whether this provider can apply a LoRA adapter at all.

    ``provider.kind`` is not the question and is never asked: the capability is ModelRack's to
    declare (``adapter_hot_swap``), and only ``LlamaCppProvider`` declares it today. Under any other
    provider the operator's directory stays configured and **inert** — read, listed, and offered to
    nobody (ADR-0140) — so every surface that lists adapters can say which it is instead of letting
    an operator wonder why the adapter they reviewed is never used.

    Args:
        provider: The constructed provider.

    Returns:
        ``True`` when an adapter can be served, ``False`` when the directory is inert.
    """
    return bool(provider.capabilities().adapter_hot_swap)


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


def adapter_catalog(
    database: Database, adapters: AdapterSettings, *, provider: Any | None = None
) -> dict[str, Any]:
    """Everything ``GET /api/v1/adapters`` answers: the directory, and what was measured under each.

    The directory is read once (:func:`adapter_overview`) and joined with the ``adapters`` table,
    which outlives it (ADR-0080): an adapter measured once and since removed from the directory is
    still listed, ``in_directory: false``. For each adapter with a row, the runs created under it
    are counted and, per base it was measured on, the scores **measured on the adapter subject**
    are set beside the bare base's — side by side, never merged (ADR-0059).

    Args:
        database: The application's database handle.
        adapters: ``settings.adapters``.
        provider: The running provider, to answer ``provider_can_serve`` — whether an adapter can
            be applied here at all (:func:`can_serve_adapters`). ``None`` leaves the key ``null``:
            "nobody asked a provider" and "the provider cannot serve them" are different facts, and
            a caller with no provider must not report the second.

    Returns:
        The document api.md §2a describes. Adapters being off, or the directory missing, is a
        ``note`` beside the table's rows rather than an error.
    """
    from baseaicore import to_rfc3339
    from sqlalchemy import func, select

    from freeweight.infrastructure.adapters import AdapterDirectoryMissing
    from freeweight.infrastructure.db.models import Adapter, Model
    from freeweight.infrastructure.db.models_evidence import CapabilityEvidence
    from freeweight.infrastructure.db.models_runs import Run

    reading: DirectoryReading | None = None
    note: str | None = None
    try:
        reading = adapter_overview(database, adapters).reading
    except (AdaptersDisabled, AdapterDirectoryMissing) as exc:
        note = exc.message
    entries: dict[str, dict[str, Any]] = {
        entry.artifact_sha256: {
            **entry.as_json(),
            "in_directory": True,
            "measured": False,
            "run_count": 0,
            "last_run_at": None,
            "subjects": [],
        }
        for entry in (reading.entries if reading is not None else ())
    }
    with database.read() as session:
        for row in session.scalars(select(Adapter).order_by(Adapter.name)):
            declared = row.declared_capabilities_json
            entry = entries.setdefault(
                row.artifact_sha256,
                {
                    "name": row.name,
                    "artifact_sha256": row.artifact_sha256,
                    "artifact_path": row.artifact_path,
                    "manifest_path": None,
                    "source_sha256": row.source_sha256,
                    "base_model_name": row.base_model_name,
                    "base_artifact_digest": row.base_artifact_digest,
                    "base_confidence": row.base_confidence,
                    "declared_capabilities": (
                        [str(one) for one in declared] if isinstance(declared, list) else []
                    ),
                    "data_classification": row.data_classification,
                    "notes": row.notes,
                    "available": False,
                    "unavailable_reason": "no longer in the adapter directory",
                    "in_directory": False,
                },
            )
            count, last = session.execute(
                select(func.count(Run.id), func.max(Run.created_at)).where(Run.adapter_id == row.id)
            ).one()
            evidence = list(
                session.scalars(
                    select(CapabilityEvidence).where(CapabilityEvidence.adapter_id == row.id)
                )
            )
            bases = set(session.scalars(select(Run.model_id).where(Run.adapter_id == row.id)))
            bases.update(one.model_id for one in evidence)
            subjects = []
            for model_id in sorted(bases):
                model = session.get(Model, model_id)
                mine = [one for one in evidence if one.model_id == model_id]
                bare = session.scalars(
                    select(CapabilityEvidence).where(
                        CapabilityEvidence.model_id == model_id,
                        CapabilityEvidence.adapter_id.is_(None),
                    )
                )
                subjects.append(
                    {
                        "base": model.canonical_id if model is not None else model_id,
                        "subject": mine[0].subject_canonical_id if mine else None,
                        "measured": {one.capability_id: one.score for one in mine},
                        "base_measured": {one.capability_id: one.score for one in bare},
                    }
                )
            entry.update(
                measured=True,
                run_count=int(count),
                last_run_at=None if last is None else to_rfc3339(last),
                subjects=subjects,
            )
    return {
        "enabled": adapters.enabled,
        "directory": str(reading.directory) if reading is not None else None,
        "note": note,
        "provider_can_serve": None if provider is None else can_serve_adapters(provider),
        "adapters": sorted(
            entries.values(), key=lambda one: (str(one["name"]), str(one["artifact_sha256"]))
        ),
        "invalid": [
            {"path": str(path), "problem": problem}
            for path, problem in (reading.invalid if reading is not None else ())
        ],
        "drafts": [str(path) for path in (reading.drafts if reading is not None else ())],
        "unmanifested": [
            str(path) for path in (reading.unmanifested if reading is not None else ())
        ],
    }


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


@dataclass(frozen=True, slots=True)
class ServingModeArm:
    """One arm of a serving-mode A/B.

    Attributes:
        registered: Whether the server this arm ran against had adapters registered.
        run_id: The run.
        profile_hash: That run's ``runtime_profile_hash``. **The two arms differ here**, which is
            what makes them two separable measurements rather than a comparison this application
            has to remember how to make.
    """

    registered: bool
    run_id: str
    profile_hash: str


@dataclass(frozen=True, slots=True)
class ServingModeResult:
    """Both arms of one serving-mode A/B.

    Attributes:
        clean: The arm served with no adapters registered.
        registered: The arm served with the operator's adapters registered.
    """

    clean: ServingModeArm
    registered: ServingModeArm

    @property
    def separable(self) -> bool:
        """Whether the two arms are permanently distinguishable, which they must be."""
        return self.clean.profile_hash != self.registered.profile_hash

    def as_json(self) -> dict[str, Any]:
        """Render for ``--json``."""
        return {
            "clean": {
                "run_id": self.clean.run_id,
                "runtime_profile_hash": self.clean.profile_hash,
                "adapters_registered": False,
            },
            "registered": {
                "run_id": self.registered.run_id,
                "runtime_profile_hash": self.registered.profile_hash,
                "adapters_registered": True,
            },
            "separable": self.separable,
        }
