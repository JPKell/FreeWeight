"""freeweight.services.database_admin — backup, restore, vacuum, deletion and retention.

Kept separate from :mod:`freeweight.services.database`: that module is read-mostly (status,
health, the startup migration check), while this one performs the explicit, operator-initiated
destructive-adjacent operations database standards §7 and §8 govern — always previewed, always
confirmed, always transactional, always logged, never triggered as a side effect of anything else.

It is the service behind ``freeweight db`` and behind the database page's four actions
(``GET /api/v1/database/stats``, ``POST /api/v1/database/delete-preview``,
``DELETE /api/v1/database/results``, ``POST /api/v1/database/backup|vacuum``). Neither surface
holds any of the rules; both call in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from baseaicore import ValidationError, from_rfc3339, sha256_of, to_rfc3339
from sqlalchemy import Engine, delete, func, select, text

from freeweight.config import data_dir
from freeweight.infrastructure.db.backup import (
    BackupResult,
    RestoreResult,
    backup,
    checkpoint,
    database_size_bytes,
    prune_backups,
    reclaimable_bytes,
    restore,
    sqlite_path,
)
from freeweight.infrastructure.db.base import utcnow
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.database import DatabaseStatus, migration_runner

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import Session

    from freeweight.services.database import Database

__all__ = [
    "AUTO_BACKUP_ROW_THRESHOLD",
    "DatabaseStats",
    "DeletionOutcome",
    "DeletionPreview",
    "DeletionScope",
    "DeletionSelection",
    "VacuumOutcome",
    "backup_database",
    "database_stats",
    "delete_results",
    "preview_deletion",
    "prune_database_backups",
    "restore_database",
    "vacuum_database",
]

logger = logging.getLogger(__name__)

# The filename family rotation applies to for operator-invoked `freeweight db backup` with no
# --output. A backup written to a path the operator named is never a rotation candidate.
_MANUAL_BACKUP_PREFIX = "freeweight-"


def _backups_dir(engine: Engine) -> Path:
    """Where automatic and default-path backups live.

    Beside the database file on SQLite, and under the XDG data directory on PostgreSQL, which has
    no local file to sit beside — the case that previously reached
    :func:`~freeweight.infrastructure.db.backup.sqlite_path` and failed with "Expected a SQLite
    engine" for the entirely ordinary ``freeweight db backup`` with no ``--output``.
    """
    if engine.dialect.name == "sqlite":
        return sqlite_path(engine).parent / "backups"
    return data_dir() / "backups"


def _default_backup_path(engine: Engine, *, revision: str | None) -> Path:
    """Choose ``<backups>/freeweight-<revision>-<UTC timestamp>.<ext>``.

    The revision is in the name, not only the timestamp (database standards §7): an operator
    picking one file out of a directory of them needs to know which schema it holds without
    opening it. ``.sqlite3`` for a SQLite copy, ``.dump`` for a ``pg_dump`` custom archive — the
    two are not interchangeable, and the extension is what stops someone trying.
    """
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    suffix = ".sqlite3" if engine.dialect.name == "sqlite" else ".dump"
    return _backups_dir(engine) / f"{_MANUAL_BACKUP_PREFIX}{revision or 'base'}-{stamp}{suffix}"


def backup_database(
    database: Database, *, output: Path | None = None, keep: int = 5
) -> BackupResult:
    """Take a backup of the configured database, per ``freeweight db backup``.

    Works on both dialects: the SQLite backup API for SQLite, ``pg_dump --format=custom`` for
    PostgreSQL.

    Args:
        database: The application's database handle.
        output: Where to write the backup. Defaults to a timestamped, revision-named file under
            ``<data_dir>/backups/``.
        keep: How many default-path backups to retain (``settings.storage.backup_retention``).
            Applies only when ``output`` is ``None`` — a path the operator named is theirs, and
            this function never deletes files from a directory they chose.

    Returns:
        The :class:`~freeweight.infrastructure.db.backup.BackupResult`.
    """
    engine = database.engine
    if output is not None:
        return backup(engine, output)
    revision = migration_runner(engine).current()
    return backup(
        engine,
        _default_backup_path(engine, revision=revision),
        keep=keep,
        prefix=_MANUAL_BACKUP_PREFIX,
    )


def restore_database(database: Database, *, source: Path, confirm: bool) -> RestoreResult:
    """Restore the configured database from a backup, per ``freeweight db restore``.

    SQLite only. On PostgreSQL this refuses and names the ``pg_restore`` invocation instead
    (database standards §7) — an in-process restore there would need privileges the application's
    role deliberately does not hold, and cannot run safely underneath a live database.

    Args:
        database: The application's database handle. Its pool is disposed as part of the restore —
            a file-level swap cannot happen safely underneath live handles — and is reopened on
            next use.
        source: The backup file to restore from.
        confirm: Must be ``True`` — CLI standards §5 requires an explicit flag for anything that
            would otherwise prompt.

    Returns:
        The :class:`~freeweight.infrastructure.db.backup.RestoreResult`.

    Raises:
        DatabaseError: ``confirm`` is ``False``, the dialect is PostgreSQL, or the backup is
            missing, fails its integrity check, or sits at a revision this build does not know.
            See :func:`~freeweight.infrastructure.db.backup.restore`.
    """
    engine = database.engine
    known = migration_runner(engine).known_revisions()
    return restore(engine, source, confirm=confirm, known_revisions=known)


def prune_database_backups(database: Database, *, keep: int) -> tuple[Path, ...]:
    """Rotate default-path backups down to ``keep``, per database standards §7.

    Exposed separately from :func:`backup_database` so retention can be applied without taking a
    new backup — for instance after an operator lowers ``storage.backup_retention``.
    """
    return prune_backups(_backups_dir(database.engine), prefix=_MANUAL_BACKUP_PREFIX, keep=keep)


@dataclass(frozen=True, slots=True)
class VacuumOutcome:
    """The before/after picture of a ``freeweight db vacuum``.

    Attributes:
        estimated_reclaimable_bytes: What the free-page count predicted before running, which is
            the preview database standards §8 requires ("always preview"). Exact on SQLite; ``0``
            on PostgreSQL, which offers no comparable cheap estimate.
        size_before_bytes: Database size immediately before the vacuum.
        size_after_bytes: Database size immediately after it.
        reclaimed_bytes: ``size_before_bytes - size_after_bytes``, floored at zero. A vacuum can
            legitimately reclaim less than predicted — or briefly nothing at all — so this is
            reported as measured rather than assumed to equal the estimate.
    """

    estimated_reclaimable_bytes: int
    size_before_bytes: int
    size_after_bytes: int
    reclaimed_bytes: int


def vacuum_database(database: Database) -> VacuumOutcome:
    """Reclaim free space, per ``freeweight db vacuum``.

    ``VACUUM`` cannot run inside a transaction block on either dialect, so this runs it over a
    connection explicitly set to ``AUTOCOMMIT`` rather than through the ORM session — the one
    place in this module that deliberately does not go through
    :func:`~freeweight.infrastructure.db.session.session_scope`.

    Args:
        database: The application's database handle.

    Returns:
        The :class:`VacuumOutcome`, with the pre-run estimate and the measured before/after sizes.

    Raises:
        DatabaseError: The dialect is neither SQLite nor PostgreSQL.
    """
    engine = database.engine
    if engine.dialect.name not in ("sqlite", "postgresql"):
        raise DatabaseError(
            f"Unsupported dialect {engine.dialect.name!r}; only sqlite and postgresql are "
            "supported.",
            details={"dialect": engine.dialect.name},
        )
    estimate = reclaimable_bytes(engine)
    # Checkpoint on both sides of the VACUUM, or the two sizes are not comparable: under WAL
    # the main file lags the sidecar, and VACUUM itself writes a large WAL. Measured without
    # this, a vacuum that genuinely reclaimed space reports the database as having *grown*.
    checkpoint(engine)
    size_before = database_size_bytes(engine)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM"))
    checkpoint(engine)
    size_after = database_size_bytes(engine)
    return VacuumOutcome(
        estimated_reclaimable_bytes=estimate,
        size_before_bytes=size_before,
        size_after_bytes=size_after,
        reclaimed_bytes=max(0, size_before - size_after),
    )


# ---------------------------------------------------------------------------
# Statistics, deletion and retention (Phase 10)
#
# Everything below implements database standards §8: *always* preview, *always* confirm, *always*
# transactional, with an automatic backup above a row threshold — and never touching a model or a
# machine row, because "history of what exists outlives history of what was measured".
#
# The preview and the deletion are bound together by a token rather than by trust. The token is a
# hash of the selection *and* of the counts the preview reported; the deletion recomputes both and
# refuses if either moved. That is what makes "the preview counts exactly match what deletion
# removes" a property of the code rather than of the interval between two clicks.
# ---------------------------------------------------------------------------

AUTO_BACKUP_ROW_THRESHOLD = 1_000
"""Rows above which a deletion takes a backup first (database standards §8).

