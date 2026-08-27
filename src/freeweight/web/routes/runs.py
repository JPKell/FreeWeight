"""freeweight.web.routes.runs — the run API, the run pages and the live event stream.

Two routers. :data:`api_router` serves ``/api/v1/runs…`` for clients and the browser's live view;
:data:`router` serves the HTML pages, which are ordinary server-rendered forms and links
([ADR-0020](../../../../docs/adr/0020-ui-rendering-strategy.md): progressive enhancement, no SPA).
The pages POST to the same service functions the API calls, so a run started from a form and one
started from ``curl`` are the same object created the same way.

Handler kinds follow [ADR-0003](../../../../docs/adr/0003-sync-vs-async-strategy.md) exactly. Every
handler that touches the database is a plain ``def``, which Starlette runs in its bounded worker
threadpool. :func:`run_events` is ``async def``, because an SSE handler holds its connection open
for the length of a run and must not occupy a worker thread for minutes — and every store read
inside it is dispatched back to a thread with :func:`asyncio.to_thread`, so the event loop never
blocks on SQLite (API standards §8's rule for exactly this handler).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Protocol

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from freeweight.__about__ import __version__
from freeweight.domain.benchmark import BenchmarkNotFound
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.events import (
    POLL_INTERVAL_SECONDS,
    RunEventPublisher,
    format_event_frame,
    format_heartbeat,
    read_events,
)
from freeweight.services.models import list_models_with_latest_descriptor
from freeweight.services.runs import (
    ExecutionConfig,
    RunDetail,
    RunNotFound,
    cancel_run,
    create_run,
    get_run,
    list_runs,
    list_samples,
)
from freeweight.services.telemetry_recording import TelemetrySeries, load_series
from freeweight.web.rendering import render

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from freeweight.services.database import Database

__all__ = ["Chart", "api_router", "router"]

api_router = APIRouter(tags=["runs"])
router = APIRouter(include_in_schema=False)

_HEARTBEAT_INTERVAL_SECONDS = 15.0
_MAX_EVENT_BATCH = 200


class _Disconnectable(Protocol):
    """The one thing :func:`_event_stream` needs from a request: whether it went away.

    A structural type rather than ``fastapi.Request`` itself, so a test can drive the generator
    with a minimal fake instead of constructing a real ASGI request. The same shape
    :mod:`freeweight.web.routes.system` uses for the telemetry stream.
    """

    async def is_disconnected(self) -> bool: ...


def _publisher(request: Request) -> RunEventPublisher:
    """Build an event publisher over the request's own database handle."""
    return RunEventPublisher(request.app.state.database)


def _summary_json(summary: Any) -> dict[str, Any]:  # noqa: ANN401 — a RunSummary
    """Render a run summary as the API's JSON object."""
    from baseaicore.timeutil import to_rfc3339

    return {
        "id": summary.id,
        "status": summary.status,
        "suite": {"key": summary.suite_key, "version": summary.suite_version},
        "model": summary.model_canonical_id,
        "label": summary.label,
        "created_at": to_rfc3339(summary.created_at),
        "started_at": to_rfc3339(summary.started_at) if summary.started_at else None,
        "completed_at": to_rfc3339(summary.completed_at) if summary.completed_at else None,
        "reproducibility_fingerprint": summary.reproducibility_fingerprint,
        "error": (
            None
            if summary.error_code is None
            else {"code": summary.error_code, "message": summary.error_text}
        ),
    }


