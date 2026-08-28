"""freeweight.domain.comparison — what may be compared with what, and what separates them.

Pure domain: stdlib, :mod:`baseaicore`, and this package's own
:mod:`~freeweight.domain.provenance`. It is handed *descriptions* of measurements — never a
session, never a run row — which is what lets every rule below be tested against hand-built
subjects.

**The comparability matrix is BaseAiCore's; the studies are FreeWeight's.**
:meth:`~baseaicore.MeasurementSubject.is_comparable_with` implements
[Canonical Model Identity §5](../../../../docs/architecture/canonical-model-identity.md) and
deliberately answers :attr:`~baseaicore.Comparability.INDETERMINATE` for two *different*
identities, because a measurement subject does not carry model family: from the subject alone,
``qwen3.5:9b-q8_0`` against ``qwen3.5:9b-q4_0`` and ``qwen3.5:9b`` against ``llama4:70b`` look
identical. This module supplies the missing fact — the descriptor's ``family`` and
``quantization`` — and turns that ``indeterminate`` into a named :class:`StudyKind`: a
**quantization study** when the family matches, and ``unrelated`` when it does not.

**Nothing here merges across a boundary marked ``separate``.** :func:`group_subjects` partitions
the subjects into groups whose members are directly comparable, and everything else is a
*side-by-side* study carrying the field-level fingerprint diff that separates it
(Machine Identity §4 rule 3, §8 rule 5). There is no code path in this module that averages two
measurements; the strongest thing it produces is permission for a caller to.

**Metric kind is looked up, never guessed from a name.** Comparability across machines depends on
whether a metric measures the model or the hardware, and a rule that pattern-matched on ``"ms"``
would classify ``instructions_followed`` as a latency the first time somebody named a metric
badly. :data:`METRIC_KINDS` is the explicit table, and an unlisted key is
:attr:`~baseaicore.MetricKind.QUALITY` — the *conservative* default, since quality is the only
kind that survives a machine change and therefore the only kind whose default cannot silently
hide a separation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from baseaicore import (
    Comparability,
    ComparabilityVerdict,
    IdentityConfidence,
    MeasurementSubject,
    MetricKind,
)

from freeweight.domain.provenance import FieldDiff, diff_documents

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "ENERGY_METRIC_PREFIXES",
    "MEMORY_METRIC_PREFIXES",
    "METRIC_KINDS",
    "PERFORMANCE_METRIC_PREFIXES",
    "ComparisonGroup",
    "ComparisonSubject",
    "PairVerdict",
    "StudyKind",
    "group_subjects",
    "metric_kind_for",
    "separation_diff",
    "verdict_for_pair",
]


class StudyKind(StrEnum):
    """What kind of comparison two measurements can honestly support.

    :attr:`DIRECT` is the only one that permits merging. The three ``*_STUDY`` kinds are the
    explicit, never-merged comparisons of Canonical Model Identity §5 — the ones a user actually
    wants when they ask "does q4 cost me anything?" — and each is shown side by side with the
    dimension that separates it named. :attr:`INCOMPARABLE` covers a boundary that is not a study
    at all: a benchmark-version or dataset change, where the two numbers answer different
    questions. :attr:`UNRELATED` is two different models, which is a *listing*, not a study.
    """

    DIRECT = "direct"
    RUNTIME_STUDY = "runtime_study"
    QUANTIZATION_STUDY = "quantization_study"
    MACHINE_STUDY = "machine_study"
    INCOMPARABLE = "incomparable"
    UNRELATED = "unrelated"


PERFORMANCE_METRIC_PREFIXES: tuple[str, ...] = (
    "prompt_tokens_per_second",
    "decode_tokens_per_second",
    "prompt_eval_ms",
    "decode_ms",
    "load_ms",
    "total_ms",
    "ttft_ms",
    "inter_chunk_ms",
    "inter_token_ms",
    "reuse_speedup",
    "cold_prefill_ms",
    "warm_prefill_ms",
)
"""Metric keys that measure the *machine* serving the model, not the model's answers."""

MEMORY_METRIC_PREFIXES: tuple[str, ...] = (
    "peak_vram_bytes",
    "theoretical_kv_bytes_per_token",
    "observed_kv_bytes_per_token",
    "observed_mb_per_1k_context",
    "kv_overhead_ratio",
    "kv_slope_fit_r_squared",
    "max_successful_context_tokens",
)
"""Metric keys that describe device memory. Never comparable across machines (ADR-0027 §5)."""

