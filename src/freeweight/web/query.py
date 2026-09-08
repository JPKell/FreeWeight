"""freeweight.web.query — parsing the query parameters more than one route accepts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from baseaicore import ValidationError, from_rfc3339

if TYPE_CHECKING:
    from datetime import datetime

__all__ = ["parse_instant"]


def parse_instant(value: str | None, *, field: str) -> datetime | None:
    """Parse an RFC 3339 query parameter, or refuse it by name.

    Args:
        value: The raw parameter; ``None`` or empty means "not given".
        field: The parameter's name, for the refusal.

    Raises:
        ValidationError: The value is not RFC 3339.
    """
    if not value:
        return None
    try:
        return from_rfc3339(value)
    except Exception as exc:  # noqa: BLE001 — every parse failure is one validation error
        raise ValidationError(
            f"{field} must be an RFC 3339 instant, such as 2026-08-28T00:00:00Z; got {value!r}.",
            details={"field": field, "value": value},
        ) from exc
