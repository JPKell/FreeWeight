"""Integration tests for how transactions are opened, which on SQLite is a real design decision.

``BEGIN IMMEDIATE`` (engine.py) exists to convert an *unrecoverable* mid-transaction failure into
a retryable wait: a deferred transaction that reads and then writes gets ``SQLITE_BUSY_SNAPSHOT``
if anyone committed in between, which ``busy_timeout`` does not apply to and which no amount of
waiting fixes. Taking the write lock up front means contention is felt before any work exists to
lose.

The price is that it declares every transaction a writer, and WAL's whole point is that readers
never contend. So a read-only unit of work has to say so, and these tests pin both halves: that
saying so restores concurrency, and that saying so is enforced rather than trusted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from freeweight.infrastructure.db.session import session_factory, session_scope


def test_two_read_only_transactions_run_concurrently(engine: Engine) -> None:
    """The concurrency an unconditional BEGIN IMMEDIATE gives away."""
    factory = session_factory(engine)

    with session_scope(factory, read_only=True) as first:
        first.execute(text("SELECT count(*) FROM settings")).scalar_one()
        with session_scope(factory, read_only=True) as second:
            assert second.execute(text("SELECT count(*) FROM settings")).scalar_one() == 0


def test_a_reader_does_not_block_a_writer(engine: Engine) -> None:
    """A page view must not queue behind, or in front of, a run recording its samples."""
    factory = session_factory(engine)

    with session_scope(factory, read_only=True) as reader:
        reader.execute(text("SELECT count(*) FROM settings")).scalar_one()
        with session_scope(factory) as writer:
            writer.execute(
                text(
                    "INSERT INTO settings (key, value_json, updated_at) "
                    "VALUES ('written-during-a-read', '1', '2026-08-26 00:00:00')"
                )
            )

    with session_scope(factory, read_only=True) as after:
        assert after.execute(text("SELECT count(*) FROM settings")).scalar_one() == 1


def test_writing_inside_a_read_only_scope_is_refused(engine: Engine, dialect: str) -> None:
    """``read_only`` is enforced, not trusted.

    A write let through here would silently reintroduce the read-then-write upgrade hazard that
    the deferred ``BEGIN`` is only safe *because* it cannot hit.
    """
    if dialect != "sqlite":
        pytest.skip("query_only is a SQLite pragma; PostgreSQL readers never contend anyway")
    factory = session_factory(engine)

    with (
        pytest.raises(OperationalError, match="readonly database"),
        session_scope(factory, read_only=True) as session,
    ):
        session.execute(
            text(
                "INSERT INTO settings (key, value_json, updated_at) "
                "VALUES ('nope', '1', '2026-08-26 00:00:00')"
            )
        )


def test_a_connection_reused_after_a_read_only_scope_can_still_write(engine: Engine) -> None:
    """The failure mode of cleaning up on the way out instead of declaring on the way in.

    ``query_only`` is set on a pooled connection. If it were only reset when a read-only scope
    exits normally, an exception inside one would return a permanently read-only connection to
    the pool, and every later write on it would fail for reasons nothing points at.
    """
    factory = session_factory(engine)

    with pytest.raises(RuntimeError), session_scope(factory, read_only=True) as session:
        session.execute(text("SELECT 1")).scalar_one()
        raise RuntimeError("something went wrong mid-read")

    with session_scope(factory) as session:
        session.execute(
            text(
                "INSERT INTO settings (key, value_json, updated_at) "
                "VALUES ('after-a-failed-read', '1', '2026-08-26 00:00:00')"
            )
        )
    with session_scope(factory, read_only=True) as session:
        assert session.execute(text("SELECT count(*) FROM settings")).scalar_one() == 1
