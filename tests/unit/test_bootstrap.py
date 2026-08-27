"""Unit tests for freeweight.bootstrap: the composition root."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI

from freeweight.bootstrap import Application, bootstrap, create_app_from_environment


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """``bootstrap`` calls ``configure_logging``, which mutates the root logger; restore it."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


def test_bootstrap_wires_settings_and_app() -> None:
    application = bootstrap()

    assert isinstance(application, Application)
    assert isinstance(application.app, FastAPI)
    assert application.loaded_settings.settings.server.host == "127.0.0.1"


def test_create_app_from_environment_returns_a_fastapi_app() -> None:
    app = create_app_from_environment()

    assert isinstance(app, FastAPI)
