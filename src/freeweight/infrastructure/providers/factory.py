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

from pathlib import Path
from typing import TYPE_CHECKING

from baseaicore import ConfigurationError

if TYPE_CHECKING:
    from modelrack.provider import Provider

    from freeweight.config import AdapterSettings, ProviderSettings

__all__ = ["SUPPORTED_PROVIDER_KINDS", "build_provider", "register_configured_adapters"]

SUPPORTED_PROVIDER_KINDS: frozenset[str] = frozenset({"ollama", "llamacpp", "fake"})
"""``provider.kind`` values this build can construct.

``"ollama"`` is the production adapter (spec §5: "a model provider (Ollama by default)"; Phase 3's
own goal is Ollama discovery specifically). ``"fake"`` constructs
:class:`~modelrack.testing.FakeProvider` and exists so the running application — not just its unit
tests — can be exercised with no GPU, no Ollama and no network (testing standards §1: e2e runs
"through HTTP and CLI" against the fake). ``"llamacpp"`` constructs
:class:`~modelrack.providers.llamacpp.LlamaCppProvider`, which launches and supervises its own
server over a directory of GGUF weights — **the one kind that can serve a LoRA adapter**
(ADR-0062), and therefore the only kind under
which an adapter subject can ever be measured (Phase 15). ``openai_compatible`` and ``vllm`` are
valid :class:`~baseaicore.ProviderKind` members but have no adapter wired here yet; naming one is a
configuration error today, not a silent fallback to Ollama.
"""


def build_provider(
    settings: ProviderSettings, *, adapters: AdapterSettings | None = None
) -> Provider:
    """Construct the configured :class:`~modelrack.provider.Provider`, adapters and all.

    Args:
        settings: ``settings.provider`` from the resolved application configuration.
        adapters: ``settings.adapters``, for a caller that measures models. ``None`` — the default,
            and what the judge, calibration and health paths pass — means the same as an empty
            ``[adapters] directory``: no adapters are offered. Those callers never measure an
            adapter subject, so registering for them would launch servers differently for no
            reason.

    Returns:
        A provider satisfying the :class:`~modelrack.provider.Provider` protocol, with the
        operator's adapters already offered to it where both are configured. Opens no connection
        and launches no process by itself — :class:`OllamaProvider` builds a pooled ``httpx.Client``
        lazily, and :class:`LlamaCppProvider` starts a server only when a model is first loaded.

    Raises:
        ConfigurationError: ``settings.kind`` is not one of :data:`SUPPORTED_PROVIDER_KINDS`, or it
            is ``"llamacpp"`` and ``model_directory`` is empty.
        AdapterDirectoryMissing: ``adapters.directory`` names something that is not a directory.
    """
    provider = _build_kind(settings)
    if adapters is not None:
        register_configured_adapters(provider, adapters)
    return provider


def _build_kind(settings: ProviderSettings) -> Provider:
    """Construct the provider for ``settings.kind``, before any adapter is offered to it."""
    if settings.kind == "ollama":
        from modelrack.providers.ollama import OllamaProvider

        return OllamaProvider(settings.base_url, timeout=settings.timeout_seconds)
    if settings.kind == "llamacpp":
        return _build_llamacpp(settings)
    if settings.kind == "fake":
        from modelrack.testing import FakeProvider

        return FakeProvider()
    raise ConfigurationError(
        f"provider.kind={settings.kind!r} is not supported; expected one of "
        f"{sorted(SUPPORTED_PROVIDER_KINDS)!r}.",
        details={"field": "provider.kind", "value": settings.kind},
    )


def _build_llamacpp(settings: ProviderSettings) -> Provider:
    """Construct the supervised llama.cpp server this configuration describes (ADR-0062).

    **ModelRack reads no environment variable and no configuration file** (ModelRack spec §12), so
    every path it uses is named here, by this application.

    Args:
        settings: ``settings.provider``, with ``kind == "llamacpp"``.

    Returns:
        The provider. Launches nothing: a server starts when a model is first loaded.

    Raises:
        ConfigurationError: ``model_directory`` is empty. There is no default worth guessing — a
            wrong directory is a server serving weights nobody asked for — and empty is the case an
            operator reaches by copying the block, so it is named rather than defaulted.
    """
    from modelrack.providers.llamacpp import LlamaCppProvider

    directory = settings.model_directory.strip()
    if not directory:
        raise ConfigurationError(
            "provider.model_directory is required for provider.kind='llamacpp': the server is "
            "launched over a directory of GGUF weights, and there is no default worth guessing.",
            details={"field": "provider.model_directory"},
        )
    configured_state = settings.state_dir.strip()
    if configured_state:
        state_path = Path(configured_state).expanduser()
    else:
        from freeweight.config import data_dir

        state_path = data_dir() / "llamacpp"
    state_path.mkdir(parents=True, exist_ok=True)
    return LlamaCppProvider(
        Path(directory).expanduser(),
        state_dir=state_path,
        server_path=settings.server_path,
        timeout=settings.timeout_seconds,
        memory_max_bytes=settings.memory_max_bytes,
        memory_high_bytes=settings.memory_high_bytes,
    )


def register_configured_adapters(provider: Provider, adapters: AdapterSettings) -> int:
    """Offer the operator's adapter directory to ``provider``, and return how many were offered.

    Called from the same composition root that built the provider, because reading the directory
    and converting its manifests is the application's job and not ModelRack's
    (ADR-0061 rule 3).

    The set passed is the **complete** set: ``register_adapters`` replaces rather than merges, so a
    rescan restates the directory whole and an adapter no longer in it is retired at the provider's
    next idle. There is no incremental form and none is wanted — the directory is the truth.

    Args:
        provider: The provider just constructed. One that cannot register adapters is left alone.
        adapters: ``settings.adapters``. Empty ``directory`` means adapters are off (ADR-0061
            rule 2) and this is a no-op.

    Returns:
        How many adapters were offered — the *available* entries only. An unavailable adapter (a
        missing artifact, a digest that no longer matches) is never offered to a provider, because
        a provider cannot tell that it should refuse it.

    Raises:
        AdapterDirectoryMissing: ``adapters.directory`` is set to something that is not a
            directory.
    """
    directory = adapters.resolved_directory()
    if directory is None:
        return 0
    from freeweight.infrastructure.adapters import read_directory, registrations_from

    register = getattr(provider, "register_adapters", None)
    if register is None:
        # Reading still happens: a misconfigured directory is refused whatever the provider is, so
        # an operator does not discover the typo only after switching provider kinds.
        read_directory(directory)
        return 0
    registrations = registrations_from(read_directory(directory).entries)
    register(registrations)
    return len(registrations)
