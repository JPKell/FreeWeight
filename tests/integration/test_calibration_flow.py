"""Calibration end to end: grade, partition, measure, gate — and never leak the holdout.

The phase's own test list, the clauses that need a database or a provider:

* **the holdout is provably never rendered into a judge prompt** — asserted by scanning every
  prompt the jury was actually sent for the holdout samples' content, not by reading the code;
* **the gate**: below threshold the run completes, the result is badged ``uncalibrated``, and
  ``capability_evidence`` has **no row** — the absence asserted directly, because "we emitted it
  quietly at the floor" is precisely the failure the gate exists to prevent;
* **``CALIBRATION_INSUFFICIENT`` is distinguished from a failed gate**, in code and in the payload;
* **the grade-distribution check fires on an all-4-and-5 calibration set**;
* **two calibration runs of the same goal, jury and grades produce identical partitions and
  identical agreement figures**;
* **moving one criterion from ``judge`` to ``rule`` measurably raises ``judge_validity_factor``**,
  and both values are shown.

Everything runs against :class:`~modelrack.testing.FakeProvider` with a **deterministic fake jury
whose bias is configurable**, which is how a "generous juror" becomes a test case rather than a
field report.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from baseaicore import ValidationError
from modelrack.providers.fake import FakeModel
from modelrack.testing import FakeGeneration, FakeProvider, FakeScript
from sqlalchemy import func, inspect, select, text

from freeweight.config import JudgeSettings
from freeweight.domain.calibration import CalibrationState
from freeweight.domain.goals.pack import Criterion
from freeweight.domain.jury import assemble_jury
from freeweight.domain.scorers.judged import JudgedCriterionResult, JurorVerdict, combine_verdicts
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.infrastructure.db.models_goals import CalibrationReport, CalibrationSample
from freeweight.services.calibration import (
    CalibrationInsufficient,
    GradeSubmission,
    add_samples,
    anchors_for,
    grading_progress,
    latest_outcome,
    record_grades,
    run_calibration,
)
from freeweight.services.database import MIGRATIONS_LOCATION, Database
from freeweight.services.goals import sync_goals, write_pack
from freeweight.services.jury import build_jury
from freeweight.services.prompts import load_pack

if TYPE_CHECKING:
    from modelrack import Provider

_ANCHORED = {
    "points": 5,
    "descriptors": {"5": "Wry and understated.", "3": "Flat reportage.", "1": "Earnest."},
}
# Twelve samples spanning the scale, so the partition has something to stratify.
_GRADES = [1, 2, 3, 4, 5, 1, 2, 3, 4, 5, 2, 4]


def _goal_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": "noir_voice",
        "name": "Noir voice",
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "created_by": "tester",
        "criteria": [
            {
                "key": "tells",
                "name": "No LLM tells",
                "rung": "rule",
                "weight": 0.5,
                "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
            },
            {
                "key": "wit",
                "name": "Dry wit",
                "rung": "judge",
                "weight": 0.5,
                "scale": _ANCHORED,
            },
        ],
        "judge": {"jury_size": 2, "repetitions": 1},
        "calibration": {"min_samples": 8, "target_samples": 12, "holdout_fraction": 0.4},
    }
    body.update(changes)
    return body


def _task_record() -> dict[str, Any]:
    return {
        "prompt_id": "goals.noir_voice.warehouse",
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": "One essay prompt from the author's own work.",
        "task": "goal.noir_voice",
        "capability": "creative_writing",
        "system": None,
        "template": "Write three paragraphs about the night the inventory did not add up.",
        "variables": {},
        "response": {"format": "text", "json_schema_ref": None, "expectations": []},
        "model_requirements": {
            "min_context_tokens": 2048,
            "requires_capabilities": [],
            "recommended_temperature": 0.8,
        },
        "metadata": {
            "author": "tester",
            "created_at": "2026-08-27T00:00:00Z",
            "changed_at": "2026-08-27T00:00:00Z",
            "change_reason": "First version.",
            "supersedes": None,
            "tags": ["goal"],
            "goal_task": {"key": "warehouse", "name": "Warehouse night"},
        },
    }


@dataclass(frozen=True)
class FakeJury:
    """A deterministic jury whose bias is configurable.

    The point of this double is that its *true* agreement with the author is known by
    construction: with ``bias=0`` it reproduces the author's grade exactly, with ``bias=1`` it is
    consistently one point generous, and with ``scatter`` it disagrees in a fixed, reproducible
    pattern. That is what makes "a generous juror" a test case rather than a field report.

    Attributes:
        truth: ``{sample content: the author's grade}``.
        bias: A constant offset added to every grade, clamped to the scale.
        scatter: An alternating offset applied to every other sample, for a noisy jury.
        jurors: How many jurors to report, so inter-juror agreement is computable.
    """

    truth: dict[str, int]
    bias: int = 0
    scatter: int = 0
    jurors: int = 2
    assembly: Any = field(
        default_factory=lambda: assemble_jury(["a", "b"], candidate=None, jury_size=2)
    )
    seen: list[str] = field(default_factory=list)
    anchors: dict[str, Any] = field(default_factory=dict)
    """Rebound by ``run_calibration`` to the partition's own anchors.

    Present so this double accepts the same rebinding the real service does — which is the
    mechanism that stops a caller's stale anchor mapping reaching the jury."""

    def with_anchors(self, anchors: Any) -> FakeJury:
        """Return a copy bound to the partition's own exemplars, as the real service does."""
        from dataclasses import replace

        return replace(self, anchors=dict(anchors))

    def judge_prompt_reference(self) -> dict[str, str]:
        """The judge prompt's identity, exactly as the real service reports it."""
        return {
            "prompt_id": "goals.judge.rubric",
            "prompt_version": "1.0.0",
            "prompt_sha256": "sha256:" + "ab" * 32,
        }

    def grade_all(
        self, criteria: Sequence[Criterion], response_text: str, case: Any
    ) -> list[JudgedCriterionResult]:
        """Grade every criterion, deterministically, from the author's own grade."""
        del case
        self.seen.append(response_text)
        base = self.truth.get(response_text, 3)
        offset = self.scatter if len(self.seen) % 2 == 0 else -self.scatter
        results: list[JudgedCriterionResult] = []
        for criterion in criteria:
            points = criterion.scale.points if criterion.scale else 5
            verdicts = [
                JurorVerdict(
                    juror_canonical_id=f"juror{index}",
                    juror_ordinal=index,
                    repetition=1,
                    grade=max(1, min(points, base + self.bias + offset + index * 0)),
                    rationale="because the rubric says so",
                )
                for index in range(self.jurors)
            ]
            results.append(combine_verdicts(criterion, verdicts))
        return results


@pytest.fixture
def database(tmp_path: Path) -> Any:
    """A migrated, empty database."""
    url = f"sqlite:///{tmp_path / 'calibration.sqlite3'}"
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


@pytest.fixture
def goal(goals_root: Path) -> Any:
    return write_pack(goals_root, goal=_goal_body(), tasks=[_task_record()])


def _sample_texts(count: int = 12) -> list[str]:
    return [f"Calibration sample number {index}, written by hand." for index in range(count)]


def _seed_grades(
    database: Any, goal: Any, *, grades: Sequence[int] | None = None
) -> dict[str, int]:
    """Add samples, grade them, and return ``{content: grade}``."""
    sync_goals(database, [goal])
    texts = _sample_texts(len(grades or _GRADES))
    ids = add_samples(database, goal, contents=[{"content": text} for text in texts])
    values = list(grades or _GRADES)
    record_grades(
        database,
        goal,
        [
            GradeSubmission(
                sample_id=sample_id, criterion_key="wit", grade=grade, note=f"note {grade}"
            )
            for sample_id, grade in zip(ids, values, strict=True)
        ],
        graded_by="tester",
    )
    return dict(zip(texts, values, strict=True))


class TestTheHoldoutIsNeverShownToTheJury:
    """Asserted by scanning what was actually sent, not by reading the code."""

    def test_no_holdout_sample_appears_in_any_judge_prompt(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        recorder = _RecordingProvider(
            FakeProvider(
                FakeScript(
                    models=(FakeModel(name="alpha"), FakeModel(name="beta")),
                    generations=(FakeGeneration(text=json.dumps({"grade": 3, "reason": "flat"})),),
                ),
                seed=7,
            )
        )
        jury = build_jury(
            cast("Provider", recorder),
            pack=goal.pack,
            library=load_pack(),
            settings=JudgeSettings(jury_size=2, repetitions=1),
            candidate_canonical_id="",
            available=recorder.canonical_ids(),
            allow_remote_provider=False,
            anchors=anchors_for(database, goal),
        )
        run_calibration(database, goal, jury=jury, graded_by="tester")

        with database.read() as session:
            holdout = [
                sample.content
                for sample in session.scalars(
                    select(CalibrationSample).where(CalibrationSample.partition == "holdout")
                )
            ]
            anchors = [
                sample.content
                for sample in session.scalars(
                    select(CalibrationSample).where(CalibrationSample.partition == "anchor")
                )
            ]
        assert holdout, "the partition produced no holdout to check"
        assert anchors, "the partition produced no anchors to compare against"

        # The *anchors* block of every prompt — everything before the answer under judgement.
        anchor_blocks = [prompt.split("ANSWER TO GRADE")[0] for prompt in recorder.prompts]
        assert anchor_blocks
        for content in holdout:
            for block in anchor_blocks:
                assert content not in block, (
                    "a held-out sample reached the judge prompt's exemplar block"
                )
        del truth

    def test_the_anchors_do_reach_it_which_is_what_they_are_for(
        self, database: Any, goal: Any
    ) -> None:
        _seed_grades(database, goal)
        recorder = _RecordingProvider(
            FakeProvider(
                FakeScript(
                    models=(FakeModel(name="alpha"), FakeModel(name="beta")),
                    generations=(FakeGeneration(text=json.dumps({"grade": 3, "reason": "flat"})),),
                ),
                seed=7,
            )
        )
        jury = build_jury(
            cast("Provider", recorder),
            pack=goal.pack,
            library=load_pack(),
            settings=JudgeSettings(jury_size=2, repetitions=1),
            candidate_canonical_id="",
            available=recorder.canonical_ids(),
            allow_remote_provider=False,
            anchors=anchors_for(database, goal),
        )
        # The anchors mapping is empty on the first pass — the partition has not been computed
        # yet, so nothing is labelled ``anchor``. Run once to establish it, then again with the
        # anchors it produced.
        run_calibration(database, goal, jury=jury, graded_by="tester")
        second = build_jury(
            cast("Provider", recorder),
            pack=goal.pack,
            library=load_pack(),
            settings=JudgeSettings(jury_size=2, repetitions=1),
            candidate_canonical_id="",
            available=recorder.canonical_ids(),
            allow_remote_provider=False,
            anchors=anchors_for(database, goal),
        )
        recorder.prompts.clear()
        run_calibration(database, goal, jury=second, graded_by="tester")
        with database.read() as session:
            anchors = [
                sample.content
                for sample in session.scalars(
                    select(CalibrationSample).where(CalibrationSample.partition == "anchor")
                )
            ]
        assert any(any(content in prompt for prompt in recorder.prompts) for content in anchors), (
            "no anchor reached the judge prompt, so the exemplars are not being rendered"
        )

    def test_the_anchors_mapping_reads_only_the_anchor_half(self, database: Any, goal: Any) -> None:
        _seed_grades(database, goal)
        jury = FakeJury(truth={})
        run_calibration(database, goal, jury=jury, graded_by="tester")
        with database.read() as session:
            holdout = {
                sample.content
                for sample in session.scalars(
                    select(CalibrationSample).where(CalibrationSample.partition == "holdout")
                )
            }
        exemplars = anchors_for(database, goal)["wit"]
        assert holdout
        assert not holdout & {item.content for item in exemplars}

    def test_the_jury_is_only_ever_shown_the_holdout_as_the_answer_under_judgement(
        self, database: Any, goal: Any
    ) -> None:
        _seed_grades(database, goal)
        jury = FakeJury(truth={})
        run_calibration(database, goal, jury=jury, graded_by="tester")
        with database.read() as session:
            holdout = {
                sample.content
                for sample in session.scalars(
                    select(CalibrationSample).where(CalibrationSample.partition == "holdout")
                )
            }
        assert set(jury.seen) == holdout


@dataclass
class _RecordingProvider:
    """A provider that records every prompt it is asked to generate from."""

    inner: FakeProvider
    prompts: list[str] = field(default_factory=list)

    def canonical_ids(self) -> list[str]:
        return sorted(descriptor.identity.canonical_id for descriptor in self.inner.list_models())

    def list_models(self, *, refresh: bool = False) -> Any:
        return self.inner.list_models(refresh=refresh)

    def resolve(self, reference: str, *, refresh: bool = False) -> Any:
        return self.inner.resolve(reference, refresh=refresh)

    def generate(self, request: Any) -> Any:
        self.prompts.extend(message.content for message in request.messages if message.content)
        return self.inner.generate(request)

    def capabilities(self) -> Any:
        return self.inner.capabilities()


class TestTheGate:
    def test_below_the_threshold_the_result_is_uncalibrated(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        # A jury that grades every sample 3 regardless: no relationship to the author at all.
        outcome = run_calibration(database, goal, jury=FakeJury(truth={}), graded_by="tester")
        assert outcome.verdict.state is CalibrationState.UNCALIBRATED
        assert outcome.verdict.passed is False
        del truth

    def test_and_capability_evidence_has_no_row(self, database: Any, goal: Any) -> None:
        # Asserted directly. "We emitted it quietly at the floor" is exactly the failure the gate
        # exists to prevent, so the absence is checked rather than inferred. Phase 11 created the
        # table, and the assertion means exactly what it meant when the table did not exist: a
        # goal below its gate has no row here. The end-to-end form — a run of an uncalibrated
        # goal, recomputed, still yields no row for `user.<slug>` *or* for the capability it
        # contributes to — is tests/contract/test_evidence_export.py's.
        _seed_grades(database, goal)
        run_calibration(database, goal, jury=FakeJury(truth={}), graded_by="tester")
        with database.read() as session:
            assert inspect(session.get_bind()).has_table("capability_evidence")
            count = session.scalar(text("SELECT COUNT(*) FROM capability_evidence"))
            assert count == 0

    def test_but_every_sample_is_still_inspectable(self, database: Any, goal: Any) -> None:
        _seed_grades(database, goal)
        outcome = run_calibration(database, goal, jury=FakeJury(truth={}), graded_by="tester")
        assert outcome.criteria
        assert outcome.criteria[0].result.n > 0
        assert outcome.criteria[0].lint, "an uncalibrated criterion must say why"

    def test_and_the_diagnostics_name_the_samples_where_they_diverged(
        self, database: Any, goal: Any
    ) -> None:
        truth = _seed_grades(database, goal)
        outcome = run_calibration(database, goal, jury=FakeJury(truth={}), graded_by="tester")
        divergences = outcome.criteria[0].disagreements
        assert divergences
        assert divergences[0].divergence >= divergences[-1].divergence
        assert divergences[0].author_note
        assert divergences[0].jury_rationale
        assert divergences[0].excerpt
        del truth

    def test_above_the_threshold_it_is_calibrated(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        outcome = run_calibration(database, goal, jury=FakeJury(truth=truth), graded_by="tester")
        assert outcome.verdict.state is CalibrationState.CALIBRATED
        assert outcome.verdict.passed is True
        assert outcome.criteria[0].result.kappa_w == pytest.approx(1.0)

    def test_the_threshold_is_the_goal_s_own(self, goals_root: Path, database: Any) -> None:
        strict = write_pack(
            goals_root,
            goal=_goal_body(
                slug="strict_voice",
                calibration={"min_samples": 8, "min_agreement": 0.99, "holdout_fraction": 0.4},
            ),
            tasks=[_task_record()],
        )
        truth = _seed_grades(database, strict)
        outcome = run_calibration(
            database, strict, jury=FakeJury(truth=truth, bias=1), graded_by="tester"
        )
        assert outcome.verdict.min_agreement == pytest.approx(0.99)
        assert outcome.verdict.state is CalibrationState.UNCALIBRATED


class TestInsufficientIsNotUncalibrated:
    def test_too_few_grades_raises_its_own_error(self, database: Any, goal: Any) -> None:
        _seed_grades(database, goal, grades=[1, 2, 3, 4])
        with pytest.raises(CalibrationInsufficient) as caught:
            run_calibration(database, goal, jury=FakeJury(truth={}), graded_by="tester")
        assert caught.value.code == "CALIBRATION_INSUFFICIENT"
        assert caught.value.details["remaining"] == 4  # noqa: PLR2004 — eight needed, four given

    def test_a_failed_gate_does_not_raise_at_all(self, database: Any, goal: Any) -> None:
        # It is a real and useful answer, so it comes back as a result rather than an exception.
        _seed_grades(database, goal)
        outcome = run_calibration(database, goal, jury=FakeJury(truth={}), graded_by="tester")
        assert outcome.verdict.state is CalibrationState.UNCALIBRATED

    def test_the_two_states_render_differently(self, database: Any, goal: Any) -> None:
        _seed_grades(database, goal)
        failed = run_calibration(
            database, goal, jury=FakeJury(truth={}), graded_by="tester"
        ).as_json()
        assert failed["calibration_state"] == "uncalibrated"
        assert failed["weighted_kappa_w"] is not None
        # And the insufficient case never produces a coefficient at all.
        progress = grading_progress(database, goal)
        assert progress.min_samples == 8  # noqa: PLR2004 — the policy minimum


class TestTheGradeDistributionCheck:
    def test_it_fires_on_an_all_four_and_five_set(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal, grades=[4, 5, 4, 5, 5, 4, 5, 4, 5, 5, 4, 3])
        outcome = run_calibration(database, goal, jury=FakeJury(truth=truth), graded_by="tester")
        assert any("weaker examples" in warning for warning in outcome.warnings)

    def test_a_set_with_no_variance_is_refused_outright(self, database: Any, goal: Any) -> None:
        # Different from a warning: with no variance there is nothing to agree about, and any
        # coefficient would be a division nobody should perform.
        _seed_grades(database, goal, grades=[4] * 12)
        with pytest.raises(ValidationError, match="nothing to agree about"):
            run_calibration(database, goal, jury=FakeJury(truth={}), graded_by="tester")

    def test_a_spread_set_produces_no_warning(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        outcome = run_calibration(database, goal, jury=FakeJury(truth=truth), graded_by="tester")
        assert not [warning for warning in outcome.warnings if "weaker examples" in warning]


class TestReproducibility:
    """Acceptance criterion 5: identical partitions and identical agreement figures."""

    def test_two_runs_produce_the_same_partition(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        run_calibration(database, goal, jury=FakeJury(truth=truth), graded_by="tester")
        with database.read() as session:
            first = {
                sample.id: sample.partition for sample in session.scalars(select(CalibrationSample))
            }
        run_calibration(database, goal, jury=FakeJury(truth=truth), graded_by="tester")
        with database.read() as session:
            second = {
                sample.id: sample.partition for sample in session.scalars(select(CalibrationSample))
            }
        assert first == second

    def test_and_the_same_agreement_figures(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        first = run_calibration(
            database, goal, jury=FakeJury(truth=truth, bias=1), graded_by="tester"
        )
        second = run_calibration(
            database, goal, jury=FakeJury(truth=truth, bias=1), graded_by="tester"
        )
        assert first.criteria[0].result.as_json() == second.criteria[0].result.as_json()
        assert first.verdict.weighted_kappa_w == second.verdict.weighted_kappa_w

    def test_the_partition_seed_is_recorded_on_every_sample(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        outcome = run_calibration(database, goal, jury=FakeJury(truth=truth), graded_by="tester")
        assert outcome.partition_seed == 0
        with database.read() as session:
            seeds = {sample.partition_seed for sample in session.scalars(select(CalibrationSample))}
        assert seeds == {0}

    def test_the_report_is_replaced_rather_than_accumulated(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        run_calibration(database, goal, jury=FakeJury(truth=truth), graded_by="tester")
        run_calibration(database, goal, jury=FakeJury(truth=truth), graded_by="tester")
        with database.read() as session:
            count = session.scalar(select(func.count()).select_from(CalibrationReport))
        # One goal-level row plus one per judged criterion.
        assert count == 2  # noqa: PLR2004 — the row count is the assertion


class TestAGenerousJury:
    """A juror whose bias is known by construction, so the statistics can be checked against it."""

    def test_one_point_generous_shows_up_as_bias_and_not_as_noise(
        self, database: Any, goal: Any
    ) -> None:
        truth = _seed_grades(database, goal)
        outcome = run_calibration(
            database, goal, jury=FakeJury(truth=truth, bias=1), graded_by="tester"
        )
        item = outcome.criteria[0]
        assert item.result.bias > 0
        # Still ranks the samples the same way — the top grade ties because a generous jury runs
        # off the top of the scale, which is what a real one does too.
        assert item.result.rho is not None
        assert item.result.rho > 0.9  # noqa: PLR2004 — "high", stated as a number
        assert item.result.kappa_w is not None
        assert item.result.kappa_w > 0.5  # noqa: PLR2004 — still ranks correctly

    def test_and_the_lint_names_the_offset_rather_than_the_noise(
        self, database: Any, goal: Any
    ) -> None:
        truth = _seed_grades(database, goal)
        outcome = run_calibration(
            database, goal, jury=FakeJury(truth=truth, bias=2), graded_by="tester"
        )
        assert "systematic offset" in outcome.criteria[0].lint

    def test_a_scattered_jury_shows_up_as_low_agreement_and_near_zero_bias(
        self, database: Any, goal: Any
    ) -> None:
        truth = _seed_grades(database, goal)
        outcome = run_calibration(
            database, goal, jury=FakeJury(truth=truth, scatter=2), graded_by="tester"
        )
        item = outcome.criteria[0]
        assert abs(item.result.bias) < 1.0
        assert item.result.mae > 0


class TestMechanizingACriterionRaisesValidity:
    """Acceptance criterion 3, end to end, with both values shown."""

    def test_moving_wit_from_judge_to_rule(self, goals_root: Path, database: Any) -> None:
        judged = write_pack(goals_root, goal=_goal_body(), tasks=[_task_record()])
        truth = _seed_grades(database, judged)
        before = run_calibration(
            database, judged, jury=FakeJury(truth=truth, bias=1), graded_by="tester"
        )

        mechanized_body = _goal_body(slug="noir_voice_rules")
        mechanized_body["criteria"] = [
            mechanized_body["criteria"][0],
            {
                "key": "wit",
                "name": "Dry wit",
                "rung": "rule",
                "weight": 0.5,
                "rule": {"type": "forbidden_phrases", "phrases": ["obviously", "of course"]},
            },
        ]
        mechanized_body.pop("judge", None)
        mechanized = write_pack(goals_root, goal=mechanized_body, tasks=[_task_record()])
        sync_goals(database, [mechanized])
        after = run_calibration(database, mechanized, jury=FakeJury(truth={}), graded_by="tester")
        assert after.verdict.judge_validity_factor > before.verdict.judge_validity_factor
        assert after.verdict.judge_validity_factor == 1.0
        assert after.verdict.state is CalibrationState.NOT_REQUIRED
        # Both values are shown, which is the half of the criterion that is about the UI.
        assert "judge_validity_factor" in before.as_json()
        assert "judge_validity_factor" in after.as_json()


class TestGradingIsResumable:
    def test_progress_reports_what_remains(self, database: Any, goal: Any) -> None:
        sync_goals(database, [goal])
        ids = add_samples(database, goal, contents=[{"content": text} for text in _sample_texts(4)])
        record_grades(
            database,
            goal,
            [GradeSubmission(sample_id=ids[0], criterion_key="wit", grade=4)],
            graded_by="tester",
        )
        progress = grading_progress(database, goal)
        assert progress.samples == 4  # noqa: PLR2004 — the count is the assertion
        assert progress.recorded == 1
        assert len(progress.remaining) == 3  # noqa: PLR2004 — the count is the assertion
        assert progress.complete is False

    def test_re_grading_replaces_rather_than_duplicating(self, database: Any, goal: Any) -> None:
        sync_goals(database, [goal])
        ids = add_samples(database, goal, contents=[{"content": "one sample"}])
        for grade in (2, 5):
            record_grades(
                database,
                goal,
                [GradeSubmission(sample_id=ids[0], criterion_key="wit", grade=grade)],
                graded_by="tester",
            )
        progress = grading_progress(database, goal)
        assert progress.recorded == 1
        assert progress.complete is True

    def test_identical_samples_are_added_once(self, database: Any, goal: Any) -> None:
        sync_goals(database, [goal])
        first = add_samples(database, goal, contents=[{"content": "the same text"}])
        second = add_samples(database, goal, contents=[{"content": "the same text"}])
        assert len(first) == 1
        assert second == []

    def test_a_grade_off_the_scale_is_refused(self, database: Any, goal: Any) -> None:
        sync_goals(database, [goal])
        ids = add_samples(database, goal, contents=[{"content": "one sample"}])
        with pytest.raises(ValidationError, match="outside criterion"):
            record_grades(
                database,
                goal,
                [GradeSubmission(sample_id=ids[0], criterion_key="wit", grade=9)],
                graded_by="tester",
            )

    def test_a_grade_on_an_unknown_criterion_is_refused(self, database: Any, goal: Any) -> None:
        sync_goals(database, [goal])
        ids = add_samples(database, goal, contents=[{"content": "one sample"}])
        with pytest.raises(ValidationError, match="no criterion"):
            record_grades(
                database,
                goal,
                [GradeSubmission(sample_id=ids[0], criterion_key="charm", grade=3)],
                graded_by="tester",
            )


class TestTheStoredReport:
    def test_it_reads_back_with_every_coefficient_and_its_n(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        written = run_calibration(
            database, goal, jury=FakeJury(truth=truth, bias=1), graded_by="tester"
        )
        read_back = latest_outcome(database, goal)
        assert read_back is not None
        assert read_back.verdict.weighted_kappa_w == pytest.approx(written.verdict.weighted_kappa_w)
        assert read_back.criteria[0].result.n == written.criteria[0].result.n
        assert read_back.criteria[0].result.n > 0

    def test_a_goal_that_was_never_calibrated_reads_back_as_nothing(
        self, database: Any, goal: Any
    ) -> None:
        sync_goals(database, [goal])
        assert latest_outcome(database, goal) is None

    def test_the_judge_set_travels_with_the_report(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        run_calibration(database, goal, jury=FakeJury(truth=truth), graded_by="tester")
        read_back = latest_outcome(database, goal)
        assert read_back is not None
        assert read_back.judge_set["prompt_id"] == "goals.judge.rubric"
        assert "jurors" in read_back.judge_set

    def test_every_rendered_coefficient_carries_its_n(self, database: Any, goal: Any) -> None:
        truth = _seed_grades(database, goal)
        body = run_calibration(
            database, goal, jury=FakeJury(truth=truth), graded_by="tester"
        ).as_json()
        assert body["n_holdout"] > 0
        for item in body["criteria"]:
            assert "kappa_w" in item
            assert item["n_holdout"] > 0


class TestTheAuthorsGradesSurviveEverything:
    """They are the most valuable rows in the database and the most expensive to reproduce."""

    def test_reloading_the_pack_does_not_destroy_them(self, database: Any, goal: Any) -> None:
        # ``sync_goals`` runs on every CLI command and every API request that touches a goal.
        # ``calibration_grades`` cascades from ``goal_criteria``, so a sync that recreated the
        # criteria would delete a whole grading sitting on the next command the author typed.
        _seed_grades(database, goal)
        before = grading_progress(database, goal).recorded
        for _ in range(3):
            sync_goals(database, [goal])
        assert grading_progress(database, goal).recorded == before
        assert before == len(_GRADES)

    def test_a_criterion_the_pack_no_longer_declares_takes_its_grades_with_it(
        self, database: Any, goal: Any, goals_root: Path
    ) -> None:
        # Which is correct: the measurement those grades were of no longer exists.
        from freeweight.services.goals import replace_pack

        _seed_grades(database, goal)
        body = _goal_body()
        body["criteria"] = [body["criteria"][0] | {"weight": 1.0}]
        body.pop("judge", None)
        _previous, current = replace_pack(
            goals_root, slug="noir_voice", goal=body, tasks=[_task_record()]
        )
        sync_goals(database, [current])
        assert grading_progress(database, current).recorded == 0

    def test_re_syncing_keeps_the_criterion_row_identity(self, database: Any, goal: Any) -> None:
        from freeweight.infrastructure.db.models_goals import GoalCriterion

        sync_goals(database, [goal])
        with database.read() as session:
            first = {row.key: row.id for row in session.scalars(select(GoalCriterion))}
        sync_goals(database, [goal])
        with database.read() as session:
            second = {row.key: row.id for row in session.scalars(select(GoalCriterion))}
        assert first == second


class TestAJudgedGoalThatCouldNotBeGradedIsUncalibrated:
    """Not ``not_required`` — that would say the goal never needed a jury."""

    def test_a_jury_that_grades_nothing_leaves_the_goal_uncalibrated(
        self, database: Any, goal: Any
    ) -> None:
        _seed_grades(database, goal)
        outcome = run_calibration(database, goal, jury=_SilentJury(), graded_by="tester")
        assert outcome.verdict.state is CalibrationState.UNCALIBRATED
        assert outcome.verdict.weighted_kappa_w is None
        assert outcome.criteria == ()

    def test_and_its_validity_factor_reflects_the_unmeasured_weight(
        self, database: Any, goal: Any
    ) -> None:
        # Half the rubric is rules at 1.0 and half is a judged criterion nothing could measure,
        # which is worth zero — not 1.0, which is what an empty mapping would have implied.
        _seed_grades(database, goal)
        outcome = run_calibration(database, goal, jury=_SilentJury(), graded_by="tester")
        assert outcome.verdict.judge_validity_factor == pytest.approx(0.5)


@dataclass
class _SilentJury:
    """A jury every one of whose members refuses. Assembled, reachable, and useless."""

    assembly: Any = field(
        default_factory=lambda: assemble_jury(["a", "b"], candidate=None, jury_size=2)
    )
    anchors: dict[str, Any] = field(default_factory=dict)

    def with_anchors(self, anchors: Any) -> _SilentJury:
        from dataclasses import replace

        return replace(self, anchors=dict(anchors))

    def judge_prompt_reference(self) -> dict[str, str]:
        return {
            "prompt_id": "goals.judge.rubric",
            "prompt_version": "1.0.0",
            "prompt_sha256": "sha256:" + "ab" * 32,
        }

    def grade_all(
        self, criteria: Sequence[Criterion], response_text: str, case: Any
    ) -> list[JudgedCriterionResult]:
        del response_text, case
        return [
            combine_verdicts(
                criterion,
                [JurorVerdict("a", 0, 1, refused_reason="protocol_error")],
            )
            for criterion in criteria
        ]


def test_the_goal_models_register_the_tables_their_foreign_keys_point_at() -> None:
    """Importing the calibration service alone must be enough to write through it.

    SQLAlchemy resolves a foreign key's target table by *name*, at flush time. Three tables here
    point at ``samples`` and ``models``, which live in sibling model modules — so a process that
    imported only this one used to fail its first write with ``NoReferencedTableError``, which is
    the worst possible moment to find out. Asserted in a subprocess because this test session has
    already imported everything.
    """
    import subprocess
    import sys

    script = (
        "import freeweight.services.calibration as _;"
        "from freeweight.infrastructure.db.base import Base;"
        "names = set(Base.metadata.tables);"
        "assert 'samples' in names, names;"
        "assert 'models' in names, names;"
        "assert 'calibration_samples' in names, names;"
        "print('ok')"
    )
    result = subprocess.run(  # noqa: S603 — this interpreter, an argument list, no shell
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
