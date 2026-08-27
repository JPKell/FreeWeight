"""freeweight.web.rendering — the one Jinja environment every page renders through.

One environment for the whole application, built once: templates are compiled and cached on it,
so a per-request environment would recompile the layout on every page view and quietly discard the
cache that makes the second view fast.

Autoescaping is on for HTML by default (:func:`jinja2.select_autoescape`) — a model name, a
hostname and a provider error message all reach a template from outside this process, and none of
them are trusted markup.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from baseaicore.timeutil import to_rfc3339
from jinja2 import Environment, FileSystemLoader, select_autoescape

__all__ = ["render", "templates"]

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _format_bytes(value: int | None) -> str:
    """Render a byte count at human scale; ``None`` becomes an em dash, never ``0``.

    An unreported RAM total and a machine with no RAM are different facts, and UI standards §3 is
    explicit that an unavailable reading shows ``—`` rather than a zero someone might average.
    """
    if value is None:
        return "\u2014"
    if value < 1024:
        return f"{value} B"
    scaled = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        scaled /= 1024
        if scaled < 1024 or unit == "TiB":
            return f"{scaled:.1f} {unit}"
    raise AssertionError("unreachable: the TiB branch always returns")  # pragma: no cover


def _format_timestamp(value: datetime | None) -> str:
    """Render an instant as RFC 3339 in UTC; ``None`` becomes an em dash."""
    if value is None:
        return "\u2014"
    return to_rfc3339(value)


@lru_cache(maxsize=1)
def templates() -> Environment:
    """Return the process-wide Jinja environment, building it on first use."""
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(),
        auto_reload=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["bytes"] = _format_bytes
    environment.filters["timestamp"] = _format_timestamp
    return environment


def render(template_name: str, /, **context: Any) -> str:
    """Render ``template_name`` with ``context``.

    Args:
        template_name: Path relative to ``web/templates/``, e.g. ``"models/index.html"``.
        **context: Template variables.

    Returns:
        The rendered HTML.
    """
    return templates().get_template(template_name).render(**context)
