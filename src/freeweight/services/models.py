"""freeweight.services.models — discovery through ModelRack, and the models views.

Phase 3's whole responsibility: *FreeWeight can discover Ollama models exclusively through
ModelRack and persist canonical BaseAiCore model identities* (development plan, Phase 3). This
module is the only caller of a :class:`~modelrack.provider.Provider` outside the composition root
that builds one (:mod:`freeweight.infrastructure.providers.factory`) — routes and CLI commands take
a provider as a parameter and call functions here, never the provider directly, so business logic
never lives in a route handler or a command body (coding standards).

Two operations, matching the two ways a caller names a model:

* :func:`discover_models` — the bulk pipeline: list every model the provider serves, upsert each
  identity, and store an immutable descriptor snapshot when its content has actually changed.
* :func:`get_model_detail` — one model by reference (a stored ULID/prefix, a canonical ID, or a bare
  provider name); falls back to :meth:`~modelrack.provider.Provider.resolve` and records the
  observation when the reference is an alias for a name already on file
  (canonical model identity §2.3).

:func:`get_last_discovery` and the ``settings`` row :func:`discover_models` writes are how the
models page and ``models list`` say the data is stale without probing the provider on every read —
the acceptance criterion that the page and CLI "still work and say why the data is stale" while
Ollama is down (development plan, Phase 3) is met by reading what the *last* discovery attempt
found, never by a live call from a page that would otherwise be a plain database read.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from baseaicore import UNSUPPORTED, SuiteError, sha256_of
from baseaicore.timeutil import from_rfc3339, to_rfc3339
from modelrack.errors import ModelNotFound, ProviderError

from freeweight.infrastructure.db.errors import DatabaseUnavailable
from freeweight.infrastructure.db.repositories.model_descriptors import ModelDescriptorRepository
from freeweight.infrastructure.db.repositories.models import ModelRepository
from freeweight.infrastructure.db.repositories.settings import SettingsRepository

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from baseaicore import Measurement
    from baseaicore import ModelDescriptor as CoreModelDescriptor
    from modelrack.provider import Provider
    from sqlalchemy.orm import Session

    from freeweight.infrastructure.db.models import Model
    from freeweight.infrastructure.db.models import ModelDescriptor as OrmModelDescriptor
    from freeweight.services.database import Database

__all__ = [
    "DescriptorSummary",
    "DiscoveryOutcome",
    "LastDiscovery",
    "ModelDetail",
    "ModelListRow",
    "compute_descriptor_hash",
    "discover_models",
    "get_last_discovery",
    "get_model_detail",
    "list_models_with_latest_descriptor",
]

_LAST_DISCOVERY_SETTING_KEY = "models.last_discovery"


@contextmanager
def _translated() -> Iterator[None]:
    """Translate driver failures into the suite's error hierarchy.

    The same translation :func:`freeweight.services.inventory.list_models` applies to its own
    reads: without it, a database that has never been migrated reaches a route as a raw
    ``sqlalchemy.exc.OperationalError`` ("no such table"), which the caller's
    ``except DatabaseError`` does not catch, so the page 500s instead of rendering the error state
    it already has.

    Re-raises any :class:`~baseaicore.SuiteError` unchanged rather than only ``DatabaseError``: a
    block wrapped here may legitimately raise :class:`~modelrack.errors.ModelNotFound`,
    :class:`~baseaicore.ValidationError` or a provider error of its own, and none of those are a
    database failure to be relabelled as one.
    """
    try:
        yield
    except SuiteError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(f"Could not read the database: {exc}") from exc


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    """What one ``freeweight models refresh`` accomplished.

    Attributes:
        added: Identities never seen before this run.
        updated: Identities already known whose descriptor changed (including a ``name_only``
            identity that gained a digest —
            :meth:`~freeweight.infrastructure.db.repositories.models.ModelRepository.upsert_identity`
            upgrades that row in place, so it is "updated", not "added").
        unchanged: Identities already known whose descriptor is byte-identical to the last one
            stored — re-discovery's idempotency (development plan, Phase 3): running this twice
            in a row with nothing changed leaves the descriptor history exactly as long as it was.
        total: ``added + updated + unchanged`` — how many models the provider reported.
        checked_at: When this discovery ran.
    """

    added: int
    updated: int
    unchanged: int
    total: int
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class LastDiscovery:
    """The most recent discovery attempt, as persisted in the ``settings`` table.

    Read by the models page and ``models list`` so they can say why the data might be stale without
    probing the provider themselves (module docstring).

    Attributes:
        checked_at: When that attempt ran.
        ok: Whether it completed. ``False`` means the provider could not be reached or timed out —
            the outcome fields are then all zero, and ``detail`` names what went wrong.
        detail: A human-readable summary: a count on success, the provider's own error on failure.
        added: See :class:`DiscoveryOutcome`. Zero when ``ok`` is ``False``.
        updated: See :class:`DiscoveryOutcome`. Zero when ``ok`` is ``False``.
        unchanged: See :class:`DiscoveryOutcome`. Zero when ``ok`` is ``False``.
    """

    checked_at: datetime
    ok: bool
    detail: str
    added: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass(frozen=True, slots=True)
class ModelListRow:
    """One row of the models list: an identity joined with its latest descriptor.

    Acceptance criterion 1 (development plan, Phase 3): "the UI lists them with quantization,
    parameters and context." :class:`~freeweight.services.inventory.ModelSummary` predates
    descriptors entirely (Phase 2) and stays identity-only; this is the Phase 3 view built for the
    list page and ``models list``.
    """

    id: str
    canonical_id: str
    provider_kind: str
    provider_model_name: str
    artifact_digest: str | None
    identity_confidence: str
    first_seen_at: datetime
    last_seen_at: datetime
    quantization: str | None
    parameter_count: int | None
    max_context: int | None


def list_models_with_latest_descriptor(database: Database) -> tuple[ModelListRow, ...]:
    """Return every model identity joined with its latest descriptor, most recently seen first.

    A pure database read — never touches the provider (module docstring).

    Raises:
        DatabaseUnavailable: The database could not be read.
    """
    descriptor_repo = ModelDescriptorRepository()
    with _translated(), database.read() as session:
        rows = []
        for model in ModelRepository().list_all(session):
            latest = descriptor_repo.latest_for_model(session, model.id)
            rows.append(
                ModelListRow(
                    id=model.id,
                    canonical_id=model.canonical_id,
                    provider_kind=model.provider_kind,
                    provider_model_name=model.provider_model_name,
                    artifact_digest=model.artifact_digest,
                    identity_confidence=model.identity_confidence,
                    first_seen_at=model.first_seen_at,
                    last_seen_at=model.last_seen_at,
                    quantization=latest.quantization if latest is not None else None,
                    parameter_count=latest.parameter_count if latest is not None else None,
                    max_context=latest.max_context if latest is not None else None,
                )
            )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class DescriptorSummary:
    """One immutable descriptor snapshot, as plain data for a template or a CLI table.

    Mirrors :class:`~baseaicore.descriptor.ModelDescriptor`; see its attributes for what each field
    means. ``declared_capabilities`` is rendered as sorted strings rather than the enum the domain
    type carries, since this is already past the boundary where nothing needs the enum back.
    """

    observed_at: datetime
    family: str | None
    architecture: str | None
    parameter_count: int | None
    active_parameter_count: int | None
    expert_count: int | None
    quantization: str | None
    weight_format: str | None
    size_bytes: int | None
    max_context: int | None
    embedding_dim: int | None
    layers: int | None
    attention_heads: int | None
    kv_heads: int | None
    head_dim: int | None
    vocab_size: int | None
    sliding_window: int | None
    declared_capabilities: tuple[str, ...]
    license_text: str | None
    descriptor_hash: str


@dataclass(frozen=True, slots=True)
class ModelDetail:
    """One model identity, its alias history, and its descriptor snapshots — the detail page."""

    id: str
    canonical_id: str
    provider_kind: str
    provider_model_name: str
    artifact_digest: str | None
    identity_confidence: str
    first_seen_at: datetime
    last_seen_at: datetime
    aliases: tuple[dict[str, str], ...]
    latest_descriptor: DescriptorSummary | None
    descriptor_history: tuple[DescriptorSummary, ...]
    resolved_alias: str | None
    """The alias this particular lookup resolved and recorded, or ``None`` if ``reference`` was
    already the stored ULID, canonical ID or provider name — set only on the call that observed it,
    never derived from ``aliases`` (which may hold others from earlier lookups)."""


def _to_optional_int(value: Measurement) -> int | None:
    """Collapse a :data:`~baseaicore.measurement.Measurement` to ``int | None`` for storage.

    ``model_descriptors`` deliberately keeps plain nullable columns (data model §2): unlike a GPU
    reading, "the provider did not report this" and "not yet observed" are not cases this table
    needs to tell apart, so :data:`~baseaicore.measurement.UNSUPPORTED` and a genuinely absent value
    both become ``NULL``.
    """
    return None if value is UNSUPPORTED else int(value)


def compute_descriptor_hash(descriptor: CoreModelDescriptor) -> str:
    """Hash the measurement-defining subset of a descriptor: everything except when it was read.

    Two snapshots that differ only in ``observed_at`` are the same descriptor as far as this
    application's history is concerned
    (:mod:`freeweight.infrastructure.db.repositories.model_descriptors`'s module docstring);
    ``identity`` and ``raw`` are excluded too — identity is the caller's own key, not the
    descriptor's content, and ``raw`` is diagnostics-only (canonical model identity §3) and would
    fragment history over provider response formatting nobody asked to track.

    Computed over the *original* :data:`~baseaicore.measurement.Measurement` values, before
    :func:`_to_optional_int` collapses ``UNSUPPORTED`` to the same ``NULL`` a genuinely-absent
    reading would produce — :func:`~baseaicore.hashing.canonical_json` already renders
    ``UNSUPPORTED`` as the fixed string ``"unsupported"`` (ADR-0016 §4), so a model gaining a
    previously-unreported field is correctly seen as changed.

    ``declared_capabilities`` is a ``frozenset``, whose iteration order is not stable across
    processes; it is sorted here before hashing; canonical JSON's own key sorting does not reach
    inside a list's elements.

    Args:
        descriptor: The descriptor as ModelRack returned it.

    Returns:
        64 lowercase hex characters.
    """
    payload: dict[str, Any] = {
        "family": descriptor.family,
        "architecture": descriptor.architecture,
        "parameter_count": descriptor.parameter_count,
        "active_parameter_count": descriptor.active_parameter_count,
        "expert_count": descriptor.expert_count,
        "quantization": descriptor.quantization,
        "weight_format": descriptor.weight_format,
        "size_bytes": descriptor.size_bytes,
        "max_context": descriptor.max_context,
        "embedding_dim": descriptor.embedding_dim,
        "layers": descriptor.layers,
        "attention_heads": descriptor.attention_heads,
        "kv_heads": descriptor.kv_heads,
        "head_dim": descriptor.head_dim,
        "vocab_size": descriptor.vocab_size,
        "rope_config": dict(descriptor.rope_config) if descriptor.rope_config is not None else None,
        "sliding_window": descriptor.sliding_window,
        "declared_capabilities": sorted(flag.value for flag in descriptor.declared_capabilities),
        "license_text": descriptor.license_text,
    }
    return sha256_of(payload)


def _store_descriptor(
    session: Session, model_id: str, descriptor: CoreModelDescriptor, descriptor_hash: str
) -> OrmModelDescriptor:
    """Insert one immutable snapshot row for ``descriptor``."""
    return ModelDescriptorRepository().insert(
        session,
        model_id=model_id,
        observed_at=descriptor.observed_at,
        family=descriptor.family,
        architecture=descriptor.architecture,
        parameter_count=_to_optional_int(descriptor.parameter_count),
        active_parameter_count=_to_optional_int(descriptor.active_parameter_count),
        expert_count=_to_optional_int(descriptor.expert_count),
        quantization=descriptor.quantization,
        weight_format=descriptor.weight_format,
        size_bytes=_to_optional_int(descriptor.size_bytes),
        max_context=_to_optional_int(descriptor.max_context),
        embedding_dim=_to_optional_int(descriptor.embedding_dim),
        layers=_to_optional_int(descriptor.layers),
        attention_heads=_to_optional_int(descriptor.attention_heads),
        kv_heads=_to_optional_int(descriptor.kv_heads),
        head_dim=_to_optional_int(descriptor.head_dim),
        vocab_size=_to_optional_int(descriptor.vocab_size),
        rope_config_json=dict(descriptor.rope_config)
        if descriptor.rope_config is not None
        else None,
        sliding_window=_to_optional_int(descriptor.sliding_window),
        declared_capabilities_json=sorted(flag.value for flag in descriptor.declared_capabilities),
        license_text=descriptor.license_text,
        raw_json=dict(descriptor.raw),
        descriptor_hash=descriptor_hash,
    )


def _record_discovery_outcome(session: Session, value: dict[str, Any], *, now: datetime) -> None:
    SettingsRepository().set(session, _LAST_DISCOVERY_SETTING_KEY, value, now=now)


def discover_models(database: Database, provider: Provider, *, now: datetime) -> DiscoveryOutcome:
    """Run one full discovery pass: list every model the provider serves and persist it.

    The pipeline (development plan, Phase 3): list, normalize, upsert identity, store a descriptor
    snapshot when it changed. ``refresh=True`` is passed to
    :meth:`~modelrack.provider.Provider.list_models` deliberately — a caller who explicitly asked to
    refresh means it, and should not have a provider's metadata cache hand back a five-minute-old
    answer (:mod:`modelrack.cache`).

    Args:
        database: The application's database handle.
        provider: The provider to discover through, built by
            :func:`~freeweight.infrastructure.providers.factory.build_provider`.
        now: The instant to record every upsert and the outcome against. Injected so callers are
            deterministic in tests.

    Returns:
        The counts this run produced.

    Raises:
        ProviderError: The provider could not be listed (unreachable, timed out, or answered with
            something ModelRack could not parse). The failed attempt is still recorded — see the
            module docstring — before this propagates, so a page reading :func:`get_last_discovery`
            afterwards can say why.
    """
    try:
        descriptors: Sequence[CoreModelDescriptor] = provider.list_models(refresh=True)
    except ProviderError as exc:
        with database.write() as session:
            _record_discovery_outcome(
                session,
                {"ok": False, "checked_at": to_rfc3339(now), "detail": str(exc)},
                now=now,
            )
        raise

    model_repo = ModelRepository()
    added = updated = unchanged = 0
    with database.write() as session:
        for descriptor in descriptors:
            identity = descriptor.identity
            provider_kind = identity.provider_kind.value
            existing = model_repo.get_by_identity(
                session,
                provider_kind=provider_kind,
                provider_model_name=identity.provider_model_name,
                artifact_digest=identity.artifact_digest,
            )
            name_only_sibling = (
                model_repo.get_by_identity(
                    session,
                    provider_kind=provider_kind,
                    provider_model_name=identity.provider_model_name,
                    artifact_digest=None,
                )
                if identity.artifact_digest is not None
                else None
            )
            is_new = existing is None and name_only_sibling is None
            previous_confidence = (
                existing.identity_confidence
                if existing is not None
                else name_only_sibling.identity_confidence
                if name_only_sibling is not None
                else None
            )

            model = model_repo.upsert_identity(
                session,
                provider_kind=provider_kind,
                provider_model_name=identity.provider_model_name,
                artifact_digest=identity.artifact_digest,
                canonical_id=identity.canonical_id,
                identity_confidence=identity.identity_confidence.value,
                now=now,
            )

            descriptor_hash = compute_descriptor_hash(descriptor)
            if is_new:
                added += 1
                _store_descriptor(session, model.id, descriptor, descriptor_hash)
                continue

            latest = ModelDescriptorRepository().latest_for_model(session, model.id)
            content_changed = latest is None or latest.descriptor_hash != descriptor_hash
            confidence_changed = previous_confidence != identity.identity_confidence.value
            if content_changed:
                # A row identical in every hashed field is not re-inserted just because the
                # identity's confidence improved: the confidence lives on `models`, not on this
                # snapshot, and duplicating an unchanged descriptor to reflect it would defeat
                # re-discovery's idempotency (this function's own docstring).
                _store_descriptor(session, model.id, descriptor, descriptor_hash)
            if content_changed or confidence_changed:
                updated += 1
            else:
                unchanged += 1

        outcome = DiscoveryOutcome(
            added=added,
            updated=updated,
            unchanged=unchanged,
            total=len(descriptors),
            checked_at=now,
        )
        _record_discovery_outcome(
            session,
            {
                "ok": True,
                "checked_at": to_rfc3339(now),
                "detail": f"discovered {outcome.total} model(s)",
                "added": added,
                "updated": updated,
                "unchanged": unchanged,
            },
            now=now,
        )
    return outcome


def get_last_discovery(database: Database) -> LastDiscovery | None:
    """Return the most recent discovery attempt, or ``None`` if ``models refresh`` has never run.

    A pure database read — never touches the provider (module docstring).

    Raises:
        DatabaseUnavailable: The database could not be read.
    """
    with _translated(), database.read() as session:
        raw = SettingsRepository().get(session, _LAST_DISCOVERY_SETTING_KEY)
    if raw is None:
        return None
    return LastDiscovery(
        checked_at=from_rfc3339(raw["checked_at"]),
        ok=raw["ok"],
        detail=raw["detail"],
        added=raw.get("added", 0),
        updated=raw.get("updated", 0),
        unchanged=raw.get("unchanged", 0),
    )


def _lookup_stored(session: Session, reference: str) -> Model | None:
    """Try every local, database-only way of resolving ``reference``; ``None`` if none match.

    Tried in order: an application-local ULID or an unambiguous prefix of one, the canonical ID,
    and finally the exact provider-reported name. A prefix matching more than one model is refused
    rather than resolved by picking one (api.md §2).

    Raises:
        ValidationError: ``reference`` is an ambiguous ULID prefix.
    """
    from baseaicore import ValidationError

    repo = ModelRepository()
    prefix_matches = repo.get_by_id_prefix(session, reference)
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        raise ValidationError(
            f"{reference!r} matches {len(prefix_matches)} models; use a longer prefix.",
            details={"reference": reference, "candidates": [m.id for m in prefix_matches]},
        )
    by_canonical_id = repo.get_by_canonical_id(session, reference)
    if by_canonical_id is not None:
        return by_canonical_id
    return repo.get_by_provider_model_name(session, reference)


def _to_detail(
    model: Model,
    latest: OrmModelDescriptor | None,
    history: list[OrmModelDescriptor],
    *,
    resolved_alias: str | None,
) -> ModelDetail:
    def summarize(row: OrmModelDescriptor) -> DescriptorSummary:
        return DescriptorSummary(
            observed_at=row.observed_at,
            family=row.family,
            architecture=row.architecture,
            parameter_count=row.parameter_count,
            active_parameter_count=row.active_parameter_count,
            expert_count=row.expert_count,
            quantization=row.quantization,
            weight_format=row.weight_format,
            size_bytes=row.size_bytes,
            max_context=row.max_context,
            embedding_dim=row.embedding_dim,
            layers=row.layers,
            attention_heads=row.attention_heads,
            kv_heads=row.kv_heads,
            head_dim=row.head_dim,
            vocab_size=row.vocab_size,
            sliding_window=row.sliding_window,
            declared_capabilities=tuple(cast("list[str]", row.declared_capabilities_json or ())),
            license_text=row.license_text,
            descriptor_hash=row.descriptor_hash,
        )

    return ModelDetail(
        id=model.id,
        canonical_id=model.canonical_id,
        provider_kind=model.provider_kind,
        provider_model_name=model.provider_model_name,
        artifact_digest=model.artifact_digest,
        identity_confidence=model.identity_confidence,
        first_seen_at=model.first_seen_at,
        last_seen_at=model.last_seen_at,
        aliases=tuple(cast("list[dict[str, str]]", model.aliases_json or ())),
        latest_descriptor=summarize(latest) if latest is not None else None,
        descriptor_history=tuple(summarize(row) for row in history),
        resolved_alias=resolved_alias,
    )


def get_model_detail(
    database: Database, provider: Provider, reference: str, *, now: datetime
) -> ModelDetail:
    """Resolve ``reference`` to one model and return everything known about it.

    Tries the database first (:func:`_lookup_stored`); only reaches the provider when nothing
    local matches, via :meth:`~modelrack.provider.Provider.resolve`. When that resolution changes
    what was typed — an alias, an old tag, an unambiguous prefix the provider itself accepts — the
    observation is recorded on the model it resolved to (canonical model identity §2.3), never
    silently discarded.

    Args:
        database: The application's database handle.
        provider: The provider to fall back to when ``reference`` names nothing stored yet.
        reference: A stored ULID or prefix, a canonical ID, or a bare provider name.
        now: The instant an alias observation, if any, is recorded against.

    Returns:
        The full detail record.

    Raises:
        ValidationError: ``reference`` is an ambiguous local prefix.
        ModelNotFound: Nothing local matches and the provider does not have ``reference`` either, or
            it resolves to an identity this application has not discovered yet (``models refresh``
            has not run for it).
        ProviderError: The provider could not be reached while falling back to it.
        DatabaseUnavailable: The database could not be read or written.
    """
    model_repo = ModelRepository()
    descriptor_repo = ModelDescriptorRepository()
    with _translated(), database.write() as session:
        model = _lookup_stored(session, reference)
        resolved_alias: str | None = None

        if model is None:
            identity = provider.resolve(reference)
            model = model_repo.get_by_identity(
                session,
                provider_kind=identity.provider_kind.value,
                provider_model_name=identity.provider_model_name,
                artifact_digest=identity.artifact_digest,
            )
            if model is None:
                raise ModelNotFound(
                    f"{reference!r} resolves to {identity.canonical_id} through the provider, but "
                    "it has not been discovered yet. Run `freeweight models refresh` first.",
                    details={
                        "reference": reference,
                        "resolved_canonical_id": identity.canonical_id,
                    },
                )

        if reference not in (model.id, model.canonical_id, model.provider_model_name):
            model = model_repo.record_alias(session, model.id, alias=reference, now=now)
            resolved_alias = reference

        latest = descriptor_repo.latest_for_model(session, model.id)
        history = descriptor_repo.history_for_model(session, model.id)
        return _to_detail(model, latest, history, resolved_alias=resolved_alias)
