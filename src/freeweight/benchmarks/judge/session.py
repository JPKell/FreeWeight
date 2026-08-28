"""freeweight.benchmarks.judge.session — how ``native.judge`` drives one case's presentations.

Six of benchmark catalog §3.11's seven tests need more than one provider call for one sample,
because the measurement *is* the difference between two calls: the same pair in both orders, the
same comparison five times, the same answer with and without attribution. One call could not
express any of them.

This is an :class:`~freeweight.benchmarks.interaction.Interaction`
([ADR-0033](../../../../../docs/adr/0033-benchmark-interaction-protocol.md)): the run engine owns
the provider, the frozen execution parameters and the cost accounting; this module owns only what
to say next. What it says next is decided entirely by the case's declared plan and never by the
model's text — the loop here has no branch on a response at all, which is testing standards §3
applied to the one suite whose input is an opinion.

**The outcome's text is the trial record, not the last turn.** A judged case is a *set* of
verdicts, and its final turn describes none of them. Serializing
:class:`~freeweight.domain.judging.JudgeRecord` as the sample's text is what lets the scorer stay a
pure function of ``(expectation, text)`` — and what makes the stored sample drill to every
presentation the judge was shown rather than to whichever one happened to be last.

**Order within a presentation is declared; order between presentations is seeded.** A swap is
declared by the case, because the swap is the measurement. Where a case has genuine freedom — the
three sub-comparisons of a transitivity triple — the order is shuffled from the case's own key, so
it is reproducible from the run record and identical on every machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modelrack.types import Message, Role

from freeweight.benchmarks.interaction import InteractionOutcome
from freeweight.domain.judging import (
    JudgeChoice,
    JudgeRecord,
    JudgeTrial,
    parse_choice,
    present,
    randomized_order,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from modelrack.types import GenerationResult

    from freeweight.benchmarks.interaction import TurnCaller
    from freeweight.domain.benchmark import BenchmarkCase
    from freeweight.services.prompts import PromptLibrary

__all__ = [
    "ANSWER_PROMPT_ID",
    "EXCERPT_CHARACTERS",
    "OWN_SUBJECT",
    "PAIRWISE_PROMPT_ID",
    "JudgeSession",
    "presentation_variables",
]

PAIRWISE_PROMPT_ID = "benchmarks.judge.pairwise"
"""The record every presentation renders."""

ANSWER_PROMPT_ID = "benchmarks.judge.answer"
"""The record the self-preference test uses to obtain the judge's own answer first."""

OWN_SUBJECT = "own"
"""The subject name standing for the judge's own answer in a self-preference case."""

EXCERPT_CHARACTERS = 200
"""How much of each verdict is kept on the record.

Spec §14's bounded exception: scorer evidence may hold "an excerpt of the answer capped at 200
characters". A judge's one-sentence reason fits comfortably; anything longer is a rationale, and a
rationale is content."""

_ATTRIBUTION_TEMPLATE = "Answer {label} is your own answer; the other is another model's.\n"
"""The one line that distinguishes an attributed presentation from a blinded one.

Exactly one line, and it names only which label is the judge's own. The self-preference delta is
the difference between two presentations that are otherwise identical, so anything else this line
said would be measured as self-preference."""


def presentation_variables(*, question: str, answers: str, attribution: str = "") -> dict[str, Any]:
    """Return the prompt variables for one presentation.

    A named function rather than an inline dict because the suite's *case-level* prompt hash is
    rendered from the same variables the session sends, and two places building that mapping
    would let the stored ``rendered_prompt_hash`` describe a prompt that was never sent.

    Args:
        question: The question both answers respond to.
        answers: The labelled answer block.
        attribution: The attribution line, or ``""`` for a blinded presentation.

    Returns:
        The variables.
    """
    return {"question": question, "answers": answers, "attribution": attribution}


