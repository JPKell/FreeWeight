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

from baseaicore import ConfigurationError, RuntimeProfile
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
    "BenchmarkSettings",
    "CalibrationSettings",
    "ConfigurationError",
    "GoalSettings",
    "InsecureBindingError",
    "JudgeSettings",
    "LoadedSettings",
    "LoggingSettings",
    "ProviderSettings",
    "RuntimeSettings",
    "ProvidersSettings",
    "ServerSettings",
    "Settings",
    "StorageSettings",
    "TelemetrySettings",
    "config_dir",
    "data_dir",
    "prompt_override_dir",
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


class RuntimeSettings(BaseModel):
    """``[runtime]`` — how a model is loaded and served, as opposed to how a run is executed.

    ADR-0023's runtime profile, made settable. It is a **separate section from ``[execution]``**
    because the two are separate axes and are separated everywhere else: an execution parameter
    (repetitions, seed, cooldown) is how a measurement is taken, while a runtime setting is what is
    being measured *under*. They hash into the fingerprint through different fields —
    ``execution.effective_parameters`` and ``runtime_profile_hash`` — and ADR-0017 makes a differing
    runtime profile a **hard separation**, not a discount: evidence measured at one context does not
    describe another.

    Every field defaults to ``None``, which is the legal profile meaning "provider defaults"
    (ADR-0023 §1). There is no "no profile" state; ``RuntimeProfile()`` has a stable hash and is
    stored like any other.

    **Only settings a provider can actually honour per request appear here.** Ollama configures
    flash attention and KV-cache precision at server startup (``OLLAMA_FLASH_ATTENTION``,
    ``OLLAMA_KV_CACHE_TYPE``), not per request, so offering them here would record a promise the
    run cannot keep — the same dishonesty ADR-0007 rule 2 forbids of a capability flag. They stay
    server-side until a provider exposes them.

    Attributes:
        context_size: The context to serve, in tokens — Ollama's ``num_ctx``. Unset means the
            provider decides, and the run records its served context as ``assumed`` rather than
            ``configured``. **Setting it is what makes a context comparison possible**: two runs of
            one model at two contexts are two subjects, and without this they are indistinguishable.
        gpu_layers: Layers to offload to the GPU. Unset lets the provider fit it.
        threads: CPU threads for the parts that stay on the host.
        batch_size: Prompt-evaluation batch size.
        keep_alive: How long the provider should hold the model resident after a call, in its own
            duration syntax. A benchmark that reloads between every test measures loading.
    """

    model_config = ConfigDict(extra="forbid")

    context_size: int | None = Field(default=None, gt=0)
    gpu_layers: int | None = Field(default=None, ge=0)
    threads: int | None = Field(default=None, gt=0)
    batch_size: int | None = Field(default=None, gt=0)
    keep_alive: str | None = None

    def to_profile(self) -> RuntimeProfile:
        """Build the :class:`~baseaicore.RuntimeProfile` these settings describe."""
        return RuntimeProfile(
            context_size=self.context_size,
            gpu_layers=self.gpu_layers,
            threads=self.threads,
            batch_size=self.batch_size,
            keep_alive=self.keep_alive,
        )


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


class ExecutionSettings(BaseModel):
    """Default benchmark execution parameters (spec §12, ``[execution]``).

    These are the *application* layer of the second precedence chain spec §12 describes
    (application → suite → test → saved settings → run overrides). A run resolves them into its
    own ``effective_config`` and freezes that into the run record, so changing a default here
    never retroactively changes what an existing run says it did.

    Phase 5 reads ``warmup_repetitions``, ``measured_repetitions``, ``test_timeout_seconds``,
    ``run_timeout_seconds``, ``randomize_case_order`` and ``seed``. The idle-detection group and
    ``cooldown_seconds`` are declared here because they belong to this section in the spec and
    because a configuration key that appears one phase later is a configuration key users write
    into a file and have silently rejected in the meantime (``extra="forbid"``); they take effect
    at Phase 6, which is where the idle-detection outcome of spec §13 is implemented.
    """

    model_config = ConfigDict(extra="forbid")

    warmup_repetitions: int = Field(default=1, ge=0)
    measured_repetitions: int = Field(default=3, ge=1)
    cooldown_seconds: float = Field(default=5.0, ge=0)
    test_timeout_seconds: float = Field(default=600.0, gt=0)
    run_timeout_seconds: float = Field(default=86400.0, gt=0)
    randomize_case_order: bool = True
    seed: int = 0
    gpu_index: int = Field(default=0, ge=0)
    idle_gpu_threshold_percent: float = Field(default=10.0, ge=0, le=100)
    idle_required_samples: int = Field(default=3, ge=1)
    idle_wait_timeout_seconds: float = Field(default=120.0, ge=0)
    on_idle_timeout: Literal["warn", "refuse"] = "warn"
    store_responses: bool = False


