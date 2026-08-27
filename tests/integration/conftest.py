"""Shared fixtures for integration tests: one migrated database, on each supported dialect.

Database standards §5.2 requires the migration suite to run on **both** supported dialects, and
the plan's Phase 2 test list says the same of the repository suite. Both therefore take the
``engine`` fixture below, which is parametrized over SQLite and PostgreSQL — a suite that ran only
on SQLite would leave the entire ``ON CONFLICT`` half of
:func:`~freeweight.infrastructure.db.upsert.upsert`, and the savepoint-retry path that exists
*for* PostgreSQL's MVCC concurrency, executed by nothing.

PostgreSQL skips when no server is configured, except under ``FWTEST_REQUIRE_POSTGRES=1``, which
turns the skip into a failure so a misconfigured CI job cannot silently stop testing a whole
dialect (weightsdb spec §7 describes the same pattern).

**Neither variable may use the ``FREEWEIGHT_`` prefix.** That prefix belongs to the application's
own settings: ``tests/conftest.py`` strips every ``FREEWEIGHT_*`` variable so no test reads the
developer's environment, and ``load_settings`` would reject the leftovers as unknown keys anyway
(``extra="forbid"``). Named with that prefix, these two were deleted before any test could read
them — so ``FREEWEIGHT_REQUIRE_POSTGRES=1`` quietly did nothing, and the CI job that sets it would
have skipped every PostgreSQL test and still reported green, which is the exact outcome the flag
exists to prevent.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION

_DEFAULT_POSTGRES_URL = "postgresql+psycopg://freeweight:freeweight@localhost:5432/freeweight_test"


@pytest.fixture
def postgres_url() -> str:
    """A reachable PostgreSQL URL, or a skip (or failure, under ``FREEWEIGHT_REQUIRE_POSTGRES``)."""
    url = os.environ.get("FWTEST_POSTGRES_URL", _DEFAULT_POSTGRES_URL)
    require = os.environ.get("FWTEST_REQUIRE_POSTGRES") == "1"
    try:
        engine = create_engine_for(url)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001 — any failure means "no usable server", by design
        if require:
            pytest.fail(f"FWTEST_REQUIRE_POSTGRES=1 but {url} is unreachable: {exc}")
        pytest.skip(f"no PostgreSQL server available at {url}: {exc}")
    return url


def _reset_postgres(url: str) -> None:
    """Return the test database to empty.

    The server is reused across runs, so each test starts by dropping the whole ``public`` schema
    rather than assuming a pristine database — including ``alembic_version``, which a previous
    test's migration left behind and which would otherwise make a "fresh install" test start from
    head.
    """
    engine = create_engine_for(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def dialect(request: pytest.FixtureRequest) -> str:
    """The dialect under test. Parametrizes every test that takes it, directly or transitively."""
    return str(request.param)


@pytest.fixture
def database_url(dialect: str, tmp_path: Path, request: pytest.FixtureRequest) -> str:
    """A URL for an empty, unmigrated database on ``dialect``."""
    if dialect == "sqlite":
        return f"sqlite:///{tmp_path / 'test.sqlite3'}"
    url = str(request.getfixturevalue("postgres_url"))
    _reset_postgres(url)
    return url


@pytest.fixture
def unmigrated_engine(database_url: str) -> Iterator[Engine]:
    """An engine on an empty database, disposed at teardown."""
    engine = create_engine_for(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def engine(unmigrated_engine: Engine) -> Engine:
    """An engine on a database migrated to head."""
    MigrationRunner(unmigrated_engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    return unmigrated_engine


@pytest.fixture
def runner(unmigrated_engine: Engine) -> MigrationRunner:
    """A :class:`MigrationRunner` over the real migration history, on an empty database."""
    return MigrationRunner(unmigrated_engine, script_location=MIGRATIONS_LOCATION)
