"""RULER adapter — long-context effective capacity across context lengths.

RULER scores retrieval/aggregation tasks at a ladder of context lengths (4k, 8k, 16k, …). Its
records carry ``context_length``, ``task`` and ``score`` (a 0..1 accuracy). The benchmark's
headline is per-length accuracy; the effective context is the largest length still above a
threshold, but that judgement is left to aggregation — the adapter reports the per-length numbers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from freeweight.external.adapters.base import AdapterOutcome, AdapterSample
from freeweight.external.adapters.parsing import clamp_unit_score, safe_json, safe_jsonl
from freeweight.external.manifest import ExternalManifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["RulerAdapter"]


class RulerAdapter:
    """Parses RULER per-length scores into normalized results."""

    manifest = ExternalManifest(
        key="external.ruler",
        name="RULER",
        version="1.0.0",
        category="long_context",
        capabilities=("long_context",),
        source_repository="https://github.com/NVIDIA/RULER",
        release_tag="2024-08-01",
        commit="9d1c2e4",
        license="Apache-2.0",
        install_command=(),
        pinned_packages=(),
        requires_sandbox=False,
        metrics={"average_score": True},
    )

    def command(self, *, datasets_dir: Path, model_ref: str) -> Sequence[str]:
        """The argv that runs this benchmark against ``model_ref`` under its environment."""
        return ("python", "-m", "ruler.eval", "--model", model_ref, "--data", str(datasets_dir))

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
                error_text=error or "RULER output carried no records",
            )
        samples: list[AdapterSample] = []
        by_length: dict[str, list[float]] = {}
        for index, row in enumerate(rows):
            sample = self._one_sample(row, index)
            if sample is None:
                skipped += 1
                continue
            samples.append(sample)
            if isinstance(row, dict) and sample.score is not None:
                length = str(row.get("context_length", "unknown"))
                by_length.setdefault(length, []).append(sample.score)
        if not samples:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="no RULER record parsed",
            )
        scored = [s.score for s in samples if s.score is not None]
        metrics: dict[str, float] = {}
        if scored:
            metrics["average_score"] = sum(scored) / len(scored)
        for length, values in by_length.items():
            metrics[f"score.{length}"] = sum(values) / len(values)
        return AdapterOutcome(samples=tuple(samples), metrics=metrics, partial=skipped > 0)

    def _one_sample(self, row: object, index: int) -> AdapterSample | None:
        if not isinstance(row, dict):
            return None
        case_id = str(
            row.get("id", f"{row.get('task', 'task')}-{row.get('context_length', index)}")
        )
        if "score" not in row:
            return AdapterSample(
                case_id=case_id,
                score=None,
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="record carried no 'score' field",
            )
        score = clamp_unit_score(row["score"])
        if score is None:
            return AdapterSample(
                case_id=case_id,
                score=None,
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="'score' was not a 0..1 number",
            )
        return AdapterSample(
            case_id=case_id,
            score=score,
            detail={"context_length": row.get("context_length"), "task": row.get("task")},
        )
