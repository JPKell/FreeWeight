"""lm-evaluation-harness adapter — MMLU-Pro and GSM8K, general capability.

Reads the harness's own results JSON: a top-level ``results`` object keyed by task, each carrying
metric entries named ``<metric>,<filter>`` (``exact_match,none``, ``acc,none``), and — when the
harness is run with ``--log_samples`` — a JSON-lines file of per-document records carrying
``doc_id`` and a boolean or 0/1 per metric. The summary metrics come from ``results``; the
per-case samples come from the samples file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from freeweight.external.adapters.base import AdapterOutcome, AdapterSample
from freeweight.external.adapters.parsing import (
    clamp_unit_score,
    excerpt,
    safe_json,
    safe_jsonl,
)
from freeweight.external.datasets import DatasetSpec
from freeweight.external.manifest import ExternalManifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["LmEvalHarnessAdapter"]

_SUMMARY_METRICS = ("exact_match", "acc", "acc_norm")


class LmEvalHarnessAdapter:
    """Parses EleutherAI lm-evaluation-harness output into normalized results."""

    manifest = ExternalManifest(
        key="external.lm_eval_harness",
        name="lm-evaluation-harness (MMLU-Pro, GSM8K)",
        version="1.0.0",
        category="general_capability",
        capabilities=("reasoning",),
        source_repository="https://github.com/EleutherAI/lm-evaluation-harness",
        release_tag="v0.4.5",
        commit="v0.4.5",
        license="MIT",
        install_command=(
            "python",
            "-m",
            "pip",
            "install",
            "lm-eval==0.4.5",
        ),
        pinned_packages=("lm-eval==0.4.5",),
        datasets=(
            DatasetSpec(
                name="mmlu_pro",
                url="https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro",
                sha256="sha256:" + "0" * 64,
                filename="mmlu_pro.parquet",
            ),
            DatasetSpec(
                name="gsm8k",
                url="https://huggingface.co/datasets/openai/gsm8k",
                sha256="sha256:" + "1" * 64,
                filename="gsm8k.parquet",
            ),
        ),
        requires_sandbox=False,
        metrics={"exact_match": True, "acc": True},
    )

    def command(self, *, datasets_dir: Path, model_ref: str) -> Sequence[str]:
        """The harness invocation. Datasets are pre-installed under ``datasets_dir``."""
        return (
            "lm_eval",
            "--model",
            "local-completions",
            "--model_args",
            f"model={model_ref}",
            "--tasks",
            "mmlu_pro,gsm8k",
            "--output_path",
            str(datasets_dir.parent / "results"),
            "--log_samples",
        )

    def parse(self, raw_output: bytes) -> AdapterOutcome:
        """Parse the harness results JSON (with optional embedded ``samples``)."""
        document, error = safe_json(raw_output)
        if error is not None:
            return self._parse_samples_only(raw_output, error)
        if not isinstance(document, dict):
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="harness output is not a JSON object",
            )
        results = document.get("results")
        metrics = self._summary_metrics(results) if isinstance(results, dict) else {}
        samples, partial = self._samples(document.get("samples"))
        if not samples and not metrics:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="harness output carried neither results nor samples",
            )
        return AdapterOutcome(samples=samples, metrics=metrics, partial=partial)

    def _summary_metrics(self, results: dict[str, object]) -> dict[str, float]:
        collected: dict[str, float] = {}
        for task, entry in results.items():
            if not isinstance(entry, dict):
                continue
            for raw_key, value in entry.items():
                metric = raw_key.split(",", 1)[0]
                if metric in _SUMMARY_METRICS:
                    score = clamp_unit_score(value)
                    if score is not None:
                        collected[f"{task}.{metric}"] = score
        return collected

    def _samples(self, samples: object) -> tuple[tuple[AdapterSample, ...], bool]:
        if not isinstance(samples, list):
            return (), False
        parsed: list[AdapterSample] = []
        skipped = 0
        for index, row in enumerate(samples):
            sample = self._one_sample(row, index)
            if sample is None:
                skipped += 1
                continue
            parsed.append(sample)
        return tuple(parsed), skipped > 0

    def _one_sample(self, row: object, index: int) -> AdapterSample | None:
        if not isinstance(row, dict):
            return None
        case_id = str(row.get("doc_id", index))
        for metric in _SUMMARY_METRICS:
            if metric in row:
                score = clamp_unit_score(row[metric])
                if score is None:
                    return AdapterSample(
                        case_id=case_id,
                        score=None,
                        error_code="EXTERNAL_BENCHMARK_FAILED",
                        error_text=f"case {case_id} carried a non-numeric {metric}",
                        detail={"raw": excerpt(row.get(metric))},
                    )
                return AdapterSample(
                    case_id=case_id,
                    score=score,
                    detail={
                        "metric": metric,
                        "target": excerpt(row.get("target", "")),
                        "response": excerpt(row.get("filtered_resps", row.get("resps", ""))),
                    },
                )
        return None

    def _parse_samples_only(self, raw_output: bytes, json_error: str) -> AdapterOutcome:
        """Fall back to JSON-lines: the harness's ``--log_samples`` writes one doc per line."""
        rows, skipped = safe_jsonl(raw_output)
        if not rows:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text=json_error,
            )
        samples, partial = self._samples(rows)
        if not samples:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="no per-document record parsed from JSON-lines output",
            )
        return AdapterOutcome(samples=samples, partial=partial or skipped > 0)
