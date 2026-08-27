"""freeweight.domain.scorers.rule — rung-2 constraint checking for instruction following.

Benchmark catalog §3.4 names the constraint kinds — exact list length, required phrase, forbidden
phrase, word-count range, specific opening and closing text, formatting, language, and several at
once — and the metrics they roll up into: strict prompt accuracy, loose prompt accuracy,
instruction-level accuracy, and violation counts by class.

**A constraint is data, and it is the same data the model was shown.** Each constraint declares its
own human-readable rendering (:meth:`Constraint.as_instruction`), and the suite renders the prompt's
constraint list from exactly the constraints the scorer will check. That is what stops the two from
drifting: there is no second place where the rules are written down in English.

**Three accuracies, because they answer three questions.**

* *Strict prompt accuracy* — every constraint met, on the response exactly as returned. One number
  per case, 1 or 0.
* *Loose prompt accuracy* — every constraint met after the response is stripped of the wrappers a
  model adds around a correct answer: a leading "Sure, here you go:", surrounding code fences,
  surrounding quotes. A model that obeyed every instruction and then said hello is a different
  failure from one that ignored the instructions, and reporting only the strict figure conflates
  them.
* *Instruction-level accuracy* — the fraction of individual constraints met, which is what makes a
  response that broke one rule of five distinguishable from one that broke five.

Nothing here consults a model, and nothing here is a heuristic about meaning: every constraint is
decided by counting, comparing or matching a linted pattern.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.domain.benchmark import BenchmarkCase

__all__ = [
    "EXPECTATION_KEY",
    "Constraint",
    "ConstraintKind",
    "RuleScorer",
    "ViolationClass",
    "check_constraints",
    "loose_text",
]

EXPECTATION_KEY = "constraints"
"""The key under which a case declares the constraints this scorer checks."""

ERROR_NO_CONSTRAINTS = "NO_EXPECTATION"
"""The case declared no constraints, so "did it follow them" has no answer."""

ERROR_CONSTRAINT_INVALID = "CONSTRAINT_INVALID"
"""A constraint is malformed — an unknown kind, or a bound that is not a number."""

_MAXIMUM_PATTERN_LENGTH = 200
"""Longest regex a case may declare.

A bound, not a style rule: a pattern is data in a case file, it runs against model output, and
catastrophic backtracking in it would hang the run rather than fail the criterion (spec §14). The
dialect is linted as well (:func:`_lint_pattern`); the length cap is the cheap half of the guard.
"""

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL)
_LEAD_IN = re.compile(
    r"^(?:sure|certainly|of course|absolutely|here(?:'s| is)\b)[^\n]*\n+", re.IGNORECASE
)
"""A conversational first line, and only a *first line*.

