"""Contract: FreeWeight's evidence export, end to end, including the M3 exit condition.

Roadmap §1's M3 exit condition is the last test in this file: *an evidence bundle exported by
FreeWeight is consumed by a ``setspec``-only harness — no FreeWeight import, no database access —
including a calibrated ``user.*`` goal record.* The harness is a subprocess that imports ``setspec``
and asserts that no ``freeweight`` module was ever loaded.

Everything else here is Phase 11's own test list, in the order the development plan states it:
contributors named, absence never zero, re-aggregation never fresher, incremental bundles,
the gate withholding both halves, ``contributes_to`` emitting both halves, hard separations,
the sixth factor at exactly 1.0 across the catalogue, an older SetSpec degrading rather than
failing, and staleness badging.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from baseaicore import utc_now
from fastapi.testclient import TestClient
from modelrack.testing import FakeGeneration, FakeScript
from setspec import load_envelope
from setspec.capability.v1 import EvidenceBundleIn

from freeweight.config import EvidenceSettings, ExecutionSettings, Settings, load_settings
from freeweight.domain.confidence import ConfidencePolicy, Environment
from freeweight.domain.goals.pack import Criterion
from freeweight.domain.jury import assemble_jury
from freeweight.domain.scorers.judged import JudgedCriterionResult, JurorVerdict, combine_verdicts
from freeweight.infrastructure.db.models_runs import BenchmarkSuite, MetricValue, Run
from freeweight.infrastructure.db.repositories.evidence import EvidenceRepository
from freeweight.infrastructure.db.repositories.runs import RunRepository
from freeweight.services import evidence as evidence_service
from freeweight.services.calibration import (
    GradeSubmission,
    add_samples,
    record_grades,
    run_calibration,
)
from freeweight.services.evidence import (
    EvidenceQuery,
    evidence_bundle,
    load_capability_mapping,
    query_evidence,
    recompute_evidence,
    staleness_of,
)
from freeweight.services.goals import load_goals, sync_goals, write_pack
from freeweight.services.runs import ExecutionConfig, build_registry, create_run, get_run
from freeweight.services.scheduler import RunScheduler
from freeweight.web.app import create_app

pytestmark = pytest.mark.contract

WEIGHTS = """
version = "test"

