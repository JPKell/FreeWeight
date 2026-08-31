"""freeweight.services.results — the metric-level query and the dashboard's figures.

Two surfaces read from here: ``GET /api/v1/results`` with the results page behind it, and the
dashboard. They share a module because they share the one rule that makes either of them
trustworthy.

**The dashboard never computes a number.** Every figure it shows is a stored ``metric_values`` row
of one identified run — the *latest completed* run of that suite for that model — carried up with
its ``run_id``, its ``run_test_id``, its sample count and its exclusion count. Nothing here means
anything across runs, because averaging two runs is exactly where a dashboard starts telling a
story the samples do not support: two runs of one suite may have measured different benchmark
versions, on different hardware, at different context sizes, and their mean is a number about
nothing. ``tests/e2e/test_dashboard.py`` recomputes the headline figures straight from the raw
samples and asserts equality — the anti-lie test the phase names as this work's chief risk.

**A missing figure is a missing figure.** A model that has not run a suite has no cell, not a zero;
a metric the machine could not measure carries its ``unavailable_reason`` up to the UI, which
renders ``—`` and says why (ADR-0016 §4, UI standards §5).

**Latest-completed-run resolution lives here, once.** :func:`latest_completed_run` is what the
dashboard uses to fill a heatmap cell, and it is also the resolver
``GET /results/compare?subjects=<model>&suite=<key>`` needs so that ``subjects`` may name models
rather than runs — the ambiguity ``PHASE9_ISSUES.md`` §9 left open, closed here rather than in two
places that would drift.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import NotFoundError, SuiteError, ValidationError, to_rfc3339
from weightsdb import DatabaseUnavailable

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from freeweight.services.database import Database

__all__ = [
    "DEFAULT_RESULTS_LIMIT",
    "MAX_RESULTS_LIMIT",
    "Dashboard",
    "DashboardFilter",
    "HeatmapCell",
    "MetricHeatmap",
    "PanelRow",
    "ResultRow",
    "ResultsPage",
    "ResultsQuery",
    "ScatterPoint",
    "SummaryCards",
    "build_dashboard",
    "latest_completed_run",
    "query_results",
    "resolve_subject_runs",
]


@contextmanager
def _translated() -> Iterator[None]:
    """Translate raw driver failures into the suite's error hierarchy.

    The same guard :mod:`freeweight.services.runs` uses, and for the same reason: without it an
    unmigrated database reaches a route as ``sqlalchemy.exc.OperationalError`` and 500s a page
    that has a perfectly good error state to render. A :class:`~baseaicore.SuiteError` passes
    through unchanged.
    """
    try:
        yield
    except SuiteError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(f"Could not read the database: {exc}") from exc


class ModelNotFound(NotFoundError):
    """No model matches the given reference (spec §13, ``MODEL_NOT_FOUND``)."""

    code: ClassVar[str] = "MODEL_NOT_FOUND"


class SampleNotFound(NotFoundError):
    """No sample matches the given id.

    Its code is the generic ``NOT_FOUND`` rather than ``RUN_NOT_FOUND``: a sample id that resolves
    to nothing is a bad link, not a missing run, and giving it the run's code would send a client
    looking in the wrong place.
    """


DEFAULT_RESULTS_LIMIT = 50
"""API standards §6: ``limit`` defaults to 50."""

MAX_RESULTS_LIMIT = 500
"""API standards §6: ``limit`` is clamped to 500, and the response says so in ``page.limit``."""


@dataclass(frozen=True, slots=True)
class ResultRow:
    """One metric of one run, with everything a reader needs to judge comparability.

    The subject fields (model, machine, runtime profile, suite version) travel with the number
    because spec §11 contract 4 requires an exported or displayed result to let a consumer decide
    comparability without asking FreeWeight a second question.

    ``metric_value_id`` is carried because one run legitimately holds several rows under one
    ``metric_key`` — the run-level roll-up and one per test — so nothing shorter identifies a row.
    That is also why it is the last component of the sort key: without it the order is not total,
    and cursor pagination silently skips the second row of every repeated key.
    """

    metric_value_id: str
    run_id: str
    run_test_id: str | None
    run_test_key: str | None
    metric_key: str
    numeric_value: float | None
    unavailable_reason: str | None
    unit: str
    aggregation: str
    higher_is_better: bool
    sample_count: int
    excluded_count: int
    stddev: float | None
    coefficient_of_variation: float | None
    gpu_index: int | None
    model_canonical_id: str
    suite_key: str
    suite_version: str
    machine_fingerprint: str
    runtime_profile_hash: str
    run_status: str
    run_created_at: datetime
    label: str | None

    def as_json(self) -> dict[str, Any]:
        """The wire form ``GET /api/v1/results`` returns for one row."""
        return {
            "metric_value_id": self.metric_value_id,
            "run_id": self.run_id,
            "run_test_id": self.run_test_id,
            "run_test_key": self.run_test_key,
            "metric_key": self.metric_key,
            "value": "unsupported" if self.numeric_value is None else self.numeric_value,
            "unavailable_reason": self.unavailable_reason,
            "unit": self.unit,
            "aggregation": self.aggregation,
            "higher_is_better": self.higher_is_better,
            "sample_count": self.sample_count,
            "excluded_count": self.excluded_count,
            "stddev": "unsupported" if self.stddev is None else self.stddev,
            "coefficient_of_variation": (
                "unsupported"
                if self.coefficient_of_variation is None
                else self.coefficient_of_variation
            ),
            "gpu_index": self.gpu_index,
            "model": self.model_canonical_id,
            "suite": self.suite_key,
            "suite_version": self.suite_version,
            "machine_fingerprint": self.machine_fingerprint,
            "runtime_profile_hash": self.runtime_profile_hash,
            "run_status": self.run_status,
            "created_at": to_rfc3339(self.run_created_at),
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class ResultsQuery:
    """Filters for the metric-level query (API §5).

    Every filter is an explicit named parameter, never a query DSL (API standards §6).

    Attributes:
        model: Model canonical ID, ULID or unambiguous prefix.
        suite: Benchmark suite key.
        metric_key: Exact metric key.
        machine: Machine fingerprint.
        runtime_profile: Runtime profile hash.
        since: Only runs created at or after this instant.
        until: Only runs created strictly before this instant.
        status: Only runs in this state. Defaults to ``completed``, because a metric from a run
            that failed halfway is a partial measurement and showing it beside a whole one, with
            nothing to distinguish them, is the quiet kind of lie.
        limit: Page size, clamped to :data:`MAX_RESULTS_LIMIT`.
        cursor: Opaque continuation token from a previous page.
    """

    model: str | None = None
    suite: str | None = None
    metric_key: str | None = None
    machine: str | None = None
    runtime_profile: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    status: str | None = "completed"
    limit: int = DEFAULT_RESULTS_LIMIT
    cursor: str | None = None

    def clamped_limit(self) -> int:
        """The effective page size, clamped into ``[1, MAX_RESULTS_LIMIT]``."""
        return max(1, min(self.limit, MAX_RESULTS_LIMIT))


@dataclass(frozen=True, slots=True)
class ResultsPage:
    """One page of metric rows.

    Attributes:
        rows: The rows, in a total order: run creation descending, then run ID, then metric key,
            so cursor pagination can neither skip nor repeat (API standards §6).
        limit: The limit actually applied, which may be lower than the one requested.
        next_cursor: The token for the following page, or ``None`` at the end.
        has_more: Always present, per the collection envelope.
    """

    rows: tuple[ResultRow, ...]
    limit: int
    next_cursor: str | None
    has_more: bool

    def as_json(self) -> dict[str, Any]:
        """The collection envelope API standards §3 requires."""
        return {
            "items": [row.as_json() for row in self.rows],
            "page": {
                "limit": self.limit,
                "next_cursor": self.next_cursor,
                "has_more": self.has_more,
            },
        }


def _encode_cursor(row: ResultRow) -> str:
    """Encode the sort key of ``row`` as an opaque cursor.

    Opaque base64 of the stable sort key, never constructed by a client (API standards §6). It
    carries all three components of the total order, so a page boundary that lands in the middle
    of one run's metrics resumes inside that run rather than skipping the rest of it.

    The timestamp is ISO 8601 at **full stored precision**, not the suite's RFC 3339 rendering.
    That rendering truncates to milliseconds, which is correct for a document a person reads and
    wrong for a sort key: a cursor built from a truncated instant matches neither ``<`` nor ``==``
    against the microsecond value in the column, and the next page silently comes back empty.
    """
    payload = json.dumps(
        {
            "created_at": row.run_created_at.isoformat(),
            "run_id": row.run_id,
            "metric_key": row.metric_key,
            "metric_value_id": row.metric_value_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str, str, str]:
    """Decode a cursor, refusing anything this build did not issue.

    Raises:
        ValidationError: The cursor is not one of ours.
    """
    padding = "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        return (
            datetime.fromisoformat(payload["created_at"]),
            str(payload["run_id"]),
            str(payload["metric_key"]),
            str(payload["metric_value_id"]),
        )
    except Exception as exc:  # noqa: BLE001 — every malformed cursor is one validation error
        raise ValidationError(
            "That cursor was not issued by this API. Drop it and start from the first page.",
            details={"field": "cursor"},
        ) from exc


def _base_statement(session: Session, query: ResultsQuery) -> Any:  # noqa: ANN401 — a Select
    """Build the joined SELECT the metric-level query runs, filters applied."""
    from sqlalchemy import select

    from freeweight.infrastructure.db.models import Machine, Model, RuntimeProfile
    from freeweight.infrastructure.db.models_runs import (
        BenchmarkSuite,
        BenchmarkTestRow,
        MetricValue,
        Run,
        RunTest,
    )

    statement = (
        select(
            MetricValue,
            Run,
            BenchmarkSuite.key,
            BenchmarkSuite.version,
            Model.canonical_id,
            Machine.machine_fingerprint,
            RuntimeProfile.profile_hash,
            BenchmarkTestRow.key,
        )
        .join(Run, Run.id == MetricValue.run_id)
        .join(BenchmarkSuite, BenchmarkSuite.id == Run.suite_id)
        .join(Model, Model.id == Run.model_id)
        .join(Machine, Machine.id == Run.machine_id)
        .join(RuntimeProfile, RuntimeProfile.id == Run.runtime_profile_id)
        .outerjoin(RunTest, RunTest.id == MetricValue.run_test_id)
        .outerjoin(BenchmarkTestRow, BenchmarkTestRow.id == RunTest.test_id)
        .where(MetricValue.sample_id.is_(None))
        .order_by(
            Run.created_at.desc(),
            Run.id.desc(),
            MetricValue.metric_key.asc(),
            MetricValue.id.asc(),
        )
    )
    if query.status:
        statement = statement.where(Run.status == query.status)
    if query.suite:
        statement = statement.where(BenchmarkSuite.key == query.suite)
    if query.metric_key:
        statement = statement.where(MetricValue.metric_key == query.metric_key)
    if query.machine:
        statement = statement.where(Machine.machine_fingerprint == query.machine)
    if query.runtime_profile:
        statement = statement.where(RuntimeProfile.profile_hash == query.runtime_profile)
    if query.since is not None:
        statement = statement.where(Run.created_at >= query.since)
    if query.until is not None:
        statement = statement.where(Run.created_at < query.until)
    if query.model:
        model_id = _resolve_model_id(session, query.model)
        statement = statement.where(Run.model_id == model_id)
    return statement


def _resolve_model_id(session: Session, reference: str) -> str:
    """Resolve a model reference to a ``models.id``, or refuse by name.

    Accepts the same four forms every other surface does — canonical ID, ULID, unambiguous ULID
    prefix, or the provider's own model name — so a filter can be typed with whatever string the
    run was started with.

    Raises:
        ModelNotFound: Nothing matches.
        ValidationError: The reference is an ambiguous prefix.
    """
    from freeweight.infrastructure.db.repositories.models import ModelRepository

    repository = ModelRepository()
    candidates = repository.get_by_id_prefix(session, reference)
    if len(candidates) == 1:
        return str(candidates[0].id)
    if len(candidates) > 1:
        raise ValidationError(
            f"{reference!r} matches {len(candidates)} models; use more characters.",
            details={"model": reference, "candidates": [row.id for row in candidates]},
        )
    row = (
        repository.get_by_canonical_id(session, reference)
        or repository.get_by_id(session, reference)
        or repository.get_by_provider_model_name(session, reference)
    )
    if row is not None:
        return str(row.id)
    raise ModelNotFound(f"No model matches {reference!r}.", details={"model": reference})


def _row_from(record: Any) -> ResultRow:  # noqa: ANN401 — one row of the joined SELECT
    """Build a :class:`ResultRow` from one joined record."""
    (
        metric,
        run,
        suite_key,
        suite_version,
        canonical_id,
        machine_fingerprint,
        profile_hash,
        test_key,
    ) = record
    return ResultRow(
        metric_value_id=metric.id,
        run_id=run.id,
        run_test_id=metric.run_test_id,
        run_test_key=test_key,
        metric_key=metric.metric_key,
        numeric_value=metric.numeric_value,
        unavailable_reason=metric.unavailable_reason,
        unit=metric.unit,
        aggregation=metric.aggregation,
        higher_is_better=bool(metric.higher_is_better),
        sample_count=metric.sample_count or 0,
        excluded_count=metric.excluded_count or 0,
        stddev=metric.stddev,
        coefficient_of_variation=metric.coefficient_of_variation,
        gpu_index=metric.gpu_index,
        model_canonical_id=canonical_id,
        suite_key=suite_key,
        suite_version=suite_version,
        machine_fingerprint=machine_fingerprint,
        runtime_profile_hash=profile_hash,
        run_status=run.status,
        run_created_at=run.created_at,
        label=run.label,
    )


def query_results(database: Database, query: ResultsQuery) -> ResultsPage:
    """Run the metric-level query behind ``GET /api/v1/results``.

    Cursor-paginated over a total order, so a page boundary is stable while runs are being
    written. One extra row is fetched to decide ``has_more`` without a second COUNT — a count over
    a filtered join of six tables is the expensive half of this query, and ``total`` is documented
    as optional precisely so it can be skipped (API standards §3).

    Args:
        database: The application's database handle.
        query: The filters and the page request.

    Returns:
        One :class:`ResultsPage`.

    Raises:
        NotFoundError: ``query.model`` matches no model.
        ValidationError: ``query.model`` is an ambiguous prefix, or ``query.cursor`` was not
            issued by this API.
    """
    limit = query.clamped_limit()
    with _translated(), database.read() as session:
        statement = _base_statement(session, query)
        if query.cursor:
            from sqlalchemy import or_

            from freeweight.infrastructure.db.models_runs import MetricValue, Run

            created_at, run_id, metric_key, metric_value_id = _decode_cursor(query.cursor)
            same_run = (Run.created_at == created_at) & (Run.id == run_id)
            statement = statement.where(
                or_(
                    Run.created_at < created_at,
                    (Run.created_at == created_at) & (Run.id < run_id),
                    same_run & (MetricValue.metric_key > metric_key),
                    same_run
                    & (MetricValue.metric_key == metric_key)
                    & (MetricValue.id > metric_value_id),
                )
            )
        records = list(session.execute(statement.limit(limit + 1)))
        rows = [_row_from(record) for record in records]
    has_more = len(rows) > limit
    page = tuple(rows[:limit])
    return ResultsPage(
        rows=page,
        limit=limit,
        next_cursor=_encode_cursor(page[-1]) if has_more and page else None,
        has_more=has_more,
    )


def latest_completed_run(database: Database, *, model_reference: str, suite_key: str) -> str | None:
    """The most recent **completed** run of ``suite_key`` for ``model_reference``.

    "Latest completed" and not "latest": a run that failed halfway measured part of a suite, and
    a dashboard cell filled from one would be a figure over a different set of cases than the cell
    beside it.

    Args:
        database: The application's database handle.
        model_reference: Canonical ID, ULID or unambiguous prefix.
        suite_key: The benchmark suite key.

    Returns:
        The run ID, or ``None`` when this model has never completed that suite.

    Raises:
        NotFoundError: No model matches ``model_reference``.
        ValidationError: ``model_reference`` is an ambiguous prefix.
    """
    from sqlalchemy import select

    from freeweight.infrastructure.db.models_runs import BenchmarkSuite, Run

    with _translated(), database.read() as session:
        model_id = _resolve_model_id(session, model_reference)
        return session.scalars(
            select(Run.id)
            .join(BenchmarkSuite, BenchmarkSuite.id == Run.suite_id)
            .where(
                Run.model_id == model_id,
                BenchmarkSuite.key == suite_key,
                Run.status == "completed",
            )
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(1)
        ).first()


def resolve_subject_runs(
    database: Database, references: Sequence[str], *, suite: str | None
) -> tuple[str, ...]:
    """Resolve ``subjects`` for a comparison, accepting runs **or** models.

    ``GET /results/compare?subjects=a,b,c&suite=…`` is documented in api.md without saying which
    kind of thing a subject is, and Phase 9 shipped the run reading with ``suite`` as a guard.
    This is the other reading, added rather than substituted: a reference that resolves to a run
    is that run, and a reference that does not — but names a model — resolves to that model's
    latest completed run **of ``suite``**. Naming a model without a suite is refused, because
    "compare these two models" has no answer until someone says at what.

    Args:
        database: The application's database handle.
        references: The subject references, in the order the caller gave them.
        suite: The suite key, when one was given.

    Returns:
        Run IDs, in the caller's order.

    Raises:
        NotFoundError: A reference matches neither a run nor a model, or names a model that has
            never completed ``suite``.
        ValidationError: A model reference was given with no ``suite``, or a reference is an
            ambiguous prefix.
    """
    from freeweight.infrastructure.db.repositories.runs import RunRepository

    resolved: list[str] = []
    with _translated(), database.read() as session:
        repository = RunRepository()
        for reference in references:
            exact = repository.get_by_id(session, reference)
            if exact is not None:
                resolved.append(str(exact.id))
                continue
            matches = repository.get_by_id_prefix(session, reference)
            if len(matches) == 1:
                resolved.append(str(matches[0].id))
                continue
            if len(matches) > 1:
                raise ValidationError(
                    f"{reference!r} matches {len(matches)} runs; use more characters.",
                    details={"subject": reference, "candidates": [row.id for row in matches]},
                )
            resolved.append(_model_subject(database, session, reference, suite))
    return tuple(resolved)


def _model_subject(database: Database, session: Session, reference: str, suite: str | None) -> str:
    """Resolve one subject that is not a run: it must be a model, and it needs a suite."""
    _resolve_model_id(session, reference)
    if not suite:
        raise ValidationError(
            f"{reference!r} names a model, so the comparison needs a suite: add "
            "?suite=<key> to say what to compare them at.",
            details={"subject": reference, "field": "suite"},
        )
    run_id = latest_completed_run(database, model_reference=reference, suite_key=suite)
    if run_id is None:
        from freeweight.services.runs import RunNotFound

        raise RunNotFound(
            f"{reference!r} has no completed run of {suite!r} to compare.",
            details={"subject": reference, "suite": suite},
        )
    return run_id


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DashboardFilter:
    """What the dashboard is scoped to.

    Attributes:
        suite: Restrict every panel to one suite key.
        model: Restrict every panel to one model reference.
        machine: Restrict to one machine fingerprint. Performance, VRAM and energy figures are
            only comparable within a machine, so this filter is how a two-machine database stops
            looking like one confusing one.
        since: Only runs created at or after this instant.
    """

    suite: str | None = None
    model: str | None = None
    machine: str | None = None
    since: datetime | None = None

    def as_query(self, *, metric_key: str | None = None) -> ResultsQuery:
        """The metric-level query this filter implies."""
        return ResultsQuery(
            model=self.model,
            suite=self.suite,
            metric_key=metric_key,
            machine=self.machine,
            since=self.since,
            status="completed",
            limit=MAX_RESULTS_LIMIT,
        )


@dataclass(frozen=True, slots=True)
class SummaryCards:
    """The four questions the dashboard answers before a user scrolls.

    Attributes:
        completed_runs: Completed runs in scope.
        models_measured: Distinct models with at least one completed run in scope.
        suites_run: Distinct suites with at least one completed run in scope.
        samples_stored: Raw samples behind those runs — the denominator under everything else.
        unsupported_metrics: Metric rows in scope that this machine could not measure. Shown as a
            card of its own rather than hidden: the size of what could **not** be measured is a
            headline fact about a machine, not a footnote.
        machines: Distinct machines in scope. More than one means the performance panels span an
            incomparable boundary, and the UI says so.
        latest_run_at: When the most recent completed run in scope finished.
    """

    completed_runs: int
    models_measured: int
    suites_run: int
    samples_stored: int
    unsupported_metrics: int
    machines: int
    latest_run_at: datetime | None


@dataclass(frozen=True, slots=True)
class HeatmapCell:
    """One model × suite cell, filled from exactly one run.

    Attributes:
        run_id: The run this figure came from. Present so the cell links to its raw source, which
            is what makes "no headline metric is more than two interactions away" true.
        run_test_id: The test within it, when the metric is a test-level one.
        value: The stored figure, or ``None`` when the run could not measure it.
        unavailable_reason: Why, when ``value`` is ``None``.
        sample_count: Supported samples behind the figure.
        excluded_count: Samples excluded from it.
    """

    model_canonical_id: str
    suite_key: str
    metric_key: str
    run_id: str
    run_test_id: str | None
    value: float | None
    unavailable_reason: str | None
    unit: str
    higher_is_better: bool
    sample_count: int
    excluded_count: int
    machine_fingerprint: str
    suite_version: str


@dataclass(frozen=True, slots=True)
class MetricHeatmap:
    """The comparison heatmap: models down, suites across, one headline metric per suite.

    Attributes:
        models: Row order — models with at least one cell.
        suites: Column order — suites with at least one cell.
        cells: Keyed ``(model, suite)``. A missing key is a missing measurement and renders as an
            empty cell, never as a zero.
        headline_metric: Which metric each suite contributes, so the column header can say.
        separated: ``True`` when the cells span more than one machine or more than one version of
            a suite, in which case the UI must label the boundary rather than let the eye compare
            across it (UI standards §5).
    """

    models: tuple[str, ...]
    suites: tuple[str, ...]
    cells: Mapping[tuple[str, str], HeatmapCell]
    headline_metric: Mapping[str, str]
    separated: bool

    def cell(self, model: str, suite: str) -> HeatmapCell | None:
        """The cell at ``(model, suite)``, or ``None`` when it was never measured."""
        return self.cells.get((model, suite))


@dataclass(frozen=True, slots=True)
class ScatterPoint:
    """One point of a two-metric scatter, carrying the run it came from."""

    label: str
    run_id: str
    x: float
    y: float
    x_metric: str
    y_metric: str
    x_unit: str
    y_unit: str
    machine_fingerprint: str


@dataclass(frozen=True, slots=True)
class PanelRow:
    """One metric row in a dashboard panel, already resolved to its run."""

    model_canonical_id: str
    suite_key: str
    metric_key: str
    run_id: str
    run_test_id: str | None
    run_test_key: str | None
    value: float | None
    unavailable_reason: str | None
    unit: str
    higher_is_better: bool
    sample_count: int
    excluded_count: int


@dataclass(frozen=True, slots=True)
class Panel:
    """One dashboard section.

    Attributes:
        key: Stable identifier, used as the section's DOM id so a link can deep-link to it.
        title: The heading.
        description: One sentence saying what the panel means, shown under the heading rather
            than in a tooltip, because a dense panel a reader cannot interpret is decoration.
        rows: The figures, newest run first.
    """

    key: str
    title: str
    description: str
    rows: tuple[PanelRow, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether this panel has nothing to show yet."""
        return not self.rows


