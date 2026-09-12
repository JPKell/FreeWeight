"""The runtime profile: settable, sent, recorded, and honest when it was only assumed.

ADR-0023 says a run resolves an explicit profile, sends it, and records the context it was served
at with the source of that number. Three of those four were true of the record and false of the
request until this was fixed — the profile was stored and hashed into the reproducibility
fingerprint and then never given to the provider — so these tests pin each link of the chain
separately:

* ``[runtime]`` produces a profile, and a differing context produces a differing hash, which is
  what makes two contexts two subjects rather than two indistinguishable runs (ADR-0017);
* ``_build_request`` carries the profile onto the wire, which is the link that was missing;
* the observed context is recorded as a measurement, and where it contradicts an *assumed* one the
  run says so as a degradation rather than by rewriting a frozen fingerprint.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest
from baseaicore import UNSUPPORTED, ModelIdentity, ProviderKind, RuntimeProfile
from modelrack import Message, Role

from freeweight.config import ExecutionSettings, RuntimeSettings
from freeweight.domain.benchmark import BenchmarkCase
from freeweight.services.runs import (
    ExecutionConfig,
    _build_request,
    _context_divergence,
    _provider_request,
    _residency_rows,
    _RunContext,
)

_IDENTITY = ModelIdentity(
    provider_kind=ProviderKind.OLLAMA, provider_model_name="qwen3:8b", artifact_digest=None
)


def _context(**changes: Any) -> _RunContext:
    """A run context with only the fields these tests read."""
    base = {
        "run_id": "01" + "R" * 24,
        "suite_key": "native.echo",
        "suite_id": "01" + "S" * 24,
        "config": ExecutionConfig.resolve(ExecutionSettings()),
        "identity": _IDENTITY,
        "model_canonical_id": "ollama/qwen3:8b",
        "served_context": 40960,
        "served_context_source": "assumed",
        "gpu_index": 0,
        "multi_gpu_visible": False,
    }
    return _RunContext(**{**base, **changes})


class TestTheSettingsProduceAProfile:
    def test_an_unset_section_is_provider_defaults_not_an_absence(self) -> None:
        """ADR-0023 §1: ``RuntimeProfile()`` is a legal, hashable profile."""
        profile = RuntimeSettings().to_profile(provider_kind="ollama")

        assert profile.context_size is None
        assert profile.profile_hash

    def test_two_contexts_are_two_profiles_with_two_hashes(self) -> None:
        """The property that makes a context comparison possible at all (ADR-0017)."""
        small = RuntimeSettings(context_size=2048).to_profile(provider_kind="ollama")
        large = RuntimeSettings(context_size=8192).to_profile(provider_kind="ollama")

        assert small.context_size == 2048  # noqa: PLR2004 — the value under test
        assert small.profile_hash != large.profile_hash
        assert (
            small.profile_hash != RuntimeSettings().to_profile(provider_kind="ollama").profile_hash
        )

    def test_llamacpp_gets_fit_off_and_ollama_gets_no_option_at_all(self) -> None:
        """ADR-0121 §2: ``--fit off`` is a llama.cpp launch flag, hashed; Ollama never sees it."""
        settings = RuntimeSettings(context_size=8192)
        llamacpp = settings.to_profile(provider_kind="llamacpp")
        ollama = settings.to_profile(provider_kind="ollama")
        assert llamacpp.provider_options == {"--fit": "off"}
        assert ollama.provider_options == {}
        assert llamacpp.profile_hash != ollama.profile_hash
        # An Ollama profile hashes exactly as it did before the keys existed: stored hashes stand.
        assert ollama.profile_hash == RuntimeProfile(context_size=8192).profile_hash

    def test_fit_to_device_true_is_llama_servers_own_default_and_adds_nothing(self) -> None:
        profile = RuntimeSettings(fit_to_device=True).to_profile(provider_kind="llamacpp")
        assert profile.provider_options == {}

    def test_kv_precision_and_flash_attention_reach_the_profile_and_separate_it(self) -> None:
        """ADR-0120 rule 5: a different precision is a different subject."""
        f16 = RuntimeSettings(flash_attention=True).to_profile(provider_kind="llamacpp")
        q8 = RuntimeSettings(flash_attention=True, kv_cache_precision="q8_0").to_profile(
            provider_kind="llamacpp"
        )
        assert q8.kv_cache_precision == "q8_0"
        assert q8.flash_attention is True
        assert f16.profile_hash != q8.profile_hash

    @pytest.mark.parametrize("precision", ["q8_0", "q4_0"])
    def test_a_quantized_cache_without_flash_attention_is_refused(
        self, precision: Literal["q8_0", "q4_0"]
    ) -> None:
        """ADR-0120 rule 3: llama.cpp would silently serve f16."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError, match="flash_attention"):
            RuntimeSettings(kv_cache_precision=precision)
        with pytest.raises(PydanticValidationError, match="flash_attention"):
            RuntimeSettings(kv_cache_precision=precision, flash_attention=False)

    @pytest.mark.parametrize(
        "settings",
        [RuntimeSettings(flash_attention=True), RuntimeSettings(kv_cache_precision="f16")],
    )
    def test_ollama_refuses_the_two_launch_settings_by_name(
        self, settings: RuntimeSettings
    ) -> None:
        """ADR-0120 rule 4: a profile claiming a daemon-wide setting is a fabricated subject."""
        from baseaicore import ConfigurationError

        with pytest.raises(ConfigurationError) as caught:
            settings.to_profile(provider_kind="ollama")
        assert caught.value.details["field"] in {
            "runtime.flash_attention",
            "runtime.kv_cache_precision",
        }
        assert settings.to_profile(provider_kind="llamacpp").profile_hash

    def test_a_context_of_zero_is_refused_rather_than_stored(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            RuntimeSettings(context_size=0)


class TestTheProfileReachesTheProvider:
    """The link that was missing: a profile stored but never sent describes a run that did not
    happen."""

    def test_the_request_carries_the_context(self) -> None:
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="hello")

        request = _build_request(
            _IDENTITY,
            case,
            ExecutionConfig.resolve(ExecutionSettings()),
            RuntimeProfile(context_size=8192),
        )

        assert request.runtime_profile.context_size == 8192  # noqa: PLR2004 — the value under test

    def test_the_request_carries_the_adapter(self) -> None:
        """The same link, for the subject's second axis: a run that stores an adapter, hashes it
        into its fingerprint and never asks the provider to apply it measures the bare base and
        files the numbers under the adapter (risk T12)."""
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="hello")

        request = _build_request(
            _IDENTITY,
            case,
            ExecutionConfig.resolve(ExecutionSettings()),
            RuntimeProfile(),
            "terse",
        )

        assert request.adapter == "terse"

    def test_no_adapter_leaves_the_request_byte_identical_to_a_bare_base_call(self) -> None:
        """``None`` is the bare base, and is what every pre-1.1 run meant (ADR-0058)."""
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="hello")

        request = _build_request(_IDENTITY, case, ExecutionConfig.resolve(ExecutionSettings()))

        assert request.adapter is None

    def test_no_profile_still_produces_the_defaults_profile(self) -> None:
        """Never ``None``: ModelRack's request type has no "no profile" state either."""
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="hello")

        request = _build_request(_IDENTITY, case, ExecutionConfig.resolve(ExecutionSettings()))

        assert request.runtime_profile == RuntimeProfile()

    def test_the_context_becomes_the_providers_own_option(self) -> None:
        """End of the chain: ModelRack translates ``context_size`` to Ollama's ``num_ctx``."""
        from modelrack.providers._ollama_wire import generation_options

        options = generation_options(
            temperature=None,
            top_p=None,
            top_k=None,
            seed=None,
            max_output_tokens=None,
            stop=(),
            repeat_penalty=None,
            context_size=8192,
            gpu_layers=None,
            threads=None,
            batch_size=None,
            provider_options={},
        )

        assert options["num_ctx"] == 8192  # noqa: PLR2004 — the value under test


