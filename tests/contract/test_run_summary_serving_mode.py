"""Contract: a run summary carries ``adapters_registered`` when its profile stated it (ADR-0135).

Row WA1. FreeWeight states the field for every run on an adapter-capable provider and stores the
hash BaseAiCore computes with it (ADR-0074). Before ``1.3.0`` the ``runtime_profiles`` row had no
column for it and the export sent the profile without it, so SetSpec's own hash check refused the
run. Three claims, each asserted rather than argued:

1. **A run whose profile states the field exports it**, the summary validates through
   ``BenchmarkRunSummaryV1_1In`` and the published `1.1` JSON Schema, and the ``freeweight.export``
   document is `1.1` — in both JSON forms.
2. **A run whose profile leaves it unstated does not move**: no key, a `1.0` document, and a summary
   the frozen `1.0` writer reproduces byte for byte (ADR-0084 rule 2).
3. **A stored run reads back the profile that was hashed**, so executing, resuming or repeating it
   presents the provider the same profile, in each of the three states.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import jsonschema
import pytest
from baseaicore import RuntimeProfile, canonical_json
from setspec import SchemaVersion, json_schema_for
from setspec.benchmark.v1 import BenchmarkRunSummaryOut, BenchmarkRunSummaryV1_1In

from freeweight.config import ExecutionSettings, Settings
from freeweight.services.export import (
    EXPORT_SCHEMA_VERSION,
    ExportFormat,
    ExportScope,
    ExportSelection,
    iter_export,
)
from freeweight.services.runs import ExecutionConfig, _stored_runtime_profile, create_run
from freeweight.services.scheduler import RunScheduler

pytestmark = pytest.mark.contract

_RUN_SUMMARY_1_1 = SchemaVersion(1, 1)


def _measured_run(environment: Any, profile: RuntimeProfile) -> str:  # noqa: ANN401 — RunEnvironment
    """Create and execute one echo run under ``profile``; return its id."""
    created = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=ExecutionConfig.resolve(
            ExecutionSettings(
                warmup_repetitions=0,
                cooldown_seconds=0,
                idle_gpu_threshold_percent=0,
                randomize_case_order=False,
            ),
            measured_repetitions=1,
        ),
        runtime_profile=profile,
    )
    RunScheduler(
        environment.database,
        environment.provider,
        registry=environment.registry,
        settings=Settings(),
    ).run_once()
    return str(created.id)


def _exported(environment: Any, run_id: str, export_format: ExportFormat) -> dict[str, Any]:  # noqa: ANN401
    """The export document for one run — the single JSON envelope, or the one JSONL line."""
    selection = ExportSelection(scope=ExportScope.RUN, selector=run_id, export_format=export_format)
    document: dict[str, Any] = json.loads("".join(iter_export(environment.database, selection)))
    return document


class TestAStatedRunExportsTheField:
    """Claim 1: the defect row WA1 closes."""

    @pytest.mark.parametrize("export_format", [ExportFormat.JSON, ExportFormat.JSONL])
    @pytest.mark.parametrize("state", [False, True])
    def test_its_summary_states_the_field_and_validates_at_1_1(
        self, run_environment: Callable[..., Any], state: bool, export_format: ExportFormat
    ) -> None:
        environment = run_environment()
        run_id = _measured_run(environment, RuntimeProfile(adapters_registered=state))

        document = _exported(environment, run_id, export_format)

        summary = document["payload"]["runs"][0]["summary"]
        assert summary["runtime_profile"]["adapters_registered"] is state
        parsed = BenchmarkRunSummaryV1_1In.model_validate(summary)
        assert parsed.runtime_profile.adapters_registered is state
        jsonschema.validate(summary, json_schema_for("benchmark.run_summary", _RUN_SUMMARY_1_1))
        assert document["schema_version"] == "1.1"

    def test_one_stated_run_makes_the_whole_json_export_1_1(
        self, run_environment: Callable[..., Any]
    ) -> None:
        """A container is `1.1` if any run in it is — the bundle rule, one document out."""
        environment = run_environment()
        stated = _measured_run(environment, RuntimeProfile(adapters_registered=True))
        unstated = _measured_run(environment, RuntimeProfile())
        selection = ExportSelection(scope=ExportScope.COMPARISON, selector=f"{unstated},{stated}")

        document = json.loads("".join(iter_export(environment.database, selection)))

        profiles = [run["summary"]["runtime_profile"] for run in document["payload"]["runs"]]
        assert sorted("adapters_registered" in profile for profile in profiles) == [False, True]
        assert document["schema_version"] == "1.1"


class TestAnUnstatedRunDoesNotMove:
    """Claim 2: an installation whose provider serves no adapters exports what it always did."""

    @pytest.mark.parametrize("export_format", [ExportFormat.JSON, ExportFormat.JSONL])
    def test_it_exports_no_key_at_1_0(
        self, run_environment: Callable[..., Any], export_format: ExportFormat
    ) -> None:
        environment = run_environment()
        run_id = _measured_run(environment, RuntimeProfile())

        document = _exported(environment, run_id, export_format)

        assert (
            "adapters_registered"
            not in document["payload"]["runs"][0]["summary"]["runtime_profile"]
        )
        assert document["schema_version"] == str(EXPORT_SCHEMA_VERSION)

    def test_the_frozen_1_0_writer_reproduces_its_summary_byte_for_byte(
        self, run_environment: Callable[..., Any]
    ) -> None:
        environment = run_environment()
        run_id = _measured_run(environment, RuntimeProfile())

        summary = _exported(environment, run_id, ExportFormat.JSON)["payload"]["runs"][0]["summary"]

        assert canonical_json(BenchmarkRunSummaryOut.model_validate(summary).model_dump()) == (
            canonical_json(summary)
        )


class TestAStoredRunReadsBackTheProfileThatWasHashed:
    """Claim 3: the row is where a queued, resumed or repeated run gets its profile from."""

    @pytest.mark.parametrize("state", [None, False, True])
    def test_the_rebuilt_profile_hashes_as_the_original(
        self, run_environment: Callable[..., Any], state: bool | None
    ) -> None:
        from freeweight.infrastructure.db.models_runs import Run

        environment = run_environment()
        profile = RuntimeProfile(context_size=4096, adapters_registered=state)
        run_id = _measured_run(environment, profile)

        with environment.database.read() as session:
            run = session.get(Run, run_id)
            assert run is not None
            rebuilt = _stored_runtime_profile(session, run.runtime_profile_id)

        assert rebuilt.adapters_registered is state
        assert rebuilt.profile_hash == profile.profile_hash
