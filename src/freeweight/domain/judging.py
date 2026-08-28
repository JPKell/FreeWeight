"""freeweight.domain.judging — the judge infrastructure every judged number stands on.

Phase 8 builds the instrument and measures it; it does not yet point one at a user's rubric. The
distinction is the whole point of this module. ``native.judge`` asks *how good is this model at
judging*; the goal suites of Phases 8A–8B ask a jury to score a candidate. Both need the same six
things, and they are here so the second cannot quietly do them differently from the first:

* **Selection** — which models are eligible to judge, and why a model is not
  (:func:`eligible_jurors`). A model never judges its own output
  ([ADR-0031 §4](../../../../docs/adr/0031-user-defined-goal-benchmarks.md)); a remote juror needs
  two separate opt-ins.
* **Order randomization** — :func:`randomized_order`, seeded from the run's own recorded seed, so
  the order a jury saw is reproducible from the run record rather than from wall-clock luck.
* **Blinding** — :func:`blind_labels`, which replaces candidate identity with ``A``/``B``/``C``
  before any text reaches a judge.
* **Repeated trials** — :class:`JudgeTrial` and :class:`JudgeRecord` keep *every* repetition. The
  dispersion across trials is the measurement's error bar, and averaging it at write time would
  destroy the thing being characterized.
* **Agreement measurement** — :func:`agreement_rate` and :func:`majority_choice`, the two figures
  that turn a set of verdicts into one verdict plus a statement of how contested it was.
* **Linkage** — :func:`judge_benchmark_reference`, so any judged score can name the juror's own
  ``native.judge`` results. "How trustworthy is this instrument" is one interaction away
  ([Benchmark Catalog §1](../../../../docs/apps/freeweight/benchmark-catalog.md)).

**Nothing here decides control flow from model text.** :func:`parse_choice` reads a declared
verdict line or a JSON object and returns :attr:`JudgeChoice.UNPARSEABLE` for anything else; it
never guesses a preference from prose. A judge that did not answer in the requested form has not
answered, and recording that is a measurement — testing standards §3, applied to the one part of
this application whose input is a model's opinion.

Pure domain: stdlib and :mod:`baseaicore` only.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from baseaicore import canonical_json

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "BIAS_METRIC_KEYS",
    "BLIND_ALPHABET",
    "JUDGE_SUITE_KEY",
    "REASON_NOT_INSTALLED",
    "REASON_REMOTE_NOT_PERMITTED",
    "REASON_SELF_JUDGING",
    "BiasObservation",
    "BlindedPresentation",
    "JudgeChoice",
    "JudgeRecord",
    "JudgeTrial",
    "JurorEligibility",
    "agreement_rate",
    "blind_labels",
    "eligible_jurors",
    "judge_benchmark_reference",
    "majority_choice",
    "observations_to_detail",
    "parse_choice",
    "present",
    "presentation_orders",
    "randomized_order",
]

JUDGE_SUITE_KEY = "native.judge"
"""The suite whose results characterize a juror.

Named here rather than in the benchmark package because :func:`judge_benchmark_reference` is what
every judged score carries, and a judged score is a domain object: the link must exist even in a
build where the suite has not been run."""

BLIND_ALPHABET = "ABCDEFGH"
"""Labels a blinded presentation uses, in order.

Letters rather than numbers because a judge asked to choose between "1" and "2" reliably drifts
towards the lower number, which is a position bias this module exists to *measure* rather than to
introduce."""

_VERDICT_LINE = re.compile(
    r"^\s*(?:verdict|choice|answer)\s*[:=]\s*([A-Za-z]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
"""The one form a verdict may take in free text.

