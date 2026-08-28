"""freeweight.services.settings — the settings a running server may change, and the ones it may not.

Configuration standards §7 draws one line and this module is that line in code. A small, declared
set of settings is editable at runtime and stored in the application database; everything
security-relevant — the bind address, the exposure flag, auth tokens, the remote-provider
allowance, the database URL, the data root — is **file, environment or CLI only** and is refused
here with ``403 FORBIDDEN`` naming the key (API §8).

The refusal is by allowlist, not by blocklist. :data:`RUNTIME_SETTINGS` enumerates what may be
changed; anything not in it is refused whether or not anyone remembered to add it to a list of
dangerous keys. A new security-relevant setting is therefore config-only by default, which is the
direction a mistake should fall in.

Precedence is the documented one: ``defaults → file → database → env → CLI``. A database-backed
value therefore does **not** win over an environment variable, and the service says so rather than
letting the UI show a stored value the running server is not using — a settings page that lies
about what is in effect is worse than no settings page.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import SuiteError, ValidationError, utc_now

from freeweight.config import ENV_PREFIX, Settings

if TYPE_CHECKING:
    from datetime import datetime

    from freeweight.services.database import Database

__all__ = [
    "CONFIG_ONLY_KEYS",
    "RUNTIME_SETTINGS",
    "RuntimeSetting",
    "SettingConfigOnly",
    "SettingUnknown",
    "SettingView",
    "read_settings",
    "update_settings",
]

logger = logging.getLogger(__name__)

SETTINGS_KEY_PREFIX = "runtime."
"""Namespace for runtime-changeable values in the ``settings`` table.

Prefixed so that operational keys the application stores for itself — the last discovery time,
say — cannot be reached by name through this API."""


class SettingConfigOnly(SuiteError):
    """This setting is security-relevant and is never editable from the UI.

    ``FORBIDDEN`` rather than ``VALIDATION_ERROR``: the request was well-formed and the key
    exists. What was refused was the *authority* to change it from here (API §8).
    """

    code: ClassVar[str] = "FORBIDDEN"


class SettingUnknown(ValidationError):
    """No such runtime-changeable setting."""


@dataclass(frozen=True, slots=True)
class RuntimeSetting:
    """One setting the UI may change.

    Attributes:
        key: The dotted path into :class:`~freeweight.config.Settings`, e.g.
            ``"telemetry.interval_ms"``.
        kind: ``"int"``, ``"float"``, ``"bool"`` or ``"choice"``.
        description: What it does, in the words the settings page shows.
        minimum: Inclusive lower bound for a number.
        maximum: Inclusive upper bound for a number.
        choices: The permitted values for ``"choice"``.
        unit: The unit, when the value has one. Rendered beside the field, because a number
            without its unit in the UI is the same defect as a number without its unit in a name.
    """

    key: str
    kind: str
    description: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    unit: str | None = None

    @property
    def section(self) -> str:
        """The settings section this key lives in."""
        return self.key.split(".", 1)[0]

    @property
    def field(self) -> str:
        """The field name within its section."""
        return self.key.split(".", 1)[1]

    @property
    def env_var(self) -> str:
        """The environment variable that overrides this setting."""
        return f"{ENV_PREFIX}{self.section.upper()}__{self.field.upper()}"


RUNTIME_SETTINGS: tuple[RuntimeSetting, ...] = (
    RuntimeSetting(
        key="telemetry.interval_ms",
        kind="int",
        description=(
            "How often the machine telemetry sampler reads the host and the GPUs. Lower is more "
            "detail and more overhead; the overhead is measured and recorded on every run."
        ),
        minimum=100,
        maximum=60_000,
        unit="ms",
    ),
    RuntimeSetting(
        key="telemetry.persist_during_runs",
        kind="bool",
        description="Store the telemetry series alongside a run, so its charts survive a restart.",
    ),
    RuntimeSetting(
        key="telemetry.calibrate_overhead",
        kind="bool",
        description=(
            "Measure what sampling itself costs before a run, and record it on the run. Turning "
            "this off does not make sampling free; it makes its cost unrecorded."
        ),
    ),
    RuntimeSetting(
        key="execution.warmup_repetitions",
        kind="int",
        description="Unmeasured generations before the measured ones, to reach a warm state.",
        minimum=0,
        maximum=20,
        unit="repetitions",
    ),
    RuntimeSetting(
        key="execution.measured_repetitions",
        kind="int",
        description="How many times each case runs. More repetitions, tighter dispersion.",
        minimum=1,
        maximum=50,
        unit="repetitions",
    ),
    RuntimeSetting(
        key="execution.cooldown_seconds",
        kind="float",
        description="Idle gap between tests, so one test's heat is not the next test's result.",
        minimum=0.0,
        maximum=600.0,
        unit="s",
    ),
    RuntimeSetting(
        key="execution.randomize_case_order",
        kind="bool",
        description="Shuffle case order within a test, so position cannot become a result.",
    ),
    RuntimeSetting(
        key="execution.seed",
        kind="int",
        description="The seed every randomized decision derives from. Recorded in every run.",
        minimum=0,
        maximum=2**31 - 1,
    ),
    RuntimeSetting(
        key="execution.idle_gpu_threshold_percent",
        kind="float",
        description=(
            "Utilization the GPU must be below before a run is measured. 0 disables the check "
            "and records that it was disabled."
        ),
        minimum=0.0,
        maximum=100.0,
        unit="%",
    ),
    RuntimeSetting(
        key="execution.on_idle_timeout",
        kind="choice",
        description=(
            "What to do when the GPU never goes idle: warn and measure anyway, recording "
            "measured_while_busy, or refuse to measure at all."
        ),
        choices=("warn", "refuse"),
    ),
    RuntimeSetting(
        key="storage.backup_retention",
        kind="int",
        description="How many automatic backups to keep before rotating the oldest away.",
        minimum=0,
        maximum=100,
        unit="backups",
    ),
    RuntimeSetting(
        key="logging.level",
        kind="choice",
        description="Log verbosity for the running server.",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
)
"""Every setting the UI may change, and nothing else.