class BenchmarkSettings(BaseModel):
    """The ``[benchmarks]`` section: limits a machine, not a suite author, decides.

    A shipped suite's content is fixed and hashed — that is what makes two runs of it comparable.
    What is *not* fixed is how far a sweep can go before the machine running it cannot serve the
    context any more, and that is a property of the hardware rather than of the benchmark.

    Attributes:
        long_context_max_tokens: The ceiling of ``native.long_context``'s depth sweep. The shipped
            ladder doubles — 2 000, 4 000, 8 000, 16 000, 32 000 — and is truncated to, or extended
            by doubling up to, this value. Raising it on a machine that can serve more turns
            ``effective_context_tokens`` from a floor into a measurement; lowering it on a small
            card keeps the suite runnable instead of failing every rung.

            **It separates results.** The effective ladder is hashed into the suite's
            ``dataset_hashes``, so a 32 000-token sweep and a 128 000-token sweep are two different
            measurements and are never averaged — a sweep that stopped earlier reports a smaller
            effective context for reasons that have nothing to do with the model.
    """

    model_config = ConfigDict(extra="forbid")

    long_context_max_tokens: int = Field(default=32_000, ge=1_000, le=2_000_000)


class GoalSettings(BaseModel):
    """Where user-authored goal packs live, and the bounds on what one may contain (spec §12).

    ``root`` is deliberately under the *config* directory rather than the data directory: a goal
    pack is hand-editable, git-trackable JSON the user owns
    ([ADR-0031 §6](../../docs/adr/0031-user-defined-goal-benchmarks.md)), not application state.

    ``max_pack_bytes`` and ``rule_timeout_ms`` are the two bounds spec §14 names on user-authored
    content: an import is size-capped before a byte is written, and a criterion's rule runs under
    a timeout so a catastrophic pattern fails the criterion rather than the process.
    """

    model_config = ConfigDict(extra="forbid")

    root: str | None = None
    max_pack_bytes: int = Field(default=5_242_880, gt=0)
    rule_timeout_ms: int = Field(default=250, gt=0)

    @model_validator(mode="after")
    def _apply_config_dir_default(self) -> GoalSettings:
        """Resolve ``root`` against the XDG config directory when it is not set."""
        if self.root is None:
            self.root = str(config_dir() / "goals")
        return self

    @property
    def root_path(self) -> Path:
        """``root`` as a path, expanded."""
        return Path(self.root or (config_dir() / "goals")).expanduser()


class JudgeSettings(BaseModel):
    """The default jury a goal's judged criteria are scored by (spec §12).

    Every field here is a *default*; a goal pack's own ``judge`` block overrides it, and the
    resolved configuration is a ``goal_hash`` input because a different jury is a different
    instrument (ADR-0032 §4).

    ``allow_remote`` is only half the remote opt-in: ``providers.allow_remote`` is the other half
    and both are required (ADR-0031 §4). Neither can be satisfied by accident.
    """

    model_config = ConfigDict(extra="forbid")

    jury_size: int = Field(default=3, ge=1)
    models: tuple[str, ...] = ()
    repetitions: int = Field(default=3, ge=1)
    randomize_order: bool = True
    blind_candidate_identity: bool = True
    refuse_self_judging: bool = True
    allow_remote: bool = False
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    _split_models = field_validator("models", mode="before")(_split_csv)


