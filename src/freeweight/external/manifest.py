"""freeweight.external.manifest — what an external benchmark declares about itself.

An external manifest is a native :class:`~freeweight.domain.benchmark.BenchmarkManifest` (so it
appears alongside native suites everywhere) plus the extra provenance ADR-0018 and benchmark
catalog §5 require of an external benchmark: the source repository, the pinned release tag and
commit, the licence, the install command, the datasets it pins, whether it needs a sandbox, and
whether it needs the network at install time.

The point of pinning the tag *and* the commit is that a tag can move; the commit cannot. A run's
provenance records the commit, so two runs that both say ``v1.2.0`` can be shown to have used the
same code or not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from freeweight.external.datasets import DatasetSpec

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["ExternalManifest"]


@dataclass(frozen=True, slots=True)
class ExternalManifest:
    """The declarative record of one external benchmark adapter.

    Attributes:
        key: The suite key, e.g. ``"external.ifeval"``. Namespaced under ``external`` so it never
            collides with a native suite and a reader can tell at a glance which kind it is.
        name: Human-readable name.
        version: The adapter's own version — how FreeWeight drives this benchmark. Distinct from
            ``release_tag``, which is the *benchmark project's* version; a change to either
            separates results.
        category: The benchmark catalog §2 category.
        capabilities: The capability IDs this benchmark contributes evidence to.
        source_repository: The upstream project's repository URL.
        release_tag: The pinned release/tag of the upstream project.
        commit: The pinned commit the tag resolved to when this adapter was written. A tag can be
            re-pointed; the commit is what actually reproduces.
        license: The upstream project's licence identifier.
        install_command: The argument list that creates the benchmark's environment, run under the
            invocation contract (no shell). Empty for an adapter that only parses recorded output.
        pinned_packages: The exact ``package==version`` set installed into the isolated
            environment, so an install is reproducible and auditable.
        datasets: The datasets this benchmark pins, each with its hash.
        requires_sandbox: Whether this benchmark executes model-generated code and therefore needs
            a sandbox tier. ``True`` for EvalPlus and CRUXEval; ``False`` for the rest.
        requires_network_at_install: Whether creating the environment needs the network. Recorded
            so a headless/offline deployment knows what it cannot install.
        metrics: The metric keys this benchmark's normalized output produces, with direction.
    """

    key: str
    name: str
    version: str
    category: str
    capabilities: tuple[str, ...]
    source_repository: str
    release_tag: str
    commit: str
    license: str
    install_command: tuple[str, ...] = ()
    pinned_packages: tuple[str, ...] = ()
    datasets: tuple[DatasetSpec, ...] = ()
    requires_sandbox: bool = False
    requires_network_at_install: bool = True
    metrics: Mapping[str, bool] = field(default_factory=dict)
    """``{metric_key: higher_is_better}`` — the metrics the adapter emits."""

    def provenance(self) -> dict[str, object]:
        """The provenance block recorded on every result this benchmark produces.

        Everything a person needs to know *which* external benchmark, at which version, against
        which data, produced a number — and to reproduce it or challenge its comparability.
        """
        return {
            "runner": "external",
            "source_repository": self.source_repository,
            "release_tag": self.release_tag,
            "commit": self.commit,
            "license": self.license,
            "adapter_version": self.version,
            "dataset_hashes": {spec.name: spec.sha256 for spec in self.datasets},
            "requires_sandbox": self.requires_sandbox,
        }

    def dataset_names(self) -> Sequence[str]:
        """The names of the datasets this benchmark pins, for `external verify`."""
        return tuple(spec.name for spec in self.datasets)
