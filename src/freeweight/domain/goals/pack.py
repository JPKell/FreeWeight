"""freeweight.domain.goals.pack — the shape of a goal, parsed and structurally validated.

[Subjective Goals §2](../../../../../docs/apps/freeweight/subjective-goals.md) describes a
directory of JSON: ``goal.json`` for identity, criteria, weights, judge configuration and the
calibration policy; ``tasks/`` for the prompts a candidate answers; ``calibration/`` for the
samples and the user's grades. This module parses the first of those and holds the result.

**Two kinds of refusal, and they happen in two places.** *Shape* errors — a missing slug, a rung
this build does not know, a weight that is not a number — raise :class:`GoalPackInvalid` here,
because a pack that cannot be read cannot be linted either. *Semantic* problems — weights that do
not sum to one, a judged criterion with no descriptors, a criterion a rule could check — are
findings from :mod:`freeweight.domain.goals.lint`, which produces all of them at once with a
severity each. ``freeweight goals validate`` on a deliberately bad pack must name **every**
problem, and an exception on the first one would name exactly one.

**A slug is an identity, not a label.** It becomes ``user.<slug>`` in the capability vocabulary
([ADR-0032 §1](../../../../../docs/adr/0032-judge-validity-and-user-capability-namespace.md)), it
is the directory name on disk, and it is never renamed — a rename is a new goal. So it is
pattern-checked before anything touches the filesystem (security standards §4) and refused when it
would collide with a shipped capability root.

Pure domain: stdlib and :mod:`setspec`'s capability vocabulary, which is a contract rather than a
framework.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "CAPABILITY_PREFIX",
    "SCHEMA_VERSION",
    "SLUG_PATTERN",
    "CalibrationConfig",
    "Criterion",
    "GoalPack",
    "GoalPackInvalid",
    "GoalTask",
    "JudgeConfig",
    "Rung",
    "ScaleSpec",
    "parse_pack",
]

SCHEMA_VERSION = "1.0"
"""The ``goal.json`` record format this build speaks."""

CAPABILITY_PREFIX = "user"
"""The reserved SetSpec capability root every goal is emitted under (ADR-0032 §1)."""

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
"""What a slug may be.

