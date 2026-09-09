"""Editing the ``[provider]`` block in place (ADR-0117).

What matters is not that the value round-trips — it is that everything the operator wrote and this
edit did not touch comes back byte for byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from baseaicore import SuiteError, ValidationError

from freeweight.config import load_settings
from freeweight.services.providers import (
    ProviderConfigChanged,
    config_digest,
    describe_provider,
    save_provider,
)

_FILE = """\
# the deployment note nobody wants to lose
[server]
port = 8767  # deliberately not the default

# the box under the desk
[provider]
kind = "ollama"
base_url = "http://127.0.0.1:11434"
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_FILE, encoding="utf-8")
    return path


def test_a_write_keeps_every_comment_and_every_untouched_key(config_path: Path) -> None:
    save_provider(config_path, {"base_url": "http://10.0.0.5:11434"})
    text = config_path.read_text(encoding="utf-8")
    assert "# the deployment note nobody wants to lose" in text
    assert "port = 8767  # deliberately not the default" in text
    assert "# the box under the desk" in text
    settings = load_settings(config_path=config_path).settings
    assert settings.provider.base_url == "http://10.0.0.5:11434"
    assert settings.server.port == 8767


def test_the_previous_file_is_kept_beside_the_new_one(config_path: Path) -> None:
    save_provider(config_path, {"kind": "fake"})
    assert config_path.with_name("config.toml.bak").read_text(encoding="utf-8") == _FILE


def test_a_document_the_application_would_refuse_never_lands(config_path: Path) -> None:
    with pytest.raises(SuiteError) as caught:
        save_provider(config_path, {"timeout_seconds": -1.0})
    assert "timeout_seconds" in str(caught.value)
    assert config_path.read_text(encoding="utf-8") == _FILE


def test_a_key_outside_the_provider_block_is_refused_by_name(config_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        save_provider(config_path, {"database_url": "sqlite:///elsewhere.sqlite3"})
    assert "database_url" in str(caught.value)
    assert config_path.read_text(encoding="utf-8") == _FILE


def test_a_write_against_a_stale_digest_is_refused(config_path: Path) -> None:
    stale = config_digest(config_path)
    config_path.write_text(_FILE + "\n# someone else was editing\n", encoding="utf-8")
    with pytest.raises(ProviderConfigChanged):
        save_provider(config_path, {"kind": "fake"}, base_digest=stale)
    assert "someone else was editing" in config_path.read_text(encoding="utf-8")


def test_an_environment_variable_is_reported_as_shadowing(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration standards §7: the environment beats the file, so the page says so."""
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__BASE_URL", "http://elsewhere:11434")
    settings = load_settings(config_path=config_path).settings
    assert describe_provider(settings).shadowed_by == "FREEWEIGHT_PROVIDER__BASE_URL"
