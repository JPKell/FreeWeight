"""The A-2 regression panel against a **real** LoRA, which it had never met (risks T11).

The LA3 journey measured adapter subjects with `native.echo` for speed, so the fixed regression
panel — the thing built to catch a LoRA that learned a voice and stopped taking direction — has
only ever been composed, never run against real adapter weights. This test runs it: rows 1 and 2 of
`FIXED_REGRESSION_SUITES` on the bare base and on one adapter, and prints both sides.

    FWTEST_LLAMACPP_MODELS=<dir of GGUF bases>
    FWTEST_LLAMACPP_ADAPTERS=<dir of adapters + reviewed manifests>
    FWTEST_LLAMACPP_BASE=Qwen2.5-1.5B-Instruct.Q8_0
    FWTEST_A2_ADAPTER=terse
    FWTEST_A2_MAX_OUTPUT_TOKENS=2048  # optional; overrides the panel's own cap

Since [ADR-0089](../../docs/adr/0089-the-fixed-regression-rows-bound-their-own-output.md) the two
fixed rows carry their own per-turn cap, so this variable is only needed to *widen* it — to measure
what the panel costs against an adapter that never stops, which is a fact worth knowing about any
adapter that provokes it.

**A negative result is a result.** This test asserts that the panel *produces comparable numbers
for both subjects*, not that the adapter passes: whether a particular LoRA has forgotten
instruction-following is a fact about that LoRA, and pinning a threshold here would turn one
machine's adapter into the definition of the panel. What it refuses to allow is a panel that
cannot tell — no score, or the base's score standing in for the subject's (ADR-0059).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

_MODELS_ENV = "FWTEST_LLAMACPP_MODELS"
_ADAPTERS_ENV = "FWTEST_LLAMACPP_ADAPTERS"
_BASE_ENV = "FWTEST_LLAMACPP_BASE"
_ADAPTER_ENV = "FWTEST_A2_ADAPTER"
_MAX_OUTPUT_ENV = "FWTEST_A2_MAX_OUTPUT_TOKENS"
_DAMAGED_ENV = "FWTEST_A2_DAMAGED_ADAPTER"

_DAMAGED_SEPARATION_FLOOR = 0.30
"""How far a **known damaged** adapter must fall below its base for the panel to be doing its job.

Not a threshold on adapters in general — :func:`test_the_regression_panel_meets_a_real_adapter`
deliberately refuses to pin one, because whether a particular LoRA has forgotten something is a
fact about that LoRA. This is a threshold on the **panel**, asserted against one artefact that is
damaged on purpose and whose measured drop on this machine is `0.545`. A run that no longer
separates it has stopped measuring the adapter at all, which is exactly the defect row H6 found and
which no unit test can catch: sending the adapter and *serving* it are different claims."""

_WEIGHTS = """
version = "a2"

[capabilities.instruction_following]
sources = [
  { suite = "native.instruction_following", metric_key = "strict_prompt_accuracy", weight = 0.6 },
  { suite = "native.instruction_following", metric_key = "loose_prompt_accuracy", weight = 0.4 },
]