@dataclass(frozen=True, slots=True)
class Dashboard:
    """Everything the dashboard renders, already resolved."""

    filter: DashboardFilter
    cards: SummaryCards
    heatmap: MetricHeatmap
    quality_vs_speed: tuple[ScatterPoint, ...]
    quality_vs_vram: tuple[ScatterPoint, ...]
    panels: tuple[Panel, ...]
    suites_available: tuple[str, ...] = ()
    models_available: tuple[str, ...] = ()
    machines_available: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing measured in scope at all."""
        return self.cards.completed_runs == 0


GOAL_HEADLINE_METRIC = "composite_score"
"""The headline of any user-authored goal suite.

Every goal installs under ``goal.<slug>`` and every one of them declares ``composite_score`` as its
scored metric, so the heatmap resolves a goal column without the suite having to declare one. The
goal's rubric is in the *suite version* — a different ``goal_hash`` is a different version — so two
rubrics never share a cell."""


@lru_cache(maxsize=1)
def _headlines_from_manifests() -> Mapping[str, str]:
    """Read every shipped suite's declared ``headline_metric``.

    The declaration lives on the suite's own manifest, where the editorial judgement belongs: a
    heatmap needs one number per cell, and which of eleven ``native.judge`` metrics means "how good
    a judge is this" is a decision the suite owns. It used to be a table in this module, which meant
    a new suite was added in one place and forgotten in another — its column simply never appeared.

    Cached because it reads fifteen files and the answer cannot change inside one process.

    Returns:
        ``{suite key: metric key}`` for every shipped suite that declares one. A suite that
        declares none is absent, and gets no heatmap column — the honest outcome of nobody having
        decided, rather than a guess.
    """
    from freeweight.domain.benchmark import BenchmarkManifest

    root = Path(__file__).resolve().parents[1] / "benchmarks"
    headlines: dict[str, str] = {}
    for path in sorted(root.glob("*/manifest.json")):
        manifest = BenchmarkManifest.from_json(json.loads(path.read_text(encoding="utf-8")))
        if manifest.headline_metric:
            headlines[manifest.key] = manifest.headline_metric
    return headlines


def _headline_for(suite_key: str) -> str | None:
    """The headline metric of one suite, shipped or user-authored."""
    if suite_key.startswith("goal."):
        return GOAL_HEADLINE_METRIC
    return _headlines_from_manifests().get(suite_key)


_PANELS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "token-economy",
        "Token economy",
        "What a correct answer costs in tokens. Lower is cheaper; quality per 1k output tokens "
        "is the one to read when two models disagree about verbosity.",
        (
            "output_tokens_per_success",
            "total_tokens_per_success",
            "quality_per_1k_output_tokens",
            "successes_per_million_output_tokens",
        ),
    ),
    (
        "context",
        "Context",
        "How far the context window actually stretches on this machine, and what each 1k of it "
        "costs in memory.",
        (
            "max_successful_context_tokens",
            "observed_mb_per_1k_context",
            "kv_overhead_ratio",
            "kv_slope_fit_r_squared",
            "retrieval_accuracy",
        ),
    ),
    (
        "audit",
        "Audit precision and recall",
        "Finding real defects without inventing them. Precision and recall are shown together "
        "because either alone is trivially gamed.",
        (
            "precision",
            "recall",
            "f1",
            "clean_code_false_positive_rate",
            "line_localization_accuracy",
        ),
    ),
    (
        "tools",
        "Tool behaviour",
        "Whether the model picks the right tool, fills its arguments correctly, and recovers "
        "when a call fails.",
        (
            "tool_selection_accuracy",
            "argument_schema_validity",
            "argument_semantic_correctness",
            "hallucinated_tool_rate",
            "recovery_rate",
        ),
    ),
    (
        "judge-bias",
        "Judge bias",
        "How good this model is as an instrument. A juror that prefers the first answer, the "
        "longer answer, or its own answer is measurable, and these are the measurements.",
        (
            "pairwise_accuracy",
            "swap_consistency",
            "position_preference_rate",
            "verbosity_preference_rate",
            "self_preference_delta",
        ),
    ),
    (
        "goals",
        "Your goals",
        "Your own rubrics. score_method_mix sits beside every score, never instead of it: a 0.82 "
        "that is 80 % rules is a different fact from a 0.82 a jury produced, and the judge "
        "validity factor is how much the jury's agreement with you discounts it.",
        (
            "composite_score",
            "score_method_mix_rule",
            "score_method_mix_reference",
            "score_method_mix_human",
            "score_method_mix_judge",
            "judge_validity_factor",
            "applied_weight_share",
            "gated_sample_rate",
        ),
    ),
    (
        "energy",
        "Energy",
        "Joules per token and per successful task, integrated over the run's real timestamps. "
        "Comparable between runs of the same suite on the same machine; not an absolute figure.",
        (
            "gpu_energy_joules",
            "joules_per_output_token",
            "joules_per_successful_task",
            "output_tokens_per_joule",
            "peak_gpu_power_watts",
        ),
    ),
)
"""``(key, title, description, metric_keys)`` for every panel below the fold."""

QUALITY_METRICS: tuple[str, ...] = (
    "task_success",
    "defect_detection_score",
    "retrieval_accuracy",
    "strict_prompt_accuracy",
    "schema_conformance",
    "post_correction_accuracy",
    "tool_selection_accuracy",
    "harness_roundtrip_success",
    GOAL_HEADLINE_METRIC,
)
"""Metrics that stand for "how good was it" on the two scatter charts, in preference order."""

SPEED_METRIC = "decode_tokens_per_second"
VRAM_METRIC = "peak_vram_bytes"


def _summary_cards(session: Session, filter_: DashboardFilter) -> SummaryCards:
    """Count the four headline facts, plus the two that qualify them."""
    from sqlalchemy import func, select

    from freeweight.infrastructure.db.models import Machine, Model
    from freeweight.infrastructure.db.models_runs import (
        BenchmarkSuite,
        MetricValue,
        Run,
        RunTest,
        Sample,
    )

    conditions = [Run.status == "completed"]
    if filter_.since is not None:
        conditions.append(Run.created_at >= filter_.since)
    runs = (
        select(Run.id)
        .join(BenchmarkSuite, BenchmarkSuite.id == Run.suite_id)
        .join(Machine, Machine.id == Run.machine_id)
        .where(*conditions)
    )
    if filter_.suite:
        runs = runs.where(BenchmarkSuite.key == filter_.suite)
    if filter_.machine:
        runs = runs.where(Machine.machine_fingerprint == filter_.machine)
    if filter_.model:
        runs = runs.where(Run.model_id == _resolve_model_id(session, filter_.model))
    run_ids = runs.subquery()

    completed = session.execute(select(func.count()).select_from(run_ids)).scalar_one()
    models = session.execute(
        select(func.count(func.distinct(Run.model_id))).where(Run.id.in_(select(run_ids.c.id)))
    ).scalar_one()
    suites = session.execute(
        select(func.count(func.distinct(BenchmarkSuite.key)))
        .join(Run, Run.suite_id == BenchmarkSuite.id)
        .where(Run.id.in_(select(run_ids.c.id)))
    ).scalar_one()
    machines = session.execute(
        select(func.count(func.distinct(Run.machine_id))).where(Run.id.in_(select(run_ids.c.id)))
    ).scalar_one()
    samples = session.execute(
        select(func.count())
        .select_from(Sample)
        .join(RunTest, RunTest.id == Sample.run_test_id)
        .where(RunTest.run_id.in_(select(run_ids.c.id)))
    ).scalar_one()
    unsupported = session.execute(
        select(func.count())
        .select_from(MetricValue)
        .where(
            MetricValue.run_id.in_(select(run_ids.c.id)),
            MetricValue.numeric_value.is_(None),
            MetricValue.sample_id.is_(None),
        )
    ).scalar_one()
    latest = session.execute(
        select(func.max(Run.completed_at)).where(Run.id.in_(select(run_ids.c.id)))
    ).scalar_one()
    del Model
    return SummaryCards(
        completed_runs=int(completed),
        models_measured=int(models),
        suites_run=int(suites),
        samples_stored=int(samples),
        unsupported_metrics=int(unsupported),
        machines=int(machines),
        latest_run_at=latest,
    )


def _latest_rows(database: Database, filter_: DashboardFilter) -> tuple[ResultRow, ...]:
    """Every metric row of the **latest completed run** per (model, suite) in scope.

    One pass over the metric-level query, then a fold that keeps only the newest run per pair.
    Doing it this way rather than with a correlated subquery per cell keeps the dashboard to a
    small, fixed number of statements at any data volume, which is what
    ``tests/performance/test_dashboard_queries.py`` pins.
    """
    page = query_results(database, filter_.as_query())
    newest: dict[tuple[str, str], tuple[datetime, str]] = {}
    for row in page.rows:
        key = (row.model_canonical_id, row.suite_key)
        current = newest.get(key)
        if current is None or (row.run_created_at, row.run_id) > current:
            newest[key] = (row.run_created_at, row.run_id)
    keep = {run_id for _, run_id in newest.values()}
    return tuple(row for row in page.rows if row.run_id in keep)


def _heatmap(rows: Sequence[ResultRow]) -> MetricHeatmap:
    """Fold the latest rows into the model × suite comparison heatmap."""
    cells: dict[tuple[str, str], HeatmapCell] = {}
    headline: dict[str, str] = {}
    for row in rows:
        metric = _headline_for(row.suite_key)
        if metric is None or row.metric_key != metric:
            continue
        key = (row.model_canonical_id, row.suite_key)
        # A suite whose headline metric is emitted per test (native.performance emits
        # decode_tokens_per_second twice) contributes the run-level row when there is one, and
        # otherwise the first test-level row. Never both, and never their average.
        existing = cells.get(key)
        if existing is not None and existing.run_test_id is None:
            continue
        headline[row.suite_key] = metric
        cells[key] = HeatmapCell(
            model_canonical_id=row.model_canonical_id,
            suite_key=row.suite_key,
            metric_key=row.metric_key,
            run_id=row.run_id,
            run_test_id=row.run_test_id,
            value=row.numeric_value,
            unavailable_reason=row.unavailable_reason,
            unit=row.unit,
            higher_is_better=row.higher_is_better,
            sample_count=row.sample_count,
            excluded_count=row.excluded_count,
            machine_fingerprint=row.machine_fingerprint,
            suite_version=row.suite_version,
        )
    models = tuple(sorted({key[0] for key in cells}))
    suites = tuple(sorted({key[1] for key in cells}))
    machines = {cell.machine_fingerprint for cell in cells.values()}
    versions: dict[str, set[str]] = {}
    for cell in cells.values():
        versions.setdefault(cell.suite_key, set()).add(cell.suite_version)
    separated = len(machines) > 1 or any(len(seen) > 1 for seen in versions.values())
    return MetricHeatmap(
        models=models,
        suites=suites,
        cells=cells,
        headline_metric=headline,
        separated=separated,
    )


def _best_quality(rows: Iterable[ResultRow]) -> Mapping[str, ResultRow]:
    """The one quality figure per model, chosen by :data:`QUALITY_METRICS` preference order."""
    ranked: dict[str, tuple[int, ResultRow]] = {}
    order = {key: index for index, key in enumerate(QUALITY_METRICS)}
    for row in rows:
        rank = order.get(row.metric_key)
        if rank is None or row.numeric_value is None:
            continue
        current = ranked.get(row.model_canonical_id)
        if current is None or rank < current[0]:
            ranked[row.model_canonical_id] = (rank, row)
    return {model: row for model, (_, row) in ranked.items()}


def _scatter(rows: Sequence[ResultRow], *, against: str) -> tuple[ScatterPoint, ...]:
    """Pair each model's quality figure with ``against``, dropping models missing either.

    Dropped rather than plotted at zero: a model with no VRAM reading has not used no memory, and
    a point at the origin would say it had (ADR-0016).
    """
    quality = _best_quality(rows)
    other = {
        row.model_canonical_id: row
        for row in rows
        if row.metric_key == against and row.numeric_value is not None
    }
    points: list[ScatterPoint] = []
    for model, y_row in sorted(quality.items()):
        x_row = other.get(model)
        if x_row is None or x_row.numeric_value is None or y_row.numeric_value is None:
            continue
        points.append(
            ScatterPoint(
                label=model,
                run_id=y_row.run_id,
                x=float(x_row.numeric_value),
                y=float(y_row.numeric_value),
                x_metric=x_row.metric_key,
                y_metric=y_row.metric_key,
                x_unit=x_row.unit,
                y_unit=y_row.unit,
                machine_fingerprint=y_row.machine_fingerprint,
            )
        )
    return tuple(points)


def _panels(rows: Sequence[ResultRow]) -> tuple[Panel, ...]:
    """Fold the latest rows into the declared panels."""
    by_key: dict[str, list[ResultRow]] = {}
    for row in rows:
        by_key.setdefault(row.metric_key, []).append(row)
    panels: list[Panel] = []
    for key, title, description, metric_keys in _PANELS:
        selected: list[PanelRow] = []
        for metric_key in metric_keys:
            for row in by_key.get(metric_key, ()):
                selected.append(
                    PanelRow(
                        model_canonical_id=row.model_canonical_id,
                        suite_key=row.suite_key,
                        metric_key=row.metric_key,
                        run_id=row.run_id,
                        run_test_id=row.run_test_id,
                        run_test_key=row.run_test_key,
                        value=row.numeric_value,
                        unavailable_reason=row.unavailable_reason,
                        unit=row.unit,
                        higher_is_better=row.higher_is_better,
                        sample_count=row.sample_count,
                        excluded_count=row.excluded_count,
                    )
                )
        panels.append(Panel(key=key, title=title, description=description, rows=tuple(selected)))
    return tuple(panels)


def _available(session: Session) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The filter bar's option lists: every suite, model and machine that has a completed run."""
    from sqlalchemy import select

    from freeweight.infrastructure.db.models import Machine, Model
    from freeweight.infrastructure.db.models_runs import BenchmarkSuite, Run

    suites = sorted(
        set(
            session.scalars(
                select(BenchmarkSuite.key)
                .join(Run, Run.suite_id == BenchmarkSuite.id)
                .where(Run.status == "completed")
            )
        )
    )
    models = sorted(
        set(
            session.scalars(
                select(Model.canonical_id)
                .join(Run, Run.model_id == Model.id)
                .where(Run.status == "completed")
            )
        )
    )
    machines = sorted(
        set(
            session.scalars(
                select(Machine.machine_fingerprint)
                .join(Run, Run.machine_id == Machine.id)
                .where(Run.status == "completed")
            )
        )
    )
    return tuple(suites), tuple(models), tuple(machines)


