"""A run measures an adapter subject, and evidence never crosses between subjects.

Phase 15. The single failure this phase exists to prevent is a join that quietly attributes an
adapter's measurements to its base, or a base's to an adapter
([ADR-0058 §4](../../docs/adr/0058-the-execution-subject-gains-an-adapter-axis.md),
[ADR-0059](../../docs/adr/0059-adapter-evidence-is-measured-never-inherited.md)). It looks exactly
like a working query, so it is asserted here rather than reviewed for.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import DataClassification, IdentityConfidence

from freeweight.config import EvidenceSettings, ExecutionSettings, Settings
from freeweight.domain.subjects import IncompatibleAdapter
from freeweight.infrastructure.adapters import AdapterEntry
from freeweight.services.evidence import Subject, recompute_evidence, subject_of_run
from freeweight.services.runs import ExecutionConfig, create_run

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import RunEnvironment

_ADAPTER_DIGEST = "sha256:" + "c3" * 32
_SIBLING_DIGEST = "sha256:" + "d4" * 32


def _adapter_capable() -> Any:
    """A `FakeScript` declaring `adapter_hot_swap`, for the tests that measure a subject.

    ``FULL_CAPABILITIES`` leaves the flag ``False`` on purpose: the fake is where a consumer meets
    the *refusal* path (ADR-0062 decision 5), and that refusal is asserted here too
    (:class:`TestAnAdapterNeedsAProviderThatCanServeOne`). But a run under ``--adapter`` now sends
    the adapter to the provider, so a test that wants an adapter subject *measured* has to give
    the fake the flag — otherwise it is asserting a property of a run that never happened, which
    is exactly the defect row H6 found.
    """
    from dataclasses import replace

    from modelrack.testing import FULL_CAPABILITIES, FakeScript

    return FakeScript(capabilities=replace(FULL_CAPABILITIES, adapter_hot_swap=True))


def _entry(env: RunEnvironment, name: str, *, artifact_digest: str) -> AdapterEntry:
    """An entry declaring the fake provider's own model as its base, proved by digest."""
    identity = env.provider.list_models()[0].identity
    return AdapterEntry(
        name=name,
        manifest_path=Path(f"/adapters/{name}.manifest.json"),
        artifact_path=Path(f"/adapters/{name}.gguf"),
        artifact_sha256=artifact_digest,
        source_sha256=None,
        base_model_name=identity.provider_model_name,
        base_artifact_digest=identity.artifact_digest,
        base_confidence=IdentityConfidence.DIGEST,
        declared_capabilities=("instruction_following",),
        data_classification=DataClassification.INTERNAL,
        notes=None,
        available=True,
    )


def _start(env: RunEnvironment, *, adapter: str | None, entries: tuple[AdapterEntry, ...]) -> Any:
    """Queue one run of the echo suite, optionally under an adapter."""
    return create_run(
        env.database,
        env.provider,
        env.collector,
        env.registry,
        model_ref=env.model_ref,
        suite_key="native.echo",
        execution=ExecutionConfig.resolve(
            ExecutionSettings(warmup_repetitions=0, cooldown_seconds=0), measured_repetitions=1
        ),
        adapter_name=adapter,
        adapter_entries=entries,
    )


