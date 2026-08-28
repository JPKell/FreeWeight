"""Integration: the four starter packs, and the properties that make them worth shipping.

Subjective Goals §8 asks four things of a starter, and each is checked here:

* it **parses, validates and lints clean** — a shipped pack with an error finding would fail the
  application's own startup for anyone who forked it;
* its **worked calibration set reproduces its documented agreement figures** under a fixed seed
  and a reference jury, so the number on the starters page is a fact rather than a decoration;
* forking one and leaving it alone badges it **unforked** — in the pack, in the UI and in exports;
* read in the documented order, the packs demonstrate **rising deterministic weight**, which is
  the lesson they exist to teach.

The reference jury is deterministic and derived from the author's own grades with a documented
alternating one-point scatter. It is not a model: a shipped figure has to be reproducible on any
machine, with no GPU and no network, or nobody can check it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from freeweight.domain.goals.lint import Severity
from freeweight.domain.jury import assemble_jury
from freeweight.domain.scorers.judged import JurorVerdict, combine_verdicts
from freeweight.goals.starters import (
    CARRIES,
    READING_ORDER,
    StarterNotFound,
    fork_starter,
    list_starters,
    load_starter_calibration,
    starter_directory,
)
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.calibration import (
    GradeSubmission,
    add_samples,
    record_grades,
    run_calibration,
)
from freeweight.services.database import MIGRATIONS_LOCATION, Database
from freeweight.services.goals import load_goal, sync_goals

REFERENCE_JURY_BIAS = 0
REFERENCE_JURY_SCATTER = 1
REFERENCE_JURY_SIZE = 3


@dataclass(frozen=True)
class ReferenceJury:
    """The deterministic jury a starter's documented figures were measured against.

    It reproduces the author's own grade with an alternating one-point scatter, which is what
    makes the resulting ``kappa_w`` high but not perfect — a starter whose worked set produced
    1.0 would teach nothing about what a real jury does.

    Attributes:
        truth: ``{sample content: {criterion key: the author's grade}}``.
        bias: A constant offset, clamped to the scale.
        scatter: The alternating offset.
        jurors: How many jurors to report, so inter-juror agreement is computable.
    """

    truth: dict[str, dict[str, int]]
    bias: int = REFERENCE_JURY_BIAS
    scatter: int = REFERENCE_JURY_SCATTER
    jurors: int = REFERENCE_JURY_SIZE
    assembly: Any = field(
        default_factory=lambda: assemble_jury(
            ["a", "b", "c"], candidate=None, jury_size=REFERENCE_JURY_SIZE
        )
    )
    seen: list[str] = field(default_factory=list)
    anchors: dict[str, Any] = field(default_factory=dict)

    def with_anchors(self, anchors: Any) -> ReferenceJury:  # noqa: ANN401 — the protocol's type
        """Return a copy bound to the partition's own exemplars, as the real service does."""
        return replace(self, anchors=dict(anchors))

    def judge_prompt_reference(self) -> dict[str, str]:
        """The judge prompt's identity, as the real service reports it."""
        return {
            "prompt_id": "goals.judge.rubric",
            "prompt_version": "1.0.0",
            "prompt_sha256": "sha256:" + "ab" * 32,
        }

    def grade_all(self, criteria: Sequence[Any], response_text: str, case: Any) -> list[Any]:  # noqa: ANN401 — JudgedCriterionResult
        """Grade every criterion deterministically from the author's own grade."""
        del case
        self.seen.append(response_text)
        offset = self.scatter if len(self.seen) % 2 == 0 else -self.scatter
        results = []
        for criterion in criteria:
            points = criterion.scale.points if criterion.scale else 5
            base = self.truth.get(response_text, {}).get(criterion.key, 3)
            grade = max(1, min(points, base + self.bias + offset))
            results.append(
                combine_verdicts(
                    criterion,
                    [
                        JurorVerdict(
                            juror_canonical_id=f"juror{index}",
                            juror_ordinal=index,
                            repetition=1,
                            grade=grade,
                            rationale="reference jury",
                        )
                        for index in range(self.jurors)
                    ],
                )
            )
        return results


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    """A migrated, empty database."""
    url = f"sqlite:///{tmp_path / 'starters.sqlite3'}"
    engine = create_engine_for(url)
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    handle = Database.from_url(url)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def goals_root(tmp_path: Path) -> Path:
    root = tmp_path / "goals"
    root.mkdir()
    return root


@pytest.mark.parametrize("key", READING_ORDER)
class TestEveryStarterIsValid:
    def test_it_parses(self, key: str) -> None:
        goal = load_goal(starter_directory(key))

        assert goal.slug == key
        assert goal.pack.criteria
        assert goal.pack.tasks
        assert goal.goal_hash.startswith("sha256:")

    def test_it_lints_without_an_error(self, key: str) -> None:
        """A shipped pack with an error finding would fail startup for whoever forked it."""
        goal = load_goal(starter_directory(key))

        errors = [finding for finding in goal.findings if finding.severity is Severity.ERROR]
        assert errors == [], [finding.as_json() for finding in errors]

    def test_its_only_warnings_are_the_lints_own_suggestions(self, key: str) -> None:
        """The mechanizable-criterion hint over-fires by design and is a suggestion, not a fault.

        Asserted rather than ignored: any *other* warning on a shipped pack is something a user
        would have to fix in content they did not write.
        """
        goal = load_goal(starter_directory(key))

        unexpected = [
            finding.code
            for finding in goal.findings
            if finding.severity is Severity.WARNING
            and finding.code != "MECHANIZABLE_JUDGED_CRITERION"
        ]
        assert unexpected == []

    def test_every_judged_criterion_has_an_anchored_scale(self, key: str) -> None:
        goal = load_goal(starter_directory(key))

        for criterion in goal.pack.judged_criteria:
            assert criterion.scale is not None, criterion.key
            assert criterion.scale.anchored, criterion.key

    def test_it_ships_a_worked_calibration_set_spanning_the_scale(self, key: str) -> None:
        """A set that is all excellent has no variance to agree about (Subjective Goals §5.1)."""
        calibration = load_starter_calibration(key)

        assert calibration is not None
        assert len(calibration.samples) == 12  # noqa: PLR2004 — the documented target
        by_criterion: dict[str, set[int]] = {}
        for grade in calibration.grades:
            by_criterion.setdefault(grade.criterion, set()).add(grade.grade)
        for criterion, grades in by_criterion.items():
            assert min(grades) <= 2, f"{criterion} has no weak examples"  # noqa: PLR2004
            assert max(grades) >= 4, f"{criterion} has no strong examples"  # noqa: PLR2004
        assert all(grade.note for grade in calibration.grades), "a grade with no note"

    def test_its_worked_set_reproduces_its_documented_figures(
        self, key: str, database: Database, goals_root: Path
    ) -> None:
        """The claim on the starters page, recomputed from the shipped grades."""
        goal = fork_starter(goals_root, key)
        sync_goals(database, [goal])
        calibration = load_starter_calibration(key)
        assert calibration is not None

        ids = add_samples(
            database, goal, contents=[{"content": text} for text in calibration.samples]
        )
        truth: dict[str, dict[str, int]] = {}
        submissions = []
        for grade in calibration.grades:
            submissions.append(
                GradeSubmission(
                    sample_id=ids[grade.sample],
                    criterion_key=grade.criterion,
                    grade=grade.grade,
                    note=grade.note,
                )
            )
            truth.setdefault(calibration.samples[grade.sample], {})[grade.criterion] = grade.grade
        record_grades(database, goal, submissions, graded_by="starter")

        outcome = run_calibration(
            database, goal, jury=ReferenceJury(truth=truth), graded_by="starter"
        )

        measured = {agreement.criterion_key: agreement.as_json() for agreement in outcome.criteria}
        assert set(measured) == set(calibration.expected)
        for criterion, expected in calibration.expected.items():
            for figure in ("kappa_w", "rho", "mae", "bias"):
                assert measured[criterion][figure] == pytest.approx(expected[figure], abs=1e-6), (
                    f"{key}.{criterion}.{figure}"
                )
            assert measured[criterion]["n_holdout"] == expected["n_holdout"]
            assert measured[criterion]["band"] == expected["band"]
        assert outcome.verdict.judge_validity_factor == pytest.approx(
            calibration.summary["judge_validity_factor"], abs=1e-6
        )
        assert outcome.verdict.state.value == calibration.summary["calibration_state"]

    def test_it_runs_end_to_end_on_a_fresh_install(
        self, key: str, database: Database, goals_root: Path
    ) -> None:
        """Acceptance criterion 3: each pack runs end to end and shows its deterministic weight."""
        from freeweight.services.goals import summarize
        from freeweight.services.runs import build_registry

        goal = fork_starter(goals_root, key)
        sync_goals(database, [goal])
        registry = build_registry(goals=[goal])

        suite = registry.get(f"goal.{key}")
        assert suite.tests, "the goal installed no runnable test"
        summary = summarize(goal)
        deterministic = summary.score_method_mix["rule"] + summary.score_method_mix["reference"]
        assert deterministic == pytest.approx(
            next(pack.deterministic_weight for pack in list_starters() if pack.key == key),
            abs=1e-4,
        )


class TestTheGoalExports:
    def test_a_calibrated_goal_exports_a_setspec_calibration_report(
        self, database: Database, goals_root: Path
    ) -> None:
        """Spec §7.3's ``benchmark.calibration_report``, validated by SetSpec's own reader."""
        from setspec.goal.v1 import CalibrationReportIn

        from freeweight.services.export import iter_goal_export

        goal = fork_starter(goals_root, "brand_voice")
        sync_goals(database, [goal])
        calibration = load_starter_calibration("brand_voice")
        assert calibration is not None
        ids = add_samples(
            database, goal, contents=[{"content": text} for text in calibration.samples]
        )
        truth: dict[str, dict[str, int]] = {}
        submissions = []
        for grade in calibration.grades:
            submissions.append(
                GradeSubmission(
                    sample_id=ids[grade.sample],
                    criterion_key=grade.criterion,
                    grade=grade.grade,
                    note=grade.note,
                )
            )
            truth.setdefault(calibration.samples[grade.sample], {})[grade.criterion] = grade.grade
        record_grades(database, goal, submissions, graded_by="starter")
        run_calibration(database, goal, jury=ReferenceJury(truth=truth), graded_by="starter")

        document = json.loads(
            "".join(iter_goal_export(database, goal, document="calibration_report"))
        )

        assert document["schema"] == "benchmark.calibration_report"
        parsed = CalibrationReportIn.model_validate(document["payload"])
        assert parsed.goal_hash == goal.goal_hash
        assert parsed.n_holdout > 0
        # Every coefficient carries its n: a kappa_w without one is a number pretending to be
        # a fact (Subjective Goals §5.4).
        assert all(item.agreement.n_holdout > 0 for item in parsed.criteria)

    def test_an_uncalibrated_goal_refuses_a_report_rather_than_faking_one(
        self, database: Database, goals_root: Path
    ) -> None:
        from freeweight.services.export import ExportRefused, iter_goal_export

        goal = fork_starter(goals_root, "creative_voice")
        sync_goals(database, [goal])

        with pytest.raises(ExportRefused, match="no calibration report"):
            list(iter_goal_export(database, goal, document="calibration_report"))

    def test_the_goal_pack_export_validates_against_setspecs_own_reader(
        self, database: Database, goals_root: Path
    ) -> None:
        from setspec.goal.v1 import GoalPackIn

        from freeweight.services.export import iter_goal_export

        goal = fork_starter(goals_root, "summary_faithfulness")

        document = json.loads("".join(iter_goal_export(database, goal, document="goal_pack")))

        assert document["schema"] == "benchmark.goal_pack"
        parsed = GoalPackIn.model_validate(document["payload"])
        assert parsed.slug == "summary_faithfulness"
        assert parsed.unforked is True
        assert len(parsed.criteria) == len(goal.pack.criteria)


class TestTheReadingOrderTeaches:
    def test_deterministic_weight_rises_strictly_down_the_documented_order(self) -> None:
        """The lesson: the better you understand what you want, the less of it needs a judge."""
        shares = [pack.deterministic_weight for pack in list_starters()]

        assert [pack.key for pack in list_starters()] == list(READING_ORDER)
        assert shares == sorted(shares), shares
        assert len(set(shares)) == len(shares), "two packs share a deterministic share"
        assert shares[0] == pytest.approx(0.40, abs=0.01)
        assert shares[-1] == pytest.approx(0.90, abs=0.01)

    def test_every_starter_carries_its_own_description(self) -> None:
        assert set(CARRIES) == set(READING_ORDER)
        for pack in list_starters():
            assert pack.carries == CARRIES[pack.key]
            assert pack.intent, pack.key


class TestForkingIsNotDefaulting:
    def test_a_fork_that_is_unedited_is_badged_unforked(self, goals_root: Path) -> None:
        goal = fork_starter(goals_root, "creative_voice")

        assert goal.pack.unforked is True
        assert goal.pack.forked_from == "creative_voice"
        body = json.loads((goal.pack_path / "goal.json").read_text(encoding="utf-8"))
        assert body["unforked"] is True
        assert any(finding.code == "UNFORKED_STARTER" for finding in goal.findings), (
            "the lint does not say the pack is unedited"
        )

    def test_the_badge_reaches_the_goal_pack_export(self, goals_root: Path) -> None:
        from freeweight.services.export import goal_pack_payload

        goal = fork_starter(goals_root, "brand_voice")

        payload = goal_pack_payload(goal)

        assert payload["unforked"] is True
        assert payload["goal_hash"] == goal.goal_hash

    def test_every_starter_task_is_marked_as_starter_content(self, goals_root: Path) -> None:
        """Step 4's rule: starter tasks are explicitly labelled as things to replace."""
        goal = fork_starter(goals_root, "technical_explanation")

        assert goal.pack.tasks
        assert all(task.is_starter for task in goal.pack.tasks)

    def test_a_fork_can_be_named_and_two_forks_measure_the_same_thing(
        self, goals_root: Path
    ) -> None:
        """Two forks of one starter share a ``goal_hash`` and differ in their capability.

        Subjective Goals §2.2: the hash covers the *measurement-defining* content only. Renaming a
        goal must not separate a year of results, so two identically-defined rubrics hash the same
        however they are named — while their evidence is still emitted under ``user.<slug>``, which
        is what keeps two people's copies from being confused with each other.
        """
        first = fork_starter(goals_root, "creative_voice")
        second = fork_starter(goals_root, "creative_voice", slug="my_own_voice")

        assert first.slug == "creative_voice"
        assert second.slug == "my_own_voice"
        assert second.pack.forked_from == "creative_voice"
        assert first.goal_hash == second.goal_hash
        assert first.pack.capability_id != second.pack.capability_id

    def test_forking_the_same_slug_twice_is_refused(self, goals_root: Path) -> None:
        from freeweight.services.goals import GoalSlugCollision

        fork_starter(goals_root, "summary_faithfulness")

        with pytest.raises(GoalSlugCollision):
            fork_starter(goals_root, "summary_faithfulness")


class TestTheLookupIsSafe:
    def test_an_unknown_key_is_refused_by_name(self) -> None:
        with pytest.raises(StarterNotFound, match="creative_voice"):
            starter_directory("no_such_starter")

    @pytest.mark.parametrize("key", ["../secrets", "a/b", ".hidden", "/etc/passwd"])
    def test_a_traversal_attempt_never_reaches_the_filesystem(self, key: str) -> None:
        """Security standards §5: a path is validated before anything is opened."""
        from baseaicore import ValidationError

        with pytest.raises((ValidationError, StarterNotFound)):
            starter_directory(key)