[capabilities.structured_output]
sources = [
  { suite = "native.structured_output", metric_key = "schema_conformance", weight = 1.0 },
]
"""


def _directory(name: str) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        pytest.skip(f"{name} is not set")
    path = Path(raw).expanduser()
    if not path.is_dir():
        pytest.skip(f"{name}={path} is not a directory")
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

    weights = tmp_path / "weights.toml"
    weights.write_text(_WEIGHTS, encoding="utf-8")
    return Settings(
        provider=ProviderSettings(
            kind="llamacpp",
            model_directory=str(_directory(_MODELS_ENV)),
            state_dir=str(tmp_path / "state"),
            timeout_seconds=900.0,
        ),
        adapters=AdapterSettings(directory=str(_directory(_ADAPTERS_ENV))),
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

    base_name = os.environ.get(_BASE_ENV, "").strip()
    if not base_name:
        pytest.skip(f"{_BASE_ENV} is not set; it must name the base GGUF (without .gguf)")

    url = f"sqlite:///{tmp_path / 'a2.sqlite3'}"
    engine = create_engine_for(url)
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()

    database = Database.from_url(url)
    provider = build_provider(settings.provider, adapters=settings.adapters)
    collector = build_collector()
    discover_models(database, provider, now=datetime.now(UTC))
    return {
        "database": database,
        "provider": provider,
        "collector": collector,
        "registry": build_registry(),
        "settings": settings,
        "model_ref": base_name,
        "entries": read_entries(settings.adapters),
    }


def _measure(journey: Any, *, suite: str, adapter: str | None) -> str:
    from freeweight.services.adapters import serving_mode
    from freeweight.services.evidence import recompute_for_run
    from freeweight.services.runs import ExecutionConfig, create_run
    from freeweight.services.scheduler import RunScheduler

    settings = journey["settings"]
    cap = os.environ.get(_MAX_OUTPUT_ENV, "").strip()
    summary = create_run(
        journey["database"],
        journey["provider"],
        journey["collector"],
        journey["registry"],
        model_ref=journey["model_ref"],
        suite_key=suite,
        execution=ExecutionConfig.resolve(
            settings.execution,
            measured_repetitions=1,
            max_output_tokens=int(cap) if cap else None,
        ),
        runtime_profile=settings.runtime.to_profile(
            adapters_registered=serving_mode(journey["provider"], journey["entries"])
        ),
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


def test_the_regression_panel_meets_a_real_adapter(journey: Any) -> None:
    """Run rows 1 and 2 of the fixed panel on the base and on one adapter, and print both."""
    from freeweight.domain.panels import FIXED_REGRESSION_SUITES
    from freeweight.infrastructure.db.repositories.models import ModelRepository
    from freeweight.services.adapters import measured_scores, resolve_subject
    from freeweight.services.evidence import EvidenceQuery, query_evidence

    adapter = os.environ.get(_ADAPTER_ENV, "terse").strip()
    database = journey["database"]
    entries = journey["entries"]
    if not any(entry.name == adapter for entry in entries):
        pytest.skip(f"no adapter named {adapter!r} in the configured directory")

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

    for suite in FIXED_REGRESSION_SUITES:
        _measure(journey, suite=suite, adapter=None)
        _measure(journey, suite=suite, adapter=adapter)

    base = measured_scores(database, resolve_subject(model, entries, None))
    subject = measured_scores(database, resolve_subject(model, entries, adapter))
    samples = {
        (record.subject_canonical_id, record.capability_id): record.sample_count
        for record in query_evidence(database, EvidenceQuery()).records
    }
    base_id = resolve_subject(model, entries, None).canonical_id
    subject_id = resolve_subject(model, entries, adapter).canonical_id

    print(  # noqa: T201 — the evidence
        f"\nA-2 regression panel v1, {journey['model_ref']} vs +{adapter}:"
    )
    for capability in sorted(set(base) | set(subject)):
        before = base.get(capability)
        after = subject.get(capability)
        delta = "—" if before is None or after is None else f"{after - before:+.3f}"
        # The sample count is printed beside every number, because a delta of one case in eleven
        # is a statement about this suite's resolution and not about this adapter
        # (ADR-0016 rule 6).
        print(  # noqa: T201 — the evidence
            f"  {capability:<24} base {before if before is None else f'{before:.3f}'}"
            f" (n={samples.get((base_id, capability))})"
            f"   {adapter} {after if after is None else f'{after:.3f}'}"
            f" (n={samples.get((subject_id, capability))})   delta {delta}"
        )

    # The panel must be able to *tell*. It is not asserted which way the answer comes out.
    assert base, "the bare base produced no regression-panel score"
    assert subject, "the adapter subject produced no regression-panel score of its own"
    assert set(base) == set(subject), (
        "the two subjects were scored on different capabilities, so nothing is comparable"
    )


def test_the_panel_still_separates_a_known_damaged_adapter(journey: Any) -> None:
    """The end-to-end guard the unit tests cannot give: is the adapter actually being *served*?

    `test_runtime_profile.py` asserts that the run's adapter reaches the request. That is one
    claim; whether the provider then applies it is another, and the gap between them is where row
    H6's defect lived for two rows — `_build_request` dropped the adapter, every adapter subject
    measured the bare base, and the numbers looked plausible because they *were* numbers, just the
    base's. No fake can catch that, because the fake is the thing being bypassed.

    This can: one adapter that is damaged on purpose, whose fluent nonsense is unmistakable to a
    scorer. If the panel stops separating it from the base, something between the run and the GPU
    has stopped applying adapters — whatever that something turns out to be.

    Skipped unless `FWTEST_A2_DAMAGED_ADAPTER` names a deliberately damaged adapter in the
    configured directory: the artefact is produced outside the suite (ADR-0061 rule 6), so this
    test states its dependency rather than assuming the machine has one.
    """
    from freeweight.domain.panels import FIXED_REGRESSION_SUITES
    from freeweight.infrastructure.db.repositories.models import ModelRepository
    from freeweight.services.adapters import measured_scores, resolve_subject

    damaged = os.environ.get(_DAMAGED_ENV, "").strip()
    if not damaged:
        pytest.skip(f"{_DAMAGED_ENV} is not set; it must name a deliberately damaged adapter")
    entries = journey["entries"]
    if not any(entry.name == damaged for entry in entries):
        pytest.skip(f"no adapter named {damaged!r} in the configured directory")

    with journey["database"].read() as session:
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

    for suite in FIXED_REGRESSION_SUITES:
        _measure(journey, suite=suite, adapter=None)
        _measure(journey, suite=suite, adapter=damaged)

    base = measured_scores(journey["database"], resolve_subject(model, entries, None))
    broken = measured_scores(journey["database"], resolve_subject(model, entries, damaged))

    print(f"\ndamaged-adapter canary, {journey['model_ref']} vs +{damaged}:")  # noqa: T201
    for capability in sorted(set(base) | set(broken)):
        before, after = base.get(capability), broken.get(capability)
        print(  # noqa: T201
            f"  {capability:<24} base {'—' if before is None else f'{before:.3f}'}"
            f"   {damaged} {'—' if after is None else f'{after:.3f}'}"
            f"   delta {'—' if before is None or after is None else f'{after - before:+.3f}'}"
        )

    assert base and broken, "both subjects must score for the comparison to mean anything"
    worst = min(broken[c] - base[c] for c in sorted(set(base) & set(broken)))
    assert worst <= -_DAMAGED_SEPARATION_FLOOR, (
        f"the panel no longer separates a deliberately damaged adapter: its worst drop against "
        f"the base is {worst:+.3f}, and anything above {-_DAMAGED_SEPARATION_FLOOR:+.3f} means "
        "the adapter is probably not reaching the weights at all (row H6)"
    )
