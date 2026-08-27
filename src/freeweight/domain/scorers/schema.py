"""freeweight.domain.scorers.schema — rung-2 JSON and JSON Schema conformance.

Benchmark catalog §3.5's figures, each of them a count rather than an opinion: valid-JSON rate,
schema-conformance rate, required-field presence, type correctness, enum adherence and nesting
correctness.

**A bounded validator, not a JSON Schema implementation.** This module understands the keywords the
catalog names — ``type``, ``properties``, ``required``, ``additionalProperties``, ``items``,
``enum``, the numeric and length bounds — and **refuses** a schema that uses anything else, rather
than ignoring the keyword and reporting conformance it never checked. That refusal is the whole
design: a validator that silently skipped ``oneOf`` would score a wrong document as correct, which
is worse than having no figure at all. Benchmark schemas are ours, so the bound costs nothing; the
day a case needs more, this module says so out loud.

Every failure is reported as a **path** (``items[2].colour``) and a class, because the catalog's
per-class rates are counts of these, and because a failing sample has to drill down to something a
person can read.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.domain.benchmark import BenchmarkCase

__all__ = [
    "EXPECTATION_KEY",
    "JsonSchemaScorer",
    "SUPPORTED_KEYWORDS",
    "SchemaUnsupported",
    "Violation",
    "ViolationKind",
    "extract_json",
    "validate",
]

EXPECTATION_KEY = "schema"
"""The key under which a case declares the JSON Schema its answer must conform to."""

ERROR_NO_SCHEMA = "NO_EXPECTATION"
"""The case declared no schema, so conformance has no meaning."""

ERROR_SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
"""The case declared a schema keyword this validator refuses to pretend to check."""

SUPPORTED_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "description",
        "title",
        "$schema",
        "examples",
    }
)
"""Every keyword this validator decides. Anything else is refused, never ignored."""

_TYPES: Mapping[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}

_FENCE = re.compile(r"```[A-Za-z0-9_+-]*\s*\n(?P<body>.*?)\n?\s*```", re.DOTALL)
_MAXIMUM_PATTERN_LENGTH = 200


class SchemaUnsupported(ValueError):
    """The schema uses a keyword this validator does not decide.

    Raised, not swallowed. See the module docstring: a skipped keyword produces a conformance rate
    that describes a weaker check than the one it claims.
    """


class ViolationKind(StrEnum):
    """The catalog §3.5 classes a conformance failure is counted under."""

    MISSING_REQUIRED = "missing_required"
    WRONG_TYPE = "wrong_type"
    ENUM = "enum"
    NESTING = "nesting"
    ADDITIONAL = "additional"
    BOUND = "bound"
    PATTERN = "pattern"


@dataclass(frozen=True, slots=True)
class Violation:
    """One place the document disagrees with the schema.

    Attributes:
        path: A dotted/indexed path into the document, ``"$"`` for the root.
        kind: Which class of failure this is.
        detail: One sentence naming what was expected and what was there.
    """

    path: str
    kind: ViolationKind
    detail: str

    def as_json(self) -> dict[str, str]:
        """Render for storage in a sample's ``result_json``."""
        return {"path": self.path, "kind": self.kind.value, "detail": self.detail}


def extract_json(text: str) -> tuple[Any, str | None]:
    """Parse the one JSON document in ``text``.

    Args:
        text: The model's response.

    Returns:
        ``(document, None)`` on success, or ``(None, reason)`` when nothing parses. A fenced
        block is unwrapped first — the catalog measures whether the *document* conforms, and a
        model that produced correct JSON inside a fence made a formatting mistake, not a schema
        one. It is counted as such: the caller records ``fenced`` in the detail, so "valid JSON
        rate" and "obeyed the no-fence instruction" stay separable.
    """
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced is not None:
        candidate = fenced.group("body").strip()
    if not candidate:
        return None, "The response was empty."
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError as exc:
        return None, f"Not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}."


