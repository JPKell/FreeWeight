"""The JSON Schema scorer and its bounded validator.

Benchmark catalog §3.5's figures, plus the one property that makes them trustworthy: the validator
**refuses** a keyword it does not decide rather than skipping it. A validator that ignored
``oneOf`` would report a conformance rate for a check it never performed, which is worse than
reporting no rate at all.
"""

from __future__ import annotations

import pytest

from freeweight.domain.benchmark import BenchmarkCase
from freeweight.domain.scorers.schema import (
    JsonSchemaScorer,
    SchemaUnsupported,
    ViolationKind,
    extract_json,
    validate,
)

_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "sku": {"type": "string", "minLength": 2},
        "units": {"type": "integer", "minimum": 0},
        "status": {"type": "string", "enum": ["active", "dormant"]},
        "lines": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {"sku": {"type": "string"}, "units": {"type": "integer"}},
                "required": ["sku", "units"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sku", "units"],
    "additionalProperties": False,
}

_CONFORMING = '{"sku": "A1", "units": 3, "status": "active", "lines": [{"sku": "A1", "units": 3}]}'


def _case(schema: object = _SCHEMA) -> BenchmarkCase:
    return BenchmarkCase(case_id="case-1", ordinal=0, prompt="p", expectation={"schema": schema})


class TestKnownPassAndFail:
    """The document either conforms or it does not, and the classes say how it failed."""

    def test_a_conforming_document_scores_one(self) -> None:
        verdict = JsonSchemaScorer().score(_case(), _CONFORMING)
        assert verdict.score == 1.0
        assert verdict.detail["valid_json"] == 1.0
        assert verdict.detail["violations"] == []

    @pytest.mark.parametrize(
        ("document", "kind"),
        [
            ('{"units": 3}', ViolationKind.MISSING_REQUIRED),
            ('{"sku": "A1", "units": "three"}', ViolationKind.WRONG_TYPE),
            ('{"sku": "A1", "units": 3, "status": "asleep"}', ViolationKind.ENUM),
            ('{"sku": "A1", "units": 3, "colour": "red"}', ViolationKind.ADDITIONAL),
            ('{"sku": "A", "units": 3}', ViolationKind.BOUND),
            ('{"sku": "A1", "units": 3, "lines": [{"sku": "A1"}]}', ViolationKind.NESTING),
        ],
    )
    def test_each_failure_class_is_reported(self, document: str, kind: ViolationKind) -> None:
        verdict = JsonSchemaScorer().score(_case(), document)
        assert verdict.score == 0.0
        assert kind.value in {item["kind"] for item in verdict.detail["violations"]}

    def test_every_violation_is_reported_not_only_the_first(self) -> None:
        # The per-class rates need all of them; stopping at the first would make a document with
        # four mistakes indistinguishable from one with one.
        verdict = JsonSchemaScorer().score(_case(), '{"colour": "red"}')
        assert verdict.detail["violation_count"] == 3


class TestBoundary:
    """Types and bounds at their edges, where a naive validator gets Python's booleans wrong."""

    def test_a_boolean_is_not_an_integer(self) -> None:
        assert validate(True, {"type": "integer"})[0].kind is ViolationKind.WRONG_TYPE

    def test_a_whole_float_satisfies_integer_and_a_fractional_one_does_not(self) -> None:
        assert validate(3.0, {"type": "integer"}) == ()
        assert validate(3.5, {"type": "integer"})[0].kind is ViolationKind.WRONG_TYPE

    def test_an_inclusive_bound_is_inclusive(self) -> None:
        assert validate(0, {"type": "integer", "minimum": 0}) == ()
        assert validate(-1, {"type": "integer", "minimum": 0})[0].kind is ViolationKind.BOUND

    def test_an_empty_array_fails_min_items(self) -> None:
        assert validate([], {"type": "array", "minItems": 1})[0].kind is ViolationKind.BOUND


class TestMalformedAndMissing:
    """Prose scores zero; an unusable case scores nothing at all."""

    def test_prose_is_a_measurement_not_an_absence(self) -> None:
        verdict = JsonSchemaScorer().score(_case(), "Here is the record you asked for.")
        assert verdict.score == 0.0
        assert verdict.detail["valid_json"] == 0.0
        assert "Not valid JSON" in verdict.detail["parse_error"]

    def test_a_fenced_document_still_parses(self) -> None:
        # A fence is a formatting mistake, not a schema one. It is unwrapped so the conformance
        # figure measures the document; whether the fence was asked for is the instruction-
        # following suite's question, not this one's.
        assert JsonSchemaScorer().score(_case(), f"```json\n{_CONFORMING}\n```").score == 1.0

    def test_an_empty_response_scores_zero(self) -> None:
        assert JsonSchemaScorer().score(_case(), "   ").score == 0.0

    def test_a_case_with_no_schema_is_unscoreable(self) -> None:
        case = BenchmarkCase(case_id="c", ordinal=0, prompt="p")
        verdict = JsonSchemaScorer().score(case, _CONFORMING)
        assert verdict.score is None
        assert verdict.error_code == "NO_EXPECTATION"

    def test_an_unsupported_keyword_is_refused_rather_than_ignored(self) -> None:
        verdict = JsonSchemaScorer().score(_case({"oneOf": [{"type": "string"}]}), '"x"')
        assert verdict.score is None
        assert verdict.error_code == "SCHEMA_UNSUPPORTED"
        assert "oneOf" in (verdict.error_text or "")

    def test_the_validator_raises_on_a_keyword_it_does_not_decide(self) -> None:
        with pytest.raises(SchemaUnsupported, match="allOf"):
            validate({}, {"type": "object", "allOf": []})

    def test_an_over_long_pattern_is_refused(self) -> None:
        with pytest.raises(SchemaUnsupported, match="limit"):
            validate("x", {"type": "string", "pattern": "a" * 201})

    def test_extract_json_reports_why_it_failed(self) -> None:
        document, reason = extract_json("{not json")
        assert document is None
        assert reason is not None and "line 1" in reason
