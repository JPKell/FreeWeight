"""Unit tests for freeweight.config: precedence, validation and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from freeweight.config import (
    LOOPBACK_HOSTS,
    ConfigurationError,
    InsecureBindingError,
    StorageSettings,
    leaf_keys,
    load_settings,
    load_settings_tolerant,
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


def test_evidence_defaults_are_adr_0017s_table() -> None:
    """Configuration standards §9: every default asserted, so a default cannot drift unnoticed."""
    evidence = load_settings().settings.evidence
    assert evidence.n_target == 30
    assert evidence.quality_half_life_days == 90.0
    assert evidence.performance_half_life_days == 30.0
    assert evidence.freshness_floor == 0.3
    assert evidence.stale_below == 0.5
    assert evidence.name_only_identity_factor == 0.6
    assert evidence.performance_drift_factor == 0.7
    assert evidence.quality_drift_factor == 0.5
    assert evidence.goal_contribution_weight == 1.0
    assert evidence.capability_weights_path is None
    assert evidence.weights_path is None


def test_evidence_parameters_are_range_checked(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[evidence]\nstale_below = 1.5\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="evidence.stale_below"):
        load_settings(config_path=config_file)


def test_backup_retention_defaults_to_five() -> None:
    """Database standards §7: keep the last 5 automatic backups."""
    assert StorageSettings().backup_retention == 5


def test_ollama_with_a_llamacpp_only_runtime_key_is_refused_at_load(tmp_path: Path) -> None:
    """ADR-0120 rule 4, at the file: the server never starts with a profile it cannot serve."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[provider]\nkind = "ollama"\n[runtime]\nflash_attention = true\n')
    with pytest.raises(ConfigurationError, match="runtime.flash_attention"):
        load_settings(config_path=config_file)


def test_llamacpp_honours_the_same_runtime_keys(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[provider]\nkind = "llamacpp"\nmodel_directory = "~/m"\n'
        '[runtime]\nflash_attention = true\nkv_cache_precision = "q4_0"\n'
        "[benchmarks]\nmax_fit_context_tokens = 32768\n"
    )
    loaded = load_settings(config_path=config_file)
    assert loaded.settings.runtime.kv_cache_precision == "q4_0"
    assert loaded.settings.benchmarks.max_fit_context_tokens == 32768  # noqa: PLR2004


def test_a_memory_throttle_without_a_cap_is_refused_by_key(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[provider]\nkind = "llamacpp"\nmemory_high_bytes = 1024\n')
    with pytest.raises(ConfigurationError, match="memory_high_bytes"):
        load_settings(config_path=config_file)


def test_leaf_keys_includes_every_section_field() -> None:
    keys = leaf_keys()
    assert "server.host" in keys
    assert "telemetry.interval_ms" in keys
    assert "server" not in keys  # bare section names are not leaves


def test_load_settings_tolerant_reports_an_unknown_key_instead_of_raising(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhostt = "127.0.0.1"\nport = 8790\n', encoding="utf-8")

    loaded, problems = load_settings_tolerant(config_file)

    assert problems == ("unknown configuration key 'server.hostt'",)
    assert loaded.settings.server.port == 8790
    assert loaded.settings.server.host == "127.0.0.1"  # the unknown key, not a sibling, is dropped


def test_load_settings_tolerant_reports_an_unknown_section(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[not_a_real_section]\nx = 1\n", encoding="utf-8")

    loaded, problems = load_settings_tolerant(config_file)

    assert problems == ("unknown configuration key 'not_a_real_section'",)
    assert loaded.settings.server.host == "127.0.0.1"


def test_load_settings_tolerant_still_raises_on_a_genuine_validation_error(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[server]\nport = 999999\n", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_settings_tolerant(config_file)


def test_load_settings_tolerant_matches_load_settings_on_a_clean_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[server]\nport = 8790\n", encoding="utf-8")

    loaded, problems = load_settings_tolerant(config_file)
    strict = load_settings(config_path=config_file)

    assert problems == ()
    assert loaded.settings == strict.settings
    assert loaded.sources == strict.sources


# --- ADR-0144: provider profiles -------------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text(text, encoding="utf-8")
    return config_file


def test_the_reference_machines_bare_provider_block_is_the_default_profile(
    tmp_path: Path,
) -> None:
    """The operator's own file — a bare ``[provider]`` — must not change meaning (ADR-0144 rule 2).

    This is the shape on the reference machine, verbatim: no ``active``, no ``[providers.<name>]``
    table, `kind = "llamacpp"` with a context size. It has to resolve to exactly what it resolved
    to before profiles existed.
    """
    config_file = _write(
        tmp_path,
        '[provider]\nkind = "llamacpp"\nmodel_directory = "~/ai/models/llm"\n'
        "\n[runtime]\ncontext_size = 8192\nfit_to_device = false\n",
    )

    loaded = load_settings(config_path=config_file)

    assert loaded.settings.provider.kind == "llamacpp"
    assert loaded.settings.provider.model_directory == "~/ai/models/llm"
    assert loaded.settings.active_profile_name == "default"
    assert sorted(loaded.settings.providers.profiles) == ["default"]
    assert loaded.settings.providers.profiles["default"].kind == "llamacpp"
    assert loaded.sources["provider.kind"] == "file"


def test_a_file_with_no_provider_block_still_has_a_default_profile() -> None:
    loaded = load_settings()

    assert loaded.settings.active_profile_name == "default"
    assert loaded.settings.providers.profiles["default"] == loaded.settings.provider


def test_adding_a_profile_changes_nothing_until_active_names_it(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path,
        '[provider]\nkind = "ollama"\n\n[providers.served]\nkind = "llamacpp"\n'
        'model_directory = "/models"\n',
    )

    loaded = load_settings(config_path=config_file)

    assert loaded.settings.provider.kind == "ollama"
    assert loaded.settings.active_profile_name == "default"
    assert sorted(loaded.settings.providers.profiles) == ["default", "served"]


def test_active_selects_the_profile_and_sources_name_its_table(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path,
        '[provider]\nactive = "served"\nkind = "ollama"\n\n[providers.served]\n'
        'kind = "llamacpp"\nmodel_directory = "/models"\n',
    )

    loaded = load_settings(config_path=config_file)

    assert loaded.settings.provider.kind == "llamacpp"
    assert loaded.settings.provider.model_directory == "/models"
    assert loaded.settings.active_profile_name == "served"
    # The `[provider]` block is still the `default` profile, and still says ollama.
    assert loaded.settings.providers.profiles["default"].kind == "ollama"
    assert loaded.sources["provider.kind"] == "file [providers.served]"
    assert loaded.sources["provider.base_url"] == "default"
    assert loaded.sources["providers.served.model_directory"] == "file"


def test_active_naming_no_profile_is_refused_with_the_names_that_exist(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path, '[provider]\nactive = "typo"\n\n[providers.served]\nkind = "ollama"\n'
    )

    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config_file)

    assert "names no provider profile" in str(caught.value)
    assert "default, served" in str(caught.value)


def test_a_default_profile_table_beside_a_provider_block_is_refused(tmp_path: Path) -> None:
    config_file = _write(
        tmp_path, '[provider]\nkind = "ollama"\n\n[providers.default]\nkind = "llamacpp"\n'
    )

    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config_file)

    assert "two definitions of the profile 'default'" in str(caught.value)


def test_an_empty_provider_block_beside_a_default_profile_table_is_allowed(
    tmp_path: Path,
) -> None:
    """Only a `[provider]` block that *sets* something collides (ADR-0144 rule 4)."""
    config_file = _write(
        tmp_path, '[providers.default]\nkind = "llamacpp"\nmodel_directory = "/models"\n'
    )

    loaded = load_settings(config_path=config_file)

    assert loaded.settings.provider.kind == "llamacpp"


def test_a_scalar_under_providers_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    config_file = _write(tmp_path, "[providers]\nallow_remot = true\n")

    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config_file)

    assert "allow_remot" in str(caught.value)


def test_a_profile_may_not_choose_itself(tmp_path: Path) -> None:
    config_file = _write(tmp_path, '[providers.served]\nkind = "ollama"\nactive = "served"\n')

    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config_file)

    assert "belongs on the [provider] block alone" in str(caught.value)


