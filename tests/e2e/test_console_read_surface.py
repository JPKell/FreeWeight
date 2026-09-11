"""The API surface WeightRoomGym's FreeWeight pages read, over HTTP (row WP3).

Each application binds loopback, so an operator on the LAN reaches FreeWeight's pages only through
the console, and the console calls ``/api/v1`` only (arc index §2 item 4). Every route here is one
the console needed and FreeWeight did not serve — or served narrower than api.md said: the runs
filters and cursor, the samples cursor, the case inspector, a run's telemetry, the models filters
and sort, enabling a model with a JSON body, and the adapters reading.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.e2e.test_declared_endpoints import _completed_run, client, workspace
from typer.testing import CliRunner

from freeweight.cli.main import app as cli_app

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

__all__ = ["client", "workspace"]  # the fixtures, re-used from the declared-endpoint tests

runner = CliRunner()


def _refresh() -> None:
    assert runner.invoke(cli_app, ["models", "refresh"]).exit_code == 0


def _labelled_run(client: TestClient, label: str) -> str:
    created = client.post(
        "/api/v1/runs",
        json={"model": "fake-model:8b-q8_0", "suite": "native.echo", "label": label},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


class TestTheModelsListing:
    def test_each_item_says_its_family_whether_it_is_enabled_and_whether_it_has_results(
        self, client: TestClient
    ) -> None:
        _refresh()
        items = client.get("/api/v1/models").json()["items"]

        assert items
        assert all({"family", "enabled", "has_results"} <= set(item) for item in items)
        assert not any(item["has_results"] for item in items), "nothing has been measured yet"

    def test_has_results_separates_the_measured_model_from_the_rest(
        self, client: TestClient
    ) -> None:
        _completed_run(client)
        measured = client.get("/api/v1/models", params={"has_results": "true"}).json()["items"]
        unmeasured = client.get("/api/v1/models", params={"has_results": "false"}).json()["items"]

        assert [item["provider_model_name"] for item in measured] == ["fake-model:8b-q8_0"]
        assert all(not item["has_results"] for item in unmeasured)

    def test_the_family_filter_matches_the_latest_descriptor(self, client: TestClient) -> None:
        """It used to read an attribute the row never had, so it matched nothing, ever."""
        _refresh()
        family = client.get("/api/v1/models").json()["items"][0]["family"]

        found = client.get("/api/v1/models", params={"family": family}).json()["items"]

        assert found and all(item["family"] == family for item in found)

    def test_sort_orders_by_either_key_and_refuses_any_other(self, client: TestClient) -> None:
        _refresh()
        ascending = client.get("/api/v1/models", params={"sort": "canonical_id"}).json()["items"]
        descending = client.get("/api/v1/models", params={"sort": "-canonical_id"}).json()["items"]
        refused = client.get("/api/v1/models", params={"sort": "size_bytes"})

        names = [item["canonical_id"] for item in ascending]
        assert names == sorted(names)
        assert [item["canonical_id"] for item in descending] == sorted(names, reverse=True)
        assert refused.status_code == 400
        assert refused.json()["error"]["details"]["field"] == "sort"


class TestEnablingAModelOverTheApi:
    def test_a_json_body_disables_the_model_and_enables_it_again(self, client: TestClient) -> None:
        _refresh()
        model_id = client.get("/api/v1/models").json()["items"][0]["id"]

        off = client.post(f"/api/v1/models/{model_id}/enabled", json={"enabled": False})

        assert off.status_code == 200, off.text
        assert off.json()["enabled"] is False and off.json()["canonical_id"]
        assert client.get(f"/api/v1/models/{model_id}").json()["enabled"] is False
        on = client.post(f"/api/v1/models/{model_id}/enabled", json={"enabled": True})
        assert on.json()["enabled"] is True
        assert client.get(f"/api/v1/models/{model_id}").json()["enabled"] is True

    def test_an_unknown_model_is_a_404_and_a_body_without_the_flag_is_refused(
        self, client: TestClient
    ) -> None:
        unknown = client.post(
            "/api/v1/models/01ZZZZZZZZZZZZZZZZZZZZZZZZ/enabled", json={"enabled": True}
        )
        malformed = client.post("/api/v1/models/01ZZZZZZZZZZZZZZZZZZZZZZZZ/enabled", json={})

        assert unknown.status_code == 404, unknown.text
        assert malformed.status_code == 400, malformed.text


class TestTheRunsListing:
    def test_each_filter_narrows_and_the_page_says_so(self, client: TestClient) -> None:
        first = _completed_run(client)
        _refresh()
        labelled = _labelled_run(client, "wp3 filter")
        body = client.get("/api/v1/runs").json()
        machine = body["runs"][-1]["machine_fingerprint"]

        def ids(**params: str) -> set[str]:
            return {run["id"] for run in client.get("/api/v1/runs", params=params).json()["runs"]}

        assert body["page"] == {"limit": 50, "next_cursor": None, "has_more": False}
        assert ids() == {first, labelled}
        assert ids(label="wp3 filter") == {labelled}
        assert ids(suite="native.echo") == {first, labelled}
        assert ids(suite="native.performance") == set()
        assert ids(model="fake-model:8b-q8_0") == {first, labelled}
        assert ids(machine=machine) == {first, labelled}
        assert ids(adapter="no-such-adapter") == set()
        assert ids(since="2999-01-01T00:00:00Z") == set()
        assert ids(until="2000-01-01T00:00:00Z") == set()
        assert all(run["adapter"] is None for run in body["runs"])
        assert all(run["runtime_profile_hash"] for run in body["runs"])

    def test_the_cursor_pages_through_every_run_once(self, client: TestClient) -> None:
        _completed_run(client)
        _labelled_run(client, "second")
        _labelled_run(client, "third")

        seen: list[str] = []
        cursor: str | None = None
        while True:
            params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
            body = client.get("/api/v1/runs", params=params).json()
            seen.extend(run["id"] for run in body["runs"])
            cursor = body["page"]["next_cursor"]
            if cursor is None:
                assert body["page"]["has_more"] is False
                break

        assert len(seen) == 3 and len(set(seen)) == 3

    def test_a_forged_cursor_and_a_malformed_instant_are_refused_by_name(
        self, client: TestClient
    ) -> None:
        forged = client.get("/api/v1/runs", params={"cursor": "not-ours"})
        instant = client.get("/api/v1/runs", params={"since": "yesterday"})

        assert forged.status_code == 400
        assert forged.json()["error"]["details"]["field"] == "cursor"
        assert instant.status_code == 400
        assert instant.json()["error"]["details"]["field"] == "since"


class TestSamplesAndTheCaseInspector:
    def _test_id(self, client: TestClient, run_id: str) -> str:
        return str(client.get(f"/api/v1/runs/{run_id}/tests").json()["tests"][0]["id"])

    def test_samples_page_by_cursor_in_declaration_order(self, client: TestClient) -> None:
        run_id = _completed_run(client)
        test_id = self._test_id(client, run_id)
        path = f"/api/v1/runs/{run_id}/tests/{test_id}/samples"
        everything = client.get(path).json()

        paged: list[str] = []
        cursor: str | None = None
        while True:
            body = client.get(path, params={"limit": "1", **({"cursor": cursor} if cursor else {})})
            page = body.json()
            paged.extend(sample["id"] for sample in page["samples"])
            cursor = page["page"]["next_cursor"]
            if cursor is None:
                break

        assert paged == [sample["id"] for sample in everything["samples"]]
        assert everything["page"]["has_more"] is False
        assert {"prompt_id", "prompt_version", "client_ttft_ms"} <= set(everything["samples"][0])

    def test_one_sample_is_the_whole_stored_record(self, client: TestClient) -> None:
        run_id = _completed_run(client)
        test_id = self._test_id(client, run_id)
        sample = client.get(f"/api/v1/runs/{run_id}/tests/{test_id}/samples").json()["samples"][0]

        body = client.get(f"/api/v1/samples/{sample['id']}").json()

        assert body["sample"]["id"] == sample["id"]
        assert body["run_id"] == run_id and body["run_test_id"] == test_id
        assert body["run_status"] == "completed" and body["run_test_key"]
        assert isinstance(body["tool_calls"], list)
        assert isinstance(body["criterion_scores"], list)
        assert isinstance(body["telemetry"], list)

    def test_an_unknown_sample_is_a_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/samples/01ZZZZZZZZZZZZZZZZZZZZZZZZ")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


class TestARunsTelemetry:
    def test_every_series_shares_the_timestamps_index(self, client: TestClient) -> None:
        run_id = _completed_run(client)

        body = client.get(f"/api/v1/runs/{run_id}/telemetry").json()

        assert body["run_id"] == run_id
        count = body["sample_count"]
        assert len(body["timestamps"]) == count
        assert len(body["cpu_percent"]) == count and len(body["ram_used_bytes"]) == count
        assert all(len(gpu["vram_used_bytes"]) == count for gpu in body["gpus"])

    def test_an_unknown_run_is_a_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/runs/01ZZZZZZZZZZZZZZZZZZZZZZZZ/telemetry")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


class TestTheAdaptersReading:
    def test_adapters_off_is_a_200_that_names_the_key(self, client: TestClient) -> None:
        body = client.get("/api/v1/adapters").json()

        assert body["enabled"] is False
        assert "[adapters] directory" in body["note"]
        assert body["adapters"] == [] and body["directory"] is None
