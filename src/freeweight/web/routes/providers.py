"""freeweight.web.routes.providers — ``GET/PUT /provider`` and the Provider page.

[ADR-0117](../../docs/adr/0117-provider-registrations-are-edited-in-place-in-the-config-file.md):
the configuration file stays the source of truth and this page edits the ``[provider]`` block in
place. The handlers parse, call :mod:`freeweight.services.providers`, rebuild the running
provider handle, and render.

Everything else in the file — the bind, the exposure flag, tokens, the database URL, the data
roots — is untouched by construction and refused by name if asked for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from baseaicore import SuiteError
from fastapi import APIRouter, Request
from mirrorwall import json_response
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from freeweight.__about__ import __version__
from freeweight.config import load_settings
from freeweight.infrastructure.providers.factory import build_provider
from freeweight.services.providers import (
    WRITABLE_FIELDS,
    close_provider,
    config_digest,
    describe_provider,
    save_provider,
)
from freeweight.web.rendering import render

__all__ = ["api_router", "router"]

api_router = APIRouter(tags=["provider"])
router = APIRouter(include_in_schema=False)

_NUMERIC_FIELDS = frozenset({"timeout_seconds"})


class ProviderBody(BaseModel):
    """``PUT /provider``'s body: any of the provider block's writable keys."""

    model_config = ConfigDict(extra="forbid")

    kind: str | None = Field(default=None)
    base_url: str | None = Field(default=None)
    timeout_seconds: float | None = Field(default=None)
    model_directory: str | None = Field(default=None)
    state_dir: str | None = Field(default=None)
    server_path: str | None = Field(default=None)
    base_digest: str | None = Field(default=None)


def _config_path(request: Request) -> Path:
    return Path(request.app.state.config_path)


def _document(request: Request) -> dict[str, Any]:
    path = _config_path(request)
    return {
        "provider": describe_provider(request.app.state.settings).as_json(),
        "config_path": str(path),
        "config_digest": config_digest(path),
        "writable_fields": list(WRITABLE_FIELDS),
    }


def _reload(request: Request) -> None:
    """Re-read the file and point the running server at the provider it now names.

    Only the provider handle is rebuilt; every other value this process captured at startup keeps
    what it captured (ADR-0117 decision 5), and the page says so.
    """
    app = request.app
    previous = app.state.provider
    settings = load_settings(config_path=app.state.config_path).settings
    app.state.settings.provider = settings.provider
    app.state.provider = build_provider(settings.provider, adapters=app.state.settings.adapters)
    # The replaced handle owns a process when the kind is llamacpp; dropping it would orphan a
    # `llama-server` holding the card. Work in flight on the old provider is interrupted, which is
    # the honest reading of an operator changing the provider while it runs.
    close_provider(previous)


@api_router.get("/provider")
def get_provider(request: Request) -> JSONResponse:
    """The configured provider, the file it lives in, and what shadows it."""
    return json_response(_document(request))


@api_router.put("/provider")
def put_provider(request: Request, body: ProviderBody) -> JSONResponse:
    """Write the ``[provider]`` block into the configuration file and re-open the provider.

    ``400 VALIDATION_ERROR`` names a value the provider model rejects; ``409 CONFLICT`` means the
    file changed since ``base_digest`` was read, and nothing was written.
    """
    values = body.model_dump(exclude_none=True)
    values.pop("base_digest", None)
    save_provider(_config_path(request), values, base_digest=body.base_digest)
    _reload(request)
    return json_response(_document(request))


def _page(
    request: Request, *, notice: str = "", error: str = "", status_code: int = 200
) -> HTMLResponse:
    """Render the Provider page with whatever the last write had to say."""
    return HTMLResponse(
        render(
            "providers/index.html",
            app_version=__version__,
            page="provider",
            document=_document(request),
            notice=notice,
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/provider", response_class=HTMLResponse)
def provider_page(request: Request) -> HTMLResponse:
    """Render the configured provider as an editable form."""
    return _page(request)


@router.post("/provider", response_model=None)
async def provider_form(request: Request) -> HTMLResponse | RedirectResponse:
    """The Provider form: the block's keys, written in place (CSRF-checked)."""
    form = await request.form()
    values: dict[str, Any] = {}
    for field_name in WRITABLE_FIELDS:
        raw = form.get(field_name)
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        values[field_name] = float(text) if field_name in _NUMERIC_FIELDS and text else text
    digest = form.get("base_digest")
    try:
        save_provider(
            _config_path(request), values, base_digest=digest if isinstance(digest, str) else None
        )
    except SuiteError as exc:
        return _page(
            request,
            error=f"{exc.message} ({exc.code})",
            status_code=409 if exc.code == "CONFLICT" else 400,
        )
    _reload(request)
    return RedirectResponse("/provider", status_code=303)