Deliberately short. Each entry is a setting whose wrong value costs a measurement, not a setting
whose wrong value costs a user their machine's security boundary — those are below."""

CONFIG_ONLY_KEYS: frozenset[str] = frozenset(
    {
        "server.host",
        "server.port",
        "server.allow_lan_exposure",
        "server.allowed_hosts",
        "storage.database_url",
        "storage.artifact_dir",
        "provider.base_url",
        "providers.allow_remote",
        "judge.allow_remote",
        "auth.tokens",
        "goals.root",
        "external.root",
        "logging.include_content",
        "sandbox.tier",
    }
)
"""Keys that are refused with ``FORBIDDEN`` and named in the refusal.

Every one of them decides who can reach this machine, what leaves it, or where its data lives
(configuration standards §7). They are listed explicitly — rather than left to fall through the
allowlist as "unknown" — so the refusal can say *why* rather than "no such setting", which is the
difference between a user changing the file and a user filing a bug."""

_BY_KEY: Mapping[str, RuntimeSetting] = {setting.key: setting for setting in RUNTIME_SETTINGS}


@dataclass(frozen=True, slots=True)
class SettingView:
    """One runtime setting as the API and the settings page show it.

    Attributes:
        setting: Its declaration.
        effective_value: What the running server is actually using.
        stored_value: What the database holds, or ``None`` when nothing has been stored.
        source: Where ``effective_value`` came from: ``"env"``, ``"database"`` or
            ``"file or default"``.
        overridden_by_env: Whether an environment variable is winning over the stored value. When
            it is, the page says so beside the field instead of pretending the stored value is in
            effect.
    """

    setting: RuntimeSetting
    effective_value: Any
    stored_value: Any
    source: str
    overridden_by_env: bool

    def as_json(self) -> dict[str, Any]:
        """The wire form ``GET /api/v1/settings`` returns for one setting."""
        return {
            "key": self.setting.key,
            "value": self.effective_value,
            "stored_value": self.stored_value,
            "source": self.source,
            "overridden_by_env": self.overridden_by_env,
            "kind": self.setting.kind,
            "description": self.setting.description,
            "unit": self.setting.unit,
            "minimum": self.setting.minimum,
            "maximum": self.setting.maximum,
            "choices": list(self.setting.choices),
            "env_var": self.setting.env_var,
        }