class TestTheContextSurvivesTheServingMode:
    """WP6 finding 4 read two runs, one bare and one under an adapter, both recording 8 192 while
    the provider reported 32 768. ``--lora`` and ``--ctx-size`` are independent flags, and the
    profile that carries one carries the other."""

    @pytest.mark.parametrize("registered", [None, False, True])
    def test_the_configured_context_becomes_ctx_size_in_every_serving_mode(
        self, registered: bool | None
    ) -> None:
        from modelrack.providers._llamacpp_wire import launch_flags  # noqa: PLC2701 — the argv

        profile = RuntimeSettings(context_size=8192).to_profile(
            provider_kind="llamacpp", adapters_registered=registered
        )

        flags = launch_flags(profile)
        assert profile.adapters_registered is registered
        assert flags[:2] == ("--ctx-size", "8192")


class TestOneRunIsOneArgv:
    """Row WPF10: every provider call of a run builds its request in one place.

    Under llama.cpp the runtime profile *is* the server's command line, and ModelRack keys its
    supervised server on those flags: a request stating a different profile does not borrow the
    run's server, it restarts it under its own flags. A second request builder was therefore a
    second argv — which is what WPF2's Gate C caught live, two servers in one sitting, one with
    ``--ctx-size 8192 --fit off`` and one with neither, answering ``/props`` with 32 768.
    """

    def test_an_interaction_turn_carries_the_same_argv_as_a_single_call(self) -> None:
        """The defect, at the level below the engine: the turn's profile is the run's profile."""
        from modelrack.providers._llamacpp_wire import launch_flags  # noqa: PLC2701 — the argv

        config = ExecutionConfig.resolve(ExecutionSettings())
        profile = RuntimeSettings(context_size=8192).to_profile(provider_kind="llamacpp")
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="hello")

        single = _build_request(_IDENTITY, case, config, profile, "terse")
        turn = _provider_request(
            identity=_IDENTITY,
            messages=[Message(role=Role.USER, content="hello")],
            config=config,
            runtime_profile=profile,
            adapter_name="terse",
            tools=(),
        )

        assert launch_flags(turn.runtime_profile) == launch_flags(single.runtime_profile)
        assert launch_flags(turn.runtime_profile) == ("--ctx-size", "8192", "--fit", "off")
        assert turn.adapter == single.adapter == "terse"

    def test_the_run_engine_has_exactly_one_request_builder(self) -> None:
        """A structural guard, because the defect was a *second* builder, not a wrong value.

        Reading it back from the source is crude and it is the only check that fails when someone
        adds a third call path and hand-rolls its request — the failure mode this row exists to
        close. A new path calls :func:`_provider_request`; nothing else constructs the type.
        """
        import inspect

        from freeweight.services import runs

        source = inspect.getsource(runs)
        assert source.count("GenerationRequest(") == 1


