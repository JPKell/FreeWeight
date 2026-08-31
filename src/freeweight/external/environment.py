"""freeweight.external.environment — isolated per-benchmark environments, created and verified.

Every external benchmark gets its own environment under ``[external].root`` — a Python venv, its
pinned packages, its datasets and a recorded install state — entirely separate from FreeWeight's
own environment. **FreeWeight never imports an external benchmark package** (ADR-0018, spec §2);
the isolation is proven, not asserted, by :func:`assert_no_contamination`, which compares
``sys.modules`` across an operation and refuses any external package that leaked in.

The environment is created by running the manifest's ``install_command`` through the invocation
contract (argv, timeout, captured output — no shell), and its state is written to an
``install.json`` so ``freeweight external verify`` can report what is installed and whether its
datasets still match their pins.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from freeweight.external.datasets import verify_dataset
from freeweight.external.errors import ExternalBenchmarkFailed
from freeweight.external.invocation import Invocation, run_invocation

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from freeweight.external.invocation import InvocationResult
    from freeweight.external.manifest import ExternalManifest

__all__ = [
    "BenchmarkEnvironment",
    "InstallState",
    "assert_no_contamination",
    "external_module_prefixes",
]

_INSTALL_STATE_FILE = "install.json"

# The import prefixes an external benchmark brings in. None of these may ever appear in
# ``sys.modules`` as a result of running FreeWeight — importing one would defeat the whole
# isolation, and the heavy scientific stack behind them is exactly what ADR-0018 keeps out of the
# application environment.
_EXTERNAL_PREFIXES: frozenset[str] = frozenset(
    {
        "lm_eval",
        "instruction_following_eval",
        "evalplus",
        "cruxeval",
        "bfcl",
        "ruler",
        "judgebench",
        "llmbar",
        "criticbench",
        "torch",
        "transformers",
    }
)


def external_module_prefixes() -> frozenset[str]:
    """The module prefixes that must never enter FreeWeight's own ``sys.modules``."""
    return _EXTERNAL_PREFIXES


def assert_no_contamination(before: Iterable[str], after: Iterable[str]) -> None:
    """Refuse if any external benchmark module entered ``sys.modules`` across an operation.

    P13 AC3, made a property rather than a promise: FreeWeight importing nothing from an external
    benchmark is proven by taking a ``sys.modules`` snapshot before and after and asserting no
    module under an external prefix is newly present.

    Args:
        before: Module names present before the operation.
        after: Module names present after it.

    Raises:
        ExternalBenchmarkFailed: An external benchmark package was imported into this process.
    """
    leaked = sorted(
        name for name in set(after) - set(before) if name.split(".", 1)[0] in _EXTERNAL_PREFIXES
    )
    if leaked:
        raise ExternalBenchmarkFailed(
            "An external benchmark package was imported into FreeWeight's own process, which "
            f"ADR-0018 forbids: {leaked}. External benchmarks run as subprocesses only.",
            details={"leaked_modules": leaked},
        )