Anchored to a whole line, deliberately. A judge that writes "I think A is better, though B has a
nicer tone" has expressed a preference a person could read, and reading it would make this module
a second judge sitting on top of the first."""

_TIE_WORDS = frozenset({"tie", "equal", "draw", "neither", "same"})


class JudgeChoice(StrEnum):
    """One judge's verdict on one presentation.

    :attr:`UNPARSEABLE` is a first-class outcome, not an error: a judge that answered in prose
    has told us something real about its usefulness as an instrument, and a suite that raised
    here would report that model as untested rather than as unusable.
    """

    FIRST = "first"
    """The candidate presented first was preferred."""

    SECOND = "second"
    """The candidate presented second was preferred."""

    TIE = "tie"
    """Neither was preferred. A declared tie, never an inference from silence."""

    UNPARSEABLE = "unparseable"
    """The judge did not answer in the requested form."""


@dataclass(frozen=True, slots=True)
class JudgeTrial:
    """One presentation to one judge, and what came back.

    Attributes:
        ordinal: Which presentation of this case this was, from 0. Recorded because the whole
            point of a repeated or swapped presentation is that the ordinal matters.
        order: The blinded labels in the order they were shown, e.g. ``("A", "B")`` for the
            candidate-first presentation and ``("B", "A")`` for the swap. Stored, because a
            verdict without the order it was given in cannot be compared with its swap.
        subjects: The real identities behind :attr:`order`, in the same positions. This is what
            blinding hides from the judge and what scoring needs back.
        choice: The parsed verdict.
        group: A free-form tag naming which sub-question this trial answered — ``"ab"``,
            ``"bc"``, ``"ac"`` for a transitivity triple; ``"anonymized"`` and ``"attributed"``
            for a self-preference case. Empty when a case asks one question. It exists because
            several catalog §3.11 tests ask a judge more than one *different* question per case,
            and grouping them by position in the list would make the record's meaning depend on
            an ordering nobody wrote down.
        raw_excerpt: A bounded excerpt of the judge's answer, for a person reading the sample.
            Capped by the caller; this module stores whatever it is handed.
    """

    ordinal: int
    order: tuple[str, ...]
    subjects: tuple[str, ...]
    choice: JudgeChoice
    group: str = ""
    raw_excerpt: str = ""

    @property
    def chosen_subject(self) -> str | None:
        """The identity the judge preferred, or ``None`` for a tie or an unparseable answer.

        This is the field that makes a swapped pair comparable: two trials agree when they chose
        the same *subject*, never when they chose the same *position*.
        """
        match self.choice:
            case JudgeChoice.FIRST:
                return self.subjects[0] if self.subjects else None
            case JudgeChoice.SECOND:
                return self.subjects[1] if len(self.subjects) > 1 else None
            case _:
                return None

    def as_json(self) -> dict[str, Any]:
        """Return this trial as the plain object a transcript serializes."""
        return {
            "ordinal": self.ordinal,
            "order": list(self.order),
            "subjects": list(self.subjects),
            "choice": self.choice.value,
            "group": self.group,
            "raw_excerpt": self.raw_excerpt,
        }

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> JudgeTrial:
        """Rebuild a trial from :meth:`as_json`'s output.

        Args:
            body: One serialized trial.

        Returns:
            The trial. An unrecognised ``choice`` becomes :attr:`JudgeChoice.UNPARSEABLE` rather
            than raising: a record written by a future build must still be readable as "we could
            not use this verdict", which is true and is better than a crash in a scorer.
        """
        try:
            choice = JudgeChoice(str(body.get("choice", "")))
        except ValueError:
            choice = JudgeChoice.UNPARSEABLE
        return cls(
            ordinal=int(body.get("ordinal", 0)),
            order=tuple(str(item) for item in body.get("order", ())),
            subjects=tuple(str(item) for item in body.get("subjects", ())),
            choice=choice,
            group=str(body.get("group", "")),
            raw_excerpt=str(body.get("raw_excerpt", "")),
        )


@dataclass(frozen=True, slots=True)
class JudgeRecord:
    """Every presentation one case made, in the order they were made.

    The unit a judged scorer reads. It is serialized as the sample's response text so that a
    judged sample drills to *what the judge was asked and what it said* rather than to whichever
    turn happened to be last — a trajectory of opinions is the measurement, and one turn of it is
    not.

    Attributes:
        trials: Every presentation, including repetitions.
        stopped: Why the interaction ended: ``"completed"`` or a provider error code.
    """

    trials: tuple[JudgeTrial, ...] = ()
    stopped: str = "completed"

    def as_text(self) -> str:
        """Return the canonical JSON a scorer parses back.

        Canonical rather than pretty: this string is hashed onto the sample, so two runs whose
        judges answered identically must produce identical bytes.
        """
        return canonical_json(
            {"trials": [trial.as_json() for trial in self.trials], "stopped": self.stopped}
        )

    @classmethod
    def from_text(cls, text: str) -> JudgeRecord | None:
        """Parse a record from :meth:`as_text`'s output.

        Args:
            text: The stored text.

        Returns:
            The record, or ``None`` when ``text`` is not one. ``None`` rather than an exception,
            because the caller is a scorer and a scorer reports "unscoreable" instead of raising.
        """
        try:
            body = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(body, dict) or not isinstance(body.get("trials"), list):
            return None
        return cls(
            trials=tuple(
                JudgeTrial.from_json(item) for item in body["trials"] if isinstance(item, dict)
            ),
            stopped=str(body.get("stopped", "completed")),
        )


def parse_choice(text: str, *, labels: Sequence[str]) -> JudgeChoice:
    """Read one judge's verdict, or refuse to.

    Two accepted forms, in order: a JSON object anywhere in the answer carrying ``choice`` (or
    ``verdict``), and a whole line reading ``VERDICT: A``. Anything else is
    :attr:`JudgeChoice.UNPARSEABLE`.

    Args:
        text: Exactly what the judge returned.
        labels: The labels that were presented, in presentation order — normally ``("A", "B")``.

    Returns:
        The verdict. Case-insensitive on the label; a declared tie word yields
        :attr:`JudgeChoice.TIE`.

    Raises:
        ValueError: ``labels`` is empty. There is no verdict to parse from a presentation that
            offered nothing, and returning ``UNPARSEABLE`` would blame the judge for it.
    """
    if not labels:
        raise ValueError("parse_choice needs the labels that were presented; got none.")
    found = _from_json(text)
    if found is None:
        match = _VERDICT_LINE.search(text)
        found = match.group(1) if match is not None else None
    if found is None:
        return JudgeChoice.UNPARSEABLE
    token = found.strip().casefold()
    if token in _TIE_WORDS:
        return JudgeChoice.TIE
    for position, label in enumerate(labels):
        if token == label.casefold():
            return JudgeChoice.FIRST if position == 0 else JudgeChoice.SECOND
    return JudgeChoice.UNPARSEABLE


def _from_json(text: str) -> str | None:
    """Return the ``choice``/``verdict`` value of the first JSON object in ``text``, if any."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        body = json.loads(text[start : index + 1])
                    except ValueError:
                        break
                    if isinstance(body, dict):
                        for key in ("choice", "verdict", "answer"):
                            value = body.get(key)
                            if isinstance(value, str):
                                return value
                    break
        start = text.find("{", start + 1)
    return None