def _detail_json(detail: RunDetail) -> dict[str, Any]:
    """Render a run detail as the API's JSON object: the run, its tests and its metrics."""
    body = _summary_json(detail.run)
    body["effective_config"] = detail.effective_config.to_json()
    body["last_event_sequence"] = detail.last_event_sequence
    body["tests"] = [
        {
            "id": test.id,
            "key": test.test_key,
            "name": test.test_name,
            "status": test.status,
            "skip_reason": test.skip_reason,
            "completed_cases": test.completed_cases,
            "total_cases": test.total_cases,
            "repetitions": test.repetitions,
            "error": (
                None
                if test.error_code is None
                else {"code": test.error_code, "message": test.error_text}
            ),
        }
        for test in detail.tests
    ]
    body["metrics"] = [
        {
            "key": metric.metric_key,
            "run_test_id": metric.run_test_id,
            # An unavailable metric is the string "unsupported", never null and never 0
            # (spec §11 rule 6, ADR-0016).
            "value": (metric.numeric_value if metric.unavailable_reason is None else "unsupported"),
            "unavailable_reason": metric.unavailable_reason,
            "unit": metric.unit,
            "aggregation": metric.aggregation,
            "higher_is_better": metric.higher_is_better,
            "sample_count": metric.sample_count,
            "excluded_count": metric.excluded_count,
            # A device figure names its device; a device-independent one carries null rather than
            # a defaulted 0, which would claim an attribution nobody made (ADR-0027 §5).
            "gpu_index": metric.gpu_index,
            "stddev": metric.stddev,
            "coefficient_of_variation": metric.coefficient_of_variation,
        }
        for metric in detail.metrics
    ]
    # api.md: "Run with tests, aggregate metrics, degradations and the fingerprint document".
    # The document, not only its hash — a hash a caller cannot explain is no use during a
    # regression hunt (Machine Identity §4 rule 2).
    body["provenance"] = {
        "served_context": detail.run.served_context,
        "served_context_source": detail.run.served_context_source,
        "gpu_index": detail.run.gpu_index,
        "multi_gpu_visible": detail.run.multi_gpu_visible,
        "telemetry_overhead_percent": detail.run.telemetry_overhead_percent,
        "prompt_pack": {
            "id": detail.run.prompt_pack_id,
            "version": detail.run.prompt_pack_version,
            "hash": detail.run.prompt_pack_hash,
        },
        "fingerprint_document": dict(detail.run.fingerprint_document),
    }
    body["degradations"] = [dict(item) for item in detail.run.degradations]
    return body


@api_router.post("/runs", status_code=status.HTTP_201_CREATED, summary="Start a run")
def create_run_endpoint(request: Request, body: dict[str, Any]) -> JSONResponse:
    """Validate and queue a run, returning ``201`` with the run object.

    Validation happens before anything is persisted, so a rejected request creates nothing
    (api.md §4).

    Args:
        request: The incoming request; carries the application's database, provider and telemetry
            collector.
        body: ``{"model": …, "suites": [key], "execution": {...}, "label": …}``. ``suites`` takes
            a list for forward compatibility with multi-suite runs; Phase 5 runs the first entry
            and refuses more than one rather than silently dropping the rest.

    Returns:
        ``201`` with the queued run.

    Raises:
        ValidationError: ``model`` or ``suites`` is missing, or more than one suite was named.
        BenchmarkNotFound: The named suite is not registered.
        ModelNotFound: The model is not stored.
    """
    from baseaicore import ValidationError

    model_ref = body.get("model")
    suites = body.get("suites") or ([body["suite"]] if body.get("suite") else [])
    if not model_ref or not suites:
        raise ValidationError(
            "A run needs a `model` and at least one entry in `suites`.",
            details={"fields": ["model", "suites"]},
        )
    if len(suites) > 1:
        raise ValidationError(
            "Phase 5 executes one suite per run; name one suite, or start one run per suite. "
            "Refusing rather than silently running only the first.",
            details={"suites": list(suites)},
        )
    execution_body = body.get("execution") or {}
    sampling_body = body.get("sampling") or {}
    settings = request.app.state.settings
    execution = ExecutionConfig.resolve(
        settings.execution,
        measured_repetitions=execution_body.get("measured_repetitions"),
        seed=execution_body.get("seed"),
        store_responses=execution_body.get("store_responses"),
        temperature=sampling_body.get("temperature"),
        max_output_tokens=sampling_body.get("max_output_tokens"),
    )
    summary = create_run(
        request.app.state.database,
        request.app.state.provider,
        request.app.state.telemetry.collector,
        request.app.state.registry,
        model_ref=str(model_ref),
        suite_key=str(suites[0]),
        execution=execution,
        label=body.get("label"),
    )
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=_summary_json(summary))


