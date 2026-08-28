"""freeweight.benchmarks.long_context.benchmark — ``native.long_context``.

Benchmark catalog §3.12. Four sweeps over one corpus of buried facts: by context length, by where
in the document the fact sits, by how many near-miss distractors surround it, and — the one that
needs two facts rather than one — by whether the model can join information that is far apart.

**Advertised context and effective context are different numbers.** This suite exists to produce
the second, and it produces it by measuring the first sweep against its own shortest-context
baseline. A model that answers nothing anywhere has *no* effective context rather than a small one,
and :func:`~freeweight.domain.metrics.effective_context_tokens` says so with a reason rather than
reporting the shortest length tested.

**A case the model cannot be served is skipped, not failed.** Every case declares
``required_context_tokens``, so a 32 000-token probe against a model served 8 192 tokens is a
recorded skip with a reason — the run engine's existing mechanism — rather than a truncation the
suite would have scored as a retrieval error.

**The shipped sweep stops at 32 000 tokens.** The catalog's range reaches 128K "only those
supported"; the documents are expanded at suite-build time, and a 128K case would add half a
megabyte of filler to every process that so much as lists the available benchmarks. 32 000 is
enough for the effective-context figure to differ from the advertised one on the models this tool
is for, and the sweep is data — a longer one is a case-file edit and a suite version bump.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baseaicore import canonical_json, sha256_of

from freeweight.benchmarks.corpora import corpus_hash, load
from freeweight.benchmarks.loading import (
    SuiteBenchmark,
    build_tests,
    load_cases,
    load_manifest,
    verify_prompts,
)
from freeweight.benchmarks.long_context.haystack import assemble
from freeweight.benchmarks.long_context.scoring import LongContextScorer
from freeweight.services.prompts import load_pack

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.benchmarks.long_context.haystack import Haystack
    from freeweight.domain.benchmark import BenchmarkManifest
    from freeweight.services.prompts import PromptLibrary

__all__ = [
    "CORPUS_NAME",
    "LADDER_DATASET_KEY",
    "PROMPT_ID",
    "build",
    "ladder_hash",
    "load_suite_manifest",
    "sweep_ladder",
]

LADDER_DATASET_KEY = "context_ladder"
"""``dataset_hashes`` key for the effective sweep ladder, which the ceiling can change."""

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_CASES_PATH = Path(__file__).parent / "cases.json"

PROMPT_ID = "benchmarks.long_context.retrieve"
"""The one prompt record every case in this suite renders."""

CORPUS_NAME = "long_context_needles"
"""The corpus this suite is built from; its hash is the suite's ``dataset_hashes`` entry."""


def _case_body(
    *,
    case_id: str,
    scenario: str,
    haystack: Haystack,
    question: str,
    answer: str,
    normalize: Sequence[str],
) -> dict[str, Any]:
    """Assemble one case's declaration from a built document."""
    return {
        "case_id": case_id,
        "scenario": scenario,
        "required_context_tokens": haystack.required_context_tokens,
        "variables": {"context": haystack.text, "question": question},
        "expectation": {
            "exact": {"any_of": [answer], "normalize": list(normalize), "contains": True},
            "long_context": {
                "context_tokens": haystack.context_tokens,
                "position_percent": haystack.position_percent,
                "distractor_count": haystack.distractor_count,
            },
        },
    }


def sweep_ladder(declared: Sequence[int], *, ceiling: int) -> tuple[int, ...]:
    """Fit a declared context ladder to the machine's ceiling.

    The shipped ladder doubles. Below the ceiling it is truncated; above it, it keeps doubling
    from its own top rung until the next one would exceed the ceiling, and the ceiling itself is
    the final rung whenever a doubling does not land on it exactly. The first rung always
    survives, even on a ceiling below it: a sweep with no rungs measures nothing, and a suite that
    silently became empty would report an absent effective context as a model property.

    Args:
        declared: The ladder ``cases.json`` ships, ascending.
        ceiling: ``benchmarks.long_context_max_tokens``.

    Returns:
        The ladder this machine will actually sweep, ascending and without duplicates.
    """
    rungs = [value for value in declared if value <= ceiling]
    if not rungs:
        return (declared[0],) if declared else ()
    top = rungs[-1]
    while top * 2 <= ceiling:
        top *= 2
        rungs.append(top)
    if rungs[-1] < ceiling:
        rungs.append(ceiling)
    return tuple(rungs)


def ladder_hash(cases: Mapping[str, Any]) -> str:
    """Hash the effective ladder of every sweep in ``cases``.

    Recorded in the built manifest's ``dataset_hashes`` so that a run at one ceiling and a run at
    another are **separated rather than averaged**. A sweep that stopped at 32 000 tokens reports a
    smaller ``effective_context_tokens`` than one that went to 128 000 for reasons that have
    nothing to do with the model, and a comparison across the two would attribute the difference
    to the model.

    Args:
        cases: The built case document, each test carrying its resolved ``context_lengths``.

    Returns:
        ``sha256:…`` over the ladders, keyed by test.
    """
    ladders = {
        str(test["key"]): [int(value) for value in test["context_lengths"]]
        for test in cases["tests"]
    }
    return f"sha256:{sha256_of(canonical_json(ladders))}"


