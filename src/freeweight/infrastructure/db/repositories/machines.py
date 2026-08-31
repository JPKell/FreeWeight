"""freeweight.infrastructure.db.repositories.machines — the ``machines`` table's only writer.

Repository methods take a session; they never open one (database standards §6) — the caller's
service function owns the transaction boundary. Every method returns a detached ORM instance
(``expire_on_commit=False`` on the session factory), never a live, session-bound query result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from weightsdb import upsert

from freeweight.infrastructure.db.models import Machine

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

__all__ = ["MachineRepository"]


class MachineRepository:
    """Reads and writes :class:`~freeweight.infrastructure.db.models.Machine` rows.

    Stateless: holds no session and no cache, so one instance is safely shared across requests.
    """

    def get_by_fingerprint(self, session: Session, machine_fingerprint: str) -> Machine | None:
        """Return the machine with this fingerprint, or ``None`` if it has never been seen.

        Args:
            session: The caller's active session.
            machine_fingerprint: The fingerprint to look up.

        Returns:
            The matching :class:`Machine`, or ``None``.
        """
        return session.scalars(
            select(Machine).where(Machine.machine_fingerprint == machine_fingerprint)
        ).one_or_none()

    def get_by_id(self, session: Session, machine_id: str) -> Machine | None:
        """Return the machine with this primary key, or ``None`` if it does not exist."""
        return session.get(Machine, machine_id)

    def list_all(self, session: Session) -> list[Machine]:
        """Return every known machine, ordered by first sighting.

        Phase 2 has no callers of this beyond tests — the machines page is Phase 4's — but the
        method exists now because Phase 4's telemetry service needs it on day one and the query
        has no dependency on anything Phase 4 introduces.
        """
        return list(session.scalars(select(Machine).order_by(Machine.first_seen_at.asc())).all())

    def upsert(
        self,
        session: Session,
        *,
        machine_fingerprint: str,
        hostname: str | None,
        os_name: str | None,
        os_version: str | None,
        kernel: str | None,
        architecture: str | None,
        cpu_model: str | None,
        physical_cores: int | None,
        logical_cores: int | None,
        ram_bytes: int | None,
        gpus_json: Any,
        storage_json: Any,
        python_version: str | None,
        now: datetime,
    ) -> Machine:
        """Insert this machine on first sight, or refresh it on every later sighting.

        A single natural key (``machine_fingerprint``) with no partial-index subtlety, so this
        goes through the one sanctioned dialect-correct upsert
        (:func:`~freeweight.infrastructure.db.upsert.upsert`) rather than hand-rolled conditional
        writes. ``first_seen_at`` is set from ``now`` on the initial insert (so it is
        deterministic, not wall-clock time) but named in ``no_update``, so a later sighting can
        never move it.

        Args:
            session: The caller's active session.
            machine_fingerprint: The stable identity to upsert on.
            hostname: See :class:`~baseaicore.machine.MachineProfile`.
            os_name: See :class:`~baseaicore.machine.MachineProfile`.
            os_version: See :class:`~baseaicore.machine.MachineProfile`.
            kernel: See :class:`~baseaicore.machine.MachineProfile`.
            architecture: See :class:`~baseaicore.machine.MachineProfile`.
            cpu_model: See :class:`~baseaicore.machine.MachineProfile`.
            physical_cores: See :class:`~baseaicore.machine.MachineProfile`; ``None`` when not
                reported. Not a :data:`~baseaicore.measurement.Measurement` pair at this table —
                see :class:`~freeweight.infrastructure.db.models.Machine`.
            logical_cores: As above.
            ram_bytes: As above.
            gpus_json: The machine's GPU set, already JSON-serializable (every
                :data:`~baseaicore.measurement.Unsupported` field rendered as the string
                ``"unsupported"`` by the caller).
            storage_json: The machine's storage devices, serialized the same way.
            python_version: The interpreter version that collected this profile.
            now: The instant to record as ``last_seen_at`` (and, on first sight, ``first_seen_at``
                too, via the column default). Injected so callers are deterministic in tests.

        Returns:
            The current :class:`Machine` row after the write.
        """
        upsert(
            session,
            Machine,
            values={
                "machine_fingerprint": machine_fingerprint,
                "hostname": hostname,
                "os_name": os_name,
                "os_version": os_version,
                "kernel": kernel,
                "architecture": architecture,
                "cpu_model": cpu_model,
                "physical_cores": physical_cores,
                "logical_cores": logical_cores,
                "ram_bytes": ram_bytes,
                "gpus_json": gpus_json,
                "storage_json": storage_json,
                "python_version": python_version,
                "first_seen_at": now,
                "last_seen_at": now,
            },
            index_elements=["machine_fingerprint"],
            no_update=frozenset({"first_seen_at"}),
        )
        session.flush()
        machine = self.get_by_fingerprint(session, machine_fingerprint)
        if machine is None:
            raise RuntimeError(
                f"Machine {machine_fingerprint!r} was not found immediately after being "
                "upserted in this same transaction; this indicates a driver or session bug."
            )
        return machine
