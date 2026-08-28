"""freeweight.services.comparison — assemble a comparison, and refuse the ones that are not one.

The read side of Phase 9. It resolves run references, loads what each run was measured *against*,
asks :mod:`freeweight.domain.comparison` what may be shown beside what, and lays the aggregate
metrics out as one row per metric key and one cell per run.

**Nothing here averages anything.** The layout is deliberately a *table*, not a merge: every cell
is one run's own stored figure, with the sample and exclusion counts that run recorded. Where two
runs are separated, they are in different groups and the table says so beside the numbers, with
the field-level fingerprint diff that separates them (Machine Identity §4 rule 3). The strongest
thing this module produces is permission — :attr:`MetricRow.mergeable` — and even that is a fact
about the runs rather than an aggregation of them.

**Comparability is decided per metric, because it differs per metric.** The same two runs on two
machines are comparable for ``answer_correct`` and separated for ``decode_tokens_per_second``, so
each row carries its own grouping and its own verdicts. A single comparison-wide verdict would
have to be the strictest one, which would hide every quality comparison behind a hardware
difference that does not affect it.

**A run with no fingerprint document still compares.** The document is what *explains* a
separation; its absence weakens the explanation, not the rule. The diff is then empty and the
reason still names the dimension.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from baseaicore import (
    Comparability,
    MeasurementSubject,
    MetricKind,
    ModelIdentity,
    NotFoundError,
    ProviderKind,
    ValidationError,
    is_supported,
)
from baseaicore.timeutil import to_rfc3339

from freeweight.domain.comparison import (
    ComparisonGroup,
    ComparisonSubject,
    PairVerdict,
    StudyKind,
    group_subjects,
    metric_kind_for,
    separation_diff,
    verdict_for_pair,
)
from freeweight.infrastructure.db.repositories.models import ModelRepository
from freeweight.infrastructure.db.repositories.runs import MetricValueRepository, RunRepository

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import Session

    from freeweight.domain.provenance import FieldDiff
    from freeweight.services.database import Database

__all__ = [
    "KV_SWEEP_METRIC",
    "MINIMUM_SWEEP_POINTS",
    "Comparison",
    "ComparisonRefused",
    "ContextSweep",
    "MetricCell",
    "MetricRow",
    "RunColumn",
    "SubjectNotFound",
    "compare_runs",
    "comparison_json",
    "enforce_suite",
]

_MINIMUM_SUBJECTS = 2


class SubjectNotFound(NotFoundError):
    """No run matches one of the references a comparison was asked for.

    Its own stable code so a caller can tell "you named a run that does not exist" from "these two
    runs cannot be compared", which are a usage error and a result respectively.
    """

    code = "COMPARISON_SUBJECT_NOT_FOUND"


class ComparisonRefused(ValidationError):
    """A comparison was asked for that is not a comparison.

    Raised only for the two cases that make the *request* meaningless — fewer than two subjects,
    or the same run named twice. An incomparable pair of runs is **not** this: that is a real
    result with real numbers in it, shown side by side with the reason they are separated, and
    raising here would throw away the very explanation the user asked for.
    """

    code = "COMPARISON_REFUSED"


@dataclass(frozen=True, slots=True)
class RunColumn:
    """One run as a column of a comparison table.

    Attributes:
        run_id: The run.
        label: What to call the column — the run's label, or its model's canonical ID.
        model_canonical_id: The model, for the column header's second line.
        suite_key: The suite this run executed.
        suite_version: That suite's version. A difference here separates every row.
        quantization: The descriptor's weight quantization, or ``None`` where unreported.
        family: The descriptor's model family, or ``None``.
        runtime_profile_hash: The serving parameters' hash.
        kv_cache_precision: The runtime profile's KV precision, shown because it is the axis of a
            KV-precision study and is otherwise invisible inside a hash.
        context_size: The runtime profile's requested context, or ``None``.
        machine_fingerprint: The machine.
        machine_hostname: That machine's hostname, or ``None`` — a fingerprint is not a name a
            person recognizes.
        gpu_index: The device this run's memory and energy figures are attributed to.
        started_at_rfc3339: When the run started, or ``None`` for one that never did.
        fingerprint: The reproducibility fingerprint.
        identity_confidence: ``digest`` or ``name_only``. A ``name_only`` column carries a caveat
            on every direct comparison it takes part in.
    """

    run_id: str
    label: str
    model_canonical_id: str
    suite_key: str
    suite_version: str
    quantization: str | None
    family: str | None
    runtime_profile_hash: str
    kv_cache_precision: str | None
    context_size: int | None
    machine_fingerprint: str
    machine_hostname: str | None
    gpu_index: int | None
    started_at_rfc3339: str | None
    fingerprint: str
    identity_confidence: str


@dataclass(frozen=True, slots=True)
class MetricCell:
    """One run's stored value for one metric, with the counts that produced it.

    Attributes:
        run_id: Which column this cell belongs to.
        numeric_value: The number, or ``None`` when the run could not produce one.
        unavailable_reason: Why there is no number. Non-``None`` exactly when ``numeric_value`` is
            ``None`` — the pairing storage already guarantees, carried through unchanged so a UI
            renders ``—`` with the reason rather than a blank.
        unit: The metric's unit.
        sample_count: How many samples the value was computed from.
        excluded_count: How many contributed nothing.
        stddev: Sample standard deviation, or ``None`` for a single observation.
        coefficient_of_variation: Spread relative to the mean, or ``None``.
        gpu_index: The device, for a per-device figure.
        group_index: Which comparability group this cell's run fell into for this metric. Cells
            with different group indices are never to be compared as numbers, however close they
            look.
    """

    run_id: str
    numeric_value: float | None
    unavailable_reason: str | None
    unit: str
    sample_count: int | None
    excluded_count: int | None
    stddev: float | None
    coefficient_of_variation: float | None
    gpu_index: int | None
    group_index: int


@dataclass(frozen=True, slots=True)
class MetricRow:
    """One metric across every run in the comparison, grouped by what may be compared.

    Attributes:
        metric_key: The metric.
        unit: Its unit.
        higher_is_better: Its declared direction. Declared by the benchmark, never inferred here.
        kind: Its comparability class — quality, performance, memory or energy — which is what
            decided the grouping.
        cells: One per run, in the order the runs were given. A run whose suite never produced
            this metric still has a cell, carrying ``metric_not_produced``: a missing cell and a
            refused measurement are indistinguishable in a table otherwise.
        groups: The partition for this metric.
        verdicts: Every pairwise verdict behind that partition, so the UI can explain any cell
            pair a user asks about.
        mergeable: Whether every run in this row fell into one group — the only case in which a
            caller may combine these cells into a single figure.
    """

    metric_key: str
    unit: str
    higher_is_better: bool
    kind: MetricKind
    cells: tuple[MetricCell, ...]
    groups: tuple[ComparisonGroup, ...]
    verdicts: tuple[PairVerdict, ...]

    @property
    def mergeable(self) -> bool:
        """Whether these cells may be combined into one figure."""
        return len(self.groups) <= 1

    @property
    def best_run_id(self) -> str | None:
        """The run with the best value **within the first group**, or ``None``.

        Confined to one group on purpose: picking a winner across a separation is exactly the
        silent comparison the whole module exists to prevent. A row whose runs are separated has
        no single winner, and saying so is the honest answer.
        """
        if not self.mergeable or not self.groups:
            return None
        scored = [cell for cell in self.cells if cell.numeric_value is not None]
        if not scored:
            return None
        chooser = max if self.higher_is_better else min
        return chooser(scored, key=lambda cell: cell.numeric_value or 0.0).run_id


KV_SWEEP_METRIC = "model_vram_bytes"
"""The per-model residency figure a context sweep is fitted against.

