"""freeweight.services.health — the one health report shared by the API and the CLI.

Both ``GET /api/v1/health`` and ``freeweight health`` call :func:`get_health_report`, which is how
Phase 1's acceptance criterion — the two surfaces return identical component data — holds by
construction rather than by coincidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from baseaicore.timeutil import Clock, to_rfc3339, utc_now
from pydantic import BaseModel, ConfigDict

from freeweight.__about__ import __version__

if TYPE_CHECKING:
    from freeweight.services.database import Database

__all__ = ["HealthComponent", "HealthReport", "get_health_report"]

type ComponentStatus = Literal["ok", "degraded", "unavailable", "not_configured"]
type OverallStatus = Literal["ok", "degraded", "unavailable"]

_SEVERITY: dict[ComponentStatus, int] = {
    "ok": 0,
    "not_configured": 0,
    "degraded": 1,
    "unavailable": 2,
}


class HealthComponent(BaseModel):
    """One dependency's status, per Graceful Degradation §3."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: ComponentStatus
    detail: str


class HealthReport(BaseModel):
    """The application-wide health shape, identical across the HTTP API and the CLI."""

    model_config = ConfigDict(extra="forbid")

    status: OverallStatus
    version: str
    checked_at: str
    components: tuple[HealthComponent, ...] = ()


def _database_component(database: Database | None) -> HealthComponent:
    """Build the ``database`` component, tolerating a totally unreadable configuration.

    Imported lazily to avoid a cycle: :mod:`freeweight.services.database` imports
    :class:`HealthComponent` from this module, so this module cannot import it back at module
    level. A configuration that fails to load at all becomes a ``degraded`` component rather than
    an exception, for the same reason
    :func:`~freeweight.services.database.database_health_component` never raises: a health check
    must not be the thing that takes health reporting down.

    Args:
        database: The caller's handle, or ``None`` to open one for this check alone. The web
            application passes the handle it serves from — checking the health of a *different*
            connection than the one requests use would report on something nobody is using. A
            one-shot ``freeweight health`` has no such handle and passes ``None``.
    """
    from freeweight.config import ConfigurationError, load_settings
    from freeweight.services.database import Database, database_health_component

    if database is not None:
        return database_health_component(database)

    try:
        loaded = load_settings()
    except ConfigurationError as exc:
        return HealthComponent(
            name="database", status="degraded", detail=f"configuration: {exc.message}"
        )
    database_url = loaded.settings.storage.database_url
    if database_url is None:
        # Unreachable via load_settings(): StorageSettings always fills this in. Handled anyway
        # because a health check silently trusting an invariant is exactly the kind of assumption
        # this function exists to not make.
        return HealthComponent(
            name="database", status="degraded", detail="no database_url configured"
        )
    with Database.from_url(
        database_url, statement_timeout_ms=loaded.settings.storage.statement_timeout_ms
    ) as opened:
        return database_health_component(opened)


def get_health_report(*, database: Database | None = None, clock: Clock = utc_now) -> HealthReport:
    """Build the current health report.

    Both ``GET /api/v1/health`` and ``freeweight health`` call this one function, which is what
    keeps the two surfaces identical by construction. They differ only in where the connection
    comes from: the route passes the handle the server is already serving from, so the check
    reports on the connection requests actually use; the CLI passes nothing and one is opened for
    the check alone.

    Args:
        database: The caller's database handle, or ``None`` to open one for this check alone.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The :class:`HealthReport`, worst-component-first.
    """
    components: tuple[HealthComponent, ...] = (_database_component(database),)
    worst = max((_SEVERITY[component.status] for component in components), default=0)
    overall: OverallStatus = "unavailable" if worst >= 2 else "degraded" if worst == 1 else "ok"
    return HealthReport(
        status=overall,
        version=__version__,
        checked_at=to_rfc3339(clock()),
        components=components,
    )