class TestARunRecordsItsSubject:
    """`--adapter` is a named axis on the run, not a runtime setting."""

    def test_a_run_with_no_adapter_records_none(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        """Every run before Phase 15 was this, and it still means what it always meant."""
        env = run_environment(script=_adapter_capable())

        summary = _start(env, adapter=None, entries=())

        assert subject_of_run(env.database, summary.id).adapter_id is None

    def test_a_run_under_an_adapter_records_it_and_creates_the_row(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)

        summary = _start(env, adapter="terse", entries=entries)

        subject = subject_of_run(env.database, summary.id)
        assert subject.adapter_id is not None
        from freeweight.infrastructure.db.models import Adapter

        with env.database.read() as session:
            row = session.get(Adapter, subject.adapter_id)
            assert row is not None
            assert row.name == "terse"
            assert row.artifact_sha256 == _ADAPTER_DIGEST

    def test_two_runs_under_one_adapter_share_one_row(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        """Keyed on the artifact digest, so a second run does not make a second subject."""
        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)

        first = _start(env, adapter="terse", entries=entries)
        second = _start(env, adapter="terse", entries=entries)

        assert (
            subject_of_run(env.database, first.id).adapter_id
            == subject_of_run(env.database, second.id).adapter_id
        )

    def test_an_unknown_adapter_is_refused_and_creates_no_run(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        """Validation happens before anything is written (api.md §4)."""
        env = run_environment(script=_adapter_capable())
        from freeweight.infrastructure.db.models_runs import Run

        with pytest.raises(IncompatibleAdapter):
            _start(env, adapter="nope", entries=())

        with env.database.read() as session:
            assert session.query(Run).count() == 0

    def test_an_adapter_for_another_base_is_refused(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        env = run_environment(script=_adapter_capable())
        wrong = _entry(env, "wrong", artifact_digest=_SIBLING_DIGEST)
        wrong = AdapterEntry(
            **{
                **{field: getattr(wrong, field) for field in wrong.__slots__},
                "base_artifact_digest": "sha256:" + "ee" * 32,
            }
        )

        with pytest.raises(IncompatibleAdapter):
            _start(env, adapter="wrong", entries=(wrong,))

    def test_a_provider_that_cannot_hot_swap_refuses_the_run_rather_than_measuring_the_base(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        """ADR-0058 §5's second half, and the defect row H6 found, asserted from the outside.

        The default fake declares no ``adapter_hot_swap``. A run under ``--adapter`` against it
        used to be created, execute, and record the **bare base's** numbers under an
        adapter-bearing subject — because the adapter never reached the provider at all. It is now
        refused where it is created, and nothing is written.
        """
        from freeweight.infrastructure.db.models_runs import Run

        env = run_environment()
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)

        with pytest.raises(IncompatibleAdapter) as caught:
            _start(env, adapter="terse", entries=entries)

        assert "does not support LoRA adapters" in str(caught.value)
        with env.database.read() as session:
            assert session.query(Run).count() == 0


class TestEvidenceIsNeverInherited:
    """The subject is the aggregation key, so no join exists that could cross subjects."""

    def test_a_base_and_an_adapter_subject_are_different_subjects(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)

        base_run = _start(env, adapter=None, entries=entries)
        adapter_run = _start(env, adapter="terse", entries=entries)

        base_subject = subject_of_run(env.database, base_run.id)
        adapter_subject = subject_of_run(env.database, adapter_run.id)
        assert base_subject != adapter_subject
        assert base_subject.model_id == adapter_subject.model_id
        assert base_subject.adapter_id is None
        assert adapter_subject.adapter_id is not None

    def test_two_adapters_on_one_base_are_two_subjects(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        """A sibling adapter's evidence is no more the subject's than the base's is."""
        env = run_environment(script=_adapter_capable())
        entries = (
            _entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),
            _entry(env, "pirate", artifact_digest=_SIBLING_DIGEST),
        )

        terse = subject_of_run(env.database, _start(env, adapter="terse", entries=entries).id)
        pirate = subject_of_run(env.database, _start(env, adapter="pirate", entries=entries).id)

        assert terse.adapter_id != pirate.adapter_id

    def test_recomputing_a_base_subject_never_reads_an_adapter_subjects_runs(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        """`adapter_id IS NULL`, never "any adapter" — the loose filter is the whole bug."""
        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
        _start(env, adapter=None, entries=entries)
        adapter_run = _start(env, adapter="terse", entries=entries)
        adapter_id = subject_of_run(env.database, adapter_run.id).adapter_id

        report = recompute_evidence(
            env.database,
            subject=Subject(
                model_id=subject_of_run(env.database, adapter_run.id).model_id,
                runtime_profile_id=subject_of_run(env.database, adapter_run.id).runtime_profile_id,
                machine_id=subject_of_run(env.database, adapter_run.id).machine_id,
                adapter_id=None,
            ),
        )

        assert all(record.adapter_id != adapter_id for record in report.emitted)
        assert all(not record.is_adapter_bearing for record in report.emitted)


class TestThePanelIsMeasuredNeverInherited:
    """ADR-0059's rule 1, asserted through the real service rather than reviewed for."""

    def test_a_fresh_subjects_evidence_is_empty_even_when_its_base_is_measured(
        self,
        run_environment: Callable[..., RunEnvironment],
        evidence_settings: EvidenceSettings,
    ) -> None:
        """The failure this phase exists to prevent, and it looks exactly like a working join."""
        from freeweight.services.adapters import measured_scores, resolve_subject

        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
        _run_to_completion(env, adapter=None, entries=entries, settings=evidence_settings)

        base = resolve_subject(_model_row(env), entries, None)
        adapter = resolve_subject(_model_row(env), entries, "terse")

        assert measured_scores(env.database, base), "the base must have evidence for this to test"
        assert measured_scores(env.database, adapter) == {}

    def test_measuring_the_adapter_does_not_move_the_bases_evidence(
        self,
        run_environment: Callable[..., RunEnvironment],
        evidence_settings: EvidenceSettings,
    ) -> None:
        """Recomputing one subject must not rewrite another's rows (replace_for_subject)."""
        from freeweight.services.adapters import measured_scores, resolve_subject

        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
        _run_to_completion(env, adapter=None, entries=entries, settings=evidence_settings)
        base = resolve_subject(_model_row(env), entries, None)
        before = measured_scores(env.database, base)

        _run_to_completion(env, adapter="terse", entries=entries, settings=evidence_settings)

        assert measured_scores(env.database, base) == before
        assert measured_scores(env.database, resolve_subject(_model_row(env), entries, "terse"))

    def test_the_panel_is_declared_plus_regression_plus_performance(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        from freeweight.domain.panels import FIXED_REGRESSION_SUITES, PERFORMANCE_SUITE
        from freeweight.services.adapters import panel_for, resolve_subject
        from freeweight.services.evidence import load_capability_mapping

        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
        subject = resolve_subject(_model_row(env), entries, "terse")

        composed = panel_for(env.database, subject, mapping=load_capability_mapping())

        assert [part.name for part in composed.panel.parts] == [
            "declared",
            "regression",
            "performance",
        ]
        assert composed.panel.part("regression").suites[:2] == FIXED_REGRESSION_SUITES
        assert composed.panel.part("performance").suites == (PERFORMANCE_SUITE,)
        assert composed.measured == {}
        assert not composed.has_evidence

    def test_the_regression_panels_third_row_comes_from_the_base(
        self,
        run_environment: Callable[..., RunEnvironment],
        evidence_settings: EvidenceSettings,
        tmp_path: Path,
    ) -> None:
        """The base's evidence chooses a suite to *run*; it never becomes the subject's score."""
        from freeweight.services.adapters import panel_for, resolve_subject
        from freeweight.services.evidence import load_capability_mapping

        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
        _run_to_completion(env, adapter=None, entries=entries, settings=evidence_settings)
        subject = resolve_subject(_model_row(env), entries, "terse")

        composed = panel_for(
            env.database,
            subject,
            mapping=load_capability_mapping(tmp_path / "weights.toml"),
        )

        assert composed.panel.regression_third_row is not None
        assert composed.measured == {}, "the base's evidence must not become the subject's"


def _model_row(env: RunEnvironment) -> Any:
    """The one discovered model, as the identity fields a subject needs."""
    from freeweight.infrastructure.db.repositories.models import ModelRepository

    with env.database.read() as session:
        row = ModelRepository().get_by_canonical_id(session, env.model_ref)
        assert row is not None
        return type(
            "Row",
            (),
            {
                "provider_kind": row.provider_kind,
                "provider_model_name": row.provider_model_name,
                "artifact_digest": row.artifact_digest,
            },
        )()


_WEIGHTS = """
version = "test"

[capabilities.reliability]
sources = [
  { suite = "native.echo", metric_key = "harness_roundtrip_success", weight = 1.0 },
]
"""


@pytest.fixture
def evidence_settings(tmp_path: Path) -> EvidenceSettings:
    """A mapping under which the echo suite produces one `reliability` record."""
    weights = tmp_path / "weights.toml"
    weights.write_text(_WEIGHTS, encoding="utf-8")
    return EvidenceSettings(capability_weights_path=str(weights))


def _run_to_completion(
    env: RunEnvironment,
    *,
    adapter: str | None,
    entries: tuple[AdapterEntry, ...],
    settings: EvidenceSettings,
) -> str:
    """Start one run, execute it, and recompute the evidence it supports."""
    from freeweight.services.evidence import recompute_for_run
    from freeweight.services.scheduler import RunScheduler

    summary = _start(env, adapter=adapter, entries=entries)
    RunScheduler(
        env.database,
        env.provider,
        registry=env.registry,
        settings=Settings(evidence=settings),
    ).run_once()
    recompute_for_run(env.database, summary.id, settings=settings)
    return str(summary.id)


class TestServingModeIsSeparableFromSelection:
    """ADR-0060: which adapter ran is the subject; whether any were registered is the profile."""

    def test_the_two_arms_hash_differently(self) -> None:
        """No new comparison mechanism is needed, because the arms already separate (ADR-0074)."""
        from freeweight.config import RuntimeSettings

        runtime = RuntimeSettings()
        clean = runtime.to_profile(adapters_registered=False)
        registered = runtime.to_profile(adapters_registered=True)

        assert clean.profile_hash != registered.profile_hash

    def test_an_unstated_serving_mode_keeps_the_hash_it_always_had(self) -> None:
        """An ordinary run's profile hash must not move at 1.1 (ADR-0074, additive)."""
        from baseaicore import RuntimeProfile

        from freeweight.config import RuntimeSettings

        assert RuntimeSettings().to_profile().profile_hash == RuntimeProfile().profile_hash

    def test_two_runs_in_the_two_arms_are_two_subjects_of_one_base(
        self, run_environment: Callable[..., RunEnvironment]
    ) -> None:
        from freeweight.config import RuntimeSettings
        from freeweight.services.runs import get_run

        env = run_environment(script=_adapter_capable())
        runs = [
            create_run(
                env.database,
                env.provider,
                env.collector,
                env.registry,
                model_ref=env.model_ref,
                suite_key="native.echo",
                execution=ExecutionConfig.resolve(
                    ExecutionSettings(warmup_repetitions=0, cooldown_seconds=0),
                    measured_repetitions=1,
                ),
                runtime_profile=RuntimeSettings().to_profile(adapters_registered=registered),
            )
            for registered in (False, True)
        ]

        subjects = [subject_of_run(env.database, run.id) for run in runs]
        assert subjects[0].model_id == subjects[1].model_id
        assert subjects[0].runtime_profile_id != subjects[1].runtime_profile_id
        assert all(subject.adapter_id is None for subject in subjects), (
            "the A/B measures the base; neither arm is an adapter subject"
        )
        assert all(get_run(env.database, run.id).run is not None for run in runs)


class TestTheComparisonGrouping:
    """Subjects sit under their base, side by side, and are never merged."""

    def test_subjects_group_under_one_base_with_the_bare_base_first(
        self,
        run_environment: Callable[..., RunEnvironment],
        evidence_settings: EvidenceSettings,
    ) -> None:
        from freeweight.services.evidence import EvidenceQuery, group_by_base, query_evidence

        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
        _run_to_completion(env, adapter=None, entries=entries, settings=evidence_settings)
        _run_to_completion(env, adapter="terse", entries=entries, settings=evidence_settings)

        groups = group_by_base(query_evidence(env.database, EvidenceQuery()).records)

        assert len(groups) == 1
        group = groups[0]
        assert group.adapter_count == 1
        assert [subject.adapter_name for subject in group.subjects] == [None, "terse"]
        assert all(subject.record_count > 0 for subject in group.subjects)
        assert len({subject.canonical_id for subject in group.subjects}) == 2

    def test_a_base_with_more_subjects_than_the_cap_says_how_many_are_not_shown(
        self,
        run_environment: Callable[..., RunEnvironment],
        evidence_settings: EvidenceSettings,
    ) -> None:
        """The grouping is bounded, and states what it withheld rather than dropping it.

        A dozen subjects render; a hundred do not, and a silently truncated table is worse than a
        long one because a reader cannot tell it is looking at part of the answer.
        """
        from freeweight.services.evidence import EvidenceQuery, group_by_base, query_evidence

        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
        _run_to_completion(env, adapter=None, entries=entries, settings=evidence_settings)
        _run_to_completion(env, adapter="terse", entries=entries, settings=evidence_settings)

        records = query_evidence(env.database, EvidenceQuery()).records
        group = group_by_base(records, max_subjects=1)[0]

        assert len(group.subjects) == 1
        assert group.subjects[0].adapter_name is None, "the bare base is never the one hidden"
        assert group.hidden_subject_count == 1
        assert group.adapter_count == 1, "the count reports every adapter, shown or not"

    def test_an_unmeasured_subject_contributes_no_row_rather_than_the_bases(
        self,
        run_environment: Callable[..., RunEnvironment],
        evidence_settings: EvidenceSettings,
    ) -> None:
        """The grouping counts each subject's own records and sums nothing across them."""
        from freeweight.services.evidence import EvidenceQuery, group_by_base, query_evidence

        env = run_environment(script=_adapter_capable())
        entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
        _run_to_completion(env, adapter=None, entries=entries, settings=evidence_settings)

        groups = group_by_base(query_evidence(env.database, EvidenceQuery()).records)

        assert [subject.adapter_name for subject in groups[0].subjects] == [None]
        assert groups[0].adapter_count == 0
