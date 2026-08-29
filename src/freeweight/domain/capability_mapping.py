"""freeweight.domain.capability_mapping — benchmark metrics → capabilities, as configuration.

[Benchmark Catalog §6](../../../../docs/apps/freeweight/benchmark-catalog.md) says how benchmark
metrics become the ``capability.evidence`` LoadCoach consumes: *weights are configuration, shipped
with defaults, versioned with the evidence*. This module is the shape of that configuration and the
arithmetic that turns one metric value into one capability score contribution. It reads no file —
the service hands it a parsed TOML body — and knows nothing about databases.

**A source is a (suite, metric, weight) triple, and a capability score is a weighted mean over the
sources that have a value.** A source whose metric is unsupported on this machine contributes
nothing and is excluded from the denominator, so an absent measurement never drags a score toward
zero (ADR-0016). A capability none of whose sources has a value has **no evidence**, which the
evidence service records as an absence, never as a score of zero.

**Normalisation is declared, never guessed.** A ratio metric maps onto the score directly, inverted
when lower is better — a false-positive *rate* of 0.1 contributes 0.9. Anything else — tokens per
second, milliseconds, bytes — has no natural ``0..1`` and must declare the value that earns full
marks (``full_score_at``) or the value at which the contribution reaches zero (``zero_score_at``).
A source that declares neither for a non-ratio metric is a configuration error and is refused when
the mapping is checked against the installed manifests, not silently scored.

Every scale is a judgement call with no empirical basis yet, exactly as ADR-0017 says of the
confidence parameters. That is why they live in a versioned file rather than in code, why the
mapping's version is part of every evidence record's ``policy_version``, and why a customised file
produces a different policy version from the shipped one.

Pure domain: stdlib and :mod:`baseaicore`, plus SetSpec's capability vocabulary for validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import ValidationError, canonical_json, sha256_of
from baseaicore import ValidationError as SuiteValidationError
from setspec.vocabulary import validate_capability

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "RATIO_UNIT",
    "CapabilityMapping",
    "MappingInvalid",
    "MetricSource",
    "normalize_value",
    "parse_mapping",
    "weighted_score",
]

RATIO_UNIT = "ratio"
"""The manifest unit whose values already live in ``0..1`` and need no declared scale."""

_MAX_SCORE = 1.0


class MappingInvalid(ValidationError):
    """The capability mapping file is malformed, names an unknown capability, or cannot score.

    Its own stable code per spec §13 rather than the generic ``VALIDATION_ERROR``: a user who
    edited ``capability_weights.toml`` needs to be told that the *mapping* is wrong, with the
    entry named, not that "a validation failed" somewhere in aggregation.
    """

    code: ClassVar[str] = "CAPABILITY_MAPPING_INVALID"


@dataclass(frozen=True, slots=True)
class MetricSource:
    """One benchmark metric's contribution to one capability.

    Attributes:
        suite_key: The benchmark suite, e.g. ``"native.tool_use"``.
        metric_key: The metric within it, spelled exactly as the manifest declares it.
        weight: This source's share of the capability score. Positive; the shares of one
            capability need not sum to one, because a capability whose sources are partly
            unsupported is scored over the sources that have a value.
        full_score_at: For a higher-is-better metric with no natural ``0..1``: the value at which
            the contribution reaches ``1.0``. Linear below it, clamped above it.
        zero_score_at: For a lower-is-better metric with no natural ``0..1``: the value at which
            the contribution reaches ``0.0``. Linear below it, clamped beyond it.
    """

    suite_key: str
    metric_key: str
    weight: float
    full_score_at: float | None = None
    zero_score_at: float | None = None

    @property
    def contributing_metric_key(self) -> str:
        """The key this source is named by in ``contributing_metrics``: ``<suite>.<metric>``.

        Suite-qualified because two suites may both declare ``task_success`` and a consumer
        reading a bare key could not tell them apart; dot-joined because that is the one
        separator SetSpec's metric-key pattern admits.
        """
        return f"{self.suite_key}.{self.metric_key}"

    def as_json(self) -> dict[str, Any]:
        """Return the source in the shape the mapping file declares it."""
        body: dict[str, Any] = {
            "suite": self.suite_key,
            "metric_key": self.metric_key,
            "weight": self.weight,
        }
        if self.full_score_at is not None:
            body["full_score_at"] = self.full_score_at
        if self.zero_score_at is not None:
            body["zero_score_at"] = self.zero_score_at
        return body


@dataclass(frozen=True, slots=True)
class CapabilityMapping:
    """Every capability this build can produce evidence for, and what feeds each one.

    Attributes:
        version: The mapping's own version, part of every evidence record's ``policy_version``.
        sources: ``{capability_id: sources}``, capabilities in declaration order.
        notes: Free text the file carried, for a reader; never hashed.
    """

    version: str
    sources: Mapping[str, tuple[MetricSource, ...]]
    notes: str = ""
    _hash: str = field(default="", repr=False, compare=False)

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Every capability the mapping scores, in declaration order."""
        return tuple(self.sources)

    def capabilities_for(self, suite_key: str) -> tuple[str, ...]:
        """Return the capabilities one suite feeds, in declaration order."""
        return tuple(
            capability
            for capability, sources in self.sources.items()
            if any(source.suite_key == suite_key for source in sources)
        )

    def source_for(
        self, capability_id: str, suite_key: str, metric_key: str
    ) -> MetricSource | None:
        """Return the source for one metric under one capability, or ``None``."""
        return next(
            (
                source
                for source in self.sources.get(capability_id, ())
                if source.suite_key == suite_key and source.metric_key == metric_key
            ),
            None,
        )

    @property
    def content_hash(self) -> str:
        """A ``sha256:``-prefixed hash of the mapping's scoring content.

        Over the version and the sources only — never the notes — so a comment edit does not
        change any evidence record's policy version while a weight edit always does.
        """
        return "sha256:" + sha256_of(canonical_json(self.as_json(include_notes=False)))

    def as_json(self, *, include_notes: bool = True) -> dict[str, Any]:
        """Return the mapping as a JSON-safe mapping, in the file's own shape."""
        body: dict[str, Any] = {
            "version": self.version,
            "capabilities": {
                capability: {"sources": [source.as_json() for source in sources]}
                for capability, sources in self.sources.items()
            },
        }
        if include_notes and self.notes:
            body["notes"] = self.notes
        return body


