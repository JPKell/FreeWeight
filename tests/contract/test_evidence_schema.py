"""Contract: the ``capability.evidence`` FreeWeight writes, read back through SetSpec's contract.

The producer's half of testing standards §8, for the payload LoadCoach routes on:

1. **A writer emits what the schema declares, and nothing else.** Every record goes out through
   ``CapabilityEvidenceOut`` (``extra="forbid"``), so a field FreeWeight invented fails here.
2. **A reader accepts it, and preserves what it does not know** (ADR-0009 rule 4).
3. **It validates against the published JSON Schema** — the document a non-Python consumer holds,
   which pydantic's own validation would never exercise.
4. **It matches the goldens structurally**: the keys a record carries are the keys SetSpec's
   ``full`` golden carries, and never fewer than its ``minimal`` one.
5. **A payload missing ``measured_at``, ``policy_version`` or ``vocabulary_version`` is rejected
   with the field named** (SetSpec spec §18).

The evidence behind these assertions comes from a real run on the fake provider, so the record
under test is one the run engine wrote rather than one a test assembled by hand.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError as PydanticValidationError
from setspec import SchemaVersion, golden_names, golden_payloads, json_schema_for
from setspec.capability.v1 import CapabilityEvidenceIn, CapabilityEvidenceOut, EvidenceBundleIn

from freeweight.config import EvidenceSettings, ExecutionSettings, Settings
from freeweight.services.evidence import (
    EvidenceQuery,
    EvidenceRecord,
    evidence_bundle,
    query_evidence,
)
from freeweight.services.runs import ExecutionConfig, create_run
from freeweight.services.scheduler import RunScheduler

pytestmark = pytest.mark.contract

V1 = SchemaVersion(1, 0)

WEIGHTS = """
version = "test"