Not configurable, and deliberately low: the cost of an unnecessary backup is a few megabytes and a
second, and the cost of a missing one is a user's irreplaceable measurement history."""

_PRESERVED_TABLES: Final[tuple[str, ...]] = (
    "machines",
    "models",
    "model_descriptors",
    "runtime_profiles",
    "benchmark_suites",
    "benchmark_tests",
    "goals",
    "goal_criteria",
    "goal_tasks",
    "calibration_samples",
    "calibration_grades",
    "calibration_reports",
)
"""Tables a result deletion never removes a row from.

Reported *by name and with their counts* in every preview, because "this will delete 412 samples"
answers only half the question a user is actually asking; the other half is "and what survives"."""


class DeletionScope(StrEnum):
    """What a result deletion covers.

    Attributes:
        RUN: One run, by ULID or unambiguous prefix.
        MODEL: Every run of one model.
        SUITE: Every run of one benchmark suite key.
        BEFORE: Every run created strictly before an RFC 3339 instant.
        ALL: Every run in the database. Still previewed, still confirmed, still transactional.
    """

    RUN = "run"
    MODEL = "model"
    SUITE = "suite"
    BEFORE = "before"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class DeletionSelection:
    """A validated description of what to delete.

    Attributes:
        scope: What the deletion covers.
        selector: The scope's argument; ``None`` for :attr:`DeletionScope.ALL`.
    """

    scope: DeletionScope
    selector: str | None = None

    def __post_init__(self) -> None:
        """Refuse a scope/selector pairing that cannot mean anything.

        Raises:
            ValidationError: A scope that needs a selector did not get one, or ``ALL`` got one.
        """
        if self.scope is DeletionScope.ALL:
            if self.selector:
                raise ValidationError(
                    "scope=all takes no selector; drop it, or name the scope you meant.",
                    details={"scope": self.scope.value, "selector": self.selector},
                )
            return
        if not self.selector or not self.selector.strip():
            raise ValidationError(
                f"scope={self.scope.value} needs a selector.",
                details={"scope": self.scope.value, "field": "selector"},
            )

    def as_json(self) -> dict[str, Any]:
        """The wire form, and the thing the preview token is computed over."""
        return {"scope": self.scope.value, "selector": self.selector}


