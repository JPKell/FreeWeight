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
    from modelrack.provider import Provider
    from sweatmeter import TelemetryCollector

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
_DEGRADED_SEVERITY = _SEVERITY["degraded"]

# Graceful Degradation §3: "status for the whole application is the worst component status that is
# *required* for its core function. Optional components never make the application unavailable."
# FreeWeight's own dependencies (spec §5) name none as required at startup — the database is the one
# exception, since without it virtually every page and command has nothing to read or write.
_REQUIRED_COMPONENTS: frozenset[str] = frozenset({"database"})


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


def _provider_component(provider: Provider | None) -> HealthComponent:
    """Build the ``provider`` component, tolerating a totally unreachable or unconfigured provider.

    Mirrors :func:`_database_component`'s shape: never raises, and opens its own one-shot provider
    from configuration when the caller (a one-shot ``freeweight health``) has none already built.
    :meth:`~modelrack.provider.Provider.health` itself never raises (its own contract), so the only
    failure this function must absorb is a bad ``provider.kind`` in configuration.

    Args:
        provider: The caller's handle, or ``None`` to build one for this check alone. The web
            application passes the provider it serves from — reused rather than rebuilt, for the
            same connection-pooling reason :func:`_database_component` reuses the database handle.
    """
    from modelrack.provider import ProviderStatus

    if provider is None:
        from freeweight.config import ConfigurationError, load_settings
        from freeweight.infrastructure.providers.factory import build_provider

        try:
            loaded = load_settings()
            provider = build_provider(loaded.settings.provider)
        except ConfigurationError as exc:
            return HealthComponent(
                name="provider", status="degraded", detail=f"configuration: {exc.message}"
            )

    health = provider.health()
    status: ComponentStatus = (
        "ok"
        if health.status is ProviderStatus.OK
        else "degraded"
        if health.status is ProviderStatus.DEGRADED
        else "unavailable"
    )
    return HealthComponent(name="provider", status=status, detail=health.detail)


def _gpu_telemetry_component(collector: TelemetryCollector | None) -> HealthComponent:
    """Build the ``gpu_telemetry`` component, tolerating a machine with no GPU at all.

    Mirrors :func:`_database_component`'s shape: the caller's shared collector is reused when
    given (the web application's own sampler collector), or a one-shot collector is built for a
    standalone ``freeweight health``/``freeweight doctor``.

    A machine with no GPU is a real, common configuration, not a fault — but the phase this
    component ships in is explicit that it degrades overall health rather than reading as
    ``not_configured``, so a user watching the health page notices the moment a GPU that should be
    there stops answering, exactly as they would notice the database going away.

    Args:
        collector: The caller's collector, or ``None`` to build one for this check alone.

    Returns:
        ``ok`` with the GPU count when at least one device reported without error; ``degraded``
        naming the reason otherwise (SweatMeter's own diagnostic string, or ``"no_gpus"``).
    """
    if collector is None:
        from freeweight.services.telemetry import build_collector

        collector = build_collector()
    snapshot = collector.snapshot()
    reason = snapshot.unavailable_reasons().get("gpu")
    if snapshot.gpus and reason is None:
        count = len(snapshot.gpus)
        return HealthComponent(
            name="gpu_telemetry", status="ok", detail=f"{count} GPU(s) reporting"
        )
    return HealthComponent(
        name="gpu_telemetry",
        status="degraded",
        detail=f"no GPU telemetry available ({reason or 'no_gpus'})",
    )


def _machine_component(collector: TelemetryCollector | None) -> HealthComponent:
    """Build the ``machine`` component from this host's static profile.

    ``machine_profile()`` never raises (SweatMeter spec §11.1), so the only degraded case is a
    platform SweatMeter could not identify at all — the ``NullHostReader`` degrade path — signalled
    here by the most basic identity facts (``hostname``, ``os_name``) both being unreported.

    Args:
        collector: The caller's collector, or ``None`` to build one for this check alone.

    Returns:
        ``ok`` naming the machine's fingerprint when the host platform was identified; ``degraded``
        otherwise.
    """
    if collector is None:
        from freeweight.services.telemetry import build_collector

        collector = build_collector()
    profile = collector.machine_profile()
    if profile.hostname is not None and profile.os_name is not None:
        return HealthComponent(
            name="machine",
            status="ok",
            detail=f"identified as {profile.machine_fingerprint[:12]}…",
        )
    return HealthComponent(
        name="machine", status="degraded", detail="host platform not fully identified"
    )


def get_health_report(
    *,
    database: Database | None = None,
    provider: Provider | None = None,
    telemetry: TelemetryCollector | None = None,
    clock: Clock = utc_now,
) -> HealthReport:
    """Build the current health report.

    Both ``GET /api/v1/health`` and ``freeweight health`` call this one function, which is what
    keeps the two surfaces identical by construction. They differ only in where the connection and
    the provider come from: the route passes the handle and provider the server is already serving
    from, so the check reports on what requests actually use; the CLI passes neither and one of each
    is opened for the check alone.

    Args:
        database: The caller's database handle, or ``None`` to open one for this check alone.
        provider: The caller's provider handle, or ``None`` to build one for this check alone.
        telemetry: The caller's SweatMeter collector, or ``None`` to build one for this check
            alone. Backs both ``gpu_telemetry`` and ``machine``.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The :class:`HealthReport`, worst-component-first. The overall status is the worst of the
        required components (``database``) uncapped, joined with the worst of every optional one
        (``provider``, ``gpu_telemetry``, ``machine``) capped at ``degraded`` — an unreachable
        provider, or a machine with no GPU, is never by itself what makes the whole application
        ``unavailable`` (Graceful Degradation §3).
    """
    components: tuple[HealthComponent, ...] = (
        _database_component(database),
        _provider_component(provider),
        _gpu_telemetry_component(telemetry),
        _machine_component(telemetry),
    )
    worst = max(
        (
            _SEVERITY[component.status]
            if component.name in _REQUIRED_COMPONENTS
            else min(_SEVERITY[component.status], _DEGRADED_SEVERITY)
            for component in components
        ),
        default=0,
    )
    overall: OverallStatus = "unavailable" if worst >= 2 else "degraded" if worst == 1 else "ok"
    return HealthReport(
        status=overall,
        version=__version__,
        checked_at=to_rfc3339(clock()),
        components=components,
    )
