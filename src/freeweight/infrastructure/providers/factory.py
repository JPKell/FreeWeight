"""freeweight.infrastructure.providers.factory — the one place a ``Provider`` is constructed.

Coding Standards §5: "Every application has one composition root where concretions are built.
Nothing else calls a constructor for infrastructure." For a model provider, that root is this
function — called from :mod:`freeweight.web.app`'s lifespan for the running server and from each
``freeweight models`` command for a one-shot CLI invocation — never from ``services/`` or
``domain/`` directly.

This is also the only module in ``freeweight`` that imports :mod:`modelrack.providers.ollama`,
which is in turn the only place in ModelRack that imports ``httpx`` (its own module docstring says
so). Nothing else in this application touches provider HTTP, which is Phase 3's own boundary test:
no ``httpx`` import anywhere in ``freeweight`` outside this file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baseaicore import ConfigurationError

if TYPE_CHECKING:
    from modelrack.provider import Provider

    from freeweight.config import ProviderSettings

__all__ = ["SUPPORTED_PROVIDER_KINDS", "build_provider"]

SUPPORTED_PROVIDER_KINDS: frozenset[str] = frozenset({"ollama", "fake"})
"""``provider.kind`` values this phase can construct.

``"ollama"`` is the production adapter (spec §5: "a model provider (Ollama by default)"; Phase 3's
own goal is Ollama discovery specifically). ``"fake"`` constructs
:class:`~modelrack.testing.FakeProvider` and exists so the running application — not just its unit
tests — can be exercised with no GPU, no Ollama and no network (testing standards §1: e2e runs
"through HTTP and CLI" against the fake). ``openai_compatible``, ``llamacpp`` and ``vllm`` are valid
:class:`~baseaicore.ProviderKind` members but have no adapter wired here yet; naming one is a
configuration error today, not a silent fallback to Ollama.
"""


def build_provider(settings: ProviderSettings) -> Provider:
    """Construct the configured :class:`~modelrack.provider.Provider`.

    Args:
        settings: ``settings.provider`` from the resolved application configuration.

    Returns:
        A provider satisfying the :class:`~modelrack.provider.Provider` protocol. Opens no
        connection by itself — :class:`~modelrack.providers.ollama.OllamaProvider` builds a pooled
        ``httpx.Client`` lazily and makes no request until a method is called.

    Raises:
        ConfigurationError: ``settings.kind`` is not one of :data:`SUPPORTED_PROVIDER_KINDS`.
    """
    if settings.kind == "ollama":
        from modelrack.providers.ollama import OllamaProvider

        return OllamaProvider(settings.base_url, timeout=settings.timeout_seconds)
    if settings.kind == "fake":
        from modelrack.testing import FakeProvider

        return FakeProvider()
    raise ConfigurationError(
        f"provider.kind={settings.kind!r} is not supported; expected one of "
        f"{sorted(SUPPORTED_PROVIDER_KINDS)!r}.",
        details={"field": "provider.kind", "value": settings.kind},
    )
