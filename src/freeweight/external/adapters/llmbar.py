"""LLMBar adapter — judge robustness, including adversarial subsets.

LLMBar tests whether a judge prefers the objectively-better response even when the worse one is
written to look better. Its records carry ``id``, ``subset`` (``Natural`` or an adversarial name)
and ``correct``. The metric is accuracy overall and per subset, because the adversarial subsets
are the point: a judge that scores well on ``Natural`` and badly on ``Adversarial`` is fragile.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from freeweight.external.adapters.base import AdapterOutcome, AdapterSample
from freeweight.external.adapters.judgebench import _boolean_sample, _rows_or_error
from freeweight.external.manifest import ExternalManifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["LlmBarAdapter"]


class LlmBarAdapter:
    """Parses LLMBar per-subset correctness into normalized results."""

    manifest = ExternalManifest(
        key="external.llmbar",
        name="LLMBar",
        version="1.0.0",
        category="judging",
        capabilities=("judging",),
        source_repository="https://github.com/princeton-nlp/LLMBar",
        release_tag="2023-12-01",
        commit="c4d5e6f",
        license="MIT",
        install_command=(),
        pinned_packages=(),
        requires_sandbox=False,
        metrics={"overall_accuracy": True},
    )

    def command(self, *, datasets_dir: Path, model_ref: str) -> Sequence[str]:
        """The argv that runs this benchmark against ``model_ref`` under its environment."""
        return (
            "python",
            "-m",
            "llmbar.evaluate",
            "--judge",
            model_ref,
            "--data",
            str(datasets_dir),
        )

    def parse(self, raw_output: bytes) -> AdapterOutcome:
        """Parse the tool\'s recorded output as untrusted input; never raises on bad content."""
        rows, skipped, error = _rows_or_error(raw_output)
        if not rows:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text=error or "LLMBar output carried no records",
            )
        samples: list[AdapterSample] = []
        by_subset: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            sample = _boolean_sample(row, index, field_name="correct")
            if sample is None:
                skipped += 1
                continue
            samples.append(sample)
            if isinstance(row, dict) and sample.score is not None:
                subset = str(row.get("subset", "Natural"))
                by_subset.setdefault(subset, []).append(int(sample.score))
        if not samples:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="no LLMBar record parsed",
            )
        scored = [int(s.score) for s in samples if s.score is not None]
        metrics: dict[str, float] = {}
        if scored:
            metrics["overall_accuracy"] = sum(scored) / len(scored)
        for subset, values in by_subset.items():
            metrics[f"accuracy.{subset}"] = sum(values) / len(values)
        return AdapterOutcome(samples=tuple(samples), metrics=metrics, partial=skipped > 0)
