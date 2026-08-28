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

from typing import Any

import pytest
from baseaicore import UNSUPPORTED, ModelIdentity, ProviderKind, RuntimeProfile

from freeweight.config import ExecutionSettings, RuntimeSettings
from freeweight.domain.benchmark import BenchmarkCase
from freeweight.services.runs import (
    ExecutionConfig,
    _build_request,
    _context_divergence,
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
        "gpu_index": 0,
        "multi_gpu_visible": False,
    }
    return _RunContext(**{**base, **changes})


class TestTheSettingsProduceAProfile:
    def test_an_unset_section_is_provider_defaults_not_an_absence(self) -> None:
        """ADR-0023 §1: ``RuntimeProfile()`` is a legal, hashable profile."""
        profile = RuntimeSettings().to_profile()

        assert profile.context_size is None
        assert profile.profile_hash

    def test_two_contexts_are_two_profiles_with_two_hashes(self) -> None:
        """The property that makes a context comparison possible at all (ADR-0017)."""
        small = RuntimeSettings(context_size=2048).to_profile()
        large = RuntimeSettings(context_size=8192).to_profile()

        assert small.context_size == 2048  # noqa: PLR2004 — the value under test
        assert small.profile_hash != large.profile_hash
        assert small.profile_hash != RuntimeSettings().to_profile().profile_hash

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


def _now() -> Any:  # noqa: ANN401 — a datetime
    from datetime import UTC, datetime

    return datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
