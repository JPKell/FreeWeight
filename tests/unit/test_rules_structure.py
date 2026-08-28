"""``structure`` and ``json_schema``: Markdown shape, and conformance to a supplied schema."""

from __future__ import annotations

import pytest

from freeweight.domain.scorers.rules import (
    UNSUPPORTED_EMPTY_TEXT,
    UNSUPPORTED_NO_BAND,
    RuleInvalid,
)
from freeweight.domain.scorers.rules.structure import (
    UNSUPPORTED_NO_SCHEMA,
    UNSUPPORTED_SCHEMA_KEYWORD,
    json_schema,
    structure,
)

_SHAPED = """# Title

Some prose about the migration.

## Detail

- one
- two
- three

```python
print("hello")
```
"""


class TestStructure:
    def test_known_pass(self) -> None:
        result = structure(_SHAPED, {"headings": {"min": 2}, "list_items": {"min": 3}})
        assert result.score == 1.0
        assert result.detail["headings"] == 2  # noqa: PLR2004 — the count is the assertion

    def test_known_fail_on_a_missing_shape(self) -> None:
        assert structure("Just prose.", {"headings": {"min": 2}}).score == 0.0

    def test_the_obvious_trip_is_a_heading_too_deep(self) -> None:
        result = structure("#### Deep\n\ntext", {"max_heading_depth": 3})
        assert result.detail["deepest_heading_level"] == 4  # noqa: PLR2004 — the level is the point
        assert result.score == 0.0

    def test_code_blocks_can_be_forbidden(self) -> None:
        assert structure(_SHAPED, {"code_blocks": {"max": 0}}).score == 0.0
        assert structure("no fences here", {"code_blocks": {"max": 0}}).score == 1.0

    def test_a_partial_shape_scores_the_share_met(self) -> None:
        result = structure("# Title\n\ntext", {"headings": {"min": 1}, "list_items": {"min": 3}})
        assert result.score == pytest.approx(0.5)

    def test_empty_input_is_unsupported(self) -> None:
        assert structure("", {"headings": {"min": 1}}).unsupported_reason == UNSUPPORTED_EMPTY_TEXT

    def test_a_criterion_asking_for_nothing_is_unsupported(self) -> None:
        assert structure(_SHAPED, {}).unsupported_reason == UNSUPPORTED_NO_BAND

    def test_unicode_headings_are_counted(self) -> None:
        assert structure("# Größe\n\ntext", {"headings": {"min": 1}}).score == 1.0

    @pytest.mark.parametrize("depth", [0, 7, "three", True])
    def test_a_bad_heading_depth_is_refused(self, depth: object) -> None:
        with pytest.raises(RuleInvalid, match="max_heading_depth"):
            structure(_SHAPED, {"max_heading_depth": depth})


class TestJsonSchema:
    _SCHEMA = {
        "type": "object",
        "required": ["name", "count"],
        "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
    }

    def test_known_pass(self) -> None:
        result = json_schema('{"name": "bay", "count": 23}', {"schema": self._SCHEMA})
        assert result.score == 1.0
        assert result.detail["violation_count"] == 0

    def test_known_fail_names_the_path(self) -> None:
        result = json_schema('{"name": "bay"}', {"schema": self._SCHEMA})
        assert result.score == 0.0
        assert result.detail["violations"][0]["path"]

    def test_the_obvious_trip_is_prose(self) -> None:
        result = json_schema("The bay is 23.", {"schema": self._SCHEMA})
        assert result.score == 0.0
        assert "parse_error" in result.detail

    def test_a_fenced_document_is_read(self) -> None:
        text = '```json\n{"name": "bay", "count": 23}\n```'
        assert json_schema(text, {"schema": self._SCHEMA}).score == 1.0

    def test_empty_input_is_unsupported(self) -> None:
        assert json_schema("", {"schema": self._SCHEMA}).unsupported_reason == (
            UNSUPPORTED_EMPTY_TEXT
        )

    def test_no_schema_is_unsupported(self) -> None:
        assert json_schema("{}", {}).unsupported_reason == UNSUPPORTED_NO_SCHEMA

    def test_an_unsupported_keyword_is_unsupported_not_a_failure(self) -> None:
        # A validator that skipped what it could not check would report conformance with a weaker
        # check than the user asked for.
        result = json_schema('{"a": 1}', {"schema": {"type": "object", "allOf": []}})
        assert result.score is None
        assert result.unsupported_reason == UNSUPPORTED_SCHEMA_KEYWORD

    def test_unicode_values_validate(self) -> None:
        assert json_schema('{"name": "Größe", "count": 1}', {"schema": self._SCHEMA}).score == 1.0

    def test_a_non_object_schema_is_refused(self) -> None:
        with pytest.raises(RuleInvalid, match="JSON object"):
            json_schema("{}", {"schema": "object"})