[capabilities.reliability]
sources = [
  { suite = "native.echo", metric_key = "harness_roundtrip_success", weight = 1.0 },
]
"""

_ANSWER = (
    "I counted the pallets twice. The second count matched the first, which was the problem.\n\n"
    "Nobody had signed the log since Tuesday. I walked the north aisle and found the same "
    "nothing, arranged differently."
)
_ANCHORED = {
    "points": 5,
    "descriptors": {"5": "Wry and understated.", "3": "Flat reportage.", "1": "Earnest."},
}
_GRADES = [1, 2, 3, 4, 5, 1, 2, 3, 4, 5, 2, 4]


def _goal_body(slug: str, *, contributes_to: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": slug,
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
            {"key": "wit", "name": "Dry wit", "rung": "judge", "weight": 0.5, "scale": _ANCHORED},
        ],
        "judge": {"jury_size": 2, "repetitions": 1},
        "calibration": {"min_samples": 8, "target_samples": 12, "holdout_fraction": 0.4},
    }
    if contributes_to:
        body["contributes_to"] = contributes_to
    return body


def _task_record(slug: str) -> dict[str, Any]:
    return {
        "prompt_id": f"goals.{slug}.warehouse",
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": "One essay prompt from the author's own work.",
        "task": f"goal.{slug}",
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


@dataclass
class StubJury:
    """A deterministic jury that serves both calibration and a run, with a known agreement.

    With ``truth`` it reproduces the author's grade exactly, so a calibration passes its gate with
    ``kappa_w = 1``; with ``truth={}`` every sample is a 3 and the calibration fails it. The same
    object judges the run's samples, so the report's jury and the run's jury are one instrument.
    """

    truth: dict[str, int]
    assembly: Any = field(
        default_factory=lambda: assemble_jury(["juror-a", "juror-b"], candidate=None, jury_size=2)
    )
    anchors: dict[str, Any] = field(default_factory=dict)

    def with_anchors(self, anchors: Any) -> StubJury:
        from dataclasses import replace

        return replace(self, anchors=dict(anchors))

    def judge_prompt_reference(self) -> dict[str, str]:
        return {
            "prompt_id": "goals.judge.rubric",
            "prompt_version": "1.0.0",
            "prompt_sha256": "sha256:" + "ab" * 32,
        }

    def refusal_detail(self) -> dict[str, Any]:
        return {**self.assembly.as_json(), **self.judge_prompt_reference()}

    def _verdicts(self, criterion: Criterion, response_text: str) -> list[JurorVerdict]:
        points = criterion.scale.points if criterion.scale else 5
        grade = max(1, min(points, self.truth.get(response_text, 3)))
        return [
            JurorVerdict(
                juror_canonical_id=f"juror-{index}",
                juror_ordinal=index,
                repetition=1,
                grade=grade,
                rationale="because the rubric says so",
            )
            for index in range(2)
        ]

    def grade_all(
        self, criteria: Sequence[Criterion], response_text: str, case: Any
    ) -> list[JudgedCriterionResult]:
        del case
        return [
            combine_verdicts(criterion, self._verdicts(criterion, response_text))
            for criterion in criteria
        ]

    def score_judged(
        self, *, criteria: Sequence[Criterion], response_text: str, case: Any
    ) -> list[Any]:
        return [result.outcome for result in self.grade_all(criteria, response_text, case)]


def _execution() -> ExecutionConfig:
    return ExecutionConfig.resolve(
        ExecutionSettings(
            warmup_repetitions=0,
            cooldown_seconds=0,
            idle_gpu_threshold_percent=0,
            randomize_case_order=False,
        ),
        measured_repetitions=1,
    )


@pytest.fixture
def evidence_settings(tmp_path: Path) -> EvidenceSettings:
    weights = tmp_path / "weights.toml"
    weights.write_text(WEIGHTS, encoding="utf-8")
    return EvidenceSettings(capability_weights_path=str(weights))


@pytest.fixture
def goals_root(tmp_path: Path) -> Path:
    root = tmp_path / "goals"
    root.mkdir()
    return root


def _run(environment: Any, suite_key: str, *, settings: Settings) -> str:
    summary = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key=suite_key,
        execution=_execution(),
    )
    RunScheduler(
        environment.database, environment.provider, registry=environment.registry, settings=settings
    ).run_once()
    detail = get_run(environment.database, summary.id)
    assert detail.run.status == "completed", detail.run
    return str(summary.id)


@pytest.fixture
def echo_evidence(
    run_environment: Callable[..., Any], evidence_settings: EvidenceSettings
) -> tuple[Any, str, Settings]:
    """An echo run whose completion wrote one ``reliability`` record."""
    environment = run_environment()
    settings = Settings(evidence=evidence_settings)
    run_id = _run(environment, "native.echo", settings=settings)
    return environment, run_id, settings


def _calibrate(database: Any, goal: Any, *, agree: bool) -> StubJury:
    """Seed twelve graded samples and calibrate with a jury that agrees, or does not."""
    sync_goals(database, [goal])
    texts = [f"Calibration sample number {index}, written by hand." for index in range(12)]
    ids = add_samples(database, goal, contents=[{"content": text} for text in texts])
    record_grades(
        database,
        goal,
        [
            GradeSubmission(sample_id=sample_id, criterion_key="wit", grade=grade, note="n")
            for sample_id, grade in zip(ids, _GRADES, strict=True)
        ],
        graded_by="tester",
    )
    truth = dict(zip(texts, _GRADES, strict=True)) if agree else {}
    jury = StubJury(truth=truth)
    outcome = run_calibration(database, goal, jury=jury, graded_by="tester")
    assert outcome.verdict.passed is agree, outcome.verdict
    return jury


def _goal_run(
    run_environment: Callable[..., Any],
    goals_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    slug: str,
    contributes_to: str | None,
    agree: bool,
) -> tuple[Any, str, Settings]:
    """Write, calibrate and run one judged goal; return the environment and the run id."""
    import freeweight.services.jury as jury_module

    goal = write_pack(
        goals_root, goal=_goal_body(slug, contributes_to=contributes_to), tasks=[_task_record(slug)]
    )
    goals = load_goals(goals_root)
    environment = run_environment(
        script=FakeScript(generations=(FakeGeneration(text=_ANSWER),)),
        registry=build_registry(goals=goals),
    )
    jury = _calibrate(environment.database, goal, agree=agree)
    monkeypatch.setattr(jury_module, "build_jury", lambda *a, **k: jury)
    settings = Settings()
    run_id = _run(environment, f"goal.{slug}", settings=settings)
    return environment, run_id, settings


class TestEvidenceNamesItsContributors:
    def test_a_completed_run_yields_a_record_that_explains_itself(
        self, echo_evidence: tuple[Any, str, Settings]
    ) -> None:
        environment, run_id, _settings = echo_evidence
        page = query_evidence(environment.database, EvidenceQuery())
        assert [record.capability_id for record in page.records] == ["reliability"]
        record = page.records[0]
        assert record.source_run_ids == (run_id,)
        assert [m.metric_key for m in record.contributing_metrics] == [
            "native.echo.harness_roundtrip_success"
        ]
        assert record.contributing_metrics[0].weight == 1.0
        assert record.contributing_metrics[0].sample_count == record.sample_count > 0
        assert record.judge_validity_factor == 1.0
        assert set(record.factors) >= {
            "sample_factor",
            "consistency_factor",
            "freshness_factor",
            "environment_factor",
            "identity_factor",
            "judge_validity_factor",
        }
        assert record.benchmark_versions == {"native.echo": "1.0.0"}

    def test_a_customised_mapping_derives_its_own_policy_version(
        self, echo_evidence: tuple[Any, str, Settings]
    ) -> None:
        environment, _run_id, _settings = echo_evidence
        record = query_evidence(environment.database, EvidenceQuery()).records[0]
        assert record.policy_version.startswith("test+")
        assert record.policy["n_target"] == 30


class TestAbsenceIsNeverZero:
    def test_a_suite_that_feeds_no_capability_produces_no_record(
        self, run_environment: Callable[..., Any]
    ) -> None:
        """Under the shipped mapping ``native.echo`` feeds nothing, so nothing is written."""
        environment = run_environment()
        _run(environment, "native.echo", settings=Settings())
        page = query_evidence(environment.database, EvidenceQuery())
        assert page.records == ()
        report = recompute_evidence(environment.database)
        assert report.emitted == ()
        assert report.withheld == ()

    def test_the_shipped_mapping_scores_a_real_suite(
        self, run_environment: Callable[..., Any]
    ) -> None:
        environment = run_environment(
            script=FakeScript(generations=(FakeGeneration(word_count=12),))
        )
        _run(environment, "native.instruction_following", settings=Settings())
        page = query_evidence(environment.database, EvidenceQuery())
        assert [record.capability_id for record in page.records] == ["instruction_following"]
        record = page.records[0]
        assert record.policy_version == "1.0"
        assert {m.metric_key for m in record.contributing_metrics} <= {
            "native.instruction_following.strict_prompt_accuracy",
            "native.instruction_following.instruction_level_accuracy",
            "native.instruction_following.loose_prompt_accuracy",
        }
        assert "tool_use" not in [r.capability_id for r in page.records]


class TestReaggregationIsNeverFresher:
    def test_recomputing_unchanged_runs_does_not_raise_confidence(
        self, echo_evidence: tuple[Any, str, Settings]
    ) -> None:
        """The test that makes ADR-0017 mean what it says (ADR-0022 §2, spec §20 6a)."""
        environment, _run_id, settings = echo_evidence
        first = query_evidence(environment.database, EvidenceQuery()).records[0]
        later = first.computed_at + timedelta(days=40)
        report = recompute_evidence(environment.database, settings=settings.evidence, now=later)
        assert len(report.emitted) == 1
        second = query_evidence(environment.database, EvidenceQuery()).records[0]
        assert second.measured_at == first.measured_at
        assert second.computed_at == later
        assert second.confidence < first.confidence
        assert second.factors["freshness_factor"] == pytest.approx(
            first.factors["freshness_factor"] * 0.5 ** (40 / 90), rel=1e-6
        )


class TestIncrementalBundles:
    def test_a_full_export_is_complete_and_a_since_export_is_not(
        self, echo_evidence: tuple[Any, str, Settings]
    ) -> None:
        environment, _run_id, settings = echo_evidence
        first = json.loads(evidence_bundle(environment.database, EvidenceQuery()))
        assert first["payload"]["complete"] is True
        assert len(first["payload"]["evidence"]) == 1

        generated_at = load_envelope(json.dumps(first), expect="benchmark.evidence_bundle")
        since = generated_at.generated_at
        incremental = json.loads(evidence_bundle(environment.database, EvidenceQuery(since=since)))
        assert incremental["payload"]["complete"] is False
        assert incremental["payload"]["evidence"] == []

        recompute_evidence(
            environment.database, settings=settings.evidence, now=since + timedelta(seconds=5)
        )
        refreshed = json.loads(evidence_bundle(environment.database, EvidenceQuery(since=since)))
        assert refreshed["payload"]["complete"] is False
        assert len(refreshed["payload"]["evidence"]) == 1

    def test_a_filtered_bundle_is_never_complete(
        self, echo_evidence: tuple[Any, str, Settings]
    ) -> None:
        environment, _run_id, _settings = echo_evidence
        filtered = json.loads(
            evidence_bundle(environment.database, EvidenceQuery(capability="reliability"))
        )
        assert filtered["payload"]["complete"] is False
        assert len(filtered["payload"]["evidence"]) == 1


class TestTheGateWithholdsBothHalves:
    def test_a_goal_below_the_gate_contributes_no_row_anywhere(
        self,
        run_environment: Callable[..., Any],
        goals_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing for ``user.<slug>`` and nothing for ``contributes_to`` — the forgotten half."""
        environment, _run_id, settings = _goal_run(
            run_environment,
            goals_root,
            monkeypatch,
            slug="noir_voice",
            contributes_to="creative_writing",
            agree=False,
        )
        page = query_evidence(environment.database, EvidenceQuery())
        assert page.records == ()
        report = recompute_evidence(environment.database, settings=settings.evidence)
        assert report.emitted == ()
        withheld = {item.capability_id: item for item in report.withheld}
        assert set(withheld) == {"user.noir_voice", "creative_writing"}
        assert withheld["user.noir_voice"].code == "GOAL_UNCALIBRATED"
        assert withheld["creative_writing"].goal_slug == "noir_voice"
        with environment.database.read() as session:
            assert EvidenceRepository().list_all(session) == []


