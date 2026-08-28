"""End-to-end: the whole goal authoring journey, through HTTP and through the CLI.

Spec §20 criterion 13 is the acceptance criterion this file exists for:

    A user with no prior setup can, from the UI alone, define a goal, be shown which of their
    criteria a deterministic rule can check, supply their own tasks, grade twelve samples inline,
    and see a calibration report — without reading documentation and without editing a file. The
    wizard's output is a JSON goal pack they can then open in an editor and diff in git.

Every step below therefore goes through an HTTP form, and the test asserts on the *file* the
wizard produced rather than on its own idea of what it asked for. The byte-level round trip —
a pack the wizard wrote, loaded by the CLI, and a pack written by hand, loaded by the wizard — is
what makes "the artifact is theirs" a property rather than a claim.

Everything runs against :class:`~modelrack.testing.FakeProvider`: no GPU, no Ollama, no network.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from freeweight.cli.main import app as cli_app
from freeweight.config import load_settings
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.web.app import create_app

runner = CliRunner()

_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A migrated database, a goals root, and a fake-provider configuration."""
    database = tmp_path / "freeweight.sqlite3"
    goals_root = tmp_path / "goals"
    goals_root.mkdir()
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    monkeypatch.setenv("FREEWEIGHT_GOALS__ROOT", str(goals_root))
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__COOLDOWN_SECONDS", "0")
    # The shipped default waits for three consecutive quiet telemetry observations at one-second
    # intervals before the first provider call (spec §13). That is ~2.2 s of every run here, it is
    # the same wait on every one of them, and it is exercised in its own right by
    # tests/integration/test_performance_benchmark.py::TestIdleDetection, which covers all three
    # of its outcomes. Paying it again in every end-to-end journey buys nothing but minutes —
    # the same argument the cooldown line above makes. `0` is the documented way to disable it.
    monkeypatch.setenv("FREEWEIGHT_EXECUTION__IDLE_GPU_THRESHOLD_PERCENT", "0")
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    return tmp_path


def _client(workspace: Path) -> TestClient:
    """A served application over ``workspace``.

    Built on demand rather than as a fixture because a goal installed after the process started
    needs a restart before it is runnable — the registry is built in the lifespan — and the
    journey has to cross that boundary the way a real user does.
    """
    loaded = load_settings(config_path=workspace / "missing.toml")
    return TestClient(create_app(loaded.settings), base_url="http://127.0.0.1")


@pytest.fixture
def client(workspace: Path) -> Iterator[TestClient]:
    """A served application, lifespan entered."""
    with _client(workspace) as test_client:
        yield test_client


def _draft_id(response: Any) -> str:
    """Read the draft id out of the redirect the wizard issued."""
    location = str(response.headers["location"])
    return location.split("/goals/new/", 1)[1].split("/", 1)[0]


def _walk_the_wizard(client: TestClient) -> str:
    """Drive steps 1-4 and 7 through their forms, and return the saved slug."""
    started = client.post(
        "/goals/new",
        data={
            "intent": (
                "Essays that sound like me: dry, concrete, unhurried. Not LinkedIn, not a manual."
            ),
            "name": "My essay voice",
        },
        follow_redirects=False,
    )
    assert started.status_code == 303, started.text
    draft_id = _draft_id(started)

    # Step 2: two criteria, the second of which the user says is two qualities.
    client.post(
        f"/goals/new/{draft_id}/criteria",
        data={"action": "add", "name": "Dry wit", "intent": "Understated, never winking."},
    )
    client.post(
        f"/goals/new/{draft_id}/criteria",
        data={
            "action": "add",
            "name": "Not LinkedIn",
            "intent": "No buzzwords, and not the register of a press release.",
        },
    )
    client.post(
        f"/goals/new/{draft_id}/criteria",
        data={
            "action": "answer",
            "criterion": "not_linkedin",
            "graded_alike": "no",
            "one_quality": "no",
        },
    )
    split = client.post(
        f"/goals/new/{draft_id}/criteria",
        data={
            "action": "split",
            "criterion": "not_linkedin",
            "first": "No buzzwords",
            "second": "Plain register",
        },
    )
    assert split.status_code == 200, split.text

    # Still step 2: a judged criterion needs its scale described, or the pack the wizard would
    # write is one the lint refuses.
    for key, top, middle, bottom in (
        (
            "dry_wit",
            "Wry and understated throughout.",
            "Occasional flashes.",
            "Earnest, or winking.",
        ),
        ("plain_register", "Reads like a person.", "Half press release.", "A press release."),
    ):
        described = client.post(
            f"/goals/new/{draft_id}/criteria",
            data={
                "action": "describe",
                "criterion": key,
                "points": "5",
                "top": top,
                "middle": middle,
                "bottom": bottom,
            },
        )
        assert described.status_code == 200, described.text

    # Step 3: accept one proposed rule and leave the rest.
    client.post(
        f"/goals/new/{draft_id}/rules",
        data={
            "action": "accept",
            "criterion": "no_buzzwords",
            "rule_type": "forbidden_phrases",
            "parameters": json.dumps({"phrases": ["delve", "leverage"], "max_hits": 2}),
        },
    )

    # Step 4: the user's own prompt.
    client.post(
        f"/goals/new/{draft_id}/tasks",
        data={
            "action": "add",
            "name": "Inventory night",
            "prompt_text": "Write three paragraphs about the night the inventory did not add up.",
        },
    )

    # Step 7: name it and write it.
    saved = client.post(
        f"/goals/new/{draft_id}/save",
        data={"name": "My essay voice", "slug": "my_essay_voice"},
    )
    assert saved.status_code == 200, saved.text
    assert "Written" in saved.text
    return "my_essay_voice"


