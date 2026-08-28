"""Contract: what FreeWeight writes, read back through SetSpec's own models.

API standards §7 makes a producer responsible for four things, and a test that only checked its
own field names would verify none of them:

1. **A writer emits what the schema declares, and nothing else.** Every payload goes out through
   its strict outbound model (``extra="forbid"``), so a field FreeWeight invented is a failure
   here rather than a surprise in a consumer.
2. **A reader accepts it.** Each document is parsed back through the *inbound* model — the
   preserving one a consumer would use — because "we can write it" and "someone else can read it"
   are different claims.
3. **A reader preserves unknown fields on round trip** (rule 4), so an older reader does not strip
   a newer writer's additions.
4. **An unsupported measurement is the string ``"unsupported"``** (rule 5, ADR-0016 §4) — never
   ``null``, never ``0``, never an absent key.

These run in CI's own `contracts` job. The evidence contracts
(``capability.evidence``, ``benchmark.evidence_bundle``) are Phase 11's and are stubbed beside
this file; the three schemas FreeWeight emits *today* are covered here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION, Database

pytestmark = pytest.mark.contract

_GOAL_SLUG = "contract_voice"


def _goal_body() -> dict[str, Any]:
    return {
        "slug": _GOAL_SLUG,
        "name": "A goal for the contract suite",
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "intent": "Exists so the exported pack has something to declare.",
        "created_by": "tester",
        "criteria": [
            {
                "key": "no_tells",
                "name": "No LLM tells",
                "rung": "rule",
                "weight": 0.6,
                "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
            },
            {
                "key": "wit",
                "name": "Dry wit",
                "rung": "judge",
                "weight": 0.4,
                "scale": {
                    "points": 5,
                    "descriptors": {
                        "5": "Wry and understated.",
                        "3": "Occasional flashes.",
                        "1": "Earnest throughout.",
                    },
                },
            },
        ],
        "judge": {"jury_size": 2, "repetitions": 1},
        "calibration": {"min_samples": 8, "target_samples": 12, "holdout_fraction": 0.4},
    }


def _task_record() -> dict[str, Any]:
    return {
        "prompt_id": f"goals.{_GOAL_SLUG}.one",
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": "One task.",
        "task": f"goal.{_GOAL_SLUG}",
        "capability": "creative_writing",
        "system": None,
        "template": "Write three paragraphs about a warehouse at night.",
        "variables": {},
        "response": {"format": "text", "json_schema_ref": None, "expectations": []},
        "model_requirements": {
            "min_context_tokens": 2048,
            "requires_capabilities": [],
            "recommended_temperature": 0.8,
        },
        "metadata": {
            "author": "tester",
            "created_at": "2026-08-28T00:00:00Z",
            "changed_at": "2026-08-28T00:00:00Z",
            "change_reason": "First version.",
            "supersedes": None,
            "tags": ["goal"],
            "goal_task": {"key": "one", "name": "Warehouse night"},
        },
    }


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    """A migrated, empty database."""
    url = f"sqlite:///{tmp_path / 'contract.sqlite3'}"
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
def goal(tmp_path: Path) -> Any:
    """One written, loadable goal pack."""
    from freeweight.services.goals import write_pack

    root = tmp_path / "goals"
    root.mkdir()
    return write_pack(root, goal=_goal_body(), tasks=[_task_record()])


class TestTheGoalPackContract:
    """``benchmark.goal_pack`` — the rubric a consumer reads to decide comparability."""

    def test_it_is_written_through_the_strict_outbound_model(self, goal: Any) -> None:
        """A field FreeWeight invented would be refused here, not carried to a consumer."""
        from setspec.goal.v1 import GoalPackOut

        from freeweight.services.export import goal_pack_payload

        payload = GoalPackOut.model_validate(goal_pack_payload(goal))

        assert payload.slug == _GOAL_SLUG
        assert payload.goal_hash == goal.goal_hash

    def test_a_consumer_reads_it_back(self, database: Database, goal: Any) -> None:
        from setspec.goal.v1 import GoalPackIn

        from freeweight.services.export import iter_goal_export

        document = json.loads("".join(iter_goal_export(database, goal, document="goal_pack")))
        parsed = GoalPackIn.model_validate(document["payload"])

        assert document["schema"] == "benchmark.goal_pack"
        assert document["schema_version"] == "1.0"
        assert document["generator"]["name"] == "freeweight"
        assert [item.key for item in parsed.criteria] == ["no_tells", "wit"]
        assert [item.key for item in parsed.tasks] == ["one"]

    def test_a_reader_preserves_a_field_this_build_does_not_know(self, goal: Any) -> None:
        """API standards §7 rule 4: an older reader must not strip a newer writer's addition."""
        from setspec.goal.v1 import GoalPackIn

        from freeweight.services.export import goal_pack_payload

        payload = goal_pack_payload(goal) | {"a_field_from_the_future": {"nested": 1}}

        parsed = GoalPackIn.model_validate(payload)
        round_tripped = parsed.model_dump()

        assert round_tripped["a_field_from_the_future"] == {"nested": 1}

    def test_the_strict_writer_refuses_that_same_field(self, goal: Any) -> None:
        """Rule 5's other half: a *writer* never emits what the schema does not declare."""
        from pydantic import ValidationError as PydanticValidationError
        from setspec.goal.v1 import GoalPackOut

        from freeweight.services.export import goal_pack_payload

        payload = goal_pack_payload(goal) | {"a_field_from_the_future": 1}

        with pytest.raises(PydanticValidationError):
            GoalPackOut.model_validate(payload)


