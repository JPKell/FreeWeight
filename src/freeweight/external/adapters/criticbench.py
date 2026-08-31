"""CriticBench adapter — critiquing across generation, critique and correction.

CriticBench measures a model's ability to generate an answer, critique an answer, and correct one.
Its records carry ``id``, ``dimension`` (``generation`` / ``critique`` / ``correction``) and a
0..1 ``score``. The metric is per-dimension accuracy plus an overall.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from freeweight.external.adapters.base import AdapterOutcome, AdapterSample
from freeweight.external.adapters.parsing import clamp_unit_score, safe_json, safe_jsonl
from freeweight.external.manifest import ExternalManifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["CriticBenchAdapter"]

_DIMENSIONS = ("generation", "critique", "correction")


class CriticBenchAdapter:
    """Parses CriticBench per-dimension scores into normalized results."""

    manifest = ExternalManifest(
        key="external.criticbench",
        name="CriticBench",
        version="1.0.0",
        category="critiquing",
        capabilities=("critiquing",),
        source_repository="https://github.com/CriticBench/CriticBench",
        release_tag="2024-02-01",
        commit="1a2b3c4",
        license="Apache-2.0",
        install_command=(),
        pinned_packages=(),
        requires_sandbox=False,
        metrics={"overall_score": True},
    )

    def command(self, *, datasets_dir: Path, model_ref: str) -> Sequence[str]:
        """The argv that runs this benchmark against ``model_ref`` under its environment."""
        return (
            "python",
            "-m",
            "criticbench.run",
            "--model",
            model_ref,
            "--data",
            str(datasets_dir),
        )

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
                error_text=error or "CriticBench output carried no records",
            )
        samples: list[AdapterSample] = []
        by_dimension: dict[str, list[float]] = {}
        for index, row in enumerate(rows):
            sample = self._one_sample(row, index)
            if sample is None:
                skipped += 1
                continue
            samples.append(sample)
            if isinstance(row, dict) and sample.score is not None:
                dimension = str(row.get("dimension", "unknown")).lower()
                by_dimension.setdefault(dimension, []).append(sample.score)
        if not samples:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="no CriticBench record parsed",
            )
        scored = [s.score for s in samples if s.score is not None]
        metrics: dict[str, float] = {}
        if scored:
            metrics["overall_score"] = sum(scored) / len(scored)
        for dimension in _DIMENSIONS:
            values = by_dimension.get(dimension)
            if values:
                metrics[f"score.{dimension}"] = sum(values) / len(values)
        return AdapterOutcome(samples=tuple(samples), metrics=metrics, partial=skipped > 0)

    def _one_sample(self, row: object, index: int) -> AdapterSample | None:
        if not isinstance(row, dict):
            return None
        case_id = str(row.get("id", index))
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
            detail={"dimension": row.get("dimension", "unknown")},
        )