class TestContributesToEmitsTwice:
    def test_a_calibrated_goal_appears_as_itself_and_inside_the_shipped_capability(
        self,
        run_environment: Callable[..., Any],
        goals_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        environment, run_id, _settings = _goal_run(
            run_environment,
            goals_root,
            monkeypatch,
            slug="noir_voice",
            contributes_to="creative_writing",
            agree=True,
        )
        records = {
            record.capability_id: record
            for record in query_evidence(environment.database, EvidenceQuery()).records
        }
        assert set(records) == {"user.noir_voice", "creative_writing"}

        own = records["user.noir_voice"]
        assert own.goal_hash is not None
        assert own.goal_slug == "noir_voice"
        assert own.source_run_ids == (run_id,)
        assert own.calibration is not None and own.calibration["n_holdout"] >= 1
        assert own.judge_set is not None and own.judge_set["jurors"] == ["juror-a", "juror-b"]
        assert 0.05 <= own.judge_validity_factor < 1.0
        assert own.score_method_mix is not None
        assert own.score_method_mix["rule"] == pytest.approx(0.5)
        assert own.score_method_mix["judge"] == pytest.approx(0.5)
        assert {m.metric_key for m in own.contributing_metrics} == {
            "criterion.tells",
            "criterion.wit",
        }
        assert own.vocabulary_version == "1.1"

        blended = records["creative_writing"]
        assert [m.metric_key for m in blended.contributing_metrics] == [
            "goal.noir_voice.composite_score"
        ]
        assert blended.goal_hash == own.goal_hash, "the blend keeps the goal's identity too"
        assert blended.judge_validity_factor == pytest.approx(own.judge_validity_factor)
        assert blended.score == pytest.approx(own.score)

    def test_the_wire_forms_satisfy_setspecs_goal_group_rules(
        self,
        run_environment: Callable[..., Any],
        goals_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        environment, _run_id, _settings = _goal_run(
            run_environment,
            goals_root,
            monkeypatch,
            slug="noir_voice",
            contributes_to="creative_writing",
            agree=True,
        )
        for record in query_evidence(environment.database, EvidenceQuery()).records:
            payload = record.wire_payload().model_dump()
            assert payload["uncalibrated"] is False
            assert payload["goal_hash"]


class TestHardSeparations:
    def test_two_versions_of_one_suite_never_merge(
        self, echo_evidence: tuple[Any, str, Settings]
    ) -> None:
        """A run under an older suite version is kept apart and named, not averaged in."""
        environment, run_id, settings = echo_evidence
        with environment.database.write() as session:
            run = RunRepository().get_by_id(session, run_id)
            assert run is not None and run.completed_at is not None
            suite = session.get(BenchmarkSuite, run.suite_id)
            assert suite is not None
            older = BenchmarkSuite(
                key=suite.key,
                name=suite.name,
                version="0.0.1",
                category=suite.category,
                runner=suite.runner,
                manifest_hash="sha256:older",
                prompt_subset_hash=suite.prompt_subset_hash,
                dataset_hashes_json={},
            )
            session.add(older)
            session.flush()
            stale = Run(
                machine_id=run.machine_id,
                model_id=run.model_id,
                model_descriptor_id=run.model_descriptor_id,
                runtime_profile_id=run.runtime_profile_id,
                suite_id=older.id,
                status="completed",
                created_at=run.created_at - timedelta(days=1),
                started_at=run.created_at - timedelta(days=1),
                completed_at=run.completed_at - timedelta(days=1),
                reproducibility_fingerprint="older",
                fingerprint_document_json=run.fingerprint_document_json,
                provider_kind=run.provider_kind,
                provider_version=run.provider_version,
            )
            session.add(stale)
            session.flush()
            session.add(
                MetricValue(
                    run_id=stale.id,
                    metric_key="harness_roundtrip_success",
                    numeric_value=0.0,
                    unit="ratio",
                    aggregation="mean",
                    higher_is_better=True,
                    sample_count=50,
                    excluded_count=0,
                )
            )
            stale_id = stale.id
        report = recompute_evidence(environment.database, settings=settings.evidence)
        record = report.emitted[0]
        assert record.benchmark_versions == {"native.echo": "1.0.0"}
        assert stale_id not in record.source_run_ids
        assert record.score == pytest.approx(1.0)
        assert any("0.0.1" in note for note in report.separated)


class TestTheSixthFactorChangedNoExistingNumber:
    def test_judge_validity_is_exactly_one_for_every_native_and_external_suite(self) -> None:
        """A regression test over the whole catalogue, through the real aggregation path."""
        import json as _json

        manifests = [
            _json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(
                (Path(__file__).resolve().parents[2] / "src" / "freeweight" / "benchmarks").glob(
                    "*/manifest.json"
                )
            )
        ]
        assert manifests
        assert all(body["runner"] in {"native", "external"} for body in manifests)
        now = utc_now()
        measurements = tuple(
            evidence_service._SuiteMeasurement(
                suite_key=body["key"],
                suite_version=body["version"],
                runner=body["runner"],
                dataset_hashes={},
                prompt_subset_hash=None,
                run_ids=(f"run-{index}",),
                completed_at=now,
                environment=Environment(provider_kind="fake", provider_version="1.0"),
                model_descriptor_id=None,
                metrics={
                    metric["metric_key"]: evidence_service._MergedMetric(
                        metric_key=metric["metric_key"],
                        value=0.5,
                        unit=metric["unit"],
                        higher_is_better=bool(metric["higher_is_better"]),
                        sample_count=10,
                        excluded_count=0,
                        dispersion=0.1,
                    )
                    for metric in body.get("metrics", ())
                },
            )
            for index, body in enumerate(manifests)
        )
        rows = evidence_service._SubjectRuns(
            subject=evidence_service.Subject("m", "p", "mc"),
            model=SimpleNamespace(
                id="m",
                canonical_id="fake/x@sha256:abcdefabcdef",
                identity_confidence="digest",
                provider_kind="fake",
                provider_model_name="x",
                artifact_digest="sha256:" + "ab" * 32,
                last_seen_at=now,
            ),
            machine=SimpleNamespace(id="mc", machine_fingerprint="fingerprint"),
            profile=SimpleNamespace(id="p", profile_hash="profile"),
            descriptor=None,
            measurements=measurements,
            separated=(),
            current_environment=None,
        )
        mapping = load_capability_mapping()
        records, withheld, notes = evidence_service._records_for(
            rows,
            mapping=mapping,
            policy=ConfidencePolicy(),
            policy_version="1.0",
            settings=EvidenceSettings(),
            now=now,
        )
        assert not withheld and not notes
        assert {record.capability_id for record in records} == set(mapping.capabilities)
        for record in records:
            assert record.judge_validity_factor == 1.0, record.capability_id
            assert record.goal_hash is None
            assert record.factors["judge_validity_factor"] == 1.0


class TestAnOlderSetSpecDegradesRatherThanFailing:
    def test_a_build_predating_vocabulary_1_1_reads_the_bundle_and_ignores_user_records(
        self,
        run_environment: Callable[..., Any],
        goals_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ADR-0009's forward-compatibility rule, asserted end to end rather than assumed."""
        import setspec.vocabulary as vocabulary

        environment, _run_id, _settings = _goal_run(
            run_environment,
            goals_root,
            monkeypatch,
            slug="noir_voice",
            contributes_to="creative_writing",
            agree=True,
        )
        text = evidence_bundle(environment.database, EvidenceQuery())

        # An older build: vocabulary 1.0, no `user` root.
        monkeypatch.setattr(vocabulary, "CAPABILITIES", vocabulary.CAPABILITIES - {"user"})
        monkeypatch.setattr(vocabulary, "CAPABILITY_VOCABULARY_VERSION", "1.0")

        envelope = load_envelope(text, expect="benchmark.evidence_bundle")
        bundle = EvidenceBundleIn.model_validate(envelope.payload)
        known = [
            record
            for record in bundle.evidence
            if vocabulary.is_known_capability(record.capability_id)
        ]
        ignored = [
            record
            for record in bundle.evidence
            if not vocabulary.is_known_capability(record.capability_id)
        ]
        assert [record.capability_id for record in known] == ["creative_writing"]
        assert [record.capability_id for record in ignored] == ["user.noir_voice"]


class TestTheApiSurface:
    @pytest.fixture
    def client(
        self,
        echo_evidence: tuple[Any, str, Settings],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> Iterator[tuple[TestClient, Any]]:
        environment, _run_id, _settings = echo_evidence
        monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", environment.database_url)
        monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
        monkeypatch.setenv(
            "FREEWEIGHT_EVIDENCE__CAPABILITY_WEIGHTS_PATH", str(tmp_path / "weights.toml")
        )
        loaded = load_settings(config_path=tmp_path / "missing.toml")
        with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
            yield test_client, environment

    def test_the_collection_is_a_page_of_setspec_envelopes(
        self, client: tuple[TestClient, Any]
    ) -> None:
        test_client, _environment = client
        body = test_client.get("/api/v1/evidence").json()
        assert body["page"]["has_more"] is False
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["schema"] == "capability.evidence"
        assert item["schema_version"] == "1.0"
        assert item["payload"]["capability_id"] == "reliability"

    def test_the_export_is_one_bundle_envelope_with_no_wrapper(
        self, client: tuple[TestClient, Any]
    ) -> None:
        test_client, _environment = client
        response = test_client.get("/api/v1/evidence/export")
        assert response.status_code == 200
        assert response.headers["content-disposition"].startswith("attachment")
        body = response.json()
        assert body["schema"] == "benchmark.evidence_bundle"
        assert "items" not in body
        assert body["payload"]["complete"] is True
        since = test_client.get(
            "/api/v1/evidence/export", params={"since": body["generated_at"]}
        ).json()
        assert since["payload"]["complete"] is False

    def test_filters_and_refusals(self, client: tuple[TestClient, Any]) -> None:
        test_client, _environment = client
        assert (
            test_client.get("/api/v1/evidence", params={"capability": "tool_use"}).json()["items"]
            == []
        )
        assert (
            test_client.get("/api/v1/evidence", params={"model": "no-such-model"}).status_code
            == 404
        )
        assert (
            test_client.get("/api/v1/evidence/export", params={"since": "yesterday"}).status_code
            == 400
        )

    def test_staleness_badging_appears_when_freshness_drops(
        self, client: tuple[TestClient, Any]
    ) -> None:
        """ADR-0017's staleness surface, on the page and in the service."""
        test_client, environment = client
        fresh = test_client.get("/evidence").text
        assert "fresh" in fresh and 'status-interrupted">stale' not in fresh

        with environment.database.write() as session:
            for row in EvidenceRepository().list_all(session):
                row.measured_at = row.measured_at - timedelta(days=400)
                row.computed_at = row.computed_at - timedelta(days=400)
        aged = test_client.get("/evidence").text
        assert 'status-interrupted">stale' in aged
        assert "days ago" in aged

        record = query_evidence(environment.database, EvidenceQuery()).records[0]
        verdict = staleness_of(record, now=utc_now())
        assert verdict.stale is True
        assert verdict.freshness == pytest.approx(0.3)
        assert verdict.reasons

        only = test_client.get("/evidence", params={"stale": "only"}).text
        assert 'status-interrupted">stale' in only

    def test_drift_alone_badges_stale(self, client: tuple[TestClient, Any]) -> None:
        _test_client, environment = client
        record = query_evidence(environment.database, EvidenceQuery()).records[0]
        from dataclasses import replace

        drifted = replace(record, factors={**record.factors, "drift": ["gpu_driver_version"]})
        assert staleness_of(drifted, now=utc_now()).stale is True
        assert staleness_of(record, now=utc_now()).stale is False


HARNESS = """
import json
import sys

from setspec import load_envelope
from setspec.capability.v1 import EvidenceBundleIn
from setspec.vocabulary import is_known_capability

text = open(sys.argv[1], encoding="utf-8").read()
envelope = load_envelope(text, expect="benchmark.evidence_bundle")
bundle = EvidenceBundleIn.model_validate(envelope.payload)
records = list(bundle.evidence)
assert records, "an empty bundle proves nothing"
assert all(is_known_capability(record.capability_id) for record in records)
goal = [record for record in records if record.capability_id.startswith("user.")]
assert len(goal) == 1, [record.capability_id for record in records]
record = goal[0]
assert record.goal_hash, "a user.* record carries the goal's identity"
assert record.calibration is not None and record.calibration.n_holdout >= 1
assert record.judge_set is not None and record.judge_set.jurors
assert 0.05 <= record.judge_validity_factor <= 1.0
assert record.uncalibrated is False
loaded = [name for name in sys.modules if name == "freeweight" or name.startswith("freeweight.")]
assert not loaded, loaded
print(
    json.dumps(
        {
            "records": len(records),
            "capabilities": sorted(item.capability_id for item in records),
            "goal": record.capability_id,
            "kappa_w": record.calibration.kappa_w,
            "n_holdout": record.calibration.n_holdout,
            "judge_validity_factor": record.judge_validity_factor,
            "complete": bundle.complete,
        }
    )
)
"""


class TestTheM3ExitCondition:
    def test_a_setspec_only_harness_consumes_the_bundle_including_a_calibrated_goal(
        self,
        run_environment: Callable[..., Any],
        goals_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Roadmap §1, M3: no FreeWeight import, no database access, a calibrated ``user.*``."""
        environment, _run_id, _settings = _goal_run(
            run_environment,
            goals_root,
            monkeypatch,
            slug="noir_voice",
            contributes_to="creative_writing",
            agree=True,
        )
        exported = tmp_path / "evidence.json"
        exported.write_text(
            evidence_bundle(environment.database, EvidenceQuery()), encoding="utf-8"
        )

        completed = subprocess.run(  # noqa: S603 — our own interpreter, our own script
            [sys.executable, "-I", "-c", HARNESS, str(exported)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        outcome = json.loads(completed.stdout)
        assert outcome["complete"] is True
        assert outcome["goal"] == "user.noir_voice"
        assert outcome["capabilities"] == ["creative_writing", "user.noir_voice"]
        assert outcome["kappa_w"] == pytest.approx(1.0)
        assert outcome["n_holdout"] >= 1
        assert 0.05 <= outcome["judge_validity_factor"] < 1.0


class TestTheCli:
    """``freeweight evidence show|export`` are the same two service functions as the API."""

    @pytest.fixture
    def cli_env(
        self,
        echo_evidence: tuple[Any, str, Settings],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> tuple[Any, Path]:
        environment, _run_id, _settings = echo_evidence
        monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", environment.database_url)
        monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
        monkeypatch.setenv(
            "FREEWEIGHT_EVIDENCE__CAPABILITY_WEIGHTS_PATH", str(tmp_path / "weights.toml")
        )
        return environment, tmp_path

    def test_show_prints_the_records_with_their_factors(self, cli_env: tuple[Any, Path]) -> None:
        from typer.testing import CliRunner

        from freeweight.cli.main import app as cli_app

        result = CliRunner().invoke(cli_app, ["evidence", "show"])
        assert result.exit_code == 0, result.output
        assert "reliability" in result.output
        assert "factors:" in result.output
        assert "native.echo.harness_roundtrip_success" in result.output
        assert "fresh" in result.output

    def test_show_json_is_the_apis_collection_envelope(self, cli_env: tuple[Any, Path]) -> None:
        from typer.testing import CliRunner

        from freeweight.cli.main import app as cli_app

        environment, _tmp_path = cli_env
        result = CliRunner().invoke(cli_app, ["evidence", "show", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.stdout)
        assert set(body) == {"items", "page"}
        assert body["items"][0]["schema"] == "capability.evidence"
        served = query_evidence(environment.database, EvidenceQuery()).as_json()
        assert body["items"][0]["payload"] == served["items"][0]["payload"]

    def test_show_recompute_reports_what_it_did_on_stderr(self, cli_env: tuple[Any, Path]) -> None:
        from typer.testing import CliRunner

        from freeweight.cli.main import app as cli_app

        result = CliRunner().invoke(cli_app, ["evidence", "show", "--recompute", "--json"])
        assert result.exit_code == 0, result.output
        assert "Recomputed evidence for 1 subject(s)" in result.stderr
        assert set(json.loads(result.stdout)) == {"items", "page"}

    def test_export_writes_the_bundle_the_endpoint_returns(self, cli_env: tuple[Any, Path]) -> None:
        from typer.testing import CliRunner

        from freeweight.cli.main import app as cli_app

        _environment, tmp_path = cli_env
        target = tmp_path / "evidence.json"
        result = CliRunner().invoke(cli_app, ["evidence", "export", "--output", str(target)])
        assert result.exit_code == 0, result.output
        envelope = load_envelope(
            target.read_text(encoding="utf-8"), expect="benchmark.evidence_bundle"
        )
        bundle = EvidenceBundleIn.model_validate(envelope.payload)
        assert bundle.complete is True
        assert [record.capability_id for record in bundle.evidence] == ["reliability"]

        piped = CliRunner().invoke(cli_app, ["evidence", "export"])
        assert piped.exit_code == 0
        assert json.loads(piped.stdout)["schema"] == "benchmark.evidence_bundle"

    def test_a_bad_filter_is_a_usage_error(self, cli_env: tuple[Any, Path]) -> None:
        from typer.testing import CliRunner

        from freeweight.cli.main import app as cli_app

        runner = CliRunner()
        assert runner.invoke(cli_app, ["evidence", "export", "--since", "yesterday"]).exit_code == 2
        assert runner.invoke(cli_app, ["evidence", "show", "--model", "no-such"]).exit_code == 2

    def test_version_names_the_schemas_this_build_writes(self) -> None:
        from typer.testing import CliRunner

        from freeweight.cli.main import app as cli_app

        result = CliRunner().invoke(cli_app, ["version", "--json"])
        assert result.exit_code == 0
        schemas = json.loads(result.stdout)["schemas"]
        assert schemas["capability.evidence"] == "1.0"
        assert schemas["benchmark.evidence_bundle"] == "1.0"
        assert "capability.evidence 1.0" in CliRunner().invoke(cli_app, ["--version"]).output