Lowercase, underscore-separated, starting with a letter, at most 64 characters. It is a directory
name, a capability specialization and a URL path segment, and the allowlist is checked before any
of the three is built from it (security standards §4)."""

_SCALE_POINTS = (3, 5, 7)
_REQUIRED_DESCRIPTOR_COUNT = 3
_WEIGHT_SUM_TOLERANCE = 1e-6


class GoalPackInvalid(ValidationError):
    """A goal pack this build refuses to read.

    Attributes:
        code: ``"GOAL_INVALID"``, the stable code spec §13 names.
    """

    code: ClassVar[str] = "GOAL_INVALID"


class Rung(StrEnum):
    """Which rung of the scoring ladder a criterion is scored at (benchmark catalog §1).

    Recorded per criterion because it is what makes ``score_method_mix`` computable, and therefore
    what lets a reader tell a score that is mostly rules from one that is mostly judgement. Those
    are different kinds of number and presenting them identically is the failure the field exists
    to prevent.
    """

    RULE = "rule"
    REFERENCE = "reference"
    HUMAN = "human"
    JUDGE = "judge"

    @property
    def is_deterministic(self) -> bool:
        """Whether this rung is decided without a person or a model."""
        return self in {Rung.RULE, Rung.REFERENCE}


@dataclass(frozen=True, slots=True)
class ScaleSpec:
    """An ordinal scale and the descriptors that anchor it.

    Attributes:
        points: 3, 5 or 7. An even scale is refused: it removes the midpoint, which is where a
            grader puts "this is fine", and forces a preference the grader does not have.
        descriptors: Grade point (as a string) to the sentence describing it. At least the top,
            middle and bottom must be described — an unanchored scale reliably produces agreement
            near zero, so it is refused at authoring time rather than discovered after twelve
            samples have been graded (ADR-0031 §3).
    """

    points: int
    descriptors: Mapping[str, str] = field(default_factory=dict)

    @property
    def anchored(self) -> bool:
        """Whether the scale carries enough descriptors to be gradeable."""
        return len(self.descriptors) >= _REQUIRED_DESCRIPTOR_COUNT

    @classmethod
    def from_json(cls, body: Mapping[str, Any], *, criterion_key: str) -> ScaleSpec:
        """Parse one criterion's ``scale`` block.

        Args:
            body: The block.
            criterion_key: The criterion it belongs to, for the message.

        Returns:
            The scale.

        Raises:
            GoalPackInvalid: ``points`` is missing or is not 3, 5 or 7, or ``descriptors`` is not
                an object of strings.
        """
        points = body.get("points")
        if points not in _SCALE_POINTS:
            raise GoalPackInvalid(
                f"Criterion {criterion_key!r} declares scale.points {points!r}; an ordinal scale "
                f"here is one of {list(_SCALE_POINTS)}. An even scale removes the midpoint, "
                "which is where a grader puts 'this is fine'.",
                details={"criterion": criterion_key, "points": points},
            )
        raw = body.get("descriptors", {})
        if not isinstance(raw, dict):
            raise GoalPackInvalid(
                f"Criterion {criterion_key!r} declares a non-object scale.descriptors.",
                details={"criterion": criterion_key},
            )
        return cls(points=int(points), descriptors={str(k): str(v) for k, v in raw.items()})


@dataclass(frozen=True, slots=True)
class Criterion:
    """One measurable quality within a goal, and how it is scored.

    Attributes:
        key: Stable within the goal, never renamed. A renamed criterion whose history merged with
            the old one would silently compare two different measurements.
        name: The display label. **Not** a ``goal_hash`` input: renaming a criterion for
            readability must not separate a year of results.
        rung: Which ladder rung scores it.
        weight: Its share of the composite.
        is_gate: Whether failing it zeroes the sample's composite outright.
        rule: The rule or reference parameters, with ``type`` naming which check runs. ``None``
            for a human or judged criterion.
        scale: The ordinal scale, for a human or judged criterion. ``None`` otherwise.
        mode: ``"absolute"`` or ``"pairwise"`` for a judged criterion; ``None`` otherwise.
        intent: The author's note about what they meant. Display only, never hashed.
    """

    key: str
    name: str
    rung: Rung
    weight: float
    is_gate: bool = False
    rule: Mapping[str, Any] | None = None
    scale: ScaleSpec | None = None
    mode: str | None = None
    intent: str = ""

    @property
    def rule_type(self) -> str | None:
        """The check this criterion runs, or ``None`` for a graded criterion."""
        return None if self.rule is None else str(self.rule.get("type", ""))

    @property
    def rule_parameters(self) -> Mapping[str, Any]:
        """The rule's parameters, ``type`` excluded."""
        if self.rule is None:
            return {}
        return {key: value for key, value in self.rule.items() if key != "type"}

    @classmethod
    def from_json(cls, body: Mapping[str, Any], *, ordinal: int) -> Criterion:
        """Parse one criterion declaration.

        Args:
            body: The declaration.
            ordinal: Its position, for the message when it has no key to name it by.

        Returns:
            The criterion.

        Raises:
            GoalPackInvalid: ``key`` or ``name`` is missing, ``rung`` is not a known rung,
                ``weight`` is not a number in ``0 < w <= 1``, ``rule`` is present but is not an
                object with a ``type``, or ``mode`` is neither ``absolute`` nor ``pairwise``.
        """
        key = str(body.get("key", ""))
        if not key:
            raise GoalPackInvalid(
                f"Criterion {ordinal} declares no 'key'; a criterion key identifies a measurement "
                "over time, so a goal cannot have an anonymous one.",
                details={"ordinal": ordinal},
            )
        raw_rung = str(body.get("rung", ""))
        try:
            rung = Rung(raw_rung)
        except ValueError as exc:
            raise GoalPackInvalid(
                f"Criterion {key!r} declares rung {raw_rung!r}; the ladder's rungs here are "
                f"{[member.value for member in Rung]}.",
                details={"criterion": key, "rung": raw_rung},
            ) from exc
        weight = body.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, int | float):
            raise GoalPackInvalid(
                f"Criterion {key!r} declares weight {weight!r}, which is not a number.",
                details={"criterion": key},
            )
        if not 0.0 < float(weight) <= 1.0:
            raise GoalPackInvalid(
                f"Criterion {key!r} declares weight {weight}; a weight is a share of the "
                "composite and must be above 0 and at most 1.",
                details={"criterion": key, "weight": weight},
            )
        rule = body.get("rule")
        if rule is not None:
            if not isinstance(rule, dict) or not str(rule.get("type", "")):
                raise GoalPackInvalid(
                    f"Criterion {key!r} declares a 'rule' with no 'type'; a deterministic "
                    "criterion is defined by the check it runs.",
                    details={"criterion": key},
                )
        scale_body = body.get("scale")
        scale = (
            ScaleSpec.from_json(scale_body, criterion_key=key)
            if isinstance(scale_body, dict)
            else None
        )
        mode = body.get("mode")
        if mode is not None and str(mode) not in {"absolute", "pairwise"}:
            raise GoalPackInvalid(
                f"Criterion {key!r} declares mode {mode!r}; a judged criterion is scored "
                "'absolute' or 'pairwise'.",
                details={"criterion": key, "mode": mode},
            )
        return cls(
            key=key,
            name=str(body.get("name", key)),
            rung=rung,
            weight=float(weight),
            is_gate=bool(body.get("gate", False)),
            rule=dict(rule) if isinstance(rule, dict) else None,
            scale=scale,
            mode=None if mode is None else str(mode),
            intent=str(body.get("intent", "")),
        )


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    """The jury a goal's judged criteria are scored by.

    Every field here is a ``goal_hash`` input: a different jury is a different instrument, and a
    different instrument is a different measurement (ADR-0032 §4).

    Attributes:
        jury_size: How many distinct models score each case. ``1`` disables the jury and loses
            inter-juror agreement, which the result then says.
        models: The jury, by canonical model ID. Empty means auto-selection from what is
            installed.
        repetitions: How many times each juror scores each case.
        allow_remote: The goal's half of the remote opt-in. ``providers.allow_remote`` is the
            other half, and both are required.
        prompt_id: The judge prompt record.
        prompt_version: That record's version.
    """

    jury_size: int = 3
    models: tuple[str, ...] = ()
    repetitions: int = 3
    allow_remote: bool = False
    prompt_id: str = "goals.judge.rubric"
    prompt_version: str = "1.0.0"

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> JudgeConfig:
        """Parse a ``judge`` block.

        Raises:
            GoalPackInvalid: ``jury_size`` or ``repetitions`` is not a positive whole number, or
                ``models`` is not a list of strings.
        """
        defaults = cls()
        for name in ("jury_size", "repetitions"):
            value = body.get(name, getattr(defaults, name))
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GoalPackInvalid(
                    f"judge.{name} must be a positive whole number; got {value!r}.",
                    details={"field": f"judge.{name}", "value": value},
                )
        models = body.get("models", ())
        if isinstance(models, str) or not isinstance(models, list | tuple):
            raise GoalPackInvalid(
                f"judge.models must be a list of canonical model IDs; got {models!r}.",
                details={"field": "judge.models"},
            )
        return cls(
            jury_size=int(body.get("jury_size", defaults.jury_size)),
            models=tuple(str(item) for item in models),
            repetitions=int(body.get("repetitions", defaults.repetitions)),
            allow_remote=bool(body.get("allow_remote", False)),
            prompt_id=str(body.get("prompt_id", defaults.prompt_id)),
            prompt_version=str(body.get("prompt_version", defaults.prompt_version)),
        )

    def as_json(self) -> dict[str, Any]:
        """Return the block as stored on ``goals.judge_config_json``."""
        return {
            "jury_size": self.jury_size,
            "models": list(self.models),
            "repetitions": self.repetitions,
            "allow_remote": self.allow_remote,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
        }


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    """How a goal's judged criteria are calibrated against the author's own grades.

    **Not** a ``goal_hash`` input. These are policy parameters, recorded on the calibration report
    with the policy version exactly as ADR-0017's own parameters are: re-grading the same rubric
    refines the instrument's characterization without changing what is being measured
    (ADR-0032 §4).

    Attributes:
        target_samples: How many samples the wizard asks for.
        min_samples: Below this, ``CALIBRATION_INSUFFICIENT`` — the user has not yet done the
            work, which is a different state from having done it and learned the rubric is not
            measurable.
        holdout_fraction: The share withheld from the jury.
        partition_seed: The seed the split is reproducible from.
        min_agreement: The weighted ``kappa_w`` the gate compares.
    """

    target_samples: int = 12
    min_samples: int = 8
    holdout_fraction: float = 0.4
    partition_seed: int = 0
    min_agreement: float = 0.40

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> CalibrationConfig:
        """Parse a ``calibration`` block.

        Raises:
            GoalPackInvalid: A count is not a positive whole number, ``min_samples`` exceeds
                ``target_samples``, ``holdout_fraction`` is not strictly between 0 and 1, or
                ``min_agreement`` is outside ``-1..1``.
        """
        defaults = cls()
        counts: dict[str, int] = {}
        for name in ("target_samples", "min_samples"):
            value = body.get(name, getattr(defaults, name))
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GoalPackInvalid(
                    f"calibration.{name} must be a positive whole number; got {value!r}.",
                    details={"field": f"calibration.{name}", "value": value},
                )
            counts[name] = int(value)
        if counts["min_samples"] > counts["target_samples"]:
            raise GoalPackInvalid(
                f"calibration.min_samples ({counts['min_samples']}) is above "
                f"calibration.target_samples ({counts['target_samples']}); the target is what the "
                "wizard asks for and the minimum is what it will accept.",
                details={"field": "calibration.min_samples"},
            )
        fraction = body.get("holdout_fraction", defaults.holdout_fraction)
        if isinstance(fraction, bool) or not isinstance(fraction, int | float):
            raise GoalPackInvalid(
                f"calibration.holdout_fraction must be a number; got {fraction!r}.",
                details={"field": "calibration.holdout_fraction"},
            )
        if not 0.0 < float(fraction) < 1.0:
            raise GoalPackInvalid(
                f"calibration.holdout_fraction must be above 0 and below 1; got {fraction}. A "
                "holdout of everything leaves no anchors, and one of nothing leaves no honest "
                "estimate of agreement.",
                details={"field": "calibration.holdout_fraction", "value": fraction},
            )
        seed = body.get("partition_seed", defaults.partition_seed)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise GoalPackInvalid(
                f"calibration.partition_seed must be a whole number; got {seed!r}.",
                details={"field": "calibration.partition_seed"},
            )
        agreement = body.get("min_agreement", defaults.min_agreement)
        if isinstance(agreement, bool) or not isinstance(agreement, int | float):
            raise GoalPackInvalid(
                f"calibration.min_agreement must be a number; got {agreement!r}.",
                details={"field": "calibration.min_agreement"},
            )
        if not -1.0 <= float(agreement) <= 1.0:
            raise GoalPackInvalid(
                f"calibration.min_agreement must be within -1..1; got {agreement}.",
                details={"field": "calibration.min_agreement", "value": agreement},
            )
        return cls(
            target_samples=counts["target_samples"],
            min_samples=counts["min_samples"],
            holdout_fraction=float(fraction),
            partition_seed=int(seed),
            min_agreement=float(agreement),
        )

    def as_json(self) -> dict[str, Any]:
        """Return the block as stored on ``goals.calibration_config_json``."""
        return {
            "target_samples": self.target_samples,
            "min_samples": self.min_samples,
            "holdout_fraction": self.holdout_fraction,
            "partition_seed": self.partition_seed,
            "min_agreement": self.min_agreement,
        }