The trailing ``\n+`` is load-bearing: a one-line answer that happens to begin "Certainly the
fastest option is…" is the answer, not a wrapper around one, and stripping it would forgive a
violation instead of forgiving a preamble."""


class ViolationClass(StrEnum):
    """The violation classes benchmark catalog §3.4 counts.

    A class per constraint kind, declared on the kind rather than derived from its name: a count
    keyed on a substring of the kind's name gets a new kind wrong the first time one is added.
    """

    FORMAT = "format"
    LENGTH = "length"
    KEYWORD = "keyword"
    STRUCTURE = "structure"
    LANGUAGE = "language"


class ConstraintKind(StrEnum):
    """Every constraint this scorer can decide, all of them by counting or matching."""

    REQUIRED_PHRASE = "required_phrase"
    FORBIDDEN_PHRASE = "forbidden_phrase"
    WORD_COUNT_RANGE = "word_count_range"
    LINE_COUNT = "line_count"
    LIST_LENGTH = "list_length"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    MATCHES = "matches"
    EVERY_LINE_MATCHES = "every_line_matches"
    SCRIPT = "script"


_CLASSES: Mapping[ConstraintKind, ViolationClass] = {
    ConstraintKind.REQUIRED_PHRASE: ViolationClass.KEYWORD,
    ConstraintKind.FORBIDDEN_PHRASE: ViolationClass.KEYWORD,
    ConstraintKind.WORD_COUNT_RANGE: ViolationClass.LENGTH,
    ConstraintKind.LINE_COUNT: ViolationClass.LENGTH,
    ConstraintKind.LIST_LENGTH: ViolationClass.STRUCTURE,
    ConstraintKind.STARTS_WITH: ViolationClass.FORMAT,
    ConstraintKind.ENDS_WITH: ViolationClass.FORMAT,
    ConstraintKind.MATCHES: ViolationClass.FORMAT,
    ConstraintKind.EVERY_LINE_MATCHES: ViolationClass.STRUCTURE,
    ConstraintKind.SCRIPT: ViolationClass.LANGUAGE,
}


_NEEDS_VALUE: frozenset[ConstraintKind] = frozenset(
    {
        ConstraintKind.REQUIRED_PHRASE,
        ConstraintKind.FORBIDDEN_PHRASE,
        ConstraintKind.LIST_LENGTH,
        ConstraintKind.STARTS_WITH,
        ConstraintKind.ENDS_WITH,
        ConstraintKind.MATCHES,
        ConstraintKind.EVERY_LINE_MATCHES,
        ConstraintKind.SCRIPT,
    }
)
"""Kinds whose ``value`` is their operand. The two counting kinds carry bounds instead."""

_NEEDS_BOUND: frozenset[ConstraintKind] = frozenset(
    {
        ConstraintKind.WORD_COUNT_RANGE,
        ConstraintKind.LINE_COUNT,
        ConstraintKind.LIST_LENGTH,
    }
)
"""Kinds that need at least one bound to mean anything."""


class ConstraintInvalid(ValueError):
    """A constraint declaration this module refuses to interpret.

    Raised where the case is built, never during scoring: a scorer that guessed at a malformed
    constraint would report a model failure caused by a typo in a fixture.
    """


@dataclass(frozen=True, slots=True)
class Constraint:
    """One machine-checkable requirement on a response.

    Attributes:
        kind: What is being required.
        value: The kind's operand — a phrase, a pattern, a script name, or ``None`` for the
            purely numeric kinds.
        minimum: Inclusive lower bound, for the counting kinds.
        maximum: Inclusive upper bound, for the counting kinds.
        case_sensitive: Whether phrase and prefix comparisons respect case. ``False`` by default,
            because "include the word inference" is almost never a claim about capitalisation —
            a case that means it says so.
        instruction: The sentence the model is shown. Declared, not generated from the fields, so
            a constraint can be phrased naturally; :meth:`as_instruction` falls back to a
            mechanical rendering when it is absent.
    """

    kind: ConstraintKind
    value: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    case_sensitive: bool = False
    instruction: str = ""

    @property
    def violation_class(self) -> ViolationClass:
        """The catalog §3.4 class a failure of this constraint is counted under."""
        return _CLASSES[self.kind]

    def as_instruction(self) -> str:
        """Return the sentence shown to the model for this constraint."""
        if self.instruction:
            return self.instruction
        parts = [self.kind.value.replace("_", " ")]
        if self.value is not None:
            parts.append(repr(self.value))
        if self.minimum is not None or self.maximum is not None:
            parts.append(f"between {self.minimum} and {self.maximum}")
        return " ".join(parts)

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> Constraint:
        """Build one constraint from a case's declaration.

        Args:
            body: The declaration, with at least ``kind``.

        Returns:
            The constraint.

        Raises:
            ConstraintInvalid: ``kind`` is missing or unknown, a bound is not a whole number, a
                kind that needs a ``value`` has none, or a declared pattern fails the dialect lint.
        """
        raw_kind = str(body.get("kind", ""))
        try:
            kind = ConstraintKind(raw_kind)
        except ValueError as exc:
            raise ConstraintInvalid(
                f"Unknown constraint kind {raw_kind!r}; this build checks "
                f"{[member.value for member in ConstraintKind]}."
            ) from exc
        value = body.get("value")
        if kind in _NEEDS_VALUE and value is None:
            raise ConstraintInvalid(f"Constraint {kind.value!r} requires a 'value'.")
        if kind in _NEEDS_BOUND and body.get("minimum") is None and body.get("maximum") is None:
            raise ConstraintInvalid(
                f"Constraint {kind.value!r} requires a 'minimum', a 'maximum', or both; a range "
                "with neither bound is met by every response and would silently measure nothing."
            )
        bounds: dict[str, int | None] = {}
        for name in ("minimum", "maximum"):
            supplied = body.get(name)
            if supplied is None:
                bounds[name] = None
            elif isinstance(supplied, bool) or not isinstance(supplied, int):
                raise ConstraintInvalid(
                    f"Constraint {kind.value!r} bound {name!r} must be a whole number; got "
                    f"{supplied!r}."
                )
            else:
                bounds[name] = supplied
        if kind in {ConstraintKind.MATCHES, ConstraintKind.EVERY_LINE_MATCHES}:
            _lint_pattern(str(value))
        return cls(
            kind=kind,
            value=None if value is None else str(value),
            minimum=bounds["minimum"],
            maximum=bounds["maximum"],
            case_sensitive=bool(body.get("case_sensitive", False)),
            instruction=str(body.get("instruction", "")),
        )


def _lint_pattern(pattern: str) -> None:
    """Refuse a regex whose dialect this build will not run.

    Spec §14's linted dialect, applied to benchmark case data for the same reason it is applied to
    user-authored goal criteria: a pattern with a backreference or an unbounded nested quantifier
    can take exponential time on model output, and a criterion that hangs the run is worse than
    one that fails.

    Raises:
        ConstraintInvalid: The pattern is too long, does not compile, uses a backreference, or
            nests an unbounded quantifier inside another.
    """
    if len(pattern) > _MAXIMUM_PATTERN_LENGTH:
        raise ConstraintInvalid(
            f"Constraint pattern is {len(pattern)} characters; the limit is "
            f"{_MAXIMUM_PATTERN_LENGTH}."
        )
    if re.search(r"\\[1-9]|\(\?P=", pattern):
        raise ConstraintInvalid(
            f"Constraint pattern {pattern!r} uses a backreference, which this dialect refuses: "
            "backreferences are what make catastrophic backtracking reachable."
        )
    if re.search(r"\([^)]*[+*][^)]*\)\s*[+*]", pattern):
        raise ConstraintInvalid(
            f"Constraint pattern {pattern!r} nests an unbounded quantifier inside another; bound "
            "the inner repetition explicitly."
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConstraintInvalid(f"Constraint pattern {pattern!r} does not compile: {exc}") from exc


def loose_text(text: str) -> str:
    """Strip the wrappers a model adds around an otherwise-obedient answer.

    Exactly three: a code fence around the whole response, a conversational lead-in line, and
    surrounding quotation marks. Deliberately not "clean up the answer" — each of these is a
    wrapper the model added *outside* the thing it was asked for, and anything more would start
    forgiving the violations the strict figure exists to count.
    """
    result = text.strip()
    fenced = _FENCE.match(result)
    if fenced is not None:
        result = fenced.group("body").strip()
    result = _LEAD_IN.sub("", result).strip()
    if len(result) >= 2 and result[0] == result[-1] and result[0] in {'"', "'"}:
        result = result[1:-1].strip()
    return result


def check_constraints(
    constraints: Sequence[Constraint], text: str
) -> tuple[tuple[Constraint, ...], tuple[Constraint, ...]]:
    """Partition ``constraints`` into those ``text`` meets and those it violates.

    Args:
        constraints: The constraints to check.
        text: The response text, already normalized by the caller if it means to be lenient.

    Returns:
        ``(met, violated)``, each in declaration order.
    """
    met: list[Constraint] = []
    violated: list[Constraint] = []
    for constraint in constraints:
        (met if _is_met(constraint, text) else violated).append(constraint)
    return tuple(met), tuple(violated)


def _is_met(constraint: Constraint, text: str) -> bool:
    """Decide one constraint against one response."""
    subject = text if constraint.case_sensitive else text.casefold()
    value = constraint.value or ""
    needle = value if constraint.case_sensitive else value.casefold()
    match constraint.kind:
        case ConstraintKind.REQUIRED_PHRASE:
            return needle in subject
        case ConstraintKind.FORBIDDEN_PHRASE:
            return needle not in subject
        case ConstraintKind.WORD_COUNT_RANGE:
            return _within(len(text.split()), constraint)
        case ConstraintKind.LINE_COUNT:
            return _within(len(_lines(text)), constraint)
        case ConstraintKind.LIST_LENGTH:
            return _within(
                sum(1 for line in _lines(text) if re.match(str(value), line) is not None),
                constraint,
            )
        case ConstraintKind.STARTS_WITH:
            return subject.startswith(needle)
        case ConstraintKind.ENDS_WITH:
            return subject.rstrip().endswith(needle.rstrip())
        case ConstraintKind.MATCHES:
            return re.search(value, text, re.DOTALL) is not None
        case ConstraintKind.EVERY_LINE_MATCHES:
            lines = _lines(text)
            return bool(lines) and all(re.match(value, line) is not None for line in lines)
        case _:  # ConstraintKind.SCRIPT
            return _is_script(text, value)


def _lines(text: str) -> list[str]:
    """Return the response's non-blank lines, stripped."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _within(count: int, constraint: Constraint) -> bool:
    """Return whether ``count`` sits inside the constraint's inclusive bounds.

    A constraint with neither bound is met by any count: "declare a range and then declare none of
    it" is a case-authoring mistake, and failing every response for it would report it as a model
    failure. :meth:`Constraint.from_json` is where such a case is caught.
    """
    if constraint.minimum is not None and count < constraint.minimum:
        return False
    return not (constraint.maximum is not None and count > constraint.maximum)