@dataclass(frozen=True, slots=True)
class DeletionPreview:
    """Exactly what a deletion would remove, and exactly what it would keep.

    Attributes:
        selection: What was asked for.
        run_ids: The runs that matched, newest first.
        removed_counts: Rows that would be removed, by table. Only tables with a non-zero count
            appear, so a preview reads as a list of consequences rather than a schema dump.
        preserved_counts: Rows in the tables a result deletion never touches, by table. Present so
            the same numbers can be re-read after the deletion and shown to be unchanged.
        total_rows: The sum of ``removed_counts``.
        token: The confirmation token. Opaque to the caller; recomputed and checked by
            :func:`delete_results`.
        will_backup: Whether the deletion will take a backup first.
    """

    selection: DeletionSelection
    run_ids: tuple[str, ...]
    removed_counts: Mapping[str, int]
    preserved_counts: Mapping[str, int]
    total_rows: int
    token: str
    will_backup: bool

    def as_json(self) -> dict[str, Any]:
        """The wire form ``POST /api/v1/database/delete-preview`` returns."""
        return {
            "selection": self.selection.as_json(),
            "run_ids": list(self.run_ids),
            "run_count": len(self.run_ids),
            "removed_counts": dict(self.removed_counts),
            "preserved_counts": dict(self.preserved_counts),
            "total_rows": self.total_rows,
            "token": self.token,
            "will_backup": self.will_backup,
        }

    def summary_line(self) -> str:
        """The one-sentence preview UI standards §6 and database standards §8 both require."""
        if not self.run_ids:
            return "Nothing matches this selection; nothing would be deleted."
        parts = ", ".join(
            f"{count} {table.replace('_', ' ')}"
            for table, count in sorted(self.removed_counts.items())
        )
        return f"This will delete {parts}. Models and machines are not touched."


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    """What a deletion actually did.

    Attributes:
        preview: The preview it was authorized by.
        deleted_counts: Rows actually removed, by table, as reported by the delete statements.
        backup_path: The backup taken first, or ``None`` when the deletion was below the
            threshold.
        preserved_counts_after: The preserved tables re-counted after the transaction committed.
    """

    preview: DeletionPreview
    deleted_counts: Mapping[str, int]
    backup_path: Path | None
    preserved_counts_after: Mapping[str, int]

    def as_json(self) -> dict[str, Any]:
        """The wire form ``DELETE /api/v1/database/results`` returns."""
        return {
            "run_count": len(self.preview.run_ids),
            "deleted_counts": dict(self.deleted_counts),
            "total_rows": sum(self.deleted_counts.values()),
            "backup_path": str(self.backup_path) if self.backup_path is not None else None,
            "preserved_counts": dict(self.preserved_counts_after),
        }


