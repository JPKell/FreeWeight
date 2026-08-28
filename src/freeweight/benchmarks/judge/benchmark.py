"""freeweight.benchmarks.judge.benchmark — ``native.judge``.

Benchmark catalog §3.11's seven tests, built from one corpus of comparisons. Each test turns a
corpus family into cases whose *plan* — which subjects are presented, in which order, how many
times, and whether the judge is told which answer is its own — is written into the case rather
than into the session. The session then makes exactly the calls the plan names and records the
verdicts; every figure is produced by counting those verdicts in
:mod:`freeweight.domain.scorers.judge`.

**The plan is data because the plan is the experiment.** "The same pair in both orders" and "the
same comparison five times" are not implementation details of a loop; they are what position bias
and repetition stability *are*. Writing them down beside the corpus means a reader can see what
each test asks without reading a driver, and means a test cannot silently stop swapping.

**Nothing in this suite is judged by this suite.** The model under test supplies opinions; the
corpus supplies the gold preference and the ordering; the scorer counts. The word "judge" names
what is being measured, not how.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeweight.benchmarks.corpora import corpus_hash, load
from freeweight.benchmarks.judge.session import JudgeSession, presentation_variables
from freeweight.benchmarks.loading import (
    SuiteBenchmark,
    build_tests,
    load_cases,
    load_manifest,
    verify_prompts,
)
from freeweight.domain.judging import present, presentation_orders
from freeweight.domain.scorers.judge import JudgeScorer, JudgeTestKind
from freeweight.services.prompts import load_pack

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.domain.benchmark import BenchmarkManifest
    from freeweight.services.prompts import PromptLibrary

__all__ = ["CORPUS_NAME", "PROMPT_ID", "build", "load_suite_manifest"]

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_CASES_PATH = Path(__file__).parent / "cases.json"

PROMPT_ID = "benchmarks.judge.pairwise"
"""The record a case's stored prompt hash is rendered from, and the one every presentation uses."""

CORPUS_NAME = "judge_comparisons"
"""The corpus this suite is built from; its hash is the suite's ``dataset_hashes`` entry."""

_DEFAULT_REPETITIONS = 3
"""How many times a repetition-stability case asks the same question.

Three, because two can only ever report agreement of 1.0 or 0.5 and a judge that flips one time in
three is exactly the interesting case."""


def _both_orders(subjects: Sequence[str], *, group: str = "") -> list[dict[str, Any]]:
    """Return the two presentations of a pair: as given, and swapped."""
    return [
        {
            "group": group,
            "subjects": [subjects[position] for position in order],
            "attributed": False,
            "fixed": True,
        }
        for order in presentation_orders(subjects)
    ]


def _pair_case(entry: Mapping[str, Any], kind: JudgeTestKind, repetitions: int) -> dict[str, Any]:
    """Build one case from a ``pairs`` corpus entry."""
    subjects = ("better", "worse")
    if kind is JudgeTestKind.REPETITION:
        plan = [
            {"group": "", "subjects": list(subjects), "attributed": False, "fixed": True}
            for _ in range(repetitions)
        ]
    else:
        plan = _both_orders(subjects)
    return {
        "case_id": str(entry["id"]),
        "texts": {"better": str(entry["better"]), "worse": str(entry["worse"])},
        "question": str(entry["question"]),
        "presentations": plan,
        "expectation": {"judge": {"kind": kind.value, "gold": str(entry["gold"])}},
    }


def _bias_case(
    entry: Mapping[str, Any], kind: JudgeTestKind, names: tuple[str, str]
) -> dict[str, Any]:
    """Build one case from the ``verbosity`` or ``style`` corpus families."""
    return {
        "case_id": str(entry["id"]),
        "texts": {name: str(entry[name]) for name in names},
        "question": str(entry["question"]),
        "presentations": _both_orders(names),
        "expectation": {"judge": {"kind": kind.value, "disfavoured": str(entry["disfavoured"])}},
    }


