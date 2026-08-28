"""freeweight.benchmarks.long_context.haystack — building one document of a declared size.

A long-context case is a *document with something buried in it*, and the document is the variable
being swept. Checking a hundred thousand tokens of filler into git would give the repository a
corpus nobody can diff and a suite whose dataset hash changes whenever an editor touches
whitespace, so the filler is expanded here from a small sentence pool the corpus does hold.

**Expansion is deterministic and hashed by its inputs.** The same ``(tokens, position, needle)``
triple always produces the same document, in this process and in any other, so a case's rendered
prompt hash is stable across runs and machines. Sentences are numbered as they are laid down,
which keeps them distinguishable to a reader without introducing content a model could answer from.

**Token counts are an approximation, and the approximation is declared.** This module has no
tokenizer — a suite that shipped one would be measuring that tokenizer, and the count a provider
reports is the count that goes on the sample. :data:`CHARACTERS_PER_TOKEN` is the same rule of
thumb :mod:`modelrack.testing`'s fake uses, so a case asking for "about 8 000 tokens" gets a
document of about that size on any backend, and the *measured* size is whatever the provider
counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CHARACTERS_PER_TOKEN",
    "Haystack",
    "approximate_tokens",
    "assemble",
]

CHARACTERS_PER_TOKEN = 4
"""Characters this module counts as one token when sizing a document.

A declared approximation, not a measurement. It exists so a case can ask for a document of a
stated size without this package owning a tokenizer; every token count that reaches a result comes
from the provider."""

_ANSWER_HEADROOM_TOKENS = 512
"""Context a case needs beyond its document: the instruction, the question and the answer.

Added to a case's ``required_context_tokens`` so that a case is skipped for a model that cannot
serve it, rather than sent and failed on a truncation the suite would have scored as a retrieval
error."""


@dataclass(frozen=True, slots=True)
class Haystack:
    """One assembled document and the facts about how it was assembled.

    Attributes:
        text: The document.
        context_tokens: The document's approximate size, by :data:`CHARACTERS_PER_TOKEN`.
        position_percent: Where the needle was placed, as a percentage of the document.
        distractor_count: How many near-miss sentences were mixed in.
        required_context_tokens: What a model must be served to receive this case at all.
    """

    text: str
    context_tokens: int
    position_percent: int
    distractor_count: int

    @property
    def required_context_tokens(self) -> int:
        """The served context this case needs, document plus headroom."""
        return self.context_tokens + _ANSWER_HEADROOM_TOKENS


def approximate_tokens(text: str) -> int:
    """Return ``text``'s approximate size in tokens.

    Args:
        text: The document.

    Returns:
        The character count divided by :data:`CHARACTERS_PER_TOKEN`, rounded down. Rounded down
        rather than up so a case never claims to be larger than it is.
    """
    return len(text) // CHARACTERS_PER_TOKEN


@lru_cache(maxsize=64)
def assemble(  # noqa: PLR0913 — every argument is a swept dimension of the case
    *,
    filler: tuple[str, ...],
    facts: tuple[str, ...],
    distractors: tuple[str, ...],
    context_tokens: int,
    position_percent: int,
    second_position_percent: int = -1,
) -> Haystack:
    """Build one document of about ``context_tokens`` tokens with ``facts`` buried in it.

    Cached, because ``build_registry`` runs at every startup and in every test that touches the
    run engine, and rebuilding a quarter of a megabyte of filler each time would make suite
    construction the slowest part of a unit test that never runs a case.

    Args:
        filler: The sentence pool, cycled with a running number so no two lines are identical.
        facts: The sentences to bury, in order. One for a retrieval case, two for a distributed
            reasoning case.
        distractors: Near-miss sentences to mix in, spread evenly through the document.
        context_tokens: The document's approximate target size.
        position_percent: Where the first fact goes, as a percentage of the filler.
        second_position_percent: Where the second fact goes, or ``-1`` when there is only one.

    Returns:
        The assembled document and its facts.

    Raises:
        ValueError: ``filler`` is empty, ``facts`` is empty, ``context_tokens`` is not positive,
            or a position is outside ``0..100``. Every one of them would produce a document that
            silently measured something other than what the case asked for.
    """
    if not filler:
        raise ValueError("A haystack needs a non-empty filler pool.")
    if not facts:
        raise ValueError(
            "A haystack needs at least one fact to bury; a document with no needle "
            "measures nothing."
        )
    if context_tokens <= 0:
        raise ValueError(f"A haystack needs a positive context_tokens; got {context_tokens}.")
    positions = [position_percent] + (
        [second_position_percent] if second_position_percent >= 0 else []
    )
    for percent in positions:
        if not 0 <= percent <= 100:  # noqa: PLR2004 — a percentage's own bounds
            raise ValueError(f"A needle position must be within 0..100; got {percent}.")

    budget = context_tokens * CHARACTERS_PER_TOKEN
    lines = _filler_lines(filler, distractors, budget)
    for fact, percent in sorted(zip(facts, positions, strict=True), key=lambda item: -item[1]):
        index = min(len(lines), round(len(lines) * percent / 100))
        lines.insert(index, fact)
    text = "\n".join(lines)
    return Haystack(
        text=text,
        context_tokens=approximate_tokens(text),
        position_percent=position_percent,
        distractor_count=len(distractors),
    )


def _filler_lines(
    filler: Sequence[str], distractors: Sequence[str], budget_characters: int
) -> list[str]:
    """Lay down numbered filler until the character budget is spent, mixing distractors in evenly.

    The distractors are spread rather than clustered: a document whose near misses all sat together
    would let a model that found one of them skip the rest of the text, which measures search
    behaviour rather than retrieval.
    """
    lines: list[str] = []
    used = 0
    number = 0
    every = 0
    if distractors:
        # One distractor per this many filler lines, chosen so they finish about when the filler
        # does rather than all landing in the first paragraph.
        approximate_lines = max(len(distractors), budget_characters // 70)
        every = max(1, approximate_lines // len(distractors))
    placed = 0
    while used < budget_characters:
        number += 1
        sentence = filler[(number - 1) % len(filler)]
        line = f"{number}. {sentence}"
        lines.append(line)
        used += len(line) + 1
        if every and placed < len(distractors) and number % every == 0:
            distractor = f"{number}a. {distractors[placed]}"
            lines.append(distractor)
            used += len(distractor) + 1
            placed += 1
    return lines
