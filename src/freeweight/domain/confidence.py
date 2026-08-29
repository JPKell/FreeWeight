"""freeweight.domain.confidence — ADR-0017's confidence formula, with ADR-0032's sixth factor.

One formula, one owner, one implementation: **FreeWeight computes confidence; LoadCoach applies
it.** Everything a consumer needs to weigh a capability score is computed here and carried on the
evidence record, and nothing here reads a database, a clock or a provider — every input is an
argument, which is what makes each factor testable against a hand-computed value.

::

    confidence = sample_factor × consistency_factor × freshness_factor
               × environment_factor × identity_factor × judge_validity_factor
                 clamped to [0.05, 1.0]

The sixth factor is **1.0 for every measurement scored at ladder rungs 1–4**, which is every native
and external suite, so no result specified before ADR-0032 changes value; it falls below 1.0 only
for a user-defined goal's judged criteria (:mod:`freeweight.domain.calibration` computes it).

Two rules that are easy to get wrong, and are therefore asserted rather than assumed:

* **Freshness decays from ``measured_at``** — the latest ``completed_at`` among the contributing
  runs — never from the aggregation time. Recomputing evidence over unchanged runs does not make
  it fresher ([ADR-0022 §2](../../../../docs/adr/0022-capability-evidence-record-contract.md)).
* **A hard separation is not a confidence reduction.** A differing benchmark version, dataset
  hash, prompt subset hash, model digest, runtime profile, ``goal_hash`` or judge set is a
  *different measurement*; :func:`separations` names the dimensions that differ so a caller can
  partition, and nothing in this module ever discounts across one.

Every parameter is a judgement call with no empirical basis yet — ADR-0017 says so — and is
therefore configuration, carried in :class:`ConfidencePolicy` and recorded on every evidence record
beside the policy version, so a number computed under one set of parameters is never silently
compared with one computed under another.

Pure domain: stdlib and :mod:`baseaicore` only.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from baseaicore import IdentityConfidence, MetricKind

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CONFIDENCE_CEILING",
    "CONFIDENCE_FLOOR",
    "POLICY_VERSION",
    "ConfidenceBreakdown",
    "ConfidencePolicy",
    "Environment",
    "SeparationKey",
    "compute_confidence",
    "consistency_factor",
    "detect_drift",
    "environment_factor",
    "freshness_factor",
    "identity_factor",
    "is_stale",
    "sample_factor",
    "separations",
]

POLICY_VERSION = "1.0"
"""The confidence-policy version this build's *default* parameters are published under.

Recorded on every evidence record (ADR-0022 §1, ``policy_version``). A record computed under
customised parameters carries a derived version instead — see
:meth:`ConfidencePolicy.policy_version` — so two policies coexist as two versions rather than as
one version meaning two things."""

CONFIDENCE_FLOOR = 0.05
"""ADR-0017's floor. Weak evidence survives as a tiebreak; it is never discarded by the formula."""

CONFIDENCE_CEILING = 1.0

_MAX_CONSISTENCY_PENALTY = 0.5
_SECONDS_PER_DAY = 86_400.0
_VERSION_NUMBERS = re.compile(r"(\d+)")


