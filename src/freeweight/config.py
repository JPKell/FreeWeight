"""freeweight.config — typed settings, source-tracked, per Configuration Standards.

Precedence, lowest to highest: built-in defaults, ``config.toml``, ``FREEWEIGHT_``-prefixed
environment variables, then explicit overrides (the CLI's highest layer). Overriding is per leaf
field, not per section (§1): setting one field of ``[server]`` never discards its siblings.

This module performs its own merge of the file, environment and override layers rather than
leaning on ``pydantic-settings``'s own source-priority machinery, because ``config show`` has to
report *which* layer produced every leaf value (§1) — a property that is easiest to get right by
building the merged dict ourselves and tracking provenance alongside it, then handing the result to
pydantic once for validation.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from baseaicore import ConfigurationError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)

__all__ = [
    "EXAMPLE_CONFIG_TOML",
    "ENV_PREFIX",
    "LOOPBACK_HOSTS",
    "AuthSettings",
    "ConfigurationError",
    "InsecureBindingError",
    "LoadedSettings",
    "LoggingSettings",
    "ProviderSettings",
    "ProvidersSettings",
    "ServerSettings",
    "Settings",
    "StorageSettings",
    "TelemetrySettings",
    "config_dir",
    "data_dir",
    "load_settings",
    "resolve_config_path",
    "state_dir",
]

ENV_PREFIX = "FREEWEIGHT_"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_ALL_INTERFACES_HOST = "0.0.0.0"  # noqa: S104 — compared against, never bound to, by this module
_RESERVED_ENV_SUFFIXES = frozenset({"CONFIG", "DATA_DIR", "LOG_LEVEL"})


class InsecureBindingError(ConfigurationError):
    """A configured bind/auth combination would expose the service unsafely.

    Raised by :func:`load_settings` before anything opens a socket (Configuration Standards §4).
    Every rule here has a documented, deliberate acknowledgement that lifts it; none can be
    satisfied by accident.

    Attributes:
        code: ``"INSECURE_BINDING"``, stable and part of the public contract.
    """

    code: ClassVar[str] = "INSECURE_BINDING"


def _split_csv(value: Any) -> Any:
    """Accept a comma-separated string for a tuple field, as environment variables must (§3)."""
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return value


class ServerSettings(BaseModel):
    """Bind address and HTTP-level limits."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    allow_lan_exposure: bool = False
    allowed_hosts: tuple[str, ...] = ()
    request_timeout_seconds: float = Field(default=120.0, gt=0)

    _split_allowed_hosts = field_validator("allowed_hosts", mode="before")(_split_csv)


class StorageSettings(BaseModel):
    """Database and artifact locations.

    Phase 2 is the first phase that opens the database this describes; the fields exist now so
    ``config show``/``config init`` present the full, honest picture of what a fresh install would
    use, per Configuration Standards §5.
    """

    model_config = ConfigDict(extra="forbid")

    database_url: str | None = None
    auto_migrate: bool = True
    artifact_dir: str | None = None
    retention_days: int = Field(default=0, ge=0)
    backup_retention: int = Field(default=5, ge=0)
    statement_timeout_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _apply_data_dir_defaults(self) -> StorageSettings:
        """Fill in the zero-configuration defaults, resolved against the XDG data directory."""
        if self.database_url is None:
            self.database_url = f"sqlite:///{data_dir()}/freeweight.sqlite3"
        if self.artifact_dir is None:
            self.artifact_dir = str(data_dir() / "artifacts")
        if "auto_migrate" not in self.model_fields_set:
            self.auto_migrate = self._auto_migrate_default()
        return self

    def _auto_migrate_default(self) -> bool:
        """Migrate on startup by default on SQLite, never on PostgreSQL.

        Database standards §5.1 and §7: the automatic restore-on-failure guarantee is SQLite-only,
        "which is why its ``auto_migrate`` defaults to off" for PostgreSQL. A single ``True``
        default for both dialects would silently migrate a shared PostgreSQL database on the first
        startup of a newly deployed version, with no rollback available if that migration fails —
        the one case the standard singles out as unacceptable.

        Only the *default* is dialect-dependent. An operator who writes ``auto_migrate`` in
        ``config.toml`` or sets ``FREEWEIGHT_STORAGE__AUTO_MIGRATE`` gets exactly what they asked
        for on either dialect; ``model_fields_set`` is what distinguishes "unset" from "set to the
        same value the default would have chosen".
        """
        url = self.database_url or ""
        return url.startswith("sqlite")


class ProviderSettings(BaseModel):
    """The default model provider FreeWeight talks to."""

    model_config = ConfigDict(extra="forbid")

    kind: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: float = Field(default=300.0, gt=0)


class ProvidersSettings(BaseModel):
    """Cross-provider policy, distinct from the single default provider's own settings."""

    model_config = ConfigDict(extra="forbid")

    allow_remote: bool = False