ENERGY_METRIC_PREFIXES: tuple[str, ...] = (
    "gpu_energy_joules",
    "mean_gpu_power_watts",
    "peak_gpu_power_watts",
    "max_gpu_temperature_c",
    "max_cpu_temperature_c",
    "joules_per_request",
    "joules_per_output_token",
    "joules_per_successful_task",
    "output_tokens_per_joule",
    "successful_tasks_per_kwh",
)
"""Metric keys derived from power samples. Hardware-bound, and therefore machine-separated."""

METRIC_KINDS: Mapping[str, MetricKind] = {
    **{key: MetricKind.PERFORMANCE for key in PERFORMANCE_METRIC_PREFIXES},
    **{key: MetricKind.MEMORY for key in MEMORY_METRIC_PREFIXES},
    **{key: MetricKind.ENERGY for key in ENERGY_METRIC_PREFIXES},
}
"""The declared kind of every metric whose kind is not ``quality``.

A table rather than a heuristic, for the reason ``higher_is_better`` is declared rather than
inferred: a rule that guessed from the key gets the first badly-named metric wrong, and gets it
wrong in the direction that merges two numbers it should have separated."""


def metric_kind_for(metric_key: str) -> MetricKind:
    """Return the comparability class of one metric key.

    Args:
        metric_key: The key as a manifest declares it.

    Returns:
        Its entry in :data:`METRIC_KINDS`, or :attr:`~baseaicore.MetricKind.QUALITY` for a key not
        listed there. Quality is the safe default: it is the only kind that survives a machine
        change, so an unlisted key defaults to ``warn`` (badge the machine) rather than to a
        silent merge — and a metric that *should* have been separated shows up as a missing table
        entry rather than as a wrong number.
    """
    return METRIC_KINDS.get(metric_key, MetricKind.QUALITY)


@dataclass(frozen=True, slots=True)
class ComparisonSubject:
    """One measurement, described completely enough to decide what it may be compared with.

    Everything BaseAiCore's matrix needs, plus the two facts it deliberately does not carry — the
    descriptor's ``family`` and ``quantization`` — and the fingerprint document that explains any
    separation.

    Attributes:
        run_id: The run this describes. The stable handle a UI and a CLI both point at.
        subject: Which weights, under which runtime profile, on which machine.
        benchmark_key: The suite key, e.g. ``"native.memory_kv"``.
        benchmark_version: The suite version. A change **separates** results (spec §19).
        dataset_hashes: The suite's pinned dataset hashes; empty for a self-contained suite.
        fingerprint: The run's reproducibility fingerprint.
        fingerprint_document: The document that fingerprint was computed from, so a separation can
            be explained field by field rather than by two hex strings (Machine Identity §4).
        family: The descriptor's model family, or ``None`` where the provider did not report one.
        quantization: The descriptor's weight quantization, or ``None``.
        label: What to call this column in a table. The run's label, or its model's canonical ID.
    """

    run_id: str
    subject: MeasurementSubject
    benchmark_key: str
    benchmark_version: str
    dataset_hashes: Mapping[str, str] = field(default_factory=dict)
    fingerprint: str = ""
    fingerprint_document: Mapping[str, Any] = field(default_factory=dict)
    family: str | None = None
    quantization: str | None = None
    label: str = ""

    @property
    def is_name_only(self) -> bool:
        """Whether these weights are identified by name alone, with no digest to pin them."""
        return self.subject.identity.identity_confidence is IdentityConfidence.NAME_ONLY


@dataclass(frozen=True, slots=True)
class PairVerdict:
    """Whether two measurements may be compared, how, and what separates them.

    Attributes:
        left: The first measurement's run id.
        right: The second's.
        comparability: BaseAiCore's categorical outcome.
        study: What kind of comparison this supports, once family and quantization are known.
        reason: One sentence naming the rule that produced this outcome, for the UI to show
            beside the numbers and for the CLI to print when it refuses.
        metric_kind: The kind of metric this verdict was reached for. The same two runs are
            ``warn`` for quality and ``separate`` for performance, so a verdict without its kind
            is not an answer to anything.
        diff: The field-level fingerprint diff, non-empty exactly when the two are not directly
            comparable and both carry a document.
    """

    left: str
    right: str
    comparability: Comparability
    study: StudyKind
    reason: str
    metric_kind: MetricKind
    diff: tuple[FieldDiff, ...] = ()

    @property
    def may_merge(self) -> bool:
        """Whether a caller may average these two measurements into one figure.

        ``warn`` merges — a cross-machine *quality* comparison is allowed with the machine badged,
        and a ``name_only`` identity is allowed with the caveat shown. ``separate`` and
        ``indeterminate`` never do.
        """
        return self.comparability in {Comparability.COMPARABLE, Comparability.WARN}