@dataclass(frozen=True, slots=True)
class JudgeSession:
    """Runs one judge case: obtain an answer if the case needs one, then present and record.

    Args:
        library: The pack the presentation prompts are rendered from.
        pairwise_prompt_id: Which record a presentation renders.
        answer_prompt_id: Which record the self-preference test's first turn renders.
    """

    library: PromptLibrary
    pairwise_prompt_id: str = PAIRWISE_PROMPT_ID
    answer_prompt_id: str = ANSWER_PROMPT_ID

    def run(self, caller: TurnCaller, case: BenchmarkCase) -> InteractionOutcome:
        """Make every presentation the case declares and record what came back.

        Args:
            caller: The engine's next-turn function.
            case: The case, whose ``metadata`` carries the question, the subject texts and the
                presentation plan.

        Returns:
            The outcome, whose ``text`` is the serialized
            :class:`~freeweight.domain.judging.JudgeRecord`. A provider failure part-way through
            ends the interaction and is recorded on the record's ``stopped``; the presentations
            that did complete are kept, because a judge that answered three of four questions has
            told us three things.
        """
        from modelrack.errors import ProviderError

        question = str(case.metadata.get("question", ""))
        texts = {
            str(name): str(text) for name, text in dict(case.metadata.get("texts", {})).items()
        }
        plan = [dict(entry) for entry in case.metadata.get("presentations", ())]
        results: list[GenerationResult] = []
        trials: list[JudgeTrial] = []
        stopped = "completed"
        error_code: str | None = None
        error_text: str | None = None

        if OWN_SUBJECT in {str(name) for entry in plan for name in entry.get("subjects", ())}:
            rendered = self.library.render(self.answer_prompt_id, {"question": question})
            try:
                own = caller(_turns(rendered.system, rendered.user))
            except ProviderError as exc:
                return InteractionOutcome(
                    text=JudgeRecord(trials=(), stopped=exc.code).as_text(),
                    detail={"presentations": 0, "own_answer_obtained": 0.0},
                    error_code=exc.code,
                    error_text=exc.message,
                )
            results.append(own)
            texts[OWN_SUBJECT] = own.text

        for ordinal, entry in enumerate(_ordered(plan, case.case_id)):
            subjects = tuple(str(name) for name in entry.get("subjects", ()))
            missing = [name for name in subjects if name not in texts]
            if missing:  # pragma: no cover — a case declaring an absent subject fails at build
                raise ValueError(
                    f"Judge case {case.case_id!r} presents subjects {missing} it supplies no text "
                    "for."
                )
            presentation = present(
                subjects, [texts[name] for name in subjects], range(len(subjects))
            )
            attribution = (
                _ATTRIBUTION_TEMPLATE.format(
                    label=presentation.labels[presentation.subjects.index(OWN_SUBJECT)]
                )
                if entry.get("attributed") and OWN_SUBJECT in presentation.subjects
                else ""
            )
            rendered = self.library.render(
                self.pairwise_prompt_id,
                presentation_variables(
                    question=question,
                    answers=presentation.rendered(),
                    attribution=attribution,
                ),
            )
            try:
                result = caller(_turns(rendered.system, rendered.user))
            except ProviderError as exc:
                stopped, error_code, error_text = exc.code, exc.code, exc.message
                break
            results.append(result)
            trials.append(
                JudgeTrial(
                    ordinal=ordinal,
                    order=presentation.labels,
                    subjects=presentation.subjects,
                    choice=parse_choice(result.text, labels=presentation.labels),
                    group=str(entry.get("group", "")),
                    raw_excerpt=result.text[:EXCERPT_CHARACTERS],
                )
            )

        record = JudgeRecord(trials=tuple(trials), stopped=stopped)
        unparseable = sum(1 for trial in trials if trial.choice is JudgeChoice.UNPARSEABLE)
        return InteractionOutcome(
            results=tuple(results),
            text=record.as_text(),
            detail={
                "presentations": len(trials),
                "unparseable_verdicts": float(unparseable),
            },
            error_code=error_code,
            error_text=error_text,
        )


def _turns(system: str | None, user: str) -> list[Message]:
    """Open a one-turn conversation from a rendered prompt."""
    messages: list[Message] = []
    if system:
        messages.append(Message(role=Role.SYSTEM, content=system))
    messages.append(Message(role=Role.USER, content=user))
    return messages


def _ordered(plan: Sequence[Mapping[str, Any]], case_id: str) -> tuple[Mapping[str, Any], ...]:
    """Return the plan's presentations in the order they will be made.

    A presentation whose entry declares ``"fixed": true`` keeps its declared position — a swap is
    only a swap if the pair is asked both ways, and shuffling the two apart would still measure
    position bias but would stop the record reading as a pair. Everything else is shuffled from
    the case's own key, deterministically, so the order is reproducible without being an artefact
    of how the case file happened to be written.
    """
    if all(entry.get("fixed") for entry in plan):
        return tuple(plan)
    return randomized_order(plan, seed_material=f"judge:{case_id}")
