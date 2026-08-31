"""freeweight.external.sandbox — the tiered containment every code execution goes through.

ADR-0018's ladder, implemented: **container (podman preferred, then docker) → bwrap → refuse.**
There is no host-execution tier, no flag that creates one, and no fallback below the decided
tier: a machine that offers nothing gets a refusal, and a decided tier that fails at run time is
an error — never a quieter tier tried in its place, because "silently degrading to host
execution" is this subsystem's named failure mode and every design choice here exists to make it
structurally impossible rather than carefully avoided.

**One decision, one door.** :func:`select_tier` decides the tier once per run and the decision is
recorded on everything that used it; :func:`run_sandboxed` is the only function that executes a
sandboxed command, and its first act is to refuse a ``REFUSED`` decision. Every entry point — the
run engine executing a code-execution benchmark, whatever the CLI or a route may one day drive —
reaches execution through these two functions or not at all. A structural test
(`tests/security/test_sandbox_refusal.py`) holds the door count at one by asserting that nothing
else under ``freeweight`` starts a subprocess.

The tiers differ in strength and the difference is documented, not hidden: a container gives a
read-only rootfs, no network, cgroup resource limits and dropped capabilities; bwrap gives
namespace isolation (``--unshare-all``), read-only binds of the minimal runtime and rlimits, but
shares the kernel with no cgroup caps — ADR-0018 records this as the reference machine's
accepted residual risk. Results are comparable across tiers for correctness and labelled by tier
for performance, which is why the tier is recorded on every result.
"""

from __future__ import annotations

import enum
import logging
import os
import resource
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from freeweight.external.errors import SandboxUnavailable
from freeweight.external.invocation import Invocation, InvocationResult, run_invocation

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from freeweight.config import SandboxSettings

__all__ = [
    "SandboxCommand",
    "SandboxDecision",
    "SandboxTier",
    "detect_runtimes",
    "run_sandboxed",
    "select_tier",
]

logger = logging.getLogger(__name__)

_CONTAINER_RUNTIMES = ("podman", "docker")
_DEFAULT_CONTAINER_IMAGE = "python:3.12-slim"
_PROBE_TIMEOUT_SECONDS = 10.0


class SandboxTier(enum.Enum):
    """The three tiers of ADR-0018. There is deliberately no ``HOST`` member."""

    CONTAINER = "container"
    BWRAP = "bwrap"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class SandboxDecision:
    """The tier one run's code execution will use — decided once, recorded everywhere.

    Attributes:
        tier: The decided tier. ``REFUSED`` is a real decision: it means every code-execution
            benchmark in the run is skipped with ``sandbox_unavailable``, and
            :func:`run_sandboxed` will refuse to execute under it.
        runtime: The concrete mechanism (``"podman"``, ``"docker"``, ``"bwrap"``) or ``None``
            when refused.
        reason: Why this tier — for the run's record and for ``freeweight doctor``. A refusal's
            reason names what was looked for and not found, so the remedy is legible.
    """

    tier: SandboxTier
    runtime: str | None
    reason: str

    @property
    def label(self) -> str:
        """The value recorded on results: the runtime for a usable tier, else ``"refused"``."""
        return self.tier.value if self.tier is not SandboxTier.REFUSED else "refused"


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    """One command to execute inside the sandbox.

    Attributes:
        argv: The argument list, as it should run *inside* the sandbox.
        workdir: The one host directory the sandbox may write: bound at ``/work`` (container) or
            bound in place (bwrap). Everything else the sandbox sees is read-only.
        timeout_seconds: Wall-clock budget; the sandboxed process (group) is killed beyond it.
        cpu_limit: CPU cores (container ``--cpus``; advisory under bwrap, where the CPU rlimit
            covers total CPU-seconds instead).
        memory_limit_mb: Memory cap (container ``--memory``; ``RLIMIT_AS`` under bwrap).
        env: Environment inside the sandbox. Complete, never inherited.
        container_image: Image for the container tier. Ignored under bwrap.
        output_cap_bytes: Cap on captured stdout+stderr.
    """

    argv: tuple[str, ...]
    workdir: Path
    timeout_seconds: float
    cpu_limit: int = 2
    memory_limit_mb: int = 2048
    env: Mapping[str, str] = field(default_factory=dict)
    container_image: str = _DEFAULT_CONTAINER_IMAGE
    output_cap_bytes: int = 8 * 1024 * 1024