@api_router.get("/runs", summary="List runs")
def list_runs_endpoint(
    request: Request,
    run_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Return runs newest-first, optionally filtered by status."""
    runs = list_runs(request.app.state.database, status=run_status, limit=limit)
    return {"runs": [_summary_json(run) for run in runs]}


@api_router.get("/runs/{run_id}", summary="One run with its tests and metrics")
def get_run_endpoint(request: Request, run_id: str) -> dict[str, Any]:
    """Return one run, its tests and its aggregate metrics.

    Raises:
        RunNotFound: Nothing matches ``run_id``.
    """
    return _detail_json(get_run(request.app.state.database, run_id))


@api_router.post(
    "/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED, summary="Cancel a run"
)
def cancel_run_endpoint(request: Request, run_id: str) -> JSONResponse:
    """Request cancellation, returning ``202`` with the run's new state.

    ``202``, not ``200``: a *running* run enters ``cancelling`` and stops at the executor's next
    boundary, so the request is accepted rather than completed (api.md §4).

    Raises:
        RunNotFound: Nothing matches ``run_id``.
        RunNotCancellable: The run is terminal — answered as ``409`` by the error handlers.
    """
    summary = cancel_run(request.app.state.database, _publisher(request), run_id)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=_summary_json(summary))


@api_router.get("/runs/{run_id}/tests", summary="One run's tests")
def run_tests_endpoint(request: Request, run_id: str) -> dict[str, Any]:
    """Return one run's tests, as the drill-down list."""
    detail = get_run(request.app.state.database, run_id)
    return {"tests": _detail_json(detail)["tests"]}


@api_router.get("/runs/{run_id}/tests/{run_test_id}/samples", summary="One test's raw samples")
def run_samples_endpoint(
    request: Request,
    run_id: str,
    run_test_id: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> dict[str, Any]:
    """Return one test's raw samples — the rows every headline number drills to.

    ``score`` is ``null`` for a sample that could not be scored, never ``0``, and the sample stays
    in this list so the exclusion is visible (spec §13).

    Raises:
        RunNotFound: ``run_test_id`` belongs to no run, or to a different run than ``run_id``.
    """
    detail = get_run(request.app.state.database, run_id)
    if run_test_id not in {test.id for test in detail.tests}:
        raise RunNotFound(
            f"Run {detail.run.id!r} has no test {run_test_id!r}.",
            details={"run": detail.run.id, "run_test": run_test_id},
        )
    samples = list_samples(request.app.state.database, run_test_id, limit=limit)
    return {
        "samples": [
            {
                "id": sample.id,
                "case_id": sample.case_id,
                "ordinal": sample.ordinal,
                "repetition": sample.repetition,
                "status": sample.status,
                "score": sample.score,
                "score_method": sample.score_method,
                "response_hash": sample.response_hash,
                "response_text": sample.response_text,
                "output_chars": sample.output_chars,
                "input_tokens": sample.input_tokens,
                "output_tokens": sample.output_tokens,
                "client_wall_ms": sample.client_wall_ms,
                "finish_reason": sample.finish_reason,
                "error": (
                    None
                    if sample.error_code is None
                    else {"code": sample.error_code, "message": sample.error_text}
                ),
                "detail": sample.detail,
            }
            for sample in samples
        ]
    }


def _resolve_last_event_id(request: Request, last_event_id: str | None) -> int:
    """Resolve where a subscriber resumes from: the header, else the query, else the beginning.

    ``Last-Event-ID`` is sent automatically by ``EventSource`` when the *browser* reconnects a
    dropped stream. It is **not** sent after a full page reload, which starts a brand-new
    ``EventSource`` — so the page also passes ``?last_event_id=`` with the highest sequence it
    rendered server-side. Both routes end here, and both mean "everything strictly after this".
    That is what makes acceptance criterion 2 — refresh mid-run, no missing events — true of a
    reload and not only of a dropped connection.

    A malformed value is treated as ``0`` (replay from the beginning) rather than rejected: a
    client that has lost track of its position needs the whole stream, not a ``400``.
    """
    raw = request.headers.get("last-event-id") or last_event_id or "0"
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


async def _event_stream(
    request: _Disconnectable,
    database: Database,
    run_id: str,
    *,
    after_sequence: int,
    close_when_drained: bool = False,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    heartbeat_interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncGenerator[str, None]:
    """Yield this run's SSE frames from ``after_sequence`` onwards, then close on the terminal one.

    Replay and live are the same loop: every iteration asks the store for what comes after the
    highest sequence already sent. A reconnecting client therefore cannot receive a duplicate (it
    asks for what it has not seen) and cannot miss one (nothing is dropped in memory, because
    nothing is held in memory).

    Closes after a terminal event, per API standards §8 — a client that sees the connection close
    *without* one treats it as an interruption and reconnects.

    Args:
        request: Polled each cycle so the loop ends when the browser goes away.
        database: The application's database handle.
        run_id: The run to stream.
        after_sequence: Resume point; ``0`` streams from the beginning.
        close_when_drained: The run had already reached a terminal state when this connection
            opened, so its event stream is finite and complete. Once it has been drained there is
            nothing left to wait for and the connection closes instead of polling forever. Set by
            the route from the run it has just looked up. Without it, a client reconnecting with a
            ``Last-Event-ID`` at or past the terminal event never receives a terminal frame —
            there is none left to send — and the connection is held open for the life of the
            process, which is exactly what a page reloaded just after a run finished does.
        poll_interval_seconds: How often to look for new events. Overridable so tests do not wait.
        heartbeat_interval_seconds: Heartbeat cadence absent new events; 15 s in production.
    """
    sequence = after_sequence
    next_heartbeat = time.monotonic() + heartbeat_interval_seconds
    while not await request.is_disconnected():
        batch = await asyncio.to_thread(
            read_events, database, run_id, after_sequence=sequence, limit=_MAX_EVENT_BATCH
        )
        for event in batch:
            yield format_event_frame(event)
            sequence = event.sequence
            if event.is_terminal:
                return
        now = time.monotonic()
        if now >= next_heartbeat:
            yield format_heartbeat()
            next_heartbeat = now + heartbeat_interval_seconds
        if not batch:
            if close_when_drained:
                return
            await asyncio.sleep(poll_interval_seconds)


@api_router.get("/runs/{run_id}/events", summary="Live run events, server-sent")
async def run_events(
    request: Request,
    run_id: str,
    last_event_id: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> StreamingResponse:
    """Stream one run's events, replaying from ``Last-Event-ID`` when the client sends one.

    Raises:
        RunNotFound: Nothing matches ``run_id``. Checked before the stream opens, so a bad id is
            a ``404`` rather than an empty stream that never ends.
    """
    database: Database = request.app.state.database
    detail = await asyncio.to_thread(get_run, database, run_id)
    after = _resolve_last_event_id(request, last_event_id)
    return StreamingResponse(
        _event_stream(
            request,
            database,
            detail.run.id,
            after_sequence=after,
            close_when_drained=detail.run.is_terminal,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request) -> HTMLResponse:
    """Render the run list and the form that starts a new one.

    The four states UI standards §6 requires are all here: the error state when the database
    cannot be read, the empty state when no run exists yet, the populated table, and — since a run
    is long — a live status column that the detail page streams.
    """
    database: Database = request.app.state.database
    try:
        runs = list_runs(database, limit=50)
        models = list_models_with_latest_descriptor(database)
    except DatabaseError as exc:
        return HTMLResponse(
            render(
                "runs/index.html",
                app_version=__version__,
                page="runs",
                runs=(),
                models=(),
                suites=(),
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=503,
        )
    return HTMLResponse(
        render(
            "runs/index.html",
            app_version=__version__,
            page="runs",
            runs=runs,
            models=models,
            suites=request.app.state.registry.keys(),
            error=None,
        )
    )


@router.post("/runs")
def start_run_form(
    request: Request,
    model: Annotated[str, Form()],
    suite: Annotated[str, Form()],
    label: Annotated[str, Form()] = "",
) -> Response:
    """Start a run from the page's form and redirect to its live view.

    A plain HTML form, not a JSON fetch (ADR-0020). On a validation failure the run list is
    re-rendered with the error beside the form and the user's input intact (UI standards §6),
    rather than redirecting to a page that cannot say what went wrong.
    """
    from baseaicore import SuiteError

    settings = request.app.state.settings
    try:
        summary = create_run(
            request.app.state.database,
            request.app.state.provider,
            request.app.state.telemetry.collector,
            request.app.state.registry,
            model_ref=model,
            suite_key=suite,
            execution=ExecutionConfig.resolve(settings.execution),
            label=label or None,
        )
    except (SuiteError, BenchmarkNotFound) as exc:
        database: Database = request.app.state.database
        return HTMLResponse(
            render(
                "runs/index.html",
                app_version=__version__,
                page="runs",
                runs=list_runs(database, limit=50),
                models=list_models_with_latest_descriptor(database),
                suites=request.app.state.registry.keys(),
                error=f"{exc.message} ({exc.code})",
                form_model=model,
                form_suite=suite,
                form_label=label,
            ),
            status_code=400,
        )
    return RedirectResponse(f"/runs/{summary.id}", status_code=303)


@dataclass(frozen=True, slots=True)
class Chart:
    """One telemetry series, reduced to what an inline SVG and its text alternative need.

    Attributes:
        key: A slug unique within the page, used for the SVG's accessible name.
        label: The series' human-readable name, unit included — UI standards §5 requires every
            number to show its unit, and a chart's unit belongs in its label rather than only in a
            tooltip nobody can read on a phone.
        unit: The unit on its own, for the summary table.
        points: ``"x,y "``-joined coordinates in a 0–100 × 0–100 viewBox.
        minimum: The lowest reported value.
        maximum: The highest reported value, and the top of the axis.
        mean: The mean of the reported values.
        reported: How many observations carried this value.
        missing: How many did not. A gap is drawn as a gap; a missing reading is never plotted as
            zero, which would read as an idle machine (ADR-0016).
    """

    key: str
    label: str
    unit: str
    points: str
    minimum: float
    maximum: float
    mean: float
    reported: int
    missing: int


def _chart(key: str, label: str, unit: str, values: Sequence[float | None]) -> Chart | None:
    """Reduce one series to a :class:`Chart`, or ``None`` when nothing was reported.

    The vertical axis always starts at zero. UI standards §5 forbids truncated axes that mislead,
    and a telemetry chart is exactly where a truncated axis turns a 2 % utilization wobble into a
    dramatic sawtooth.

    A series in which every reading is missing produces ``None`` — an empty chart with a
    zero-to-zero axis says "the machine did nothing" rather than "this could not be read".
    """
    reported = [value for value in values if value is not None]
    if not reported:
        return None
    top = max(reported)
    span = top if top > 0 else 1.0
    step = 100.0 / (len(values) - 1) if len(values) > 1 else 0.0
    points = " ".join(
        f"{index * step:.2f},{100.0 - (value / span) * 100.0:.2f}"
        for index, value in enumerate(values)
        if value is not None
    )
    return Chart(
        key=key,
        label=label,
        unit=unit,
        points=points,
        minimum=min(reported),
        maximum=top,
        mean=sum(reported) / len(reported),
        reported=len(reported),
        missing=len(values) - len(reported),
    )


def _charts(series: TelemetrySeries) -> list[Chart]:
    """Build every chart a run's telemetry supports, host first and then per device.

    Per device, never combined: there is no machine-wide GPU figure in this system, so two GPUs
    produce two sets of charts rather than one averaged pair (ADR-0027 §5).
    """
    charts = [
        _chart("cpu", "Host CPU utilization (%)", "%", series.cpu_percent),
        _chart("ram", "Host RAM used (bytes)", "bytes", series.ram_used_bytes),
    ]
    for gpu in series.gpus:
        charts.extend(
            [
                _chart(
                    f"gpu{gpu.gpu_index}-util",
                    f"GPU {gpu.gpu_index} utilization (%)",
                    "%",
                    gpu.utilization_percent,
                ),
                _chart(
                    f"gpu{gpu.gpu_index}-vram",
                    f"GPU {gpu.gpu_index} VRAM used (bytes)",
                    "bytes",
                    gpu.vram_used_bytes,
                ),
                _chart(
                    f"gpu{gpu.gpu_index}-power",
                    f"GPU {gpu.gpu_index} power (W)",
                    "W",
                    gpu.power_watts,
                ),
                _chart(
                    f"gpu{gpu.gpu_index}-temp",
                    f"GPU {gpu.gpu_index} temperature (°C)",
                    "°C",
                    gpu.temperature_c,
                ),
            ]
        )
    return [chart for chart in charts if chart is not None]


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail_page(request: Request, run_id: str) -> HTMLResponse:
    """Render one run: its provenance, its metrics, its telemetry and its live event timeline.

    ``last_event_sequence`` is rendered into the page so the browser's ``EventSource`` resumes
    from what the server already showed. Without it a page refreshed mid-run would either miss the
    events that arrived between render and connect, or replay the whole run into a timeline that
    already had them.

    The telemetry charts are rendered **server-side as inline SVG**, with a summary table beside
    them (UI standards §5's "text/table alternative for the key figures"). A run's telemetry is a
    finite, already-persisted series, so fetching it over a second request and drawing it in
    JavaScript would add a loading state, a failure state and a dependency to a page that needs
    none of them (ADR-0020: progressive enhancement, no SPA).
    """
    database: Database = request.app.state.database
    try:
        detail = get_run(database, run_id)
        series = load_series(database, detail.run.id)
    except (RunNotFound, DatabaseError) as exc:
        return HTMLResponse(
            render(
                "runs/detail.html",
                app_version=__version__,
                page="runs",
                detail=None,
                run_ref=run_id,
                charts=(),
                telemetry_samples=0,
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=404 if isinstance(exc, RunNotFound) else 503,
        )
    return HTMLResponse(
        render(
            "runs/detail.html",
            app_version=__version__,
            page="runs",
            detail=detail,
            run_ref=run_id,
            charts=_charts(series),
            telemetry_samples=series.sample_count,
            error=None,
        )
    )


@router.post("/runs/{run_id}/cancel")
def cancel_run_form(request: Request, run_id: str) -> RedirectResponse:
    """Cancel a run from the detail page and return to it.

    Never fails the request: a run that has already finished is reported by the page it redirects
    to, which shows the real status. A banner saying "too late" would tell the user nothing the
    status does not.
    """
    from baseaicore import SuiteError

    try:
        cancel_run(request.app.state.database, _publisher(request), run_id)
    except SuiteError:
        pass
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/runs/{run_id}/tests/{run_test_id}", response_class=HTMLResponse)
def run_samples_page(request: Request, run_id: str, run_test_id: str) -> HTMLResponse:
    """Render one test's raw samples — the second of the two clicks UI standards §5 allows.

    Raises:
        RunNotFound: The run or the test does not exist, or the test belongs to another run.
    """
    database: Database = request.app.state.database
    try:
        detail = get_run(database, run_id)
        test = next((item for item in detail.tests if item.id == run_test_id), None)
        if test is None:
            raise RunNotFound(
                f"Run {detail.run.id!r} has no test {run_test_id!r}.",
                details={"run": detail.run.id, "run_test": run_test_id},
            )
        samples = list_samples(database, run_test_id)
    except (RunNotFound, DatabaseError) as exc:
        return HTMLResponse(
            render(
                "runs/samples.html",
                app_version=__version__,
                page="runs",
                detail=None,
                test=None,
                samples=(),
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=404 if isinstance(exc, RunNotFound) else 503,
        )
    return HTMLResponse(
        render(
            "runs/samples.html",
            app_version=__version__,
            page="runs",
            detail=detail,
            test=test,
            samples=samples,
            error=None,
        )
    )
