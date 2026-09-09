"""tests/unit/test_config_schema.py — the ADR-0127 settings-schema document.

FreeWeight publishes its configuration surface as one JSON document so WeightRoomGym can render a
settings form without hardcoding a fifth copy of FreeWeight's keys. The golden test is the
contract: a change to ``Settings``, ``RUNTIME_SETTINGS`` or ``CONFIG_ONLY_KEYS`` that isn't also
reflected here is exactly the drift this document exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from freeweight.services.settings import (
    CONFIG_ONLY_KEYS,
    RUNTIME_SETTINGS,
    SCHEMA_VERSION,
    config_schema_document,
)

GOLDEN = Path(__file__).parent / "golden" / "config_schema.json"


def _without_environment_specific_fields(document: dict[str, object]) -> dict[str, object]:
    """Strip the two fields that vary with the machine and the release: ``version``, which
    changes on every release, and ``config_path``, which is wherever the test's isolated XDG tree
    happened to land."""
    trimmed = dict(document)
    trimmed.pop("version", None)
    trimmed.pop("config_path", None)
    return trimmed


def test_schema_document_matches_the_golden() -> None:
    """A fresh install, no file, no environment: the document the golden was recorded from."""
    document = config_schema_document()

    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert _without_environment_specific_fields(document) == expected


def test_every_runtime_changeable_and_security_key_exists_in_json_schema() -> None:
    document = config_schema_document()
    json_schema = document["json_schema"]
    section_defs = json_schema["$defs"]
    section_schemas = {
        name: section_defs[ref["$ref"].rsplit("/", 1)[-1]]
        for name, ref in json_schema["properties"].items()
    }

    def _exists(key: str) -> bool:
        section, _, field_name = key.partition(".")
        section_schema = section_schemas.get(section)
        if section_schema is None:
            return False
        return field_name in section_schema.get("properties", {})

    for setting in RUNTIME_SETTINGS:
        assert _exists(setting.key), f"{setting.key} is runtime-changeable but not in json_schema"
    for key in CONFIG_ONLY_KEYS:
        assert _exists(key), f"{key} is a security key but not in json_schema"


def test_config_only_excludes_runtime_and_security_keys() -> None:
    document = config_schema_document()
    runtime_keys = {setting.key for setting in RUNTIME_SETTINGS}

    assert not (set(document["config_only"]) & runtime_keys)
    assert not (set(document["config_only"]) & CONFIG_ONLY_KEYS)
    assert set(document["security_keys"]) == CONFIG_ONLY_KEYS


def test_schema_version_is_one_dot_zero() -> None:
    assert config_schema_document()["schema_version"] == SCHEMA_VERSION == "1.0"


def test_document_never_carries_a_configured_secret_value(tmp_path: Path) -> None:
    """The document describes the shape of the configuration, never a configured value."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[auth]\ntokens = ["super-secret-value"]\n', encoding="utf-8")

    document = config_schema_document(config_file)

    assert "super-secret-value" not in json.dumps(document)


def test_an_unknown_key_in_the_file_is_reported_under_problems(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhostt = "127.0.0.1"\n', encoding="utf-8")

    document = config_schema_document(config_file)

    assert document["problems"] == ["unknown configuration key 'server.hostt'"]
    # the rest of the document still builds, over the valid remainder of the file
    assert document["runtime_changeable"]
    assert document["security_keys"]


def test_a_genuine_validation_error_still_raises(tmp_path: Path) -> None:
    from freeweight.config import ConfigurationError

    config_file = tmp_path / "config.toml"
    config_file.write_text("[server]\nport = 999999\n", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        config_schema_document(config_file)


def test_a_database_sourced_runtime_value_is_reported_in_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weightsdb import MigrationRunner

    from freeweight.config import load_settings
    from freeweight.services.database import MIGRATIONS_LOCATION, Database
    from freeweight.services.settings import update_settings

    database_url = f"sqlite:///{tmp_path / 'freeweight.sqlite3'}"
    with Database.from_url(database_url) as database:
        MigrationRunner(database.engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        update_settings(database, load_settings().settings, {"telemetry.interval_ms": 500})
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", database_url)

    document = config_schema_document()

    assert document["sources"]["telemetry.interval_ms"] == "database"
