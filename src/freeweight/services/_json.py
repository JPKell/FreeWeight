"""freeweight.services._json — the one round trip from a value to storable, JSON-safe Python.

Everything a service writes into a ``*_json`` column goes through :func:`baseaicore.canonical_json`
first, so an ``UNSUPPORTED`` measurement, a ``Money`` or a frozen dataclass is rendered the one way
the suite renders it — and refused, rather than silently coerced, when it cannot be.
"""

from __future__ import annotations

import json
from typing import Any, cast

from baseaicore import canonical_json

__all__ = ["canonical_dict", "json_safe"]


def json_safe(value: object) -> Any:  # noqa: ANN401 — a JSON value has no narrower type
    """Round-trip ``value`` through canonical JSON into plain, JSON-safe Python."""
    return json.loads(canonical_json(value))


def canonical_dict(value: object) -> dict[str, Any]:
    """Round-trip a mapping through canonical JSON into a plain ``dict``.

    ``json.loads`` is typed to return ``Any``; the cast is safe because :func:`canonical_json`
    only ever produces a JSON object at the top level for a mapping input.
    """
    return cast("dict[str, Any]", json_safe(value))