def validate(document: Any, schema: Mapping[str, Any], path: str = "$") -> tuple[Violation, ...]:  # noqa: ANN401 — any JSON value
    """Return every way ``document`` fails ``schema``.

    Args:
        document: The parsed JSON value.
        schema: The schema, using only :data:`SUPPORTED_KEYWORDS`.
        path: The path of ``document`` within the whole answer, for the violation messages.

    Returns:
        Every violation found, in document order. Empty means conformant. Validation does not stop
        at the first failure: the per-class rates need all of them, and a report that named one
        problem per attempt would make a document with four mistakes look like one with one.

    Raises:
        SchemaUnsupported: The schema uses a keyword outside :data:`SUPPORTED_KEYWORDS`, or a
            ``pattern`` longer than this module will compile.
    """
    unknown = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unknown:
        raise SchemaUnsupported(
            f"Schema at {path} uses {unknown}, which this validator does not decide; it decides "
            f"{sorted(SUPPORTED_KEYWORDS)}. Refused rather than ignored — a skipped keyword "
            "reports conformance that was never checked."
        )
    violations: list[Violation] = []
    declared_type = schema.get("type")
    if declared_type is not None and not _is_type(document, str(declared_type)):
        return (
            Violation(
                path=path,
                kind=ViolationKind.WRONG_TYPE,
                detail=f"Expected {declared_type}, got {_name_of(document)}.",
            ),
        )
    if "const" in schema and document != schema["const"]:
        violations.append(
            Violation(path, ViolationKind.ENUM, f"Expected the constant {schema['const']!r}.")
        )
    if "enum" in schema and document not in list(schema["enum"]):
        violations.append(
            Violation(path, ViolationKind.ENUM, f"{document!r} is not one of {schema['enum']!r}.")
        )
    violations.extend(_bounds(document, schema, path))
    if isinstance(document, dict):
        violations.extend(_object(document, schema, path))
    elif isinstance(document, list):
        violations.extend(_array(document, schema, path))
    return tuple(violations)


def _object(document: Mapping[str, Any], schema: Mapping[str, Any], path: str) -> list[Violation]:
    """Validate an object's required fields, its properties and its extra keys."""
    violations: list[Violation] = []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    for name in schema.get("required", ()):
        if str(name) not in document:
            violations.append(
                Violation(
                    f"{path}.{name}",
                    ViolationKind.MISSING_REQUIRED,
                    f"Required field {name!r} is absent.",
                )
            )
    if schema.get("additionalProperties") is False:
        for name in sorted(set(document) - set(properties)):
            violations.append(
                Violation(
                    f"{path}.{name}",
                    ViolationKind.ADDITIONAL,
                    f"Field {name!r} is not declared and additionalProperties is false.",
                )
            )
    for name, subschema in properties.items():
        if name in document:
            violations.extend(_nested(document[name], subschema, f"{path}.{name}"))
    return violations


def _array(document: Sequence[Any], schema: Mapping[str, Any], path: str) -> list[Violation]:
    """Validate an array's items against the single ``items`` subschema."""
    subschema = schema.get("items")
    if not isinstance(subschema, dict):
        return []
    violations: list[Violation] = []
    for index, item in enumerate(document):
        violations.extend(_nested(item, subschema, f"{path}[{index}]"))
    return violations


def _nested(value: Any, subschema: Any, path: str) -> list[Violation]:  # noqa: ANN401 — JSON values
    """Validate one nested value, re-classifying a structural failure as ``nesting``.

    A wrong type *inside* a nested object or array is a nesting failure as well as a type failure;
    the catalog counts "nesting correctness" separately, and the honest reading of that figure is
    "did the shape below the root come out right". The leaf's own class is kept in the detail, so
    nothing is lost by the re-classification.
    """
    if not isinstance(subschema, dict):
        return []
    nested = validate(value, subschema, path)
    if not isinstance(value, dict | list):
        return list(nested)
    return [
        Violation(item.path, ViolationKind.NESTING, f"{item.kind.value}: {item.detail}")
        for item in nested
    ]


def _bounds(document: Any, schema: Mapping[str, Any], path: str) -> list[Violation]:  # noqa: ANN401
    """Check the numeric, string-length and array-length bounds."""
    violations: list[Violation] = []
    checks: tuple[tuple[str, str, Any], ...] = (
        ("minimum", "at least", document if isinstance(document, int | float) else None),
        ("maximum", "at most", document if isinstance(document, int | float) else None),
        ("minLength", "at least", len(document) if isinstance(document, str) else None),
        ("maxLength", "at most", len(document) if isinstance(document, str) else None),
        ("minItems", "at least", len(document) if isinstance(document, list) else None),
        ("maxItems", "at most", len(document) if isinstance(document, list) else None),
    )
    for keyword, wording, measured in checks:
        bound = schema.get(keyword)
        if bound is None or measured is None:
            continue
        too_small = keyword.startswith("min") and measured < bound
        too_large = keyword.startswith("max") and measured > bound
        if too_small or too_large:
            violations.append(
                Violation(path, ViolationKind.BOUND, f"Expected {wording} {bound}; got {measured}.")
            )
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and isinstance(document, str):
        if len(pattern) > _MAXIMUM_PATTERN_LENGTH:
            raise SchemaUnsupported(
                f"Schema pattern at {path} is {len(pattern)} characters; the limit is "
                f"{_MAXIMUM_PATTERN_LENGTH}."
            )
        if re.search(pattern, document) is None:
            violations.append(
                Violation(path, ViolationKind.PATTERN, f"Does not match {pattern!r}.")
            )
    return violations