class TestTheCalibrationReportContract:
    """``benchmark.calibration_report`` — the agreement figures, each with its ``n_holdout``."""

    def test_an_uncalibrated_goal_refuses_rather_than_emitting_an_empty_report(
        self, database: Database, goal: Any
    ) -> None:
        """ "Never calibrated" and "calibrated to nothing" are different answers."""
        from freeweight.services.export import ExportRefused, iter_goal_export

        with pytest.raises(ExportRefused, match="no calibration report"):
            list(iter_goal_export(database, goal, document="calibration_report"))

    def test_a_calibrated_goal_is_read_back_by_a_consumer(
        self, database: Database, goal: Any
    ) -> None:
        from setspec.goal.v1 import CalibrationReportIn

        from freeweight.services.export import iter_goal_export

        _calibrate(database, goal)

        document = json.loads(
            "".join(iter_goal_export(database, goal, document="calibration_report"))
        )
        parsed = CalibrationReportIn.model_validate(document["payload"])

        assert document["schema"] == "benchmark.calibration_report"
        assert parsed.goal_slug == _GOAL_SLUG
        assert parsed.goal_hash == goal.goal_hash
        assert parsed.criteria, "a calibrated report with no criterion says nothing"

    def test_every_coefficient_carries_its_n_holdout(self, database: Database, goal: Any) -> None:
        """Subjective Goals §5.4.

        A ``kappa_w`` without its ``n`` is a number pretending to be a fact.
        """
        from setspec.goal.v1 import CalibrationReportIn

        from freeweight.services.export import iter_goal_export

        _calibrate(database, goal)
        document = json.loads(
            "".join(iter_goal_export(database, goal, document="calibration_report"))
        )
        parsed = CalibrationReportIn.model_validate(document["payload"])

        assert parsed.n_holdout > 0
        for item in parsed.criteria:
            assert item.agreement.n_holdout > 0, item.criterion_key


