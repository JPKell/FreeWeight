"""freeweight.external.invocation — how any external process is started, watched and stopped.

One function starts every subprocess the external subsystem runs — environment creation, dataset
tooling, benchmark invocation, sandboxed execution. The rules it enforces are Security Standards
§1 T1→T2 and ADR-0018's invocation contract, and they hold for every caller because there is no
second starter:

* an **argument list**, never a shell — nothing here interpolates into a command string;
* a **wall-clock timeout**, after which the whole process group is terminated and, if it does not
  exit, killed;
* **captured output with a size cap**, because a subprocess that floods stdout must not exhaust
  the host's memory, and its output is untrusted input either way;
* **no inherited environment** beyond what the caller names — a subprocess never receives this
  process's secrets by default.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

__all__ = ["Invocation", "InvocationResult", "run_invocation"]

_TERMINATE_GRACE_SECONDS = 5.0
_DEFAULT_OUTPUT_CAP_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Invocation:
    """One subprocess to run: what, where, under which limits.

    Attributes:
        argv: The argument list. Never joined into a string, never passed to a shell.
        timeout_seconds: Wall-clock budget. When exceeded the process group is terminated, then
            killed, and the result says so.
        cwd: Working directory, or ``None`` for the current one.
        env: The **complete** environment the process sees. This process's environment is not
            inherited — a benchmark subprocess has no business reading credentials this process
            was started with. Callers that need ``PATH`` pass one.
        output_cap_bytes: The most stdout+stderr may occupy together; beyond it the process is
            killed and the result is marked truncated.
        preexec: A callable run in the child before ``exec`` (rlimits for the bwrap tier), or
            ``None``.
    """

    argv: tuple[str, ...]
    timeout_seconds: float
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    output_cap_bytes: int = _DEFAULT_OUTPUT_CAP_BYTES
    preexec: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """What one subprocess did.

    Attributes:
        argv: The argument list that ran, for the invocation record.
        exit_code: The process's exit code, or the negative signal number if it died to one.
        stdout: Captured standard output (possibly truncated at the cap).
        stderr: Captured standard error (possibly truncated at the cap).
        duration_ms: Wall-clock time from start to reaped, in milliseconds.
        timed_out: Whether the wall-clock budget was exceeded. A timed-out process was killed;
            ``exit_code`` then reports how it died, and the caller reports the budget it blew.
        output_truncated: Whether the output cap cut the capture short.
    """

    argv: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_ms: float
    timed_out: bool
    output_truncated: bool


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process group; escalate to SIGKILL if it survives the grace period."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_invocation(invocation: Invocation) -> InvocationResult:
    """Run one subprocess to completion under its limits, and report what happened.

    Never raises for anything the subprocess does: a non-zero exit, a timeout, a kill and a
    flood of output are all *results*, reported as data for the caller to translate. It raises
    only when the process cannot be started at all (``FileNotFoundError`` for a missing
    executable, ``PermissionError``), because that is a fact about this machine rather than about
    the benchmark.

    The child is started in its own session (`start_new_session=True`), so a timeout kills the
    whole process tree — a hanging benchmark that spawned workers does not leave them behind.

    Args:
        invocation: What to run and under which limits.

    Returns:
        The result, including captured (possibly truncated) output and how the process ended.

    Raises:
        FileNotFoundError: ``argv[0]`` does not exist.
        PermissionError: ``argv[0]`` is not executable.
    """
    started = time.perf_counter()
    process = subprocess.Popen(  # noqa: S603 — argv list, no shell; the module contract
        list(invocation.argv),
        bufsize=0,  # raw pipes: select() and read() agree about readiness, with no hidden buffer
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        cwd=str(invocation.cwd) if invocation.cwd is not None else None,
        env=dict(invocation.env),
        start_new_session=True,
        preexec_fn=invocation.preexec,  # noqa: PLW1509 — rlimits must apply in the child
    )
    assert process.stdout is not None and process.stderr is not None  # noqa: S101 — PIPE above

    deadline = time.monotonic() + invocation.timeout_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    sizes = {"stdout": 0, "stderr": 0}
    timed_out = False
    truncated = False

    try:
        open_streams = 2
        while open_streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(timeout=min(remaining, 0.5)):
                name = key.data
                chunk = key.fileobj.read(65536)  # type: ignore[union-attr]
                if not chunk:
                    selector.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                room = invocation.output_cap_bytes - (sizes["stdout"] + sizes["stderr"])
                if room <= 0:
                    truncated = True
                    break
                kept = chunk[:room]
                truncated = truncated or len(kept) < len(chunk)
                captured[name].append(kept)
                sizes[name] += len(kept)
            if truncated:
                break
        if timed_out or truncated:
            _kill_group(process)
            process.wait(timeout=_TERMINATE_GRACE_SECONDS * 2)
        else:
            # Both streams closed but the process may still be running (a benchmark that closes
            # its pipes early and keeps computing is within its rights) — wait out the remainder
            # of the budget, not a fixed grace.
            process.wait(timeout=max(deadline - time.monotonic(), 0.0))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(process)
        process.wait()
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    duration_ms = (time.perf_counter() - started) * 1000.0
    return InvocationResult(
        argv=invocation.argv,
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=b"".join(captured["stdout"]),
        stderr=b"".join(captured["stderr"]),
        duration_ms=duration_ms,
        timed_out=timed_out,
        output_truncated=truncated,
    )
