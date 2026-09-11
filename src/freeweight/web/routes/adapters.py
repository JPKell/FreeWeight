"""freeweight.web.routes.adapters — ``GET /api/v1/adapters`` (api.md §2a).

The API form of ``freeweight adapters list --json``, joined with what was measured under each
adapter. FreeWeight's own UI has no adapters page; the console's is built on this route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from freeweight.services.adapters import adapter_catalog

__all__ = ["api_router"]

api_router = APIRouter(tags=["adapters"])


@api_router.get("/adapters", summary="The LoRA adapters, and what was measured under each")
def list_adapters_endpoint(request: Request) -> dict[str, Any]:
    """Return the adapter directory's reading joined with the ``adapters`` table (api.md §2a).

    Always ``200``: adapters being off, or the directory missing, is the body's ``note``.
    """
    settings = request.app.state.settings
    return adapter_catalog(request.app.state.database, settings.adapters)
