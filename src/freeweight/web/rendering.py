"""freeweight.web.rendering — the one Jinja environment every page renders through.

Since Phase 12 this is MirrorWall's environment, not a local one: the shell, the component macros,
the design tokens and the shared filters come from the package, and this module supplies only what
is FreeWeight's — the product name, the navigation, the theme-storage key the pre-Phase-12 UI
already used (so a user's stored choice survives the adoption), and the template directory holding
this application's own pages.

Built once and cached: templates are compiled and cached on the environment, so a per-request
environment would recompile the layout on every page view and quietly discard the cache that makes
the second view fast.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mirrorwall import CSRF_FIELD_NAME, create_template_environment

from freeweight.__about__ import __version__

if TYPE_CHECKING:
    from jinja2 import Environment

__all__ = [
    "APP_TABS",
    "NAV_ITEMS",
    "TELEMETRY_STREAM_URL",
    "configure_shell",
    "render",
    "templates",
]

_TEMPLATES_DIR = Path(__file__).parent / "templates"

TELEMETRY_STREAM_URL = "/api/v1/system/telemetry/stream"

APP_TABS: tuple[tuple[str, str], ...] = (
    ("freeweight", "FreeWeight"),
    ("loadcoach", "LoadCoach"),
    ("ideapress", "IdeaPress"),
    ("promptcadence", "PromptCadence"),
)
"""The suite's four applications, in the console's order, for the top-bar tab strip (row WM2).
Rendered only when ``[console] url`` is set; a peer is reached through the console
(``<url>/apps/<name>``), never on its own loopback port."""

NAV_ITEMS: tuple[dict[str, str], ...] = (
    {"key": "home", "href": "/", "label": "Overview"},
    {"key": "dashboard", "href": "/dashboard", "label": "Dashboard"},
    {"key": "machines", "href": "/machines", "label": "Machines"},
    {"key": "provider", "href": "/provider", "label": "Provider"},
    {"key": "models", "href": "/models", "label": "Models"},
    {"key": "runs", "href": "/runs", "label": "Runs"},
    {"key": "results", "href": "/results", "label": "Results"},
    {"key": "compare", "href": "/compare", "label": "Compare"},
    {"key": "evidence", "href": "/evidence", "label": "Evidence"},
    {"key": "sources", "href": "/sources", "label": "Sources"},
    {"key": "goals", "href": "/goals", "label": "Goals"},
    {"key": "database", "href": "/database", "label": "Database"},
    {"key": "system", "href": "/system", "label": "System"},
    {"key": "settings", "href": "/settings", "label": "Settings"},
)


@lru_cache(maxsize=1)
def templates() -> Environment:
    """Return the process-wide Jinja environment, building it on first use.

    MirrorWall supplies autoescaping, ``StrictUndefined`` and the shared filters; this function
    adds only the shell's slot values and one alias: ``bytes`` names MirrorWall's ``bytes_human``,
    because every pre-Phase-12 template already renders byte counts through that filter name and
    the two implementations agree (em dash for an absent value, never ``0``).
    """
    environment = create_template_environment(
        app_template_dirs=(_TEMPLATES_DIR,),
        globals_={
            "product_name": "FreeWeight",
            "product_version": __version__,
            "nav_items": NAV_ITEMS,
            # The key the pre-Phase-12 shell already wrote; keeping it means a user's stored
            # theme choice survives the MirrorWall adoption instead of silently resetting.
            "theme_storage_key": "freeweight-theme",
            "show_telemetry_bar": True,
            "telemetry_stream_url": TELEMETRY_STREAM_URL,
            # MirrorWall 0.3 opt-ins (row WM2, design brief §6): inline meters on the four
            # telemetry groups, the product name as a link home, and the tab strip — which
            # renders nothing until `configure_shell` hands it a console URL.
            "telemetry_field_meters": ["cpu", "ram", "gpu", "vram"],
            "product_href": "/",
            "console_url": "",
            "app_tabs": APP_TABS,
            "own_app": "freeweight",
            # CSRF defaults so a form's `_csrf` partial renders under StrictUndefined on any page.
            # A form page rendered through `web.csrf.render_form_page` overrides these with a real
            # token and sets the matching `__Host-` cookie; a non-form page keeps the empty token,
            # which its forms (there are none) never submit (ADR-0026 §2).
            "csrf_token": "",
            "csrf_field_name": CSRF_FIELD_NAME,
        },
    )
    environment.filters["bytes"] = environment.filters["bytes_human"]
    return environment


def configure_shell(*, console_url: str) -> None:
    """Hand the shell what only the running configuration knows.

    Args:
        console_url: ``[console] url`` — WeightRoomGym's base URL, or ``""`` for no tab strip.
    """
    templates().globals["console_url"] = console_url.rstrip("/")


def render(template_name: str, /, **context: Any) -> str:
    """Render ``template_name`` with ``context``.

    Args:
        template_name: Path relative to ``web/templates/``, e.g. ``"models/index.html"``.
        **context: Template variables.

    Returns:
        The rendered HTML.
    """
    # The CSRF token the request bound (empty outside a request, or on a page with no form).
    # Injected centrally so no route has to remember it (ADR-0026 §2); an explicit value in
    # ``context`` still wins, which keeps a test that renders a template in isolation in control.
    from freeweight.web.csrf import current_csrf_token

    context.setdefault("csrf_token", current_csrf_token())
    return templates().get_template(template_name).render(**context)
