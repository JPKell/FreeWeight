"""IFEval adapter — instruction following, objectively verifiable instruction types.

IFEval reports two accuracies over a set of instructions per prompt: strict and loose. Its
per-prompt records carry ``follow_instruction_list`` (a list of booleans, one per instruction)
and ``instruction_id_list``. A prompt's case score is the fraction of its instructions followed
under strict checking; the benchmark-level metrics are the prompt-level and instruction-level
strict accuracies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from freeweight.external.adapters.base import AdapterOutcome, AdapterSample
from freeweight.external.adapters.parsing import excerpt, safe_json, safe_jsonl
from freeweight.external.manifest import ExternalManifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["IFEvalAdapter"]


class IFEvalAdapter:
    """Parses IFEval per-prompt output into normalized results."""

    manifest = ExternalManifest(
        key="external.ifeval",
        name="IFEval",
        version="1.0.0",
        category="instruction_following",
        capabilities=("instruction_following",),
        source_repository="https://github.com/google-research/google-research/tree/master/instruction_following_eval",
        release_tag="2023-11-14",
        commit="e39c7f5",
        license="Apache-2.0",
        install_command=("python", "-m", "pip", "install", "instruction-following-eval==0.1.0"),
        pinned_packages=("instruction-following-eval==0.1.0",),
        datasets=(),
        requires_sandbox=False,
        metrics={
            "prompt_level_strict_accuracy": True,
            "instruction_level_strict_accuracy": True,
        },
    )

    def command(self, *, datasets_dir: Path, model_ref: str) -> Sequence[str]:
        """The argv that runs this benchmark against ``model_ref`` under its environment."""
        return (
            "python",
            "-m",
            "instruction_following_eval.evaluation_main",
            "--input_response_data",
            str(datasets_dir / "responses.jsonl"),
        )

    def parse(self, raw_output: bytes) -> AdapterOutcome:
        """Parse IFEval output: a summary object, or per-prompt JSON-lines."""
        document, error = safe_json(raw_output)
        if error is None and isinstance(document, dict) and "records" in document:
            return self._from_summary(document)
        rows, skipped = safe_jsonl(raw_output)
        if not rows:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text=error or "IFEval output carried no per-prompt records",
            )
        return self._from_rows(rows, skipped)

    def _from_summary(self, document: dict[str, object]) -> AdapterOutcome:
        records = document.get("records")
        if not isinstance(records, list):
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="IFEval 'records' is not a list",
            )
        return self._from_rows(records, 0)

    def _from_rows(self, rows: list[object], skipped: int) -> AdapterOutcome:
        samples: list[AdapterSample] = []
        followed_total = 0
        instructions_total = 0
        prompts_all_followed = 0
        for index, row in enumerate(rows):
            sample = self._one_sample(row, index)
            if sample is None:
                skipped += 1
                continue
            samples.append(sample)
            follow = row.get("follow_instruction_list") if isinstance(row, dict) else None
            if isinstance(follow, list) and follow:
                followed = sum(1 for value in follow if value is True)
                followed_total += followed
                instructions_total += len(follow)
                if followed == len(follow):
                    prompts_all_followed += 1
        if not samples:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="no IFEval prompt record parsed",
            )
        metrics: dict[str, float] = {}
        if instructions_total:
            metrics["instruction_level_strict_accuracy"] = followed_total / instructions_total
            metrics["prompt_level_strict_accuracy"] = prompts_all_followed / len(samples)
        return AdapterOutcome(samples=tuple(samples), metrics=metrics, partial=skipped > 0)

    def _one_sample(self, row: object, index: int) -> AdapterSample | None:
        if not isinstance(row, dict):
            return None
        follow = row.get("follow_instruction_list")
        case_id = str(row.get("prompt_id", row.get("key", index)))
        if not isinstance(follow, list) or not follow:
            return AdapterSample(
                case_id=case_id,
                score=None,
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="prompt record has no follow_instruction_list",
            )
        followed = sum(1 for value in follow if value is True)
        return AdapterSample(
            case_id=case_id,
            score=followed / len(follow),
            detail={
                "instructions": len(follow),
                "followed": followed,
                "instruction_ids": excerpt(row.get("instruction_id_list", [])),
            },
        )
