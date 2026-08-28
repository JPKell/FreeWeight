"""freeweight.benchmarks.critique.benchmark — ``native.critique``.

Benchmark catalog §3.10. Two tests over one corpus, split by the fact the corpus already knows:
whether the candidate answer the model is asked to review was right to begin with.

**The split is what makes the two dangerous rates measurable.** A critic's
``hallucinated_criticism_rate`` and its ``regression_rate`` are only defined on answers that were
*already correct*, and its ``error_detection_recall`` only on answers that were not. Running both
halves in one test would leave every one of those rates with samples that cannot contribute, and
declaring them per test is what keeps each rate's denominator visible in the test it belongs to.

**The corpus, not the prompt, decides what is correct.** The model is shown the question and the
candidate answer and nothing else — no hint of which half of the corpus it is in — and its verdict
is compared against ground truth the prompt never carried.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.benchmarks.corpora import corpus_hash, load
from freeweight.benchmarks.loading import (
    SuiteBenchmark,
    build_tests,
    load_cases,
    load_manifest,
    verify_prompts,
)
from freeweight.domain.scorers.critique import CritiqueScorer
from freeweight.services.prompts import load_pack

if TYPE_CHECKING:
    from collections.abc import Mapping

    from freeweight.domain.benchmark import BenchmarkManifest
    from freeweight.services.prompts import PromptLibrary

__all__ = ["CORPUS_NAME", "PROMPT_ID", "build", "load_suite_manifest"]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_CASES_PATH = Path(__file__).parent / "cases.json"

PROMPT_ID = "benchmarks.critique.review"
"""The one prompt record every case in this suite renders."""

CORPUS_NAME = "critique_answers"
"""The corpus this suite is built from; its hash is the suite's ``dataset_hashes`` entry."""


def _cases(cases: Mapping[str, Any]) -> dict[str, Any]:
    """Fill each declared test with the corpus entries whose correctness it selects.

    Args:
        cases: The parsed ``cases.json``.

    Returns:
        The same structure with a ``cases`` list on every test.
    """
    corpus = load(str(cases["corpus"]))
    normalize = list(corpus.get("normalize", ()))
    built: list[dict[str, Any]] = []
    for test in cases["tests"]:
        wanted = bool(test["answer_is_correct"])
        entries = [
            entry for entry in corpus["entries"] if bool(entry["answer_is_correct"]) is wanted
        ]
        built.append(
            {
                **dict(test),
                "cases": [
                    {
                        "case_id": str(entry["id"]),
                        "scenario": "already_correct" if wanted else "injected_error",
                        "variables": {
                            "question": str(entry["question"]),
                            "candidate_answer": str(entry["candidate_answer"]),
                        },
                        "expectation": {
                            "critique": {
                                "answer_is_correct": bool(entry["answer_is_correct"]),
                                "gold_answers": list(entry["gold_answers"]),
                                "candidate_answer": str(entry["candidate_answer"]),
                                "normalize": normalize,
                            }
                        },
                        "metadata": {"injected_error": entry.get("injected_error") or ""},
                    }
                    for entry in entries
                ],
            }
        )
    return {"tests": built}


def load_suite_manifest() -> BenchmarkManifest:
    """Load ``manifest.json`` from beside this module."""
    return load_manifest(_MANIFEST_PATH)


def build(library: PromptLibrary | None = None) -> SuiteBenchmark:
    """Build the suite, verifying the manifest against the installed pack and corpus.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one.

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` or ``dataset_hashes`` do not describe
            what is installed.
        PromptNotFound: The manifest declares a prompt the pack does not have.
    """
    pack = library if library is not None else load_pack()
    manifest = load_suite_manifest()
    verify_prompts(manifest, pack)
    declared = manifest.dataset_hashes.get(CORPUS_NAME)
    actual = corpus_hash(CORPUS_NAME)
    if declared != actual:
        raise ValueError(
            f"Benchmark {manifest.key!r} declares dataset_hashes[{CORPUS_NAME!r}] = {declared!r}; "
            f"the installed corpus hashes to {actual!r}. Rebuild the manifest and bump the suite "
            "version — the corpus this suite measures has changed, which separates its results."
        )
    return SuiteBenchmark(
        manifest=manifest,
        tests=build_tests(
            manifest=manifest,
            cases=_cases(load_cases(_CASES_PATH)),
            pack=pack,
            prompt_id=PROMPT_ID,
            scorer_for=lambda _body: CritiqueScorer(),
        ),
    )