class TelemetrySettings(BaseModel):
    """Sampling behaviour for the (Phase 4) telemetry bar and (Phase 6) run recording."""

    model_config = ConfigDict(extra="forbid")

    interval_ms: int = Field(default=1000, gt=0)
    persist_during_runs: bool = True
    calibrate_overhead: bool = True


class AuthSettings(BaseModel):
    """Bearer tokens. Empty is the loopback-default, unauthenticated posture (ADR-0014)."""

    model_config = ConfigDict(extra="forbid")

    tokens: tuple[str, ...] = ()

    _split_tokens = field_validator("tokens", mode="before")(_split_csv)


class LoggingSettings(BaseModel):
    """Structured-logging behaviour."""

    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    format: Literal["text", "json", "auto"] = "auto"
    include_content: bool = False


class Settings(BaseModel):
    """The complete, validated FreeWeight configuration.

    Constructed only by :func:`load_settings`, which resolves the precedence chain first — never
    call ``Settings(**raw_dict)`` directly on unmerged input, or the file/env/CLI layering in
    Configuration Standards §1 is bypassed.
    """

    model_config = ConfigDict(extra="forbid")

    server: ServerSettings = Field(default_factory=ServerSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    providers: ProvidersSettings = Field(default_factory=ProvidersSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


@dataclass(frozen=True, slots=True)
class LoadedSettings:
    """The result of resolving configuration: the settings, and where every value came from."""

    settings: Settings
    config_path: Path
    config_file_used: bool
    sources: dict[str, str]


def config_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/freeweight``, falling back to ``~/.config/freeweight``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "freeweight"


def data_dir() -> Path:
    """Return ``$FREEWEIGHT_DATA_DIR``, else ``$XDG_DATA_HOME/freeweight``, else the XDG default."""
    override = os.environ.get(f"{ENV_PREFIX}DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "freeweight"


def state_dir() -> Path:
    """Return ``$XDG_STATE_HOME/freeweight``, falling back to ``~/.local/state/freeweight``."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "freeweight"


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the configuration file location per Configuration Standards §2.

    Order: an explicit path (``--config``), then ``FREEWEIGHT_CONFIG``, then a project-local
    ``./freeweight.toml`` if one exists in the current directory, then the XDG default. A missing
    file at the resolved path is not an error — :func:`load_settings` falls back to defaults.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    local = Path.cwd() / "freeweight.toml"
    if local.is_file():
        return local
    return config_dir() / "config.toml"


def _read_env(prefix: str) -> dict[str, Any]:
    """Parse ``<prefix>SECTION__FIELD`` environment variables into a nested dict.

    Reserved suffixes (``CONFIG``, ``DATA_DIR``, ``LOG_LEVEL``) are excluded from the generic
    nested parse; ``LOG_LEVEL`` is instead wired to ``logging.level`` as a documented convenience,
    at lower priority than an explicit ``FREEWEIGHT_LOGGING__LEVEL``.
    """
    nested: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if suffix in _RESERVED_ENV_SUFFIXES:
            continue
        path = suffix.lower().split("__")
        node = nested
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value

    log_level = os.environ.get(f"{prefix}LOG_LEVEL")
    if log_level and "level" not in nested.get("logging", {}):
        nested.setdefault("logging", {})["level"] = log_level
    return nested


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` onto ``base``, recursively, per leaf field rather than per section."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _known_dotted_keys() -> list[str]:
    """Every ``section`` and ``section.field`` name Settings recognizes, for typo suggestions."""
    known: list[str] = []
    for section_name, section_field in Settings.model_fields.items():
        known.append(section_name)
        section_model = section_field.annotation
        if isinstance(section_model, type) and issubclass(section_model, BaseModel):
            known.extend(
                f"{section_name}.{field_name}" for field_name in section_model.model_fields
            )
    return known


def _translate_validation_error(
    exc: PydanticValidationError, config_path: Path
) -> ConfigurationError:
    """Turn a pydantic ``ValidationError`` into a :class:`ConfigurationError` naming the field.

    Every problem is listed at once, each with the closest known key when the problem is an
    unrecognized one (Configuration Standards §4).
    """
    known_keys = _known_dotted_keys()
    problems: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        if error["type"] == "extra_forbidden":
            suggestion = difflib.get_close_matches(loc, known_keys, n=1)
            hint = f" (did you mean '{suggestion[0]}'?)" if suggestion else ""
            problems.append(f"unknown configuration key '{loc}'{hint}")
        else:
            problems.append(f"{loc}: {error['msg']} (got {error.get('input')!r})")
    message = f"Configuration invalid ({config_path}): " + "; ".join(problems)
    return ConfigurationError(message, details={"file": str(config_path), "problems": problems})


def _validate_security(settings: Settings) -> None:
    """Refuse the unsafe bind/auth combinations named in Configuration Standards §4."""
    server = settings.server
    if server.host == _ALL_INTERFACES_HOST and not server.allow_lan_exposure:
        raise InsecureBindingError(
            "server.host is '0.0.0.0' (all interfaces) but server.allow_lan_exposure is false. "
            "Exposing the service beyond this machine must be a deliberate act: set "
            "server.allow_lan_exposure = true if that is intended.",
            details={"field": "server.allow_lan_exposure", "host": server.host},
        )
    if server.host not in LOOPBACK_HOSTS:
        if not server.allowed_hosts:
            raise InsecureBindingError(
                "server.host is not loopback but server.allowed_hosts is empty. A non-loopback "
                "bind must name every hostname it will accept, or DNS rebinding can reach it.",
                details={"field": "server.allowed_hosts", "host": server.host},
            )
        if not settings.auth.tokens:
            raise InsecureBindingError(
                "server.host is not loopback but no auth.tokens are configured. A non-loopback "
                "bind must require at least one bearer token.",
                details={"field": "auth.tokens", "host": server.host},
            )


def _track_sources(
    file_data: dict[str, Any], env_data: dict[str, Any], cli_data: dict[str, Any]
) -> dict[str, str]:
    """Report, for every leaf field, which layer produced its effective value."""
    sources: dict[str, str] = {}
    for section_name, section_field in Settings.model_fields.items():
        section_model = section_field.annotation
        if not (isinstance(section_model, type) and issubclass(section_model, BaseModel)):
            continue
        for field_name in section_model.model_fields:
            path = f"{section_name}.{field_name}"
            if section_name in cli_data and field_name in cli_data[section_name]:
                sources[path] = "cli"
            elif section_name in env_data and field_name in env_data[section_name]:
                env_key = f"{ENV_PREFIX}{section_name.upper()}__{field_name.upper()}"
                sources[path] = f"env {env_key}"
            elif section_name in file_data and field_name in file_data[section_name]:
                sources[path] = "file"
            else:
                sources[path] = "default"
    return sources


def load_settings(
    *,
    config_path: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> LoadedSettings:
    """Resolve configuration through the full precedence chain and validate it.

    Args:
        config_path: An explicit ``--config`` path. See :func:`resolve_config_path` for the
            fallback order when this is ``None``.
        cli_overrides: Explicit values from CLI flags, nested the same way as the TOML file
            (``{"server": {"port": 9000}}``). This is the highest-precedence layer.

    Returns:
        The validated :class:`LoadedSettings`.

    Raises:
        ConfigurationError: The file is not valid TOML, a key is unrecognized, a value fails a
            field's type or range, or an unsafe bind/auth combination is configured
            (:class:`InsecureBindingError`, a subclass).
    """
    resolved_path = resolve_config_path(config_path)
    file_data: dict[str, Any] = {}
    file_used = False
    if resolved_path.is_file():
        try:
            with resolved_path.open("rb") as handle:
                file_data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(
                f"Configuration file {resolved_path} is not valid TOML: {exc}",
                details={"file": str(resolved_path)},
            ) from exc
        file_used = True

    env_data = _read_env(ENV_PREFIX)
    cli_data = cli_overrides or {}
    merged = _deep_merge(_deep_merge(file_data, env_data), cli_data)

    try:
        settings = Settings.model_validate(merged)
    except PydanticValidationError as exc:
        raise _translate_validation_error(exc, resolved_path) from exc

    _validate_security(settings)

    sources = _track_sources(file_data, env_data, cli_data)
    return LoadedSettings(
        settings=settings, config_path=resolved_path, config_file_used=file_used, sources=sources
    )


EXAMPLE_CONFIG_TOML = """\
# FreeWeight configuration.
# Every key below is optional; a fresh install with no file at all is fully functional.
# Precedence: defaults -> this file -> FREEWEIGHT_* environment variables -> CLI flags.

[server]
host = "127.0.0.1"
port = 8765
allow_lan_exposure = false
allowed_hosts = []          # required when host is not loopback
request_timeout_seconds = 120.0

[storage]
# database_url and artifact_dir default to locations under the XDG data directory.
# auto_migrate defaults to true on SQLite and false on PostgreSQL, where a failed migration
# cannot be rolled back automatically (database standards §5.1, §7). Set it explicitly to
# override that on either dialect.
retention_days = 0          # 0 = keep everything
backup_retention = 5        # automatic pre-migration backups to keep
# statement_timeout_ms applies to PostgreSQL only (also used as lock_timeout); unset = server
# default. SQLite's analogue is its busy_timeout, which the engine always sets.

[provider]
kind = "ollama"
base_url = "http://127.0.0.1:11434"
timeout_seconds = 300.0

[providers]
allow_remote = false

[telemetry]
interval_ms = 1000
persist_during_runs = true
calibrate_overhead = true

[auth]
tokens = []                 # required for a non-loopback bind

[logging]
level = "INFO"
format = "auto"              # text | json | auto (text on a TTY, json otherwise)
include_content = false
"""