class TestWhatWasObservedIsRecorded:
    def test_residency_rows_carry_their_units(self) -> None:
        rows = _residency_rows(
            _context(
                model_vram_bytes=5_274_117_078,
                model_total_bytes=5_274_117_078,
                observed_context=2048,
            ),
            run_id="01" + "R" * 24,
            now=_now(),
        )

        by_key = {row["metric_key"]: row for row in rows}
        assert by_key["model_vram_bytes"]["unit"] == "bytes"
        assert by_key["served_context_observed"]["unit"] == "tokens"
        assert by_key["served_context_observed"]["numeric_value"] == 2048  # noqa: PLR2004

    def test_a_provider_that_reports_nothing_produces_no_rows(self) -> None:
        """Not a row of zeroes, and not a row saying "unsupported" either: environment metrics are
        emitted only when measured, exactly as telemetry is."""
        assert _residency_rows(_context(), run_id="01" + "R" * 24, now=_now()) == []


class TestAnAssumedContextThatWasWrongSaysSo:
    def test_a_disagreement_is_recorded_as_a_degradation(self) -> None:
        degradations = _context_divergence(_context(served_context=262144, observed_context=112128))

        assert len(degradations) == 1
        detail = degradations[0].detail
        assert degradations[0].kind == "served_context_assumed_incorrectly"
        assert detail["recorded_served_context"] == 262144  # noqa: PLR2004
        assert detail["observed_served_context"] == 112128  # noqa: PLR2004
        assert "--context-size" in detail["explanation"]

    def test_agreement_is_silent(self) -> None:
        assert _context_divergence(_context(served_context=40960, observed_context=40960)) == []

    def test_nothing_observed_is_silent(self) -> None:
        """An unobservable context is not evidence that the assumption was wrong."""
        assert _context_divergence(_context(observed_context=UNSUPPORTED)) == []

    def test_nothing_recorded_is_silent(self) -> None:
        assert _context_divergence(_context(served_context=None, observed_context=2048)) == []

    def test_a_configured_context_that_disagrees_is_not_explained_as_an_assumption(self) -> None:
        """WP6 finding 4: two runs with ``[runtime] context_size = 8192`` were told they had
        assumed it "because nothing requested one", with 32 768 read from the provider.

        The degradation still fires — it is the louder case, not a quieter one — and it says which
        case it is, because on a memory-capped machine a KV cache four times the recorded size is
        the difference between fitting and spilling."""
        degradations = _context_divergence(
            _context(
                served_context=8192, served_context_source="configured", observed_context=32768
            )
        )

        assert len(degradations) == 1
        detail = degradations[0].detail
        assert detail["recorded_served_context_source"] == "configured"
        assert "assumed" not in detail["explanation"]
        assert "nothing requested one" not in detail["explanation"]
        assert "8192" in detail["explanation"]
        assert "32768" in detail["explanation"]


def _now() -> Any:  # noqa: ANN401 — a datetime
    from datetime import UTC, datetime

    return datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
