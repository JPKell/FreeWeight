"""freeweight.web.routes.adapters — ``GET /api/v1/adapters`` and the manifest draft (api.md §2a).

The API form of ``freeweight adapters list --json``, joined with what was measured under each
adapter. FreeWeight's own UI has no adapters page; the console's is built on these routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from freeweight.services.adapters import (
    AdaptersDisabled,
    DraftRefused,
    adapter_catalog,
    draft_manifest,
)

__all__ = ["api_router"]

api_router = APIRouter(tags=["adapters"])


@api_router.get("/adapters", summary="The LoRA adapters, and what was measured under each")
def list_adapters_endpoint(request: Request) -> dict[str, Any]:
    """Return the adapter directory's reading joined with the ``adapters`` table (api.md §2a).

    Always ``200``: adapters being off, or the directory missing, is the body's ``note``.
    """
    settings = request.app.state.settings
    return adapter_catalog(
        request.app.state.database, settings.adapters, provider=request.app.state.provider
    )


class DraftBody(BaseModel):
    """``POST /api/v1/adapters/{name}/draft``'s body (row WX7)."""

    model_config = ConfigDict(extra="forbid")

    base_model_name: str = Field(min_length=1, max_length=200)
    """The base this adapter was trained against — the one field nobody can read off a GGUF."""
    declared_capabilities: list[str] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=2000)


@api_router.post("/adapters/{name}/draft", summary="Draft a manifest for an unmanifested artifact")
def draft_adapter_endpoint(request: Request, name: str, body: DraftBody) -> dict[str, Any]:
    """Write ``<name>.manifest.draft.json`` for an artifact that has no manifest yet.

    ADR-0061 rule 4 and [ADR-0145](../../../docs/adr/): the draft is a **proposal**. It registers
    nothing, is offered to no provider and is measured under by nothing — the suffix keeps it out
    of the directory reading — until a person has checked the base and renamed it.

    Returns:
        ``{"adapter", "path", "payload"}`` — the name, where the draft was written, and what it
        says, so the caller can show the reviewer the record rather than only a filename.

    Raises:
        DraftRefused: ``409``. The name is not one path segment, there is no such unmanifested
            artifact, a manifest or draft is already there — an existing draft is never
            overwritten — or ``[adapters] directory`` is empty, so there is nowhere to write.
            Adapters being off is translated here rather than left as the service's
            ``CONFIGURATION_ERROR``, which the suite answers ``500``: an operator who asks to
            draft on an installation with adapters off has made a request this route refuses, not
            found a fault in the server.
    """
    try:
        path, payload = draft_manifest(
            request.app.state.settings.adapters,
            name,
            base_model_name=body.base_model_name,
            declared_capabilities=body.declared_capabilities,
            notes=body.notes,
        )
    except AdaptersDisabled as exc:
        raise DraftRefused(exc.message, details=dict(exc.details)) from exc
    return {"adapter": name, "path": path, "payload": payload}