def _calibrate(database: Database, goal: Any) -> None:
    """Grade this goal's samples and measure agreement with a deterministic jury."""
    from dataclasses import dataclass, field, replace

    from freeweight.domain.jury import assemble_jury
    from freeweight.domain.scorers.judged import JurorVerdict, combine_verdicts
    from freeweight.services.calibration import (
        GradeSubmission,
        add_samples,
        record_grades,
        run_calibration,
    )
    from freeweight.services.goals import sync_goals

    @dataclass(frozen=True)
    class DeterministicJury:
        """A jury that grades from the sample's own index, so the figures are reproducible."""

        grades: dict[str, int]
        assembly: Any = field(
            default_factory=lambda: assemble_jury(["a", "b"], candidate=None, jury_size=2)
        )
        anchors: dict[str, Any] = field(default_factory=dict)

        def with_anchors(self, anchors: Any) -> Any:  # noqa: ANN401 — the protocol's own type
            return replace(self, anchors=dict(anchors))

        def judge_prompt_reference(self) -> dict[str, str]:
            return {
                "prompt_id": "goals.judge.rubric",
                "prompt_version": "1.0.0",
                "prompt_sha256": "sha256:" + "ab" * 32,
            }

        def grade_all(self, criteria: Any, response_text: str, case: Any) -> list[Any]:  # noqa: ANN401
            del case
            grade = self.grades.get(response_text, 3)
            return [
                combine_verdicts(
                    criterion,
                    [
                        JurorVerdict(
                            juror_canonical_id=f"juror{index}",
                            juror_ordinal=index,
                            repetition=1,
                            grade=grade,
                            rationale="deterministic",
                        )
                        for index in range(2)
                    ],
                )
                for criterion in criteria
            ]

    sync_goals(database, [goal])
    texts = [f"Calibration sample number {index}." for index in range(12)]
    ids = add_samples(database, goal, contents=[{"content": text} for text in texts])
    author = {text: (index % 5) + 1 for index, text in enumerate(texts)}
    record_grades(
        database,
        goal,
        [
            GradeSubmission(
                sample_id=sample_id, criterion_key="wit", grade=author[text], note=f"note {text}"
            )
            for sample_id, text in zip(ids, texts, strict=True)
        ],
        graded_by="tester",
    )
    run_calibration(database, goal, jury=DeterministicJury(grades=author), graded_by="tester")