def build_dashboard(database: Database, filter_: DashboardFilter) -> Dashboard:
    """Assemble everything the dashboard renders.

    Every figure is a stored metric row of one named run; nothing here averages, rescales or
    re-derives. That is the property ``tests/e2e/test_dashboard.py`` checks by recomputing the
    headline figures from the raw samples.

    Args:
        database: The application's database handle.
        filter_: What to scope to.

    Returns:
        The :class:`Dashboard`.

    Raises:
        NotFoundError: ``filter_.model`` matches no model.
        ValidationError: ``filter_.model`` is an ambiguous prefix.
    """
    rows = _latest_rows(database, filter_)
    with _translated(), database.read() as session:
        cards = _summary_cards(session, filter_)
        suites, models, machines = _available(session)
    return Dashboard(
        filter=filter_,
        cards=cards,
        heatmap=_heatmap(rows),
        quality_vs_speed=_scatter(rows, against=SPEED_METRIC),
        quality_vs_vram=_scatter(rows, against=VRAM_METRIC),
        panels=_panels(rows),
        suites_available=suites,
        models_available=models,
        machines_available=machines,
    )


@dataclass(frozen=True, slots=True)
class CaseInspection:
    """One raw sample with everything that produced and scored it (the case inspector).

    Attributes:
        sample: The stored sample.
        run: The run it belongs to.
        run_test_id: The test within that run.
        run_test_key: That test's key.
        tool_calls: Every tool call the sample made, in turn and call order.
        criterion_scores: Per-criterion goal scores, when the sample came from a goal suite.
        telemetry: The telemetry observations that fell inside the sample's window, with the
            caveat that the window is reconstructed rather than recorded — see
            ``PHASE9_ISSUES.md`` §3.
        prompt_text: The rendered prompt, when the run stored prompts.
    """

    sample: Any
    run: Any
    run_test_id: str
    run_test_key: str | None
    tool_calls: tuple[Any, ...] = ()
    criterion_scores: tuple[Any, ...] = ()
    telemetry: tuple[Any, ...] = ()
    prompt_text: str | None = None
    judge_verdicts: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)