@dataclass(frozen=True, slots=True)
class DatabaseStats:
    """The ``GET /api/v1/database/stats`` snapshot (API §7).

    Attributes:
        status: Revision, row counts, size and integrity, from
            :func:`~freeweight.services.database.get_status`.
        last_backup_path: The most recent backup on disk, or ``None``.
        last_backup_at: Its modification time, or ``None``.
        backup_count: How many backups the retention directory holds.
        artifact_bytes: How much disk the artifact directory occupies, or ``None`` when it does
            not exist yet. ``None`` rather than ``0``: an artifact directory that has never been
            created and one that is empty are different facts (ADR-0016's spirit).
    """

    status: DatabaseStatus
    last_backup_path: Path | None
    last_backup_at: datetime | None
    backup_count: int
    artifact_bytes: int | None

    def as_json(self) -> dict[str, Any]:
        """The wire form."""
        return {
            "dialect": self.status.dialect,
            "current_revision": self.status.current_revision,
            "head_revision": self.status.head_revision,
            "is_at_head": self.status.is_at_head,
            "size_bytes": self.status.size_bytes,
            "table_row_counts": dict(self.status.table_row_counts),
            "integrity_ok": self.status.integrity_ok,
            "integrity_detail": self.status.integrity_detail,
            "last_backup_path": (
                str(self.last_backup_path) if self.last_backup_path is not None else None
            ),
            "last_backup_at": (
                to_rfc3339(self.last_backup_at) if self.last_backup_at is not None else None
            ),
            "backup_count": self.backup_count,
            "artifact_bytes": self.artifact_bytes,
        }


