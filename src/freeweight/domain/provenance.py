"""freeweight.domain.provenance — the reproducibility fingerprint, and what refuses a repeat.

Pure domain: stdlib and :mod:`baseaicore` only. Everything here is a function of values the run
engine has already resolved, which is what makes the fingerprint testable without a database, a
provider or a machine.

**The document is the artefact; the hash is a shortcut to it.**
[Machine Identity §4](../../../../docs/architecture/machine-identity-and-reproducibility.md) rule
2 — "the full input document is stored, not just the hash" — is why
:func:`build_fingerprint_document` returns the document and :func:`compute_fingerprint` is a
separate one-line function over it. A hash nobody can explain is useless during a regression hunt,
and two runs that differ are compared by :func:`diff_documents`, field by field, rather than by
staring at two hex strings.

**What is in it, and one thing that is deliberately not.** The document carries the model identity
and digest, the runtime profile hash, the provider and its version, the machine fingerprint, the
drift-sensitive environment (driver, CUDA, OS), the benchmark's suite key, version, manifest hash,
dataset hashes and ``prompt_subset_hash``, the resolved execution parameters including the served
context with its source and the target GPU index, and the application's identity. It does **not**
carry the prompt *pack* hash: editing a prompt no benchmark uses must separate nothing, so the
pack's identity is recorded on the run as provenance and the per-benchmark subset is what is
hashed ([ADR-0028 §1](../../../../docs/adr/0028-prompt-pack-granularity.md)).

Phase 5's fingerprint covered the inputs that phase had and was documented as incomplete. This
document has a different shape, so a Phase 5 run and a Phase 6 run of the same subject have
different fingerprints — which is correct: they were produced by measurements with different
provenance, and the comparison view separates them rather than averaging them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from baseaicore import UNSUPPORTED, Measurement, canonical_json, is_supported, sha256_of

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "Degradation",
    "FieldDiff",
    "RepeatBlocker",
    "ServedContext",
    "ServedContextSource",
    "build_fingerprint_document",
    "case_selection_hash",
    "check_repeatable",
    "compute_fingerprint",
    "diff_documents",
    "divergence_degradation",
    "resolve_served_context",
]


class ServedContextSource(StrEnum):
    """Where a run's served context came from (data model §2, ``runs.served_context_source``).

    The distinction is load-bearing, not documentation. ``CONFIGURED`` means the caller set it and
    the provider declared it could honour that; ``REPORTED`` means the provider said what it was
    serving; ``ASSUMED`` means neither, and the model's advertised maximum was recorded as a
    guess. A benchmark whose context axis came from an assumption is a benchmark whose axis may be
    wrong, and only the recorded source can ever say so
    ([ADR-0023 §4](../../../../docs/adr/0023-runtime-profile-resolution.md)).
    """

    CONFIGURED = "configured"
    REPORTED = "reported"
    ASSUMED = "assumed"


@dataclass(frozen=True, slots=True)
class ServedContext:
    """The context length a run was actually served, and how that was established.

    Attributes:
        tokens: The context in tokens, or :data:`~baseaicore.UNSUPPORTED` when nothing — not the
            caller, not the provider, not the descriptor — could say. ``UNSUPPORTED`` rather than
            a default, because a context length nobody reported is not 4096.
        source: Which of the three ways it was established.
    """

    tokens: Measurement = UNSUPPORTED
    source: ServedContextSource = ServedContextSource.ASSUMED

    @property
    def numeric_tokens(self) -> int | None:
        """The context as the plain ``int`` ``runs.served_context`` takes, or ``None``."""
        return int(self.tokens) if is_supported(self.tokens) else None


def resolve_served_context(
    *,
    requested_context: int | None,
    context_configurable: bool,
    reported_context: Measurement = UNSUPPORTED,
    advertised_max_context: Measurement = UNSUPPORTED,
) -> ServedContext:
    """Resolve the context this run is served at, preferring fact over assumption.

    Order, and it is a strict preference order rather than a merge:

    1. The caller asked for a context **and** the provider declared ``context_configurable`` —
       ``CONFIGURED``. A provider that cannot configure context but accepts the setting anyway
       would produce a run whose recorded context never happened, which is why the capability
       gates this branch rather than the request alone (ModelRack spec §11.10).
    2. The provider reported what it is serving — ``REPORTED``.
    3. The model advertises a maximum — ``ASSUMED``. The advertised maximum is not the served
       context; recording it as one *without* the source would be the fabrication.
    4. Nothing at all — ``UNSUPPORTED``, still ``ASSUMED``, and the run says so.

    Args:
        requested_context: The runtime profile's ``context_size``, or ``None``.
        context_configurable: Whether the provider declared it can honour a requested context.
        reported_context: What the provider says it is serving.
        advertised_max_context: What the model's descriptor advertises.

    Returns:
        The resolved context and its source.
    """
    if requested_context is not None and context_configurable:
        return ServedContext(requested_context, ServedContextSource.CONFIGURED)
    if is_supported(reported_context):
        return ServedContext(reported_context, ServedContextSource.REPORTED)
    if is_supported(advertised_max_context):
        return ServedContext(advertised_max_context, ServedContextSource.ASSUMED)
    return ServedContext(UNSUPPORTED, ServedContextSource.ASSUMED)


@dataclass(frozen=True, slots=True)
class Degradation:
    """One thing that was less than ideal about a run, recorded on the run itself.

    Stored in ``runs.degradations_json``. A degradation is not an error: the run completed and its
    numbers are real, but something about the conditions is part of how they should be read —
    ``measured_while_busy`` with the utilization that was observed (spec §13),
    ``multi_gpu_placement_unknown`` (ADR-0027 §3), ``repeat_forced`` with the divergences the user
    chose to proceed past. Recording it is what makes contamination visible in the provenance
    instead of turning up months later as unexplained dispersion.

    Attributes:
        kind: The stable degradation name.
        detail: The numbers or names that justify it.
    """

    kind: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        """Render as the object stored in ``runs.degradations_json``."""
        return {"kind": self.kind, "detail": dict(self.detail)}


def case_selection_hash(case_ids: Iterable[str]) -> str:
    """Return the ``sha256:``-prefixed hash of the cases a run actually executed.

    Over the **sorted** ids, deliberately: two runs that executed the same cases in a different
    randomized order measured the same thing, and a hash that changed with the shuffle would
    separate results the seed already explains.
    """
    return f"sha256:{sha256_of(canonical_json(sorted(case_ids)))}"


def build_fingerprint_document(  # noqa: PLR0913 — every argument is a documented fingerprint input
    *,
    model: Mapping[str, Any],
    runtime_profile_hash: str,
    provider: Mapping[str, Any],
    machine_fingerprint: str,
    environment: Mapping[str, Any],
    benchmark: Mapping[str, Any],
    execution: Mapping[str, Any],
    application: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the reproducibility-fingerprint document, JSON-safe and complete.

    One function, one shape, one place to change it — the run engine, the repeat check and the
    comparison view all read documents built here, so a field added for one of them is present for
    all three.

    Args:
        model: ``provider_kind``, ``provider_model_name``, ``artifact_digest``,
            ``identity_confidence``, ``descriptor_hash``.
        runtime_profile_hash: The serving parameters' stable hash.
        provider: ``kind`` and ``version``.
        machine_fingerprint: The machine this was measured on.
        environment: The drift-sensitive fields — ``gpu_driver_version``, ``cuda_version``,
            ``os_version`` (Machine Identity §5). Present even when every value is unknown, since
            "we could not read the driver version" is itself part of the record.
        benchmark: ``suite_key``, ``suite_version``, ``manifest_hash``, ``dataset_hashes`` and
            ``prompt_subset_hash`` — the per-benchmark subset, never the pack hash.
        execution: ``effective_parameters``, ``repetitions``, ``seed``, ``case_selection_hash``,
            ``served_context``, ``served_context_source``, ``gpu_index``, ``multi_gpu_visible``.
        application: ``name``, ``version``, ``git_commit``.

    Returns:
        The document, with keys in the order Machine Identity §4 writes them. Order does not
        affect the hash — :func:`~baseaicore.canonical_json` sorts keys — but it makes the stored
        document readable next to the specification it implements.
    """
    return {
        "model": dict(model),
        "runtime_profile_hash": runtime_profile_hash,
        "provider": dict(provider),
        "machine_fingerprint": machine_fingerprint,
        "environment": dict(environment),
        "benchmark": dict(benchmark),
        "execution": dict(execution),
        "application": dict(application),
    }