The *model's* VRAM as the provider reports it, not the device's total — which is what makes this
fit an isolation rather than a mitigation, and is the difference between it and
``native.memory_kv``'s in-run slope."""

MINIMUM_SWEEP_POINTS = 3
"""Runs needed before a context sweep is offered.

Two points fit a line exactly, so ``r_squared`` would be ``1.0`` by construction and say nothing
about agreement. Three is the smallest number at which the fit can be *wrong* and show it."""


@dataclass(frozen=True, slots=True)
class ContextSweep:
    """The KV cost function of one model, fitted across several runs at different contexts.

    This is a **study over runs**, not a benchmark result, and that distinction is the reason it
    lives here rather than inside a suite. ``size_vram`` scales with the context a model was
    *loaded* at — llama.cpp allocates the whole KV slot up front — so an in-run sweep of prompt
    lengths measures KV *fill*, not KV *cost*. The honest measurement differences one run against
    another at a different ``context_size``, and a benchmark is one run under one profile
    (ADR-0034 §6).

    Attributes:
        model_canonical_id: The model every point measures. A sweep never spans two.
        points: ``(context_size, model_vram_bytes)`` per run, ascending by context.
        weights_bytes: The fitted zero-context intercept — approximately the weights plus the
            runtime's fixed allocations.
        bytes_per_token: The fitted gradient: what one token of context costs in VRAM.
        r_squared: How well the line fits. A sweep taken while something else was using the GPU
            shows here rather than silently biasing the slope.
        residual_stddev_bytes: Spread of the observations around the line, in bytes.
    """

    model_canonical_id: str
    points: tuple[tuple[int, float], ...]
    weights_bytes: float
    bytes_per_token: float
    r_squared: float
    residual_stddev_bytes: float

    def predict_bytes(self, context_tokens: int) -> float:
        """VRAM this model would need served at ``context_tokens``.

        The allocation question a scheduler actually asks. Extrapolating past the sweep's own
        range is the caller's risk: the fit says nothing about a context nobody measured.

        Args:
            context_tokens: The served context to predict for.

        Returns:
            Predicted bytes.
        """
        return self.weights_bytes + self.bytes_per_token * context_tokens


