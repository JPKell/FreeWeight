"""The external framework end to end on recorded fixtures: environment, run, normalize, provenance.

No network and no real benchmark install — the invocation runner and sandbox runner are injected,
and the "tool output" is a recorded fixture. What is exercised is the framework's own contract:
verify → run → parse → normalize, with the sandbox tier recorded, a hang killed and reported, and
a sandbox-required benchmark skipped (never host-run) when no tier is available.
"""

from __future__ import annotations

import sys
from pathlib import Path

from freeweight.external.adapters import Adapter, get_adapter
from freeweight.external.environment import (
    BenchmarkEnvironment,
    assert_no_contamination,
    external_module_prefixes,
    snapshot_modules,
)
from freeweight.external.framework import run_external_benchmark
from freeweight.external.invocation import InvocationResult
from freeweight.external.sandbox import SandboxCommand, SandboxDecision, SandboxTier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "external"


def _adapter(key: str) -> Adapter:
    adapter = get_adapter(key)
    assert adapter is not None, key
    return adapter


def _result(stdout: bytes, *, exit_code: int = 0, timed_out: bool = False) -> InvocationResult:
    return InvocationResult(
        argv=("x",),
        exit_code=exit_code,
        stdout=stdout,
        stderr=b"",
        duration_ms=1.0,
        timed_out=timed_out,
        output_truncated=False,
    )


def _install(adapter: Adapter, tmp_path: Path) -> BenchmarkEnvironment:
    """Install the benchmark and lay down each pinned dataset with content matching its hash.

    verify() is a real gate: it re-hashes each dataset against the manifest's pin. The test does
    not know the pinned files' true bytes (they are not redistributed), so it rewrites the
    manifest's pins to the hash of stub content — exercising the framework, not the pins, which
    have their own dedicated tests in ``tests/security/test_dataset_verification.py``.
    """
    import dataclasses

    from freeweight.external.datasets import sha256_file

    manifest = adapter.manifest
    env = BenchmarkEnvironment(manifest, tmp_path)
    env.install(timeout_seconds=1, run=lambda _inv: _result(b""))
    if manifest.datasets:
        repinned = []
        for spec in manifest.datasets:
            path = env.datasets_dir / spec.filename
            path.write_bytes(b"stub-" + spec.name.encode())
            repinned.append(dataclasses.replace(spec, sha256=sha256_file(path)))
        object.__setattr__(
            env, "_manifest", dataclasses.replace(manifest, datasets=tuple(repinned))
        )
    return env


class TestANonSandboxBenchmark:
    def test_it_runs_and_normalizes_with_provenance(self, tmp_path: Path) -> None:
        adapter = _adapter("external.bfcl")
        env = _install(adapter, tmp_path)
        output = _result((FIXTURES / "bfcl.clean.json").read_bytes())

        result = run_external_benchmark(
            adapter,
            env,
            model_ref="fake/model@sha256:abc",
            sandbox_decision=SandboxDecision(
                tier=SandboxTier.REFUSED, runtime=None, reason="none needed"
            ),
            invocation_timeout_seconds=30,
            sandbox_timeout_seconds=30,
            run=lambda _inv: output,
        )

        assert result.ok
        assert result.sandbox_tier == "none", "a benchmark needing no sandbox records 'none'"
        assert result.metrics["overall_accuracy"] > 0
        assert str(result.provenance["source_repository"]).startswith("https://github.com/")
        assert result.provenance["commit"]
        assert result.provenance["release_tag"]

    def test_a_hang_is_killed_and_reported_with_its_timeout(self, tmp_path: Path) -> None:
        adapter = _adapter("external.bfcl")
        env = _install(adapter, tmp_path)

        result = run_external_benchmark(
            adapter,
            env,
            model_ref="fake/model",
            sandbox_decision=SandboxDecision(SandboxTier.REFUSED, None, "n/a"),
            invocation_timeout_seconds=5,
            sandbox_timeout_seconds=5,
            run=lambda _inv: _result(b"", timed_out=True),
        )

        assert not result.ok
        assert result.error_code == "EXTERNAL_BENCHMARK_FAILED"
        assert "timeout" in (result.error_text or "").lower()

    def test_a_nonzero_exit_is_reported(self, tmp_path: Path) -> None:
        adapter = _adapter("external.bfcl")
        env = _install(adapter, tmp_path)

        result = run_external_benchmark(
            adapter,
            env,
            model_ref="fake/model",
            sandbox_decision=SandboxDecision(SandboxTier.REFUSED, None, "n/a"),
            invocation_timeout_seconds=5,
            sandbox_timeout_seconds=5,
            run=lambda _inv: _result(b"boom", exit_code=1),
        )

        assert result.error_code == "EXTERNAL_BENCHMARK_FAILED"