def blind_labels(subjects: Sequence[str]) -> tuple[str, ...]:
    """Return the anonymous labels that stand in for ``subjects``, in the same positions.

    Args:
        subjects: The real identities, in presentation order.

    Returns:
        ``("A", "B", …)``, one label per subject.

    Raises:
        ValueError: More subjects than :data:`BLIND_ALPHABET` has letters. Refused rather than
            wrapping round to ``A`` a second time, which would make two subjects indistinguishable
            to the judge *and* to the scorer.
    """
    if len(subjects) > len(BLIND_ALPHABET):
        raise ValueError(
            f"A blinded presentation holds at most {len(BLIND_ALPHABET)} subjects; got "
            f"{len(subjects)}."
        )
    return tuple(BLIND_ALPHABET[index] for index in range(len(subjects)))


def presentation_orders(subjects: Sequence[str]) -> tuple[tuple[int, ...], ...]:
    """Return the two orders a pair is presented in: as given, and swapped.

    Both orders, always, and only two of them. Position bias is measured by asking the same
    question twice with the positions exchanged; enumerating every permutation of a longer list
    would multiply calls without measuring anything the swap does not already show.

    Args:
        subjects: The subjects to present.

    Returns:
        ``((0, 1), (1, 0))`` for a pair; ``((0,),)`` for a single subject, which has no swap.

    Raises:
        ValueError: More than two subjects. A three-way comparison is a transitivity case and is
            scored as three pairs, not as one presentation.
    """
    if len(subjects) > 2:
        raise ValueError(
            f"presentation_orders describes a pair; got {len(subjects)} subjects. A three-way "
            "comparison is measured as three pairwise cases (benchmark catalog §3.11)."
        )
    if len(subjects) < 2:
        return (tuple(range(len(subjects))),)
    return ((0, 1), (1, 0))


def randomized_order(items: Sequence[Any], *, seed_material: str) -> tuple[Any, ...]:
    """Return ``items`` shuffled deterministically from ``seed_material``.

    Seeded from a string the caller composes out of the run's recorded seed and the case key, so
    the order a jury saw is reproducible from the run record alone. A shuffle seeded from the
    clock would make "the same run, again" impossible to mean anything — the same reasoning the
    run engine's case-order shuffle already uses.

    Args:
        items: What to shuffle. Not mutated.
        seed_material: The reproducible seed string.

    Returns:
        The shuffled items.
    """
    shuffled = list(items)
    random.Random(seed_material).shuffle(shuffled)  # noqa: S311 — presentation order, not crypto
    return tuple(shuffled)


