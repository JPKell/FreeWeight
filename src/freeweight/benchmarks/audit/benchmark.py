"""freeweight.benchmarks.audit.benchmark — ``native.audit``.

Benchmark catalog §3.9. Two tests over one corpus: the mutated half, where the ground truth is a
line number and a defect class, and the clean half, where the ground truth is *silence*.

**The clean half is not a control group; it is half the measurement.** The catalog's rule — "a
model that reports many possible problems must not score well" — is unstateable on a corpus of
nothing but defects, where flagging every line is perfect recall. Splitting the corpus into two
tests is what makes ``clean_code_false_positive_rate`` a metric with a denominator, and it is why
each test declares only the metric keys its own cases can measure: a test that declared ``recall``
over clean code would have every sample fall through to the headline score and report it under
recall's name.

**The model is shown line numbers, and the corpus records line numbers.** The prompt renders the
source with a 1-based gutter, so "which line" is a question with one unambiguous answer on both
sides. Without it, a finding would have to be matched by text and the suite would be measuring
quotation rather than localization.
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
from freeweight.domain.scorers.audit import AuditScorer
from freeweight.services.prompts import load_pack

if TYPE_CHECKING:
    from collections.abc import Mapping

    from freeweight.domain.benchmark import BenchmarkManifest
    from freeweight.services.prompts import PromptLibrary

__all__ = ["CORPUS_NAME", "PROMPT_ID", "build", "load_suite_manifest", "number_lines"]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_CASES_PATH = Path(__file__).parent / "cases.json"

PROMPT_ID = "benchmarks.audit.detect_defects"
"""The one prompt record every case in this suite renders."""

CORPUS_NAME = "audit_code"
"""The corpus this suite is built from; its hash is the suite's ``dataset_hashes`` entry."""


def number_lines(code: str) -> str:
    """Render source with a 1-based line-number gutter.

    Args:
        code: The file's text.

    Returns:
        One line per source line, ``"   1 | def f():"``. The trailing newline of a well-formed
        file is dropped rather than rendered as an empty numbered line, which would offset every
        subsequent number by nothing but would still show the model a line the corpus does not
        count.
    """
    lines = code.rstrip("\n").split("\n")
    return "\n".join(f"{number:>4} | {line}" for number, line in enumerate(lines, start=1))


def _cases(cases: Mapping[str, Any]) -> dict[str, Any]:
    """Fill each declared test with the corpus entries its ``source`` names.

    The corpus is the single source of the code and its ground truth; ``cases.json`` declares only
    the test structure — which half feeds which test, and which metrics that test can measure.
    Duplicating the snippets into a case file would give two places for a mutation's line number
    to live, and they would disagree the first time one was edited.

    Args:
        cases: The parsed ``cases.json``.

    Returns:
        The same structure with a ``cases`` list on every test.

    Raises:
        KeyError: A test names a corpus half that does not exist.
    """
    corpus = load(str(cases["corpus"]))
    built: list[dict[str, Any]] = []
    for test in cases["tests"]:
        half = corpus[str(test["source"])]
        built.append(
            {
                **dict(test),
                "cases": [
                    {
                        "case_id": str(entry["id"]),
                        "scenario": str(test["source"]),
                        "variables": {
                            "path": str(entry["path"]),
                            "language": str(entry["language"]),
                            "numbered_code": number_lines(str(entry["code"])),
                        },
                        "expectation": {
                            "audit": {
                                "defects": list(entry.get("defects", ())),
                                "clean": not entry.get("defects"),
                            }
                        },
                    }
                    for entry in half
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
        ValueError: The manifest's ``prompt_subset_hash`` does not match the installed pack, or
            its ``dataset_hashes`` do not describe the installed corpus. Both are refused at
            startup: a suite whose provenance is wrong must not be runnable at all.
        PromptNotFound: The manifest declares a prompt the pack does not have.
    """
    pack = library if library is not None else load_pack()
    manifest = load_suite_manifest()
    verify_prompts(manifest, pack)
    _verify_corpus(manifest)
    return SuiteBenchmark(
        manifest=manifest,
        tests=build_tests(
            manifest=manifest,
            cases=_cases(load_cases(_CASES_PATH)),
            pack=pack,
            prompt_id=PROMPT_ID,
            scorer_for=lambda _body: AuditScorer(),
        ),
    )


def _verify_corpus(manifest: BenchmarkManifest) -> None:
    """Refuse a suite whose manifest does not describe the corpus installed beside it.

    The same rule as the prompt subset hash and for the same reason: a dataset hash is a
    reproducibility-fingerprint input, and silently correcting it here would let a stale manifest
    produce runs whose provenance describes a corpus that was never measured (benchmark catalog
    §5).

    Raises:
        ValueError: The declared hash and the installed corpus disagree.
    """
    declared = manifest.dataset_hashes.get(CORPUS_NAME)
    actual = corpus_hash(CORPUS_NAME)
    if declared != actual:
        raise ValueError(
            f"Benchmark {manifest.key!r} declares dataset_hashes[{CORPUS_NAME!r}] = {declared!r}; "
            f"the installed corpus hashes to {actual!r}. Rebuild the manifest and bump the suite "
            "version — the corpus this suite measures has changed, which separates its results."
        )
