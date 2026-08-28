"""freeweight.goals.starters — the four goal packs that ship with the application.

Subjective Goals §8. They exist for two reasons, and the second matters more than the first:

1. They make the feature demonstrable on a fresh install, with no model, no grading sitting and no
   documentation.
2. **They teach the shape of a good rubric by being read.** In the order
   :data:`READING_ORDER` names them, the share of weight scored deterministically rises from 40 %
   to 90 %, which is the single most useful thing a user can internalize about writing a
   measurable rubric: the better you understand what you want, the less of it needs a judge.

**They are starters, not defaults.** Nothing here is loaded as a runnable goal. A user forks one
into their own ``goals.root``, and until they have edited its criteria or its tasks the fork is
badged ``unforked`` — in the UI, in its results and in its exports. That badge is not a nag: a
voice measured on somebody else's prompts is not the user's voice, and a result that did not say
so would be a measurement of this package rather than of the user's work.

The worked calibration sets in ``calibration.json`` are what make a starter demonstrable end to
end. Each is twelve candidate outputs with the grades a careful author gave them, spanning the
scale — a calibration set that is all excellent has no variance to agree about (§5.1) — plus the
agreement figures that set reproduces under the pack's own ``partition_seed``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baseaicore import NotFoundError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "READING_ORDER",
    "STARTER_ROOT",
    "CalibrationGrade",
    "StarterCalibration",
    "StarterNotFound",
    "StarterPack",
    "fork_starter",
    "list_starters",
    "load_starter_calibration",
    "starter_directory",
]

STARTER_ROOT = Path(__file__).parent
"""Where the shipped packs live. Read-only: nothing writes inside the installed package."""

READING_ORDER: tuple[str, ...] = (
    "creative_voice",
    "technical_explanation",
    "brand_voice",
    "summary_faithfulness",
)
"""The order the packs are meant to be read in, by rising deterministic weight.

40 % → 55 % → 70 % → 90 %. Subjective Goals §8 gives the same pedagogy as "40 % to 70 % to ~90 %"
over a table that lists four packs in a different order; the sentence and the table cannot both be
followed literally, so the order that carries the lesson is declared here and asserted by
``tests/integration/test_starter_packs.py``. See ``PHASE10_ISSUES.md`` for the docs correction
this is waiting on."""

_CALIBRATION_FILE = "calibration.json"


class StarterNotFound(NotFoundError):
    """No starter pack has that key."""


@dataclass(frozen=True, slots=True)
class CalibrationGrade:
    """One grade a worked calibration set carries.

    Attributes:
        sample: The index of the candidate output within the set.
        criterion: The judged criterion this grade is on.
        grade: The author's grade, on that criterion's scale.
        note: Why. Free text, and it is what the disagreement diagnostics quote back — a
            calibration set with no notes still computes an agreement figure and teaches nothing
            when it comes out low.
    """

    sample: int
    criterion: str
    grade: int
    note: str


@dataclass(frozen=True, slots=True)
class StarterCalibration:
    """A starter's worked calibration set and the figures it reproduces.

    Attributes:
        samples: The candidate outputs, in the order the set records them. Order is not the
            grading order — the UI shuffles and blinds — but it is the order the grades index.
        grades: The author's grades.
        expected: The agreement figures this set reproduces under the pack's ``partition_seed``
            and the reference jury described in ``expected_summary``. Keyed by criterion, then by
            figure name: ``kappa_w``, ``rho``, ``mae``, ``bias``, ``n_holdout`` and ``band``.
        summary: The goal-level figures the same run produces, and a description of the reference
            jury that produced them. A shipped figure with no statement of what produced it is a
            number nobody can check.
    """

    samples: tuple[str, ...]
    grades: tuple[CalibrationGrade, ...]
    expected: Mapping[str, Mapping[str, Any]]
    summary: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StarterPack:
    """One shipped starter, as the starters page and ``freeweight goals starters`` show it.

    Attributes:
        key: The pack's key, which is also its directory name and the slug a fork gets by default.
        name: Display label.
        intent: What the pack is for, in the author's words.
        contributes_to: The shipped capability it also feeds, or ``None``.
        criteria_count: How many criteria it declares.
        task_count: How many tasks it ships.
        deterministic_weight: The share of weight scored without a judge, in ``[0, 1]``.
        judged_weight: The remainder.
        carries: The one-line description Subjective Goals §8's table gives it.
        reading_position: Its place in :data:`READING_ORDER`, from 1.
        has_calibration: Whether it ships a worked calibration set.
    """

    key: str
    name: str
    intent: str
    contributes_to: str | None
    criteria_count: int
    task_count: int
    deterministic_weight: float
    judged_weight: float
    carries: str
    reading_position: int
    has_calibration: bool

    def as_json(self) -> dict[str, Any]:
        """The wire form ``GET /api/v1/goals/starters`` returns."""
        return {
            "key": self.key,
            "name": self.name,
            "intent": self.intent,
            "contributes_to": self.contributes_to,
            "criteria_count": self.criteria_count,
            "task_count": self.task_count,
            "deterministic_weight": self.deterministic_weight,
            "judged_weight": self.judged_weight,
            "carries": self.carries,
            "reading_position": self.reading_position,
            "has_calibration": self.has_calibration,
        }


CARRIES: Mapping[str, str] = {
    "creative_voice": (
        "The hardest case: style and tone in creative non-fiction. About 40 % rule weight, judged "
        "remainder. It demonstrates that even 'voice' partly mechanises."
    ),
    "technical_explanation": (
        "Mixed: reading level, shape and self-repetition as rules; correctness and audience fit "
        "judged. The pack where the split is most visible in the score method mix."
    ),
    "brand_voice": (
        "Highest rule weight of the prose packs, about 70 %: banned terms, required disclosures, "
        "register, reading level and shape. The judged remainder is small."
    ),
    "summary_faithfulness": (
        "Mostly rung 3: claim coverage and unsupported-claim detection against annotated sources. "
        "It shows that 'did it make things up' is usually deterministic."
    ),
}
"""Subjective Goals §8's own one-line description of what each pack carries."""


