"""The sandbox ladder on real mechanisms: container where present, bwrap where present, and the
selection logic on every machine shape via injected detection.

The live tests skip with the machine's own reason when a tier is absent, never pass vacuously:
a skipped tier is a fact about this machine that the suite reports rather than hides.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from freeweight.config import SandboxSettings
from freeweight.external.invocation import Invocation, InvocationResult
from freeweight.external.sandbox import (
    SandboxCommand,
    SandboxDecision,
    SandboxTier,
    detect_runtimes,
    run_sandboxed,
    select_tier,
)

_RUNTIMES = detect_runtimes()

needs_bwrap = pytest.mark.skipif(
    not _RUNTIMES["bwrap"], reason="bwrap is not installed or not functional on this machine"
)
needs_container = pytest.mark.skipif(
    not (_RUNTIMES["podman"] or _RUNTIMES["docker"]),
    reason="no container runtime (podman or docker) is answering on this machine",
)


def _ok(invocation: Invocation) -> InvocationResult:
    return InvocationResult(
        argv=invocation.argv,
        exit_code=0,
        stdout=b"",
        stderr=b"",
        duration_ms=1.0,
        timed_out=False,
        output_truncated=False,
    )


class TestSelectionOnEveryMachineShape:
    """Injected detection lets one machine test the shapes the CI runner cannot be."""

    @pytest.mark.parametrize(
        ("installed", "configured", "expected_tier", "expected_runtime"),
        [
            (("podman", "docker", "bwrap"), "auto", SandboxTier.CONTAINER, "podman"),
            (("docker", "bwrap"), "auto", SandboxTier.CONTAINER, "docker"),
            (("bwrap",), "auto", SandboxTier.BWRAP, "bwrap"),
            ((), "auto", SandboxTier.REFUSED, None),
            (("docker", "bwrap"), "bwrap", SandboxTier.BWRAP, "bwrap"),
            (("docker",), "bwrap", SandboxTier.REFUSED, None),
            (("bwrap",), "container", SandboxTier.REFUSED, None),
            (("podman", "docker", "bwrap"), "none", SandboxTier.REFUSED, None),
        ],
    )
    def test_the_ladder(
        self,
        installed: tuple[str, ...],
        configured: str,
        expected_tier: SandboxTier,
        expected_runtime: str | None,
    ) -> None:
        which = {name: f"/usr/bin/{name}" for name in installed}.get
        decision = select_tier(
            SandboxSettings(tier=configured),  # type: ignore[arg-type]
            which=which,
            run=_ok,
        )

        assert decision.tier is expected_tier
        assert decision.runtime == expected_runtime

    def test_an_installed_but_nonfunctional_mechanism_is_not_selected(self) -> None:
        """`which` finding a binary proves nothing; the probe decides (a bwrap blocked by the
        kernel's user-namespace setting fails every invocation)."""

        def broken(invocation: Invocation) -> InvocationResult:
            return InvocationResult(
                argv=invocation.argv,
                exit_code=1,
                stdout=b"",
                stderr=b"namespaces disabled",
                duration_ms=1.0,
                timed_out=False,
                output_truncated=False,
            )

        decision = select_tier(SandboxSettings(), which=lambda name: f"/usr/bin/{name}", run=broken)

        assert decision.tier is SandboxTier.REFUSED


@needs_bwrap
class TestTheBwrapTierLive:
    def _decision(self) -> SandboxDecision:
        return select_tier(SandboxSettings(tier="bwrap"))

    def test_it_runs_and_writes_only_the_workdir(self, tmp_path: Path) -> None:
        result = run_sandboxed(
            SandboxCommand(
                argv=("/bin/sh", "-c", "echo alive; touch produced.txt"),
                workdir=tmp_path,
                timeout_seconds=30,
            ),
            self._decision(),
        )

        assert result.exit_code == 0
        assert b"alive" in result.stdout
        assert (tmp_path / "produced.txt").exists()

    def test_the_host_home_directory_is_invisible(self, tmp_path: Path) -> None:
        result = run_sandboxed(
            SandboxCommand(
                argv=("/bin/sh", "-c", "ls /home 2>&1; ls /root 2>&1"),
                workdir=tmp_path,
                timeout_seconds=30,
            ),
            self._decision(),
        )

        assert b"No such file or directory" in result.stdout + result.stderr

    def test_the_network_is_unreachable(self, tmp_path: Path) -> None:
        code = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
            "    print('NET OPEN')\n"
            "except OSError:\n"
            "    print('net blocked')\n"
        )
        result = run_sandboxed(
            SandboxCommand(
                argv=("/usr/bin/python3", "-c", code), workdir=tmp_path, timeout_seconds=30
            ),
            self._decision(),
        )

        assert b"net blocked" in result.stdout
        assert b"NET OPEN" not in result.stdout

    def test_the_memory_rlimit_is_enforced(self, tmp_path: Path) -> None:
        result = run_sandboxed(
            SandboxCommand(
                argv=("/usr/bin/python3", "-c", "x = bytearray(3 * 1024**3)"),
                workdir=tmp_path,
                timeout_seconds=30,
                memory_limit_mb=512,
            ),
            self._decision(),
        )

        assert result.exit_code != 0
        assert b"MemoryError" in result.stderr

    def test_a_hang_is_killed_at_the_timeout(self, tmp_path: Path) -> None:
        started = time.monotonic()
        result = run_sandboxed(
            SandboxCommand(argv=("/bin/sleep", "60"), workdir=tmp_path, timeout_seconds=2),
            self._decision(),
        )

        assert result.timed_out
        assert time.monotonic() - started < 30


@needs_container
@pytest.mark.container
class TestTheContainerTierLive:
    """Exercised where a runtime answers; each invocation pays a container start (~1 s)."""

    def _decision(self) -> SandboxDecision:
        return select_tier(SandboxSettings(tier="container"))

    def test_it_runs_isolated_with_no_network(self, tmp_path: Path) -> None:
        code = (
            "print('alive')\n"
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 80), timeout=2)\n"
            "    print('NET OPEN')\n"
            "except OSError:\n"
            "    print('net blocked')\n"
            "import pathlib; pathlib.Path('/work/produced.txt').write_text('x')\n"
        )
        result = run_sandboxed(
            SandboxCommand(argv=("python3", "-c", code), workdir=tmp_path, timeout_seconds=120),
            self._decision(),
        )

        assert result.exit_code == 0, result.stderr.decode()
        assert b"alive" in result.stdout
        assert b"net blocked" in result.stdout
        assert b"NET OPEN" not in result.stdout
        assert (tmp_path / "produced.txt").exists()
