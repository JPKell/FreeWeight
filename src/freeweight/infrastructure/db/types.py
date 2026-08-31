"""Deprecated: use ``weightsdb`` directly in new code.

Kept so existing migration revisions — which instantiate these column types by this module's
historical import path (``import freeweight.infrastructure.db.types``) — keep working unchanged.
Alembic loads the full revision history to walk it, so deleting this module would break every
revision file at import time; migration history is an immutable record here and is not rewritten
to avoid that (WeightsDB adoption checklist §2).
"""

from __future__ import annotations

from weightsdb import PortableJSON, UtcDateTime, measurement_columns, ulid_primary_key

__all__ = ["PortableJSON", "UtcDateTime", "measurement_columns", "ulid_primary_key"]