def agreement_rate(choices: Iterable[JudgeChoice]) -> float | None:
    """Return the share of verdicts that agreed with the modal verdict.

    Args:
        choices: Every verdict for one question, repetitions included. Unparseable verdicts are
            excluded: "the judge did not answer" is not a third opinion for the others to agree
            or disagree with.

    Returns:
        ``1.0`` when every usable verdict matched, down towards ``1/k`` when they were evenly
        split. ``None`` when no verdict was usable — the honest value for "there was nothing to
        agree about", which keeps the sample out of the aggregate instead of scoring it zero
        (ADR-0016).
    """
    usable = [choice for choice in choices if choice is not JudgeChoice.UNPARSEABLE]
    if not usable:
        return None
    counts = Counter(usable)
    return counts.most_common(1)[0][1] / len(usable)


def majority_choice(choices: Iterable[JudgeChoice]) -> JudgeChoice:
    """Return the modal verdict, or :attr:`JudgeChoice.UNPARSEABLE` when none is usable.

    Ties between two equally-frequent verdicts resolve to :attr:`JudgeChoice.TIE`, which is the
    only honest reduction: a jury split two-two has not preferred either answer, and picking the
    first-seen would manufacture a preference out of iteration order.

    Args:
        choices: Every verdict for one question.

    Returns:
        The majority verdict.
    """
    usable = [choice for choice in choices if choice is not JudgeChoice.UNPARSEABLE]
    if not usable:
        return JudgeChoice.UNPARSEABLE
    counts = Counter(usable)
    ranked = counts.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return JudgeChoice.TIE
    return ranked[0][0]


@dataclass(frozen=True, slots=True)
class JurorEligibility:
    """Whether one model may serve on a jury, and why not when it may not.

    Attributes:
        model_canonical_id: The model this describes.
        eligible: Whether it may judge.
        reasons: Every reason it may not, sorted. Plural because a remote model that is also the
            candidate is refused twice over, and a UI that showed only the first would send the
            user to fix the wrong setting.
    """

    model_canonical_id: str
    eligible: bool
    reasons: tuple[str, ...] = ()


REASON_SELF_JUDGING = "self_judging"
"""The candidate cannot judge its own output. Refused and recorded, never discounted."""

REASON_REMOTE_NOT_PERMITTED = "remote_not_permitted"
"""A remote juror needs ``providers.allow_remote`` *and* the goal's own ``judge.allow_remote``."""

REASON_NOT_INSTALLED = "not_installed"
"""The configuration names a juror this machine does not have."""


def eligible_jurors(
    available: Sequence[str],
    *,
    candidate: str | None,
    requested: Sequence[str] = (),
    remote: Mapping[str, bool] | None = None,
    allow_remote: bool = False,
) -> tuple[JurorEligibility, ...]:
    """Decide which models may judge, and record why each refusal happened.

    The two refusals are the ones both ADR-0031 §4 and the spec's error table name, and they are
    decided here — in the domain, with no provider in sight — so that the jury service, the
    ``judges validate`` command and the API endpoint cannot disagree about who is eligible.

    Args:
        available: Canonical IDs of the models this machine can serve.
        candidate: The model being measured, which is never eligible to judge itself, or ``None``
            when nothing is being measured (``freeweight judges list``).
        requested: The jury the configuration asked for, in order. Empty means "consider every
            available model", which is the auto-selection default.
        remote: Which models are remote, by canonical ID. A model absent from the mapping is
            treated as local, because the honest default for "we do not know where this runs" on
            a local-first tool is the local one, and a provider that serves remotely declares it.
        allow_remote: Whether both remote opt-ins are satisfied. The caller performs the ``and``;
            this function does not read configuration.

    Returns:
        One entry per considered model, in the order considered: ``requested`` order when a jury
        was named, otherwise ``available`` order. A requested model that is not installed appears
        with :data:`REASON_NOT_INSTALLED`, because a jury that silently shrank would produce a
        different instrument from the one the user configured without saying so.
    """
    remote_by_id = dict(remote or {})
    considered = list(requested) if requested else list(available)
    installed = set(available)
    verdicts: list[JurorEligibility] = []
    for model in considered:
        reasons: list[str] = []
        if model not in installed:
            reasons.append(REASON_NOT_INSTALLED)
        if candidate is not None and model == candidate:
            reasons.append(REASON_SELF_JUDGING)
        if remote_by_id.get(model, False) and not allow_remote:
            reasons.append(REASON_REMOTE_NOT_PERMITTED)
        verdicts.append(
            JurorEligibility(
                model_canonical_id=model,
                eligible=not reasons,
                reasons=tuple(sorted(reasons)),
            )
        )
    return tuple(verdicts)


