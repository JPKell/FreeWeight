"""freeweight.external.errors — the typed refusals of the external-benchmark subsystem.

Each code is in spec §13's stable set. They live here rather than in a shared module because
every one of them names a fact about an external benchmark's environment, dataset or sandbox —
nothing else in the application raises them.
"""

from __future__ import annotations

from typing import ClassVar

from baseaicore import SuiteError

__all__ = [
    "DatasetHashMismatch",
    "DatasetMissing",
    "ExternalBenchmarkFailed",
    "SandboxUnavailable",
]


class SandboxUnavailable(SuiteError):
    """No sandbox tier is available (or configuration selected one that is not).

    The bottom of ADR-0018's tier ladder: container → bwrap → **refuse**. This error *is* the
    refusal — there is no host-execution tier, no flag to create one, and no code path that
    catches this and runs the command anyway. A benchmark that needs a sandbox on a machine with
    none is skipped with ``sandbox_unavailable`` recorded, never run.
    """

    code: ClassVar[str] = "SANDBOX_UNAVAILABLE"


class DatasetMissing(SuiteError):
    """A benchmark's dataset is not installed where its manifest says it lives.

    The remedy is ``freeweight external install <benchmark>``; the message names it.
    """

    code: ClassVar[str] = "DATASET_MISSING"


class DatasetHashMismatch(SuiteError):
    """An installed or downloaded dataset does not hash to the manifest's pinned value.

    Raised **before** the dataset is used, ever — an unpinned dataset invalidates every
    comparison made against it. ``details`` always carries ``expected_sha256`` and
    ``actual_sha256``, because a refusal that names only one hash cannot be diagnosed.
    """

    code: ClassVar[str] = "DATASET_HASH_MISMATCH"


class ExternalBenchmarkFailed(SuiteError):
    """An external benchmark's subprocess failed, hung past its timeout, or produced output
    that does not parse.

    The adapter's output is untrusted input: malformed output is this error with the parse
    problem in ``details``, never a partial-parse rescue.
    """

    code: ClassVar[str] = "EXTERNAL_BENCHMARK_FAILED"