@dataclass(frozen=True, slots=True)
class Comparison:
    """A whole comparison: its columns, its rows, and what separates them.

    Attributes:
        columns: One per run, in the order the caller gave them.
        rows: One per metric key any of the runs produced, sorted by key so two comparisons of the
            same suites line up.
        separations: One entry per pair of runs that is not a clean direct comparison at the
            matrix's most permissive row — a separation, or a caveat such as a ``name_only``
            identity — with the study it supports and the fingerprint diff behind it. Computed
            once for the whole comparison rather than per row, because a suite-version difference
            separates every row for the same reason and repeating it per metric is noise. A pair
            separated only for performance or memory is a *per-row* fact and lives on the row.
        study: The strongest single description of what this comparison is — a quantization study,
            a runtime study, a direct comparison — for a page heading and a CLI's first line.
        context_sweep: The fitted KV cost function, when these runs happen to be one model at
            three or more different served contexts on one machine; ``None`` otherwise, which is
            the usual case. It is *derived from* the comparison rather than requested: a user who
            has run the same model at several contexts has already produced the measurement, and
            this is the surface that notices.
    """

    columns: tuple[RunColumn, ...]
    rows: tuple[MetricRow, ...]
    separations: tuple[PairVerdict, ...] = ()
    study: StudyKind = StudyKind.DIRECT
    context_sweep: ContextSweep | None = None

    def diff_between(self, left_run_id: str, right_run_id: str) -> tuple[FieldDiff, ...]:
        """Return the stored fingerprint diff between two of this comparison's runs.

        Args:
            left_run_id: One run.
            right_run_id: The other.

        Returns:
            The field-level diff, or empty when the two are not separated or either run stored no
            fingerprint document.
        """
        for verdict in self.separations:
            if {verdict.left, verdict.right} == {left_run_id, right_run_id}:
                return verdict.diff
        return ()


def _resolve(session: Session, run_ref: str) -> Any:  # noqa: ANN401 — an ORM row, kept internal
    """Resolve a full ULID or an unambiguous prefix to one run row.

    Deliberately the same acceptance rule as ``run show`` (CLI standards §7: IDs accept an
    unambiguous prefix everywhere), and deliberately refuses an ambiguous one by naming the
    candidates rather than picking the first.
    """
    repository = RunRepository()
    exact = repository.get_by_id(session, run_ref)
    if exact is not None:
        return exact
    matches = repository.get_by_id_prefix(session, run_ref)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValidationError(
            f"{run_ref!r} matches {len(matches)} runs; use a longer prefix.",
            details={"run": run_ref, "candidates": [row.id for row in matches]},
        )
    raise SubjectNotFound(f"No run matches {run_ref!r}.", details={"run": run_ref})