@dataclass(frozen=True, slots=True)
class ConfidencePolicy:
    """Every parameter of the confidence formula, in one place, with ADR-0017's defaults.

    Attributes:
        n_target: The sample count at which ``sample_factor`` reaches 1.0. Thirty by default:
            three samples are worth about 0.32, thirty about 1.0, and the curve is a square root
            so the returns diminish rather than stop.
        quality_half_life_days: Freshness half-life for quality evidence — 90 days, because
            quality is stable while the weights are.
        performance_half_life_days: Freshness half-life for performance, memory and energy
            evidence — 30 days, because speed follows the environment.
        freshness_floor: The lowest ``freshness_factor`` can fall. 0.3: old evidence degrades
            rather than vanishes, so a user who benchmarked thoroughly once and then stopped is not
            left with nothing.
        stale_below: The ``freshness_factor`` under which evidence is badged ``stale`` — about one
            half-life, at the default of 0.5.
        name_only_identity_factor: ``identity_factor`` for a ``name_only`` identity — 0.6, because
            the weights may have changed under the name.
        performance_drift_factor: ``environment_factor`` for a provider minor change or a
            driver/CUDA change, applied to performance-class evidence — 0.7.
        quality_drift_factor: ``environment_factor`` for a provider change with template or
            sampling implications, applied to quality evidence — 0.5.
    """

    n_target: int = 30
    quality_half_life_days: float = 90.0
    performance_half_life_days: float = 30.0
    freshness_floor: float = 0.3
    stale_below: float = 0.5
    name_only_identity_factor: float = 0.6
    performance_drift_factor: float = 0.7
    quality_drift_factor: float = 0.5

    def __post_init__(self) -> None:
        """Refuse parameters that would make a factor meaningless.

        Raises:
            ValueError: ``n_target`` or a half-life is not positive, or a factor or floor is
                outside ``[0, 1]``.
        """
        if self.n_target <= 0:
            raise ValueError(f"n_target must be positive; got {self.n_target}.")
        for name in ("quality_half_life_days", "performance_half_life_days"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive; got {getattr(self, name)}.")
        for name in (
            "freshness_floor",
            "stale_below",
            "name_only_identity_factor",
            "performance_drift_factor",
            "quality_drift_factor",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]; got {value}.")

    def half_life_days(self, kind: MetricKind) -> float:
        """Return the freshness half-life for one comparability class.

        Args:
            kind: The metric kind of the evidence. Quality decays on the long half-life;
                performance, memory and energy on the short one.

        Returns:
            The half-life in days.
        """
        if kind is MetricKind.QUALITY:
            return self.quality_half_life_days
        return self.performance_half_life_days

    @property
    def is_default(self) -> bool:
        """Whether every parameter is the shipped ADR-0017 value."""
        return self == ConfidencePolicy()

    def policy_version(self, *, mapping_version: str, customised: bool = False) -> str:
        """Return the ``policy_version`` evidence computed under this policy carries.

        ``"<mapping version>"`` for the shipped parameters and the shipped capability weights;
        ``"<mapping version>+<8 hex>"`` — a content hash — the moment either is customised. A
        customised policy therefore never shares a version with the shipped one, and two
        different customisations never share a version with each other, so ADR-0022 §3's
        row-wise coexistence of policy versions holds without anyone remembering to bump a number.

        Args:
            mapping_version: The capability mapping's own version.
            customised: Whether the capability weights differ from the shipped file.

        Returns:
            The version string.
        """
        if self.is_default and not customised:
            return mapping_version
        from baseaicore import canonical_json, sha256_of

        digest = sha256_of(canonical_json({"policy": self.as_json(), "mapping": mapping_version}))
        return f"{mapping_version}+{digest[:8]}"

    def as_json(self) -> dict[str, Any]:
        """Return the parameters as they are recorded beside every evidence record."""
        return {
            "n_target": self.n_target,
            "quality_half_life_days": self.quality_half_life_days,
            "performance_half_life_days": self.performance_half_life_days,
            "freshness_floor": self.freshness_floor,
            "stale_below": self.stale_below,
            "name_only_identity_factor": self.name_only_identity_factor,
            "performance_drift_factor": self.performance_drift_factor,
            "quality_drift_factor": self.quality_drift_factor,
        }


@dataclass(frozen=True, slots=True)
class Environment:
    """The drift-sensitive facts about where a measurement happened (ADR-0022 §1, ``environment``).

    Attributes:
        provider_kind: Which kind of provider served the model.
        provider_version: The provider's own version string, or ``None`` when unrecorded.
        gpu_driver_version: A drift signal; ``None`` when no GPU was involved or it is unknown.
        cuda_version: A drift signal.
        os_version: Recorded, but never a drift signal: ADR-0017 weights an OS patch level at 1.0.
    """

    provider_kind: str
    provider_version: str | None = None
    gpu_driver_version: str | None = None
    cuda_version: str | None = None
    os_version: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the environment in the shape ``capability.evidence`` carries."""
        return {
            "provider_kind": self.provider_kind,
            "provider_version": self.provider_version,
            "gpu_driver_version": self.gpu_driver_version,
            "cuda_version": self.cuda_version,
            "os_version": self.os_version,
        }

    @classmethod
    def from_json(cls, body: Mapping[str, Any], *, provider_kind: str) -> Environment:
        """Rebuild an environment from a stored ``environment`` mapping.

        Args:
            body: The stored mapping — a run's fingerprint ``environment`` section, or an evidence
                row's snapshot. Missing keys read as unrecorded rather than raising, because a
                snapshot written by an older build is still an environment.
            provider_kind: The provider kind, which the fingerprint section does not carry.

        Returns:
            The environment.
        """

        def text(key: str) -> str | None:
            value = body.get(key)
            return str(value) if value is not None else None

        return cls(
            provider_kind=str(body.get("provider_kind") or provider_kind),
            provider_version=text("provider_version"),
            gpu_driver_version=text("gpu_driver_version"),
            cuda_version=text("cuda_version"),
            os_version=text("os_version"),
        )


def sample_factor(sample_count: int, *, n_target: int = 30) -> float:
    """``min(1.0, sqrt(n / n_target))`` — diminishing returns on sample count.

    Args:
        sample_count: Supported samples behind the score. Excluded samples are not counted
            (ADR-0016 §6).
        n_target: The count at which the factor reaches 1.0.

    Returns:
        The factor in ``[0, 1]``. Zero samples is zero — a score with no samples behind it is not
        a measurement, and the floor is applied to the product, not here.

    Raises:
        ValueError: ``sample_count`` is negative or ``n_target`` is not positive.
    """
    if sample_count < 0:
        raise ValueError(f"sample_count cannot be negative; got {sample_count}.")
    if n_target <= 0:
        raise ValueError(f"n_target must be positive; got {n_target}.")
    return min(1.0, math.sqrt(sample_count / n_target))


def consistency_factor(dispersion: float | None) -> float:
    """``1 − min(0.5, dispersion)`` — a wildly variable measurement is a weak one.

    Args:
        dispersion: The coefficient of variation for a continuous metric, or the disagreement
            rate for a pass/fail one — ADR-0017 defines which applies from the metric's own kind,
            and the producer supplies the right one. ``None`` when fewer than two supported
            samples exist, where the spread is *undefined* rather than zero.

    Returns:
        The factor in ``[0.5, 1]``. An undefined dispersion is not penalised here: a single
        observation already carries a tiny ``sample_factor``, and a second penalty for the same
        fact would count it twice.

    Raises:
        ValueError: ``dispersion`` is negative.
    """
    if dispersion is None:
        return 1.0
    if dispersion < 0:
        raise ValueError(f"dispersion cannot be negative; got {dispersion}.")
    return 1.0 - min(_MAX_CONSISTENCY_PENALTY, dispersion)


def freshness_factor(
    *, measured_at: datetime, now: datetime, half_life_days: float, floor: float = 0.3
) -> float:
    """``0.5 ** (age_days / half_life_days)``, floored — decay with a floor, not a cut-off.

    ``age_days`` is ``now − measured_at`` where ``measured_at`` is the latest ``completed_at``
    among the contributing runs (ADR-0022 §2). Passing an aggregation time here is the one mistake
    this signature cannot prevent, which is why the evidence service's freshness test asserts that
    recomputing unchanged runs leaves this value unchanged.

    Args:
        measured_at: When the newest contributing run completed. Timezone-aware.
        now: The current instant. Timezone-aware; injected, never read from a clock here.
        half_life_days: 90 for quality, 30 for performance/memory/energy.
        floor: The lowest value returned.

    Returns:
        The factor in ``[floor, 1]``. A measurement from the future — a clock skew, not a
        physics event — is treated as zero days old rather than as negative age.

    Raises:
        ValueError: Either instant is naive, or ``half_life_days`` is not positive.
    """
    for name, instant in (("measured_at", measured_at), ("now", now)):
        if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
            raise ValueError(f"{name} must be timezone-aware; a naive instant has no age.")
    if half_life_days <= 0:
        raise ValueError(f"half_life_days must be positive; got {half_life_days}.")
    age_days = max(0.0, (now - measured_at).total_seconds() / _SECONDS_PER_DAY)
    return max(floor, float(0.5 ** (age_days / half_life_days)))


def _version_parts(version: str | None) -> tuple[int, ...]:
    """Return the leading numeric components of a version string, for a major/minor comparison."""
    if not version:
        return ()
    return tuple(int(part) for part in _VERSION_NUMBERS.findall(version)[:3])


def detect_drift(
    measured: Environment, current: Environment | None, *, kind: MetricKind
) -> tuple[str, ...]:
    """Name the environment dimensions that drifted between measurement and now.

    Which dimensions count depends on what the evidence measures. A driver upgrade changes how
    fast a model is served and not what it says, so it drifts performance evidence and leaves
    quality evidence alone; a provider **major** change may change templating and sampling, which
    reaches the answers themselves. An OS patch level drifts nothing (ADR-0017, ×1.0).

    Args:
        measured: The environment the evidence was measured in.
        current: The environment now, or ``None`` when it is unknown — in which case nothing can
            be said to have drifted, and nothing is.
        kind: The evidence's comparability class.

    Returns:
        The drifted dimension names, in a fixed order, empty when nothing drifted. A provider
        version difference is reported as ``provider_major`` or ``provider_minor``; a patch-level
        difference is not drift at all.
    """
    if current is None:
        return ()
    drift: list[str] = []
    before = _version_parts(measured.provider_version)
    after = _version_parts(current.provider_version)
    if before and after and before[:1] != after[:1]:
        drift.append("provider_major")
    elif before[:2] != after[:2] and (before or after):
        drift.append("provider_minor")
    if kind is not MetricKind.QUALITY:
        if measured.gpu_driver_version != current.gpu_driver_version:
            drift.append("gpu_driver_version")
        if measured.cuda_version != current.cuda_version:
            drift.append("cuda_version")
    return tuple(drift)


def environment_factor(
    measured: Environment,
    current: Environment | None,
    *,
    kind: MetricKind,
    policy: ConfidencePolicy | None = None,
) -> float:
    """1.0 with no drift; reduced once, never compounded, when something drifted.

    ADR-0017's table: ×0.7 for a provider minor change or a driver/CUDA change affecting
    performance evidence, ×0.5 for a provider change with template/sampling implications affecting
    quality evidence, ×1.0 for an OS patch level. Applied **once** per record: two drifted
    dimensions do not multiply into 0.49, because the ADR describes a state (drifted) rather than a
    count.

    Args:
        measured: The environment the evidence was measured in.
        current: The environment now, or ``None`` when unknown.
        kind: The evidence's comparability class.
        policy: The parameters; the shipped defaults when omitted.

    Returns:
        The factor.
    """
    policy = policy if policy is not None else ConfidencePolicy()
    drift = detect_drift(measured, current, kind=kind)
    if not drift:
        return 1.0
    if kind is MetricKind.QUALITY:
        # Only a change that can reach the answers themselves discounts quality evidence.
        return policy.quality_drift_factor if "provider_major" in drift else 1.0
    if "provider_major" in drift:
        return min(policy.performance_drift_factor, policy.quality_drift_factor)
    return policy.performance_drift_factor


def identity_factor(
    identity_confidence: IdentityConfidence | str, *, policy: ConfidencePolicy | None = None
) -> float:
    """1.0 for a ``digest`` identity; reduced for ``name_only``, whose weights may have changed.

    Args:
        identity_confidence: The measured model's identity confidence.
        policy: The parameters; the shipped defaults when omitted.

    Returns:
        The factor.

    Raises:
        ValueError: The value names neither identity confidence.
    """
    policy = policy if policy is not None else ConfidencePolicy()
    confidence = IdentityConfidence(identity_confidence)
    if confidence is IdentityConfidence.DIGEST:
        return 1.0
    return policy.name_only_identity_factor


def is_stale(
    *, freshness: float, drift: tuple[str, ...], policy: ConfidencePolicy | None = None
) -> bool:
    """ADR-0017's staleness surface: below about one half-life, or any detected drift.

    Args:
        freshness: The freshness factor, as of the moment the question is asked.
        drift: The drifted dimensions, from :func:`detect_drift`.
        policy: The parameters; the shipped defaults when omitted.

    Returns:
        Whether the evidence should be badged stale and offered a re-run.
    """
    policy = policy if policy is not None else ConfidencePolicy()
    return freshness < policy.stale_below or bool(drift)


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """The six factors, their product, and the facts they were computed from.

    Carried on every evidence record beside the single number, because ADR-0032's own consequence
    note is explicit: six multiplied factors compound quickly, and *the UI must explain the factor
    breakdown rather than presenting a single number, or users will conclude the feature does not
    work*.

    Attributes:
        sample_factor: From the supported sample count.
        consistency_factor: From the dispersion.
        freshness_factor: From ``measured_at`` and ``now``.
        environment_factor: From the drift between the measured and current environments.
        identity_factor: From the identity confidence.
        judge_validity_factor: The sixth factor; 1.0 for every rung 1–4 measurement.
        confidence: The clamped product.
        age_days: ``now − measured_at``, in days, so a reader sees the age the freshness came from.
        half_life_days: The half-life that applied.
        drift: The drifted environment dimensions, empty when none.
        stale: ADR-0017's staleness verdict at ``now``.
    """

    sample_factor: float
    consistency_factor: float
    freshness_factor: float
    environment_factor: float
    identity_factor: float
    judge_validity_factor: float
    confidence: float
    age_days: float
    half_life_days: float
    drift: tuple[str, ...] = ()
    stale: bool = False

    def as_json(self) -> dict[str, Any]:
        """Return the breakdown as the evidence row stores it and the UI explains it."""
        return {
            "sample_factor": self.sample_factor,
            "consistency_factor": self.consistency_factor,
            "freshness_factor": self.freshness_factor,
            "environment_factor": self.environment_factor,
            "identity_factor": self.identity_factor,
            "judge_validity_factor": self.judge_validity_factor,
            "confidence": self.confidence,
            "age_days": self.age_days,
            "half_life_days": self.half_life_days,
            "drift": list(self.drift),
            "stale": self.stale,
        }


def compute_confidence(  # noqa: PLR0913 — confidence is a function of exactly these facts
    *,
    sample_count: int,
    dispersion: float | None,
    measured_at: datetime,
    now: datetime,
    kind: MetricKind,
    measured_environment: Environment,
    current_environment: Environment | None,
    identity_confidence: IdentityConfidence | str,
    judge_validity_factor: float = 1.0,
    policy: ConfidencePolicy | None = None,
) -> ConfidenceBreakdown:
    """Compute ADR-0017's confidence with ADR-0032's sixth factor, clamped to ``[0.05, 1.0]``.

    Args:
        sample_count: Supported samples behind the score.
        dispersion: Coefficient of variation or disagreement rate, or ``None`` when undefined.
        measured_at: The latest ``completed_at`` among the contributing runs.
        now: The current instant, injected.
        kind: The evidence's comparability class, which selects the half-life and which
            environment dimensions count as drift.
        measured_environment: Where and under what the evidence was measured.
        current_environment: The environment now, or ``None`` when unknown.
        identity_confidence: The measured model's identity confidence.
        judge_validity_factor: The goal-level factor from calibration; 1.0 for anything scored at
            rungs 1–4.
        policy: The parameters; the shipped defaults when omitted.

    Returns:
        The breakdown, including the clamped product.

    Raises:
        ValueError: An input is out of its domain — a negative sample count, a naive instant, a
            judge validity factor outside ``[0, 1]``.
    """
    policy = policy if policy is not None else ConfidencePolicy()
    if not 0.0 <= judge_validity_factor <= 1.0:
        raise ValueError(
            f"judge_validity_factor must be within [0, 1]; got {judge_validity_factor}."
        )
    half_life = policy.half_life_days(kind)
    samples = sample_factor(sample_count, n_target=policy.n_target)
    consistency = consistency_factor(dispersion)
    freshness = freshness_factor(
        measured_at=measured_at, now=now, half_life_days=half_life, floor=policy.freshness_floor
    )
    drift = detect_drift(measured_environment, current_environment, kind=kind)
    environment = environment_factor(
        measured_environment, current_environment, kind=kind, policy=policy
    )
    identity = identity_factor(identity_confidence, policy=policy)
    product = samples * consistency * freshness * environment * identity * judge_validity_factor
    return ConfidenceBreakdown(
        sample_factor=samples,
        consistency_factor=consistency,
        freshness_factor=freshness,
        environment_factor=environment,
        identity_factor=identity,
        judge_validity_factor=judge_validity_factor,
        confidence=min(CONFIDENCE_CEILING, max(CONFIDENCE_FLOOR, product)),
        age_days=max(0.0, (now - measured_at).total_seconds() / _SECONDS_PER_DAY),
        half_life_days=half_life,
        drift=drift,
        stale=is_stale(freshness=freshness, drift=drift, policy=policy),
    )


@dataclass(frozen=True, slots=True)
class SeparationKey:
    """The dimensions along which two measurements are different measurements, never merged.

    ADR-0017's list plus ADR-0032 §4's two additions. Every field is a hard separation: evidence on
    different sides of any of them is partitioned, and no factor above ever discounts across one.

    Attributes:
        suite_version: The benchmark suite version.
        dataset_hashes: The suite's dataset hashes.
        prompt_subset_hash: The hash of the prompts *this benchmark* uses (ADR-0028).
        artifact_digest: The model digest, or ``None`` for a name-only identity.
        runtime_profile_hash: The runtime profile.
        machine_fingerprint: Where it was measured — a separation for performance, memory and
            energy evidence, a badge for quality evidence.
        goal_hash: The goal's measurement-defining hash, for a goal-sourced measurement.
        judge_set: The jury's identity, for a judged measurement.
    """

    suite_version: str
    dataset_hashes: Mapping[str, str] = field(default_factory=dict)
    prompt_subset_hash: str | None = None
    artifact_digest: str | None = None
    runtime_profile_hash: str | None = None
    machine_fingerprint: str | None = None
    goal_hash: str | None = None
    judge_set: str | None = None


def separations(
    left: SeparationKey, right: SeparationKey, *, kind: MetricKind = MetricKind.QUALITY
) -> tuple[str, ...]:
    """Name every dimension along which two measurements must not be merged.

    Args:
        left: One measurement's separation key.
        right: The other's.
        kind: The comparability class. A machine fingerprint separates performance, memory and
            energy evidence; quality evidence from another machine is retained with a badge
            instead (ADR-0017), so for quality the machine is not reported here.

    Returns:
        The differing dimension names, in a fixed order; empty when the two may merge.
    """
    differing: list[str] = []
    checks: tuple[tuple[str, object, object], ...] = (
        ("suite_version", left.suite_version, right.suite_version),
        ("dataset_hashes", dict(left.dataset_hashes), dict(right.dataset_hashes)),
        ("prompt_subset_hash", left.prompt_subset_hash, right.prompt_subset_hash),
        ("artifact_digest", left.artifact_digest, right.artifact_digest),
        ("runtime_profile_hash", left.runtime_profile_hash, right.runtime_profile_hash),
        ("goal_hash", left.goal_hash, right.goal_hash),
        ("judge_set", left.judge_set, right.judge_set),
    )
    differing.extend(name for name, one, other in checks if one != other)
    if kind is not MetricKind.QUALITY and left.machine_fingerprint != right.machine_fingerprint:
        differing.append("machine_fingerprint")
    return tuple(differing)
