"""Unit tests for freeweight.web.errors: every exception handler, in isolation.

A minimal FastAPI app with purpose-built routes exercises each handler directly, rather than
relying on the real application to happen to trigger every branch.
"""

from __future__ import annotations

from baseaicore import NotFoundError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from freeweight.web.errors import register_exception_handlers


class _Body(BaseModel):
    name: str


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/suite-error")
    async def _suite_error() -> None:
        raise NotFoundError("Model 'x' is not available.", details={"requested": "x"})

    @app.post("/validated")
    async def _validated(body: _Body) -> dict[str, str]:
        return {"name": body.name}

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("unexpected failure")

    return app


def _client() -> TestClient:
    return TestClient(_build_app(), raise_server_exceptions=False)


def test_suite_error_is_translated_with_its_code_and_details() -> None:
    response = _client().get("/suite-error")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"] == "Model 'x' is not available."
    assert body["error"]["details"] == {"requested": "x"}
    assert "request_id" in body["error"]
    assert "timestamp" in body["error"]
    assert response.headers["X-Request-ID"] == body["error"]["request_id"]


def test_request_validation_error_lists_every_field_problem() -> None:
    response = _client().post("/validated", json={})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["fields"]
    assert body["error"]["details"]["fields"][0]["path"] == "name"


def test_unhandled_exception_becomes_internal_error_without_leaking_details() -> None:
    response = _client().get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "unexpected failure" not in body["error"]["message"]


def test_unmatched_route_is_not_found() -> None:
    response = _client().get("/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_wrong_method_is_method_not_allowed() -> None:
    response = _client().post("/suite-error")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