class CalibrationSettings(BaseModel):
    """How judged criteria are calibrated against the author's grades (spec §12).

    ``n_holdout_target`` is the shrinkage denominator of
    [ADR-0032 §2](../../docs/adr/0032-judge-validity-and-user-capability-namespace.md): six
    holdout samples at ``kappa_w`` 0.71 yield a validity factor of 0.55, not 0.71. It is
    configuration and it is recorded on every calibration report with the policy version, exactly
    as ADR-0017's own parameters are.
    """

    model_config = ConfigDict(extra="forbid")

    target_samples: int = Field(default=12, ge=1)
    min_samples: int = Field(default=8, ge=1)
    holdout_fraction: float = Field(default=0.4, gt=0.0, lt=1.0)
    partition_seed: int = 0
    min_agreement: float = Field(default=0.40, ge=-1.0, le=1.0)
    n_holdout_target: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def _check_sample_counts(self) -> CalibrationSettings:
        """Refuse a minimum above the target.

        Raises:
            ValueError: ``min_samples`` exceeds ``target_samples``. The target is what the wizard
                asks for and the minimum is what it will accept; a minimum above it would refuse
                every set the wizard collected.
        """
        if self.min_samples > self.target_samples:
            raise ValueError(
                f"calibration.min_samples ({self.min_samples}) is above "
                f"calibration.target_samples ({self.target_samples})."
            )
        return self


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
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    benchmarks: BenchmarkSettings = Field(default_factory=BenchmarkSettings)
    goals: GoalSettings = Field(default_factory=GoalSettings)
    judge: JudgeSettings = Field(default_factory=JudgeSettings)
    calibration: CalibrationSettings = Field(default_factory=CalibrationSettings)
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


def prompt_override_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/freeweight/prompts``, the user's prompt override directory.

    Prompt standards §6's one override location. It is a *derived* path rather than a
    configuration key on purpose: an override already invalidates comparison with results produced
    by the shipped prompt, and making its location configurable would add a second thing a reader
    of a result has to know before they can tell which prompt produced it.
    """
    return config_dir() / "prompts"


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
backup_retention = 5        # automatic pre-migration backups to keep
# There is no time-based retention. A measurement does not expire: a result taken six months ago
# is as true as one taken today, and the thing that invalidates it is a change of hardware or a
# model leaving the machine — neither of which a clock can detect. Delete results by model
# (`freeweight db delete --model …`) when you remove the model, and never on a timer.
# statement_timeout_ms applies to PostgreSQL only (also used as lock_timeout); unset = server
# default. SQLite's analogue is its busy_timeout, which the engine always sets.

[benchmarks]
# The ceiling of native.long_context's depth sweep. The shipped ladder doubles to 32 000; raise it
# on a machine that can serve more, lower it on one that cannot. The effective ladder is hashed
# into the suite's dataset_hashes, so two ceilings are two measurements and are never averaged.
long_context_max_tokens = 32000

[provider]
kind = "ollama"
base_url = "http://127.0.0.1:11434"
timeout_seconds = 300.0

[providers]
allow_remote = false

[runtime]
# How a model is loaded and served, as opposed to how a run is executed (ADR-0023). Every setting
# is optional; unset means "provider defaults", which is itself a legal, hashable profile.
# A differing runtime profile is a *hard separation* (ADR-0017): results measured at one context do
# not describe another, and FreeWeight will not merge them.
# context_size = 8192   # Ollama's num_ctx. Unset = the provider decides and the run records its
                        # served context as "assumed" rather than "configured". Set it to compare
                        # one model against itself at two contexts.
# gpu_layers = 32
# threads = 8
# batch_size = 512
# keep_alive = "5m"

[telemetry]
interval_ms = 1000
persist_during_runs = true
calibrate_overhead = true

[goals]
# root defaults to <config>/goals: hand-editable, git-trackable goal packs (ADR-0031 §6).
max_pack_bytes = 5242880    # import size cap, enforced before a byte is written
rule_timeout_ms = 250       # per criterion, per sample

[judge]
jury_size = 3               # distinct local models; 1 disables the jury and says so in the result
models = []                 # empty = auto-select from installed models
repetitions = 3
randomize_order = true
blind_candidate_identity = true
refuse_self_judging = true  # a juror never judges its own output
allow_remote = false        # requires providers.allow_remote as well
temperature = 0.0

[calibration]
target_samples = 12
min_samples = 8             # below this: CALIBRATION_INSUFFICIENT, not a failed gate
holdout_fraction = 0.4
partition_seed = 0
min_agreement = 0.40        # weighted kappa_w below this emits no evidence at all
n_holdout_target = 10       # shrinkage denominator for judge_validity_factor (ADR-0032 §2)

[auth]
tokens = []                 # required for a non-loopback bind

[logging]
level = "INFO"
format = "auto"              # text | json | auto (text on a TTY, json otherwise)
include_content = false
"""
