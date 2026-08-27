"""freeweight.infrastructure.db.session — session factory and unit-of-work scope.

SQLite's transaction-start behaviour (``BEGIN IMMEDIATE``, so lock contention fails fast rather
than at commit) is dialect-specific plumbing configured once on the engine by
:mod:`freeweight.infrastructure.db.engine`; nothing here branches on dialect.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from freeweight.infrastructure.db.engine import READ_ONLY_EXECUTION_OPTION

__all__ = ["session_factory", "session_scope"]


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory bound to ``engine``.

    ``expire_on_commit=False``: a repository's return value is a detached, plain-data snapshot
    (coding standards §4 — ORM objects never leave the repository layer), and a caller reading an
    attribute off it after commit must not trigger a lazy load on a session that may already be
    closed.

    Args:
        engine: The engine to bind every session this factory produces to.

    Returns:
        A ``sessionmaker`` producing sessions against ``engine``.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope(factory: sessionmaker[Session], *, read_only: bool = False) -> Iterator[Session]:
    """Run one unit of work: commit on success, roll back on any exception, always close.

    "Any exception" includes ``KeyboardInterrupt`` and ``SystemExit`` — a ``Ctrl-C`` mid-write
    must leave the database in its pre-write state, not a half-committed one, so the ``except``
    below is deliberately ``BaseException`` rather than ``Exception``.

    Args:
        factory: A session factory from :func:`session_factory`.
        read_only: Declare that this unit of work only reads. On SQLite that selects a deferred
            ``BEGIN`` rather than ``BEGIN IMMEDIATE``, so concurrent readers stop queueing behind
            one another on the single write lock — the concurrency WAL exists to provide, and
            which an unconditional ``BEGIN IMMEDIATE`` gives away. It is enforced, not trusted:
            an attempted write inside a read-only scope raises ``attempt to write a readonly
            database`` rather than quietly reintroducing the upgrade hazard the declaration was
            supposed to rule out. Inert on PostgreSQL.

    Yields:
        A session open for exactly this unit of work.
    """
    session = factory()
    try:
        if read_only:
            # Opens the transaction now, which is what carries the option into the "begin" hook.
            # Left to the first query, the transaction would already have begun as a writer.
            session.connection(execution_options={READ_ONLY_EXECUTION_OPTION: True})
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
