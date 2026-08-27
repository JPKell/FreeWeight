"""freeweight.infrastructure.db.types — portable column types and the ULID primary key.

Written as if it were WeightsDB's own ``types`` module (spec §7): the type behaviour here is a
**major** compatibility contract per that spec §19 — changing how ``UtcDateTime`` or
``PortableJSON`` serialize would require a data migration in every consumer once this is extracted
at Phase 12, so it is not a place for a quick fix later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from baseaicore import ValidationError, new_id
from sqlalchemy import JSON, DateTime, Float, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect
    from sqlalchemy.types import TypeEngine

__all__ = ["PortableJSON", "UtcDateTime", "measurement_columns", "ulid_primary_key"]


class UtcDateTime(TypeDecorator[datetime]):
    """Stores a timezone-aware UTC instant portably across SQLite and PostgreSQL.

    SQLite has no reliable native storage for a timezone-aware value — its ``DATETIME`` affinity
    stores whatever ``isoformat()`` produces as text, and the default parser does not round-trip a
    UTC-offset suffix cleanly. This type sidesteps that dialect trap entirely rather than working
    around it: every bound value is converted to UTC and stored **naive**, and every value read
    back has UTC tzinfo reattached. The instant stored is identical either way; only the
    representation on disk changes, and the same naive-UTC-underneath approach behaves identically
    on PostgreSQL, so there is exactly one code path for both dialects.

    A naive input is rejected rather than assumed to already be UTC (database standards §3,
    weightsdb spec §11.3) — silently guessing here is exactly the kind of ambiguity that breaks a
    timestamp comparison months later, on whichever row happened to be written by the one caller
    that forgot a timezone.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Convert an aware datetime to naive UTC for storage, or raise on a naive input."""
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValidationError(
                "UtcDateTime requires a timezone-aware datetime; got a naive value "
                f"{value!r}. Use baseaicore.utc_now() or attach a timezone explicitly.",
                details={"value": repr(value)},
            )
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Reattach UTC tzinfo to the naive value read back from either dialect."""
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class PortableJSON(TypeDecorator[Any]):
    """JSON storage that becomes ``JSONB`` on PostgreSQL and plain ``JSON`` elsewhere.

    SQLAlchemy's generic ``JSON`` type already round-trips nested structures and unicode
    correctly on SQLite; the only dialect-specific behaviour needed is upgrading to ``JSONB`` on
    PostgreSQL for indexing and containment-query support later in the suite's life.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Return ``JSONB`` on PostgreSQL, else the generic ``JSON`` implementation."""
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


def ulid_primary_key() -> Mapped[str]:
    """Return a ``CHAR(26)`` ULID primary key column, defaulted from :func:`baseaicore.new_id`.

    Time-sortable, safe to expose in URLs and logs, and needs no cross-process sequence
    coordination (database standards §3). The default is a plain callable rather than a
    server-side default so it works identically on SQLite and PostgreSQL without a
    dialect-specific `DEFAULT` expression.
    """
    return mapped_column(String(26), primary_key=True, default=new_id)


def measurement_columns(name: str) -> tuple[Mapped[float | None], Mapped[str | None]]:
    """Return the ``(<name>, <name>_unavailable_reason)`` column pair for a ``Measurement`` field.

    ``NULL`` alone never means "not measurable" (ADR-0016); a table storing a value that may be
    :data:`baseaicore.measurement.UNSUPPORTED` uses this pair rather than a single nullable
    column, so "not yet measured" and "not measurable here, and here is why" stay distinguishable.
    No table in Phase 2 uses this — ``machines`` and ``model_descriptors`` deliberately keep plain
    nullable columns for their numeric fields (data model §2) — but the pair is exercised by every
    table from Phase 5 onward, so it exists now with the fixed shape the later phases depend on.

    Args:
        name: The measurement's base name; the reason column is ``f"{name}_unavailable_reason"``.

    Returns:
        A ``(value_column, reason_column)`` pair. The caller assigns them to two class attributes
        named ``name`` and ``f"{name}_unavailable_reason"`` respectively — this function does not
        (and cannot) name the attributes itself, only build the columns that belong under those
        names.
    """
    value_column: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason_column: Mapped[str | None] = mapped_column(String, nullable=True)
    return value_column, reason_column