def inspect_case(database: Database, sample_id: str) -> CaseInspection:
    """Read one sample and everything attached to it.

    This is the second of the two interactions UI standards §13 allows between a headline metric
    and its raw source, so it deliberately gathers *everything* in one query set rather than
    making the page fetch more: prompt, response, tool calls, per-criterion scores, the jurors'
    rationales, and the telemetry observed while it ran.

    Args:
        database: The application's database handle.
        sample_id: The sample to inspect.

    Returns:
        The :class:`CaseInspection`.

    Raises:
        NotFoundError: No sample has this ID.
    """
    from sqlalchemy import select

    from freeweight.infrastructure.db.models_goals import CriterionScore, JudgeVerdict
    from freeweight.infrastructure.db.models_runs import (
        BenchmarkTestRow,
        Run,
        RunTest,
        Sample,
        ToolCall,
    )

    with _translated(), database.read() as session:
        sample = session.get(Sample, sample_id)
        if sample is None:
            raise SampleNotFound(f"No sample matches {sample_id!r}.", details={"sample": sample_id})
        run_test = session.get(RunTest, sample.run_test_id)
        if run_test is None:  # pragma: no cover — a sample cannot outlive its test
            raise SampleNotFound(
                f"Sample {sample_id!r} has no run test.", details={"sample": sample_id}
            )
        run = session.get(Run, run_test.run_id)
        test_row = session.get(BenchmarkTestRow, run_test.test_id)
        tool_calls = tuple(
            session.scalars(
                select(ToolCall)
                .where(ToolCall.sample_id == sample.id)
                .order_by(ToolCall.turn_index, ToolCall.call_index)
            )
        )
        scores = tuple(
            session.scalars(
                select(CriterionScore)
                .where(CriterionScore.sample_id == sample.id)
                .order_by(CriterionScore.criterion_key)
            )
        )
        verdicts: dict[str, tuple[Any, ...]] = {}
        for score in scores:
            verdicts[score.criterion_key] = tuple(
                session.scalars(
                    select(JudgeVerdict)
                    .where(JudgeVerdict.criterion_score_id == score.id)
                    .order_by(JudgeVerdict.juror_ordinal, JudgeVerdict.repetition)
                )
            )
        telemetry = _telemetry_in_window(session, run_test.run_id, sample)
        session.expunge_all()
    return CaseInspection(
        sample=sample,
        run=run,
        run_test_id=run_test.id,
        run_test_key=test_row.key if test_row is not None else None,
        tool_calls=tool_calls,
        criterion_scores=scores,
        telemetry=telemetry,
        prompt_text=None,
        judge_verdicts=verdicts,
    )