@dataclass(frozen=True, slots=True)
class _Loaded:
    """One run, read once, in the two shapes the rest of this module needs it in."""

    column: RunColumn
    subject: ComparisonSubject
    metrics: Mapping[str, Any] = field(default_factory=dict)
    definitions: Mapping[str, tuple[str, bool]] = field(default_factory=dict)


def _load(session: Session, run_ref: str) -> _Loaded:
    """Read one run and everything a comparison needs to know about it.

    One function, one pass, because the column and the comparability subject are two views of the
    same row and building them apart is how they drift.
    """
    from freeweight.infrastructure.db.models import Machine, ModelDescriptor, RuntimeProfile
    from freeweight.infrastructure.db.models_runs import BenchmarkSuite

    run = _resolve(session, run_ref)
    suite = session.get(BenchmarkSuite, run.suite_id)
    model = ModelRepository().get_by_id(session, run.model_id)
    descriptor = session.get(ModelDescriptor, run.model_descriptor_id)
    profile = session.get(RuntimeProfile, run.runtime_profile_id)
    machine = session.get(Machine, run.machine_id)
    if suite is None or model is None or profile is None or machine is None:
        raise SubjectNotFound(
            f"Run {run.id!r} is missing the identity rows a comparison needs.",
            details={"run": run.id},
        )

    identity = ModelIdentity(
        provider_kind=ProviderKind(model.provider_kind),
        provider_model_name=model.provider_model_name,
        artifact_digest=model.artifact_digest,
    )
    subject = MeasurementSubject(
        identity=identity,
        runtime_profile_hash=profile.profile_hash,
        machine_fingerprint=machine.machine_fingerprint,
    )
    dataset_hashes = (
        {str(name): str(value) for name, value in dict(suite.dataset_hashes_json).items()}
        if isinstance(suite.dataset_hashes_json, dict)
        else {}
    )
    document = (
        dict(run.fingerprint_document_json)
        if isinstance(run.fingerprint_document_json, dict)
        else {}
    )
    label = run.label or model.canonical_id
    column = RunColumn(
        run_id=run.id,
        label=label,
        model_canonical_id=model.canonical_id,
        suite_key=suite.key,
        suite_version=suite.version,
        quantization=descriptor.quantization if descriptor is not None else None,
        family=descriptor.family if descriptor is not None else None,
        runtime_profile_hash=profile.profile_hash,
        kv_cache_precision=profile.kv_cache_precision,
        context_size=profile.context_size,
        machine_fingerprint=machine.machine_fingerprint,
        machine_hostname=machine.hostname,
        gpu_index=run.gpu_index,
        started_at_rfc3339=_rfc3339(run.started_at),
        fingerprint=run.reproducibility_fingerprint,
        identity_confidence=model.identity_confidence,
    )

    rows = [
        row
        for row in MetricValueRepository().list_for_run(session, run.id)
        if row.run_test_id is None and row.sample_id is None
    ]
    return _Loaded(
        column=column,
        subject=ComparisonSubject(
            run_id=run.id,
            subject=subject,
            benchmark_key=suite.key,
            benchmark_version=suite.version,
            dataset_hashes=dataset_hashes,
            fingerprint=run.reproducibility_fingerprint,
            fingerprint_document=document,
            family=column.family,
            quantization=column.quantization,
            label=label,
        ),
        metrics={row.metric_key: row for row in rows},
        definitions={row.metric_key: (row.unit, bool(row.higher_is_better)) for row in rows},
    )


def _rfc3339(value: Any) -> str | None:  # noqa: ANN401 — a datetime or None from an ORM column
    """Render a stored instant as RFC 3339, or ``None``."""
    return None if value is None else to_rfc3339(value)


