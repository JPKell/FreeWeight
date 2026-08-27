"""freeweight.services.events — the run event store, and the SSE frames it renders.

Two responsibilities, deliberately together: appending an event durably with a gap-free sequence,
and turning a stored event into the one wire shape
[API Standards §8](../../../../docs/standards/api-and-contract-standards.md) permits. They are one
module because the sequence number is both the database's uniqueness key and the SSE ``id`` a
client sends back as ``Last-Event-ID``; splitting them invites two answers to "what is the id of
this event".

**Persist, then publish.** :meth:`RunEventPublisher.publish` commits its own transaction before
anything can read the event. That ordering is what makes replay and restart-survival work: the
store is the source of truth, and a client that reconnects after the server was restarted asks the
same question it asks after a dropped connection.

**There is no in-memory fan-out.** API standards §8 permits one as "only a latency optimization",
and this module deliberately does not have one: subscribers read the store, after
``Last-Event-ID``, on a short poll. That choice removes an entire class of defect the phase's own
risk note names first — "duplicated events" — because a subscriber that always asks for *what
comes after what it has already seen* cannot receive a duplicate, cannot skip an event a fan-out
dropped while it was reconnecting, and needs no bounded queue to protect the server from a slow
client (the standard's "subscriber queues are bounded" concern does not arise when there are no
queues; a slow client simply reads fewer, larger batches). The cost is up to
:data:`POLL_INTERVAL_SECONDS` of latency on a live event, which is imperceptible next to a
generation call, and one indexed read per poll per subscriber. A fan-out remains available later
if that read is ever shown to matter — behind this same interface, without changing the wire
contract.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baseaicore import canonical_json, utc_now
from baseaicore.timeutil import to_rfc3339
from setspec.envelope import GeneratorInfo, SchemaVersion, dump_envelope
from sqlalchemy.exc import IntegrityError

from freeweight.__about__ import __version__
from freeweight.infrastructure.db.repositories.runs import RunEventRepository

if TYPE_CHECKING:
    from datetime import datetime

    from baseaicore.timeutil import Clock

    from freeweight.services.database import Database

__all__ = [
    "MAX_SEQUENCE_RETRIES",
    "POLL_INTERVAL_SECONDS",
    "RUN_EVENT_TYPES",
    "RunEventPublisher",
    "StoredEvent",
    "event_to_json",
    "format_event_frame",
    "format_heartbeat",
    "read_events",
]

logger = logging.getLogger(__name__)

_GENERATOR = GeneratorInfo(name="freeweight", version=__version__)
_EVENT_SCHEMA_VERSION = SchemaVersion(1, 0)

POLL_INTERVAL_SECONDS = 0.1
"""How often a live subscriber asks the store for events after the last one it saw."""

MAX_SEQUENCE_RETRIES = 8
"""How many times :meth:`RunEventPublisher.publish` recomputes a sequence after a collision.

A collision needs two writers appending to the *same run* at the same instant, which the single
scheduler thread makes rare: in practice only a cancellation arriving from a request thread while
the scheduler emits progress. Eight retries is far beyond what that contention can produce, and
exhausting them is a defect worth an exception rather than a silently dropped event.
"""

RUN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run.started",
        "run.progress",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
        "run.degraded",
        "test.started",
        "test.progress",
        "test.completed",
        "test.skipped",
        "sample.started",
        "sample.completed",
        "sample.failed",
    }
)
"""The run event vocabulary of [api.md §4](../../../../docs/apps/freeweight/api.md).

