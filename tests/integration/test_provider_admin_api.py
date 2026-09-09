"""The Provider page end to end: the file is edited, and the running server re-opens the provider.

ADR-0117. No network and no GPU — the provider is ``fake``, and what is under test is what happens
to the configuration file and to ``app.state``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from freeweight.bootstrap import bootstrap

_FILE = """\
# the deployment note nobody wants to lose
[provider]
kind = "fake"
timeout_seconds = 30.0
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_FILE, encoding="utf-8")
    monkeypatch.setenv("FREEWEIGHT_CONFIG", str(config_path))
    monkeypatch.setenv(
        "FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path}/freeweight.sqlite3"
    )
    application = bootstrap()
    with TestClient(application.app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def _config_path(client: TestClient) -> Path:
    return Path(client.app.state.config_path)  # type: ignore[attr-defined]  # FastAPI


def test_the_configured_provider_is_returned(client: TestClient) -> None:
    document = client.get("/api/v1/provider").json()
    assert document["provider"]["kind"] == "fake"
    assert document["provider"]["timeout_seconds"] == 30.0


def test_a_write_reaches_the_file_and_the_running_provider(client: TestClient) -> None:
    response = client.put("/api/v1/provider", json={"timeout_seconds": 45.0})
    assert response.status_code == 200
    assert response.json()["provider"]["timeout_seconds"] == 45.0
    text = _config_path(client).read_text(encoding="utf-8")
    assert "# the deployment note nobody wants to lose" in text
    assert "timeout_seconds = 45.0" in text
    assert client.app.state.settings.provider.timeout_seconds == 45.0  # type: ignore[attr-defined]


def test_a_value_the_model_rejects_never_lands(client: TestClient) -> None:
    response = client.put("/api/v1/provider", json={"timeout_seconds": -1.0})
    assert response.status_code == 400
    assert "timeout_seconds = 30.0" in _config_path(client).read_text(encoding="utf-8")


def test_the_page_renders_the_block(client: TestClient) -> None:
    page = client.get("/provider")
    assert page.status_code == 200
    assert "[provider]" in page.text


def test_the_replaced_provider_handle_is_closed(client: TestClient) -> None:
    """A supervising provider owns a process; dropping the handle would orphan a llama-server."""
    closed: list[str] = []
    client.app.state.provider.close = lambda: closed.append("provider")  # type: ignore[attr-defined]

    client.put("/api/v1/provider", json={"timeout_seconds": 42.0})

    assert closed == ["provider"]