REASON_METRIC_NOT_PRODUCED = "metric_not_produced_by_this_run"
"""This run's suite never produced this metric.

A distinct reason from every ADR-0016 reason a *run* can record: those say "this machine could not
measure it", and this says "this run was not asked to". Both render as ``—``; only one of them is
a gap in the measurement."""


def _cell(
    run_id: str,
    row: Any,
    *,
    unit: str,
    group_index: int,  # noqa: ANN401 — a metric_values row
) -> MetricCell:
    """Build one table cell from one stored metric row, or from its absence."""
    if row is None:
        return MetricCell(
            run_id=run_id,
            numeric_value=None,
            unavailable_reason=REASON_METRIC_NOT_PRODUCED,
            unit=unit,
            sample_count=None,
            excluded_count=None,
            stddev=None,
            coefficient_of_variation=None,
            gpu_index=None,
            group_index=group_index,
        )
    return MetricCell(
        run_id=run_id,
        numeric_value=row.numeric_value,
        unavailable_reason=row.unavailable_reason,
        unit=row.unit,
        sample_count=row.sample_count,
        excluded_count=row.excluded_count,
        stddev=row.stddev,
        coefficient_of_variation=row.coefficient_of_variation,
        gpu_index=row.gpu_index,
        group_index=group_index,
    )


def _overall_study(separations: Sequence[PairVerdict]) -> StudyKind:
    """Name the whole comparison in one word.

    The *first* separation in the matrix's own order wins, so a quantization study whose runs also
    sat on different machines is still a quantization study — the axis the user asked about is the
    one the page should be titled with.
    """
    for kind in (
        StudyKind.INCOMPARABLE,
        StudyKind.QUANTIZATION_STUDY,
        StudyKind.RUNTIME_STUDY,
        StudyKind.MACHINE_STUDY,
        StudyKind.UNRELATED,
    ):
        if any(verdict.study is kind for verdict in separations):
            return kind
    return StudyKind.DIRECT


def compare_runs(database: Database, run_refs: Sequence[str]) -> Comparison:
    """Assemble the comparison of two or more runs.

    Args:
        database: The application's database handle.
        run_refs: Full ULIDs or unambiguous prefixes, in the order the caller wants the columns.

    Returns:
        The comparison: one column per run, one row per metric key any of them produced, and the
        separations that stop the columns from being averaged.

    Raises:
        ComparisonRefused: Fewer than two distinct runs were named. Two runs that *cannot* be
            compared are not this — that is a result, and it is returned with the reason and the
            fingerprint diff attached.
        SubjectNotFound: A reference matches no run.
        ValidationError: A reference is an ambiguous prefix; the message names the candidates.
    """
    if len(run_refs) < _MINIMUM_SUBJECTS:
        raise ComparisonRefused(
            f"A comparison needs at least {_MINIMUM_SUBJECTS} runs; got {len(run_refs)}.",
            details={"subjects": list(run_refs)},
        )

    with database.read() as session:
        loaded = [_load(session, reference) for reference in run_refs]

    run_ids = [item.column.run_id for item in loaded]
    if len(set(run_ids)) != len(run_ids):
        raise ComparisonRefused(
            "A comparison names the same run more than once; a run is not a comparison with "
            "itself.",
            details={"subjects": run_ids},
        )

    subjects = [item.subject for item in loaded]
    definitions: dict[str, tuple[str, bool]] = {}
    for item in loaded:
        for key, definition in item.definitions.items():
            definitions.setdefault(key, definition)

    rows: list[MetricRow] = []
    for metric_key in sorted(definitions):
        unit, higher_is_better = definitions[metric_key]
        kind = metric_kind_for(metric_key)
        groups, verdicts = group_subjects(subjects, metric_kind=kind)
        position = {run_id: index for index, group in enumerate(groups) for run_id in group.members}
        rows.append(
            MetricRow(
                metric_key=metric_key,
                unit=unit,
                higher_is_better=higher_is_better,
                kind=kind,
                cells=tuple(
                    _cell(
                        item.column.run_id,
                        item.metrics.get(metric_key),
                        unit=unit,
                        group_index=position.get(item.column.run_id, 0),
                    )
                    for item in loaded
                ),
                groups=groups,
                verdicts=verdicts,
            )
        )

    separations = _separations(subjects)
    columns = tuple(item.column for item in loaded)
    return Comparison(
        columns=columns,
        rows=tuple(rows),
        separations=separations,
        study=_overall_study(separations),
        context_sweep=_context_sweep(columns, rows),
    )


