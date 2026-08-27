"""freeweight.infrastructure.db.upsert — the one sanctioned upsert.

Database standards §2: application code never writes an ``ON CONFLICT`` by hand, and never uses
select-then-insert, which is a race under both dialects. This is the single place that emits one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects import postgresql, sqlite

if TYPE_CHECKING:
    from sqlalchemy.orm import DeclarativeBase, Session

__all__ = ["upsert"]


def upsert(
    session: Session,
    model: type[DeclarativeBase],
    values: dict[str, Any],
    *,
    index_elements: list[str],
    no_update: frozenset[str] = frozenset(),
) -> None:
    """Insert ``values`` as a new row, or update the existing row conflicting on ``index_elements``.

    The dialect-correct ``INSERT ... ON CONFLICT (index_elements) DO UPDATE SET ...`` for a plain
    (non-partial) unique index or constraint. The ``UPDATE`` branch touches every column named in
    ``values`` **except** those listed in ``no_update`` — which exists for exactly one kind of
    column: one that must be set on the initial insert from the same clock the caller is already
    using (so a test can assert its value), but must never move on a later sighting, such as
    ``first_seen_at``. Passing it as a plain keyword to :meth:`Column.default` would make it
    non-deterministic in tests; omitting it from ``values`` entirely would make it impossible to
    set to anything but wall-clock time on the very first insert. ``no_update`` is how both are
    true at once. Any column present in neither ``values`` nor ``index_elements`` still receives
    its normal ``Column.default`` on the initial insert, exactly as a plain ``INSERT`` would.

    Not usable against a **partial** unique index (one with a ``WHERE`` clause, such as
    ``models``' ``uq_models_name_only``) — PostgreSQL and SQLite both require the same ``WHERE``
    predicate to be repeated on the conflict target for that case, which this function does not
    thread through. ``ModelRepository.upsert_identity`` handles that case with explicit
    update-then-insert logic instead; this function is for the ordinary single-natural-key case
    (``Machine.machine_fingerprint``, ``Setting.key``, ``ApiToken.token_sha256``).

    Args:
        session: The caller's active session; never opened here.
        model: The mapped class to upsert against.
        values: Column values for the insert; also the conflict ``SET`` clause, minus
            ``no_update``. Must not be empty.
        index_elements: The column names forming the unique index or constraint to conflict on.
        no_update: Columns present in ``values`` (for the insert) that must never appear in the
            conflict ``SET`` clause.

    Raises:
        ValueError: ``values`` is empty, or ``session`` is bound to a dialect other than SQLite or
            PostgreSQL (database standards §2 — only these two are supported).
    """
    if not values:
        raise ValueError("upsert() requires at least one column in `values`.")

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        sqlite_insert = sqlite.insert(model).values(**values)
        session.execute(
            sqlite_insert.on_conflict_do_update(
                index_elements=index_elements,
                set_={key: sqlite_insert.excluded[key] for key in values if key not in no_update},
            )
        )
    elif dialect_name == "postgresql":
        postgresql_insert = postgresql.insert(model).values(**values)
        session.execute(
            postgresql_insert.on_conflict_do_update(
                index_elements=index_elements,
                set_={
                    key: postgresql_insert.excluded[key] for key in values if key not in no_update
                },
            )
        )
    else:
        raise ValueError(
            f"upsert() supports sqlite and postgresql only; got dialect {dialect_name!r}."
        )
