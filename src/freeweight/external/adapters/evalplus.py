"""EvalPlus adapter — HumanEval(+) and MBPP(+), executable coding verification.

EvalPlus executes generated code against base and augmented ("plus") test suites. Its ``eval``
step writes a results JSON keyed by task id, each carrying ``base_status`` and ``plus_status``
(``"pass"`` / ``"fail"`` / ``"timeout"``). The case score is base-pass; ``fragility`` — the spec's
``base − plus`` — is reported as a benchmark metric because a model that passes the base tests but
fails the harder plus tests has fragile solutions.

**This benchmark executes model-generated code and therefore requires a sandbox tier.** The
manifest declares ``requires_sandbox=True``; the run engine refuses to run it when no tier is
available (`sandbox_unavailable`), and never on the host.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from freeweight.external.adapters.base import AdapterOutcome, AdapterSample
from freeweight.external.adapters.parsing import safe_json
from freeweight.external.datasets import DatasetSpec
from freeweight.external.manifest import ExternalManifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["EvalPlusAdapter"]

_PASS = "pass"  # noqa: S105 — a status literal, not a secret


class EvalPlusAdapter:
    """Parses EvalPlus results JSON into normalized results (base-pass is the case score)."""

    manifest = ExternalManifest(
        key="external.evalplus",
        name="EvalPlus (HumanEval+, MBPP+)",
        version="1.0.0",
        category="coding",
        capabilities=("coding",),
        source_repository="https://github.com/evalplus/evalplus",
        release_tag="v0.3.1",
        commit="v0.3.1",
        license="Apache-2.0",
        install_command=("python", "-m", "pip", "install", "evalplus==0.3.1"),
        pinned_packages=("evalplus==0.3.1",),
        datasets=(
            DatasetSpec(
                name="humaneval_plus",
                url="https://github.com/evalplus/humanevalplus_release",
                sha256="sha256:" + "2" * 64,
                filename="HumanEvalPlus.jsonl.gz",
            ),
        ),
        requires_sandbox=True,
        metrics={"pass_at_1": True, "fragility": False},
    )

    def command(self, *, datasets_dir: Path, model_ref: str) -> Sequence[str]:
        """The argv run *inside the sandbox* — EvalPlus executes generated code."""
        return (
            "python",
            "-m",
            "evalplus.evaluate",
            "--dataset",
            "humaneval",
            "--samples",
            str(datasets_dir / "samples.jsonl"),
        )

    def parse(self, raw_output: bytes) -> AdapterOutcome:
        """Parse EvalPlus's per-task base/plus statuses."""
        document, error = safe_json(raw_output)
        if error is not None:
            return AdapterOutcome(error_code="EXTERNAL_BENCHMARK_FAILED", error_text=error)
        results = self._results_object(document)
        if results is None:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="EvalPlus output carried no 'eval' results object",
            )
        samples: list[AdapterSample] = []
        base_passes = 0
        plus_passes = 0
        skipped = 0
        for task_id, entry in results.items():
            sample = self._one_sample(str(task_id), entry)
            if sample is None:
                skipped += 1
                continue
            samples.append(sample)
            if sample.score is None:
                # An unscored case contributes to neither rate — its plus_status is not counted
                # against a base_status it never had (ADR-0016).
                continue
            if sample.score == 1.0:
                base_passes += 1
            if isinstance(entry, dict) and self._status(entry, "plus_status") == _PASS:
                plus_passes += 1
        if not samples:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="no EvalPlus task result parsed",
            )
        # Aggregates are over the *scored* samples: an unscored case (no base_status) is
        # excluded, never counted as a zero (ADR-0016).
        scored = sum(1 for s in samples if s.score is not None)
        metrics = (
            {
                "pass_at_1": base_passes / scored,
                "fragility": (base_passes - plus_passes) / scored,
            }
            if scored
            else {}
        )
        return AdapterOutcome(samples=tuple(samples), metrics=metrics, partial=skipped > 0)

    def _results_object(self, document: object) -> dict[str, object] | None:
        if not isinstance(document, dict):
            return None
        results = document.get("eval", document)
        return results if isinstance(results, dict) else None

    def _status(self, entry: dict[str, object], key: str) -> str | None:
        value = entry.get(key)
        # EvalPlus sometimes nests the status under a single-element list.
        if isinstance(value, list) and value:
            value = value[0]
        return value.lower() if isinstance(value, str) else None

    def _one_sample(self, task_id: str, entry: object) -> AdapterSample | None:
        if not isinstance(entry, dict):
            return None
        base = self._status(entry, "base_status")
        if base is None:
            return AdapterSample(
                case_id=task_id,
                score=None,
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="task record carried no base_status",
            )
        plus = self._status(entry, "plus_status")
        return AdapterSample(
            case_id=task_id,
            score=1.0 if base == _PASS else 0.0,
            detail={"base_status": base, "plus_status": plus or "unknown"},
        )
