"""freeweight.benchmarks.structured_output.benchmark — ``native.structured_output``.

Benchmark catalog §3.5. Three tests — a flat object, an enumerated field, a nested structure —
scored by :class:`~freeweight.domain.scorers.schema.JsonSchemaScorer` and executed through
:class:`~freeweight.benchmarks.interaction.StructuredOutputSession`, which is what gives the
catalog's *recovery rate after one corrective retry* something to measure.

**Capability-gated, and gated honestly.** The suite requires ``structured_output``; a provider that
has not declared it produces a *skipped* test with ``unsupported_capability`` on the row, and
contributes no score at all. That is benchmark catalog §3.5's "records ``unsupported``, not a
failure" and
[graceful degradation](../../../../docs/architecture/graceful-degradation.md)'s row for the same
condition — a model that cannot be asked for a schema is not a model that is bad at schemas.

**Two rates, never one.** ``first_attempt_conformed`` and ``recovered_after_retry`` are separate
metrics. A suite reporting only the post-retry figure would rate a model that needs a second
attempt every time identically to one that never does.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.benchmarks.interaction import StructuredOutputSession
from freeweight.benchmarks.loading import (
    SuiteBenchmark,
    build_tests,
    load_cases,
    load_manifest,
    verify_prompts,
)
from freeweight.domain.scorers.schema import JsonSchemaScorer
from freeweight.services.prompts import load_pack

if TYPE_CHECKING:
    from freeweight.services.prompts import PromptLibrary

__all__ = ["PROMPT_ID", "REPAIR_PROMPT_ID", "build", "load_suite_manifest"]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_CASES_PATH = Path(__file__).parent / "cases.json"

PROMPT_ID = "benchmarks.structured_output.schema"
"""The record every case renders for its first attempt."""

REPAIR_PROMPT_ID = "benchmarks.structured_output.repair"
"""The record the corrective retry renders. Declared in the manifest, so it is inside this suite's
``prompt_subset_hash``: editing the repair prompt changes what the recovery rate measures, and must
separate this suite's results from those taken before the edit (ADR-0028 §1)."""


def _variables(case_body: Any) -> dict[str, Any]:  # noqa: ANN401 — one parsed cases.json entry
    """Render the prompt variables, putting the case's own schema in front of the model.

    The schema the scorer validates against and the schema the model is shown are the same object,
    serialized once: a suite that re-stated the shape in prose would be measuring the prose.
    """
    import json

    return {
        **dict(case_body.get("variables", {})),
        "schema_json": json.dumps(case_body["expectation"]["schema"], indent=2, sort_keys=True),
    }


def load_suite_manifest() -> Any:  # noqa: ANN401 — BenchmarkManifest, imported lazily by callers
    """Load ``manifest.json`` from beside this module."""
    return load_manifest(_MANIFEST_PATH)


def build(library: PromptLibrary | None = None) -> SuiteBenchmark:
    """Build the suite, verifying that the manifest describes the installed prompts.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one.

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` does not match the installed pack.
        PromptNotFound: The manifest declares a prompt the pack does not have.
    """
    pack = library if library is not None else load_pack()
    manifest = load_suite_manifest()
    verify_prompts(manifest, pack)
    session = StructuredOutputSession(library=pack, repair_prompt_id=REPAIR_PROMPT_ID)
    return SuiteBenchmark(
        manifest=manifest,
        tests=build_tests(
            manifest=manifest,
            cases=load_cases(_CASES_PATH),
            pack=pack,
            prompt_id=PROMPT_ID,
            scorer_for=lambda _body: JsonSchemaScorer(),
            interaction_for=lambda _body: session,
            variables_for=_variables,
        ),
    )
