"""Unit tests for freeweight.config: precedence, validation and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from freeweight.config import (
    LOOPBACK_HOSTS,
    ConfigurationError,
    InsecureBindingError,
    StorageSettings,
    load_settings,
    resolve_config_path,
)


def test_defaults_with_no_file_or_env() -> None:
    loaded = load_settings()
    settings = loaded.settings

    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8765
    assert settings.server.allow_lan_exposure is False
    assert settings.server.allowed_hosts == ()
    assert settings.storage.database_url is not None
    assert settings.storage.database_url.startswith("sqlite:///")
    assert settings.storage.database_url.endswith("freeweight.sqlite3")
    assert settings.storage.auto_migrate is True
    assert settings.provider.kind == "ollama"
    assert settings.provider.base_url == "http://127.0.0.1:11434"
    assert settings.providers.allow_remote is False
    assert settings.telemetry.interval_ms == 1000
    assert settings.telemetry.persist_during_runs is True
    assert settings.logging.level == "INFO"
    assert settings.logging.include_content is False
    assert settings.auth.tokens == ()
    assert loaded.sources["server.host"] == "default"
    assert loaded.config_file_used is False


def test_file_overrides_default_without_discarding_siblings(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[server]\nport = 8790\n", encoding="utf-8")

    loaded = load_settings(config_path=config_file)

    assert loaded.settings.server.port == 8790
    assert loaded.settings.server.host == "127.0.0.1"
    assert loaded.sources["server.port"] == "file"
    assert loaded.sources["server.host"] == "default"
    assert loaded.config_file_used is True


def test_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[server]\nport = 8790\n", encoding="utf-8")
    monkeypatch.setenv("FREEWEIGHT_SERVER__PORT", "9001")

    loaded = load_settings(config_path=config_file)

    assert loaded.settings.server.port == 9001
    assert loaded.sources["server.port"] == "env FREEWEIGHT_SERVER__PORT"


def test_cli_overrides_env_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[server]\nport = 8790\n", encoding="utf-8")
    monkeypatch.setenv("FREEWEIGHT_SERVER__PORT", "9001")

    loaded = load_settings(config_path=config_file, cli_overrides={"server": {"port": 9500}})

    assert loaded.settings.server.port == 9500
    assert loaded.sources["server.port"] == "cli"


def test_unknown_key_in_file_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhostt = "127.0.0.1"\n', encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path=config_file)


def test_unknown_env_var_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREEWEIGHT_NOT_A_REAL_SECTION__FIELD", "value")

    with pytest.raises(ConfigurationError):
        load_settings()


def test_type_violation_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[server]\nport = 999999\n", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path=config_file)


def test_malformed_toml_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("not valid toml [[[", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings(config_path=config_file)


def test_non_loopback_host_without_tokens_refuses(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[server]\nhost = "192.168.1.5"\nallowed_hosts = ["192.168.1.5"]\n', encoding="utf-8"
    )

    with pytest.raises(InsecureBindingError):
        load_settings(config_path=config_file)


def test_non_loopback_host_without_allowed_hosts_refuses(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[server]\nhost = "192.168.1.5"\n\n[auth]\ntokens = ["abc"]\n', encoding="utf-8"
    )

    with pytest.raises(InsecureBindingError):
        load_settings(config_path=config_file)


def test_bind_all_interfaces_without_lan_exposure_ack_refuses(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[server]\nhost = "0.0.0.0"\nallowed_hosts = ["myhost"]\n\n[auth]\ntokens = ["abc"]\n',
        encoding="utf-8",
    )

    with pytest.raises(InsecureBindingError):
        load_settings(config_path=config_file)


def test_bind_all_interfaces_without_tokens_refuses(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[server]\nhost = "0.0.0.0"\nallow_lan_exposure = true\nallowed_hosts = ["myhost"]\n',
        encoding="utf-8",
    )

    with pytest.raises(InsecureBindingError):
        load_settings(config_path=config_file)


def test_bind_all_interfaces_with_full_configuration_succeeds(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[server]\nhost = "0.0.0.0"\nallow_lan_exposure = true\nallowed_hosts = ["myhost"]\n'
        '\n[auth]\ntokens = ["abc"]\n',
        encoding="utf-8",
    )

    loaded = load_settings(config_path=config_file)

    assert loaded.settings.server.host == "0.0.0.0"  # noqa: S104 — asserting the configured value


def test_missing_config_file_is_not_an_error(tmp_path: Path) -> None:
    loaded = load_settings(config_path=tmp_path / "does-not-exist.toml")

    assert loaded.config_file_used is False
    assert loaded.settings.server.host == "127.0.0.1"


def test_resolve_config_path_prefers_explicit_over_default(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.toml"

    assert resolve_config_path(str(explicit)) == explicit


def test_resolve_config_path_uses_freeweight_config_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / "from-env.toml"
    monkeypatch.setenv("FREEWEIGHT_CONFIG", str(env_path))

    assert resolve_config_path() == env_path


def test_loopback_hosts_constant_contains_documented_values() -> None:
    assert LOOPBACK_HOSTS == frozenset({"127.0.0.1", "localhost", "::1"})


def test_comma_separated_env_value_becomes_a_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREEWEIGHT_SERVER__ALLOWED_HOSTS", "a.example, b.example")
    monkeypatch.setenv("FREEWEIGHT_SERVER__HOST", "a.example")
    monkeypatch.setenv("FREEWEIGHT_AUTH__TOKENS", "tok1")

    loaded = load_settings()

    assert loaded.settings.server.allowed_hosts == ("a.example", "b.example")


def test_auto_migrate_defaults_true_on_sqlite() -> None:
    """Database standards §5.1: SQLite backs up, migrates and starts."""
    assert StorageSettings().auto_migrate is True


def test_auto_migrate_defaults_false_on_postgresql() -> None:
    """Database standards §7: PostgreSQL has no automatic rollback, "which is why its
    ``auto_migrate`` defaults to off".

    A single ``True`` for both dialects would silently migrate a shared PostgreSQL database on the
    first startup of a new deployment, with nothing to restore if that migration failed.
    """
    settings = StorageSettings(database_url="postgresql+psycopg://u:p@h:5432/d")

    assert settings.auto_migrate is False


def test_an_explicit_auto_migrate_is_honoured_on_postgresql() -> None:
    """Only the *default* is dialect-dependent; an operator who asks gets what they asked for."""
    settings = StorageSettings(database_url="postgresql+psycopg://u:p@h:5432/d", auto_migrate=True)

    assert settings.auto_migrate is True


def test_an_explicit_auto_migrate_false_is_honoured_on_sqlite() -> None:
    settings = StorageSettings(database_url="sqlite:///tmp.sqlite3", auto_migrate=False)

    assert settings.auto_migrate is False


def test_backup_retention_defaults_to_five() -> None:
    """Database standards §7: keep the last 5 automatic backups."""
    assert StorageSettings().backup_retention == 5