def compute_fingerprint(document: Mapping[str, Any]) -> str:
    """Return the ``sha256:``-prefixed fingerprint of a fingerprint document.

    Over :func:`~baseaicore.canonical_json`: UTF-8, sorted keys, no insignificant whitespace, and
    :data:`~baseaicore.UNSUPPORTED` serialized as the string ``"unsupported"`` (Machine Identity §4
    rule 1). The same inputs therefore hash identically in another process, on another platform
    and after a Python upgrade.
    """
    return f"sha256:{sha256_of(canonical_json(document))}"


@dataclass(frozen=True, slots=True)
class FieldDiff:
    """One field on which two fingerprint documents disagree.

    Attributes:
        path: Dotted path into the document, e.g. ``"benchmark.prompt_subset_hash"``.
        left: The value in the first document, or ``None`` when the field is absent from it.
        right: The value in the second document.
    """

    path: str
    left: Any
    right: Any


def diff_documents(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[FieldDiff, ...]:
    """Return every leaf field on which two fingerprint documents differ, sorted by path.

    Machine Identity §4 rule 3: "two runs with different fingerprints are never silently merged or
    averaged; the comparison UI shows a field-level diff of the two documents". This is that diff,
    and ``run repeat --check`` prints it.

    Args:
        left: The earlier document.
        right: The later one.

    Returns:
        One entry per differing leaf, including fields present in only one document (the absent
        side is ``None``). Empty when the two documents are identical.
    """
    diffs: list[FieldDiff] = []

    def walk(prefix: str, first: Any, second: Any) -> None:  # noqa: ANN401 — arbitrary JSON
        if isinstance(first, dict) and isinstance(second, dict):
            for key in sorted(set(first) | set(second)):
                child = f"{prefix}.{key}" if prefix else key
                walk(child, first.get(key), second.get(key))
            return
        if first != second:
            diffs.append(FieldDiff(path=prefix, left=first, right=second))

    walk("", dict(left), dict(right))
    return tuple(sorted(diffs, key=lambda diff: diff.path))


@dataclass(frozen=True, slots=True)
class RepeatBlocker:
    """One reason the environment can no longer satisfy a recorded run's configuration.

    Attributes:
        field_path: The fingerprint-document path that moved.
        recorded: What the original run recorded.
        observed: What this environment offers now.
        reason: A stable, machine-readable name for the class of change.
        explanation: One sentence a person can act on.
    """

    field_path: str
    recorded: Any
    observed: Any
    reason: str
    explanation: str

    def as_json(self) -> dict[str, Any]:
        """Render for ``runs.degradations_json`` and for the API's refusal payload."""
        return {
            "field": self.field_path,
            "recorded": self.recorded,
            "observed": self.observed,
            "reason": self.reason,
            "explanation": self.explanation,
        }


def _get(document: Mapping[str, Any], path: str) -> Any:  # noqa: ANN401 — arbitrary JSON
    """Read a dotted path out of a document, returning ``None`` for anything absent."""
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def check_repeatable(
    recorded: Mapping[str, Any], observed: Mapping[str, Any]
) -> tuple[RepeatBlocker, ...]:
    """Return every reason a recorded run cannot be repeated in this environment.

    The four checks are Machine Identity §7's list, and each is a *refusal* rather than a warning
    because repeating a run whose subject has changed produces a second measurement of a different
    thing under a name that says it is the same thing:

    * **Model digest.** Different weights under the same name are a different subject entirely
      (ADR-0008). Absent where one was recorded is the same problem: nothing here can be shown to
      be the model that was measured.
    * **Machine fingerprint.** Performance measured elsewhere is not this machine's performance.
    * **Provider version.** A provider upgrade can change the prompt template and the sampling
      defaults, which changes both speed and output.
    * **Dataset hashes.** A dataset that moved underneath a benchmark separates its results.

    ``--force`` proceeds past all of them and records the divergence on the new run rather than
    pretending the two match.

    Args:
        recorded: The original run's fingerprint document.
        observed: A document built from this environment, with the same shape.

    Returns:
        The blockers, in the order checked. Empty when the environment still satisfies the run.
    """
    checks: tuple[tuple[str, str, str], ...] = (
        (
            "model.artifact_digest",
            "model_digest_changed",
            "The model's weights are not the ones that were measured; a digest change under the "
            "same name is a different model, not a newer one.",
        ),
        (
            "machine_fingerprint",
            "machine_changed",
            "This is not the machine the original run was measured on, so its performance "
            "numbers are not comparable.",
        ),
        (
            "provider.version",
            "provider_version_changed",
            "The provider has been upgraded; its template and sampling defaults may have moved "
            "with it.",
        ),
        (
            "benchmark.dataset_hashes",
            "dataset_hash_changed",
            "The benchmark's data is not the data the original run used.",
        ),
    )
    blockers: list[RepeatBlocker] = []
    for path, reason, explanation in checks:
        before, after = _get(recorded, path), _get(observed, path)
        if before != after:
            blockers.append(
                RepeatBlocker(
                    field_path=path,
                    recorded=before,
                    observed=after,
                    reason=reason,
                    explanation=explanation,
                )
            )
    return tuple(blockers)


def divergence_degradation(blockers: Sequence[RepeatBlocker]) -> Degradation:
    """Return the degradation a ``--force``d repeat records on the new run.

    Args:
        blockers: What was overridden. Never empty when this is called — a forced repeat that
            diverged from nothing is an ordinary repeat.

    Returns:
        A ``repeat_forced`` degradation naming every field that moved, so the new run's provenance
        says it is not the same measurement rather than quietly claiming it is.
    """
    return Degradation(
        kind="repeat_forced",
        detail={"divergences": [blocker.as_json() for blocker in blockers]},
    )
