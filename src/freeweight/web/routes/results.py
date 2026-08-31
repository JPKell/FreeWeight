"""freeweight.web.routes.results — the metric-level API, the export stream, and the drill-down.

Three surfaces, one module, because they are the three views of one thing: a stored measurement.

* ``GET /api/v1/results`` — the metric-level query (API §5), cursor-paginated.
* ``GET /api/v1/results/export`` — the streaming export (API §5, spec §7.3).
* ``GET /results`` and ``GET /results/samples/{sample_id}`` — the results table and the **case
  inspector**, which is the second of the two interactions UI standards §13 allows between a
  headline metric and the raw record that produced it.

The drill-down chain is deliberately short and deliberately linear: a dashboard figure links to a
run's test, and a sample row on that page links here. Two clicks, from any number on the
dashboard, to the prompt that was sent, the response that came back, the tool calls it made, the
per-criterion scoring and the telemetry observed while it ran.

Every handler is a plain ``def``: they all touch the database, and ADR-0003 rule 1 puts a
database-touching handler in the worker threadpool rather than on the event loop. The export is
the one that most needs it — it holds a session open across a stream — and Starlette runs a
synchronous iterator in the threadpool for exactly this reason.

No business logic lives here. The query, the pagination and the dashboard's own rules are in
:mod:`freeweight.services.results`; the export document is
:mod:`freeweight.services.export`'s. These functions parse a query string, call one service
function, and render.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from baseaicore import NotFoundError, SuiteError, ValidationError, from_rfc3339
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from weightsdb import DatabaseError

from freeweight.__about__ import __version__
from freeweight.services.export import (
    ExportFormat,
    ExportScope,
    ExportSelection,
    content_type_for,
    iter_export,
)
from freeweight.services.results import (
    DEFAULT_RESULTS_LIMIT,
    ResultsQuery,
    inspect_case,
    query_results,
)
from freeweight.web.rendering import render

if TYPE_CHECKING:
    from freeweight.services.database import Database

__all__ = ["api_router", "router"]

api_router = APIRouter(tags=["results"])
router = APIRouter(include_in_schema=False)

_ModelQuery = Annotated[str | None, Query(description="Model canonical ID, ULID or prefix.")]
_SuiteQuery = Annotated[str | None, Query(description="Benchmark suite key.")]
_MetricQuery = Annotated[str | None, Query(alias="metric_key", description="Exact metric key.")]
_MachineQuery = Annotated[str | None, Query(description="Machine fingerprint.")]
_ProfileQuery = Annotated[
    str | None, Query(alias="runtime_profile", description="Runtime profile hash.")
]
_SinceQuery = Annotated[str | None, Query(description="RFC 3339; runs created at or after.")]
_UntilQuery = Annotated[str | None, Query(description="RFC 3339; runs created strictly before.")]
_StatusQuery = Annotated[
    str | None,
    Query(description="Run status; defaults to completed. Pass 'any' to include every state."),
]
_LimitQuery = Annotated[int, Query(ge=1, le=500, description="Page size; clamped to 500.")]
_CursorQuery = Annotated[str | None, Query(description="Opaque cursor from a previous page.")]


def _instant(value: str | None, *, field: str) -> datetime | None:
    """Parse an RFC 3339 query parameter, or refuse it by name.

    Raises:
        ValidationError: The value is not RFC 3339.
    """
    if not value:
        return None
    try:
        return from_rfc3339(value)
    except Exception as exc:  # noqa: BLE001 — every parse failure is one validation error
        raise ValidationError(
            f"{field} must be an RFC 3339 instant, such as 2026-08-28T00:00:00Z; got {value!r}.",
            details={"field": field, "value": value},
        ) from exc


def _query_from(  # noqa: PLR0913 — this *is* the documented filter set
    *,
    model: str | None,
    suite: str | None,
    metric_key: str | None,
    machine: str | None,
    runtime_profile: str | None,
    since: str | None,
    until: str | None,
    status: str | None,
    limit: int,
    cursor: str | None,
) -> ResultsQuery:
    """Build the service query from the parsed parameters."""
    return ResultsQuery(
        model=model,
        suite=suite,
        metric_key=metric_key,
        machine=machine,
        runtime_profile=runtime_profile,
        since=_instant(since, field="since"),
        until=_instant(until, field="until"),
        status=None if status == "any" else (status or "completed"),
        limit=limit,
        cursor=cursor,
    )


@api_router.get("/results", summary="Stored metrics, filtered")
def list_results(  # noqa: PLR0913 — every argument is a documented query parameter
    request: Request,
    model: _ModelQuery = None,
    suite: _SuiteQuery = None,
    metric_key: _MetricQuery = None,
    machine: _MachineQuery = None,
    runtime_profile: _ProfileQuery = None,
    since: _SinceQuery = None,
    until: _UntilQuery = None,
    status: _StatusQuery = None,
    limit: _LimitQuery = DEFAULT_RESULTS_LIMIT,
    cursor: _CursorQuery = None,
) -> dict[str, Any]:
    """Return stored metric rows, newest run first (API §5).

    Only *completed* runs by default. A metric from a run that stopped halfway measured a
    different set of cases than the row beside it, and returning both under one filter would put
    the difference nowhere a caller could see it; ``status=any`` asks for them anyway.

    Args:
        request: The incoming request; the database handle lives on its application state.
        model: Model canonical ID, ULID or unambiguous prefix.
        suite: Benchmark suite key.
        metric_key: Exact metric key.
        machine: Machine fingerprint.
        runtime_profile: Runtime profile hash.
        since: RFC 3339 lower bound on run creation, inclusive.
        until: RFC 3339 upper bound on run creation, exclusive.
        status: Run status, or ``"any"``.
        limit: Page size, clamped to 500.
        cursor: Opaque continuation token.

    Returns:
        The collection envelope: ``items`` and ``page``.

    Raises:
        ValidationError: A timestamp is malformed, the cursor was not issued here, or ``model`` is
            an ambiguous prefix.
        NotFoundError: ``model`` matches nothing.
    """
    database: Database = request.app.state.database
    page = query_results(
        database,
        _query_from(
            model=model,
            suite=suite,
            metric_key=metric_key,
            machine=machine,
            runtime_profile=runtime_profile,
            since=since,
            until=until,
            status=status,
            limit=limit,
            cursor=cursor,
        ),
    )
    return page.as_json()


_FormatQuery = Annotated[
    str, Query(description="json, jsonl or csv.", pattern="^(json|jsonl|csv)$")
]
_ScopeQuery = Annotated[
    str,
    Query(
        description="run, model, suite, comparison or all.",
        pattern="^(run|model|suite|comparison|all)$",
    ),
]
_SelectorQuery = Annotated[
    str | None,
    Query(
        description=(
            "The scope's argument: a run reference, a model reference, a suite key, or a "
            "comma-separated run list. Omitted for scope=all."
        )
    ),
]


@api_router.get("/results/export", summary="Export stored results")
def export_results(  # noqa: PLR0913 — every argument is a documented query parameter
    request: Request,
    scope: _ScopeQuery = "all",
    selector: _SelectorQuery = None,
    format: _FormatQuery = "json",  # noqa: A002 — the documented parameter name (API §5)
    include_samples: bool = False,
    include_prompts: bool = False,
    include_prompt_text: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
) -> StreamingResponse:
    """Stream an export of stored results (API §5).

    The response streams: the first bytes leave before the last run is read, and the process never
    holds the whole document. That is what makes a 10 000-sample run exportable inside spec §15's
    budget rather than a memory spike proportional to how much someone has measured.

    JSON and JSONL are SetSpec-wrapped; CSV is the flattened spreadsheet form (spec §7.3).

    Args:
        request: The incoming request.
        scope: What to export.
        selector: The scope's argument.
        format: ``json``, ``jsonl`` or ``csv``.
        include_samples: Include the raw samples behind each metric.
        include_prompts: Include each sample's prompt identity and hashes.
        include_prompt_text: Add a prompt appendix — each distinct rendered prompt once, keyed by
            its hash — so the export is auditable by a reader who does not have the prompt pack.
        since: Only runs created at or after this instant.
        until: Only runs created strictly before this instant. The window is half-open, so
            consecutive windows tile without overlapping or dropping a run between them, which is
            how a history larger than one document is exported completely.

    Returns:
        A streaming response with the format's media type and a ``Content-Disposition`` naming a
        file, so a browser saves it rather than rendering a megabyte of JSON into the tab.

    Raises:
        ExportRefused: The scope/selector pairing is unanswerable, matched nothing, or covers
            more runs than one document may carry.
    """
    database: Database = request.app.state.database
    selection = ExportSelection(
        scope=ExportScope(scope),
        selector=selector,
        export_format=ExportFormat(format),
        include_samples=include_samples,
        include_prompts=include_prompts,
        include_prompt_text=include_prompt_text,
        since=since,
        until=until,
    )
    # Resolution happens before the response starts, so a refusal is a clean 4xx rather than an
    # error envelope glued onto the front of a half-written document.
    stream = iter_export(database, selection)
    first = next(stream, "")
    suffix = "csv" if selection.export_format is ExportFormat.CSV else selection.export_format.value
    filename = f"freeweight-{selection.scope.value}.{suffix}"

    def _body() -> Any:  # noqa: ANN401 — a generator of str, which Starlette encodes
        yield first
        yield from stream

    return StreamingResponse(
        _body(),
        media_type=content_type_for(selection.export_format),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _results_page(
    *,
    page: Any,  # noqa: ANN401 — a ResultsPage, or None in the error state
    filters: dict[str, str],
    error: str | None,
    status_code: int,
) -> HTMLResponse:
    """Render the results table in whichever of its states applies."""
    return HTMLResponse(
        render(
            "results/index.html",
            app_version=__version__,
            page="results",
            results=page,
            filters=filters,
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/results", response_class=HTMLResponse)
def results_page(  # noqa: PLR0913 — mirrors the API's documented filter set
    request: Request,
    model: _ModelQuery = None,
    suite: _SuiteQuery = None,
    metric_key: _MetricQuery = None,
    machine: _MachineQuery = None,
    since: _SinceQuery = None,
    status: _StatusQuery = None,
    limit: _LimitQuery = DEFAULT_RESULTS_LIMIT,
    cursor: _CursorQuery = None,
) -> HTMLResponse:
    """Render the metric-level results table.

    Server-rendered in one pass with an ordinary ``<form method="get">`` filter bar, so the page
    and its filters work with JavaScript disabled (ADR-0020, UI standards §13). ``table.js``
    upgrades the same markup with client-side column visibility and sorting when it loads, and the
    table is complete without it.
    """
    database: Database = request.app.state.database
    filters = {
        "model": model or "",
        "suite": suite or "",
        "metric_key": metric_key or "",
        "machine": machine or "",
        "since": since or "",
        "status": status or "completed",
    }
    try:
        page = query_results(
            database,
            _query_from(
                model=model,
                suite=suite,
                metric_key=metric_key,
                machine=machine,
                runtime_profile=None,
                since=since,
                until=None,
                status=status,
                limit=limit,
                cursor=cursor,
            ),
        )
    except (NotFoundError, ValidationError) as exc:
        return _results_page(
            page=None,
            filters=filters,
            error=f"{exc.message} ({exc.code})",
            status_code=404 if isinstance(exc, NotFoundError) else 400,
        )
    except DatabaseError as exc:
        return _results_page(
            page=None, filters=filters, error=f"{exc.message} ({exc.code})", status_code=503
        )
    return _results_page(page=page, filters=filters, error=None, status_code=200)


@router.get("/results/samples/{sample_id}", response_class=HTMLResponse)
def case_inspector_page(request: Request, sample_id: str) -> HTMLResponse:
    """Render one raw sample: prompt, response, tool calls, scoring and telemetry.

    The end of the drill-down. Everything on this page is a stored fact about one request, and
    nothing on it is aggregated — if a number here disagrees with a dashboard figure, the
    dashboard is wrong.
    """
    database: Database = request.app.state.database
    try:
        inspection = inspect_case(database, sample_id)
    except SuiteError as exc:
        return HTMLResponse(
            render(
                "results/case.html",
                app_version=__version__,
                page="results",
                inspection=None,
                sample_id=sample_id,
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=503 if isinstance(exc, DatabaseError) else 404,
        )
    return HTMLResponse(
        render(
            "results/case.html",
            app_version=__version__,
            page="results",
            inspection=inspection,
            sample_id=sample_id,
            error=None,
        )
    )


@router.get("/results/goals/{slug}", response_class=HTMLResponse)
def goal_results_page(request: Request, slug: str) -> HTMLResponse:
    """One goal's results: its score method mix, its calibration, and every run of it.

    Added at Phase 10A, and it is the page that keeps a goal's numbers honest in the same way the
    run detail page keeps a benchmark's honest: ``score_method_mix`` sits beside the score, the
    calibration state says in words whether evidence is emitted, and the ``unforked`` badge
    appears wherever a starter's results do.
    """
    from freeweight.services.calibration import latest_outcome
    from freeweight.services.goals import get_goal, summarize
    from freeweight.services.results import ResultsQuery

    database: Database = request.app.state.database
    root = _goals_root(request)
    try:
        goal = get_goal(root, slug)
        rows = query_results(database, ResultsQuery(suite=f"goal.{slug}", limit=500)).rows
        outcome = latest_outcome(database, goal)
    except SuiteError as exc:
        return HTMLResponse(
            render(
                "results/goal_detail.html",
                app_version=__version__,
                page="goals",
                goal=None,
                mix={},
                rows=(),
                outcome=None,
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=503 if isinstance(exc, DatabaseError) else 404,
        )
    return HTMLResponse(
        render(
            "results/goal_detail.html",
            app_version=__version__,
            page="goals",
            goal=goal,
            mix=summarize(goal).score_method_mix,
            rows=rows,
            outcome=outcome,
            error=None,
        )
    )


def _goals_root(request: Request) -> Any:  # noqa: ANN401 — a Path
    """Where the user's own goal packs live."""
    from pathlib import Path

    from freeweight.config import config_dir

    configured = request.app.state.settings.goals.root
    return Path(configured) if configured else config_dir() / "goals"
