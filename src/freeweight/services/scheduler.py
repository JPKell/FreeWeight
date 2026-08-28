"""freeweight.services.scheduler — the one thread that claims runs and executes them.

**One GPU workload at a time, by construction.** There is exactly one scheduler thread per
process, and it executes one claimed run to a terminal state before looking for another. That is
not a policy the code has to remember to enforce; it is the shape of the loop. A second run
started while one is active is therefore queued rather than run concurrently, with no lock, no
semaphore and no counter to get wrong.

**No leases.** [ADR-0010](../../../../docs/adr/0010-queue-implementation.md) chose a
database-backed queue and [ADR-0029](../../../../docs/adr/0029-queue-mechanics.md) adds leases,
ageing and a lease keeper — for LoadCoach, whose jobs are claimed by *several* workers and whose
ADR-0010 revisit trigger is "jobs executed by workers on other machines". FreeWeight runs one
scheduler in one process against a database it owns exclusively, so a lease would be a lock this
process takes against itself, and its expiry would say only what "the process is gone" already
says. The claim is still atomic (``UPDATE … WHERE status = 'queued'``), because the *recovery*
path and a second FreeWeight process started by mistake both have to be safe.

**Recovery is what makes a killed run resumable.** A run in ``preparing``, ``warming``, ``running``
or ``cancelling`` can only be held by a live process; finding one at startup means the previous
process died, and it becomes ``interrupted`` with its completed tests intact (spec §13). Recovery
runs before the loop starts, so the very first claim already sees a consistent queue.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baseaicore import SuiteError, utc_now

from freeweight.domain.run_state import RunStatus
from freeweight.infrastructure.db.errors import DatabaseUnavailable
from freeweight.infrastructure.db.repositories.runs import RunRepository
from freeweight.services.events import RunEventPublisher
from freeweight.services.runs import build_registry, execute_run

if TYPE_CHECKING:
    from baseaicore.timeutil import Clock
    from modelrack.provider import Provider
    from sweatmeter import TelemetryCollector

    from freeweight.config import Settings, TelemetrySettings
    from freeweight.domain.benchmark import BenchmarkRegistry
    from freeweight.services.database import Database

__all__ = ["RecoveryOutcome", "RunScheduler"]

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 0.25


@contextmanager
def _translated() -> Iterator[None]:
    """Translate raw driver failures into the suite's error hierarchy.

    The same translation :func:`freeweight.services.runs._translated` applies, for the same
    reason: a caller that catches ``DatabaseError`` must not have to also catch
    ``sqlalchemy.exc.OperationalError`` to survive a database that has not been migrated.
    """
    try:
        yield
    except SuiteError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(f"Could not read the run queue: {exc}") from exc


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What startup recovery found and did.

    Attributes:
        interrupted_run_ids: Runs that were mid-flight when the previous process died and are now
            ``interrupted`` — each resumable, each still holding its completed tests.
        queued_depth: How many runs were waiting to be claimed after recovery.
    """

    interrupted_run_ids: tuple[str, ...]
    queued_depth: int


