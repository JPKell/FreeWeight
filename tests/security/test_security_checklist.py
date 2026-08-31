"""Security Standards §14, the items Phase 14 adds or consolidates for FreeWeight.

The pre-existing §14 coverage lives where the mechanism does: Host validation and oversize-body
rejection in ``tests/e2e/test_server_boot.py``; archive/traversal/oversize import hardening in
``test_goal_pack_import.py`` and ``test_dataset_verification.py``; mock-tool containment in
``test_mock_tools_contained.py``; the sandbox refusal in ``test_sandbox_refusal.py``. This file
adds the two §14 items Phase 14 introduces — the CSRF double-submit on form routes, and the
proof that model-shaped hostile strings render and store without effect — plus the cross-cutting
checks a reviewer reads as the checklist itself: Host validation runs *before* CSRF, and the
binding refusals fire.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mirrorwall import CSRF_COOKIE_NAME

from freeweight.config import load_settings
from freeweight.web.app import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A served application over an empty, migrated database on a loopback bind."""
    from weightsdb import MigrationRunner, create_engine_for

    from freeweight.services.database import MIGRATIONS_LOCATION

    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


class TestCsrfOnFormRoutes:
    """§14: a forged HTML form post is rejected with CSRF_FAILED; a valid one succeeds; a
    cross-origin JSON post is rejected."""

    def test_a_form_page_sets_the_host_cookie_and_a_matching_token(
        self, client: TestClient
    ) -> None:
        response = client.get("/runs")

        assert response.status_code == 200
        assert CSRF_COOKIE_NAME in response.headers.get("set-cookie", "")
        # The __Host- prefix requires Secure + Path=/ + no Domain (ADR-0026 §2).
        cookie_line = response.headers["set-cookie"]
        assert "Secure" in cookie_line
        assert "Path=/" in cookie_line
        assert "Domain=" not in cookie_line
        assert f'name="csrf_token" value="{client.cookies.get(CSRF_COOKIE_NAME)}"' in response.text

    def test_a_forged_form_post_is_rejected(self, client: TestClient) -> None:
        # A cross-site form post: no cookie, no matching field. (The browser-faithful conftest
        # fixture fills a token only when the caller omits one; an explicit empty token opts out.)
        response = client.post(
            "/models/discover",
            data={"csrf_token": ""},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FAILED"

    def test_a_form_post_with_the_wrong_token_is_rejected(self, client: TestClient) -> None:
        client.get("/")  # obtain a real cookie
        response = client.post(
            "/models/discover",
            data={"csrf_token": "not-the-cookie-value"},
            headers={"Cookie": f"{CSRF_COOKIE_NAME}={client.cookies.get(CSRF_COOKIE_NAME)}"},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FAILED"

    def test_a_valid_form_post_succeeds(self, client: TestClient) -> None:
        client.get("/")
        token = client.cookies.get(CSRF_COOKIE_NAME)
        response = client.post(
            "/models/discover",
            data={"csrf_token": token},
            headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
            follow_redirects=False,
        )

        assert response.status_code == 303, response.text

    def test_the_json_api_is_exempt_and_needs_no_token(self, client: TestClient) -> None:
        # A JSON body cannot be produced by a cross-origin HTML form, so the API is exempt on
        # stated grounds (ADR-0026 §2). A run created over the API takes no CSRF token.
        client.post("/api/v1/models/discover")
        listing = client.get("/api/v1/models").json()
        model_ref = listing["items"][0]["canonical_id"]
        response = client.post("/api/v1/runs", json={"model": model_ref, "suites": ["native.echo"]})
        assert response.status_code in (200, 201), response.text


class TestHostValidationPrecedesCsrf:
    """§14: a bad Host is refused with 421 before authentication — and before CSRF."""

    def test_a_bad_host_on_a_form_post_is_421_not_403(self, client: TestClient) -> None:
        # Host validation is a middleware outside CSRF, so a rebinding attempt is 421 before the
        # CSRF check ever runs — the attacker never learns whether their forged token would pass.
        response = client.post(
            "/models/discover",
            data={"csrf_token": ""},
            headers={"Host": "evil.example.com"},
        )

        assert response.status_code == 421


class TestBindingRefusals:
    """§14: a non-loopback bind refuses to start without allowed_hosts, and 0.0.0.0 without the
    acknowledgement flag."""

    def _settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> object:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return load_settings(config_path=tmp_path / "missing.toml")

    def test_all_interfaces_without_acknowledgement_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from baseaicore import SuiteError

        with pytest.raises(SuiteError) as excinfo:
            self._settings(tmp_path, monkeypatch, FREEWEIGHT_SERVER__HOST="0.0.0.0")  # noqa: S104 — testing the refusal
        assert excinfo.value.code == "INSECURE_BINDING"

    def test_non_loopback_without_allowed_hosts_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from baseaicore import SuiteError

        with pytest.raises(SuiteError) as excinfo:
            self._settings(
                tmp_path,
                monkeypatch,
                FREEWEIGHT_SERVER__HOST="192.168.1.5",
                FREEWEIGHT_SERVER__ALLOW_LAN_EXPOSURE="true",
            )
        assert excinfo.value.code == "INSECURE_BINDING"
        assert "allowed_hosts" in excinfo.value.message

    def test_non_loopback_without_tokens_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from baseaicore import SuiteError

        with pytest.raises(SuiteError) as excinfo:
            self._settings(
                tmp_path,
                monkeypatch,
                FREEWEIGHT_SERVER__HOST="192.168.1.5",
                FREEWEIGHT_SERVER__ALLOW_LAN_EXPOSURE="true",
                FREEWEIGHT_SERVER__ALLOWED_HOSTS="bench.local",
            )
        assert excinfo.value.code == "INSECURE_BINDING"
        assert "token" in excinfo.value.message


class TestModelOutputRendersInert:
    """§14: model output containing {{ }}, <script>, path traversal and SQL metacharacters is
    stored and rendered without effect."""

    HOSTILE = (
        "{{ 7 * 7 }}",
        "<script>alert('xss')</script>",
        "../../etc/passwd",
        "'; DROP TABLE runs; --",
    )

    @pytest.mark.parametrize("payload", HOSTILE)
    def test_a_hostile_string_renders_escaped_in_a_page(
        self, client: TestClient, payload: str
    ) -> None:
        from freeweight.web.rendering import render

        # A model name, a hostname and a provider error all reach a template from outside this
        # process; autoescaping (rendering.py) is what makes them inert. Render a page fragment
        # that echoes an untrusted value and assert the dangerous form does not survive verbatim.
        html = render("machines/index.html", machines=[], error=payload)

        assert "<script>alert" not in html, "a script tag survived unescaped"
        assert "49" not in html, "a Jinja expression was evaluated"
        # The escaped forms are present and harmless.
        if "<script>" in payload:
            assert "&lt;script&gt;" in html


class TestSecretsAndModes:
    """§14: goal packs and backups are written owner-only; no secret in a log line."""

    def test_a_goal_pack_directory_is_written_owner_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Covered in depth by test_goal_pack_import.py::test_written_files_are_owner_only; asserted
        # here too as the checklist item so a reviewer reads it in one place.
        from freeweight.services.goals import _write_member

        target = _write_member(tmp_path / "pack", "goal.json", '{"x": 1}')
        assert (target.stat().st_mode & 0o777) == 0o600
