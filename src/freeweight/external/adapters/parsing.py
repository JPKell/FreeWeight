"""freeweight.external.adapters.parsing — the shared safety net every adapter parser sits on.

One place decides what "treat output as untrusted" means in practice, so nine adapters cannot
each get it subtly wrong: bounded JSON decoding that never raises, a numeric coercion that
rejects NaN/inf and clamps nothing silently, and a detail-excerpt cap so a hostile output cannot
store a megabyte in ``result_json``.
"""

from __future__ import annotations

import json
import math
from typing import Any

__all__ = [
    "DETAIL_EXCERPT_CHARS",
    "clamp_unit_score",
    "excerpt",
    "safe_json",
    "safe_jsonl",
]

DETAIL_EXCERPT_CHARS = 200
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def safe_json(raw: bytes) -> tuple[Any, str | None]:
    """Decode one JSON document, returning ``(value, None)`` or ``(None, reason)``.

    Never raises: a decode error, an oversize payload and non-UTF-8 bytes are all reasons
    returned as data, because this is the untrusted-input boundary and the caller must decide what
    a failure means for the run.
    """
    if len(raw) > _MAX_OUTPUT_BYTES:
        return None, f"output exceeds the {_MAX_OUTPUT_BYTES}-byte parse cap"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"output is not valid UTF-8: {exc}"
    try:
        return json.loads(text), None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"output is not valid JSON: {exc}"


def safe_jsonl(raw: bytes) -> tuple[list[Any], int]:
    """Decode JSON-lines output, returning ``(rows, skipped)``.

    Each line is decoded independently, so a truncated final line or one corrupt row in the
    middle costs exactly that row rather than the whole file — the partial-output case P13's tests
    require. ``skipped`` counts the lines that did not parse.
    """
    rows: list[Any] = []
    skipped = 0
    if len(raw) > _MAX_OUTPUT_BYTES:
        return rows, 0
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rows.append(json.loads(stripped))
        except (json.JSONDecodeError, UnicodeDecodeError):
            skipped += 1
    return rows, skipped


def clamp_unit_score(value: Any) -> float | None:  # noqa: ANN401 — value is untrusted
    """Coerce an untrusted value to a ``0.0..1.0`` score, or ``None`` when it is not one.

    Rejects NaN and infinity outright — a benchmark that emitted one has a bug, and letting it
    through would poison every aggregate it entered. A number outside ``0..1`` is also rejected
    rather than clamped: silently clamping ``1.4`` to ``1.0`` would invent a passing score the
    tool never reported.
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    if not 0.0 <= number <= 1.0:
        return None
    return number


def excerpt(text: Any, *, limit: int = DETAIL_EXCERPT_CHARS) -> str:  # noqa: ANN401 — untrusted
    """A bounded, safe string for ``result_json``: at most ``limit`` characters, never more."""
    rendered = text if isinstance(text, str) else json.dumps(text, default=str)
    if len(rendered) <= limit:
        return rendered
    return rendered[:limit] + "…"
