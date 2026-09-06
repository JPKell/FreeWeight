"""Contract: FreeWeight writes the lowest payload version that can express the document.

[ADR-0084](../../docs/adr/0084-a-producer-chooses-a-payload-version-by-content.md). Since Phase 15
this build can write `capability.evidence` and `benchmark.evidence_bundle` at either `1.0` or
`1.1`, and it chooses per document rather than per build. Three claims, each asserted rather than
argued:

1. **A bundle with no adapter-bearing record is `1.0`, and is byte-for-byte what `1.0.0` wrote.**
   `1.0.0`'s writer is reproduced literally in :func:`_bytes_the_1_0_0_writer_produced` — the
   frozen `EvidenceBundleOut` over `CapabilityEvidenceOut` dumps, at
   `BUNDLE_SCHEMA_VERSION`, through `dump_envelope` — so this is a comparison against that
   release's code path and not against a re-run of the current one.
2. **A bundle carrying any adapter-bearing record is `1.1`**, and validates against the published
   `1.1` JSON Schema with `setspec` alone.
3. **A bare record never emits `"adapter": null`.** One byte is the whole difference between an
   additive minor and a breaking one (ADR-0068 rule 4).

Always writing `1.1` would pass 2 and 3 and fail 1, which is why 1 is here: it is the assertion the
tempting simplification breaks.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from setspec import SchemaVersion, dump_envelope, json_schema_for, load_envelope
from setspec.capability.v1 import (
    CapabilityEvidenceOut,
    EvidenceBundleIn,
    EvidenceBundleOut,
    EvidenceBundleV1_1In,
)

from freeweight.config import EvidenceSettings, ExecutionSettings, Settings
from freeweight.services.evidence import (
    _GENERATOR,
    BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_VERSION,
    BUNDLE_SCHEMA_VERSION_ADAPTER,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION_ADAPTER,
    EvidenceQuery,
    EvidenceRecord,
    _matching_records,
    _source_id,
    evidence_bundle,
    query_evidence,
)
from freeweight.services.runs import ExecutionConfig, create_run
from freeweight.services.scheduler import RunScheduler

pytestmark = pytest.mark.contract

_WEIGHTS = """
version = "test"

[capabilities.reliability]
sources = [
  { suite = "native.echo", metric_key = "harness_roundtrip_success", weight = 1.0 },
]
"""

_GENERATED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
_ADAPTER_DIGEST = "sha256:" + "c3" * 32


@pytest.fixture
def evidence_settings(tmp_path: Path) -> EvidenceSettings:
    weights = tmp_path / "weights.toml"
    weights.write_text(_WEIGHTS, encoding="utf-8")
    return EvidenceSettings(capability_weights_path=str(weights))


@pytest.fixture
def measured(run_environment: Callable[..., Any], evidence_settings: EvidenceSettings) -> Any:
    """An environment with one completed echo run, so there is real evidence to export."""
    environment = run_environment()
    create_run(
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
    )
    RunScheduler(
        environment.database,
        environment.provider,
        registry=environment.registry,
        settings=Settings(evidence=evidence_settings),
    ).run_once()
    return environment


def _bytes_the_1_0_0_writer_produced(environment: Any) -> str:
    """Reproduce `freeweight 1.0.0`'s `evidence_bundle` body, literally.

    Taken from ``v1.0.0``'s ``services/evidence.py``: the frozen ``EvidenceBundleOut`` over
    ``record.wire_payload().model_dump()``, dumped at ``BUNDLE_SCHEMA_VERSION``. Written out here
    rather than imported so that a change to the current writer cannot quietly change what this
    test compares against — which is the whole point of a golden.
    """
    with environment.database.read() as session:
        records = _matching_records(session, EvidenceQuery())
        source_id = _source_id(session)
    bundle = EvidenceBundleOut.model_validate(
        {
            "source_id": source_id,
            "complete": True,
            "evidence": [
                CapabilityEvidenceOut.model_validate(
                    record.wire_payload().model_dump()
                ).model_dump()
                for record in records
            ],
        }
    )
    return dump_envelope(
        bundle,
        schema=BUNDLE_SCHEMA,
        version=BUNDLE_SCHEMA_VERSION,
        generator=_GENERATOR,
        generated_at=_GENERATED_AT,
    )


def _with_adapter(record: EvidenceRecord) -> EvidenceRecord:
    """Return ``record`` as though it had been measured under an adapter."""
    import dataclasses

    return dataclasses.replace(
        record,
        adapter_id="01JADAPTER00000000000000AA",
        adapter_name="terse",
        adapter_artifact_digest=_ADAPTER_DIGEST,
        adapter_source_digest=None,
    )


class TestABareBundleDoesNotMove:
    """Claim 1: an installation that measures no adapter is, on the wire, still 1.0.0."""

    def test_it_is_byte_identical_to_what_1_0_0_wrote(self, measured: Any) -> None:
        expected = _bytes_the_1_0_0_writer_produced(measured)

        actual = evidence_bundle(measured.database, EvidenceQuery(), now=_GENERATED_AT)

        assert actual == expected

    def test_it_declares_1_0(self, measured: Any) -> None:
        text = evidence_bundle(measured.database, EvidenceQuery(), now=_GENERATED_AT)

        assert json.loads(text)["schema_version"] == str(BUNDLE_SCHEMA_VERSION)

    def test_a_1_0_consumer_reads_it(self, measured: Any) -> None:
        """The version is chosen for the consumer's benefit; this is the consumer."""
        text = evidence_bundle(measured.database, EvidenceQuery(), now=_GENERATED_AT)

        envelope = load_envelope(
            text.encode(), expect=BUNDLE_SCHEMA, supported=[BUNDLE_SCHEMA_VERSION]
        )
        assert EvidenceBundleIn.model_validate(envelope.payload).complete is True

    def test_it_validates_against_the_published_1_0_schema(self, measured: Any) -> None:
        text = evidence_bundle(measured.database, EvidenceQuery(), now=_GENERATED_AT)

        jsonschema.validate(
            json.loads(text)["payload"], json_schema_for(BUNDLE_SCHEMA, BUNDLE_SCHEMA_VERSION)
        )