def _telemetry_in_window(session: Session, run_id: str, sample: Any) -> tuple[Any, ...]:  # noqa: ANN401
    """GPU observations that fell inside one sample's request window.

    The window is reconstructed as ``created_at - client_wall_ms`` for the same reason
    :mod:`freeweight.services.runs` reconstructs it, and with the same honesty: a sample with no
    recorded wall time has a zero-length window and therefore no observations, rather than being
    handed the run's whole series. ``PHASE10_ISSUES.md`` records the stored ``started_at`` this
    approximation is waiting for.

    The timestamp lives on the *host* row — a device row is one GPU within one observation — so
    the window is applied through the join rather than to the device row directly.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from freeweight.infrastructure.db.models_runs import TelemetryGpuSample, TelemetrySample

    if sample.client_wall_ms is None or sample.created_at is None:
        return ()
    started = sample.created_at - timedelta(milliseconds=float(sample.client_wall_ms))
    return tuple(
        session.scalars(
            select(TelemetryGpuSample)
            .join(TelemetrySample, TelemetrySample.id == TelemetryGpuSample.telemetry_sample_id)
            .where(
                TelemetryGpuSample.run_id == run_id,
                TelemetrySample.timestamp >= started,
                TelemetrySample.timestamp <= sample.created_at,
            )
            .order_by(TelemetrySample.timestamp)
        )
    )