def judge_benchmark_reference(
    juror_canonical_ids: Sequence[str],
    *,
    prompt_id: str,
    prompt_version: str,
    prompt_sha256: str,
    remote: bool,
) -> dict[str, Any]:
    """Return the link from a judged score to the jurors' own judge-benchmark results.

    Benchmark catalog §1 requires that "where a judge is used, the judge model's own
    judge-benchmark results are linked from the result". This is that link, in the shape the
    ``judge_set`` field of ``capability.evidence`` already uses (ADR-0032 §5) plus the suite key
    and the bias metrics a reader should look at — so a UI can render "how trustworthy is this
    instrument" without knowing which metrics ``native.judge`` happens to declare.

    Args:
        juror_canonical_ids: The jury, in the order it was polled.
        prompt_id: The judge prompt record's ID.
        prompt_version: That record's version.
        prompt_sha256: That record's canonical hash.
        remote: Whether any juror ran off this machine.

    Returns:
        The link, JSON-safe.
    """
    return {
        "jurors": list(juror_canonical_ids),
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "remote": remote,
        "judge_benchmark": {
            "suite_key": JUDGE_SUITE_KEY,
            "metric_keys": list(BIAS_METRIC_KEYS),
        },
    }


BIAS_METRIC_KEYS: tuple[str, ...] = (
    "pairwise_accuracy",
    "swap_consistency",
    "position_preference_rate",
    "repetition_agreement_rate",
    "verbosity_preference_rate",
    "style_preference_rate",
    "transitivity_violation_rate",
    "self_preference_delta",
)
"""The ``native.judge`` figures that characterize a juror, in catalog §3.11's table order.

Named once, here, because three things read them: the suite that produces them, the link a judged
score carries, and the UI that renders "how trustworthy is this instrument"."""


@dataclass(frozen=True, slots=True)
class BiasObservation:
    """One case's contribution to one bias figure, as a scorer records it.

    A thin pair rather than a bare float so that "this case measured nothing for this figure" is
    representable. A rate whose denominator is empty for a case must be *absent* from that case,
    not zero — the aggregation layer excludes it and counts the exclusion, which is what keeps a
    swap-consistency rate honest when a judge failed to answer either presentation.

    Attributes:
        key: The metric key, one of :data:`BIAS_METRIC_KEYS`.
        value: The measured value, or ``None`` when this case measured nothing for it.
    """

    key: str
    value: float | None = None


def observations_to_detail(observations: Iterable[BiasObservation]) -> dict[str, float]:
    """Fold observations into the ``detail`` mapping a scorer returns.

    Args:
        observations: What one case measured.

    Returns:
        Only the keys that were measured. An observation with ``value=None`` is omitted, which is
        precisely what makes the run-level rate exclude the case instead of averaging in a zero.
    """
    return {
        observation.key: float(observation.value)
        for observation in observations
        if observation.value is not None
    }


@dataclass(frozen=True, slots=True)
class BlindedPresentation:
    """One question as a judge sees it: labels, texts and the identities behind them.

    Assembled by the caller and handed to the prompt renderer, so the only place that knows which
    label hides which model is the code that built the presentation and the code that scores it —
    never the prompt.

    Attributes:
        labels: ``("A", "B")``, in presentation order.
        texts: The answers, in the same positions.
        subjects: The identities, in the same positions.
    """

    labels: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()

    def rendered(self) -> str:
        """Return the labelled answer block a judge prompt renders.

        One place builds this string, so a swapped presentation and its original cannot differ in
        anything but order.
        """
        return "\n\n".join(
            f"ANSWER {label}\n{text}" for label, text in zip(self.labels, self.texts, strict=True)
        )


def present(
    subjects: Sequence[str], texts: Sequence[str], order: Sequence[int]
) -> BlindedPresentation:
    """Build one blinded presentation of ``subjects`` in the given positional ``order``.

    Args:
        subjects: The identities, in their natural order.
        texts: Their answers, in the same order as ``subjects``.
        order: Positions into ``subjects``, giving the order to present them in.

    Returns:
        The presentation, labelled ``A``, ``B``, … in presentation order.

    Raises:
        ValueError: ``subjects`` and ``texts`` are different lengths, or ``order`` names a
            position that does not exist. Both are authoring defects and both would otherwise
            reach a judge as a presentation missing an answer.
    """
    if len(subjects) != len(texts):
        raise ValueError(
            f"present() needs one text per subject; got {len(subjects)} subjects and "
            f"{len(texts)} texts."
        )
    if any(position >= len(subjects) or position < 0 for position in order):
        raise ValueError(
            f"present() order {list(order)} names a position outside 0..{len(subjects) - 1}."
        )
    ordered_subjects = tuple(subjects[position] for position in order)
    return BlindedPresentation(
        labels=blind_labels(ordered_subjects),
        texts=tuple(texts[position] for position in order),
        subjects=ordered_subjects,
    )