class RunScheduler:
    """Claims queued runs and executes them, one at a time, on its own thread.

    Owned by whoever starts it — the web lifespan for a running server, a test for a test — and
    stopped by the same owner. Building one starts nothing.

    Args:
        database: The application's database handle.
        provider: The provider runs generate through.
        registry: The benchmarks this build can run. Defaults to
            :func:`~freeweight.services.runs.build_registry`.
        collector: The telemetry collector runs are observed through, or ``None`` to record no
            telemetry and skip the idle check. Handed in rather than built here so that the one
            collector the process already owns — the telemetry bar's — is the one a run samples
            from, instead of a second one paying ``nvidia-smi``'s cost in parallel with it.
        telemetry: The ``[telemetry]`` settings, or ``None`` to record nothing. Both this and
            ``collector`` must be present for a run to persist telemetry, so a caller cannot half
            configure it.
        poll_interval_seconds: How long the loop waits when the queue is empty. Only the *empty*
            case waits: after finishing a run the loop looks again immediately, so a queue of ten
            runs is not paced by this value.
        clock: Returns the current instant; injected for deterministic tests.
    """

    __slots__ = (
        "_clock",
        "_collector",
        "_database",
        "_poll_interval_seconds",
        "_provider",
        "_publisher",
        "_registry",
        "_settings",
        "_stop",
        "_telemetry",
        "_thread",
    )

    def __init__(
        self,
        database: Database,
        provider: Provider,
        *,
        registry: BenchmarkRegistry | None = None,
        collector: TelemetryCollector | None = None,
        telemetry: TelemetrySettings | None = None,
        settings: Settings | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Clock = utc_now,
    ) -> None:
        """Configure the scheduler without starting its thread."""
        self._database = database
        self._provider = provider
        self._registry = registry if registry is not None else build_registry()
        self._collector = collector
        self._telemetry = telemetry
        self._settings = settings
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._publisher = RunEventPublisher(database, clock=clock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def recover(self) -> RecoveryOutcome:
        """Mark orphaned runs ``interrupted`` and report the state of the queue.

        Idempotent and safe to call at any time, though it is only correct at startup: a run this
        process is *currently* executing is in one of the orphan statuses too, and calling this
        mid-run would interrupt it. The scheduler calls it once, before the loop starts, which is
        exactly when no run of this process's is in flight.

        Every interrupted run gets a ``run.interrupted`` event on its own stream, so a browser
        that reconnects after the crash sees why its live view stopped rather than an unexplained
        silence.

        Returns:
            What was found and done.
        """
        now = self._clock()
        with self._database.write() as session:
            interrupted = RunRepository().mark_orphans_interrupted(session, now=now)
        with self._database.read() as session:
            depth = RunRepository().queue_depth(session)
        for run_id in interrupted:
            logger.warning("run.interrupted", extra={"run_id": run_id})
            self._publisher.publish(
                run_id,
                "run.interrupted",
                message="The server stopped while this run was in flight; it can be resumed.",
                data={"status": RunStatus.INTERRUPTED.value},
            )
        return RecoveryOutcome(interrupted_run_ids=tuple(interrupted), queued_depth=depth)

    def start(self) -> None:
        """Run recovery, then start the scheduler thread. A no-op if already running.

        A recovery that fails does **not** stop the scheduler from starting, and does not stop the
        server from serving. Recovery's only failure mode in practice is a database that is not at
        head — no ``runs`` table to read — and refusing to boot for that would take down ``/health``
        and the pages whose whole job is to explain that the database needs migrating
        ([Graceful Degradation](../../../../docs/architecture/graceful-degradation.md)). The loop
        below is already tolerant of the same failure and retries every poll, so a database that
        becomes readable later is picked up without a restart.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            self.recover()
        except Exception:  # noqa: BLE001 — a broken database degrades the queue, never the server
            logger.exception("scheduler.recovery_failed")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="freeweight-scheduler", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 30.0) -> None:
        """Ask the loop to stop after the run in flight, and wait for the thread to exit.

        Does **not** interrupt a run in progress: killing a generation mid-flight would leave the
        sample it was producing neither stored nor honestly failed. A shutdown that outlasts
        ``timeout`` leaves the run in flight, and the next startup's recovery marks it
        ``interrupted`` — which is exactly the situation ``interrupted`` exists to describe, and
        the run stays resumable.

        Safe to call whether or not :meth:`start` was called.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("scheduler.stop_timed_out", extra={"timeout_seconds": timeout})
        self._thread = None

    def is_running(self) -> bool:
        """Whether the scheduler thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def active_run_id(self) -> str | None:
        """The id of the run being executed right now, or ``None``.

        Read from the database rather than from an attribute this thread sets: the answer must be
        the same for a request thread reading it as for the scheduler thread writing it, and a
        status column is the only thing both can see.

        Raises:
            DatabaseUnavailable: The queue could not be read — most often a database that is not
                yet at head. Translated here rather than left as a raw driver error so a caller
                (``GET /api/v1/system/status``) can degrade on the suite's own error type instead
                of importing SQLAlchemy to catch one.
        """
        with _translated(), self._database.read() as session:
            return RunRepository().active_run_id(session)

    def queue_depth(self) -> int:
        """How many runs are waiting to be claimed.

        Raises:
            DatabaseUnavailable: The queue could not be read. See :meth:`active_run_id`.
        """
        with _translated(), self._database.read() as session:
            return RunRepository().queue_depth(session)

    def run_once(self) -> str | None:
        """Claim one queued run, execute it to a terminal state, and return its id.

        The loop body, exposed as one call so a test can drive the scheduler deterministically —
        claim, execute, assert — with no thread, no sleep and no polling. The threaded loop below
        is then only "call this until asked to stop", which is the part with nothing to get wrong.

        Returns:
            The id of the run that was executed, or ``None`` when the queue was empty.
        """
        now = self._clock()
        with self._database.write() as session:
            claimed = RunRepository().claim_next_queued(session, now=now)
            run_id = claimed.id if claimed is not None else None
        if run_id is None:
            return None
        logger.info("run.claimed", extra={"run_id": run_id})
        status = execute_run(
            self._database,
            self._provider,
            self._registry,
            self._publisher,
            run_id,
            collector=self._collector,
            telemetry=self._telemetry,
            settings=self._settings,
            clock=self._clock,
        )
        logger.info("run.finished", extra={"run_id": run_id, "status": status.value})
        return run_id

    def _loop(self) -> None:
        """Claim and execute until stopped; wait only when the queue is empty.

        Any exception escaping :func:`~freeweight.services.runs.execute_run` would end this thread
        and silently stop the queue for the life of the process, so the loop catches everything
        and keeps going. ``execute_run`` already records a failure on the run itself, so this
        handler exists for the case that one *cannot* — a database that has gone away — where the
        right behaviour is to log, wait, and try again rather than to die.
        """
        while not self._stop.is_set():
            try:
                executed = self.run_once()
            except Exception:  # noqa: BLE001 — the scheduler thread must outlive any single run
                logger.exception("scheduler.iteration_failed")
                executed = None
            if executed is None:
                self._stop.wait(self._poll_interval_seconds)
