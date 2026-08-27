"""freeweight.benchmarks.instruction_following.benchmark — ``native.instruction_following``.

Benchmark catalog §3.4. Six tests, one per constraint class plus one that applies four constraints
at once, and every one of them decided by counting or matching rather than by judgement.

**The model reads the constraints the scorer checks.** ``cases.json`` declares each constraint
once, with the sentence a person would write; :func:`_variables` renders exactly those sentences
into the prompt's ``constraints`` variable, and
:class:`~freeweight.domain.scorers.rule.RuleScorer` checks exactly those declarations. There is no
second copy of the rules in English, so a case cannot ask for one thing and be marked against
another — which is the failure this suite would be least able to detect in itself.

**A refusal is not a capability failure.** The phase's named failure mode is "scoring a refusal as
a failure of capability", and this suite's answer to it is that nothing here reads the model's
*meaning*: a response that declines to write the paragraph fails the constraints it did not meet,
those constraints are named in the sample's detail, and a reader can see at a glance that the model
answered something else rather than that it cannot follow instructions.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.benchmarks.loading import (
    SuiteBenchmark,
    build_tests,
    load_cases,
    load_manifest,
    verify_prompts,
)
from freeweight.domain.scorers.rule import Constraint, RuleScorer
from freeweight.services.prompts import load_pack

if TYPE_CHECKING:
    from collections.abc import Mapping

    from freeweight.services.prompts import PromptLibrary

__all__ = ["PROMPT_ID", "build", "load_suite_manifest"]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_CASES_PATH = Path(__file__).parent / "cases.json"

PROMPT_ID = "benchmarks.instruction_following.constrained"
"""The one prompt record every case in this suite renders."""


def _variables(case_body: Mapping[str, Any]) -> dict[str, Any]:
    """Render the prompt variables for one case, constraints included.

    The ``constraints`` variable is built from the case's own constraint declarations rather than
    written out beside them: one declaration, shown to the model and checked by the scorer. A case
    that declares a constraint therefore cannot forget to tell the model about it, and a case that
    tells the model about a constraint it does not declare cannot exist at all.

    Args:
        case_body: One entry from ``cases.json``.

    Returns:
        The variables the prompt record is rendered with.

    Raises:
        ConstraintInvalid: A declared constraint is malformed. Raised at suite-build time, which
            is startup, rather than reaching a run as a case nobody can score.
    """
    constraints = [
        Constraint.from_json(dict(item))
        for item in case_body.get("expectation", {}).get("constraints", ())
    ]
    return {
        **dict(case_body.get("variables", {})),
        "constraints": "\n".join(f"- {constraint.as_instruction()}" for constraint in constraints),
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
    return SuiteBenchmark(
        manifest=manifest,
        tests=build_tests(
            manifest=manifest,
            cases=load_cases(_CASES_PATH),
            pack=pack,
            prompt_id=PROMPT_ID,
            scorer_for=lambda _body: RuleScorer(),
            variables_for=_variables,
        ),
    )
