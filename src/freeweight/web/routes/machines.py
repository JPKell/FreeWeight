"""freeweight.web.routes.machines — the machines page.

A route handler contains no business logic (coding standards): it calls one service function and
renders. The table it renders is real and, until Phase 4 writes the first machine profile,
legitimately empty — which is a state UI standards §6 requires the page to design for, not a
reason to leave the page out.
"""

from __future__ import annotations

from typing import Any

from baseaicore import NotFoundError, ValidationError, to_rfc3339
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from weightsdb import DatabaseError

from freeweight.__about__ import __version__
from freeweight.services.database import Database
from freeweight.services.inventory import list_machines, set_machine_nickname
from freeweight.web.rendering import render

__all__ = ["api_router", "router"]

router = APIRouter(include_in_schema=False)
api_router = APIRouter(tags=["machines"])


# A plain ``def`` (row WY10, after WY6): ``list_machines`` reads the database synchronously, so
# FastAPI runs it in the threadpool instead of on the event loop.
@router.get("/machines", response_class=HTMLResponse)
def machines_page(request: Request) -> HTMLResponse:
    """Render every known machine, or the error state if the database cannot be read."""
    database: Database = request.app.state.database
    try:
        machines = list_machines(database)
    except DatabaseError as exc:
        return HTMLResponse(
            render(
                "machines/index.html",
                app_version=__version__,
                page="machines",
                machines=(),
                error=f"{exc.message} ({exc.code})",
            ),
            status_code=503,
        )
    return HTMLResponse(
        render(
            "machines/index.html",
            app_version=__version__,
            page="machines",
            machines=machines,
            error=None,
        )
    )


def _machine_json(machine: Any) -> dict[str, Any]:
    """Render one machine summary as its API body (api.md §2)."""
    return {
        "id": machine.id,
        "machine_fingerprint": machine.machine_fingerprint,
        "nickname": machine.nickname,
        "display_name": machine.display_name,
        "hostname": machine.hostname,
        "os_name": machine.os_name,
        "os_version": machine.os_version,
        "cpu_model": machine.cpu_model,
        "logical_cores": machine.logical_cores,
        "ram_bytes": machine.ram_bytes,
        "first_seen_at": to_rfc3339(machine.first_seen_at),
        "last_seen_at": to_rfc3339(machine.last_seen_at),
    }


@api_router.get("/machines", summary="Every known machine")
def list_machines_endpoint(request: Request) -> dict[str, Any]:
    """Return every machine this installation has measured on, oldest sighting first.

    The current machine is flagged, because a fingerprint is not a name a person recognizes and
    "which of these am I?" is the first question this list raises. The flag is computed by
    *profiling* this host, which is a pure read — this endpoint never writes, so listing machines
    cannot change `last_seen_at` and a monitoring poll cannot make a machine look freshly used.
    Machines are recorded when a run is created, which is the moment a machine actually measured
    something.

    The list is legitimately empty on an installation that has never run anything, which is the
    same state the machines page is designed for rather than an error.

    Raises:
        DatabaseUnavailable: The database could not be read.
    """
    database: Database = request.app.state.database
    current = request.app.state.telemetry.collector.machine_profile().machine_fingerprint
    return {
        "items": [
            {**_machine_json(machine), "is_current": machine.machine_fingerprint == current}
            for machine in list_machines(database)
        ]
    }


class NicknameBody(BaseModel):
    """``PATCH /api/v1/machines/{machine_id}``'s body (row WX7)."""

    model_config = ConfigDict(extra="forbid")

    nickname: str | None = Field(default=None, max_length=120)
    """The operator's label, or ``null`` to clear it. Blank clears it too."""


@api_router.patch("/machines/{machine_id}", summary="Name one machine")
def set_nickname_endpoint(request: Request, machine_id: str, body: NicknameBody) -> dict[str, Any]:
    """Give this machine the operator's own label, or take it away.

    ``PATCH`` rather than ``PUT``: the nickname is the only writable field of a machine, every
    other column being measured from the host, and a ``PUT`` of a partial profile would invite a
    client to send the measured ones back.

    The path is the machine's **exact** ULID, not a prefix: a prefix is a convenience on a read,
    where the wrong match shows the wrong page, and a hazard on a write, where it renames the
    wrong machine.

    Raises:
        NotFoundError: No machine has that ULID, answered as ``404``.
    """
    stored = set_machine_nickname(
        request.app.state.database, machine_id=machine_id, nickname=body.nickname
    )
    return {"id": machine_id, "nickname": stored or None}


@api_router.get("/machines/{machine_id}", summary="One machine's static profile")
def get_machine_endpoint(request: Request, machine_id: str) -> dict[str, Any]:
    """Return one machine by ULID or unambiguous prefix.

    Raises:
        NotFoundError: No machine matches, answered as ``404``.
        ValidationError: The prefix is ambiguous; the message names the candidates.
    """
    machines = list_machines(request.app.state.database)
    exact = [row for row in machines if row.id == machine_id]
    matches = exact or [row for row in machines if row.id.startswith(machine_id)]
    if not matches:
        raise NotFoundError(f"No machine matches {machine_id!r}.", details={"machine": machine_id})
    if len(matches) > 1:
        raise ValidationError(
            f"{machine_id!r} matches {len(matches)} machines; use a longer prefix.",
            details={"machine": machine_id, "candidates": [row.id for row in matches]},
        )
    return _machine_json(matches[0])