def _context_sweep(columns: Sequence[RunColumn], rows: Sequence[MetricRow]) -> ContextSweep | None:
    """Fit the KV cost function, when these runs happen to be a context sweep of one model.

    The conditions are strict, and each rules out a fit that would be a number rather than a
    measurement:

    * **one model, one machine, one suite** — differencing VRAM across two models measures the
      difference between the models, not the cost of context;
    * **at least three distinct served contexts** — two points fit a line exactly and report
      ``r_squared`` of 1.0 by construction;
    * **every run reports** :data:`KV_SWEEP_METRIC` — the *model's* residency, not the device's
      total, which is what makes this an isolation rather than a mitigation.

    Args:
        columns: The comparison's columns.
        rows: Its metric rows, already assembled.

    Returns:
        The sweep, or ``None`` when these runs are not one. ``None`` is the ordinary answer and is
        not a failure — most comparisons are not context sweeps.
    """
    from freeweight.benchmarks.memory_kv.kv import ContextObservation, fit_context_slope

    if len({column.model_canonical_id for column in columns}) != 1:
        return None
    if len({column.machine_fingerprint for column in columns}) != 1:
        return None
    if len({column.suite_key for column in columns}) != 1:
        return None

    row = next((item for item in rows if item.metric_key == KV_SWEEP_METRIC), None)
    if row is None:
        return None
    residency = {
        cell.run_id: cell.numeric_value for cell in row.cells if cell.numeric_value is not None
    }

    points: dict[int, float] = {}
    for column in columns:
        value = residency.get(column.run_id)
        if column.context_size is None or value is None:
            continue
        # One point per context. Two runs at the same context are a repeatability check, not a
        # second point on the line, and averaging them here would hide that they disagreed.
        points.setdefault(int(column.context_size), float(value))
    if len(points) < MINIMUM_SWEEP_POINTS:
        return None

    fit = fit_context_slope(
        [
            ContextObservation(context_tokens=context, vram_used_bytes=value)
            for context, value in sorted(points.items())
        ]
    )
    figures = (
        fit.intercept_bytes,
        fit.slope_bytes_per_token,
        fit.r_squared,
        fit.residual_stddev_bytes,
    )
    if not all(is_supported(item.value) for item in figures):
        # A refused fit is offered as no sweep at all rather than as a sweep full of em dashes:
        # the reason lives on `native.memory_kv`'s own metrics, and repeating it here would be a
        # second place to read the same refusal.
        return None
    weights, slope, fit_quality, spread = (float(item.value) for item in figures)
    return ContextSweep(
        model_canonical_id=columns[0].model_canonical_id,
        points=tuple(sorted(points.items())),
        weights_bytes=weights,
        bytes_per_token=slope,
        r_squared=fit_quality,
        residual_stddev_bytes=spread,
    )


def _separations(subjects: Sequence[ComparisonSubject]) -> tuple[PairVerdict, ...]:
    """Every pair that is separated regardless of which metric is being looked at.

    Evaluated at :attr:`~baseaicore.MetricKind.QUALITY`, which is the *most permissive* row of the
    matrix: a pair separated even for quality is separated for everything, and that is what
    belongs on a comparison-wide banner. A pair separated only for performance is a per-row fact
    and is reported on the row, where the metric that caused it is visible.
    """
    verdicts: list[PairVerdict] = []
    for index, left in enumerate(subjects):
        for right in subjects[index + 1 :]:
            verdict = verdict_for_pair(left, right, metric_kind=MetricKind.QUALITY)
            if verdict.comparability is not Comparability.COMPARABLE:
                verdicts.append(
                    PairVerdict(
                        left=verdict.left,
                        right=verdict.right,
                        comparability=verdict.comparability,
                        study=verdict.study,
                        reason=verdict.reason,
                        metric_kind=verdict.metric_kind,
                        diff=verdict.diff or separation_diff(left, right),
                    )
                )
    return tuple(verdicts)


