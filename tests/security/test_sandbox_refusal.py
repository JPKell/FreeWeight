"""P13's named failure mode, held shut: code execution never silently degrades to the host.

The refusal is proven with an observer, not an assertion about intent: a command that would
create a file is submitted under a refused decision, the call raises, the file does not exist,
and the injected runner was never invoked — nothing ran anywhere. Mutation check: turning
:func:`~freeweight.external.sandbox.run_sandboxed`'s refusal into a warning makes
``test_refusal_executes_nothing_at_all`` fail on the file's existence and the runner's call
count, which is exactly the property this file exists to hold.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from freeweight.config import SandboxSettings
from freeweight.external.errors import SandboxUnavailable
from freeweight.external.invocation import Invocation, InvocationResult
from freeweight.external.sandbox import (
    SandboxCommand,
    SandboxDecision,
    SandboxTier,
    run_sandboxed,
    select_tier,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "freeweight"


def _nothing_installed(_name: str) -> None:
    """A machine with no podman, no docker and no bwrap."""
    return None


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


class RecordingRunner:
    """An injected runner that records every call — the observer for 'nothing executed'."""

    def __init__(self) -> None:
        self.calls: list[Invocation] = []

    def __call__(self, invocation: Invocation) -> InvocationResult:
        self.calls.append(invocation)
        return _ok(invocation)


class TestTheRefusalDecision:
    def test_a_machine_with_nothing_is_refused(self) -> None:
        decision = select_tier(SandboxSettings(), which=_nothing_installed, run=_ok)

        assert decision.tier is SandboxTier.REFUSED
        assert decision.runtime is None
        # The reason must name what was looked for, so the remedy is legible.
        assert "podman" in decision.reason and "bwrap" in decision.reason

    def test_tier_none_refuses_even_with_everything_available(self) -> None:
        decision = select_tier(
            SandboxSettings(tier="none"), which=lambda name: f"/usr/bin/{name}", run=_ok
        )

        assert decision.tier is SandboxTier.REFUSED
        assert "disabled by configuration" in decision.reason

    def test_container_override_never_falls_back_to_bwrap(self) -> None:
        """Downward-only means a *user's* choice down, never the machine's silent substitution."""
        only_bwrap = {"bwrap": "/usr/bin/bwrap"}
        decision = select_tier(SandboxSettings(tier="container"), which=only_bwrap.get, run=_ok)

        assert decision.tier is SandboxTier.REFUSED
        assert "refusing rather than falling back" in decision.reason

    def test_there_is_no_host_tier_to_select(self) -> None:
        """The enum has no HOST member and the config model rejects the word."""
        assert {tier.value for tier in SandboxTier} == {"container", "bwrap", "refused"}
        with pytest.raises(Exception, match="tier"):
            SandboxSettings(tier="host")  # type: ignore[arg-type]


class TestRefusalExecutesNothing:
    def test_refusal_executes_nothing_at_all(self, tmp_path: Path) -> None:
        """The observer: a file-creating command under a refusal leaves no file and no call."""
        runner = RecordingRunner()
        marker = tmp_path / "created-by-generated-code.txt"
        command = SandboxCommand(
            argv=("/bin/sh", "-c", f"touch {marker}"),
            workdir=tmp_path,
            timeout_seconds=5,
        )
        refused = select_tier(SandboxSettings(), which=_nothing_installed, run=_ok)

        with pytest.raises(SandboxUnavailable) as excinfo:
            run_sandboxed(command, refused, run=runner)

        assert not marker.exists(), "generated code ran despite the refusal"
        assert runner.calls == [], "something was executed under a refused decision"
        assert excinfo.value.code == "SANDBOX_UNAVAILABLE"
        assert "no host-execution tier" in str(excinfo.value.message).lower()

    def test_refusal_holds_under_concurrency(self, tmp_path: Path) -> None:
        """Two real threads race the refused door; both are refused, nothing runs."""
        runner = RecordingRunner()
        refused = SandboxDecision(
            tier=SandboxTier.REFUSED, runtime=None, reason="nothing available"
        )
        marker = tmp_path / "raced.txt"
        command = SandboxCommand(
            argv=("/bin/sh", "-c", f"touch {marker}"), workdir=tmp_path, timeout_seconds=5
        )
        refusals: list[str] = []
        barrier = threading.Barrier(2)

        def attempt() -> None:
            barrier.wait(timeout=5)
            try:
                run_sandboxed(command, refused, run=runner)
            except SandboxUnavailable as exc:
                refusals.append(exc.code)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert refusals == ["SANDBOX_UNAVAILABLE", "SANDBOX_UNAVAILABLE"]
        assert not marker.exists()
        assert runner.calls == []

    def test_a_decided_tier_that_fails_is_an_error_not_a_downgrade(self, tmp_path: Path) -> None:
        """The runtime vanishing between decision and use surfaces; nothing retries lower."""
        decision = SandboxDecision(
            tier=SandboxTier.CONTAINER, runtime="docker", reason="was available at decision time"
        )
        command = SandboxCommand(argv=("/bin/true",), workdir=tmp_path, timeout_seconds=5)

        def gone(_invocation: Invocation) -> InvocationResult:
            raise FileNotFoundError("docker: not found")

        with pytest.raises(FileNotFoundError):
            run_sandboxed(command, decision, run=gone)


class TestOneDoor:
    """M5's lesson: every entry point reaches the sandbox through one function, structurally."""

    SANCTIONED_SUBPROCESS_MODULES = {
        # The one starter every external subprocess goes through.
        Path("external") / "invocation.py",
        # Launching the user's own configured editor for `goals edit` — pre-existing, reviewed.
        Path("cli") / "commands" / "goals.py",
    }

    def test_only_the_sanctioned_modules_start_subprocesses(self) -> None:
        offenders: list[str] = []
        pattern = re.compile(r"^\s*(import subprocess|from subprocess import)", re.MULTILINE)
        for path in SRC.rglob("*.py"):
            relative = path.relative_to(SRC)
            if relative in self.SANCTIONED_SUBPROCESS_MODULES:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(relative))

        assert offenders == [], (
            f"subprocess reached from outside the sanctioned modules: {offenders} — "
            "every external process must start through external.invocation.run_invocation"
        )

    def test_os_system_and_friends_are_absent_everywhere(self) -> None:
        pattern = re.compile(r"os\.system\(|os\.popen\(|os\.exec[lv]p?e?\(|os\.spawnl")
        offenders = [
            str(path.relative_to(SRC))
            for path in SRC.rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]

        assert offenders == []
