"""Security: the settings a running server may change, and the ones no browser session can.

Configuration standards §7 draws one line — anything security-relevant is file, environment or CLI
only — and API §8 says an attempt to cross it returns ``403 FORBIDDEN`` naming the key. This file
is that line under test, because a boundary that is only documented is not a boundary.

Four properties, and each of them fails differently if it is wrong:

* the refusal is by **allowlist**, so a setting nobody thought to forbid is forbidden by default;
* a request naming one permitted key and one forbidden key changes **neither**, so a partial
  application can never leave the operator with state they cannot reason about;
* a value outside its declared range is refused **before** anything is written;
* an environment variable **wins** over a stored value, and the UI says so rather than showing a
  stored number the running server is not using.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from freeweight.config import load_settings
from freeweight.infrastructure.db.engine import create_engine_for
from freeweight.infrastructure.db.migration import MigrationRunner
from freeweight.services.database import MIGRATIONS_LOCATION
from freeweight.services.settings import CONFIG_ONLY_KEYS, RUNTIME_SETTINGS
from freeweight.web.app import create_app

# The whole point of the two tests that use it: a request that would expose the server to every
# interface must be refused, so the string has to appear in the test that proves it is.
EXPOSED_BIND = "0.0.0.0"  # noqa: S104 — the value under refusal, never a value this code binds


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database = tmp_path / "freeweight.sqlite3"
    monkeypatch.setenv("FREEWEIGHT_STORAGE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("FREEWEIGHT_PROVIDER__KIND", "fake")
    engine = create_engine_for(f"sqlite:///{database}")
    try:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    finally:
        engine.dispose()
    return tmp_path


@pytest.fixture
def client(workspace: Path) -> Iterator[TestClient]:
    loaded = load_settings(config_path=workspace / "missing.toml")
    with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as test_client:
        yield test_client


class TestTheAllowlist:
    def test_no_security_relevant_key_is_runtime_changeable(self) -> None:
        """The two sets must not intersect, whatever anyone adds to either of them later."""
        writable = {setting.key for setting in RUNTIME_SETTINGS}

        assert writable & CONFIG_ONLY_KEYS == set()

    def test_the_documented_security_relevant_keys_are_all_listed(self) -> None:
        """Configuration standards §7 names them: bind address, exposure, tokens, remote, roots."""
        for key in (
            "server.host",
            "server.allow_lan_exposure",
            "auth.tokens",
            "providers.allow_remote",
            "storage.database_url",
            "storage.artifact_dir",
        ):
            assert key in CONFIG_ONLY_KEYS, key

    def test_a_key_nobody_declared_is_refused_by_default(self, client: TestClient) -> None:
        refused = client.put(
            "/api/v1/settings", json={"changes": {"execution.some_future_setting": 1}}
        )

        assert refused.status_code == 400
        assert "not a runtime-changeable setting" in refused.json()["error"]["message"]

    @pytest.mark.parametrize(
        "key", ["server.host", "auth.tokens", "providers.allow_remote", "storage.database_url"]
    )
    def test_a_config_only_key_is_403_naming_the_key(self, client: TestClient, key: str) -> None:
        refused = client.put("/api/v1/settings", json={"changes": {key: "anything"}})

        assert refused.status_code == 403
        body = refused.json()["error"]
        assert body["code"] == "FORBIDDEN"
        assert key in body["message"]
        assert "configuration file" in body["message"]


class TestAllOrNothing:
    def test_one_forbidden_key_prevents_the_permitted_one_from_landing(
        self, client: TestClient
    ) -> None:
        before = client.get("/api/v1/settings").json()

        refused = client.put(
            "/api/v1/settings",
            json={"changes": {"telemetry.interval_ms": 250, "server.host": EXPOSED_BIND}},
        )

        assert refused.status_code == 403
        after = client.get("/api/v1/settings").json()
        assert after["items"] == before["items"], "a partial settings update was applied"

    def test_an_out_of_range_value_is_refused_before_anything_is_written(
        self, client: TestClient
    ) -> None:
        refused = client.put(
            "/api/v1/settings",
            json={"changes": {"telemetry.interval_ms": 1, "execution.seed": 7}},
        )

        assert refused.status_code == 400
        assert "at least" in refused.json()["error"]["message"]
        stored = {item["key"]: item for item in client.get("/api/v1/settings").json()["items"]}
        assert stored["execution.seed"]["stored_value"] is None


class TestWhatIsInEffect:
    def test_a_stored_value_is_reported_as_coming_from_the_database(
        self, client: TestClient
    ) -> None:
        applied = client.put("/api/v1/settings", json={"changes": {"telemetry.interval_ms": 2000}})

        assert applied.status_code == 200, applied.text
        stored = {item["key"]: item for item in applied.json()["items"]}
        assert stored["telemetry.interval_ms"]["stored_value"] == 2000  # noqa: PLR2004
        assert stored["telemetry.interval_ms"]["source"] == "database"

    def test_an_environment_variable_wins_and_the_response_says_so(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Precedence is ``defaults -> file -> database -> env -> CLI``, and the UI must not lie."""
        loaded = load_settings(config_path=workspace / "missing.toml")
        with TestClient(create_app(loaded.settings), base_url="http://127.0.0.1") as client:
            client.put("/api/v1/settings", json={"changes": {"telemetry.interval_ms": 2000}})

        monkeypatch.setenv("FREEWEIGHT_TELEMETRY__INTERVAL_MS", "500")
        reloaded = load_settings(config_path=workspace / "missing.toml")
        with TestClient(create_app(reloaded.settings), base_url="http://127.0.0.1") as client:
            stored = {item["key"]: item for item in client.get("/api/v1/settings").json()["items"]}
            page = client.get("/settings")

        assert stored["telemetry.interval_ms"]["value"] == 500  # noqa: PLR2004
        assert stored["telemetry.interval_ms"]["stored_value"] == 2000  # noqa: PLR2004
        assert stored["telemetry.interval_ms"]["source"] == "env"
        assert stored["telemetry.interval_ms"]["overridden_by_env"] is True
        assert "wins over anything stored here" in page.text

    def test_the_page_lists_the_config_only_keys_as_read_only(self, client: TestClient) -> None:
        page = client.get("/settings")

        assert page.status_code == 200
        assert "Configuration file only" in page.text
        for key in ("server.host", "auth.tokens", "providers.allow_remote"):
            assert key in page.text
        # And offers no control for any of them.
        assert 'name="setting.server.host"' not in page.text