def test_an_environment_provider_key_under_a_named_profile_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write(
        tmp_path, '[provider]\nactive = "served"\n\n[providers.served]\nkind = "ollama"\n'
    )
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "llamacpp")

    with pytest.raises(ConfigurationError) as caught:
        load_settings(config_path=config_file)

    assert "FREEWEIGHT_PROVIDER__KIND" in str(caught.value)
    assert "would be inert" in str(caught.value)


def test_an_environment_provider_key_is_untouched_on_the_default_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _write(tmp_path, '[provider]\nkind = "ollama"\n')
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")

    loaded = load_settings(config_path=config_file)

    assert loaded.settings.provider.kind == "fake"
    assert loaded.sources["provider.kind"] == "env FREEWEIGHT_PROVIDER__KIND"


def test_a_runtime_key_is_judged_against_the_active_profiles_kind(tmp_path: Path) -> None:
    """ADR-0120 rule 4 is evaluated on the profile that runs, not on `[provider]`'s own kind."""
    config_file = _write(
        tmp_path,
        '[provider]\nactive = "served"\nkind = "ollama"\n\n[providers.served]\n'
        'kind = "llamacpp"\nmodel_directory = "/models"\n\n[runtime]\nflash_attention = true\n',
    )

    loaded = load_settings(config_path=config_file)

    assert loaded.settings.runtime.flash_attention is True

    (tmp_path / "other").mkdir()
    ollama_active = _write(
        tmp_path / "other",
        '[provider]\nkind = "ollama"\n\n[runtime]\nflash_attention = true\n',
    )
    with pytest.raises(ConfigurationError):
        load_settings(config_path=ollama_active)


def test_load_settings_tolerant_keeps_the_profile_tables(tmp_path: Path) -> None:
    """`[providers.<name>]` is not an unknown key: the section validates its own extras."""
    config_file = _write(
        tmp_path, '[providers.served]\nkind = "llamacpp"\nmodel_directory = "/models"\n'
    )

    loaded, problems = load_settings_tolerant(config_file)

    assert problems == ()
    assert sorted(loaded.settings.providers.profiles) == ["default", "served"]


def test_leaf_keys_omits_the_derived_profiles_mapping() -> None:
    assert "providers.profiles" not in leaf_keys()
    assert "providers.allow_remote" in leaf_keys()
    assert "provider.active" in leaf_keys()
