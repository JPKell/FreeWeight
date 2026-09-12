"""Unit tests for freeweight.infrastructure.providers.factory.

Also carries Phase 3's boundary test — acceptance criterion 2: "FreeWeight contains no provider
HTTP code (asserted)" — by scanning every source file for an ``httpx`` import outside this one
module, the only place in the application allowed to reach it.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from baseaicore import ConfigurationError
from modelrack.errors import CapabilityUnsupported
from modelrack.providers.fake import FakeProvider
from modelrack.providers.llamacpp import LlamaCppProvider
from modelrack.providers.ollama import OllamaProvider

from freeweight.config import AdapterSettings, ProviderSettings, load_settings
from freeweight.infrastructure.adapters import AdapterDirectoryMissing
from freeweight.infrastructure.providers.factory import SUPPORTED_PROVIDER_KINDS, build_provider
from freeweight.services.adapters import can_serve_adapters

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "freeweight"
_HTTPX_IMPORT = re.compile(r"^\s*(import httpx\b|from httpx\b)", re.MULTILINE)


def test_ollama_kind_builds_an_ollama_provider() -> None:
    provider = build_provider(ProviderSettings(kind="ollama", base_url="http://127.0.0.1:11434"))

    assert isinstance(provider, OllamaProvider)


def test_fake_kind_builds_a_fake_provider() -> None:
    provider = build_provider(ProviderSettings(kind="fake"))

    assert isinstance(provider, FakeProvider)


def test_unsupported_kind_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        build_provider(ProviderSettings(kind="quantum-oracle"))

    assert excinfo.value.code == "CONFIGURATION_ERROR"
    assert "quantum-oracle" in excinfo.value.message
    assert excinfo.value.details["field"] == "provider.kind"


def test_supported_kinds_are_ollama_llamacpp_and_fake() -> None:
    assert SUPPORTED_PROVIDER_KINDS == frozenset({"ollama", "llamacpp", "fake"})


def _write_adapter(directory: Path, name: str, *, content: bytes = b"lora") -> None:
    """Write one verified adapter artifact and its reviewed manifest."""
    (directory / f"{name}.gguf").write_bytes(content)
    payload: dict[str, Any] = {
        "name": name,
        "artifact_file": f"{name}.gguf",
        "artifact_sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "format": "gguf",
        "base": {
            "provider_model_name": "qwen2.5-1.5b-instruct.q8_0",
            "artifact_digest": "sha256:" + "a1" * 32,
            "identity_confidence": "digest",
        },
        "declared_capabilities": ["instruction_following"],
        "data_classification": "internal",
        "created_at": "2026-09-05T10:00:00.000Z",
    }
    (directory / f"{name}.manifest.json").write_text(
        json.dumps(
            {
                "schema": "model.adapter_manifest",
                "schema_version": "1.0",
                "generated_at": "2026-09-05T10:00:00.000Z",
                "generator": {"name": "test", "version": "0"},
                "payload": payload,
            }
        ),
        encoding="utf-8",
    )


class TestLlamaCpp:
    """The provider FreeWeight did not have before Phase 15 (ADR-0062)."""

    def test_llamacpp_kind_builds_a_supervised_provider(self, tmp_path: Path) -> None:
        """Constructed from configuration alone: ModelRack reads no environment and no file."""
        models = tmp_path / "models"
        models.mkdir()

        provider = build_provider(
            ProviderSettings(
                kind="llamacpp",
                model_directory=str(models),
                state_dir=str(tmp_path / "state"),
            )
        )

        assert isinstance(provider, LlamaCppProvider)
        assert (tmp_path / "state").is_dir()

    def test_llamacpp_without_a_model_directory_is_refused_by_key(self) -> None:
        """No default is guessed: a wrong directory serves weights nobody asked for."""
        with pytest.raises(ConfigurationError) as caught:
            build_provider(ProviderSettings(kind="llamacpp"))

        assert caught.value.details["field"] == "provider.model_directory"
        assert "no default worth guessing" in caught.value.message


class TestAdapterRegistration:
    """`[adapters] directory` reaches the provider through the composition root, or not at all."""

    def test_a_configured_directory_is_registered_on_the_provider(self, tmp_path: Path) -> None:
        """The complete set is offered, and the provider holds exactly it."""
        models = tmp_path / "models"
        models.mkdir()
        adapters = tmp_path / "adapters"
        adapters.mkdir()
        _write_adapter(adapters, "terse")
        _write_adapter(adapters, "pirate", content=b"other")

        provider = build_provider(
            ProviderSettings(
                kind="llamacpp",
                model_directory=str(models),
                state_dir=str(tmp_path / "state"),
            ),
            adapters=AdapterSettings(directory=str(adapters)),
        )

        assert sorted(state.adapter.name for state in provider.list_adapters()) == [
            "pirate",
            "terse",
        ]

    def test_an_unavailable_adapter_is_never_offered(self, tmp_path: Path) -> None:
        """A provider cannot tell it should refuse one, so it is not handed one."""
        models = tmp_path / "models"
        models.mkdir()
        adapters = tmp_path / "adapters"
        adapters.mkdir()
        _write_adapter(adapters, "terse")
        _write_adapter(adapters, "changed", content=b"a")
        (adapters / "changed.gguf").write_bytes(b"b")

        provider = build_provider(
            ProviderSettings(
                kind="llamacpp",
                model_directory=str(models),
                state_dir=str(tmp_path / "state"),
            ),
            adapters=AdapterSettings(directory=str(adapters)),
        )

        assert [state.adapter.name for state in provider.list_adapters()] == ["terse"]

    def test_an_empty_adapters_directory_key_means_off(self, tmp_path: Path) -> None:
        """Opt-in: the default configuration registers nothing and reads nothing."""
        models = tmp_path / "models"
        models.mkdir()

        provider = build_provider(
            ProviderSettings(
                kind="llamacpp",
                model_directory=str(models),
                state_dir=str(tmp_path / "state"),
            ),
            adapters=AdapterSettings(),
        )

        assert list(provider.list_adapters()) == []

    def test_a_provider_that_cannot_serve_one_is_offered_none(self, tmp_path: Path) -> None:
        """ADR-0140: the combination is configured and inert, not a refusal and not a failure.

        WP6 finding 9: ``OllamaProvider.register_adapters`` exists and refuses with
        ``CAPABILITY_UNSUPPORTED``, so offering it the set made a configured ``[adapters]
        directory`` and ``kind = "ollama"`` an application that could not start — after its own
        provider page had already written that combination to disk.
        """
        adapters = tmp_path / "adapters"
        adapters.mkdir()
        _write_adapter(adapters, "terse")

        provider = build_provider(
            ProviderSettings(kind="ollama"),
            adapters=AdapterSettings(directory=str(adapters)),
        )

        assert not provider.capabilities().adapter_hot_swap
        assert can_serve_adapters(provider) is False
        with pytest.raises(CapabilityUnsupported):
            provider.list_adapters()

    def test_the_running_application_and_config_validate_agree_on_the_combination(
        self, tmp_path: Path
    ) -> None:
        """``config validate`` said valid while the application refused to start (WP6 §9)."""
        adapters = tmp_path / "adapters"
        adapters.mkdir()
        _write_adapter(adapters, "terse")
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[provider]\nkind = "ollama"\n\n[adapters]\ndirectory = "{adapters}"\n',
            encoding="utf-8",
        )

        settings = load_settings(config_path=config_path).settings

        assert build_provider(settings.provider, adapters=settings.adapters) is not None

    def test_a_misconfigured_directory_is_refused_even_on_ollama(self, tmp_path: Path) -> None:
        """The typo is reported whatever the provider kind, not only once one can use adapters."""
        with pytest.raises(AdapterDirectoryMissing):
            build_provider(
                ProviderSettings(kind="ollama"),
                adapters=AdapterSettings(directory=str(tmp_path / "not-here")),
            )


def test_no_freeweight_module_imports_httpx_outside_the_factory() -> None:
    """Phase 3 acceptance criterion 2: provider HTTP code lives in ModelRack, never here."""
    offenders = [
        path
        for path in _SRC_ROOT.rglob("*.py")
        if path.name != "factory.py" and _HTTPX_IMPORT.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