def _is_type(document: Any, name: str) -> bool:  # noqa: ANN401 — any JSON value
    """Return whether ``document`` is of the named JSON Schema type.

    ``bool`` is excluded from ``integer`` and ``number`` explicitly: Python makes ``True`` an
    ``int``, and a validator that inherited that would call ``{"count": true}`` a valid integer.
    Non-integral floats are excluded from ``integer`` for the same reason.
    """
    expected = _TYPES.get(name)
    if expected is None:
        return False
    if name in {"integer", "number"} and isinstance(document, bool):
        return False
    if name == "integer" and isinstance(document, float):
        return document.is_integer() and math.isfinite(document)
    return isinstance(document, expected)


def _name_of(document: Any) -> str:  # noqa: ANN401 — any JSON value
    """Return the JSON type name of a parsed value, for a violation message."""
    for name, types in _TYPES.items():
        if name in {"integer", "number"} and isinstance(document, bool):
            continue
        if isinstance(document, types):
            return "boolean" if isinstance(document, bool) else name
    return type(document).__name__


@dataclass(frozen=True, slots=True)
class JsonSchemaScorer:
    """Scores a response against the JSON Schema its case declares.

    The headline ``score`` is schema conformance: ``1.0`` when the document parses *and* has no
    violation, ``0.0`` otherwise. The component rates the catalog names live in ``detail``:
    ``valid_json``, ``required_field_presence``, ``type_correctness``, ``enum_adherence`` and
    ``nesting_correctness``, each of them a share of the checks of that class that passed rather
    than a re-statement of the headline.

    A response that is not JSON at all scores ``0.0``, not ``None``: "asked for JSON, produced
    prose" is the measurement, and it is the one this suite exists to take.
    """

    key: str = "json_schema"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Parse the response and validate it against the case's schema.

        Args:
            case: The case, carrying its schema under ``expectation["schema"]``.
            response_text: Exactly what the model returned.

        Returns:
            The verdict. ``score=None`` with a reason when the case declares no schema, or one
            using a keyword this validator refuses — both are defects in the case.
        """
        schema = case.expectation.get(EXPECTATION_KEY)
        if not isinstance(schema, dict) or not schema:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_NO_SCHEMA,
                error_text=(
                    f"Case {case.case_id!r} declares no expectation[{EXPECTATION_KEY!r}], so "
                    "there is no shape to check the answer against."
                ),
            )
        document, parse_error = extract_json(response_text)
        if parse_error is not None:
            return ScoreResult(
                score=0.0,
                method=self.method,
                detail={
                    "case": case.case_id,
                    "valid_json": 0.0,
                    "schema_conformance": 0.0,
                    "parse_error": parse_error,
                    "violations": [],
                },
            )
        try:
            violations = validate(document, schema)
        except SchemaUnsupported as exc:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_SCHEMA_UNSUPPORTED,
                error_text=str(exc),
            )
        conformance = 1.0 if not violations else 0.0
        counts = {kind: 0 for kind in ViolationKind}
        for violation in violations:
            counts[violation.kind] += 1
        return ScoreResult(
            score=conformance,
            method=self.method,
            detail={
                "case": case.case_id,
                "valid_json": 1.0,
                "schema_conformance": conformance,
                "required_field_presence": _clean(counts[ViolationKind.MISSING_REQUIRED]),
                "type_correctness": _clean(counts[ViolationKind.WRONG_TYPE]),
                "enum_adherence": _clean(counts[ViolationKind.ENUM]),
                "nesting_correctness": _clean(counts[ViolationKind.NESTING]),
                "violation_count": len(violations),
                "violations": [violation.as_json() for violation in violations],
            },
        )


def _clean(count: int) -> float:
    """Return ``1.0`` when a violation class produced no violations, ``0.0`` otherwise.

    A per-class *rate* over a single document is either one or zero; the rate the catalog names is
    the mean of these across the suite's cases, which aggregation computes. Reporting a fraction
    here — "three of five required fields present" — would let a document missing two required
    fields contribute 0.6 to a figure that is meant to say "this document had its fields".
    """
    return 1.0 if count == 0 else 0.0
