"""freeweight.infrastructure.db.repositories.settings — the ``settings`` table's only writer.

The table exists since Phase 2 but has had no reader or writer until now: it is the runtime
key-value store for small, non-secret facts the application records about its own operation —
security-relevant configuration stays in ``config.toml``/environment variables only (configuration
standards §7; :class:`~freeweight.infrastructure.db.models.Setting`'s own docstring). Its first
consumer is Phase 3's "last discovery attempt" record, read by the models page and CLI so they can
say the data is stale without probing the provider on every read (data model §2, ``settings``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weightsdb import upsert

from freeweight.infrastructure.db.models import Setting

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

__all__ = ["SettingsRepository"]


class SettingsRepository:
    """Reads and writes :class:`~freeweight.infrastructure.db.models.Setting` rows.

    Stateless: holds no session and no cache, so one instance is safely shared across requests.
    """

    def get(self, session: Session, key: str) -> Any | None:  # noqa: ANN401 — a JSON value's shape is the caller's
        """Return the JSON value stored under ``key``, or ``None`` if it has never been set."""
        setting = session.get(Setting, key)
        return setting.value_json if setting is not None else None

    def set(self, session: Session, key: str, value: Any, *, now: datetime) -> None:  # noqa: ANN401
        """Store ``value`` under ``key``, replacing whatever was there.

        Args:
            session: The caller's active session.
            key: The setting's name.
            value: A JSON-serializable value (already rendered — this method does not canonicalize
                it, so a caller storing a :data:`~baseaicore.UNSUPPORTED` field renders it first).
            now: The instant to record as ``updated_at``. Injected so callers are deterministic in
                tests, exactly as every other upsert in this package is.
        """
        upsert(
            session,
            Setting,
            values={"key": key, "value_json": value, "updated_at": now},
            index_elements=["key"],
        )