def _number(value: Any, *, where: str, name: str, allow_none: bool = False) -> float | None:  # noqa: ANN401 — untrusted TOML
    """Narrow one TOML value to a float, or refuse it by name."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MappingInvalid(
            f"{where}: {name} must be a number; got {value!r}.",
            details={"entry": where, "field": name, "value": value},
        )
    return float(value)


def _source(capability: str, index: int, entry: Any) -> MetricSource:  # noqa: ANN401 — untrusted TOML
    """Parse one ``sources`` entry, refusing anything that could not score a metric."""
    where = f"capabilities.{capability}.sources[{index}]"
    if not isinstance(entry, dict):
        raise MappingInvalid(
            f"{where}: a source is a table with suite, metric_key and weight; got {entry!r}.",
            details={"entry": where},
        )
    suite = entry.get("suite")
    metric_key = entry.get("metric_key")
    if not isinstance(suite, str) or not suite:
        raise MappingInvalid(
            f"{where}: suite must be a non-empty string.", details={"entry": where}
        )
    if not isinstance(metric_key, str) or not metric_key:
        raise MappingInvalid(
            f"{where}: metric_key must be a non-empty string.", details={"entry": where}
        )
    weight = _number(entry.get("weight"), where=where, name="weight")
    if weight is None or weight <= 0:
        raise MappingInvalid(
            f"{where}: weight must be positive; got {weight!r}. A zero or negative weight would "
            "mean the metric contributed nothing or inverted another's effect.",
            details={"entry": where, "weight": weight},
        )
    full = _number(entry.get("full_score_at"), where=where, name="full_score_at", allow_none=True)
    zero = _number(entry.get("zero_score_at"), where=where, name="zero_score_at", allow_none=True)
    if full is not None and zero is not None:
        raise MappingInvalid(
            f"{where}: declare full_score_at or zero_score_at, not both; a metric has one "
            "direction.",
            details={"entry": where},
        )
    for name, value in (("full_score_at", full), ("zero_score_at", zero)):
        if value is not None and value <= 0:
            raise MappingInvalid(
                f"{where}: {name} must be positive; got {value}.",
                details={"entry": where, "field": name, "value": value},
            )
    unknown = sorted(
        set(entry) - {"suite", "metric_key", "weight", "full_score_at", "zero_score_at"}
    )
    if unknown:
        raise MappingInvalid(
            f"{where}: unknown field(s) {unknown}.", details={"entry": where, "unknown": unknown}
        )
    return MetricSource(
        suite_key=suite,
        metric_key=metric_key,
        weight=weight,
        full_score_at=full,
        zero_score_at=zero,
    )


def parse_mapping(body: Mapping[str, Any]) -> CapabilityMapping:
    """Parse a capability mapping from a decoded TOML body.

    The file's shape::

        version = "1.0"

        [capabilities.tool_use]
        sources = [
          { suite = "native.tool_use", metric_key = "task_success", weight = 0.4 },
        ]

    Args:
        body: The decoded document.

    Returns:
        The mapping.

    Raises:
        MappingInvalid: The version is missing; a capability is not in SetSpec's vocabulary or is
            in the reserved ``user`` namespace — goals map themselves, and a shipped file that
            named one would make a user's rubric look like configuration; a source is malformed;
            or a capability declares no sources.
    """
    version = body.get("version")
    if not isinstance(version, str) or not version:
        raise MappingInvalid(
            "A capability mapping must declare a non-empty string `version`; it is part of "
            "every evidence record's policy_version.",
            details={"field": "version"},
        )
    capabilities = body.get("capabilities")
    if not isinstance(capabilities, dict) or not capabilities:
        raise MappingInvalid(
            "A capability mapping must declare at least one [capabilities.<id>] table.",
            details={"field": "capabilities"},
        )
    sources: dict[str, tuple[MetricSource, ...]] = {}
    for capability, block in capabilities.items():
        try:
            parsed = validate_capability(capability)
        except SuiteValidationError as exc:
            raise MappingInvalid(
                f"capabilities.{capability}: not a capability in SetSpec's vocabulary "
                f"({exc.message}).",
                details={"capability": capability},
            ) from exc
        if parsed.root == "user":
            raise MappingInvalid(
                f"capabilities.{capability}: the `user` namespace is reserved for goals, which "
                "map themselves (ADR-0032 §1). A mapping file cannot declare sources for one.",
                details={"capability": capability},
            )
        entries = block.get("sources") if isinstance(block, dict) else None
        if not isinstance(entries, list) or not entries:
            raise MappingInvalid(
                f"capabilities.{capability}: declares no sources. A capability with nothing "
                "feeding it should be absent from the file, not present and empty.",
                details={"capability": capability},
            )
        parsed_sources = tuple(
            _source(capability, index, entry) for index, entry in enumerate(entries)
        )
        seen: set[tuple[str, str]] = set()
        for source in parsed_sources:
            pair = (source.suite_key, source.metric_key)
            if pair in seen:
                raise MappingInvalid(
                    f"capabilities.{capability}: {source.contributing_metric_key} is listed "
                    "twice; one metric contributes to one capability once.",
                    details={"capability": capability, "metric": source.contributing_metric_key},
                )
            seen.add(pair)
        sources[capability] = parsed_sources
    notes = body.get("notes")
    return CapabilityMapping(
        version=version, sources=sources, notes=str(notes) if isinstance(notes, str) else ""
    )


def normalize_value(
    value: float, *, unit: str, higher_is_better: bool, source: MetricSource
) -> float | None:
    """Turn one measured metric value into a ``0..1`` capability contribution.

    Args:
        value: The measured value. A supported number; an unsupported measurement never reaches
            here, because it contributes nothing rather than zero.
        unit: The metric's unit as the manifest declares it.
        higher_is_better: The metric's declared direction.
        source: The source, whose declared scale is used for a non-ratio metric.

    Returns:
        The contribution in ``[0, 1]``, or ``None`` when the metric has no natural range and the
        source declared no scale — a configuration gap the caller reports rather than scores.
    """
    if source.full_score_at is not None:
        return min(_MAX_SCORE, max(0.0, value / source.full_score_at))
    if source.zero_score_at is not None:
        return min(_MAX_SCORE, max(0.0, 1.0 - value / source.zero_score_at))
    if unit != RATIO_UNIT:
        return None
    clamped = min(_MAX_SCORE, max(0.0, value))
    return clamped if higher_is_better else _MAX_SCORE - clamped


def weighted_score(contributions: Sequence[tuple[float, float]]) -> float | None:
    """``Σ(weight × contribution) / Σ(weight)`` over the sources that have a value.

    Args:
        contributions: ``(weight, contribution)`` pairs for the sources with a value.

    Returns:
        The score in ``[0, 1]``, or ``None`` when nothing contributed — an absence, never zero.
    """
    total = sum(weight for weight, _ in contributions)
    if total <= 0:
        return None
    return min(_MAX_SCORE, max(0.0, sum(weight * value for weight, value in contributions) / total))