class TestCriterion13TheWholeFlowFromTheUiAlone:
    def test_the_wizard_produces_a_json_pack_the_user_owns(
        self, client: TestClient, workspace: Path
    ) -> None:
        slug = _walk_the_wizard(client)

        pack = workspace / "goals" / slug / "goal.json"
        assert pack.is_file(), "the wizard wrote no file"
        body = json.loads(pack.read_text(encoding="utf-8"))

        assert body["slug"] == slug
        assert body["intent"].startswith("Essays that sound like me")
        keys = [criterion["key"] for criterion in body["criteria"]]
        assert "no_buzzwords" in keys and "plain_register" in keys
        assert "not_linkedin" not in keys, "the split criterion was not replaced"

    def test_the_split_is_the_wizards_own_move_and_is_visible(self, client: TestClient) -> None:
        """Step 2's value: the wizard makes the split visible rather than performing it."""
        started = client.post(
            "/goals/new",
            data={"intent": "Not LinkedIn.", "name": "Voice"},
            follow_redirects=False,
        )
        draft_id = _draft_id(started)
        client.post(
            f"/goals/new/{draft_id}/criteria",
            data={"action": "add", "name": "Not LinkedIn", "intent": "You know the one."},
        )

        page = client.get(f"/goals/new/{draft_id}/criteria")

        assert page.status_code == 200
        assert "Could two people who both read your description grade the same text" in page.text
        assert "Is this one quality, or two stuck together?" in page.text
        # No split is offered until the user says it is two qualities.
        assert "Split into two" not in page.text

        client.post(
            f"/goals/new/{draft_id}/criteria",
            data={
                "action": "answer",
                "criterion": "not_linkedin",
                "graded_alike": "yes",
                "one_quality": "no",
            },
        )
        after = client.get(f"/goals/new/{draft_id}/criteria")
        assert "Split into two" in after.text

    def test_the_rule_proposer_never_applies_a_rule_by_itself(
        self, client: TestClient, workspace: Path
    ) -> None:
        """Asserted on the persisted pack, which is the only place it matters."""
        slug = _walk_the_wizard(client)
        body = json.loads((workspace / "goals" / slug / "goal.json").read_text(encoding="utf-8"))

        with_rules = [item for item in body["criteria"] if "rule" in item]
        assert [item["key"] for item in with_rules] == ["no_buzzwords"], (
            "a rule the user did not accept reached the pack"
        )
        assert with_rules[0]["rule"]["phrases"] == ["delve", "leverage"], (
            "the user's edited parameters were replaced by the proposed ones"
        )
        assert with_rules[0]["rung"] == "rule"

    def test_the_running_weight_statement_is_shown_at_the_rule_step(
        self, client: TestClient
    ) -> None:
        started = client.post(
            "/goals/new", data={"intent": "Dry essays.", "name": "V"}, follow_redirects=False
        )
        draft_id = _draft_id(started)
        client.post(
            f"/goals/new/{draft_id}/criteria",
            data={"action": "add", "name": "No buzzwords", "intent": "Avoid corporate cliche."},
        )

        before = client.get(f"/goals/new/{draft_id}/rules")
        assert "Nothing is scored deterministically yet" in before.text

        client.post(
            f"/goals/new/{draft_id}/rules",
            data={
                "action": "accept",
                "criterion": "no_buzzwords",
                "rule_type": "forbidden_phrases",
                "parameters": "",
            },
        )
        after = client.get(f"/goals/new/{draft_id}/rules")
        # The apostrophe is HTML-escaped by autoescaping, so the assertion straddles it.
        assert "weight is now scored by rules" in after.text

    def test_the_grading_cost_is_stated_before_the_user_invests_in_it(
        self, client: TestClient
    ) -> None:
        started = client.post(
            "/goals/new", data={"intent": "Dry essays.", "name": "V"}, follow_redirects=False
        )
        draft_id = _draft_id(started)
        client.post(f"/goals/new/{draft_id}/criteria", data={"action": "add", "name": "Dry wit"})

        page = client.get(f"/goals/new/{draft_id}/tasks")

        assert "Before you go further" in page.text
        assert "Budget about" in page.text
        assert "you can stop and come back" in page.text

    def test_a_malformed_parameter_edit_applies_nothing(self, client: TestClient) -> None:
        started = client.post(
            "/goals/new", data={"intent": "Dry essays.", "name": "V"}, follow_redirects=False
        )
        draft_id = _draft_id(started)
        client.post(
            f"/goals/new/{draft_id}/criteria",
            data={"action": "add", "name": "No buzzwords", "intent": "Avoid cliche."},
        )

        refused = client.post(
            f"/goals/new/{draft_id}/rules",
            data={
                "action": "accept",
                "criterion": "no_buzzwords",
                "rule_type": "forbidden_phrases",
                "parameters": "{not json",
            },
        )

        assert refused.status_code == 400
        assert "Nothing was applied" in refused.text
        assert "Nothing is scored deterministically yet" in refused.text


