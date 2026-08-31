"""End-to-end: the FastAPI app boots and answers its Phase 1 surface."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from freeweight.config import load_settings
from freeweight.services.health import get_health_report
from freeweight.web.app import create_app


@contextmanager
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client whose application lifespan has started, as a served application's would have.

    Entering ``TestClient`` as a context manager is what runs the lifespan, and therefore what
    creates the ``Database`` handle the routes read through and the health check reports on.
    Without it these tests would exercise an app with no handle — a state the running server is
    never in.

    ``provider.kind`` is pinned to ``fake`` (testing standards §1: e2e runs "through HTTP and CLI"
    against the fake) so the health endpoint's new ``provider`` component (Phase 3) is deterministic
    rather than depending on whether something answers on this machine's real Ollama port.
    """
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    app = create_app(loaded.settings)
    # httpx's TestClient defaults to a "testserver" Host header, which the Host-validation
    # middleware would reject; binding the client to the configured loopback address keeps the
    # Host header consistent with what the app was built to accept.
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The lifespan-started client, for the tests that need nothing else from ``tmp_path``."""
    with _client(tmp_path, monkeypatch) as test_client:
        yield test_client


def test_health_endpoint_matches_documented_shape(client: TestClient) -> None:
    """``create_app`` is a pure function of ``Settings`` (its own docstring): it never opens or
    migrates a database, so a database built this way — bypassing ``bootstrap()``'s startup
    migration — is honestly reported as pending, not silently treated as healthy."""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert "version" in body
    assert "checked_at" in body
    assert body["components"][0]["name"] == "database"
    # NOTE: this file was reconstructed from compiled bytecode after the original was lost to an
    # accidental `git checkout`. Everything above is recovered verbatim from the bytecode's
    # constants; the original also had one further `==` assertion here, over `body["components"]`,
    # whose exact form the bytecode does not determine. Restore it if you remember it.
    assert "X-Request-ID" in response.headers


def test_version_endpoint_shape(client: TestClient) -> None:
    response = client.get("/api/v1/version")

    assert response.status_code == 200
    body = response.json()
    assert body["application"]["name"] == "freeweight"
    assert body["api"]["current"] == "v1"
    assert body["api"]["supported"] == ["v1"]
    assert body["api"]["deprecated"] == []


def test_health_endpoint_and_service_report_identical_data(
    client: TestClient, tmp_path: Path
) -> None:
    body = client.get("/api/v1/health").json()

    # The endpoint passes the settings the server is serving from (app.state.settings) so the
    # settings-dependent components (sandbox tier, external adapters, goals, judges) report on the
    # running configuration; the service call has to be given the same settings to match, which is
    # exactly the "identical by construction" property this test exists to hold.
    report = get_health_report(
        settings=load_settings(config_path=tmp_path / "missing.toml").settings
    ).model_dump(mode="json")

    assert body["status"] == report["status"]
    component_names = {component["name"] for component in body["components"]}
    assert component_names == {component["name"] for component in report["components"]}
    assert "sandbox" in component_names
    assert "external_benchmarks" in component_names


def test_shell_page_renders(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "FreeWeight" in response.text


def test_unknown_api_route_returns_structured_404_with_no_path_leak(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "request_id" in body["error"]
    assert "timestamp" in body["error"]
    assert "does-not-exist" not in body["error"]["message"]


def test_request_id_is_generated_and_echoed(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    request_id = response.headers["X-Request-ID"]

    assert len(request_id) > 0
    assert response.json()["status"] in ("ok", "degraded")


def test_supplied_request_id_is_honoured(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "01J9K2M4P7Q8R9S0T1U2V3W4X5"})

    assert response.headers["X-Request-ID"] == "01J9K2M4P7Q8R9S0T1U2V3W4X5"


def test_invalid_supplied_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "not valid!! spaces"})

    assert response.headers["X-Request-ID"] != "not valid!! spaces"


def test_mismatched_host_header_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Host": "evil.example.com"})

    assert response.status_code == 421
    assert response.json()["error"]["code"] == "MISDIRECTED_REQUEST"


def test_oversized_body_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/health",
        headers={"Content-Length": str(5 * 1024 * 1024)},
        content=b"x" * 100,
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_response_headers_include_no_store_and_api_version(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Api-Version"] == "v1"


def test_lifespan_events_pass_through_every_middleware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    app = create_app(loaded.settings)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200


def test_content_length_that_is_not_an_integer_is_ignored(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Content-Length": "not-a-number"})

    assert response.status_code == 200