def _is_script(text: str, script: str) -> bool:
    """Return whether every cased letter in ``text`` belongs to the named Unicode script.

    A *script* check, not a language check. Deciding that a paragraph is Spanish rather than
    Portuguese needs a model or a statistical classifier, and the catalog's language constraint
    only has to be deterministic to be useful: "answer in Greek" is met when the letters are
    Greek. A case that needs more than this is asking for a rung the catalog reserves for judges.
    """
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return False
    wanted = script.upper()
    return all(wanted in unicodedata.name(character, "") for character in letters)


@dataclass(frozen=True, slots=True)
class RuleScorer:
    """Scores a response against a case's declared constraints.

    The headline ``score`` is *strict prompt accuracy*: ``1.0`` only when every constraint is met
    on the response exactly as returned. The looser figures are reported alongside it in
    ``detail``, where the suite's metric definitions pick them up — never blended into the headline,
    because a single number that silently forgave a code fence would make two different behaviours
    indistinguishable.
    """

    key: str = "instruction_constraints"
    method: ScoreMethod = ScoreMethod.RULE
    constraint_key: str = field(default=EXPECTATION_KEY)

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Check every declared constraint and report the three accuracies.

        Args:
            case: The case, carrying its constraint declarations.
            response_text: Exactly what the model returned.

        Returns:
            ``score`` is strict prompt accuracy (``1.0`` or ``0.0``). ``detail`` carries
            ``strict_prompt_accuracy``, ``loose_prompt_accuracy``,
            ``instruction_level_accuracy``, the per-class violation counts and the list of
            violated constraints. ``score=None`` with a reason when the case declares no
            constraints, or declares one this build cannot interpret — both are defects in the
            case, and scoring them ``0.0`` would blame the model.
        """
        declared = case.expectation.get(self.constraint_key)
        if not isinstance(declared, list | tuple) or not declared:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_NO_CONSTRAINTS,
                error_text=(
                    f"Case {case.case_id!r} declares no expectation[{self.constraint_key!r}], so "
                    "there is nothing to check the response against."
                ),
            )
        try:
            constraints = tuple(Constraint.from_json(dict(item)) for item in declared)
        except (ConstraintInvalid, TypeError, ValueError) as exc:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_CONSTRAINT_INVALID,
                error_text=f"Case {case.case_id!r} declares a constraint this build refuses: {exc}",
            )

        met, violated = check_constraints(constraints, response_text)
        _, loose_violated = check_constraints(constraints, loose_text(response_text))
        counts = {member.value: 0 for member in ViolationClass}
        for constraint in violated:
            counts[constraint.violation_class.value] += 1
        strict = 1.0 if not violated else 0.0
        return ScoreResult(
            score=strict,
            method=self.method,
            detail={
                "case": case.case_id,
                "strict_prompt_accuracy": strict,
                "loose_prompt_accuracy": 1.0 if not loose_violated else 0.0,
                "instruction_level_accuracy": len(met) / len(constraints),
                "constraint_count": len(constraints),
                "violations": [
                    {"kind": constraint.kind.value, "instruction": constraint.as_instruction()}
                    for constraint in violated
                ],
                **{f"violations_{name}": count for name, count in counts.items()},
            },
        )
