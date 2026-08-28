"""Rung-2 shape: ``structure`` and ``json_schema``.

[Subjective Goals §3.1](../../../../../docs/apps/freeweight/subjective-goals.md)'s two structural
rows. ``structure`` checks Markdown shape — heading depth, list usage, code blocks; ``json_schema``
checks that the response validates against a schema the user supplied.

**``json_schema`` reuses the benchmark validator rather than adding a second one.**
:mod:`freeweight.domain.scorers.schema` decides a fixed keyword set and refuses everything else
([ADR-0033 §8](../../../../../docs/adr/0033-benchmark-interaction-protocol.md)). A schema using a
keyword this build does not implement is ``unsupported``, never a failure: a validator that skipped
what it could not check would report a conformance rate describing a weaker check than the one the
user asked for.

**Every structural requirement is a share, not a gate.** A criterion asking for three things and
getting two scores two thirds. The user who wants a shape to be disqualifying says so with
``gate: true``, which is the composite's business rather than this rule's.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    RuleInvalid,
    RuleResult,
    band,
    coverage,
    proportional,
)
from freeweight.domain.scorers.schema import SchemaUnsupported, extract_json, validate

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["json_schema", "structure"]

UNSUPPORTED_SCHEMA_KEYWORD = "schema_keyword_unsupported"
"""The supplied schema uses a keyword this build's bounded validator does not decide."""

UNSUPPORTED_NO_SCHEMA = "no_schema_declared"
"""The criterion supplied no schema, so there is nothing to validate against."""

_HEADING = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)\S", re.MULTILINE)
_FENCE = re.compile(r"^\s*```", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)


def structure(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score a response's Markdown shape against declared structural requirements.

    Parameters:
        ``headings``
            A band on how many headings the response has.
        ``max_heading_depth``
            The deepest heading level allowed, 1–6.
        ``list_items``
            A band on how many list items the response has.
        ``code_blocks``
            A band on how many fenced code blocks it has. ``{"max": 0}`` forbids them.
        ``tables``
            A band on how many table rows it has.

    The score is the share of the declared requirements met, with each band scored proportionally.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict, with every count in ``detail``. ``unsupported`` for an empty response, and
        for a criterion that declares no requirement at all.

    Raises:
        RuleInvalid: A bound is not a number, a band's minimum exceeds its maximum, or
            ``max_heading_depth`` is outside 1–6.
    """
    bands = {
        name: band(parameters, name)
        for name in ("headings", "list_items", "code_blocks", "tables")
        if name in parameters
    }
    depth_limit = parameters.get("max_heading_depth")
    if depth_limit is not None and (
        isinstance(depth_limit, bool)
        or not isinstance(depth_limit, int)
        or not 1 <= depth_limit <= 6  # noqa: PLR2004 — Markdown's own heading levels
    ):
        raise RuleInvalid(
            f"Rule parameter 'max_heading_depth' must be a whole number 1..6; got {depth_limit!r}."
        )
    if not bands and depth_limit is None:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_BAND)
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)

    headings = _HEADING.findall(text)
    counts = {
        "headings": len(headings),
        "list_items": len(_LIST_ITEM.findall(text)),
        "code_blocks": len(_FENCE.findall(text)) // 2,
        "tables": len(_TABLE_ROW.findall(text)),
    }
    deepest = max((len(marker) for marker in headings), default=0)

    parts = [proportional(counts[name], low, high) for name, (low, high) in sorted(bands.items())]
    if depth_limit is not None:
        parts.append(1.0 if deepest <= depth_limit else 0.0)
    return RuleResult(
        score=sum(parts) / len(parts),
        detail={
            **counts,
            "deepest_heading_level": deepest,
            "max_heading_depth": depth_limit,
            "bands": {name: list(bounds) for name, bounds in sorted(bands.items())},
        },
    )


def json_schema(text: str, parameters: Mapping[str, Any]) -> RuleResult:
    """Score whether a response validates against the criterion's JSON Schema.

    Parameters:
        ``schema``
            The schema, using only the keywords
            :data:`freeweight.domain.scorers.schema.SUPPORTED_KEYWORDS` names.

    Boolean: ``1.0`` when the response parses and conforms, ``0.0`` when it parses and does not.

    Args:
        text: The response.
        parameters: The criterion's ``rule`` block.

    Returns:
        The verdict, with the violations in ``detail``. ``unsupported`` when the criterion
        supplies no schema, when the response is empty, or when the schema uses a keyword this
        build does not decide — the last is the important one: a validator that skipped what it
        could not check would report conformance with a weaker check than the user asked for.

    Raises:
        RuleInvalid: ``schema`` is present and is not an object.
    """
    schema = parameters.get("schema")
    if schema is None:
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_NO_SCHEMA)
    if not isinstance(schema, dict):
        raise RuleInvalid(f"Rule parameter 'schema' must be a JSON object; got {schema!r}.")
    if not text.strip():
        return RuleResult(score=None, unsupported_reason=UNSUPPORTED_EMPTY_TEXT)
    document, parse_error = extract_json(text)
    if parse_error is not None:
        return RuleResult(score=0.0, detail={"parse_error": parse_error})
    try:
        violations = validate(document, schema)
    except SchemaUnsupported as exc:
        return RuleResult(
            score=None,
            detail={"schema_error": str(exc)},
            unsupported_reason=UNSUPPORTED_SCHEMA_KEYWORD,
        )
    return RuleResult(
        score=coverage([not violations]),
        detail={
            "violations": [
                {"path": violation.path, "kind": violation.kind.value, "detail": violation.detail}
                for violation in violations
            ][:10],
            "violation_count": len(violations),
        },
    )