def separation_diff(left: ComparisonSubject, right: ComparisonSubject) -> tuple[FieldDiff, ...]:
    """Return the field-level fingerprint diff between two subjects.

    Machine Identity §4 rule 3 and §8 rule 5: two runs with different fingerprints are never
    silently merged, and the comparison shows the diff that separates them. Empty when either
    subject carries no stored document — an honestly empty diff, since the alternative is to
    invent one from the fields this module happens to know.
    """
    if not left.fingerprint_document or not right.fingerprint_document:
        return ()
    return diff_documents(left.fingerprint_document, right.fingerprint_document)


def _study_for_different_identity(
    left: ComparisonSubject, right: ComparisonSubject
) -> tuple[StudyKind, str]:
    """Resolve BaseAiCore's ``indeterminate`` for two different identities.

    The one piece of information the matrix says it is missing: the descriptor's family. Same
    family and different quantization is the quantization study §5 names; anything else is two
    unrelated models, which is a listing rather than a comparison.
    """
    if left.family and right.family and left.family == right.family:
        if left.quantization != right.quantization:
            return (
                StudyKind.QUANTIZATION_STUDY,
                f"Same family {left.family!r} at different quantizations "
                f"({left.quantization or 'unreported'} vs {right.quantization or 'unreported'}): "
                "an explicit quantization comparison, shown side by side and never merged.",
            )
        return (
            StudyKind.UNRELATED,
            f"Two identities in family {left.family!r} at the same quantization "
            f"({left.quantization or 'unreported'}): different weights under different names, "
            "so there is no dimension that makes this a study.",
        )
    return (
        StudyKind.UNRELATED,
        "Different identities and no shared model family: these are two different models, "
        "listed side by side rather than compared.",
    )


def _study_for_separate(
    left: ComparisonSubject, right: ComparisonSubject, verdict: ComparabilityVerdict
) -> tuple[StudyKind, str]:
    """Name the dimension that produced a ``separate`` verdict.

    The dimensions are checked in the matrix's own order, so the *first* thing that differs is the
    one reported — a run at a different KV precision on a different machine is a runtime study
    whose machine also moved, and calling it a machine study would hide the interesting axis.
    """
    if left.subject.runtime_profile_hash != right.subject.runtime_profile_hash:
        return StudyKind.RUNTIME_STUDY, verdict.reason
    if left.subject.machine_fingerprint != right.subject.machine_fingerprint:
        return StudyKind.MACHINE_STUDY, verdict.reason
    if left.benchmark_version != right.benchmark_version:
        return (
            StudyKind.INCOMPARABLE,
            f"Benchmark {left.benchmark_key!r} version {left.benchmark_version} against version "
            f"{right.benchmark_version}: a suite version change separates results and they are "
            "never averaged. Re-run the older subject on the current version to compare them.",
        )
    return (
        StudyKind.INCOMPARABLE,
        verdict.reason,
    )


