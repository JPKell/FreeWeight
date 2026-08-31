"""JudgeBench adapter — judge quality on objective correctness-based preference labels.

JudgeBench pairs responses with an objectively-correct preference label and asks the model, as a
judge, to pick. Its records carry ``id`` and ``correct`` (whether the judge's pick matched the
objective label). The metric is preference accuracy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from freeweight.external.adapters.base import AdapterOutcome, AdapterSample
from freeweight.external.adapters.parsing import excerpt, safe_json, safe_jsonl
from freeweight.external.manifest import ExternalManifest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["JudgeBenchAdapter"]


class JudgeBenchAdapter:
    """Parses JudgeBench per-pair correctness into normalized results."""

    manifest = ExternalManifest(
        key="external.judgebench",
        name="JudgeBench",
        version="1.0.0",
        category="judging",
        capabilities=("judging",),
        source_repository="https://github.com/ScalerLab/JudgeBench",
        release_tag="2024-10-01",
        commit="7e2a9b1",
        license="MIT",
        install_command=(),
        pinned_packages=(),
        requires_sandbox=False,
        metrics={"preference_accuracy": True},
    )

    def command(self, *, datasets_dir: Path, model_ref: str) -> Sequence[str]:
        """The argv that runs this benchmark against ``model_ref`` under its environment."""
        return ("python", "-m", "judgebench.run", "--judge", model_ref, "--data", str(datasets_dir))

    def parse(self, raw_output: bytes) -> AdapterOutcome:
        """Parse the tool\'s recorded output as untrusted input; never raises on bad content."""
        rows, skipped, error = _rows_or_error(raw_output)
        if not rows:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text=error or "JudgeBench output carried no records",
            )
        samples: list[AdapterSample] = []
        for index, row in enumerate(rows):
            sample = _boolean_sample(row, index, field_name="correct")
            if sample is None:
                skipped += 1
                continue
            samples.append(sample)
        if not samples:
            return AdapterOutcome(
                error_code="EXTERNAL_BENCHMARK_FAILED",
                error_text="no JudgeBench record parsed",
            )
        scored = [int(s.score) for s in samples if s.score is not None]
        metrics = {"preference_accuracy": sum(scored) / len(scored)} if scored else {}
        return AdapterOutcome(samples=tuple(samples), metrics=metrics, partial=skipped > 0)


def _rows_or_error(raw_output: bytes) -> tuple[list[object], int, str | None]:
    """Shared: a JSON list, a ``{"results": [...]}`` object, or JSON-lines."""
    document, error = safe_json(raw_output)
    if error is None and isinstance(document, list):
        return document, 0, None
    if error is None and isinstance(document, dict) and isinstance(document.get("results"), list):
        return document["results"], 0, None
    rows, skipped = safe_jsonl(raw_output)
    return rows, skipped, error


def _boolean_sample(row: object, index: int, *, field_name: str) -> AdapterSample | None:
    """Shared: a case scored 1.0/0.0 from a boolean field, refusing a non-boolean honestly."""
    if not isinstance(row, dict):
        return None
    case_id = str(row.get("id", index))
    if field_name not in row:
        return AdapterSample(
            case_id=case_id,
            score=None,
            error_code="EXTERNAL_BENCHMARK_FAILED",
            error_text=f"record carried no {field_name!r} field",
        )
    value = row[field_name]
    if not isinstance(value, bool):
        return AdapterSample(
            case_id=case_id,
            score=None,
            error_code="EXTERNAL_BENCHMARK_FAILED",
            error_text=f"{field_name!r} was not a boolean",
            detail={"raw": excerpt(value)},
        )
    return AdapterSample(
        case_id=case_id,
        score=1.0 if value else 0.0,
        detail={k: row[k] for k in ("subset", "category", "source") if k in row},
    )