def _probe_bwrap(bwrap_path: str, run: Callable[[Invocation], InvocationResult]) -> bool:
    """Whether ``bwrap`` actually works here — installed is not the same as functional.

    Kernels with unprivileged user namespaces disabled leave a ``bwrap`` binary that fails every
    invocation; a tier decided on ``which bwrap`` alone would then fail at execution time, which
    is too late to skip the benchmark honestly.
    """
    probe = Invocation(
        argv=(bwrap_path, "--unshare-all", "--ro-bind", "/", "/", "/bin/true"),
        timeout_seconds=_PROBE_TIMEOUT_SECONDS,
    )
    try:
        return run(probe).exit_code == 0
    except OSError:
        return False


def _probe_container(runtime_path: str, run: Callable[[Invocation], InvocationResult]) -> bool:
    """Whether the container runtime's daemon/backend answers — the binary alone proves nothing."""
    probe = Invocation(argv=(runtime_path, "info"), timeout_seconds=_PROBE_TIMEOUT_SECONDS)
    try:
        return run(probe).exit_code == 0
    except OSError:
        return False


def detect_runtimes(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[Invocation], InvocationResult] = run_invocation,
) -> dict[str, bool]:
    """Report which sandbox mechanisms this machine offers, each actually probed.

    Args:
        which: Executable lookup; injected so tests can simulate any machine.
        run: The invocation runner used for probes; injected for the same reason.

    Returns:
        ``{"podman": bool, "docker": bool, "bwrap": bool}`` — present and *functional*.
    """
    availability: dict[str, bool] = {}
    for runtime in _CONTAINER_RUNTIMES:
        path = which(runtime)
        availability[runtime] = bool(path) and _probe_container(path or runtime, run)
    bwrap_path = which("bwrap")
    availability["bwrap"] = bool(bwrap_path) and _probe_bwrap(bwrap_path or "bwrap", run)
    return availability


def select_tier(
    settings: SandboxSettings,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[Invocation], InvocationResult] = run_invocation,
) -> SandboxDecision:
    """Decide the sandbox tier for one run: the highest available, overridable **downward** only.

    ``[sandbox].tier`` semantics:

    * ``auto`` — podman, else docker, else bwrap, else refuse.
    * ``container`` — podman or docker, else **refuse**. Never bwrap: a user who asked for a
      container's guarantees must not silently get a weaker boundary.
    * ``bwrap`` — bwrap, else refuse. A machine with a container runtime still gets bwrap here,
      because downward is the only permitted direction and this *is* downward.
    * ``none`` — refuse always. The off switch for code execution.

    Args:
        settings: The ``[sandbox]`` configuration.
        which: Executable lookup; injected so every machine shape is testable.
        run: Invocation runner for the functional probes.

    Returns:
        The decision, with the reason a person (and ``freeweight doctor``) can read.
    """
    if settings.tier == "none":
        return SandboxDecision(
            tier=SandboxTier.REFUSED,
            runtime=None,
            reason="sandbox.tier = 'none': code execution is disabled by configuration.",
        )
    available = detect_runtimes(which=which, run=run)
    if settings.tier in ("auto", "container"):
        for runtime in _CONTAINER_RUNTIMES:
            if available[runtime]:
                return SandboxDecision(
                    tier=SandboxTier.CONTAINER,
                    runtime=runtime,
                    reason=f"{runtime} is installed and answering.",
                )
        if settings.tier == "container":
            return SandboxDecision(
                tier=SandboxTier.REFUSED,
                runtime=None,
                reason=(
                    "sandbox.tier = 'container' but neither podman nor docker is available; "
                    "refusing rather than falling back to a weaker tier."
                ),
            )
    if available["bwrap"]:
        return SandboxDecision(
            tier=SandboxTier.BWRAP,
            runtime="bwrap",
            reason="bwrap is installed and functional (container runtime preferred but absent)."
            if settings.tier == "auto"
            else "sandbox.tier = 'bwrap'.",
        )
    return SandboxDecision(
        tier=SandboxTier.REFUSED,
        runtime=None,
        reason=(
            "no sandbox tier is available: podman and docker are not answering and bwrap is "
            "absent or non-functional. Code-execution benchmarks are skipped, never run on the "
            "host."
        ),
    )


def _container_argv(command: SandboxCommand, runtime: str) -> tuple[str, ...]:
    """The container tier's wrapper: ADR-0018's tier-1 flags, every one of them."""
    argv: list[str] = [
        runtime,
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",  # noqa: S108 — the container's own /tmp, not the host's
        "--workdir",
        "/work",
        "--volume",
        f"{command.workdir}:/work:rw",
        "--memory",
        f"{command.memory_limit_mb}m",
        "--cpus",
        str(command.cpu_limit),
        "--pids-limit",
        "128",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
    ]
    for name, value in command.env.items():
        argv.extend(["--env", f"{name}={value}"])
    argv.append(command.container_image)
    argv.extend(command.argv)
    return tuple(argv)


