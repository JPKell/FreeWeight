"""The capability mapping: parsing, validation, normalisation, and the shipped file itself."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from freeweight.domain.capability_mapping import (
    MappingInvalid,
    MetricSource,
    normalize_value,
    parse_mapping,
    weighted_score,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED = REPO_ROOT / "src" / "freeweight" / "config" / "capability_weights.toml"
MANIFESTS = REPO_ROOT / "src" / "freeweight" / "benchmarks"


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "version": "1.0",
        "capabilities": {
            "tool_use": {
                "sources": [
                    {"suite": "native.tool_use", "metric_key": "task_success", "weight": 0.6},
                    {"suite": "native.tool_use", "metric_key": "missed_tool_rate", "weight": 0.4},
                ]
            }
        },
    }
    body.update(changes)
    return body


class TestParsing:
    def test_a_well_formed_file_parses(self) -> None:
        mapping = parse_mapping(_body())
        assert mapping.version == "1.0"
        assert mapping.capabilities == ("tool_use",)
        assert (
            mapping.sources["tool_use"][0].contributing_metric_key == "native.tool_use.task_success"
        )
        assert mapping.capabilities_for("native.tool_use") == ("tool_use",)
        assert mapping.capabilities_for("native.echo") == ()

    def test_a_missing_version_is_refused(self) -> None:
        body = _body()
        del body["version"]
        with pytest.raises(MappingInvalid, match="version"):
            parse_mapping(body)

    def test_an_unknown_capability_is_refused(self) -> None:
        body = _body(
            capabilities={"vibes": {"sources": [{"suite": "s", "metric_key": "m", "weight": 1}]}}
        )
        with pytest.raises(MappingInvalid, match="vocabulary"):
            parse_mapping(body)

    def test_a_specialization_of_a_known_root_is_accepted(self) -> None:
        body = _body(
            capabilities={
                "coding.python": {"sources": [{"suite": "s", "metric_key": "m", "weight": 1}]}
            }
        )
        assert parse_mapping(body).capabilities == ("coding.python",)

    def test_the_user_namespace_is_refused(self) -> None:
        """Goals map themselves; a shipped file naming one would make a rubric look like config."""
        body = _body(
            capabilities={
                "user.house_voice": {"sources": [{"suite": "s", "metric_key": "m", "weight": 1}]}
            }
        )
        with pytest.raises(MappingInvalid, match="reserved"):
            parse_mapping(body)

    def test_a_zero_weight_is_refused(self) -> None:
        body = _body(
            capabilities={"tool_use": {"sources": [{"suite": "s", "metric_key": "m", "weight": 0}]}}
        )
        with pytest.raises(MappingInvalid, match="positive"):
            parse_mapping(body)

    def test_both_scales_at_once_are_refused(self) -> None:
        body = _body(
            capabilities={
                "speed": {
                    "sources": [
                        {
                            "suite": "s",
                            "metric_key": "m",
                            "weight": 1,
                            "full_score_at": 1,
                            "zero_score_at": 2,
                        }
                    ]
                }
            }
        )
        with pytest.raises(MappingInvalid, match="not both"):
            parse_mapping(body)

    def test_a_duplicate_source_is_refused(self) -> None:
        body = _body(
            capabilities={
                "tool_use": {
                    "sources": [
                        {"suite": "s", "metric_key": "m", "weight": 1},
                        {"suite": "s", "metric_key": "m", "weight": 2},
                    ]
                }
            }
        )
        with pytest.raises(MappingInvalid, match="twice"):
            parse_mapping(body)

    def test_an_unknown_source_field_is_refused(self) -> None:
        body = _body(
            capabilities={
                "tool_use": {
                    "sources": [{"suite": "s", "metric_key": "m", "weight": 1, "wight": 2}]
                }
            }
        )
        with pytest.raises(MappingInvalid, match="unknown field"):
            parse_mapping(body)

    def test_an_empty_capability_is_refused(self) -> None:
        with pytest.raises(MappingInvalid, match="no sources"):
            parse_mapping(_body(capabilities={"tool_use": {"sources": []}}))

    def test_the_content_hash_ignores_notes_and_tracks_weights(self) -> None:
        plain = parse_mapping(_body())
        noted = parse_mapping(_body(notes="a comment"))
        assert plain.content_hash == noted.content_hash
        reweighted = _body()
        reweighted["capabilities"]["tool_use"]["sources"][0]["weight"] = 0.7
        assert parse_mapping(reweighted).content_hash != plain.content_hash


class TestNormalisation:
    def test_a_ratio_maps_directly(self) -> None:
        source = MetricSource("s", "m", 1.0)
        assert normalize_value(0.8, unit="ratio", higher_is_better=True, source=source) == 0.8

    def test_a_lower_is_better_ratio_is_inverted(self) -> None:
        source = MetricSource("s", "m", 1.0)
        assert normalize_value(
            0.1, unit="ratio", higher_is_better=False, source=source
        ) == pytest.approx(0.9)

    def test_a_ratio_is_clamped(self) -> None:
        source = MetricSource("s", "m", 1.0)
        assert normalize_value(1.4, unit="ratio", higher_is_better=True, source=source) == 1.0
        assert normalize_value(-0.2, unit="ratio", higher_is_better=True, source=source) == 0.0

    def test_full_score_at_is_linear_below_and_clamped_above(self) -> None:
        source = MetricSource("s", "m", 1.0, full_score_at=100.0)
        assert normalize_value(50.0, unit="tokens/s", higher_is_better=True, source=source) == 0.5
        assert normalize_value(250.0, unit="tokens/s", higher_is_better=True, source=source) == 1.0

    def test_zero_score_at_is_linear_and_clamped(self) -> None:
        source = MetricSource("s", "m", 1.0, zero_score_at=2000.0)
        assert normalize_value(500.0, unit="ms", higher_is_better=False, source=source) == 0.75
        assert normalize_value(5000.0, unit="ms", higher_is_better=False, source=source) == 0.0

    def test_a_declared_scale_wins_over_the_ratio_rule(self) -> None:
        source = MetricSource("s", "m", 1.0, zero_score_at=3.0)
        assert normalize_value(
            1.0, unit="ratio", higher_is_better=False, source=source
        ) == pytest.approx(2 / 3)

    def test_a_non_ratio_without_a_scale_cannot_be_scored(self) -> None:
        source = MetricSource("s", "m", 1.0)
        assert normalize_value(42.0, unit="ms", higher_is_better=False, source=source) is None

    def test_the_weighted_score_ignores_absent_sources(self) -> None:
        assert weighted_score([(0.6, 1.0), (0.4, 0.5)]) == pytest.approx(0.8)
        assert weighted_score([(0.6, 1.0)]) == 1.0
        assert weighted_score([]) is None


@pytest.fixture(scope="module")
def manifests() -> dict[str, dict[str, dict[str, Any]]]:
    loaded: dict[str, dict[str, dict[str, Any]]] = {}
    for path in MANIFESTS.glob("*/manifest.json"):
        body = json.loads(path.read_text(encoding="utf-8"))
        loaded[body["key"]] = {m["metric_key"]: m for m in body.get("metrics", ())}
    return loaded


class TestTheShippedMapping:
    """The file the build ships, checked against the manifests it names."""

    def test_it_parses(self) -> None:
        mapping = parse_mapping(tomllib.loads(SHIPPED.read_text(encoding="utf-8")))
        assert mapping.version == "1.0"
        assert len(mapping.capabilities) >= 10

    def test_every_source_names_a_metric_a_shipped_suite_declares(
        self, manifests: dict[str, dict[str, dict[str, Any]]]
    ) -> None:
        mapping = parse_mapping(tomllib.loads(SHIPPED.read_text(encoding="utf-8")))
        missing = [
            f"{capability}: {source.contributing_metric_key}"
            for capability, sources in mapping.sources.items()
            for source in sources
            if source.metric_key not in manifests.get(source.suite_key, {})
        ]
        assert not missing, missing

    def test_every_source_can_be_scored(
        self, manifests: dict[str, dict[str, dict[str, Any]]]
    ) -> None:
        """A non-ratio metric with no declared scale would be skipped at aggregation time."""
        mapping = parse_mapping(tomllib.loads(SHIPPED.read_text(encoding="utf-8")))
        unscorable = []
        for capability, sources in mapping.sources.items():
            for source in sources:
                metric = manifests[source.suite_key][source.metric_key]
                if (
                    normalize_value(
                        1.0,
                        unit=metric["unit"],
                        higher_is_better=metric["higher_is_better"],
                        source=source,
                    )
                    is None
                ):
                    unscorable.append(f"{capability}: {source.contributing_metric_key}")
        assert not unscorable, unscorable

    def test_the_goal_fed_capabilities_have_no_shipped_source(self) -> None:
        """summarization and creative_writing are the user's to measure (catalog §6)."""
        mapping = parse_mapping(tomllib.loads(SHIPPED.read_text(encoding="utf-8")))
        assert "summarization" not in mapping.capabilities
        assert "creative_writing" not in mapping.capabilities
        assert not any(capability.startswith("user.") for capability in mapping.capabilities)