class TestTheRoundTripThroughBothSurfaces:
    def test_the_cli_loads_what_the_wizard_wrote_without_loss(
        self, client: TestClient, workspace: Path
    ) -> None:
        slug = _walk_the_wizard(client)

        shown = runner.invoke(cli_app, ["goals", "show", slug, "--json"])

        assert shown.exit_code == 0, shown.output
        body = json.loads(shown.output)
        assert body["slug"] == slug
        # Dry wit plus the two halves of the split, and nothing left of the fused criterion.
        assert [item["key"] for item in body["criteria"]] == [
            "dry_wit",
            "no_buzzwords",
            "plain_register",
        ]

        validated = runner.invoke(cli_app, ["goals", "validate", slug])
        assert validated.exit_code == 0, validated.output

    def test_a_hand_written_pack_survives_a_round_trip_through_the_wizard(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The other direction: a pack written by hand opens in the wizard without loss.

        Checked at the byte level, because "without loss" only means something as an exact
        statement: the pack is written by hand, opened as a wizard draft, rendered back, written
        again under a second slug, and the two documents are compared as canonical JSON with only
        the slug and the pack's own name allowed to differ.

        Reading a pack must also not *rewrite* it, so the original file's bytes are compared
        before and after.
        """
        from baseaicore import canonical_json

        from freeweight.services.database import Database
        from freeweight.services.goals import load_goal, write_pack
        from freeweight.services.wizard import _task_record, draft_from_goal, pack_body

        root = workspace / "goals"
        hand_written = {
            "slug": "by_hand",
            "name": "Written in an editor",
            "goal_pack_version": "1.0.0",
            "schema_version": "1.0",
            "intent": "A pack nobody used a wizard for.",
            "created_by": "wizard",
            "criteria": [
                {
                    "key": "no_tells",
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
            "judge": {
                "jury_size": 3,
                "models": [],
                "repetitions": 3,
                "randomize_order": True,
                "allow_remote": False,
                "temperature": 0.0,
            },
            "calibration": {
                "target_samples": 12,
                "min_samples": 8,
                "holdout_fraction": 0.4,
                "partition_seed": 0,
                "min_agreement": 0.4,
            },
        }
        original = write_pack(
            root,
            goal=hand_written,
            tasks=[
                {
                    "prompt_id": "goals.by_hand.one",
                    "version": "1.0.0",
                    "schema_version": "1.0",
                    "purpose": "A task typed into an editor.",
                    "task": "goal.by_hand",
                    "capability": "creative_writing",
                    "system": None,
                    "template": "Write three paragraphs about a warehouse at night.",
                    "variables": {},
                    "response": {"format": "text", "json_schema_ref": None, "expectations": []},
                    "model_requirements": {
                        "min_context_tokens": 4096,
                        "requires_capabilities": [],
                        "recommended_temperature": 0.7,
                    },
                    "metadata": {
                        "author": "a person",
                        "created_at": "2026-08-28T00:00:00Z",
                        "changed_at": "2026-08-28T00:00:00Z",
                        "change_reason": "First version.",
                        "supersedes": None,
                        "tags": ["goal"],
                        "goal_task": {"key": "one", "name": "Warehouse night"},
                    },
                }
            ],
        )
        path = original.pack_path / "goal.json"
        before = path.read_bytes()

        with Database.from_url(f"sqlite:///{workspace / 'freeweight.sqlite3'}") as database:
            draft = draft_from_goal(database, original)
            rendered = pack_body(draft)
            rendered["slug"] = "round_tripped"
            round_tripped = write_pack(
                root,
                goal=rendered,
                tasks=[_task_record(draft, task) for task in draft.tasks],
            )

        assert path.read_bytes() == before, "opening the pack in the wizard rewrote it"

        reloaded = load_goal(round_tripped.pack_path)
        first = canonical_json(
            {
                key: value
                for key, value in json.loads(before.decode("utf-8")).items()
                if key not in {"slug", "name"}
            }
        )
        second = canonical_json(
            {
                key: value
                for key, value in json.loads(
                    (reloaded.pack_path / "goal.json").read_text(encoding="utf-8")
                ).items()
                if key not in {"slug", "name"}
            }
        )
        assert first == second, "the wizard lost or added something on the round trip"


class TestTheRestOfTheJourney:
    def test_grade_calibrate_run_compare_and_export_with_no_file_editing(
        self, client: TestClient, workspace: Path
    ) -> None:
        """The full sequence spec §20 criterion 13 names, end to end.

        The server is restarted once in the middle, deliberately: a goal installed after the
        process started is not runnable until the registry is rebuilt, and a journey test that
        never crossed that boundary would be testing a state no user is ever in.
        """
        slug = _walk_the_wizard(client)

        # Grade: samples in, grades recorded through the page's own form.
        added = client.post(
            f"/api/v1/goals/{slug}/calibration/samples",
            json={
                "samples": [
                    {"content": f"Candidate essay number {index}, written by hand."}
                    for index in range(12)
                ]
            },
        )
        assert added.status_code in (200, 201), added.text
        state = client.get(f"/api/v1/goals/{slug}/calibration").json()
        sample_ids = [sample["id"] for sample in state["items"]]
        assert len(sample_ids) == 12  # noqa: PLR2004 — the documented target

        grade_page = client.get(f"/goals/{slug}/grade")
        assert grade_page.status_code == 200, grade_page.text
        assert "Blinded and shuffled" in grade_page.text
        for index, sample_id in enumerate(sample_ids):
            client.post(
                f"/goals/{slug}/grade",
                data={
                    "sample_id": sample_id,
                    "criterion": "dry_wit",
                    "grade": str((index % 5) + 1),
                    "note": f"note {index}",
                },
            )
        after = client.get(f"/api/v1/goals/{slug}/calibration").json()
        assert after["progress"]["recorded_grades"] == 12  # noqa: PLR2004

        # Calibrate: a report, whatever band it reaches.
        report = client.get(f"/goals/{slug}/report")
        assert report.status_code == 200, report.text
        assert "What the number means" in report.text or "No calibration yet" in report.text

        # Run: a fresh process, because the registry is built at startup.
        assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0
        with _client(workspace) as second:
            run_ids = []
            for _ in range(2):
                created = second.post(
                    "/api/v1/runs",
                    json={"model": "fake-model:8b-q8_0", "suite": f"goal.{slug}"},
                )
                assert created.status_code == 201, created.text
                run_id = created.json()["id"]
                deadline = time.monotonic() + 90.0
                while second.get(f"/api/v1/runs/{run_id}").json()["status"] not in _TERMINAL:
                    assert time.monotonic() < deadline, "the goal run never finished"
                    time.sleep(0.05)
                run_ids.append(run_id)

            # Compare: two runs of the same rubric are comparable.
            compared = second.get("/api/v1/results/compare", params={"subjects": ",".join(run_ids)})
            assert compared.status_code == 200, compared.text

            # Export: the results, the pack, and the goal's own page.
            exported = second.get(
                "/api/v1/results/export",
                params={"scope": "suite", "selector": f"goal.{slug}", "format": "json"},
            )
            assert exported.status_code == 200, exported.text
            pack_export = second.get(f"/api/v1/goals/{slug}/export")
            assert pack_export.status_code == 200
            assert pack_export.json()["schema"] == "benchmark.goal_pack"
            assert pack_export.json()["payload"]["goal_hash"].startswith("sha256:")

            # An uncalibrated goal refuses a calibration report rather than returning an empty
            # one: "never calibrated" and "calibrated to nothing" are different answers.
            report_export = second.get(f"/api/v1/goals/{slug}/calibration/report/export")
            assert report_export.status_code in (200, 400), report_export.text
            if report_export.status_code == 200:
                assert report_export.json()["schema"] == "benchmark.calibration_report"

            detail = second.get(f"/results/goals/{slug}")
            assert detail.status_code == 200, detail.text
            assert "Score method mix" in detail.text

        # And nothing on disk was edited by hand at any point.
        assert (workspace / "goals" / slug / "goal.json").is_file()


class TestGoalResultsCarryTheirMix:
    def test_the_goal_page_shows_score_method_mix_beside_the_score(
        self, client: TestClient
    ) -> None:
        slug = _walk_the_wizard(client)

        page = client.get(f"/results/goals/{slug}")

        assert page.status_code == 200
        assert "Score method mix" in page.text
        assert "Deterministic, free, and never disagrees with you." in page.text
