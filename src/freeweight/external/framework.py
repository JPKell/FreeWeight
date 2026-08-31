"""freeweight.external.framework — drive one external benchmark and normalize its result.

This ties the pieces together for one benchmark run: verify the environment and its datasets,
run the tool (sandboxed when it executes code, plain subprocess otherwise, always through the one
invocation contract), parse its output as untrusted input, and hand back an
:class:`ExternalRunResult` carrying the normalized samples, the benchmark-level metrics, the
sandbox tier used and the full provenance block — everything a native result carries plus the
external provenance ADR-0018 requires.

Error translation is the framework's job, not the adapter's: a subprocess that failed, hung or
produced unparseable output becomes ``EXTERNAL_BENCHMARK_FAILED`` with a reason, and a benchmark
that needs a sandbox on a machine with none becomes ``SANDBOX_UNAVAILABLE`` and *skips* — it is
never run on the host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from freeweight.external.errors import ExternalBenchmarkFailed, SandboxUnavailable
from freeweight.external.invocation import Invocation, run_invocation
from freeweight.external.sandbox import (
    SandboxCommand,
    SandboxDecision,
    SandboxTier,
    run_sandboxed,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from freeweight.external.adapters.base import Adapter, AdapterSample
    from freeweight.external.environment import BenchmarkEnvironment
    from freeweight.external.invocation import InvocationResult

__all__ = ["ExternalRunResult", "run_external_benchmark"]


@dataclass(frozen=True, slots=True)
class ExternalRunResult:
    """One external benchmark's normalized result, ready to persist beside native results.

    Attributes:
        key: The benchmark key.
        samples: The per-case normalized samples.
        metrics: Benchmark-level metric values.
        sandbox_tier: The tier recorded on this result: ``"container"``, ``"bwrap"``, ``"refused"``
            or ``"none"`` (a benchmark that needed no sandbox). Every result carries it, so a
            performance comparison across tiers is labelled and a correctness comparison is not
            misled.
        provenance: The manifest's provenance block — source, tag, commit, licence, dataset hashes.
        error_code: Set when the benchmark could not run or parse; ``None`` on success.
        error_text: The reason, when ``error_code`` is set.
        skipped: ``True`` when the benchmark was skipped (sandbox unavailable, dataset missing)
            rather than run and failed. A skip is a recorded reason, never a zero score.
    """

    key: str
    samples: tuple[AdapterSample, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    sandbox_tier: str = "none"
    provenance: Mapping[str, object] = field(default_factory=dict)
    error_code: str | None = None
    error_text: str | None = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        """Whether the benchmark ran and parsed cleanly."""
        return self.error_code is None and not self.skipped


def run_external_benchmark(
    adapter: Adapter,
    environment: BenchmarkEnvironment,
    *,
    model_ref: str,
    sandbox_decision: SandboxDecision,
    invocation_timeout_seconds: float,
    sandbox_timeout_seconds: float,
    run: Callable[[Invocation], InvocationResult] = run_invocation,
    sandbox_run: Callable[[SandboxCommand, SandboxDecision], InvocationResult] | None = None,
) -> ExternalRunResult:
    """Run one external benchmark end to end and return its normalized result.

    The one function every entry point (the run engine, the CLI, a route) reaches external
    execution through — there is no second path, so the sandbox refusal and the error translation
    apply uniformly.

    Args:
        adapter: The benchmark adapter.
        environment: Its installed environment.
        model_ref: The model to run against.
        sandbox_decision: The tier decided for this run by
            :func:`~freeweight.external.sandbox.select_tier`.
        invocation_timeout_seconds: Budget for a non-sandboxed benchmark's subprocess.
        sandbox_timeout_seconds: Budget for a sandboxed benchmark's execution.
        run: The plain invocation runner; injected for tests.
        sandbox_run: The sandboxed runner; injected for tests. Defaults to
            :func:`~freeweight.external.sandbox.run_sandboxed`.

    Returns:
        The normalized result — never raises for a *measurement* failure, which is recorded on the
        result instead so a batch of benchmarks continues past one that failed.
    """
    manifest = adapter.manifest
    provenance = manifest.provenance()

    # A code-execution benchmark on a machine with no tier is skipped — never run on the host.
    if manifest.requires_sandbox and sandbox_decision.tier is SandboxTier.REFUSED:
        return ExternalRunResult(
            key=manifest.key,
            sandbox_tier="refused",
            provenance=provenance,
            error_code="SANDBOX_UNAVAILABLE",
            error_text=(
                f"{manifest.key!r} executes generated code and no sandbox tier is available: "
                f"{sandbox_decision.reason}"
            ),
            skipped=True,
        )

    try:
        environment.verify()
    except (ExternalBenchmarkFailed, SandboxUnavailable) as exc:
        return ExternalRunResult(
            key=manifest.key,
            provenance=provenance,
            error_code=exc.code,
            error_text=exc.message,
            skipped=True,
        )
    except Exception as exc:  # noqa: BLE001 — a dataset problem is a skip, not a crash
        return ExternalRunResult(
            key=manifest.key,
            provenance=provenance,
            error_code=getattr(exc, "code", "DATASET_MISSING"),
            error_text=str(exc),
            skipped=True,
        )

    tier_label = "none"
    try:
        if manifest.requires_sandbox:
            tier_label = sandbox_decision.label
            output = _run_sandboxed(
                adapter,
                environment,
                model_ref=model_ref,
                decision=sandbox_decision,
                timeout_seconds=sandbox_timeout_seconds,
                sandbox_run=sandbox_run or run_sandboxed,
            )
        else:
            output = _run_plain(
                adapter,
                environment,
                model_ref=model_ref,
                timeout_seconds=invocation_timeout_seconds,
                run=run,
            )
    except SandboxUnavailable as exc:
        return ExternalRunResult(
            key=manifest.key,
            sandbox_tier="refused",
            provenance=provenance,
            error_code=exc.code,
            error_text=exc.message,
            skipped=True,
        )

    if output.timed_out:
        budget = _effective_timeout(manifest, invocation_timeout_seconds, sandbox_timeout_seconds)
        return ExternalRunResult(
            key=manifest.key,
            sandbox_tier=tier_label,
            provenance=provenance,
            error_code="EXTERNAL_BENCHMARK_FAILED",
            error_text=f"{manifest.key!r} exceeded its {budget}-second timeout and was killed.",
        )
    if output.exit_code != 0:
        return ExternalRunResult(
            key=manifest.key,
            sandbox_tier=tier_label,
            provenance=provenance,
            error_code="EXTERNAL_BENCHMARK_FAILED",
            error_text=(
                f"{manifest.key!r} exited {output.exit_code}: "
                f"{output.stderr.decode('utf-8', 'replace')[:200]}"
            ),
        )

    outcome = adapter.parse(output.stdout)
    if not outcome.ok:
        return ExternalRunResult(
            key=manifest.key,
            samples=outcome.samples,
            sandbox_tier=tier_label,
            provenance=provenance,
            error_code=outcome.error_code or "EXTERNAL_BENCHMARK_FAILED",
            error_text=outcome.error_text or "no samples parsed",
        )
    return ExternalRunResult(
        key=manifest.key,
        samples=outcome.samples,
        metrics=outcome.metrics,
        sandbox_tier=tier_label,
        provenance={**provenance, "partial_parse": outcome.partial},
    )


def _run_plain(
    adapter: Adapter,
    environment: BenchmarkEnvironment,
    *,
    model_ref: str,
    timeout_seconds: float,
    run: Callable[[Invocation], InvocationResult],
) -> InvocationResult:
    argv = _command(adapter, environment, model_ref)
    return run(
        Invocation(
            argv=tuple(argv),
            timeout_seconds=timeout_seconds,
            cwd=environment.root,
            env={"PATH": "/usr/bin:/bin", "HOME": str(environment.root)},
        )
    )


def _run_sandboxed(
    adapter: Adapter,
    environment: BenchmarkEnvironment,
    *,
    model_ref: str,
    decision: SandboxDecision,
    timeout_seconds: float,
    sandbox_run: Callable[[SandboxCommand, SandboxDecision], InvocationResult],
) -> InvocationResult:
    argv = _command(adapter, environment, model_ref)
    return sandbox_run(
        SandboxCommand(
            argv=tuple(argv),
            workdir=environment.datasets_dir,
            timeout_seconds=timeout_seconds,
        ),
        decision,
    )


def _command(adapter: Adapter, environment: BenchmarkEnvironment, model_ref: str) -> Sequence[str]:
    return adapter.command(datasets_dir=environment.datasets_dir, model_ref=model_ref)


def _effective_timeout(
    manifest: object, invocation_timeout_seconds: float, sandbox_timeout_seconds: float
) -> float:
    """The timeout that applied: the sandbox budget when the benchmark needs one, else the plain."""
    if getattr(manifest, "requires_sandbox", False):
        return sandbox_timeout_seconds
    return invocation_timeout_seconds