def _effective(settings: Settings, setting: RuntimeSetting) -> Any:  # noqa: ANN401 — any scalar
    """The value the running server is using for one key."""
    return getattr(getattr(settings, setting.section), setting.field)


def _stored(database: Database, setting: RuntimeSetting) -> Any:  # noqa: ANN401 — any scalar
    """The value the database holds for one key, or ``None``."""
    from freeweight.infrastructure.db.repositories.settings import SettingsRepository

    with database.read() as session:
        return SettingsRepository().get(session, SETTINGS_KEY_PREFIX + setting.key)


def _stored_or_none(database: Database, setting: RuntimeSetting) -> Any:  # noqa: ANN401
    """:func:`_stored`, but a database that cannot be read simply has nothing stored.

    Used only by :func:`apply_stored`, which runs during startup — before anything has had a
    chance to report that the database is unmigrated or unreadable. A database with no ``settings``
    table has no stored overrides by definition, and failing startup over it would replace a page
    that says "run `freeweight db upgrade`" with a server that will not start at all.

    Every *other* caller uses :func:`_stored` and lets the failure through, because a settings page
    that silently reported "file or default" for a database it could not read would be lying.
    """
    try:
        return _stored(database, setting)
    except Exception:  # noqa: BLE001 — an unreadable database has no overrides, by definition
        logger.debug("settings.stored_unavailable", extra={"key": setting.key})
        return None


def read_settings(database: Database, settings: Settings) -> tuple[SettingView, ...]:
    """Return every runtime-changeable setting with its effective value and its source.

    Args:
        database: The application's database handle.
        settings: The configuration the server is running with.

    Returns:
        One view per entry in :data:`RUNTIME_SETTINGS`, in declaration order.
    """
    views: list[SettingView] = []
    for setting in RUNTIME_SETTINGS:
        stored = _stored(database, setting)
        from_env = setting.env_var in os.environ
        source = "env" if from_env else ("database" if stored is not None else "file or default")
        views.append(
            SettingView(
                setting=setting,
                effective_value=_effective(settings, setting),
                stored_value=stored,
                source=source,
                overridden_by_env=from_env and stored is not None,
            )
        )
    return tuple(views)


def _coerce(setting: RuntimeSetting, raw: Any) -> Any:  # noqa: ANN401 — untrusted input
    """Coerce and bound-check one submitted value.

    Raises:
        ValidationError: The value is the wrong shape, or outside the declared range.
    """
    try:
        if setting.kind == "bool":
            value: Any = (
                raw
                if isinstance(raw, bool)
                else str(raw).strip().lower() in {"1", "true", "yes", "on"}
            )
        elif setting.kind == "int":
            value = int(raw)
        elif setting.kind == "float":
            value = float(raw)
        else:
            value = str(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{setting.key} expects {setting.kind}; got {raw!r}.",
            details={"field": setting.key, "value": raw},
        ) from exc
    if setting.kind == "choice" and value not in setting.choices:
        raise ValidationError(
            f"{setting.key} must be one of {', '.join(setting.choices)}; got {value!r}.",
            details={"field": setting.key, "choices": list(setting.choices)},
        )
    if setting.minimum is not None and float(value) < setting.minimum:
        raise ValidationError(
            f"{setting.key} must be at least {setting.minimum}; got {value}.",
            details={"field": setting.key, "minimum": setting.minimum},
        )
    if setting.maximum is not None and float(value) > setting.maximum:
        raise ValidationError(
            f"{setting.key} must be at most {setting.maximum}; got {value}.",
            details={"field": setting.key, "maximum": setting.maximum},
        )
    return value