``noun.verb`` in past tense, and part of the public contract for API v1 (observability standards
§4.1) — which is why publishing an unknown type is refused rather than accepted: an event nobody
declared is an event no client can be written against.
"""

_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
)
"""Events after which a stream may close. A client seeing none treats closure as interruption."""


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One persisted run event, detached from the session that read it.

    Attributes:
        event_id: The event's own ULID.
        run_id: The run it belongs to.
        sequence: Gap-free within the run, from 1. This is the SSE ``id``.
        event_type: One of :data:`RUN_EVENT_TYPES`.
        timestamp: When the event happened.
        message: One human-readable line, or ``None``.
        progress_completed: Units finished, or ``None`` when this event carries no progress.
        progress_total: Units expected, or ``None``.
        data: The event's own payload.
    """

    event_id: str
    run_id: str
    sequence: int
    event_type: str
    timestamp: datetime
    message: str | None
    progress_completed: int | None
    progress_total: int | None
    data: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        """Whether a stream may close after this event (API standards §8)."""
        return self.event_type in _TERMINAL_EVENT_TYPES


def _to_stored(row: Any) -> StoredEvent:  # noqa: ANN401 — one ORM row, never leaving this module
    """Convert a ``RunEvent`` ORM row into the detached value the rest of the app sees."""
    data = row.data_json if isinstance(row.data_json, dict) else {}
    return StoredEvent(
        event_id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        event_type=row.event_type,
        timestamp=row.timestamp,
        message=row.message,
        progress_completed=row.progress_completed,
        progress_total=row.progress_total,
        data=dict(data),
    )


class RunEventPublisher:
    """Appends run events durably, with a gap-free per-run sequence.

    Owns its own short transaction per event rather than joining the caller's: an event describing
    "sample 12 completed" must be readable by a browser *while* the run is still going, and an
    event enlisted in the run's transaction would only become visible when the run ended. The
    ordering guarantee that matters — raw data first, then the event that announces it — is kept
    by calling this *after* the write it describes has committed.

    Args:
        database: The application's database handle.
        clock: Returns the current instant; injected for deterministic tests.
    """

    __slots__ = ("_clock", "_database")

    def __init__(self, database: Database, *, clock: Clock = utc_now) -> None:
        """Bind the publisher to a database handle."""
        self._database = database
        self._clock = clock

    def publish(
        self,
        run_id: str,
        event_type: str,
        *,
        message: str | None = None,
        progress: tuple[int, int] | None = None,
        data: dict[str, Any] | None = None,
    ) -> StoredEvent:
        """Append one event to ``run_id``'s stream and return it.

        Retries on a sequence collision: two threads can compute the same ``max(sequence) + 1``,
        and the database's uniqueness constraint is what turns that into a retry rather than a
        stream with a duplicate id in it.

        Every event is also logged at INFO under the same name, so a log search and the UI
        timeline tell the same story (observability standards §4).

        Args:
            run_id: The run the event belongs to.
            event_type: One of :data:`RUN_EVENT_TYPES`.
            message: One human-readable line for the timeline.
            progress: ``(completed, total)``, or ``None`` when this event carries no progress.
                Never ``(0, 0)`` as a stand-in for "no progress" — a progress bar renders that as
                0 %, which is a different claim from "this event is not about progress".
            data: The event's payload. Must be JSON-serializable.

        Returns:
            The stored event, with the sequence it was actually written at.

        Raises:
            ValueError: ``event_type`` is not in :data:`RUN_EVENT_TYPES`.
            RuntimeError: :data:`MAX_SEQUENCE_RETRIES` consecutive sequence collisions.
        """
        if event_type not in RUN_EVENT_TYPES:
            raise ValueError(
                f"{event_type!r} is not a declared run event type; the vocabulary is "
                f"{sorted(RUN_EVENT_TYPES)} (api.md §4)."
            )
        repository = RunEventRepository()
        now = self._clock()
        payload = dict(data or {})
        for _attempt in range(MAX_SEQUENCE_RETRIES):
            try:
                with self._database.write() as session:
                    sequence = repository.next_sequence(session, run_id)
                    row = repository.append(
                        session,
                        run_id=run_id,
                        sequence=sequence,
                        event_type=event_type,
                        message=message,
                        progress_completed=progress[0] if progress is not None else None,
                        progress_total=progress[1] if progress is not None else None,
                        data_json=payload,
                        now=now,
                    )
                    stored = _to_stored(row)
            except IntegrityError:
                continue
            # Not ``extra={"message": ...}``: ``message`` is one of ``LogRecord``'s own
            # attributes, and ``Logger.makeRecord`` raises ``KeyError: "Attempt to overwrite
            # 'message' in LogRecord"`` rather than shadowing it. It only raises once the logger
            # is actually enabled for INFO, which is why a suite whose root logger sits at WARNING
            # never sees it and a configured server fails on its very first event.
            logger.info(
                event_type,
                extra={
                    "run_id": run_id,
                    "sequence": stored.sequence,
                    "event_message": message,
                },
            )
            return stored
        raise RuntimeError(
            f"Could not append a {event_type!r} event to run {run_id!r}: "
            f"{MAX_SEQUENCE_RETRIES} consecutive sequence collisions. This means more writers are "
            "appending to one run than this application creates, which is a defect."
        )