class TestASandboxBenchmark:
    def test_it_is_skipped_when_no_tier_is_available(self, tmp_path: Path) -> None:
        adapter = _adapter("external.evalplus")  # requires_sandbox = True
        env = _install(adapter, tmp_path)

        result = run_external_benchmark(
            adapter,
            env,
            model_ref="fake/model",
            sandbox_decision=SandboxDecision(SandboxTier.REFUSED, None, "nothing installed"),
            invocation_timeout_seconds=5,
            sandbox_timeout_seconds=5,
            run=lambda _inv: _result(b"should never run"),
            sandbox_run=lambda _cmd, _dec: _result(b"should never run"),
        )

        assert result.skipped
        assert result.error_code == "SANDBOX_UNAVAILABLE"
        assert result.sandbox_tier == "refused"

    def test_it_records_the_tier_when_one_is_available(self, tmp_path: Path) -> None:
        adapter = _adapter("external.evalplus")
        env = _install(adapter, tmp_path)
        output = _result((FIXTURES / "evalplus.clean.json").read_bytes())
        ran: list[SandboxCommand] = []

        def sandbox_run(command: SandboxCommand, decision: SandboxDecision) -> InvocationResult:
            ran.append(command)
            return output

        result = run_external_benchmark(
            adapter,
            env,
            model_ref="fake/model",
            sandbox_decision=SandboxDecision(SandboxTier.CONTAINER, "docker", "available"),
            invocation_timeout_seconds=5,
            sandbox_timeout_seconds=60,
            run=lambda _inv: _result(b"WRONG PATH"),
            sandbox_run=sandbox_run,
        )

        assert result.ok
        assert result.sandbox_tier == "container"
        assert len(ran) == 1, (
            "a sandboxed benchmark must go through the sandbox runner exactly once"
        )


class TestEnvironmentIsolation:
    def test_no_external_module_is_imported_by_using_the_subsystem(self) -> None:
        """P13 AC3: exercising the whole subsystem imports nothing from an external benchmark."""
        before = snapshot_modules()
        # Touch every adapter, run one end to end — the ordinary usage that must stay isolated.
        from freeweight.external.adapters import ADAPTERS

        for adapter in ADAPTERS.values():
            adapter.parse(
                b'[{"id": "x", "valid": true, "correct": true, "passed": true, '
                b'"score": 1.0, "follow_instruction_list": [true]}]'
            )
        after = snapshot_modules()

        assert_no_contamination(before, after)  # raises if any external package leaked in
        leaked = {
            name for name in after - before if name.split(".", 1)[0] in external_module_prefixes()
        }
        assert leaked == set()

    def test_the_guard_catches_a_simulated_leak(self) -> None:
        """The isolation proof is real: a synthetic external import is caught."""
        before = {"os", "sys"}
        after = {"os", "sys", "lm_eval", "lm_eval.tasks", "torch"}

        try:
            assert_no_contamination(before, after)
        except Exception as exc:  # noqa: BLE001 — asserting the type below
            assert exc.code == "EXTERNAL_BENCHMARK_FAILED"  # type: ignore[attr-defined]
            assert "lm_eval" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("a leaked external module was not caught")

    def test_sys_modules_holds_no_external_package_right_now(self) -> None:
        present = {
            name for name in sys.modules if name.split(".", 1)[0] in external_module_prefixes()
        }
        assert present == set(), f"an external package is already imported: {present}"
