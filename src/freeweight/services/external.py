"""freeweight.services.external — the service layer over the external-benchmark subsystem.

The CLI (`freeweight external list|install|verify`) and the web sources page both go through this
module, so "which benchmarks exist, what they credit, and whether they are installed" has one
answer computed one way. It reads the adapter registry and each benchmark's environment; it
performs no I/O beyond the filesystem the environment already owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from freeweight.external.adapters import ADAPTERS, get_adapter
from freeweight.external.datasets import is_placeholder_pin
from freeweight.external.environment import BenchmarkEnvironment
from freeweight.external.errors import ExternalBenchmarkFailed

if TYPE_CHECKING:
    from pathlib import Path

    from freeweight.config import Settings
    from freeweight.external.environment import InstallState

__all__ = [
    "ExternalBenchmarkInfo",
    "install_benchmark",
    "list_benchmarks",
    "verify_benchmark",
]


@dataclass(frozen=True, slots=True)
class ExternalBenchmarkInfo:
    """One external benchmark as the CLI and the sources page present it.

    Attributes:
        key: The suite key.
        name: Human-readable name.
        category: The benchmark category.
        capabilities: The capabilities it contributes evidence to.
        source_repository: The upstream project's repository, for the credit.
        release_tag: The pinned upstream version.
        commit: The pinned upstream commit.
        license: The upstream licence.
        requires_sandbox: Whether it executes code and needs a sandbox tier.
        installed: Whether this benchmark's environment has been created here.
        dataset_names: The datasets it pins.
        has_placeholder_pins: Whether any dataset pin is a shipped placeholder rather than a
            recorded hash (M6-8). ``True`` means an install will refuse at verification until
            the true sha256 is recorded, and no result from this benchmark is publishable.
    """

    key: str
    name: str
    category: str
    capabilities: tuple[str, ...]
    source_repository: str
    release_tag: str
    commit: str
    license: str
    requires_sandbox: bool
    installed: bool
    dataset_names: tuple[str, ...]
    has_placeholder_pins: bool


def _external_root(settings: Settings) -> Path:
    """The configured external-environment root."""
    return settings.external.root_path


def list_benchmarks(settings: Settings) -> list[ExternalBenchmarkInfo]:
    """Every registered external benchmark, with its credit and its install state.

    Sorted by key so the CLI and the page present a stable order.
    """
    root = _external_root(settings)
    infos: list[ExternalBenchmarkInfo] = []
    for key in sorted(ADAPTERS):
        manifest = ADAPTERS[key].manifest
        environment = BenchmarkEnvironment(manifest, root)
        infos.append(
            ExternalBenchmarkInfo(
                key=manifest.key,
                name=manifest.name,
                category=manifest.category,
                capabilities=manifest.capabilities,
                source_repository=manifest.source_repository,
                release_tag=manifest.release_tag,
                commit=manifest.commit,
                license=manifest.license,
                requires_sandbox=manifest.requires_sandbox,
                installed=environment.is_installed(),
                dataset_names=tuple(manifest.dataset_names()),
                has_placeholder_pins=any(
                    is_placeholder_pin(spec.sha256) for spec in manifest.datasets
                ),
            )
        )
    return infos


def _environment(settings: Settings, key: str) -> BenchmarkEnvironment:
    adapter = get_adapter(key)
    if adapter is None:
        raise ExternalBenchmarkFailed(
            f"No external benchmark is registered under {key!r}. "
            "Run `freeweight external list` to see the available ones.",
            details={"key": key},
        )
    return BenchmarkEnvironment(adapter.manifest, _external_root(settings))


def install_benchmark(settings: Settings, key: str) -> InstallState:
    """Create one benchmark's isolated environment and record its state.

    Raises:
        ExternalBenchmarkFailed: The key is unknown, or the install command failed.
    """
    environment = _environment(settings, key)
    return environment.install(timeout_seconds=settings.external.install_timeout_seconds)


def verify_benchmark(settings: Settings, key: str) -> None:
    """Verify one installed benchmark and its datasets against their pins.

    Raises:
        ExternalBenchmarkFailed: The key is unknown or the benchmark is not installed.
        DatasetMissing / DatasetHashMismatch: A dataset is absent or has changed.
    """
    _environment(settings, key).verify()
