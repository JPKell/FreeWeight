"""freeweight.services.inventory — what is in the machines and models tables, as plain data.

The read side of Phase 2's two tables, shared by the HTML pages and (from Phase 3 and Phase 4) the
JSON API and CLI. Everything here returns frozen dataclasses, never ORM instances: SQLAlchemy
models do not leave the repository layer (coding standards §4), and a template that could touch a
mapped attribute would be one lazy load away from a query running inside a render.

Both functions take the application's :class:`~freeweight.services.database.Database` handle and
read through it. They never build an engine: the web application owns one for as long as it
serves, a CLI command owns one for the length of the command, and a service function that decided
that for itself would be wrong for one of the two.

Both reads are declared read-only, so on SQLite they take a deferred ``BEGIN`` and can run
alongside each other and alongside a run recording samples, instead of queueing on the single
write lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from weightsdb import DatabaseError, DatabaseUnavailable

from freeweight.infrastructure.db.repositories.machines import MachineRepository
from freeweight.infrastructure.db.repositories.models import ModelRepository

if TYPE_CHECKING:
    from freeweight.services.database import Database

__all__ = ["MachineSummary", "ModelSummary", "list_machines", "list_models"]


@contextmanager
def _translated() -> Iterator[None]:
    """Translate driver failures into the suite's error hierarchy.

    Without this, a database that has never been migrated reaches the caller as a raw
    ``sqlalchemy.exc.OperationalError`` ("no such table: models") — which a route handler catching
    :class:`~weightsdb.DatabaseError` does not catch, so the page 500s
    instead of rendering the error state it already has.
    """
    try:
        yield
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(f"Could not read the database: {exc}") from exc


@dataclass(frozen=True, slots=True)
class MachineSummary:
    """One row of the machines page.

    Attributes:
        id: The machine's ULID.
        machine_fingerprint: The stable identity every measurement is attributed to.
        hostname: As reported, or ``None`` when the host did not report one.
        os_name: Operating system name, or ``None``.
        os_version: Operating system version, or ``None``.
        cpu_model: CPU model string, or ``None``.
        logical_cores: Logical core count, or ``None`` when not reported.
        ram_bytes: Total RAM in bytes, or ``None`` when not reported.
        first_seen_at: When this machine was first recorded.
        last_seen_at: When it was last recorded.
    """

    id: str
    machine_fingerprint: str
    hostname: str | None
    os_name: str | None
    os_version: str | None
    cpu_model: str | None
    logical_cores: int | None
    ram_bytes: int | None
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class ModelSummary:
    """One row of the models page.

    Attributes:
        id: The model identity's ULID.
        canonical_id: ``provider/name@sha256:digest`` — the display and lookup key (ADR-0024).
        provider_kind: Which kind of provider reported it.
        provider_model_name: Exactly as the provider names it.
        artifact_digest: The weights digest, or ``None`` for a ``name_only`` identity.
        identity_confidence: ``"digest"`` or ``"name_only"``.
        first_seen_at: When this identity was first recorded.
        last_seen_at: When it was last recorded.
    """

    id: str
    canonical_id: str
    provider_kind: str
    provider_model_name: str
    artifact_digest: str | None
    identity_confidence: str
    first_seen_at: datetime
    last_seen_at: datetime


def list_machines(database: Database) -> tuple[MachineSummary, ...]:
    """Return every known machine, oldest sighting first.

    Args:
        database: The application's database handle.

    Returns:
        The machines, as plain data. Empty until Phase 4 writes the first machine profile — which
        is the state the machines page is designed for today.

    Raises:
        DatabaseUnavailable: The database could not be read.
    """
    with _translated(), database.read() as session:
        return tuple(
            MachineSummary(
                id=machine.id,
                machine_fingerprint=machine.machine_fingerprint,
                hostname=machine.hostname,
                os_name=machine.os_name,
                os_version=machine.os_version,
                cpu_model=machine.cpu_model,
                logical_cores=machine.logical_cores,
                ram_bytes=machine.ram_bytes,
                first_seen_at=machine.first_seen_at,
                last_seen_at=machine.last_seen_at,
            )
            for machine in MachineRepository().list_all(session)
        )


def list_models(database: Database) -> tuple[ModelSummary, ...]:
    """Return every known model identity, most recently seen first.

    Args:
        database: The application's database handle.

    Returns:
        The identities, as plain data. Empty until Phase 3's discovery runs — which is the state
        the models page is designed for today.

    Raises:
        DatabaseUnavailable: The database could not be read.
    """
    with _translated(), database.read() as session:
        return tuple(
            ModelSummary(
                id=model.id,
                canonical_id=model.canonical_id,
                provider_kind=model.provider_kind,
                provider_model_name=model.provider_model_name,
                artifact_digest=model.artifact_digest,
                identity_confidence=model.identity_confidence,
                first_seen_at=model.first_seen_at,
                last_seen_at=model.last_seen_at,
            )
            for model in ModelRepository().list_all(session)
        )