[capabilities.reliability]
sources = [
  { suite = "native.echo", metric_key = "harness_roundtrip_success", weight = 1.0 },
]
"""


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
    """A mapping that lets the fastest shipped suite (``native.echo``) feed a capability."""
    weights = tmp_path / "weights.toml"
    weights.write_text(WEIGHTS, encoding="utf-8")
    return EvidenceSettings(capability_weights_path=str(weights))


@pytest.fixture
def record(
    run_environment: Callable[..., Any], evidence_settings: EvidenceSettings
) -> EvidenceRecord:
    """One evidence record the run engine wrote when an echo run completed."""
    environment = run_environment()
    create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=_execution(),
    )
    RunScheduler(
        environment.database,
        environment.provider,
        registry=environment.registry,
        settings=Settings(evidence=evidence_settings),
    ).run_once()
    page = query_evidence(environment.database, EvidenceQuery())
    assert len(page.records) == 1, [item.capability_id for item in page.records]
    return page.records[0]


@pytest.fixture
def bundle_text(run_environment: Callable[..., Any], evidence_settings: EvidenceSettings) -> str:
    environment = run_environment()
    create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=_execution(),
    )
    RunScheduler(
        environment.database,
        environment.provider,
        registry=environment.registry,
        settings=Settings(evidence=evidence_settings),
    ).run_once()
    return evidence_bundle(environment.database, EvidenceQuery())


def _golden(schema: str, name: str) -> dict[str, Any]:
    documents = dict(zip(golden_names(schema, V1), golden_payloads(schema, V1), strict=True))
    return documents[name]


class TestTheEvidenceRecord:
    def test_it_is_written_through_the_strict_outbound_model(self, record: EvidenceRecord) -> None:
        payload = record.wire_payload()
        assert isinstance(payload, CapabilityEvidenceOut)
        assert payload.capability_id == "reliability"
        assert payload.contributing_metrics[0].metric_key == "native.echo.harness_roundtrip_success"

    def test_a_consumer_reads_it_back(self, record: EvidenceRecord) -> None:
        dumped = record.wire_payload().model_dump()
        parsed = CapabilityEvidenceIn.model_validate(dumped)
        assert parsed.capability_id == record.capability_id
        assert parsed.score == pytest.approx(record.score)
        assert parsed.extras == {}

    def test_a_reader_preserves_a_field_this_build_does_not_know(
        self, record: EvidenceRecord
    ) -> None:
        dumped = {**record.wire_payload().model_dump(), "measured_under_moonlight": True}
        parsed = CapabilityEvidenceIn.model_validate(dumped)
        assert parsed.extras["measured_under_moonlight"] is True

    def test_the_strict_writer_refuses_that_same_field(self, record: EvidenceRecord) -> None:
        dumped = {**record.wire_payload().model_dump(), "measured_under_moonlight": True}
        with pytest.raises(PydanticValidationError, match="measured_under_moonlight"):
            CapabilityEvidenceOut.model_validate(dumped)

    def test_it_validates_against_the_published_json_schema(self, record: EvidenceRecord) -> None:
        """The document a non-Python consumer holds, exercised with a real validator."""
        envelope = record.envelope(generated_at=record.computed_at)
        jsonschema.validate(
            instance=envelope["payload"], schema=json_schema_for("capability.evidence", V1)
        )

    def test_it_matches_the_goldens_structurally(self, record: EvidenceRecord) -> None:
        """The keys a record carries are exactly the keys the writer emits for the goldens.

        A SetSpec writer emits every declared field — an absent goal group travels as ``null``
        values, not as missing keys — so the comparison is against the goldens *as written*, and
        against the published schema's own property list, never against a hand-typed key list.
        """
        keys = set(record.envelope(generated_at=record.computed_at)["payload"])
        minimal = set(_golden("capability.evidence", "minimal"))
        written = set(
            CapabilityEvidenceOut.model_validate(
                _golden("capability.evidence", "full")
            ).model_dump()
        )
        declared = set(json_schema_for("capability.evidence", V1)["properties"])
        assert minimal <= keys, sorted(minimal - keys)
        assert keys == written, sorted(keys ^ written)
        assert keys <= declared, sorted(keys - declared)

    @pytest.mark.parametrize("field", ["measured_at", "policy_version", "vocabulary_version"])
    def test_a_payload_missing_a_required_field_is_rejected_naming_it(
        self, record: EvidenceRecord, field: str
    ) -> None:
        dumped = record.wire_payload().model_dump()
        del dumped[field]
        with pytest.raises(PydanticValidationError) as raised:
            CapabilityEvidenceIn.model_validate(dumped)
        assert any(error["loc"] == (field,) for error in raised.value.errors())

    def test_every_setspec_golden_is_one_this_build_could_have_written(self) -> None:
        """The consumer's side of §8.3, from the producer: every golden is readable here."""
        for name in golden_names("capability.evidence", V1):
            CapabilityEvidenceIn.model_validate(_golden("capability.evidence", name))

    def test_freshness_fields_are_the_ones_the_contract_names(self, record: EvidenceRecord) -> None:
        """``measured_at`` never after ``computed_at``, and both RFC 3339 on the wire."""
        payload = record.envelope(generated_at=record.computed_at)["payload"]
        assert payload["measured_at"] <= payload["computed_at"]
        assert payload["measured_at"].endswith("Z")
        assert payload["environment"]["provider_kind"] == "fake"
        assert payload["judge_validity_factor"] == 1.0
        # A writer emits every declared field: an absent goal group is a null, not a missing key.
        assert payload["goal_hash"] is None
        assert payload["uncalibrated"] is False


class TestTheEvidenceBundle:
    def test_it_is_one_envelope_declaring_the_frozen_schema(self, bundle_text: str) -> None:
        document = json.loads(bundle_text)
        assert document["schema"] == "benchmark.evidence_bundle"
        assert document["schema_version"] == "1.0"
        assert document["generator"]["name"] == "freeweight"
        assert "items" not in document

    def test_it_validates_against_the_published_json_schema(self, bundle_text: str) -> None:
        payload = json.loads(bundle_text)["payload"]
        jsonschema.validate(
            instance=payload, schema=json_schema_for("benchmark.evidence_bundle", V1)
        )

    def test_a_consumer_reads_it_back_and_it_matches_the_goldens(self, bundle_text: str) -> None:
        payload = json.loads(bundle_text)["payload"]
        bundle = EvidenceBundleIn.model_validate(payload)
        assert bundle.complete is True
        assert len(bundle.evidence) == 1
        assert set(payload) == set(_golden("benchmark.evidence_bundle", "full"))
        assert payload["source_id"].startswith("freeweight")