def _check_writable(key: str) -> RuntimeSetting:
    """Return the declaration for ``key``, or refuse it.

    Raises:
        SettingConfigOnly: The key is security-relevant and is file/env/CLI only.
        SettingUnknown: There is no such runtime-changeable setting.
    """
    setting = _BY_KEY.get(key)
    if setting is not None:
        return setting
    if key in CONFIG_ONLY_KEYS:
        raise SettingConfigOnly(
            f"{key} is security-relevant and is set in the configuration file, the environment "
            "or on the command line only — never from the UI (configuration standards §7). Edit "
            "config.toml and restart.",
            details={"field": key, "config_only": True},
        )
    raise SettingUnknown(
        f"{key} is not a runtime-changeable setting.",
        details={"field": key, "known": sorted(_BY_KEY)},
    )


def update_settings(
    database: Database,
    settings: Settings,
    changes: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[SettingView, ...]:
    """Store new values for runtime-changeable settings.

    Every change is validated before **any** change is written, so a request that names one good
    key and one forbidden one changes nothing. A partially applied settings update is the kind of
    state a user cannot reason about afterwards.

    The stored value takes effect for work started after it is written. Settings the running
    process captured at startup — the telemetry sampler's interval, for one — are re-read on the
    next run rather than mid-flight, because changing a measurement's conditions while it is being
    measured is how a run stops being reproducible.

    Args:
        database: The application's database handle.
        settings: The configuration the server is running with.
        changes: ``{dotted key: value}``.
        now: Injected clock for the stored timestamp.

    Returns:
        Every setting, re-read, so a caller sees the whole state rather than the delta.

    Raises:
        SettingConfigOnly: A named key is security-relevant.
        SettingUnknown: A named key is not runtime-changeable.
        ValidationError: A value is the wrong type or out of range.
    """
    from freeweight.infrastructure.db.repositories.settings import SettingsRepository

    validated: list[tuple[str, Any]] = [
        (key, _coerce(_check_writable(key), raw)) for key, raw in changes.items()
    ]
    stamp = now if now is not None else utc_now()
    repository = SettingsRepository()
    with database.write() as session:
        for key, value in validated:
            repository.set(session, SETTINGS_KEY_PREFIX + key, value, now=stamp)
    return read_settings(database, settings)


def apply_stored(database: Database, settings: Settings) -> Settings:
    """Return ``settings`` with stored runtime values folded in at their documented precedence.

    ``defaults → file → **database** → env → CLI``: a stored value overrides the file and is
    overridden by the environment. This is the one deviation from configuration standards §1, it
    exists only for settings a UI can change, and it is applied in exactly one place — here — so
    no caller has to remember the ordering.

    Args:
        database: The application's database handle.
        settings: The configuration resolved from file, environment and CLI.

    Returns:
        A new :class:`~freeweight.config.Settings`; the argument is not mutated.
    """
    overrides: dict[str, dict[str, Any]] = {}
    for setting in RUNTIME_SETTINGS:
        if setting.env_var in os.environ:
            continue
        stored = _stored_or_none(database, setting)
        if stored is None:
            continue
        overrides.setdefault(setting.section, {})[setting.field] = stored
    if not overrides:
        return settings
    data = settings.model_dump()
    for section, fields in overrides.items():
        data[section].update(fields)
    return Settings.model_validate(data)


def config_only_keys() -> Sequence[str]:
    """The security-relevant keys, sorted, for the settings page's "config only" list."""
    return sorted(CONFIG_ONLY_KEYS)
