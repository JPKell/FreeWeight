"""freeweight.web.routes.system — health, version and the live telemetry surface.

``GET /version`` is never authenticated (ADR-0026 §5): version negotiation must work before a
client can know whether its credential is valid. Authentication itself does not exist until a
later phase, so today that is simply the router's default.

``GET /system/status`` and ``GET /system/telemetry/stream`` (Phase 4) read through
``request.app.state.telemetry`` — the one sampler the web lifespan starts for as long as the
server serves (:mod:`freeweight.web.app`) — never building their own collector, for the identical
reason the health check reuses the request's own database handle.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from baseaicore import DependencyUnavailableError
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from weightsdb import DatabaseError

from freeweight.__about__ import __version__
from freeweight.config import data_dir
from freeweight.services.export import EMITTED_SCHEMAS
from freeweight.services.health import get_health_report
from freeweight.services.telemetry import format_heartbeat, format_sample_event, snapshot_to_json

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sweatmeter import TelemetrySnapshot

    from freeweight.config import Settings
    from freeweight.services.scheduler import RunScheduler
    from freeweight.services.telemetry import TelemetryService

__all__ = ["router"]

router = APIRouter(tags=["system"])


class _Disconnectable(Protocol):
    """The one thing ``_telemetry_events`` needs from a request: whether it went away.

    A structural type rather than ``fastapi.Request`` itself, so a test can drive the generator
    with a minimal fake instead of constructing a real ASGI request.
    """

    async def is_disconnected(self) -> bool: ...


_HEARTBEAT_INTERVAL_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.1


@router.get("/health", summary="Component health")
async def health(request: Request) -> JSONResponse:
    """Return the current health report; 200 for ok/degraded, 503 for unavailable.

    Reports on the handle the server is serving from, not a connection opened for the check — a
    health check against a different connection than requests use is answering a question nobody
    asked.
    """
    telemetry: TelemetryService | None = request.app.state.telemetry
    report = get_health_report(
        database=request.app.state.database,
        provider=request.app.state.provider,
        telemetry=telemetry.collector if telemetry is not None else None,
    )
    status_code = 200 if report.status in ("ok", "degraded") else 503
    return JSONResponse(status_code=status_code, content=report.model_dump(mode="json"))


@router.get("/version", summary="Application, API and schema versions")
async def version() -> dict[str, object]:
    """Return the application version, served API majors and understood schema versions."""
    return {
        "application": {"name": "freeweight", "version": __version__, "git_commit": None},
        "api": {"current": "v1", "supported": ["v1"], "deprecated": []},
        "schemas": dict(EMITTED_SCHEMAS),
    }


def _disk_headroom_bytes(settings: Settings) -> int | None:
    """Return free bytes where FreeWeight's data lives, or ``None`` if it cannot be statted.

    Walks up to the nearest existing ancestor first: on a fresh install nothing has created the
    artifact directory yet, and disk headroom is a filesystem-level fact that any ancestor answers
    identically.
    """
    target = Path(settings.storage.artifact_dir) if settings.storage.artifact_dir else data_dir()
    while not target.exists():
        parent = target.parent
        if parent == target:
            return None
        target = parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return None


def _queue_snapshot(scheduler: RunScheduler | None) -> tuple[str | None, int | None]:
    """Return ``(active_run_id, queue_depth)``, or ``(None, None)`` if the queue cannot be read.

    A status endpoint that 500s because the database is behind head is a status endpoint that
    stops working exactly when someone needs it to explain why nothing else does.
    """
    if scheduler is None:
        return None, None
    try:
        return scheduler.active_run_id(), scheduler.queue_depth()
    except DatabaseError:
        return None, None


@router.get("/system/status", summary="Operational snapshot")
async def system_status(request: Request) -> dict[str, object]:
    """Return the live operational numbers the System page renders (Observability Standards §5).

    ``active_run`` and ``queue_depth`` come from the run scheduler (Phase 5). Both are ``None``
    when the queue cannot be read at all — no scheduler, or a database that is behind head — and
    ``queue_depth`` is ``None`` rather than ``0`` in that case, because "no runs are waiting" and
    "I cannot see the queue" are different facts and only one of them is reassuring. The reason is
    already reported, component by component, at ``/health``. ``threadpool_saturation`` stays
    honestly ``None``: there is no application-owned threadpool before Phase 12 adopts MirrorWall,
    and a fabricated ``0`` would claim a measurement this build does not take.

    Raises:
        DependencyUnavailableError: The telemetry sampler is not running — never true of a request
            actually served by :func:`~freeweight.web.app.create_app`'s lifespan, only of a caller
            that skipped it.
    """
    service: TelemetryService | None = request.app.state.telemetry
    if service is None:
        raise DependencyUnavailableError("The telemetry sampler is not running.")
    snapshot = service.latest()
    active_run, queue_depth = _queue_snapshot(request.app.state.scheduler)
    return {
        "active_run": active_run,
        "queue_depth": queue_depth,
        "telemetry": snapshot_to_json(snapshot) if snapshot is not None else None,
        "threadpool_saturation": None,
        "disk_headroom_bytes": _disk_headroom_bytes(request.app.state.settings),
    }


async def _telemetry_events(
    request: _Disconnectable,
    service: TelemetryService,
    *,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
    heartbeat_interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    """Yield ``telemetry.sampled`` SSE frames as new samples appear, with a periodic heartbeat.

    Polls the sampler's ``latest()`` cache — a non-blocking, lock-protected read — rather than
    using its blocking iterator, so an ``async def`` handler never blocks the event loop waiting on
    the sampler's condition variable (API and Contract Standards §8's rule that an SSE handler must
    not run a blocking read on the loop, applied here to the sampler instead of a database).

    Not replay-capable: this module's docstring explains why a reconnecting client starts a fresh
    stream from the current snapshot rather than from ``Last-Event-ID``. The connection still
    exits cleanly on disconnect, which is what lets a client reconnect at all.

    Args:
        request: The client's request, polled each cycle so the loop ends when the client goes
            away instead of writing to a closed connection.
        service: The application's telemetry service.
        poll_interval_seconds: How often to check for a new sample. Overridable so tests do not
            wait on wall-clock sampling intervals.
        heartbeat_interval_seconds: How often to send a heartbeat comment absent a new sample.
            Overridable for the same reason; production callers get the standard 15 s.
    """
    sequence = 0
    last_snapshot: TelemetrySnapshot | None = None
    next_heartbeat = time.monotonic() + heartbeat_interval_seconds
    while not await request.is_disconnected():
        snapshot = service.latest()
        if snapshot is not None and snapshot is not last_snapshot:
            sequence += 1
            yield format_sample_event(sequence, snapshot)
            last_snapshot = snapshot
        now = time.monotonic()
        if now >= next_heartbeat:
            yield format_heartbeat()
            next_heartbeat = now + heartbeat_interval_seconds
        await asyncio.sleep(poll_interval_seconds)


@router.get("/system/telemetry/stream", summary="Live telemetry, server-sent")
async def telemetry_stream(request: Request) -> StreamingResponse:
    """Stream ``telemetry.sampled`` events at the configured sampling interval.

    Raises:
        DependencyUnavailableError: The telemetry sampler is not running.
    """
    service: TelemetryService | None = request.app.state.telemetry
    if service is None:
        raise DependencyUnavailableError("The telemetry sampler is not running.")
    return StreamingResponse(
        _telemetry_events(request, service),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