def _directory_bytes(root: Path) -> int:
    """Total size of every regular file under ``root``."""
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def database_stats(database: Database, *, artifact_dir: Path | None = None) -> DatabaseStats:
    """Assemble the database page's snapshot.

    Args:
        database: The application's database handle.
        artifact_dir: Where run artifacts live, when the caller knows. Omitted in a context that
            has no resolved settings, in which case ``artifact_bytes`` is ``None``.

    Returns:
        The :class:`DatabaseStats`.

    Raises:
        DatabaseUnavailable: The database could not be reached.
    """
    from freeweight.services.database import get_status

    status = get_status(database)
    directory = _backups_dir(database.engine)
    backups = (
        sorted(
            (path for path in directory.iterdir() if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if directory.is_dir()
        else []
    )
    latest = backups[0] if backups else None
    return DatabaseStats(
        status=status,
        last_backup_path=latest,
        last_backup_at=(
            datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC) if latest is not None else None
        ),
        backup_count=len(backups),
        artifact_bytes=(
            _directory_bytes(artifact_dir)
            if artifact_dir is not None and artifact_dir.is_dir()
            else None
        ),
    )


def _matching_run_ids(session: Session, selection: DeletionSelection) -> tuple[str, ...]:
    """Resolve a deletion selection to run IDs, newest first."""
    from freeweight.infrastructure.db.models_runs import BenchmarkSuite, Run
    from freeweight.infrastructure.db.repositories.runs import RunRepository

    statement = select(Run.id).order_by(Run.created_at.desc())
    if selection.scope is DeletionScope.ALL:
        return tuple(session.scalars(statement))
    selector = str(selection.selector)
    if selection.scope is DeletionScope.RUN:
        repository = RunRepository()
        exact = repository.get_by_id(session, selector)
        if exact is not None:
            return (str(exact.id),)
        matches = repository.get_by_id_prefix(session, selector)
        if len(matches) == 1:
            return (str(matches[0].id),)
        if not matches:
            return ()
        raise ValidationError(
            f"{selector!r} matches {len(matches)} runs; use more characters.",
            details={"run": selector, "candidates": [row.id for row in matches]},
        )
    if selection.scope is DeletionScope.SUITE:
        return tuple(
            session.scalars(
                statement.join(BenchmarkSuite, BenchmarkSuite.id == Run.suite_id).where(
                    BenchmarkSuite.key == selector
                )
            )
        )
    if selection.scope is DeletionScope.BEFORE:
        return tuple(session.scalars(statement.where(Run.created_at < from_rfc3339(selector))))
    model_id = _resolve_model_id(session, selector)
    if model_id is None:
        return ()
    return tuple(session.scalars(statement.where(Run.model_id == model_id)))


def _resolve_model_id(session: Session, selector: str) -> str | None:
    """Resolve a model reference to a ``models.id``, or ``None`` when nothing matches.

    A selection that matches no model deletes nothing rather than raising: a preview of "nothing"
    is a legitimate, informative answer, and the UI shows it as such.
    """
    from freeweight.infrastructure.db.repositories.models import ModelRepository

    repository = ModelRepository()
    candidates = repository.get_by_id_prefix(session, selector)
    if len(candidates) == 1:
        return str(candidates[0].id)
    if len(candidates) > 1:
        return None
    row = (
        repository.get_by_canonical_id(session, selector)
        or repository.get_by_id(session, selector)
        or repository.get_by_provider_model_name(session, selector)
    )
    return str(row.id) if row is not None else None


def _removal_counts(session: Session, run_ids: Sequence[str]) -> dict[str, int]:
    """Count every row a deletion of ``run_ids`` would remove, by table.

    Counted with the same subqueries the deletion uses, so the preview and the deletion cannot
    disagree about what "belongs to" a run. A table that would lose nothing is omitted.
    """
    from freeweight.infrastructure.db.models_goals import CriterionScore, JudgeVerdict
    from freeweight.infrastructure.db.models_runs import (
        Artifact,
        MetricValue,
        Run,
        RunEvent,
        RunTest,
        Sample,
        TelemetryGpuSample,
        TelemetrySample,
        ToolCall,
    )

    if not run_ids:
        return {}
    ids = list(run_ids)
    run_tests = select(RunTest.id).where(RunTest.run_id.in_(ids))
    samples = select(Sample.id).where(Sample.run_test_id.in_(run_tests))
    criterion_scores = select(CriterionScore.id).where(CriterionScore.sample_id.in_(samples))
    plans: tuple[tuple[str, Any], ...] = (
        ("judge_verdicts", JudgeVerdict.criterion_score_id.in_(criterion_scores)),
        ("criterion_scores", CriterionScore.sample_id.in_(samples)),
        ("tool_calls", ToolCall.sample_id.in_(samples)),
        ("samples", Sample.run_test_id.in_(run_tests)),
        ("metric_values", MetricValue.run_id.in_(ids)),
        ("telemetry_gpu_samples", TelemetryGpuSample.run_id.in_(ids)),
        ("telemetry_samples", TelemetrySample.run_id.in_(ids)),
        ("artifacts", Artifact.run_id.in_(ids)),
        ("run_events", RunEvent.run_id.in_(ids)),
        ("run_tests", RunTest.run_id.in_(ids)),
        ("runs", Run.id.in_(ids)),
    )
    models_by_table = {
        "judge_verdicts": JudgeVerdict,
        "criterion_scores": CriterionScore,
        "tool_calls": ToolCall,
        "samples": Sample,
        "metric_values": MetricValue,
        "telemetry_gpu_samples": TelemetryGpuSample,
        "telemetry_samples": TelemetrySample,
        "artifacts": Artifact,
        "run_events": RunEvent,
        "run_tests": RunTest,
        "runs": Run,
    }
    counts: dict[str, int] = {}
    for table, condition in plans:
        total = session.execute(
            select(func.count()).select_from(models_by_table[table]).where(condition)
        ).scalar_one()
        if total:
            counts[table] = int(total)
    return counts


def _preserved_counts(session: Session) -> dict[str, int]:
    """Count :data:`_PRESERVED_TABLES` through their mapped classes."""
    from freeweight.infrastructure.db.models import (
        Machine,
        Model,
        ModelDescriptor,
        RuntimeProfile,
    )
    from freeweight.infrastructure.db.models_goals import (
        CalibrationGrade,
        CalibrationReport,
        CalibrationSample,
        Goal,
        GoalCriterion,
        GoalTaskRow,
    )
    from freeweight.infrastructure.db.models_runs import BenchmarkSuite, BenchmarkTestRow

    mapped = {
        "machines": Machine,
        "models": Model,
        "model_descriptors": ModelDescriptor,
        "runtime_profiles": RuntimeProfile,
        "benchmark_suites": BenchmarkSuite,
        "benchmark_tests": BenchmarkTestRow,
        "goals": Goal,
        "goal_criteria": GoalCriterion,
        "goal_tasks": GoalTaskRow,
        "calibration_samples": CalibrationSample,
        "calibration_grades": CalibrationGrade,
        "calibration_reports": CalibrationReport,
    }
    return {
        table: int(session.execute(select(func.count()).select_from(model)).scalar_one())
        for table, model in ((name, mapped[name]) for name in _PRESERVED_TABLES)
    }


def _preview_token(selection: DeletionSelection, counts: Mapping[str, int]) -> str:
    """Bind a confirmation to one selection *and* one set of counts.

    Recomputed at deletion time, so a run that finished between the preview and the confirmation
    invalidates the token rather than being swept up silently. The user sees a fresh preview
    instead, which is the honest outcome: what they were shown is no longer what would happen.
    """
    return sha256_of({"selection": selection.as_json(), "counts": dict(sorted(counts.items()))})


def preview_deletion(database: Database, selection: DeletionSelection) -> DeletionPreview:
    """Report exactly what deleting ``selection`` would remove, and what it would keep.

    Args:
        database: The application's database handle.
        selection: What to delete.

    Returns:
        The :class:`DeletionPreview`, including the token :func:`delete_results` requires.

    Raises:
        ValidationError: A run reference is an ambiguous prefix, or ``scope=before`` was given
            something that is not an RFC 3339 instant.
    """
    with database.read() as session:
        run_ids = _matching_run_ids(session, selection)
        removed = _removal_counts(session, run_ids)
        preserved = _preserved_counts(session)
    total = sum(removed.values())
    return DeletionPreview(
        selection=selection,
        run_ids=run_ids,
        removed_counts=removed,
        preserved_counts=preserved,
        total_rows=total,
        token=_preview_token(selection, removed),
        will_backup=total >= AUTO_BACKUP_ROW_THRESHOLD,
    )


def delete_results(
    database: Database,
    selection: DeletionSelection,
    *,
    token: str,
    keep_backups: int = 5,
) -> DeletionOutcome:
    """Delete stored results, previewed and confirmed.

    One transaction, in dependency order, with an automatic backup first when the selection is at
    or above :data:`AUTO_BACKUP_ROW_THRESHOLD` rows (database standards §8). The delete statements
    are explicit rather than relying on ``ON DELETE CASCADE``: the cascade is declared and is what
    the schema enforces, but issuing the statements here is what lets the outcome report a count
    per table, which is what makes "the preview counts exactly match what deletion removes"
    checkable rather than assumed.

    Models, machines, descriptors, runtime profiles, benchmark definitions, goals and every
    calibration row are untouched, and are re-counted afterwards so the caller can show it.

    Args:
        database: The application's database handle.
        selection: What to delete.
        token: The token from a :func:`preview_deletion` of the same selection.
        keep_backups: Retention for the automatic backup, when one is taken.

    Returns:
        The :class:`DeletionOutcome`.

    Raises:
        DatabaseError: ``token`` does not match a fresh preview of ``selection`` — either it was
            not from this selection, or the database changed since it was issued.
    """
    from freeweight.infrastructure.db.models_goals import CriterionScore, JudgeVerdict
    from freeweight.infrastructure.db.models_runs import (
        Artifact,
        MetricValue,
        Run,
        RunEvent,
        RunTest,
        Sample,
        TelemetryGpuSample,
        TelemetrySample,
        ToolCall,
    )

    preview = preview_deletion(database, selection)
    if token != preview.token:
        raise DatabaseError(
            "This deletion was not confirmed against what it would do now. Preview it again — "
            "either the token is from a different selection, or the database changed since it "
            "was issued.",
            details={"scope": selection.scope.value, "selector": selection.selector},
        )
    if not preview.run_ids:
        return DeletionOutcome(
            preview=preview,
            deleted_counts={},
            backup_path=None,
            preserved_counts_after=preview.preserved_counts,
        )

    backup_path: Path | None = None
    if preview.will_backup:
        backup_path = backup_database(database, keep=keep_backups).path

    ids = list(preview.run_ids)
    deleted: dict[str, int] = {}
    with database.write() as session:
        run_tests = select(RunTest.id).where(RunTest.run_id.in_(ids))
        samples = select(Sample.id).where(Sample.run_test_id.in_(run_tests))
        criterion_scores = select(CriterionScore.id).where(CriterionScore.sample_id.in_(samples))
        # Deepest first. The order is not decoration: on PostgreSQL a parent deleted first would
        # cascade rows this loop then reports as zero, and the outcome would understate what it
        # removed.
        statements: tuple[tuple[str, Any], ...] = (
            (
                "judge_verdicts",
                delete(JudgeVerdict).where(JudgeVerdict.criterion_score_id.in_(criterion_scores)),
            ),
            (
                "criterion_scores",
                delete(CriterionScore).where(CriterionScore.sample_id.in_(samples)),
            ),
            ("tool_calls", delete(ToolCall).where(ToolCall.sample_id.in_(samples))),
            ("samples", delete(Sample).where(Sample.run_test_id.in_(run_tests))),
            ("metric_values", delete(MetricValue).where(MetricValue.run_id.in_(ids))),
            (
                "telemetry_gpu_samples",
                delete(TelemetryGpuSample).where(TelemetryGpuSample.run_id.in_(ids)),
            ),
            ("telemetry_samples", delete(TelemetrySample).where(TelemetrySample.run_id.in_(ids))),
            ("artifacts", delete(Artifact).where(Artifact.run_id.in_(ids))),
            ("run_events", delete(RunEvent).where(RunEvent.run_id.in_(ids))),
            ("run_tests", delete(RunTest).where(RunTest.run_id.in_(ids))),
            ("runs", delete(Run).where(Run.id.in_(ids))),
        )
        for table, statement in statements:
            # rowcount is on CursorResult, which a DELETE always returns; execute() is typed
            # as returning the Result base class, so the narrowing has to be asserted here.
            result = session.execute(statement)
            count = int(result.rowcount or 0)  # type: ignore[attr-defined]  # CursorResult
            if count:
                deleted[table] = count
    with database.read() as session:
        preserved_after = _preserved_counts(session)
    logger.info(
        "database.results_deleted",
        extra={
            "scope": selection.scope.value,
            "runs": len(ids),
            "rows": sum(deleted.values()),
            "backup": str(backup_path) if backup_path is not None else None,
        },
    )
    return DeletionOutcome(
        preview=preview,
        deleted_counts=deleted,
        backup_path=backup_path,
        preserved_counts_after=preserved_after,
    )
