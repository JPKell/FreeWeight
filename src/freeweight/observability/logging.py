"""freeweight.observability.logging — structured logging, per Observability Standards.

Two formatters (text for a TTY, JSON Lines otherwise), one logger per module, and a
``contextvars``-based correlation context so every log record produced while handling a request
carries that request's ``request_id`` without threading it through every function signature (§2).
A redaction filter removes anything shaped like a secret before it reaches either formatter (§3.2).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from baseaicore.timeutil import to_rfc3339, utc_now

from freeweight.__about__ import __version__

__all__ = ["bind_context", "configure_logging", "current_context"]

_REDACT_PATTERN: Final = re.compile(r"(?i)(token|key|secret|password|authorization|cookie)")
_REDACTED_VALUE: Final = "********"

_context_var: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar(
    "freeweight_log_context", default=None
)

_STANDARD_RECORD_ATTRS: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime"}


@contextmanager
def bind_context(**fields: Any) -> Iterator[None]:
    """Add correlation fields to every log record produced within this block.

    Nested calls compose: an inner ``bind_context`` sees and extends the outer one's fields, and
    restores exactly the outer state on exit, so a background task started within a request keeps
    that request's ``request_id`` (§2).
    """
    current = dict(_context_var.get() or {})
    current.update(fields)
    token = _context_var.set(current)
    try:
        yield
    finally:
        _context_var.reset(token)


def current_context() -> dict[str, Any]:
    """Return a copy of the correlation fields bound in the current context."""
    return dict(_context_var.get() or {})


def _redact(value: Any) -> Any:
    """Recursively replace any value whose key looks secret-shaped with a fixed placeholder."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED_VALUE if _REDACT_PATTERN.search(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Return the caller-supplied ``extra={...}`` fields on a log record.

    ``logging`` merges ``extra`` keys directly onto the record's ``__dict__``; this recovers them
    by diffing against the attributes a freshly constructed record already has.
    """
    return {
        key: value for key, value in record.__dict__.items() if key not in _STANDARD_RECORD_ATTRS
    }


class JsonFormatter(logging.Formatter):
    """Renders one JSON object per line, per Observability Standards §1."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a single JSON line, redacted."""
        payload: dict[str, Any] = {
            "ts": to_rfc3339(utc_now()),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
            "app": "freeweight",
            "version": __version__,
            "pid": os.getpid(),
        }
        payload.update(current_context())
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(_redact(payload), default=str)


class TextFormatter(logging.Formatter):
    """Renders one human-aligned line per record, for a TTY."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as an aligned, human-readable line, redacted."""
        timestamp = self.formatTime(record, "%H:%M:%S")
        base = f"{timestamp} {record.levelname:<8} {record.name} {record.getMessage()}"
        fields: dict[str, Any] = dict(current_context())
        fields.update(_extra_fields(record))
        redacted = _redact(fields)
        if redacted:
            base += " " + " ".join(f"{key}={value}" for key, value in redacted.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(*, level: str = "INFO", log_format: str = "auto") -> None:
    """Configure the root logger once, at process startup.

    Args:
        level: A standard level name (``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``).
        log_format: ``"text"``, ``"json"``, or ``"auto"`` (text on a TTY, JSON otherwise).

    Logs go to stderr, never stdout: stdout is reserved for CLI command data, per CLI
    Standards §3 — piping ``--json`` output into ``jq`` must never require filtering out log
    lines.
    """
    resolved_format = log_format
    if resolved_format == "auto":
        resolved_format = "text" if sys.stderr.isatty() else "json"

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if resolved_format == "json" else TextFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