def read_events(
    database: Database, run_id: str, *, after_sequence: int = 0, limit: int = 200
) -> tuple[StoredEvent, ...]:
    """Read one run's events after ``after_sequence``.

    The single read behind both replay and the live stream — see this module's docstring on why
    there is no second path. Runs in a read-only transaction, so a page full of subscribers never
    queues behind SQLite's single write lock.

    Args:
        database: The application's database handle.
        run_id: The run to read.
        after_sequence: Return events strictly after this sequence; ``0`` starts at the beginning.
        limit: Maximum events in this batch.

    Returns:
        The events, ascending by sequence. Empty when there is nothing new.
    """
    repository = RunEventRepository()
    with database.read() as session:
        rows = repository.list_since(session, run_id, after_sequence=after_sequence, limit=limit)
        return tuple(_to_stored(row) for row in rows)


def format_event_frame(event: StoredEvent, *, clock: Clock = utc_now) -> str:
    """Format one stored event as its SSE frame, per API standards §8.

    The envelope fields are siblings of ``payload``, never mixed into it
    ([ADR-0025 §3](../../../../docs/adr/0025-envelope-boundaries.md)) — which
    :func:`~setspec.envelope.dump_envelope` guarantees by construction, since the payload is
    handed to it whole.

    Args:
        event: The event to render.
        clock: Returns the instant stamped into the envelope's ``generated_at``; injected so a
            test can produce a byte-identical frame.

    Returns:
        The complete frame: ``id``, ``event`` and ``data`` lines plus the trailing blank line.
        ``id`` is the event's sequence, which is what the client sends back as ``Last-Event-ID``.
    """
    payload: dict[str, Any] = {
        "event_id": event.event_id,
        "sequence": event.sequence,
        "type": event.event_type,
        "entity": {"kind": "run", "id": event.run_id},
        "timestamp": to_rfc3339(event.timestamp),
        "message": event.message,
        "data": event.data,
    }
    if event.progress_completed is not None and event.progress_total is not None:
        payload["progress"] = {
            "completed": event.progress_completed,
            "total": event.progress_total,
        }
    data = dump_envelope(
        payload,
        schema="event.envelope",
        version=_EVENT_SCHEMA_VERSION,
        generator=_GENERATOR,
        clock=clock,
    )
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"


def format_heartbeat(*, clock: Clock = utc_now) -> str:
    """Format one SSE heartbeat comment, sent every 15 s per API standards §8."""
    return f": heartbeat {to_rfc3339(clock())}\n\n"


def event_to_json(event: StoredEvent) -> dict[str, Any]:
    """Render a stored event as the plain JSON the CLI's ``--jsonl`` stream prints.

    Not enveloped: the envelope identifies a *document* that outlives the request
    (ADR-0025), and a line in a terminal stream is not one. The CLI and the browser therefore see
    the same fields in the same names, one wrapped for the wire and one not.
    """
    return dict(
        json.loads(
            canonical_json(
                {
                    "event_id": event.event_id,
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "run_id": event.run_id,
                    "timestamp": event.timestamp,
                    "message": event.message,
                    "progress_completed": event.progress_completed,
                    "progress_total": event.progress_total,
                    "data": event.data,
                }
            )
        )
    )