@dataclass(frozen=True, slots=True)
class GoalTask:
    """One task a candidate answers, already rendered from its prompt record.

    A task prompt is a prompt record and obeys ADR-0012 in full (ADR-0031 §6), so the loading
    and the Jinja2 sandbox are the ones
    :mod:`freeweight.services.prompts` already provides. What reaches the domain is the *result*
    of that loading.

    Attributes:
        key: Stable within the goal.
        name: Display label.
        prompt_id: The record's ID.
        prompt_version: The record's version.
        prompt_sha256: The record's canonical hash — a ``goal_hash`` input.
        rendered_prompt_hash: The hash of the text actually sent, recorded on every sample.
        system_prompt: The rendered system turn, or ``None``.
        prompt_text: The rendered user turn.
        source: The annotated source, claim list or reference outputs a rung-3 criterion needs.
        is_starter: Whether this is unedited shipped starter content.
    """

    key: str
    name: str
    prompt_id: str
    prompt_version: str
    prompt_sha256: str
    rendered_prompt_hash: str
    prompt_text: str
    system_prompt: str | None = None
    source: Mapping[str, Any] | None = None
    is_starter: bool = False


@dataclass(frozen=True, slots=True)
class GoalPack:
    """One user-authored goal: its identity, its criteria, its tasks and its policy.

    Attributes:
        slug: The goal's stable identifier; its capability is ``user.<slug>``.
        name: Display label.
        intent: The author's own description of what they wanted. Never machine-read, never
            hashed — it exists so the goal is legible in six months.
        goal_pack_version: Semantic version of the pack.
        schema_version: The record format this pack was written against.
        contributes_to: A shipped capability this goal also feeds, or ``None``.
        criteria: The criteria, in declaration order.
        tasks: The tasks, in declaration order.
        judge: The jury configuration, or ``None`` for a goal with no judged criterion.
        calibration: The calibration policy.
        created_by: Free text the author supplied. Never harvested from the environment
            (spec §14).
        created_at: When the pack was authored, or ``None``.
        forked_from: The starter key this was copied from, or ``None``.
        unforked: Whether the criteria and tasks are unedited starter content.
    """

    slug: str
    name: str
    goal_pack_version: str
    criteria: tuple[Criterion, ...]
    tasks: tuple[GoalTask, ...]
    calibration: CalibrationConfig
    schema_version: str = SCHEMA_VERSION
    intent: str = ""
    contributes_to: str | None = None
    judge: JudgeConfig | None = None
    created_by: str = "unknown"
    created_at: datetime | None = None
    forked_from: str | None = None
    unforked: bool = False

    @property
    def capability_id(self) -> str:
        """The capability this goal's evidence is emitted under: ``user.<slug>``."""
        return f"{CAPABILITY_PREFIX}.{self.slug}"

    @property
    def judged_criteria(self) -> tuple[Criterion, ...]:
        """The criteria a jury has to score."""
        return tuple(item for item in self.criteria if item.rung is Rung.JUDGE)

    @property
    def total_weight(self) -> float:
        """The sum of every criterion's weight."""
        return sum(item.weight for item in self.criteria)

    def criterion(self, key: str) -> Criterion | None:
        """Return one criterion by key, or ``None``."""
        return next((item for item in self.criteria if item.key == key), None)