def starter_directory(key: str) -> Path:
    """The shipped directory for one starter.

    Args:
        key: The starter's key.

    Returns:
        Its directory inside the installed package.

    Raises:
        StarterNotFound: No starter has that key.
        ValidationError: The key is not a bare directory name — a path traversal attempt reaches
            here as an ordinary lookup, and is refused before it touches the filesystem
            (security standards §5).
    """
    if key != Path(key).name or key.startswith("."):
        raise ValidationError(
            f"Starter key {key!r} is not a bare name.", details={"field": "key", "value": key}
        )
    directory = STARTER_ROOT / key
    if not (directory / "goal.json").is_file():
        raise StarterNotFound(
            f"No starter pack named {key!r}. The four are: {', '.join(READING_ORDER)}.",
            details={"key": key, "available": list(READING_ORDER)},
        )
    return directory


def list_starters() -> tuple[StarterPack, ...]:
    """Return every shipped starter, in :data:`READING_ORDER`.

    Returns:
        The starters, each summarized from its own pack rather than from a table kept beside it —
        a declared criteria count that drifted from the pack would be a lie on the one page whose
        whole job is to describe what is inside.
    """
    from freeweight.services.goals import load_goal

    packs: list[StarterPack] = []
    for position, key in enumerate(READING_ORDER, start=1):
        goal = load_goal(starter_directory(key))
        total = goal.pack.total_weight or 1.0
        deterministic = sum(
            criterion.weight for criterion in goal.pack.criteria if criterion.rung.is_deterministic
        )
        packs.append(
            StarterPack(
                key=key,
                name=goal.pack.name,
                intent=goal.pack.intent,
                contributes_to=goal.pack.contributes_to,
                criteria_count=len(goal.pack.criteria),
                task_count=len(goal.pack.tasks),
                deterministic_weight=round(deterministic / total, 4),
                judged_weight=round(1.0 - deterministic / total, 4),
                carries=CARRIES[key],
                reading_position=position,
                has_calibration=(starter_directory(key) / _CALIBRATION_FILE).is_file(),
            )
        )
    return tuple(packs)


def load_starter_calibration(key: str) -> StarterCalibration | None:
    """Load one starter's worked calibration set, or ``None`` if it ships without one.

    Raises:
        StarterNotFound: No starter has that key.
    """
    path = starter_directory(key) / _CALIBRATION_FILE
    if not path.is_file():
        return None
    body = json.loads(path.read_text(encoding="utf-8"))
    return StarterCalibration(
        samples=tuple(str(text) for text in body.get("samples", ())),
        grades=tuple(
            CalibrationGrade(
                sample=int(entry["sample"]),
                criterion=str(entry["criterion"]),
                grade=int(entry["grade"]),
                note=str(entry.get("note", "")),
            )
            for entry in body.get("grades", ())
        ),
        expected={
            str(criterion): dict(figures) for criterion, figures in body.get("expected", {}).items()
        },
        summary=dict(body.get("expected_summary", {})),
    )


def _task_bodies(directory: Path) -> list[dict[str, Any]]:
    """Read a starter's task prompt records in declaration order."""
    task_root = directory / "tasks"
    if not task_root.is_dir():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(task_root.glob("*.json"))
    ]


def fork_starter(root: Path, key: str, *, slug: str | None = None) -> Any:  # noqa: ANN401
    """Copy one shipped starter into the user's goals root as a new, editable goal.

    The copy is theirs from the moment it lands: an ordinary directory of JSON they can open in an
    editor, diff in git and edit by hand. Nothing about it points back into the installed package.

    Two fields are set that the shipped pack does not carry:

    * ``forked_from`` records which starter it came from, so a result can say so.
    * ``unforked`` is ``True`` until the criteria or the tasks are edited. The badge is the honest
      statement that this is somebody else's rubric on somebody else's prompts, and it is carried
      into the UI, the results and the exports rather than living only here.

    Args:
        root: ``goals.root`` — where the user's own goals live.
        key: Which starter to fork.
        slug: The new goal's slug. Defaults to the starter's key.

    Returns:
        The loaded :class:`~freeweight.services.goals.LoadedGoal`.

    Raises:
        StarterNotFound: No starter has that key.
        GoalSlugCollision: A goal with that slug already exists.
        GoalPackInvalid: The requested slug is not a usable one.
    """
    from freeweight.services.goals import write_pack

    directory = starter_directory(key)
    body: dict[str, Any] = json.loads((directory / "goal.json").read_text(encoding="utf-8"))
    body["slug"] = slug or key
    body["forked_from"] = key
    body["unforked"] = True
    tasks: Sequence[dict[str, Any]] = _task_bodies(directory)
    for task in tasks:
        task.setdefault("metadata", {}).setdefault("goal_task", {})["is_starter"] = True
    return write_pack(root, goal=body, tasks=tasks)