class TestTheFormAgreesWithTheApi:
    def test_a_saved_form_stores_what_the_api_would_have(self, client: TestClient) -> None:
        saved = client.post(
            "/settings",
            data={
                "setting.execution.measured_repetitions": "5",
                "setting.execution.randomize_case_order": "true",
                "booleans": "execution.randomize_case_order",
            },
        )

        assert saved.status_code == 200, saved.text
        assert "Saved." in saved.text
        stored = {item["key"]: item for item in client.get("/api/v1/settings").json()["items"]}
        assert stored["execution.measured_repetitions"]["stored_value"] == 5  # noqa: PLR2004

    def test_an_unchecked_box_turns_the_setting_off_rather_than_leaving_it(
        self, client: TestClient
    ) -> None:
        """An unchecked checkbox submits nothing, so the form names every boolean it rendered."""
        client.post(
            "/settings",
            data={
                "setting.execution.randomize_case_order": "true",
                "booleans": "execution.randomize_case_order",
            },
        )

        client.post("/settings", data={"booleans": "execution.randomize_case_order"})

        stored = {item["key"]: item for item in client.get("/api/v1/settings").json()["items"]}
        assert stored["execution.randomize_case_order"]["stored_value"] is False

    def test_a_form_naming_a_config_only_key_is_403_and_changes_nothing(
        self, client: TestClient
    ) -> None:
        before = client.get("/api/v1/settings").json()

        refused = client.post("/settings", data={"setting.server.host": EXPOSED_BIND})

        assert refused.status_code == 403
        assert "security-relevant" in refused.text
        assert "server.host" in refused.text
        assert client.get("/api/v1/settings").json()["items"] == before["items"]
