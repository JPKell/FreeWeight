"""CRUXEval adapter — code reasoning (predict output / predict input), executable verification.

CRUXEval asks a model to predict a function's output for a given input, and its input for a given
output. Correctness is decided by executing the function, so this benchmark requires a sandbox.
Its per-case records carry ``passed`` (a boolean) and the task ``id``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from freeweight.external.adapters.base import AdapterOutcome, AdapterSample
from freeweight.external.adapters.parsing import excerpt, safe_json, safe_jsonl
from freeweight.external.manifest import ExternalManifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["CruxEvalAdapter"]


class CruxEvalAdapter:
    """Parses CRUXEval pass/fail records into normalized results."""

    manifest = ExternalManifest(
        key="external.cruxeval",
        name="CRUXEval",
        version="1.0.0",
        category="code_reasoning",
        capabilities=("reasoning", "coding"),
        source_repository="https://github.com/facebookresearch/cruxeval",
        release_tag="2024-01-05",
        commit="fdb0b8f",
        license="MIT",
        install_command=(),
        pinned_packages=(),
        requires_sandbox=True,
        metrics={"pass_at_1_input": True, "pass_at_1_output": True, "pass_at_1": True},
    )

    def command(self, *, datasets_dir: Path, model_ref: str) -> Sequence[str]:
        """The argv that runs this benchmark against ``model_ref`` under its environment."""
        return ("python", "-m", "cruxeval.evaluate", "--generations", str(datasets_dir))

    def parse(self, raw_output: bytes) -> AdapterOutcome:
        """Parse the tool\'s recorded output as untrusted input; never raises on bad content."""
        document, error = safe_json(raw_output)
        rows: list[object]
        skipped = 0
        if error is None and isinstance(document, list):
            rows = document
        elif (
            error is None
            and isinstance(document, dict)
            and isinstance(document.get("results"), list)
        ):
            rows = document["results"]
        else:
            rows, skipped = safe_jsonl(raw_output)
        if not rows:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text=error or "CRUXEval output carried no case records",
            )
        samples: list[AdapterSample] = []
        by_mode: dict[str, list[int]] = {"input": [], "output": []}
        for index, row in enumerate(rows):
            sample = self._one_sample(row, index)
            if sample is None:
                skipped += 1
                continue
            samples.append(sample)
            if isinstance(row, dict) and sample.score is not None:
                mode = str(row.get("mode", "")).lower()
                if mode in by_mode:
                    by_mode[mode].append(int(sample.score))
        if not samples:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="no CRUXEval case parsed",
            )
        scored = [s.score for s in samples if s.score is not None]
        metrics: dict[str, float] = {}
        if scored:
            metrics["pass_at_1"] = sum(scored) / len(scored)
        for mode, values in by_mode.items():
            if values:
                metrics[f"pass_at_1_{mode}"] = sum(values) / len(values)
        return AdapterOutcome(samples=tuple(samples), metrics=metrics, partial=skipped > 0)

    def _one_sample(self, row: object, index: int) -> AdapterSample | None:
        if not isinstance(row, dict):
            return None
        case_id = str(row.get("id", index))
        if "passed" not in row:
            return AdapterSample(
                case_id=case_id,
                score=None,
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="case record carried no 'passed' field",
            )
        passed = row["passed"]
        if not isinstance(passed, bool):
            return AdapterSample(
                case_id=case_id,
                score=None,
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="'passed' was not a boolean",
                detail={"raw": excerpt(passed)},
            )
        return AdapterSample(
            case_id=case_id,
            score=1.0 if passed else 0.0,
            detail={"mode": row.get("mode", "unknown")},
        )