def _bwrap_argv(command: SandboxCommand) -> tuple[str, ...]:
    """The bwrap tier's wrapper: ADR-0018's tier-2 flags.

    Read-only binds of the minimal runtime only — ``/usr``, ``/bin``, ``/lib*``, ``/etc`` — and
    the workdir read-write. No home directory, no application database, and ``--unshare-all``
    covers the network.
    """
    argv: list[str] = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",  # noqa: S108 — the sandbox's private tmpfs, not the host's /tmp
    ]
    for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
        argv.extend(["--ro-bind-try", root, root])
    argv.extend(["--bind", str(command.workdir), str(command.workdir)])
    argv.extend(["--chdir", str(command.workdir)])
    for name, value in command.env.items():
        argv.extend(["--setenv", name, value])
    argv.append("--")
    argv.extend(command.argv)
    return tuple(argv)


def _bwrap_rlimits(command: SandboxCommand) -> Callable[[], None]:
    """The rlimits applied to the bwrap process (and inherited by everything inside it).

    ``RLIMIT_AS`` is the memory cap; ``RLIMIT_CPU`` caps total CPU-seconds at
    ``cpu_limit × timeout`` (bwrap has no per-core throttle); ``RLIMIT_NPROC`` bounds a fork
    bomb; ``RLIMIT_FSIZE`` bounds what a runaway write can do to the workdir.

    ``RLIMIT_NPROC`` is checked by the kernel against the **user's total task count — threads
    included** — not this process's children, so the cap is current usage plus headroom. An
    absolute number small enough to bound a fork bomb blocks ``bwrap``'s own clone on any busy
    desktop (seen on this machine: 170 processes but 1 814 tasks, and ``Creating new namespace
    failed: Resource temporarily unavailable`` under a process-derived cap).
    """
    memory_bytes = command.memory_limit_mb * 1024 * 1024
    cpu_seconds = max(int(command.cpu_limit * command.timeout_seconds), 1)
    uid = os.getuid()
    running_tasks = 0
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            try:
                if entry.stat().st_uid == uid:
                    running_tasks += sum(1 for _ in Path(f"/proc/{entry.name}/task").iterdir())
            except OSError:  # pragma: no cover — a process that exited mid-scan
                continue
    nproc_cap = running_tasks + 256

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc_cap, nproc_cap))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 31, 1 << 31))

    return apply


def run_sandboxed(
    command: SandboxCommand,
    decision: SandboxDecision,
    *,
    run: Callable[[Invocation], InvocationResult] = run_invocation,
) -> InvocationResult:
    """Execute one command inside the decided sandbox tier — or refuse.

    **This is the bottom of the ladder, and it refuses.** A ``REFUSED`` decision raises before
    anything is built or started; there is no argument, environment variable or configuration
    that makes this function run a command on the host. A decided tier that fails at run time
    (the runtime disappeared between decision and use) surfaces as the invocation's own failure —
    it is never retried on a weaker tier, because tier selection happens exactly once, in
    :func:`select_tier`.

    Args:
        command: What to run and under which limits.
        decision: The tier decided for this run by :func:`select_tier`.
        run: The invocation runner; injected for tests.

    Returns:
        The invocation result, exactly as the sandboxed process produced it.

    Raises:
        SandboxUnavailable: ``decision.tier`` is ``REFUSED``. The message carries the decision's
            own reason, so the refusal a user sees names what was missing.
    """
    if decision.tier is SandboxTier.REFUSED:
        raise SandboxUnavailable(
            f"No sandbox tier is available for code execution: {decision.reason} "
            "There is no host-execution tier (ADR-0018).",
            details={"reason": decision.reason},
        )
    if decision.tier is SandboxTier.CONTAINER:
        runtime = decision.runtime or "docker"
        invocation = Invocation(
            argv=_container_argv(command, runtime),
            timeout_seconds=command.timeout_seconds,
            env={"PATH": "/usr/bin:/bin"},
            output_cap_bytes=command.output_cap_bytes,
        )
    else:
        invocation = Invocation(
            argv=_bwrap_argv(command),
            timeout_seconds=command.timeout_seconds,
            env={"PATH": "/usr/bin:/bin"},
            output_cap_bytes=command.output_cap_bytes,
            preexec=_bwrap_rlimits(command),
        )
    logger.info(
        "sandbox.execute",
        extra={"tier": decision.tier.value, "runtime": decision.runtime},
    )
    return run(invocation)


def _sequence_preview(argv: Sequence[str], limit: int = 6) -> str:  # pragma: no cover — logging aid
    """A short, safe preview of an argv for log lines."""
    preview = " ".join(argv[:limit])
    return preview if len(argv) <= limit else f"{preview} …"