def _cases(  # noqa: PLR0912 — one branch per sweep
    cases: Mapping[str, Any], *, ceiling: int = 32_000
) -> dict[str, Any]:
    """Expand each declared sweep into cases, building one document per point.

    Args:
        cases: The parsed ``cases.json``.

    Returns:
        The same structure with a ``cases`` list on every test.

    Raises:
        ValueError: A test declares a sweep shape this builder does not know. Refused at build
            time so a typo cannot ship a test with no cases in it.
    """
    corpus = load(str(cases["corpus"]))
    filler = tuple(str(item) for item in corpus["filler"])
    needles = list(corpus["needles"])
    chains = list(corpus["reasoning"])
    normalize = list(cases.get("normalize", ()))
    built: list[dict[str, Any]] = []

    for index, test in enumerate(cases["tests"]):
        shape = str(test["shape"])
        # Only the depth sweep is a *ladder*; the other three hold context constant on purpose and
        # vary position, distractors or fact separation instead. Stretching those to the ceiling
        # would change what they measure rather than how far they reach.
        declared_lengths = [int(value) for value in test["context_lengths"]]
        lengths = (
            list(sweep_ladder(declared_lengths, ceiling=ceiling))
            if shape == "depth"
            else declared_lengths
        )
        # One needle for the whole sweep. A sweep that rotated needles would vary two things at
        # once, and a dip at 16 000 tokens would be indistinguishable from a fact the model
        # happens to find harder to quote. Each *test* uses a different needle, which is a
        # comparison this suite never makes.
        needle = needles[index % len(needles)]
        # Near misses for *this* needle. A distractor about a different fact distracts from
        # nothing, and a sweep padded with them would report a distractor sensitivity the model
        # never had the chance to show.
        distractors = tuple(str(item) for item in needle.get("distractors", ()))
        declared: list[dict[str, Any]] = []
        match shape:
            case "depth":
                for length in lengths:
                    haystack = assemble(
                        filler=filler,
                        facts=(str(needle["fact"]),),
                        distractors=(),
                        context_tokens=length,
                        position_percent=int(test["position_percent"]),
                    )
                    declared.append(
                        _case_body(
                            case_id=f"{needle['id']}_{length}",
                            scenario=f"depth_{length}",
                            haystack=haystack,
                            question=str(needle["question"]),
                            answer=str(needle["answer"]),
                            normalize=normalize,
                        )
                    )
            case "position":
                for percent in (int(value) for value in test["positions"]):
                    haystack = assemble(
                        filler=filler,
                        facts=(str(needle["fact"]),),
                        distractors=(),
                        context_tokens=lengths[0],
                        position_percent=percent,
                    )
                    declared.append(
                        _case_body(
                            case_id=f"{needle['id']}_p{percent}",
                            scenario=f"position_{percent}",
                            haystack=haystack,
                            question=str(needle["question"]),
                            answer=str(needle["answer"]),
                            normalize=normalize,
                        )
                    )
            case "distractors":
                for count in (int(value) for value in test["distractor_counts"]):
                    haystack = assemble(
                        filler=filler,
                        facts=(str(needle["fact"]),),
                        distractors=distractors[:count],
                        context_tokens=lengths[0],
                        position_percent=int(test["position_percent"]),
                    )
                    declared.append(
                        _case_body(
                            case_id=f"{needle['id']}_d{count}",
                            scenario=f"distractors_{count}",
                            haystack=haystack,
                            question=str(needle["question"]),
                            answer=str(needle["answer"]),
                            normalize=normalize,
                        )
                    )
            case "reasoning":
                near, far = (int(value) for value in test["positions"])
                chain = chains[index % len(chains)]
                for length in lengths:
                    haystack = assemble(
                        filler=filler,
                        facts=tuple(str(fact) for fact in chain["facts"]),
                        distractors=(),
                        context_tokens=length,
                        position_percent=near,
                        second_position_percent=far,
                    )
                    declared.append(
                        _case_body(
                            case_id=f"{chain['id']}_{length}",
                            scenario=f"reasoning_{length}",
                            haystack=haystack,
                            question=str(chain["question"]),
                            answer=str(chain["answer"]),
                            normalize=normalize,
                        )
                    )
            case _:
                raise ValueError(
                    f"Long-context test {test['key']!r} declares unknown sweep shape {shape!r}."
                )
        built.append({**dict(test), "context_lengths": lengths, "cases": declared})
    return {"tests": built}


def load_suite_manifest() -> BenchmarkManifest:
    """Load ``manifest.json`` from beside this module."""
    return load_manifest(_MANIFEST_PATH)


def build(
    library: PromptLibrary | None = None, *, max_context_tokens: int = 32_000
) -> SuiteBenchmark:
    """Build the suite, verifying the manifest against the installed pack and corpus.

    Args:
        library: The loaded pack, or ``None`` to load the shipped one.
        max_context_tokens: ``benchmarks.long_context_max_tokens`` — the ceiling the depth sweep's
            ladder is fitted to. The effective ladder is hashed into the built manifest's
            ``dataset_hashes``, so a run at one ceiling never averages with a run at another.

    Returns:
        The benchmark.

    Raises:
        ValueError: The manifest's ``prompt_subset_hash`` or ``dataset_hashes`` do not describe
            what is installed, or a test declares an unknown sweep shape.
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
    cases = _cases(load_cases(_CASES_PATH), ceiling=max_context_tokens)
    # The ladder is configuration, so it cannot be declared in the shipped manifest — but it does
    # change what the suite measures, so it has to reach the reproducibility fingerprint. Adding it
    # to the *built* manifest's dataset hashes puts it exactly where every other content-identity
    # fact already lives, and separation follows without a second mechanism.
    resolved = replace(
        manifest,
        dataset_hashes={**manifest.dataset_hashes, LADDER_DATASET_KEY: ladder_hash(cases)},
    )
    return SuiteBenchmark(
        manifest=resolved,
        tests=build_tests(
            manifest=resolved,
            cases=cases,
            pack=pack,
            prompt_id=PROMPT_ID,
            scorer_for=lambda _body: LongContextScorer(),
        ),
    )
