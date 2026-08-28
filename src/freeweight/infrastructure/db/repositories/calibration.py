"""freeweight.infrastructure.db.repositories.calibration — the calibration tables' only writer.

Four tables and three repositories: :class:`CalibrationSampleRepository` and
:class:`CalibrationGradeRepository` hold the author's work, :class:`CalibrationReportRepository`
holds what the jury was measured against it, and :class:`JudgeVerdictRepository` holds every
juror's answer on every run sample.

**Grading survives being interrupted.** Grading twelve samples across five criteria is a real
sitting (Subjective Goals §5.5), so a grade is *upserted* per ``(sample, criterion)``: a partial
submission is normal, resubmitting one grade updates rather than duplicates it, and the progress a
caller reads back is whatever has actually been recorded.

**Verdicts are kept in full.** :class:`JudgeVerdictRepository` writes one row per juror per
repetition, in the same transaction as the criterion score it belongs to. The jury's dispersion
*is* the measurement's error bar; averaging it at write time would destroy the thing being
characterized.

Repository methods take a session and never open one (database standards §6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select

from freeweight.infrastructure.db.models_goals import (
    CalibrationGrade,
    CalibrationReport,
    CalibrationSample,
    JudgeVerdict,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from sqlalchemy.orm import Session

__all__ = [
    "CalibrationGradeRepository",
    "CalibrationReportRepository",
    "CalibrationSampleRepository",
    "JudgeVerdictRepository",
]


class CalibrationSampleRepository:
    """Reads and writes ``calibration_samples``."""

    def insert_many(
        self, session: Session, rows: Sequence[Mapping[str, Any]]
    ) -> list[CalibrationSample]:
        """Insert candidate outputs for the author to grade.

        Returns:
            The inserted rows, flushed so their ids can be handed straight to a grading UI.
        """
        created = [CalibrationSample(**dict(body)) for body in rows]
        session.add_all(created)
        session.flush()
        return created

    def list_for_goal(
        self, session: Session, goal_id: str, *, partition: str | None = None
    ) -> list[CalibrationSample]:
        """Return one goal's calibration samples, optionally one half of the partition."""
        statement = select(CalibrationSample).where(CalibrationSample.goal_id == goal_id)
        if partition is not None:
            statement = statement.where(CalibrationSample.partition == partition)
        return list(session.scalars(statement.order_by(CalibrationSample.id)))

    def get(self, session: Session, sample_id: str) -> CalibrationSample | None:
        """Return one calibration sample, or ``None``."""
        return session.get(CalibrationSample, sample_id)

    def set_partition(self, session: Session, *, sample_id: str, partition: str, seed: int) -> None:
        """Record which half of the seeded split one sample landed in."""
        row = session.get(CalibrationSample, sample_id)
        if row is not None:
            row.partition = partition
            row.partition_seed = seed

    def existing_hashes(self, session: Session, goal_id: str) -> set[str]:
        """Return the content hashes already stored for one goal.

        Used to make sample collection idempotent: pasting the same text twice adds one sample,
        because two identical samples would be graded twice and counted twice in an agreement
        figure that assumes independent observations.
        """
        return set(
            session.scalars(
                select(CalibrationSample.content_sha256).where(CalibrationSample.goal_id == goal_id)
            )
        )

    def delete_for_goal(self, session: Session, goal_id: str) -> int:
        """Delete every calibration sample of one goal, and the grades that cascade from them."""
        result = session.execute(
            delete(CalibrationSample).where(CalibrationSample.goal_id == goal_id)
        )
        return int(getattr(result, "rowcount", 0) or 0)


