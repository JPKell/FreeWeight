"""freeweight.infrastructure.db.base — the declarative base and its naming convention.

FreeWeight owns this ``MetaData``/``DeclarativeBase`` exclusively: database standards §1 forbids a
shared ``Base`` with domain meaning, so this module is never imported by another application and
carries no table with cross-application significance.

The naming convention is not cosmetic. Alembic's autogenerate diff and SQLite's batch-mode ALTER
both need every constraint and index to have a stable, predictable name; without one, a constraint
recreated by batch mode gets an auto-generated name that differs from the one the model produces,
and the parity check (database standards §5.2) fails forever on a schema that is actually correct.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

__all__ = ["Base", "utcnow"]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The one declarative base for every FreeWeight-owned table.

    ``metadata`` here is the single source of truth Alembic's autogenerate compares against
    (``MigrationRunner.check_parity``) — a model added without importing it into
    :mod:`freeweight.infrastructure.db.models` is invisible to that check, not merely untested.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Return the current instant, timezone-aware in UTC.

    Used only as a ``mapped_column`` default for ``created_at``/``first_seen_at``-style columns —
    an infrastructure-layer concern distinct from the ``Clock`` a service or domain function takes
    as a parameter (coding standards §5). Callers that need a deterministic, injectable timestamp
    for a row still pass one explicitly at the repository call site; this default only covers the
    case where nobody did.
    """
    return datetime.now(UTC)