@dataclass(frozen=True, slots=True)
class InstallState:
    """What ``freeweight external install`` recorded about one benchmark's environment.

    Attributes:
        key: The benchmark key.
        release_tag: The upstream tag installed.
        commit: The upstream commit installed.
        pinned_packages: The exact package set installed.
        datasets: The dataset names installed, each verified against its pin at install time.
        installed_at: When the environment was created, RFC 3339.
    """

    key: str
    release_tag: str
    commit: str
    pinned_packages: tuple[str, ...]
    datasets: tuple[str, ...]
    installed_at: str

    def to_json(self) -> dict[str, object]:
        """The ``install.json`` body."""
        return {
            "key": self.key,
            "release_tag": self.release_tag,
            "commit": self.commit,
            "pinned_packages": list(self.pinned_packages),
            "datasets": list(self.datasets),
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_json(cls, body: dict[str, object]) -> InstallState:
        """Parse an ``install.json`` body back into a state."""
        packages = body.get("pinned_packages", ())
        datasets = body.get("datasets", ())
        return cls(
            key=str(body["key"]),
            release_tag=str(body.get("release_tag", "")),
            commit=str(body.get("commit", "")),
            pinned_packages=tuple(str(item) for item in packages)
            if isinstance(packages, list)
            else (),
            datasets=tuple(str(item) for item in datasets) if isinstance(datasets, list) else (),
            installed_at=str(body.get("installed_at", "")),
        )


class BenchmarkEnvironment:
    """One benchmark's isolated environment root, and the operations over it.

    The root is ``<external.root>/<benchmark-key>/``, holding ``venv/``, ``datasets/`` and
    ``install.json``. Building the object touches no filesystem; :meth:`install` and
    :meth:`verify` do.
    """

    __slots__ = ("_manifest", "_root")

    def __init__(self, manifest: ExternalManifest, external_root: Path) -> None:
        """Locate this benchmark's environment under ``external_root``. Creates nothing."""
        self._manifest = manifest
        self._root = external_root / manifest.key.replace(".", "_")

    @property
    def root(self) -> Path:
        """The environment root directory."""
        return self._root

    @property
    def datasets_dir(self) -> Path:
        """Where this benchmark's datasets live."""
        return self._root / "datasets"

    @property
    def state_path(self) -> Path:
        """The ``install.json`` path."""
        return self._root / _INSTALL_STATE_FILE

    def is_installed(self) -> bool:
        """Whether this benchmark has a recorded install state."""
        return self.state_path.is_file()

    def read_state(self) -> InstallState | None:
        """The recorded install state, or ``None`` if not installed."""
        if not self.state_path.is_file():
            return None
        return InstallState.from_json(json.loads(self.state_path.read_text(encoding="utf-8")))

    def install(
        self,
        *,
        timeout_seconds: float,
        run: Callable[[Invocation], InvocationResult] = run_invocation,
    ) -> InstallState:
        """Create the environment and record its state, running the install as a subprocess.

        The install command runs through the invocation contract (argv, timeout, no shell). An
        adapter with an empty ``install_command`` (one that only parses recorded output) records
        state without running anything.

        Args:
            timeout_seconds: Budget for the install step.
            run: The invocation runner; injected so tests never install anything real.

        Returns:
            The recorded install state.

        Raises:
            ExternalBenchmarkFailed: The install command failed or timed out.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        self.datasets_dir.mkdir(parents=True, exist_ok=True)
        if self._manifest.install_command:
            result = run(
                Invocation(
                    argv=self._manifest.install_command,
                    timeout_seconds=timeout_seconds,
                    cwd=self._root,
                    env={"PATH": "/usr/bin:/bin", "HOME": str(self._root)},
                )
            )
            if result.timed_out or result.exit_code != 0:
                raise ExternalBenchmarkFailed(
                    f"Installing {self._manifest.key!r} failed"
                    + (" (timed out)" if result.timed_out else f" (exit {result.exit_code})")
                    + ".",
                    details={
                        "key": self._manifest.key,
                        "exit_code": str(result.exit_code),
                        "timed_out": str(result.timed_out),
                    },
                )
        state = InstallState(
            key=self._manifest.key,
            release_tag=self._manifest.release_tag,
            commit=self._manifest.commit,
            pinned_packages=self._manifest.pinned_packages,
            datasets=tuple(self._manifest.dataset_names()),
            installed_at=datetime.now(UTC).isoformat(),
        )
        self.state_path.write_text(json.dumps(state.to_json(), indent=2), encoding="utf-8")
        return state

    def verify(self) -> None:
        """Verify the install state exists and every pinned dataset still matches its hash.

        Raises:
            ExternalBenchmarkFailed: The benchmark is not installed.
            DatasetMissing / DatasetHashMismatch: A dataset is absent or has changed.
        """
        if not self.is_installed():
            raise ExternalBenchmarkFailed(
                f"{self._manifest.key!r} is not installed. Run "
                f"`freeweight external install {self._manifest.key}`.",
                details={"key": self._manifest.key},
            )
        for spec in self._manifest.datasets:
            verify_dataset(self.datasets_dir / spec.filename, spec.sha256, name=spec.name)


def snapshot_modules() -> frozenset[str]:
    """A ``sys.modules`` snapshot, for :func:`assert_no_contamination`."""
    return frozenset(sys.modules)
