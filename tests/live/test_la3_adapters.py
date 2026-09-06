"""LA3, FreeWeight's half: measure adapter subjects on a real base and export a `1.1` bundle.

Needs a GPU, `llama-server` on ``PATH``, a GGUF base and at least two reviewed LoRA manifests, so
it is `live` and skips loudly with what it wanted when they are absent:

```bash
FWTEST_LLAMACPP_MODELS=<dir of GGUF bases> \
FWTEST_LLAMACPP_ADAPTERS=<dir of adapters + manifests> \
FWTEST_LLAMACPP_BASE=Qwen2.5-1.5B-Instruct.Q8_0 \
.venv/bin/python -m pytest -m live tests/live/test_la3_adapters.py -rs -s -p no:randomly
```

What it proves, which no fake can:

1. `provider.kind = "llamacpp"` constructs from configuration and serves the real base.
2. Subjects enumerate as base × compatible adapter, by **digest**, against real artefacts.
3. **Two** of the three adapters are measured and the third is left alone, so an unmeasured
   subject is a real state in the exported bundle's absence rather than a contrived one.
4. Evidence measured under an adapter never reaches the base or a sibling — asserted against real
   rows, because the failure looks exactly like a working join
   ([ADR-0059](../../docs/adr/0059-adapter-evidence-is-measured-never-inherited.md)).
5. The export is `benchmark.evidence_bundle` **1.1** and validates against the published schema
   with `setspec` alone, carrying adapter-bearing and bare-base records together, which is the
   normal shape of a real export (ADR-0084).
6. The serving-mode A/B produces two separable measurements of one base, **and prints the
   overhead as a number** — ADR-0060's revisit trigger ("the measured serving-mode overhead is
   material on reference hardware — flip the default") cannot be acted on without one.

Step 3's exported file is the input to I18's second half, which happens in LoadCoach and is not
this test's to run.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

_MODELS_ENV = "FWTEST_LLAMACPP_MODELS"
_ADAPTERS_ENV = "FWTEST_LLAMACPP_ADAPTERS"
_BASE_ENV = "FWTEST_LLAMACPP_BASE"

_SUITE = "native.echo"
_MEASURED = ("terse", "verbose")
_UNMEASURED = "pirate"

_WEIGHTS = """
version = "la3"

[capabilities.reliability]
sources = [
  { suite = "native.echo", metric_key = "harness_roundtrip_success", weight = 1.0 },
]
"""


def _directory(name: str) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        pytest.skip(f"{name} is not set; it must name a directory for this journey to run")
    path = Path(raw).expanduser()
    if not path.is_dir():
        pytest.skip(f"{name}={raw!r} is not a directory")
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Any:
    """A `llamacpp` configuration over the operator's real directories."""
    from freeweight.config import (
        AdapterSettings,
        EvidenceSettings,
        ExecutionSettings,
        ProviderSettings,
        Settings,
    )

    models = _directory(_MODELS_ENV)
    adapters = _directory(_ADAPTERS_ENV)
    weights = tmp_path / "weights.toml"
    weights.write_text(_WEIGHTS, encoding="utf-8")
    return Settings(
        provider=ProviderSettings(
            kind="llamacpp",
            model_directory=str(models),
            state_dir=str(tmp_path / "state"),
            timeout_seconds=600.0,
        ),
        adapters=AdapterSettings(directory=str(adapters)),
        evidence=EvidenceSettings(capability_weights_path=str(weights)),
        execution=ExecutionSettings(
            warmup_repetitions=0,
            cooldown_seconds=0,
            idle_gpu_threshold_percent=0,
            randomize_case_order=False,
        ),
    )


@pytest.fixture
def journey(settings: Any, tmp_path: Path) -> Any:
    """A migrated database, the real llama.cpp provider with adapters registered, and the base."""
    from datetime import UTC, datetime

    from weightsdb import MigrationRunner, create_engine_for

    from freeweight.infrastructure.providers.factory import build_provider
    from freeweight.services.adapters import read_entries
    from freeweight.services.database import MIGRATIONS_LOCATION, Database
    from freeweight.services.models import discover_models
    from freeweight.services.runs import build_registry
    from freeweight.services.telemetry import build_collector

    base_name: str = os.environ.get(_BASE_ENV, "").strip()
    if not base_name:
        pytest.skip(f"{_BASE_ENV} is not set; it must name the base GGUF (without .gguf)")

    url = f"sqlite:///{tmp_path / 'la3.sqlite3'}"
    engine = create_engine_for(url)
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()

    database = Database.from_url(url)
    provider = build_provider(settings.provider, adapters=settings.adapters)
    entries = read_entries(settings.adapters)
    available = [entry.name for entry in entries if entry.available]
    if len(available) < len(_MEASURED) + 1:
        pytest.skip(
            f"this journey needs at least {len(_MEASURED) + 1} available adapters so one can be "
            f"left unmeasured; {_ADAPTERS_ENV} has {available}"
        )
    try:
        discover_models(database, provider, now=datetime.now(UTC))
        yield {
            "database": database,
            "provider": provider,
            "collector": build_collector(),
            "registry": build_registry(),
            "settings": settings,
            "entries": entries,
            "model_ref": base_name,
        }
    finally:
        # The journey's result is already recorded; a supervisor complaining on the way down must
        # not turn a passing measurement into a failing test.
        with suppress(Exception):
            close = getattr(provider, "close", None)
            if close is not None:
                close()
        database.close()


