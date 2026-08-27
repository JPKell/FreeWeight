"""Unit tests for freeweight.observability.logging."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from freeweight.observability.logging import (
    JsonFormatter,
    TextFormatter,
    bind_context,
    configure_logging,
    current_context,
)


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """``configure_logging`` mutates the root logger; restore it so tests stay isolated."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


def _make_record(*, message: str = "run.completed", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="freeweight.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_bind_context_is_empty_outside_any_block() -> None:
    assert current_context() == {}


def test_bind_context_sets_and_restores_fields() -> None:
    with bind_context(request_id="abc"):
        assert current_context() == {"request_id": "abc"}
    assert current_context() == {}


def test_nested_bind_context_composes_and_restores_outer_state() -> None:
    with bind_context(request_id="abc"):
        with bind_context(run_id="xyz"):
            assert current_context() == {"request_id": "abc", "run_id": "xyz"}
        assert current_context() == {"request_id": "abc"}
    assert current_context() == {}


def test_json_formatter_emits_parseable_json_with_required_fields() -> None:
    formatter = JsonFormatter()
    record = _make_record(duration_ms=42)

    with bind_context(request_id="req-1"):
        line = formatter.format(record)

    payload = json.loads(line)
    assert payload["event"] == "run.completed"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "freeweight.test"
    assert payload["app"] == "freeweight"
    assert payload["request_id"] == "req-1"
    assert payload["duration_ms"] == 42
    assert "ts" in payload
    assert "pid" in payload


def test_json_formatter_redacts_secret_shaped_fields() -> None:
    formatter = JsonFormatter()
    record = _make_record(message="token.issued", api_key="super-secret")

    payload = json.loads(formatter.format(record))

    assert payload["api_key"] == "********"


def test_json_formatter_includes_exception_info() -> None:
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record(message="request.failed")
        record.exc_info = sys.exc_info()

    payload = json.loads(formatter.format(record))
    assert "ValueError" in payload["exc_info"]


def test_text_formatter_includes_level_and_context_fields() -> None:
    formatter = TextFormatter()
    record = _make_record(duration_ms=7)

    with bind_context(request_id="req-2"):
        line = formatter.format(record)

    assert "INFO" in line
    assert "run.completed" in line
    assert "request_id=req-2" in line
    assert "duration_ms=7" in line


def test_text_formatter_redacts_secret_shaped_fields() -> None:
    formatter = TextFormatter()
    record = _make_record(password="hunter2")  # noqa: S106 — a field name, not a real credential

    line = formatter.format(record)

    assert "hunter2" not in line
    assert "********" in line


def test_configure_logging_installs_json_handler() -> None:
    configure_logging(level="DEBUG", log_format="json")

    logger = logging.getLogger("freeweight.test.configure")
    logger.info("probe.event", extra={"marker": "value"})

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_configure_logging_installs_text_handler_when_requested() -> None:
    configure_logging(level="WARNING", log_format="text")

    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert isinstance(root.handlers[0].formatter, TextFormatter)


def test_configure_logging_replaces_previous_handlers() -> None:
    configure_logging(level="INFO", log_format="json")
    configure_logging(level="INFO", log_format="text")

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, TextFormatter)
