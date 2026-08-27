"""Unit tests for freeweight.infrastructure.providers.factory.

Also carries Phase 3's boundary test — acceptance criterion 2: "FreeWeight contains no provider
HTTP code (asserted)" — by scanning every source file for an ``httpx`` import outside this one
module, the only place in the application allowed to reach it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from baseaicore import ConfigurationError
from modelrack.providers.fake import FakeProvider
from modelrack.providers.ollama import OllamaProvider

from freeweight.config import ProviderSettings
from freeweight.infrastructure.providers.factory import SUPPORTED_PROVIDER_KINDS, build_provider

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


def test_supported_kinds_are_exactly_ollama_and_fake() -> None:
    assert SUPPORTED_PROVIDER_KINDS == frozenset({"ollama", "fake"})


def test_no_freeweight_module_imports_httpx_outside_the_factory() -> None:
    """Phase 3 acceptance criterion 2: provider HTTP code lives in ModelRack, never here."""
    offenders = [
        path
        for path in _SRC_ROOT.rglob("*.py")
        if path.name != "factory.py" and _HTTPX_IMPORT.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