class CalibrationGradeRepository:
    """Reads and writes ``calibration_grades`` — the most valuable rows in the database."""

    def upsert(  # noqa: PLR0913 — a grade is exactly these facts
        self,
        session: Session,
        *,
        calibration_sample_id: str,
        goal_criterion_id: str,
        grade: int,
        note: str | None,
        graded_by: str,
        graded_at: datetime,
    ) -> CalibrationGrade:
        """Record one grade, replacing any previous grade for the same sample and criterion.

        Upserted rather than inserted, because grading is resumable and re-grading is a normal
        act: an author who changes their mind about sample seven has not created a second
        observation of it.
        """
        existing = session.scalars(
            select(CalibrationGrade).where(
                CalibrationGrade.calibration_sample_id == calibration_sample_id,
                CalibrationGrade.goal_criterion_id == goal_criterion_id,
            )
        ).one_or_none()
        if existing is not None:
            existing.grade = grade
            existing.note = note
            existing.graded_by = graded_by
            existing.graded_at = graded_at
            session.flush()
            return existing
        row = CalibrationGrade(
            calibration_sample_id=calibration_sample_id,
            goal_criterion_id=goal_criterion_id,
            grade=grade,
            note=note,
            graded_by=graded_by,
            graded_at=graded_at,
        )
        session.add(row)
        session.flush()
        return row

    def list_for_goal(self, session: Session, goal_id: str) -> list[CalibrationGrade]:
        """Return every grade of one goal, joined through its samples."""
        return list(
            session.scalars(
                select(CalibrationGrade)
                .join(
                    CalibrationSample,
                    CalibrationSample.id == CalibrationGrade.calibration_sample_id,
                )
                .where(CalibrationSample.goal_id == goal_id)
                .order_by(CalibrationGrade.calibration_sample_id)
            )
        )

    def count_for_goal(self, session: Session, goal_id: str) -> int:
        """Return how many grades one goal has, across all its samples and criteria."""
        return int(
            session.scalar(
                select(func.count())
                .select_from(CalibrationGrade)
                .join(
                    CalibrationSample,
                    CalibrationSample.id == CalibrationGrade.calibration_sample_id,
                )
                .where(CalibrationSample.goal_id == goal_id)
            )
            or 0
        )


class CalibrationReportRepository:
    """Reads and writes ``calibration_reports``."""

    def replace_for_goal(
        self, session: Session, goal_id: str, rows: Sequence[Mapping[str, Any]]
    ) -> list[CalibrationReport]:
        """Replace one goal's calibration report with a freshly measured one.

        Replaced rather than appended: a calibration report describes the instrument *as it now
        is*, and two reports for one goal would leave every reader deciding which is current.
        The history that matters — what a *run* was measured under — is on the run's own record.
        """
        session.execute(delete(CalibrationReport).where(CalibrationReport.goal_id == goal_id))
        created = [CalibrationReport(goal_id=goal_id, **dict(body)) for body in rows]
        session.add_all(created)
        session.flush()
        return created

    def list_for_goal(self, session: Session, goal_id: str) -> list[CalibrationReport]:
        """Return one goal's report rows, the goal-level row first."""
        rows = list(
            session.scalars(select(CalibrationReport).where(CalibrationReport.goal_id == goal_id))
        )
        return sorted(rows, key=lambda row: (row.goal_criterion_id is not None, row.id))

    def goal_level(self, session: Session, goal_id: str) -> CalibrationReport | None:
        """Return the ``goal_criterion_id IS NULL`` row: the weighted figures and the verdict."""
        return session.scalars(
            select(CalibrationReport).where(
                CalibrationReport.goal_id == goal_id,
                CalibrationReport.goal_criterion_id.is_(None),
            )
        ).one_or_none()


class JudgeVerdictRepository:
    """Writes ``judge_verdicts`` — one row per juror per repetition, retained in full."""

    def insert_many(
        self, session: Session, rows: Sequence[Mapping[str, Any]]
    ) -> list[JudgeVerdict]:
        """Insert one criterion score's verdicts, in the transaction that wrote the score."""
        created = [JudgeVerdict(**dict(body)) for body in rows]
        session.add_all(created)
        session.flush()
        return created

    def list_for_criterion_score(
        self, session: Session, criterion_score_id: str
    ) -> list[JudgeVerdict]:
        """Return every verdict behind one criterion score, in polling order."""
        return list(
            session.scalars(
                select(JudgeVerdict)
                .where(JudgeVerdict.criterion_score_id == criterion_score_id)
                .order_by(JudgeVerdict.juror_ordinal, JudgeVerdict.repetition)
            )
        )