def _measure(journey: Any, *, adapter: str | None, registered: bool | None = None) -> str:
    """Run one suite against one subject and recompute the evidence it supports."""
    from freeweight.services.evidence import recompute_for_run
    from freeweight.services.runs import ExecutionConfig, create_run
    from freeweight.services.scheduler import RunScheduler

    settings = journey["settings"]
    summary = create_run(
        journey["database"],
        journey["provider"],
        journey["collector"],
        journey["registry"],
        model_ref=journey["model_ref"],
        suite_key=_SUITE,
        execution=ExecutionConfig.resolve(settings.execution, measured_repetitions=1),
        runtime_profile=settings.runtime.to_profile(adapters_registered=registered),
        adapter_name=adapter,
        adapter_entries=journey["entries"],
    )
    RunScheduler(
        journey["database"],
        journey["provider"],
        registry=journey["registry"],
        collector=journey["collector"],
        settings=settings,
    ).run_once()
    recompute_for_run(journey["database"], summary.id, settings=settings.evidence)
    return str(summary.id)


def test_la3_freeweight_half(journey: Any, tmp_path: Path) -> None:
    """Measure two adapters, leave one alone, export a `1.1` bundle, and print the evidence."""
    import jsonschema
    from setspec import json_schema_for

    from freeweight.infrastructure.db.repositories.models import ModelRepository
    from freeweight.services.adapters import measured_scores, resolve_subject, subjects_for_model
    from freeweight.services.evidence import (
        BUNDLE_SCHEMA,
        BUNDLE_SCHEMA_VERSION_ADAPTER,
        EvidenceQuery,
        evidence_bundle,
        group_by_base,
        query_evidence,
    )

    database = journey["database"]
    entries = journey["entries"]

    # (2) Subjects enumerate as base x compatible adapter, by digest, over real artefacts.
    with database.read() as session:
        row = ModelRepository().get_by_provider_model_name(session, journey["model_ref"])
        assert row is not None, f"{journey['model_ref']!r} was not discovered"
        model = type(
            "Row",
            (),
            {
                "provider_kind": row.provider_kind,
                "provider_model_name": row.provider_model_name,
                "artifact_digest": row.artifact_digest,
            },
        )()
    subjects = subjects_for_model(model, entries)
    print("\nsubjects enumerated:")
    for subject in subjects:
        print(f"  {subject.canonical_id}  confidence={subject.confidence.value}")
    assert subjects[0].adapter is None
    assert len(subjects) >= len(_MEASURED) + 2, [s.adapter_name for s in subjects]

    # (1, 3) Measure the base and two of the three adapters; leave the third alone.
    _measure(journey, adapter=None)
    for name in _MEASURED:
        _measure(journey, adapter=name)

    # (4) No inheritance, on real rows.
    base_subject = resolve_subject(model, entries, None)
    print("\nmeasured per subject:")
    every_subject: tuple[str | None, ...] = (None, *_MEASURED, _UNMEASURED)
    for adapter_name in every_subject:
        subject = resolve_subject(model, entries, adapter_name)
        scores = measured_scores(database, subject)
        print(f"  {subject.canonical_id}: {scores or '—'}")
        if adapter_name == _UNMEASURED:
            assert scores == {}, "an unmeasured subject must inherit nothing from its base"
        else:
            assert scores, f"{adapter_name or 'base'} should have been measured"
    assert measured_scores(database, base_subject), "the base must have its own evidence"

    # The three measured subjects are three sets of rows, not one shared set.
    records = query_evidence(database, EvidenceQuery()).records
    subject_ids = {record.subject_canonical_id for record in records}
    assert len(subject_ids) == len(_MEASURED) + 1, sorted(subject_ids)

    groups = group_by_base(records)
    print("\ngrouped by base:")
    for group in groups:
        print(f"  {group.base_canonical_id}  ({group.adapter_count} adapter subjects)")
        for summary in group.subjects:
            print(
                f"    {summary.adapter_name or '(bare base)'}: {summary.record_count} record(s) "
                f"from {summary.source_run_count} run(s)"
            )
    assert len(groups) == 1
    assert groups[0].adapter_count == len(_MEASURED)

    # (5) The export is 1.1 and validates with setspec alone.
    text = evidence_bundle(database, EvidenceQuery())
    document = json.loads(text)
    assert document["schema_version"] == str(BUNDLE_SCHEMA_VERSION_ADAPTER)
    jsonschema.validate(
        document["payload"], json_schema_for(BUNDLE_SCHEMA, BUNDLE_SCHEMA_VERSION_ADAPTER)
    )
    bearing = [item for item in document["payload"]["evidence"] if "adapter" in item]
    bare = [item for item in document["payload"]["evidence"] if "adapter" not in item]
    assert len(bearing) == len(_MEASURED), [item.get("adapter") for item in bearing]
    assert bare, "the bare base's records must be in the same bundle — mixed is the normal shape"
    print(
        f"\nbundle: {document['schema_version']}, "
        f"{len(bearing)} adapter-bearing + {len(bare)} bare record(s)"
    )
    for item in bearing:
        print(f"  {item['adapter']['name']}  {item['adapter']['canonical_suffix']}")

    out = tmp_path / "la3-bundle.json"
    out.write_text(text, encoding="utf-8")
    print(f"\nbundle written to {out}")
    print("I18's second half is LoadCoach importing this file; nothing else crosses.")