def _check_slug(slug: str) -> None:
    """Refuse a slug that is not usable as a directory name and a capability specialization.

    Raises:
        GoalPackInvalid: The slug is empty, does not match :data:`SLUG_PATTERN`, or collides with
            a shipped capability root — ADR-0032 §1 reserves ``user`` precisely so that goal
            capabilities never have to compete with the vocabulary's own terms.
    """
    from setspec.vocabulary import CAPABILITIES

    if not SLUG_PATTERN.match(slug):
        raise GoalPackInvalid(
            f"Goal slug {slug!r} must match {SLUG_PATTERN.pattern}: it becomes a directory name, "
            "a URL path segment and a capability specialization, and all three are checked "
            "against the same allowlist before anything is built from it.",
            details={"slug": slug},
        )
    if slug in CAPABILITIES:
        raise GoalPackInvalid(
            f"Goal slug {slug!r} collides with a shipped capability root. Goal evidence is "
            f"emitted as '{CAPABILITY_PREFIX}.{slug}', and a slug that names a shipped root would "
            "make one person's rubric look like a term other components believe is objective "
            "(ADR-0032 §1).",
            details={"slug": slug},
        )


def parse_pack(body: Mapping[str, Any], *, tasks: Sequence[GoalTask]) -> GoalPack:
    """Parse one ``goal.json`` into a :class:`GoalPack`.

    Shape only. Weight sums, missing descriptors and mechanizable judged criteria are
    :mod:`freeweight.domain.goals.lint`'s findings, because ``goals validate`` must name every
    problem at once and an exception would name one.

    Args:
        body: The parsed ``goal.json``.
        tasks: The tasks, already loaded and rendered by the service layer.

    Returns:
        The pack.

    Raises:
        GoalPackInvalid: The slug is missing or unusable; ``schema_version`` is one this build
            does not speak; ``criteria`` is missing, empty or not a list; a criterion is
            malformed; the ``judge`` or ``calibration`` block is malformed; or two criteria share
            a key.
    """
    slug = str(body.get("slug", ""))
    _check_slug(slug)
    schema_version = str(body.get("schema_version", SCHEMA_VERSION))
    if schema_version != SCHEMA_VERSION:
        raise GoalPackInvalid(
            f"Goal {slug!r} declares schema_version {schema_version!r}; this build speaks "
            f"{SCHEMA_VERSION!r}.",
            details={"slug": slug, "schema_version": schema_version},
        )
    raw_criteria = body.get("criteria")
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise GoalPackInvalid(
            f"Goal {slug!r} declares no criteria. A goal with nothing to measure is not a "
            "benchmark.",
            details={"slug": slug},
        )
    criteria = tuple(
        Criterion.from_json(dict(entry), ordinal=index)
        for index, entry in enumerate(raw_criteria)
        if isinstance(entry, dict)
    )
    if len(criteria) != len(raw_criteria):
        raise GoalPackInvalid(
            f"Goal {slug!r} declares a criterion that is not an object.", details={"slug": slug}
        )
    keys = [item.key for item in criteria]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise GoalPackInvalid(
            f"Goal {slug!r} declares criteria {duplicates} more than once. A criterion key "
            "identifies a measurement over time, so a collision merges two of them.",
            details={"slug": slug, "duplicates": duplicates},
        )
    judge_body = body.get("judge")
    judge = JudgeConfig.from_json(judge_body) if isinstance(judge_body, dict) else None
    calibration_body = body.get("calibration")
    calibration = CalibrationConfig.from_json(
        calibration_body if isinstance(calibration_body, dict) else {}
    )
    created_at = body.get("created_at")
    parsed_at: datetime | None = None
    if isinstance(created_at, str) and created_at:
        from baseaicore import from_rfc3339

        try:
            parsed_at = from_rfc3339(created_at)
        except (ValueError, ValidationError) as exc:
            raise GoalPackInvalid(
                f"Goal {slug!r} declares created_at {created_at!r}, which is not an RFC 3339 "
                "timestamp.",
                details={"slug": slug, "created_at": created_at},
            ) from exc
    contributes_to = body.get("contributes_to")
    return GoalPack(
        slug=slug,
        name=str(body.get("name", slug)),
        goal_pack_version=str(body.get("goal_pack_version", "1.0.0")),
        criteria=criteria,
        tasks=tuple(tasks),
        calibration=calibration,
        schema_version=schema_version,
        intent=str(body.get("intent", "")),
        contributes_to=None if contributes_to in (None, "") else str(contributes_to),
        judge=judge,
        created_by=str(body.get("created_by", "unknown")),
        created_at=parsed_at,
        forked_from=(None if body.get("forked_from") in (None, "") else str(body["forked_from"])),
        unforked=bool(body.get("unforked", False)),
    )
