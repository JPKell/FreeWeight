"""BFCL adapter — Berkeley Function-Calling Leaderboard, tool-use / function calling.

BFCL scores a model's function calls per category (simple, multiple, parallel, relevance). Its
per-case records carry ``id``, ``category`` and ``valid`` (whether the predicted call matched the
expected one). The benchmark metric is per-category accuracy plus an overall.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from freeweight.external.adapters.base import AdapterOutcome, AdapterSample
from freeweight.external.adapters.parsing import excerpt, safe_json, safe_jsonl
from freeweight.external.manifest import ExternalManifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["BfclAdapter"]


class BfclAdapter:
    """Parses BFCL per-case validity into normalized results."""

    manifest = ExternalManifest(
        key="external.bfcl",
        name="BFCL (Berkeley Function-Calling Leaderboard)",
        version="1.0.0",
        category="tool_use",
        capabilities=("tool_use",),
        source_repository="https://github.com/ShishirPatil/gorilla",
        release_tag="bfcl-v3",
        commit="a1b2c3d",
        license="Apache-2.0",
        install_command=(),
        pinned_packages=(),
        requires_sandbox=False,
        metrics={"overall_accuracy": True},
    )

    def command(self, *, datasets_dir: Path, model_ref: str) -> Sequence[str]:
        """The argv that runs this benchmark against ``model_ref`` under its environment."""
        return ("bfcl", "evaluate", "--model", model_ref, "--result-dir", str(datasets_dir))

    def parse(self, raw_output: bytes) -> AdapterOutcome:
        """Parse the tool\'s recorded output as untrusted input; never raises on bad content."""
        document, error = safe_json(raw_output)
        if error is None and isinstance(document, list):
            rows: list[object] = document
            skipped = 0
        elif (
            error is None
            and isinstance(document, dict)
            and isinstance(document.get("results"), list)
        ):
            rows = document["results"]
            skipped = 0
        else:
            rows, skipped = safe_jsonl(raw_output)
        if not rows:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text=error or "BFCL output carried no case records",
            )
        samples: list[AdapterSample] = []
        by_category: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            sample = self._one_sample(row, index)
            if sample is None:
                skipped += 1
                continue
            samples.append(sample)
            if isinstance(row, dict) and sample.score is not None:
                category = str(row.get("category", "uncategorized"))
                by_category.setdefault(category, []).append(int(sample.score))
        if not samples:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="no BFCL case parsed",
            )
        scored = [int(s.score) for s in samples if s.score is not None]
        metrics: dict[str, float] = {}
        if scored:
            metrics["overall_accuracy"] = sum(scored) / len(scored)
        for category, values in by_category.items():
            metrics[f"accuracy.{category}"] = sum(values) / len(values)
        return AdapterOutcome(samples=tuple(samples), metrics=metrics, partial=skipped > 0)

    def _one_sample(self, row: object, index: int) -> AdapterSample | None:
        if not isinstance(row, dict):
            return None
        case_id = str(row.get("id", index))
        if "valid" not in row:
            return AdapterSample(
                case_id=case_id,
                score=None,
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="case record carried no 'valid' field",
            )
        valid = row["valid"]
        if not isinstance(valid, bool):
            return AdapterSample(
                case_id=case_id,
                score=None,
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="'valid' was not a boolean",
                detail={"raw": excerpt(valid)},
            )
        return AdapterSample(
            case_id=case_id,
            score=1.0 if valid else 0.0,
            detail={"category": row.get("category", "uncategorized")},
        )
