"""Integration tests for the migration history, per database standards §5.2's mandatory list.

Fresh, stepwise, idempotent, downgrade, and failure-plus-restore — **on both dialects**, via the
parametrized ``engine``/``runner`` fixtures in ``conftest.py``. The two cases that are genuinely
SQLite-only (the automatic restore guarantee, and the WAL trap underneath it) say so in their own
names and skip elsewhere rather than pretending to cover PostgreSQL.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from baseaicore import ValidationError
from sqlalchemy import Column, Engine, Integer, MetaData, Table, insert, inspect, text
from sqlalchemy.exc import IntegrityError, StatementError
from weightsdb import (
    MigrationFailed,
    MigrationRunner,
    create_engine_for,
    session_factory,
    session_scope,
)
from weightsdb.backup import backup as take_snapshot

from freeweight.infrastructure.db.base import Base
from freeweight.infrastructure.db.models import (
    Adapter,
    ApiToken,
    Machine,
    Model,
    ModelDescriptor,
    RuntimeProfile,
    Setting,
)
from freeweight.infrastructure.db.repositories.settings import SettingsRepository
from freeweight.services.database import MIGRATIONS_LOCATION, Database, ensure_ready

_EXPECTED_TABLES = {
    Adapter.__tablename__,
    Machine.__tablename__,
    Model.__tablename__,
    ModelDescriptor.__tablename__,
    RuntimeProfile.__tablename__,
    Setting.__tablename__,
    ApiToken.__tablename__,
}


def test_fresh_install_creates_all_tables(
    runner: MigrationRunner, unmigrated_engine: Engine
) -> None:
    outcome = runner.upgrade(backup=False)

    assert outcome.from_revision is None
    assert runner.is_at_head()
    assert _EXPECTED_TABLES <= set(inspect(unmigrated_engine).get_table_names())


def test_fresh_install_enforces_foreign_keys(engine: Engine) -> None:
    """``ON DELETE RESTRICT`` is only a promise if the dialect is actually enforcing keys.

    SQLite enforces foreign keys only when ``PRAGMA foreign_keys=ON`` has been issued on *this*
    connection, which is why the engine sets it in a ``connect`` listener rather than once at
    construction. Without that listener this insert would quietly succeed on SQLite.
    """
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(ModelDescriptor).values(
                id="01J000000000000000000000DE",
                model_id="a-model-that-does-not-exist",
                observed_at=datetime(2026, 8, 26, tzinfo=UTC),
                descriptor_hash="deadbeef",
            )
        )


def test_stepwise_upgrade_applies_the_known_revision(runner: MigrationRunner) -> None:
    assert runner.current() is None

    runner.upgrade("0001", backup=False)

    assert runner.current() == "0001"


def test_upgrade_is_idempotent(runner: MigrationRunner) -> None:
    first = runner.upgrade(backup=False)
    second = runner.upgrade(backup=False)

    assert first.to_revision == second.to_revision
    assert second.backed_up is False


def test_upgrade_then_downgrade_then_upgrade_round_trip(
    runner: MigrationRunner, unmigrated_engine: Engine
) -> None:
    runner.upgrade(backup=False)

    down = runner.downgrade("base")

    assert down.to_revision is None
    assert not _EXPECTED_TABLES & set(inspect(unmigrated_engine).get_table_names())

    runner.upgrade(backup=False)

    assert runner.is_at_head()
    assert _EXPECTED_TABLES <= set(inspect(unmigrated_engine).get_table_names())


def test_check_parity_matches_the_real_schema(runner: MigrationRunner) -> None:
    runner.upgrade(backup=False)

    result = runner.check_parity(Base.metadata)

    assert result.matches is True, result.diff
    assert result.diff == ""


def test_check_parity_detects_a_drifted_model(runner: MigrationRunner) -> None:
    runner.upgrade(backup=False)

    drifted = MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.tables.values():
        table.to_metadata(drifted)
    Table(
        "a_table_the_migration_never_created",
        drifted,
        Column("id", Integer, primary_key=True),
    )

    result = runner.check_parity(drifted)

    assert result.matches is False
    assert result.diff != ""


def test_timezone_aware_round_trip_and_naive_rejection(engine: Engine) -> None:
    """Database standards §3: an aware instant survives; a naive one is refused."""
    aware = datetime(2026, 8, 26, 12, 30, tzinfo=UTC)
    with session_scope(session_factory(engine)) as session:
        session.add(Setting(key="k", value_json={"v": 1}, updated_at=aware))
    with session_scope(session_factory(engine)) as session:
        stored = session.get(Setting, "k")
        assert stored is not None
        assert stored.updated_at == aware
        assert stored.updated_at.tzinfo is not None

    # Raised by UtcDateTime's bind processor, which SQLAlchemy wraps in a StatementError on the
    # way out of a flush — the refusal itself is what matters, not which layer reports it.
    naive = datetime.fromisoformat("2026-08-26T12:30:00")
    with (
        pytest.raises(StatementError) as excinfo,
        session_scope(session_factory(engine)) as session,
    ):
        session.add(Setting(key="naive", value_json=None, updated_at=naive))
    assert isinstance(excinfo.value.orig, ValidationError)


def test_on_delete_restrict_prevents_removing_a_model_with_descriptors(engine: Engine) -> None:
    with session_scope(session_factory(engine)) as session:
        session.add(
            Model(
                id="01J00000000000000000000MOD",
                provider_kind="ollama",
                provider_model_name="llama3:8b",
                artifact_digest=None,
                canonical_id="ollama/llama3:8b",
                identity_confidence="name_only",
            )
        )
        session.flush()
        session.add(
            ModelDescriptor(
                id="01J000000000000000000000DE",
                model_id="01J00000000000000000000MOD",
                observed_at=datetime(2026, 8, 26, tzinfo=UTC),
                descriptor_hash="deadbeef",
            )
        )

    with (
        pytest.raises(IntegrityError),
        session_scope(session_factory(engine)) as session,
    ):
        session.execute(text("DELETE FROM models WHERE id = '01J00000000000000000000MOD'"))


def _broken_history(tmp_path: Path, *, write_before_failing: bool) -> Path:
    """Copy the real history and append a revision that fails, optionally after writing first."""
    script_location = tmp_path / "migrations"
    versions_dir = script_location / "versions"
    versions_dir.mkdir(parents=True)
    real = Path(MIGRATIONS_LOCATION)
    shutil.copy(real / "env.py", script_location / "env.py")
    shutil.copy(real / "script.py.mako", script_location / "script.py.mako")
    shutil.copy(real / "versions" / "0001_initial_schema.py", versions_dir / "0001_initial.py")
    body = (
        "    op.create_table('a_table_from_the_failed_migration',\n"
        "                    sa.Column('id', sa.Integer(), primary_key=True))\n"
        '    op.execute("INSERT INTO settings (key, value_json, updated_at) "\n'
        "               \"VALUES ('written-by-0002', '1', '2026-08-26 00:00:00')\")\n"
        if write_before_failing
        else ""
    )
    (versions_dir / "0002_broken.py").write_text(
        '"""broken revision, deliberately, for the failure+restore test"""\n'
        "from __future__ import annotations\n"
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        "revision = '0002'\n"
        "down_revision = '0001'\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade() -> None:\n"
        f"{body}"
        "    raise RuntimeError('deliberate failure for the restore test')\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )
    return script_location


@pytest.mark.parametrize("write_before_failing", [False, True])
def test_failed_migration_restores_the_original_database_byte_identical(
    tmp_path: Path, write_before_failing: bool
) -> None:
    """A migration that raises must leave the original file untouched (database standards §7).

    Parametrized over whether the doomed revision writes anything before raising, because those
    are different failures. A revision that raises immediately leaves nothing in the WAL, and a
    restore that ignored the ``-wal`` sidecar entirely would still pass. A revision that commits
    DDL and a row first does leave the WAL dirty — and a file-copy restore that replaced only the
    main database file would have that sidecar replayed straight back on top of it, silently
    restoring nothing.
    """
    script_location = _broken_history(tmp_path, write_before_failing=write_before_failing)
    db_path = tmp_path / "test.sqlite3"
    engine = create_engine_for(f"sqlite:///{db_path}")
    runner = MigrationRunner(engine, script_location=str(script_location))
    snapshot_engine = create_engine_for(f"sqlite:///{db_path}")
    try:
        runner.upgrade("0001", backup=False)
        # Compare two independently-taken SQLite-backup-API snapshots, not the live WAL-mode
        # file's raw bytes: WAL means some already-committed data can sit only in the "-wal"
        # sidecar until a checkpoint runs, so two live reads of the main file can legitimately
        # differ byte-for-byte with zero logical difference. The backup API always produces a
        # fully checkpointed, deterministic single-file snapshot, which is the level
        # "byte-identical" actually promises (database standards §7).
        before_snapshot = tmp_path / "before.sqlite3"
        take_snapshot(snapshot_engine, before_snapshot)
        before = before_snapshot.read_bytes()

        with pytest.raises(MigrationFailed) as excinfo:
            runner.upgrade("0002")

        assert excinfo.value.details["restored"] is True
        assert runner.current() == "0001"

        after_snapshot = tmp_path / "after.sqlite3"
        take_snapshot(snapshot_engine, after_snapshot)
        assert after_snapshot.read_bytes() == before

        # And nothing the doomed revision wrote survives, through any connection — the check the
        # byte comparison above cannot make, since it reads a checkpointed copy rather than what
        # a fresh reader of the live database would see.
        reader = create_engine_for(f"sqlite:///{db_path}")
        try:
            # inspect(connection), not inspect(engine): under BEGIN IMMEDIATE a second connection
            # would ask for a write lock this one already holds, and deadlock the test rather
            # than the code.
            with reader.connect() as connection:
                leaked = connection.execute(
                    text("SELECT count(*) FROM settings WHERE key = 'written-by-0002'")
                ).scalar_one()
                tables = set(inspect(connection).get_table_names())
        finally:
            reader.dispose()
        assert leaked == 0
        assert "a_table_from_the_failed_migration" not in tables
    finally:
        engine.dispose()
        snapshot_engine.dispose()


def test_migration_outcome_states_the_dialect_restore_difference(runner: MigrationRunner) -> None:
    """Database standards §7: the difference is stated in ``MigrationOutcome``, not papered over."""
    fresh = runner.upgrade(backup=False)

    # A fresh database has nothing to roll back to, on either dialect.
    assert fresh.restore_on_failure_available is False
    assert fresh.backed_up is False


def test_rc1_database_migrates_to_head_and_keeps_its_rows(tmp_path: Path) -> None:
    """P12's named failure mode, and Phase 15's upgrade path, on one real database.

    The fixture is a database file created by a real ``1.0.0rc1`` install (commit ``0a6bc40``,
    migrated by the pre-adoption in-application runner) — not one this test created — with real
    rows in it. WeightsDB's runner must find that history's ``alembic_version`` rows, apply every
    revision since, and leave every row readable.

    Until Phase 15 this asserted that **no** revision applied, because P12's acceptance criterion
    forbade adding one. ``0008`` is the first revision since, and it is the one that most has to be
    proved on a real database rather than a fresh one: on SQLite it rebuilds ``runs``, which has
    six ``ON DELETE CASCADE`` children, so with foreign keys enforced it would delete every
    measurement in the file and report success (ADR-0082). The row counts below are that
    assertion.
    """
    fixture = (
        Path(__file__).parent.parent / "fixtures" / "databases" / "freeweight-1.0.0rc1.sqlite3"
    )
    working_copy = tmp_path / "rc1.sqlite3"
    shutil.copyfile(fixture, working_copy)

    engine = create_engine_for(f"sqlite:///{working_copy}")
    try:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        current_before = runner.current()
        assert current_before is not None, "the rc1 fixture must carry a recorded revision"
        assert current_before == "0007", (
            f"the rc1 fixture must sit one revision behind head; found {current_before!r} — the "
            "version table was not found, or the history gained a revision nobody accounted for"
        )
        counts_before = _cascading_child_counts(engine)

        outcome = ensure_ready(Database(engine), auto_migrate=True)

        assert outcome is not None, "the pending revisions must apply to an rc1 database"
        assert (outcome.from_revision, outcome.to_revision) == ("0007", runner.heads()[0])
        assert runner.is_at_head()
        assert _cascading_child_counts(engine) == counts_before, (
            "rebuilding `runs` cascaded and deleted rows: the foreign-key pragma guard in env.py "
            "did not take effect (ADR-0082)"
        )

        with engine.connect() as connection:
            hostname = connection.execute(
                text("SELECT hostname FROM machines WHERE machine_fingerprint = :fp"),
                {"fp": "a" * 64},
            ).scalar_one()
            cooldown = connection.execute(
                text("SELECT value_json FROM settings WHERE key = 'execution.cooldown_seconds'")
            ).scalar_one()
            orphans = connection.execute(
                text("SELECT COUNT(*) FROM runs WHERE adapter_id IS NOT NULL")
            ).scalar_one()
        assert hostname == "rc1-fixture-host"
        assert cooldown == 7
        assert orphans == 0, "every pre-1.1 run is a base subject; none may gain an adapter"
    finally:
        engine.dispose()


def test_1_1_0_database_migrates_to_head_and_keeps_its_rows(tmp_path: Path) -> None:
    """The released-version companion to the ``rc1`` fixture above (M9_AUDIT.md Group 3, O1).

    The fixture is a real ``freeweight==1.1.0`` install (from PyPI, in a scratch venv) migrated by
    its own ``freeweight db upgrade``, with two rows written through
    :class:`~freeweight.infrastructure.db.repositories.settings.SettingsRepository` — the same
    repository layer a real CLI write would go through. ``1.1.0`` shipped ``0008``, so this
    asserts the documented no-op (CLI standards §11) while that is head, and the real migration
    once a later revision lands — the point of capturing the fixture before it was needed.
    """
    fixture = Path(__file__).parent.parent / "fixtures" / "databases" / "freeweight-1.1.0.sqlite3"
    working_copy = tmp_path / "freeweight-1.1.0.sqlite3"
    shutil.copyfile(fixture, working_copy)

    engine = create_engine_for(f"sqlite:///{working_copy}")
    try:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        current_before = runner.current()
        assert current_before is not None, "the 1.1.0 fixture must carry a recorded revision"

        outcome = ensure_ready(Database(engine), auto_migrate=True)

        assert runner.is_at_head()
        database = Database(engine)
        repository = SettingsRepository()
        with database.read() as session:
            assert repository.get(session, "fixture.marker") == "freeweight-1.1.0-fixture"
            assert repository.get(session, "fixture.count") == 2
        if current_before == runner.heads()[0]:
            assert outcome is None  # already at head: the documented no-op
        else:  # pragma: no cover — exercised once a migration lands after 1.1.0
            assert outcome is not None
    finally:
        engine.dispose()


def test_1_0_0_database_migrates_to_head_and_keeps_its_rows(tmp_path: Path) -> None:
    """The earliest-published-release companion to the fixtures above (row L8, M9_REAUDIT O1).

    ``1.1.0``'s own fixture test above is a documented no-op — its head and this build's head
    are the same revision, so it never actually drove an upgrade. ``1.0.0`` is FreeWeight's
    earliest PyPI release with a schema at all, and it sits at ``0007``, behind head exactly
    like the ``rc1`` fixture above — so this one exercises the real ``0007 -> head`` migration
    (the same ``runs`` rebuild the `rc1` test's cascade-count guard covers), on rows written by an
    independent, real ``1.0.0`` install rather than by the pre-adoption in-application runner the
    `rc1` fixture came from.
    """
    fixture = Path(__file__).parent.parent / "fixtures" / "databases" / "freeweight-1.0.0.sqlite3"
    working_copy = tmp_path / "freeweight-1.0.0.sqlite3"
    shutil.copyfile(fixture, working_copy)

    engine = create_engine_for(f"sqlite:///{working_copy}")
    try:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        current_before = runner.current()
        assert current_before is not None, "the 1.0.0 fixture must carry a recorded revision"
        assert current_before == "0007", (
            f"the 1.0.0 fixture must sit at the revision 1.0.0 shipped; found {current_before!r}"
        )

        outcome = ensure_ready(Database(engine), auto_migrate=True)

        assert outcome is not None, "the pending revisions must apply to a 1.0.0 database"
        assert (outcome.from_revision, outcome.to_revision) == ("0007", runner.heads()[0])
        assert runner.is_at_head()

        database = Database(engine)
        repository = SettingsRepository()
        with database.read() as session:
            assert repository.get(session, "fixture.marker") == "freeweight-1.0.0-fixture"
            assert repository.get(session, "fixture.count") == 2
    finally:
        engine.dispose()


def _cascading_child_counts(engine: Engine) -> dict[str, int]:
    """Count the rows a cascading rebuild of ``runs`` would take with it."""
    tables = ("runs", "run_tests", "samples", "metric_values", "run_events", "artifacts")
    with engine.connect() as connection:
        return {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608 — table names are the literal tuple above
            for table in tables
        }