def verdict_for_pair(
    left: ComparisonSubject, right: ComparisonSubject, *, metric_kind: MetricKind
) -> PairVerdict:
    """Decide whether two measurements of one metric kind may be compared.

    Delegates the matrix to :meth:`~baseaicore.MeasurementSubject.is_comparable_with` — the rules
    live in one place, and this function adds only what a measurement subject cannot carry — then
    names the study and attaches the fingerprint diff wherever the two are not directly
    comparable.

    Args:
        left: The first measurement.
        right: The second.
        metric_kind: What kind of number is being compared. Required, because the same pair of
            runs is comparable for a quality metric and separated for a memory one.

    Returns:
        The verdict, with its reason and — for anything that is not a direct comparison — the
        field-level fingerprint diff a UI shows beside the two columns.
    """
    if left.benchmark_key != right.benchmark_key:
        return PairVerdict(
            left=left.run_id,
            right=right.run_id,
            comparability=Comparability.SEPARATE,
            study=StudyKind.INCOMPARABLE,
            reason=(
                f"Different benchmark suites ({left.benchmark_key!r} and "
                f"{right.benchmark_key!r}): two suites measure different things, and a metric key "
                "they happen to share is not the same measurement."
            ),
            metric_kind=metric_kind,
            diff=separation_diff(left, right),
        )

    verdict = left.subject.is_comparable_with(
        right.subject,
        metric_kind=metric_kind,
        benchmark_version=left.benchmark_version,
        other_benchmark_version=right.benchmark_version,
        dataset_hashes=left.dataset_hashes,
        other_dataset_hashes=right.dataset_hashes,
    )

    if verdict.comparability is Comparability.COMPARABLE:
        study, reason = StudyKind.DIRECT, verdict.reason
    elif verdict.comparability is Comparability.WARN:
        study, reason = (
            (StudyKind.MACHINE_STUDY, verdict.reason)
            if left.subject.machine_fingerprint != right.subject.machine_fingerprint
            else (StudyKind.DIRECT, verdict.reason)
        )
    elif verdict.comparability is Comparability.SEPARATE:
        study, reason = _study_for_separate(left, right, verdict)
    else:
        study, reason = _study_for_different_identity(left, right)

    return PairVerdict(
        left=left.run_id,
        right=right.run_id,
        comparability=verdict.comparability,
        study=study,
        reason=reason,
        metric_kind=metric_kind,
        diff=() if study is StudyKind.DIRECT else separation_diff(left, right),
    )


@dataclass(frozen=True, slots=True)
class ComparisonGroup:
    """A set of measurements that may be shown as one column of numbers.

    Attributes:
        members: The run ids in the group, in the order they were given.
        study: How the group relates to the *other* groups — ``direct`` for the only group in a
            comparison that needed no separation, otherwise the dimension that separated it out.
        reason: One sentence explaining why this group stands apart, for the UI's separator.
        metric_kind: The kind of metric this grouping was computed for.

    A group's members may be merged into one figure; two groups never may. That is the whole of
    the "never silently averaged" rule, expressed as a partition rather than as a check every
    caller has to remember to make.
    """

    members: tuple[str, ...]
    study: StudyKind
    reason: str
    metric_kind: MetricKind


def group_subjects(
    subjects: Sequence[ComparisonSubject], *, metric_kind: MetricKind
) -> tuple[tuple[ComparisonGroup, ...], tuple[PairVerdict, ...]]:
    """Partition measurements into groups that may be merged, for one kind of metric.

    A subject joins an existing group only when it is mergeable with **every** member of it, not
    merely with the first — comparability is not transitive across a ``name_only`` identity, and a
    group built by first-match would quietly average a subject with one it was never cleared
    against.

    Args:
        subjects: The measurements to compare, in the order the caller wants them shown.
        metric_kind: What kind of metric the grouping is for.

    Returns:
        ``(groups, verdicts)`` — the partition, and every pairwise verdict that produced it. The
        verdicts are returned rather than discarded because the reason two subjects are in
        different groups is the thing a person actually needs to read, and a UI that showed only
        the partition would show a separation with no explanation for it.
    """
    verdicts: list[PairVerdict] = []
    for index, left in enumerate(subjects):
        for right in subjects[index + 1 :]:
            verdicts.append(verdict_for_pair(left, right, metric_kind=metric_kind))
    by_pair = {(verdict.left, verdict.right): verdict for verdict in verdicts}

    def mergeable(left_id: str, right_id: str) -> PairVerdict | None:
        return by_pair.get((left_id, right_id)) or by_pair.get((right_id, left_id))

    groups: list[list[str]] = []
    separations: dict[int, PairVerdict] = {}
    for subject in subjects:
        for members in groups:
            pairings = [mergeable(subject.run_id, member) for member in members]
            if all(pair is not None and pair.may_merge for pair in pairings):
                members.append(subject.run_id)
                break
        else:
            first = groups[0][0] if groups else None
            if first is not None:
                against = mergeable(subject.run_id, first)
                if against is not None:
                    separations[len(groups)] = against
            groups.append([subject.run_id])

    return (
        tuple(
            ComparisonGroup(
                members=tuple(members),
                study=(
                    separations[position].study
                    if position in separations
                    else (StudyKind.DIRECT if position == 0 else StudyKind.INCOMPARABLE)
                ),
                reason=(
                    separations[position].reason
                    if position in separations
                    else "The first group; every other group is separated from this one."
                ),
                metric_kind=metric_kind,
            )
            for position, members in enumerate(groups)
        ),
        tuple(verdicts),
    )
