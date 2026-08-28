"""freeweight.infrastructure.db.repositories.goals — the only writer of the goal tables.

Two repositories: :class:`GoalRepository`, which projects a pack on disk into ``goals``,
``goal_criteria`` and ``goal_tasks``, and :class:`CriterionScoreRepository`, which writes the
per-criterion rows a goal's headline number drills to.

**The pack is the source of truth and these rows are its projection.** So a goal is *replaced*
rather than patched: :meth:`GoalRepository.sync` deletes the criteria and tasks it holds and writes
what the pack now declares, inside the caller's transaction. Patching would leave a criterion the
user deleted from the file still scoring runs, which is the one way a goal's rows could disagree
with the rubric its author is reading.

**Criterion scores are written in the same transaction as their sample.** A composite can never be
read back with fewer criteria than the sample it belongs to, for the same reason
``tool_calls`` rows are written with theirs
([ADR-0033](../../../../../../docs/adr/0033-benchmark-interaction-protocol.md)).

Repository methods take a session and never open one (database standards §6): the service owns the
transaction boundary. Every return value is a detached ORM instance, never a live query result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, func, select

from freeweight.infrastructure.db.models_goals import (
    CriterionScore,
    Goal,
    GoalCriterion,
    GoalTaskRow,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.orm import Session

__all__ = ["CriterionScoreRepository", "GoalRepository"]


class GoalRepository:
    """Reads and writes ``goals``, ``goal_criteria`` and ``goal_tasks``."""

    def get_by_slug(self, session: Session, slug: str) -> Goal | None:
        """Return the goal with this slug, or ``None``."""
        return session.scalars(select(Goal).where(Goal.slug == slug)).one_or_none()

    def get_by_id(self, session: Session, goal_id: str) -> Goal | None:
        """Return one goal by primary key, or ``None``."""
        return session.get(Goal, goal_id)

    def list_goals(self, session: Session) -> list[Goal]:
        """Return every goal, by slug."""
        return list(session.scalars(select(Goal).order_by(Goal.slug)))

    def criteria(self, session: Session, goal_id: str) -> list[GoalCriterion]:
        """Return one goal's criteria in declaration order."""
        return list(
            session.scalars(
                select(GoalCriterion)
                .where(GoalCriterion.goal_id == goal_id)
                .order_by(GoalCriterion.ordinal)
            )
        )

    def tasks(self, session: Session, goal_id: str) -> list[GoalTaskRow]:
        """Return one goal's tasks in declaration order."""
        return list(
            session.scalars(
                select(GoalTaskRow)
                .where(GoalTaskRow.goal_id == goal_id)
                .order_by(GoalTaskRow.ordinal)
            )
        )

    def sync(  # noqa: PLR0913 — a goal row is exactly this set of facts
        self,
        session: Session,
        *,
        slug: str,
        values: dict[str, Any],
        criteria: Sequence[dict[str, Any]],
        tasks: Sequence[dict[str, Any]],
        now: datetime,
    ) -> Goal:
        """Insert or replace one goal and everything it declares.

        A criterion or task the pack still declares is **updated in place**, matched by its key.
        That is not an optimisation: ``calibration_grades`` cascades from ``goal_criteria``, so
        deleting and recreating a criterion on every load would destroy the author's grades — the
        most expensive rows in the database — the next time anything read the pack. Only a
        criterion the pack no longer declares is deleted, and losing its grades is then correct:
        the measurement it graded no longer exists.

        Args:
            session: The caller's session.
            slug: The goal's stable identifier.
            values: The ``goals`` column values, ``slug`` and the timestamps excluded.
            criteria: The ``goal_criteria`` rows, in declaration order; ``goal_id`` and
                ``ordinal`` are filled in here.
            tasks: The ``goal_tasks`` rows, likewise.
            now: The clock the caller injected.

        Returns:
            The stored goal row.
        """
        existing = self.get_by_slug(session, slug)
        if existing is None:
            goal = Goal(slug=slug, created_at=now, updated_at=now, **values)
            session.add(goal)
            session.flush()
        else:
            for column, value in values.items():
                setattr(existing, column, value)
            existing.updated_at = now
            goal = existing
            session.flush()
        _reconcile(
            session,
            model=GoalCriterion,
            existing=self.criteria(session, goal.id),
            declared=criteria,
            goal_id=goal.id,
        )
        _reconcile(
            session,
            model=GoalTaskRow,
            existing=self.tasks(session, goal.id),
            declared=tasks,
            goal_id=goal.id,
        )
        session.flush()
        return goal

    def delete(self, session: Session, goal_id: str) -> int:
        """Delete one goal and everything that cascades from it.

        Returns:
            The number of goal rows removed: ``1``, or ``0`` when nothing matched.
        """
        result = cast("CursorResult[Any]", session.execute(delete(Goal).where(Goal.id == goal_id)))
        return int(result.rowcount or 0)

    def criterion_ids(self, session: Session, goal_id: str) -> dict[str, str]:
        """Return ``{criterion_key: row_id}`` for one goal."""
        return {row.key: row.id for row in self.criteria(session, goal_id)}


def _reconcile(
    session: Session,
    *,
    model: type[GoalCriterion] | type[GoalTaskRow],
    existing: Sequence[GoalCriterion | GoalTaskRow],
    declared: Sequence[Mapping[str, Any]],
    goal_id: str,
) -> None:
    """Update rows the pack still declares, insert new ones, delete the rest.

    Matched by ``key``, which is the identifier the pack itself never renames — a renamed
    criterion is a new criterion, and a criterion whose row was recreated would take its grades
    with it.
    """
    by_key = {row.key: row for row in existing}
    keep: set[str] = set()
    for ordinal, body in enumerate(declared):
        key = str(body["key"])
        keep.add(key)
        row = by_key.get(key)
        if row is None:
            session.add(model(goal_id=goal_id, ordinal=ordinal, **dict(body)))
            continue
        row.ordinal = ordinal
        for column, value in body.items():
            setattr(row, column, value)
    removed = [row.id for row in existing if row.key not in keep]
    if removed:
        session.execute(delete(model).where(model.id.in_(removed)))


class CriterionScoreRepository:
    """Writes and reads ``criterion_scores``."""

    def insert_many(self, session: Session, rows: Sequence[dict[str, Any]]) -> list[CriterionScore]:
        """Insert one sample's criterion rows.

        Args:
            session: The caller's session — the same one the sample was written in.
            rows: The column values.

        Returns:
            The inserted rows, flushed so their ids are available to the judge verdicts that hang
            off them.
        """
        created = [CriterionScore(**body) for body in rows]
        session.add_all(created)
        session.flush()
        return created

    def list_for_sample(self, session: Session, sample_id: str) -> list[CriterionScore]:
        """Return one sample's criterion rows, by criterion key."""
        return list(
            session.scalars(
                select(CriterionScore)
                .where(CriterionScore.sample_id == sample_id)
                .order_by(CriterionScore.criterion_key)
            )
        )

    def count_for_criterion(self, session: Session, goal_criterion_id: str) -> int:
        """Return how many samples have been scored against one criterion."""
        return int(
            session.scalar(
                select(func.count())
                .select_from(CriterionScore)
                .where(CriterionScore.goal_criterion_id == goal_criterion_id)
            )
            or 0
        )
