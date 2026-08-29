"""freeweight.web.routes.evidence — the LoadCoach integration point, and the page that explains it.

Three surfaces, one module, because they are three views of one thing: a stored evidence record.

* ``GET /api/v1/evidence`` — the current ``capability.evidence`` records, a **collection**
  envelope whose items are SetSpec envelopes (API §6, API standards §3).
* ``GET /api/v1/evidence/export`` — one ``benchmark.evidence_bundle`` envelope, the file form of
  the same data, with ``?since=`` filtering on ``computed_at`` (ADR-0022 §5).
* ``GET /evidence`` — the page: every record with its staleness badge and, one interaction away,
  the six confidence factors and the contributing metrics that explain the score.

Every handler is a plain ``def``: they all touch the database, and ADR-0003 rule 1 puts a
database-touching handler in the worker threadpool rather than on the event loop.

No business logic lives here. Aggregation, staleness and the envelopes are
:mod:`freeweight.services.evidence`'s; these functions parse a query string, call one service
function, and render.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from baseaicore import SuiteError, ValidationError, from_rfc3339, utc_now
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import HTMLResponse

from freeweight.__about__ import __version__
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.evidence import (
    DEFAULT_EVIDENCE_LIMIT,
    EvidenceQuery,
    EvidenceRecord,
    Staleness,
    evidence_bundle,
    policy_for,
    query_evidence,
    staleness_of,
)
from freeweight.web.rendering import render

if TYPE_CHECKING:
    from freeweight.services.database import Database

__all__ = ["api_router", "router"]

api_router = APIRouter(tags=["evidence"])
router = APIRouter(include_in_schema=False)

_CapabilityQuery = Annotated[
    str | None, Query(description="Exact capability ID, e.g. tool_use or user.house_voice.")
]
_ModelQuery = Annotated[str | None, Query(description="Model canonical ID, ULID or prefix.")]
_MachineQuery = Annotated[str | None, Query(description="Machine fingerprint.")]
_ProfileQuery = Annotated[
    str | None, Query(alias="runtime_profile", description="Runtime profile hash.")
]
_MinConfidenceQuery = Annotated[
    float | None, Query(ge=0.0, le=1.0, description="Records at or above this confidence.")
]
_LimitQuery = Annotated[int, Query(ge=1, le=500, description="Page size; clamped to 500.")]
_CursorQuery = Annotated[str | None, Query(description="Opaque cursor from a previous page.")]
_SinceQuery = Annotated[
    str | None,
    Query(
        description=(
            "RFC 3339. Returns evidence whose computed_at is later, on FreeWeight's clock. Send "
            "back the generated_at of the bundle you received last time; never your own clock."
        )
    ),
]

_MAX_PAGE_ROWS = 500


@dataclass(frozen=True, slots=True)
class _Row:
    """One record and its staleness verdict, as the page renders them side by side."""

    record: EvidenceRecord
    staleness: Staleness


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


@api_router.get("/evidence", summary="List capability evidence")
def list_evidence(  # noqa: PLR0913 — every argument is a documented query parameter
    request: Request,
    capability: _CapabilityQuery = None,
    model: _ModelQuery = None,
    machine: _MachineQuery = None,
    runtime_profile: _ProfileQuery = None,
    min_confidence: _MinConfidenceQuery = None,
    limit: _LimitQuery = DEFAULT_EVIDENCE_LIMIT,
    cursor: _CursorQuery = None,
) -> dict[str, Any]:
    """Return the current ``capability.evidence`` records (API §6).

    A collection envelope whose ``items`` are SetSpec envelopes — the two nest in exactly that
    order and never the reverse (ADR-0025 §2). ``user.*`` records carry ``goal_hash``,
    ``score_method_mix``, ``judge_set``, ``calibration`` and ``judge_validity_factor``
    (ADR-0032 §5); a goal below its calibration gate has no record here at all.

    Args:
        request: The incoming request; the database handle lives on its application state.
        capability: Exact capability ID.
        model: Model canonical ID, ULID or unambiguous prefix.
        machine: Machine fingerprint.
        runtime_profile: Runtime profile hash.
        min_confidence: Records at or above this confidence.
        limit: Page size, clamped to 500.
        cursor: Opaque continuation token.

    Returns:
        The collection envelope: ``items`` and ``page``.

    Raises:
        ValidationError: The cursor was not issued here, or ``model`` is an ambiguous prefix.
        NotFoundError: ``model`` matches nothing.
    """
    database: Database = request.app.state.database
    page = query_evidence(
        database,
        EvidenceQuery(
            capability=capability,
            model=model,
            machine=machine,
            runtime_profile=runtime_profile,
            min_confidence=min_confidence,
            limit=limit,
            cursor=cursor,
        ),
    )
    return page.as_json()


@api_router.get("/evidence/export", summary="Export the evidence bundle")
def export_evidence(  # noqa: PLR0913 — every argument is a documented query parameter
    request: Request,
    since: _SinceQuery = None,
    capability: _CapabilityQuery = None,
    model: _ModelQuery = None,
    machine: _MachineQuery = None,
    runtime_profile: _ProfileQuery = None,
    min_confidence: _MinConfidenceQuery = None,
) -> Response:
    """Return one ``benchmark.evidence_bundle`` — the file LoadCoach imports (API §6, §10).

    A single SetSpec envelope with no collection wrapper, because a bundle is one document.
    ``complete`` is ``true`` only when nothing narrows the selection: only a complete bundle lets
    a consumer infer removals, and a filtered one must never be mistaken for it (ADR-0022 §5).

    Args:
        request: The incoming request.
        since: RFC 3339; evidence whose ``computed_at`` is later, on FreeWeight's clock.
        capability: Exact capability ID.
        model: Model canonical ID, ULID or unambiguous prefix.
        machine: Machine fingerprint.
        runtime_profile: Runtime profile hash.
        min_confidence: Records at or above this confidence.

    Returns:
        The envelope as canonical JSON, with a ``Content-Disposition`` naming a file so a browser
        saves it rather than rendering it.

    Raises:
        ValidationError: ``since`` is malformed, or ``model`` is an ambiguous prefix.
        NotFoundError: ``model`` matches nothing.
    """
    database: Database = request.app.state.database
    text = evidence_bundle(
        database,
        EvidenceQuery(
            capability=capability,
            model=model,
            machine=machine,
            runtime_profile=runtime_profile,
            min_confidence=min_confidence,
            since=_instant(since, field="since"),
        ),
    )
    return Response(
        content=text,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="freeweight-evidence.json"'},
    )


@router.get("/evidence", response_class=HTMLResponse)
def evidence_page(
    request: Request,
    capability: _CapabilityQuery = None,
    model: _ModelQuery = None,
    stale: Annotated[str | None, Query(description="'only' to show stale records alone.")] = None,
) -> HTMLResponse:
    """The evidence page: every record, badged, with its explanation one interaction away.

    ADR-0017's staleness surface and ADR-0032's own consequence note in one place: a record is
    badged ``stale`` when its freshness has decayed below the threshold or its environment has
    drifted, and every number sits beside the six factors and the contributing metrics that
    produced it, because a single confidence figure explains nothing.
    """
    settings = request.app.state.settings
    database: Database = request.app.state.database
    now = utc_now()
    policy = policy_for(settings.evidence)
    filters = {"capability": capability or "", "model": model or "", "stale": stale or ""}
    try:
        page = query_evidence(
            database, EvidenceQuery(capability=capability, model=model, limit=_MAX_PAGE_ROWS)
        )
    except SuiteError as exc:
        return HTMLResponse(
            render(
                "evidence/index.html",
                app_version=__version__,
                page="evidence",
                rows=(),
                filters=filters,
                stale_count=0,
                capability_count=0,
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=503 if isinstance(exc, DatabaseError) else 404,
        )
    rows = [
        _Row(record=record, staleness=staleness_of(record, now=now, policy=policy))
        for record in page.records
    ]
    stale_count = sum(1 for row in rows if row.staleness.stale)
    if stale == "only":
        rows = [row for row in rows if row.staleness.stale]
    return HTMLResponse(
        render(
            "evidence/index.html",
            app_version=__version__,
            page="evidence",
            rows=rows,
            filters=filters,
            stale_count=stale_count,
            capability_count=len({row.record.capability_id for row in rows}),
            error=None,
        )
    )