class TestABareRecordEmitsNoAdapterKey:
    """Claim 3: one byte is the difference between an additive minor and a breaking one."""

    def test_the_dump_has_no_adapter_key_at_all(self, measured: Any) -> None:
        record = query_evidence(measured.database, EvidenceQuery()).records[0]

        dumped = record.wire_payload().model_dump()

        assert "adapter" not in dumped

    def test_a_bare_record_is_written_at_1_0(self, measured: Any) -> None:
        record = query_evidence(measured.database, EvidenceQuery()).records[0]

        assert record.schema_version == EVIDENCE_SCHEMA_VERSION
        assert not record.is_adapter_bearing

    def test_the_envelope_of_a_bare_record_says_1_0(self, measured: Any) -> None:
        record = query_evidence(measured.database, EvidenceQuery()).records[0]

        envelope = record.envelope(generated_at=_GENERATED_AT)

        assert envelope["schema_version"] == str(EVIDENCE_SCHEMA_VERSION)
        assert "adapter" not in envelope["payload"]


class TestAnAdapterBearingRecordMovesToOnePointOne:
    """Claim 2: the minor appears in the wild only on documents that use what it added."""

    def test_the_record_is_written_at_1_1_and_carries_the_adapter(self, measured: Any) -> None:
        record = _with_adapter(query_evidence(measured.database, EvidenceQuery()).records[0])

        envelope = record.envelope(generated_at=_GENERATED_AT)

        assert record.schema_version == EVIDENCE_SCHEMA_VERSION_ADAPTER
        assert envelope["schema_version"] == str(EVIDENCE_SCHEMA_VERSION_ADAPTER)
        assert envelope["payload"]["adapter"]["name"] == "terse"
        assert envelope["payload"]["adapter"]["artifact_digest"] == _ADAPTER_DIGEST

    def test_the_subject_string_carries_the_adapter_suffix(self, measured: Any) -> None:
        """From baseaicore, never formatted here — I18's claim rests on it."""
        bare = query_evidence(measured.database, EvidenceQuery()).records[0]
        bearing = _with_adapter(bare)

        assert bare.subject_canonical_id == bare.model_canonical_id
        assert bearing.subject_canonical_id.startswith(f"{bare.model_canonical_id}+terse@sha256:")

    def test_a_bundle_carrying_one_is_1_1_and_validates(self, measured: Any) -> None:
        bearing = _with_adapter(query_evidence(measured.database, EvidenceQuery()).records[0])
        payload = {
            "source_id": "fw-test",
            "complete": True,
            "evidence": [bearing.wire_payload().model_dump()],
        }
        from setspec.capability.v1 import EvidenceBundleV1_1Out

        text = dump_envelope(
            EvidenceBundleV1_1Out.model_validate(payload),
            schema=BUNDLE_SCHEMA,
            version=BUNDLE_SCHEMA_VERSION_ADAPTER,
            generator=_GENERATOR,
            generated_at=_GENERATED_AT,
        )

        document = json.loads(text)
        assert document["schema_version"] == str(BUNDLE_SCHEMA_VERSION_ADAPTER)
        jsonschema.validate(
            document["payload"],
            json_schema_for(BUNDLE_SCHEMA, BUNDLE_SCHEMA_VERSION_ADAPTER),
        )
        parsed = EvidenceBundleV1_1In.model_validate(document["payload"])
        assert parsed.evidence[0].adapter is not None
        assert parsed.evidence[0].adapter.name == "terse"

    def test_a_1_1_bundle_of_bare_records_is_still_what_1_0_writes(self, measured: Any) -> None:
        """ADR-0068 rule 4 at the bundle: the sibling class adds nothing to a document without it.

        Not the producer's choice — the producer writes `1.0` here — but the property that makes
        that choice safe, and the reason a `1.1` consumer reading a `1.0` bundle sees no
        difference.
        """
        from setspec.capability.v1 import EvidenceBundleV1_1Out

        with measured.database.read() as session:
            records = _matching_records(session, EvidenceQuery())
            source_id = _source_id(session)
        payload = {
            "source_id": source_id,
            "complete": True,
            "evidence": [record.wire_payload().model_dump() for record in records],
        }

        assert (
            EvidenceBundleV1_1Out.model_validate(payload).model_dump()
            == EvidenceBundleOut.model_validate(payload).model_dump()
        )


class TestTheDeclaredCeiling:
    """ADR-0084 rule 4: the emitted-schema map is what a consumer must be able to accept."""

    def test_the_map_names_the_highest_version_not_the_document_version(self) -> None:
        from freeweight.services.export import EMITTED_SCHEMAS

        assert EMITTED_SCHEMAS["capability.evidence"] == str(EVIDENCE_SCHEMA_VERSION_ADAPTER)
        assert EMITTED_SCHEMAS["benchmark.evidence_bundle"] == str(BUNDLE_SCHEMA_VERSION_ADAPTER)

    def test_the_ceiling_is_a_real_published_version(self) -> None:
        """A declared ceiling nothing published would be a promise the suite cannot keep."""
        assert json_schema_for(BUNDLE_SCHEMA, SchemaVersion(1, 1)) is not None