def enforce_suite(comparison: Comparison, suite: str | None) -> None:
    """Refuse a comparison whose subjects are not all the suite the caller named.

    Args:
        comparison: The assembled comparison.
        suite: The suite key every subject must belong to, or ``None`` to skip the check.

    Raises:
        ComparisonRefused: A subject runs a different suite. Named individually, because "one of
            these is wrong" is not something a person can act on.
    """
    if not suite:
        return
    offenders = [
        {"run_id": column.run_id, "suite": column.suite_key}
        for column in comparison.columns
        if column.suite_key != suite
    ]
    if offenders:
        raise ComparisonRefused(
            f"Every subject must be a run of {suite!r}; "
            f"{len(offenders)} of {len(comparison.columns)} are not.",
            details={"suite": suite, "offenders": offenders},
        )


def comparison_json(comparison: Comparison) -> dict[str, Any]:
    """Render a comparison as the API document.

    Lives here rather than on either surface because both of them must emit the *same* field
    names: CLI standards §3 requires ``--json`` to match the HTTP API exactly, and two
    renderers maintained apart is how that requirement stops being true.

    Args:
        comparison: The assembled comparison.

    Returns:
        The document, JSON-safe throughout.
    """
    return {
        "study": comparison.study.value,
        "subjects": [
            {
                "run_id": column.run_id,
                "label": column.label,
                "model": column.model_canonical_id,
                "suite": column.suite_key,
                "suite_version": column.suite_version,
                "family": column.family,
                "quantization": column.quantization,
                "runtime_profile_hash": column.runtime_profile_hash,
                "kv_cache_precision": column.kv_cache_precision,
                "context_size": column.context_size,
                "machine_fingerprint": column.machine_fingerprint,
                "machine_hostname": column.machine_hostname,
                "gpu_index": column.gpu_index,
                "started_at": column.started_at_rfc3339,
                "reproducibility_fingerprint": column.fingerprint,
                "identity_confidence": column.identity_confidence,
            }
            for column in comparison.columns
        ],
        "metrics": [
            {
                "metric_key": row.metric_key,
                "unit": row.unit,
                "higher_is_better": row.higher_is_better,
                "metric_kind": row.kind.value,
                "mergeable": row.mergeable,
                "best_run_id": row.best_run_id,
                "groups": [
                    {
                        "members": list(group.members),
                        "study": group.study.value,
                        "reason": group.reason,
                    }
                    for group in row.groups
                ],
                "cells": [
                    {
                        "run_id": cell.run_id,
                        "value": cell.numeric_value,
                        "unavailable_reason": cell.unavailable_reason,
                        "unit": cell.unit,
                        "sample_count": cell.sample_count,
                        "excluded_count": cell.excluded_count,
                        "stddev": cell.stddev,
                        "coefficient_of_variation": cell.coefficient_of_variation,
                        "gpu_index": cell.gpu_index,
                        "group_index": cell.group_index,
                    }
                    for cell in row.cells
                ],
            }
            for row in comparison.rows
        ],
        "separations": [
            {
                "left": verdict.left,
                "right": verdict.right,
                "comparability": verdict.comparability.value,
                "study": verdict.study.value,
                "reason": verdict.reason,
                "fingerprint_diff": [
                    {"path": entry.path, "left": entry.left, "right": entry.right}
                    for entry in verdict.diff
                ],
            }
            for verdict in comparison.separations
        ],
        "context_sweep": (
            {
                "model": comparison.context_sweep.model_canonical_id,
                "points": [
                    {"context_size": context, "model_vram_bytes": value}
                    for context, value in comparison.context_sweep.points
                ],
                "weights_bytes": comparison.context_sweep.weights_bytes,
                "bytes_per_token": comparison.context_sweep.bytes_per_token,
                "r_squared": comparison.context_sweep.r_squared,
                "residual_stddev_bytes": comparison.context_sweep.residual_stddev_bytes,
            }
            if comparison.context_sweep is not None
            else None
        ),
    }