def _triple_case(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Build one transitivity case: three pairwise sub-comparisons over one ordered triple."""
    best, middle, worst = (str(name) for name in entry["ordering"])
    return {
        "case_id": str(entry["id"]),
        "texts": {name: str(entry[name]) for name in (best, middle, worst)},
        "question": str(entry["question"]),
        "presentations": [
            {"group": "ab", "subjects": [best, middle], "attributed": False},
            {"group": "bc", "subjects": [middle, worst], "attributed": False},
            {"group": "ac", "subjects": [best, worst], "attributed": False},
        ],
        "expectation": {
            "judge": {
                "kind": JudgeTestKind.TRANSITIVITY.value,
                "ordering": [best, middle, worst],
            }
        },
    }


def _self_preference_case(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Build one self-preference case: the same pair, blinded and then attributed.

    Both conditions are presented in both orders, so the delta between them is a difference in
    *attribution* rather than a difference in position that happened to land on one side.
    """
    subjects = ("own", "reference")
    plan = _both_orders(subjects, group="anonymized")
    for presentation in _both_orders(subjects, group="attributed"):
        plan.append({**presentation, "attributed": True})
    return {
        "case_id": str(entry["id"]),
        "texts": {"reference": str(entry["reference_answer"])},
        "question": str(entry["question"]),
        "presentations": plan,
        "expectation": {"judge": {"kind": JudgeTestKind.SELF_PREFERENCE.value, "own": "own"}},
    }


def _cases(cases: Mapping[str, Any]) -> dict[str, Any]:
    """Turn each declared test's corpus family into cases carrying their presentation plan.

    Args:
        cases: The parsed ``cases.json``.

    Returns:
        The same structure with a ``cases`` list on every test.

    Raises:
        ValueError: A test names a kind this builder has no plan for — refused at build time,
            which is startup, rather than producing a case whose session makes no calls.
    """
    corpus = load(str(cases["corpus"]))
    built: list[dict[str, Any]] = []
    for test in cases["tests"]:
        kind = JudgeTestKind(str(test["kind"]))
        family = list(corpus[str(test["family"])])
        repetitions = int(test.get("repetitions", _DEFAULT_REPETITIONS))
        declared: list[dict[str, Any]] = []
        for entry in family:
            match kind:
                case JudgeTestKind.PAIRWISE | JudgeTestKind.POSITION | JudgeTestKind.REPETITION:
                    body = _pair_case(entry, kind, repetitions)
                case JudgeTestKind.VERBOSITY:
                    body = _bias_case(entry, kind, ("concise", "verbose"))
                case JudgeTestKind.STYLE:
                    body = _bias_case(entry, kind, ("plain", "flourish"))
                case JudgeTestKind.TRANSITIVITY:
                    body = _triple_case(entry)
                case _:  # JudgeTestKind.SELF_PREFERENCE
                    body = _self_preference_case(entry)
            declared.append(
                {
                    "case_id": body["case_id"],
                    "scenario": kind.value,
                    "expectation": body["expectation"],
                    "metadata": {
                        "question": body["question"],
                        "texts": body["texts"],
                        "presentations": body["presentations"],
                    },
                }
            )
        built.append({**dict(test), "cases": declared})
    return {"tests": built}


def _variables(case_body: Mapping[str, Any]) -> dict[str, Any]:
    """Render the variables for the case's *first* presentation.

    ``build_tests`` renders one prompt per case and stores its hash on every sample. A judge case
    sends several presentations, so one of them has to stand for the case in the provenance
    record; the first is chosen because it is the one the corpus declares, before any swap. The
    session renders each presentation from the same record with the same helper, so the stored
    hash is a prompt that was genuinely sent rather than a reconstruction.
    """
    metadata = dict(case_body.get("metadata", {}))
    plan = [dict(entry) for entry in metadata.get("presentations", ())]
    texts = {str(name): str(text) for name, text in dict(metadata.get("texts", {})).items()}
    first = plan[0] if plan else {"subjects": []}
    subjects = [str(name) for name in first.get("subjects", ())]
    # ``own`` has no text until the model supplies one at run time; the provenance rendering shows
    # the placeholder rather than inventing an answer, and the case's hash still changes whenever
    # the reference answer or the question changes.
    rendered = present(
        subjects, [texts.get(name, f"<{name}>") for name in subjects], range(len(subjects))
    )
    return presentation_variables(
        question=str(metadata.get("question", "")), answers=rendered.rendered()
    )


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
    session = JudgeSession(library=pack)
    return SuiteBenchmark(
        manifest=manifest,
        tests=build_tests(
            manifest=manifest,
            cases=_cases(load_cases(_CASES_PATH)),
            pack=pack,
            prompt_id=PROMPT_ID,
            scorer_for=lambda _body: JudgeScorer(),
            interaction_for=lambda _body: session,
            variables_for=_variables,
        ),
    )