class TestTheExportContract:
    """``freeweight.export`` — FreeWeight's own results document, embedding the SetSpec summary."""

    def test_the_schema_name_is_in_freeweights_own_namespace(self) -> None:
        """ADR-0035 §6: a schema this build emits is SetSpec's or FreeWeight's, never a squat.

        Tested rather than documented because prose did not hold it — this document shipped as
        ``benchmark.export``, inside the shared contract package's namespace, and nothing noticed
        until the ADR was written.
        """
        from setspec.envelope import SUPPORTED_SCHEMAS

        from freeweight.services.export import EXPORT_SCHEMA

        assert EXPORT_SCHEMA.startswith("freeweight.")
        assert EXPORT_SCHEMA not in SUPPORTED_SCHEMAS

    def test_every_shipped_metric_key_satisfies_the_setspec_contract(self) -> None:
        """Catch a key SetSpec would reject at *build* time, not halfway through a stream.

        A metric key that fails validation surfaces inside ``iter_export``, after the response's
        headers have gone out, as "response already started" — which names neither the run nor the
        key. This walks every shipped manifest instead, so the failure arrives with the suite that
        caused it. It found ``criterion.<key>`` the first time it ran.
        """
        import json
        import re

        from setspec.metrics import MetricValueFields

        pattern = MetricValueFields.model_fields["metric_key"].metadata[-1].pattern
        root = Path(__file__).resolve().parents[2] / "src" / "freeweight" / "benchmarks"
        offenders: list[str] = []
        for path in sorted(root.glob("*/manifest.json")):
            body = json.loads(path.read_text(encoding="utf-8"))
            for metric in body.get("metrics", ()):
                if not re.match(pattern, str(metric["key"])):
                    offenders.append(f"{body['key']}:{metric['key']}")

        assert not offenders, f"metric keys SetSpec would reject: {offenders}"

    def test_the_goal_suites_namespaced_keys_are_legal(self) -> None:
        """The producer that forced the pattern to allow dots, asserted directly."""
        import re

        from setspec.metrics import MetricValueFields

        pattern = MetricValueFields.model_fields["metric_key"].metadata[-1].pattern
        assert re.match(pattern, "criterion.house_voice")
        assert not re.match(pattern, "criterion.House Voice")

    def test_every_schema_name_in_the_source_is_shared_or_ours(self) -> None:
        """The other half of the rule, over the source rather than over a hand-kept list.

        Every ``schema="…"`` and ``"schema": …`` literal under ``src/`` must name something
        SetSpec defines, something in ``freeweight.``, or the SSE ``event.envelope`` that
        API standards §8 defines for a stream rather than a document.
        """
        import re

        from setspec.envelope import SUPPORTED_SCHEMAS

        source_root = Path(__file__).resolve().parents[2] / "src" / "freeweight"
        pattern = re.compile(
            r"""["']schema["']\s*:\s*["']([a-z_.]+)["']|schema=["']([a-z_.]+)["']"""
        )
        found: set[str] = set()
        for path in source_root.rglob("*.py"):
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                found.add(match.group(1) or match.group(2))

        assert found, "the scan found no schema literals at all — the pattern has rotted"
        for schema in found - {"event.envelope"}:
            assert schema in SUPPORTED_SCHEMAS or schema.startswith("freeweight."), (
                f"{schema!r} is neither a SetSpec schema nor in FreeWeight's own namespace "
                f"(ADR-0035 §1)"
            )

    def test_each_run_embeds_a_setspec_run_summary_a_consumer_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from setspec.benchmark.v1 import BenchmarkRunSummaryIn

        from freeweight.services.export import (
            EXPORT_SCHEMA,
            ExportScope,
            ExportSelection,
            iter_export,
        )

        database, run_id = _completed_run(tmp_path, monkeypatch)
        try:
            document = json.loads(
                "".join(
                    iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=run_id))
                )
            )
        finally:
            database.close()

        assert document["schema"] == EXPORT_SCHEMA
        summary = document["payload"]["runs"][0]["summary"]
        parsed = BenchmarkRunSummaryIn.model_validate(summary)

        assert parsed.suite.suite_key == "native.echo"
        assert parsed.machine_fingerprint
        assert parsed.reproducibility.reproducibility_fingerprint

    def test_an_unsupported_measurement_is_the_word_and_never_null_or_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0016 §4 on the wire, in both the SetSpec summary and FreeWeight's own metric rows."""
        from freeweight.services.export import ExportScope, ExportSelection, iter_export

        database, run_id = _completed_run(tmp_path, monkeypatch)
        try:
            document = json.loads(
                "".join(
                    iter_export(database, ExportSelection(scope=ExportScope.RUN, selector=run_id))
                )
            )
        finally:
            database.close()

        run = document["payload"]["runs"][0]
        unsupported = [row for row in run["metrics"] if row["value"] == "unsupported"]
        assert unsupported, "the fake provider exposes no GPU; something should be unsupported"
        assert all(row["value"] is not None for row in run["metrics"])
        assert all(row["value"] != 0 for row in unsupported)
        for metric in run["summary"]["aggregate_metrics"]:
            assert metric["value"] is not None
            if metric["value"] == "unsupported":
                assert metric["sample_count"] == 0, "an unsupported metric has no samples"

    def test_the_document_declares_a_major_a_reader_can_reject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """API standards §7 rule 3: readers reject an unsupported major, naming both versions."""
        from baseaicore import ValidationError

        from freeweight.services.export import EXPORT_SCHEMA, read_export

        del tmp_path, monkeypatch
        forged = json.dumps(
            {
                "schema": EXPORT_SCHEMA,
                "schema_version": "99.0",
                "generated_at": "2026-08-28T00:00:00.000Z",
                "generator": {"name": "freeweight", "version": "0.0.0"},
                "payload": {"runs": []},
            }
        )

        with pytest.raises(ValidationError, match="99.0"):
            read_export(forged)


def _completed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Database, str]:
    """Execute one ``native.echo`` run against the fake provider and return its database and ID."""
    import time

    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from freeweight.cli.main import app as cli_app
    from freeweight.config import load_settings
    from freeweight.web.app import create_app

    path = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__IDLE_GPU_THRESHOLD_PERCENT", "0")
    engine = create_engine_for(f"sqlite:///{path}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    terminal = {"completed", "failed", "cancelled", "interrupted"}
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as client:
        assert CliRunner().invoke(cli_app, ["models", "refresh"]).exit_code == 0
        created = client.post(
            "/api/v1/runs", json={"model": "fake-model:8b-q8_0", "suite": "native.echo"}
        )
        assert created.status_code == 201, created.text
        run_id = str(created.json()["id"])
        deadline = time.monotonic() + 60.0
        while client.get(f"/api/v1/runs/{run_id}").json()["status"] not in terminal:
            assert time.monotonic() < deadline, "the run never finished"
            time.sleep(0.05)
    return Database.from_url(f"sqlite:///{path}"), run_id