def _arm(journey: Any, *, registered: bool, warmups: int) -> tuple[str, float]:
    """Run one arm of the A/B on **its own server**, and return ``(run_id, warm ms)``.

    Two properties this function exists to get right, and both were wrong in the first draft:

    * **Arm A is served by a provider that genuinely has no adapters.** Passing
      ``adapters_registered=False`` to a run served by a server that *had* registered them would
      record a measurement that lies about its own conditions, which is the whole thing the
      profile-hash discipline exists to prevent.
    * **The measurement is warm.** Each arm is a separate server launch, so the first run in an arm
      pays the model load — about a second on this base. Comparing two cold runs measures loading
      twice and reports the difference between two start-ups as an adapter-registration overhead.
      The warm-ups are discarded and the last run is the number.
    """
    from freeweight.infrastructure.providers.factory import build_provider
    from freeweight.services.runs import get_run

    settings = journey["settings"]
    provider = build_provider(settings.provider, adapters=settings.adapters if registered else None)
    arm = {**journey, "provider": provider}
    try:
        for _ in range(warmups):
            _measure(arm, adapter=None, registered=registered)
        run_id = _measure(arm, adapter=None, registered=registered)
    finally:
        with suppress(Exception):
            close = getattr(provider, "close", None)
            if close is not None:
                close()
    detail = get_run(journey["database"], run_id)
    assert detail.run.status == "completed", detail.run
    started, completed = detail.run.started_at, detail.run.completed_at
    assert started is not None and completed is not None
    return run_id, (completed - started).total_seconds() * 1000.0


def test_the_serving_mode_overhead_is_measured_and_reported(journey: Any) -> None:
    """(6) Two separable measurements of one base, and the overhead as a number.

    ADR-0060's revisit trigger is "the measured serving-mode overhead is material on reference
    hardware — flip the default", and nobody can act on that without the number. It is printed
    rather than asserted against a threshold: this test's job is to produce the figure honestly,
    and deciding whether it is material is a person's.
    """
    from freeweight.services.evidence import subject_of_run

    arms = {
        registered: _arm(journey, registered=registered, warmups=2) for registered in (False, True)
    }
    subjects = {
        registered: subject_of_run(journey["database"], run_id)
        for registered, (run_id, _) in arms.items()
    }

    assert subjects[False].runtime_profile_id != subjects[True].runtime_profile_id, (
        "the two arms must be separable by runtime_profile_hash, or the A/B measures nothing"
    )
    assert all(subject.adapter_id is None for subject in subjects.values()), (
        "the A/B measures the base; neither arm may be an adapter subject"
    )

    clean_ms, registered_ms = arms[False][1], arms[True][1]
    overhead_ms = registered_ms - clean_ms
    percent = (overhead_ms / clean_ms * 100.0) if clean_ms else float("nan")
    print(
        f"\nserving-mode A/B on this base (warm, 2 discarded warm-ups per arm):\n"
        f"  clean      {clean_ms:8.1f} ms  run {arms[False][0]}\n"
        f"  registered {registered_ms:8.1f} ms  run {arms[True][0]}\n"
        f"  overhead   {overhead_ms:+8.1f} ms ({percent:+.1f} %)\n"
        "  ADR-0060's revisit trigger asks whether that is material on reference hardware."
    )
