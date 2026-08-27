"""freeweight.infrastructure.db.repositories.runs — the only writer of the run-engine tables.

Six repositories, one per table group, in one module because Phase 5's file list names one file
and because they are only meaningful together: a run is created, its tests enumerated, its samples
written and its events appended within one service call, and splitting them across six modules
would buy nothing but imports.

:class:`RuntimeProfileRepository` writes ``runtime_profiles``, which is declared in
:mod:`freeweight.infrastructure.db.models` (Phase 2) rather than
:mod:`freeweight.infrastructure.db.models_runs`. It lives here because Phase 5 is the first phase
with a reason to write that table, and a repository module whose only caller is the run engine
belongs beside the run engine's other repositories.

Repository methods take a session and never open one (database standards §6): the service owns the
transaction boundary. Every return value is a detached ORM instance
(``expire_on_commit=False``), never a live query result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, func, select, update

from freeweight.infrastructure.db.models import RuntimeProfile
from freeweight.infrastructure.db.models_runs import (
    BenchmarkSuite,
    BenchmarkTestRow,
    MetricValue,
    Run,
    RunEvent,
    RunTest,
    Sample,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.orm import Session

__all__ = [
    "BenchmarkRepository",
    "MetricValueRepository",
    "RunEventRepository",
    "RunRepository",
    "RunTestRepository",
    "RuntimeProfileRepository",
    "SampleRepository",
]

_ORPHAN_STATUSES = ("preparing", "warming", "running", "cancelling")
"""Run statuses that can only be held by a *live* process.

A row still carrying one of these at startup was left by a process that died, which is exactly
what ``interrupted`` means (spec §13). ``queued`` is deliberately absent: a queued run has started
nothing, so a restart simply picks it up.
"""


class RuntimeProfileRepository:
    """Reads and writes ``runtime_profiles``, deduplicated by content hash."""

    def get_or_create(
        self,
        session: Session,
        *,
        profile_hash: str,
        context_size: int | None,
        kv_cache_precision: str | None,
        gpu_layers: int | None,
        flash_attention: bool | None,
        threads: int | None,
        batch_size: int | None,
        keep_alive: str | None,
        provider_options_json: Any,
        now: datetime,
    ) -> RuntimeProfile:
        """Return the profile with this hash, inserting it if this is its first use.

        Select-then-insert rather than an ``ON CONFLICT`` upsert, and deliberately so: the row is
        immutable once written (its primary key *is* a hash of its content, so an update could
        only ever write the values already there), which makes the read-then-write race benign —
        the loser of a race finds the winner's row on retry and both callers end up pointing at
        the same profile. :func:`~freeweight.infrastructure.db.upsert.upsert` exists for rows
        whose columns change on a later sighting; this one has none.

        Args:
            session: The caller's active session.
            profile_hash: :attr:`baseaicore.RuntimeProfile.profile_hash` of the profile.
            context_size: Requested context window in tokens, or ``None`` for provider defaults.
            kv_cache_precision: KV-cache quantization, or ``None``.
            gpu_layers: Layers offloaded to GPU, or ``None``.
            flash_attention: Whether flash attention was requested, or ``None``.
            threads: CPU threads requested, or ``None``.
            batch_size: Batch size requested, or ``None``.
            keep_alive: Provider keep-alive string, or ``None``.
            provider_options_json: Provider-specific options, already JSON-safe.
            now: The instant to record as ``created_at`` on first insert.

        Returns:
            The stored profile row.
        """
        existing = session.scalars(
            select(RuntimeProfile).where(RuntimeProfile.profile_hash == profile_hash)
        ).one_or_none()
        if existing is not None:
            return existing
        profile = RuntimeProfile(
            profile_hash=profile_hash,
            context_size=context_size,
            kv_cache_precision=kv_cache_precision,
            gpu_layers=gpu_layers,
            flash_attention=flash_attention,
            threads=threads,
            batch_size=batch_size,
            keep_alive=keep_alive,
            provider_options_json=provider_options_json,
            created_at=now,
        )
        session.add(profile)
        session.flush()
        return profile


class BenchmarkRepository:
    """Reads and writes ``benchmark_suites`` and ``benchmark_tests``."""

    def get_suite(self, session: Session, *, key: str, version: str) -> BenchmarkSuite | None:
        """Return the installed row for one suite version, or ``None``."""
        return session.scalars(
            select(BenchmarkSuite).where(
                BenchmarkSuite.key == key, BenchmarkSuite.version == version
            )
        ).one_or_none()

    def list_suites(self, session: Session) -> list[BenchmarkSuite]:
        """Return every installed suite version, newest key/version first."""
        return list(
            session.scalars(
                select(BenchmarkSuite).order_by(
                    BenchmarkSuite.key.asc(), BenchmarkSuite.version.asc()
                )
            ).all()
        )

    def install_suite(
        self,
        session: Session,
        *,
        key: str,
        name: str,
        version: str,
        category: str | None,
        runner: str,
        manifest_hash: str,
        manifest_json: Any,
        dataset_hashes_json: Any,
        license: str | None,  # noqa: A002 — the column and the manifest field are both `license`
        now: datetime,
    ) -> BenchmarkSuite:
        """Return the row for ``(key, version)``, installing it on first use.

        A suite version is immutable: its ``manifest_hash`` is derived from its content, so a
        changed manifest is a different version and gets its own row. Installing is therefore
        "insert if absent" and never an update — which is what makes a run's ``suite_id`` a
        permanent pointer at exactly what executed.

        Args:
            session: The caller's active session.
            key: The suite key, e.g. ``"native.echo"``.
            name: Human-readable suite name.
            version: The suite version.
            category: Benchmark catalog category, or ``None``.
            runner: ``"native"``, ``"external"`` or ``"goal"``.
            manifest_hash: ``sha256:``-prefixed hash of the manifest body.
            manifest_json: The manifest body, JSON-safe.
            dataset_hashes_json: Pinned dataset hashes, JSON-safe.
            license: The suite's licence.
            now: The instant to record as ``installed_at`` on first insert.

        Returns:
            The stored suite row.
        """
        existing = self.get_suite(session, key=key, version=version)
        if existing is not None:
            return existing
        suite = BenchmarkSuite(
            key=key,
            name=name,
            version=version,
            category=category,
            runner=runner,
            manifest_hash=manifest_hash,
            manifest_json=manifest_json,
            dataset_hashes_json=dataset_hashes_json,
            license=license,
            installed_at=now,
        )
        session.add(suite)
        session.flush()
        return suite

    def install_test(
        self,
        session: Session,
        *,
        suite_id: str,
        key: str,
        name: str,
        category: str | None,
        scorer: str,
        config_json: Any,
        metric_definitions_json: Any,
        requires_json: Any,
    ) -> BenchmarkTestRow:
        """Return the row for ``(suite_id, key)``, installing it on first use.

        Immutable for the same reason :meth:`install_suite` is: the test belongs to a suite
        version, and changing a test changes the suite's manifest hash and therefore its version.
        """
        existing = session.scalars(
            select(BenchmarkTestRow).where(
                BenchmarkTestRow.suite_id == suite_id, BenchmarkTestRow.key == key
            )
        ).one_or_none()
        if existing is not None:
            return existing
        row = BenchmarkTestRow(
            suite_id=suite_id,
            key=key,
            name=name,
            category=category,
            scorer=scorer,
            config_json=config_json,
            metric_definitions_json=metric_definitions_json,
            requires_json=requires_json,
        )
        session.add(row)
        session.flush()
        return row

    def list_tests(self, session: Session, suite_id: str) -> list[BenchmarkTestRow]:
        """Return every test of one suite version, in key order."""
        return list(
            session.scalars(
                select(BenchmarkTestRow)
                .where(BenchmarkTestRow.suite_id == suite_id)
                .order_by(BenchmarkTestRow.key.asc())
            ).all()
        )


class RunRepository:
    """Reads and writes ``runs``, including the scheduler's atomic claim and startup recovery."""

    def insert(
        self,
        session: Session,
        *,
        machine_id: str,
        model_id: str,
        model_descriptor_id: str,
        runtime_profile_id: str,
        suite_id: str,
        status: str,
        effective_config_json: Any,
        reproducibility_fingerprint: str,
        fingerprint_document_json: Any,
        provider_kind: str | None,
        provider_version: str | None,
        application_version: str | None,
        label: str | None,
        now: datetime,
    ) -> Run:
        """Insert one run in its initial state and return it.

        Args:
            session: The caller's active session.
            machine_id: The machine this run will be measured on.
            model_id: The model identity being measured.
            model_descriptor_id: The exact descriptor snapshot the measurement is against.
            runtime_profile_id: The serving parameters the model is run under.
            suite_id: The installed suite version being executed.
            status: The initial status — always ``"queued"`` for a new run.
            effective_config_json: The resolved execution parameters, frozen into the run.
            reproducibility_fingerprint: The fingerprint over this run's inputs.
            fingerprint_document_json: The document the fingerprint was computed from.
            provider_kind: The provider family, for provenance.
            provider_version: The provider's reported version, or ``None`` when it reports none.
            application_version: FreeWeight's own version.
            label: The user's label for this run, or ``None``.
            now: The instant to record as ``created_at``.

        Returns:
            The inserted run.
        """
        run = Run(
            machine_id=machine_id,
            model_id=model_id,
            model_descriptor_id=model_descriptor_id,
            runtime_profile_id=runtime_profile_id,
            suite_id=suite_id,
            status=status,
            created_at=now,
            effective_config_json=effective_config_json,
            reproducibility_fingerprint=reproducibility_fingerprint,
            fingerprint_document_json=fingerprint_document_json,
            provider_kind=provider_kind,
            provider_version=provider_version,
            application_version=application_version,
            label=label,
        )
        session.add(run)
        session.flush()
        return run

    def get_by_id(self, session: Session, run_id: str) -> Run | None:
        """Return one run by primary key, or ``None``."""
        return session.get(Run, run_id)

    def get_by_id_prefix(self, session: Session, prefix: str) -> list[Run]:
        """Return every run whose ULID starts with ``prefix``.

        CLI standards §7: "IDs accept an unambiguous prefix everywhere". Returning the list rather
        than resolving it here lets the service refuse an ambiguous prefix by naming the
        candidates instead of silently picking one.
        """
        return list(
            session.scalars(select(Run).where(Run.id.startswith(prefix)).order_by(Run.id)).all()
        )

    def list_runs(
        self, session: Session, *, status: str | None = None, limit: int = 50
    ) -> list[Run]:
        """Return runs newest-first, optionally filtered by status.

        Ordered by ``(status, created_at DESC)`` when filtered so the query uses
        ``ix_runs_status_created_at`` (data model §5).
        """
        statement = select(Run)
        if status is not None:
            statement = statement.where(Run.status == status)
        return list(
            session.scalars(statement.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit))
            .unique()
            .all()
        )

    def set_status(
        self,
        session: Session,
        run_id: str,
        *,
        status: str,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        error_code: str | None = None,
        error_text: str | None = None,
    ) -> None:
        """Write a run's new status and, where the transition has them, its timestamps.

        The legality of the move is the *service's* business
        (:func:`freeweight.domain.run_state.require_run_transition`), checked against the status
        read in this same transaction. This method only writes.

        Args:
            session: The caller's active session.
            run_id: The run to update.
            status: The new status.
            started_at: Set when moving out of ``queued``; ``None`` leaves the column alone.
            completed_at: Set on reaching a terminal status; ``None`` leaves the column alone.
            error_code: Stable failure code; ``None`` leaves the column alone.
            error_text: Human-readable failure detail; ``None`` leaves the column alone.
        """
        values: dict[str, Any] = {"status": status}
        if started_at is not None:
            values["started_at"] = started_at
        if completed_at is not None:
            values["completed_at"] = completed_at
        if error_code is not None:
            values["error_code"] = error_code
        if error_text is not None:
            values["error_text"] = error_text
        session.execute(update(Run).where(Run.id == run_id).values(**values))

    def claim_next_queued(self, session: Session, *, now: datetime) -> Run | None:
        """Atomically take the oldest queued run and move it to ``preparing``.

        The guard is ``WHERE id = :id AND status = 'queued'``, and the claim is the row count that
        ``UPDATE`` reports. Reading the id first and updating unconditionally would let two
        schedulers claim the same run; the conditional update makes exactly one of them win, and
        the loser sees ``rowcount == 0`` and looks again. It is the same shape ADR-0010's
        database-backed queue describes, without the lease machinery ADR-0029 adds for LoadCoach's
        *distributed* workers — FreeWeight runs one scheduler thread in one process, so a lease
        would be a lock a process takes against itself.

        Refuses to claim while any run is already in flight — including one held by another
        process. That, not a lock, is what makes "one GPU workload at a time; queueing beyond
        that" true when a ``freeweight run start`` is typed into a terminal while a server is
        also serving: the second process finds the slot taken, claims nothing, and leaves the run
        queued for whichever scheduler frees up first. On SQLite the check and the claim share one
        ``BEGIN IMMEDIATE`` transaction, so two processes cannot both see an empty slot.

        Args:
            session: The caller's active session.
            now: The instant to record as ``started_at``.

        Returns:
            The claimed run in its new ``preparing`` state, or ``None`` when the queue is empty,
            another run is already in flight, or another claimer won the race.
        """
        if self.active_run_id(session) is not None:
            return None
        candidate = session.scalars(
            select(Run.id).where(Run.status == "queued").order_by(Run.created_at.asc()).limit(1)
        ).one_or_none()
        if candidate is None:
            return None
        result = session.execute(
            update(Run)
            .where(Run.id == candidate, Run.status == "queued")
            .values(status="preparing", started_at=now)
        )
        # `rowcount` is defined on CursorResult, which is what a DML `session.execute` returns;
        # `Session.execute`'s declared return type is the wider `Result`, hence the narrowing.
        if cast("CursorResult[Any]", result).rowcount != 1:
            return None
        session.flush()
        return session.get(Run, candidate)

    def mark_orphans_interrupted(self, session: Session, *, now: datetime) -> list[str]:
        """Move every run left mid-flight by a dead process to ``interrupted``.

        Startup recovery. Runs in :data:`_ORPHAN_STATUSES` cannot be in those states without a
        live process driving them, so finding one at startup means the previous process died.
        They become ``interrupted`` — not ``failed`` — keeping their completed tests and their
        resumability (spec §13).

        Args:
            session: The caller's active session.
            now: The instant recorded as ``completed_at``, so an interrupted run has an end time
                and its duration is not open-ended forever. Resuming clears it.

        Returns:
            The ids of the runs that were interrupted, in claim order, for the caller to log and
            to emit ``run.interrupted`` events against.
        """
        orphan_ids = list(
            session.scalars(
                select(Run.id).where(Run.status.in_(_ORPHAN_STATUSES)).order_by(Run.created_at)
            ).all()
        )
        if not orphan_ids:
            return []
        session.execute(
            update(Run).where(Run.id.in_(orphan_ids)).values(status="interrupted", completed_at=now)
        )
        return orphan_ids

    def active_run_id(self, session: Session) -> str | None:
        """Return the id of the run currently being executed, or ``None``.

        "Being executed" is any non-``queued``, non-terminal status. There is at most one, because
        one scheduler thread claims one run at a time.
        """
        return session.scalars(
            select(Run.id).where(Run.status.in_(_ORPHAN_STATUSES)).order_by(Run.created_at).limit(1)
        ).one_or_none()

    def queue_depth(self, session: Session) -> int:
        """Return how many runs are waiting to be claimed."""
        return int(
            session.scalar(select(func.count()).select_from(Run).where(Run.status == "queued")) or 0
        )


class RunTestRepository:
    """Reads and writes ``run_tests``."""

    def get_or_create(
        self,
        session: Session,
        *,
        run_id: str,
        test_id: str,
        total_cases: int,
        repetitions: int,
    ) -> RunTest:
        """Return this run's row for ``test_id``, creating it as ``pending`` on first use.

        Idempotent by ``UNIQUE (run_id, test_id)``, which is what makes **resume** work: the
        second preparation of the same run finds the rows the first one wrote, terminal statuses
        and all, instead of enumerating a fresh set and losing the completed tests.
        """
        existing = session.scalars(
            select(RunTest).where(RunTest.run_id == run_id, RunTest.test_id == test_id)
        ).one_or_none()
        if existing is not None:
            return existing
        row = RunTest(
            run_id=run_id,
            test_id=test_id,
            status="pending",
            total_cases=total_cases,
            repetitions=repetitions,
            measurement_class="n/a",
        )
        session.add(row)
        session.flush()
        return row

    def get_by_id(self, session: Session, run_test_id: str) -> RunTest | None:
        """Return one run test by primary key, or ``None``."""
        return session.get(RunTest, run_test_id)

    def list_for_run(self, session: Session, run_id: str) -> list[RunTest]:
        """Return every test of one run, in insertion (ULID) order."""
        return list(
            session.scalars(
                select(RunTest).where(RunTest.run_id == run_id).order_by(RunTest.id.asc())
            ).all()
        )

    def set_status(
        self,
        session: Session,
        run_test_id: str,
        *,
        status: str,
        skip_reason: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        error_code: str | None = None,
        error_text: str | None = None,
    ) -> None:
        """Write a test's new status and the fields that transition carries."""
        values: dict[str, Any] = {"status": status}
        if skip_reason is not None:
            values["skip_reason"] = skip_reason
        if started_at is not None:
            values["started_at"] = started_at
        if completed_at is not None:
            values["completed_at"] = completed_at
        if error_code is not None:
            values["error_code"] = error_code
        if error_text is not None:
            values["error_text"] = error_text
        session.execute(update(RunTest).where(RunTest.id == run_test_id).values(**values))

    def set_completed_cases(self, session: Session, run_test_id: str, *, completed: int) -> None:
        """Record how many of this test's cases have finished every repetition."""
        session.execute(
            update(RunTest).where(RunTest.id == run_test_id).values(completed_cases=completed)
        )


class SampleRepository:
    """Reads and writes ``samples`` — the raw records every headline number drills to."""

    def insert(self, session: Session, **values: Any) -> Sample:
        """Insert one sample and return it.

        Takes ``**values`` rather than thirty named parameters: the column set *is* the data model
        (§2, ``samples``), the caller assembles it in one place
        (:func:`freeweight.services.runs._sample_values`), and restating thirty names here would
        add a second list to keep in step with the first without validating anything the database
        does not already.

        A ``failed``, ``timeout``, ``cancelled`` or ``skipped`` sample must carry ``score=None``;
        ``ck_samples_score_null_unless_completed`` enforces it rather than trusting the caller.
        """
        sample = Sample(**values)
        session.add(sample)
        session.flush()
        return sample

    def existing_keys(self, session: Session, run_test_id: str) -> set[tuple[str, int, int]]:
        """Return the ``(case_id, ordinal, repetition)`` tuples already stored for this test.

        The resume primitive. A test continued after an interruption skips exactly these, so no
        case is measured twice and none is silently skipped — and the natural-key uniqueness
        constraint turns a mistake in that logic into an error rather than a duplicate row.
        """
        rows = session.execute(
            select(Sample.case_id, Sample.ordinal, Sample.repetition).where(
                Sample.run_test_id == run_test_id
            )
        ).all()
        return {(str(row[0]), int(row[1]), int(row[2])) for row in rows}

    def list_for_run_test(
        self, session: Session, run_test_id: str, *, limit: int = 500
    ) -> list[Sample]:
        """Return this test's samples in declaration order (``ix_samples_run_test_id_ordinal``)."""
        return list(
            session.scalars(
                select(Sample)
                .where(Sample.run_test_id == run_test_id)
                .order_by(Sample.ordinal.asc(), Sample.repetition.asc())
                .limit(limit)
            ).all()
        )

    def scores_for_run_test(self, session: Session, run_test_id: str) -> tuple[list[float], int]:
        """Return this test's usable scores and the count of samples excluded from them.

        "Excluded" is every sample whose ``score`` is ``NULL`` — a failure, a timeout, a
        cancellation or a skip. It is returned alongside the scores, not discarded, because the
        exclusion has to remain visible in the sample count (spec §13); an aggregate that reports
        only what it used cannot be distinguished from one that had nothing to exclude.

        Returns:
            ``(scores, excluded_count)``.
        """
        rows = session.execute(select(Sample.score).where(Sample.run_test_id == run_test_id)).all()
        scores = [float(row[0]) for row in rows if row[0] is not None]
        return scores, len(rows) - len(scores)

    def status_counts(self, session: Session, run_test_id: str) -> dict[str, int]:
        """Return this test's sample count by status, for the counts a UI shows beside a score."""
        rows = session.execute(
            select(Sample.status, func.count())
            .where(Sample.run_test_id == run_test_id)
            .group_by(Sample.status)
        ).all()
        return {str(row[0]): int(row[1]) for row in rows}


class MetricValueRepository:
    """Reads and writes ``metric_values``."""

    def replace_for_run(
        self, session: Session, run_id: str, *, rows: Sequence[dict[str, Any]]
    ) -> None:
        """Delete this run's aggregate metrics and write ``rows`` in their place.

        Aggregation is idempotent by construction: a resumed run aggregates again over every
        sample it now has, and re-running it must not leave the previous, partial aggregate beside
        the new one. Delete-then-insert within one transaction is how that stays true without a
        composite natural key on a table whose keys differ by level (run, run_test, sample).

        Only *aggregate* rows are replaced — those with ``sample_id IS NULL``. A per-sample metric
        belongs to its sample and is deleted with it.

        Args:
            session: The caller's active session.
            run_id: The run whose aggregates are being rewritten.
            rows: Column mappings to insert. May be empty, which clears the aggregates.
        """
        session.execute(
            delete(MetricValue).where(MetricValue.run_id == run_id, MetricValue.sample_id.is_(None))
        )
        for row in rows:
            session.add(MetricValue(**row))
        session.flush()

    def list_for_run(self, session: Session, run_id: str) -> list[MetricValue]:
        """Return this run's metric rows, key-ordered (``ix_metric_values_run_id_metric_key``)."""
        return list(
            session.scalars(
                select(MetricValue)
                .where(MetricValue.run_id == run_id)
                .order_by(MetricValue.metric_key.asc(), MetricValue.run_test_id.asc())
            ).all()
        )


class RunEventRepository:
    """Reads and writes ``run_events`` — the source of truth for SSE replay."""

    def next_sequence(self, session: Session, run_id: str) -> int:
        """Return the sequence number the next event for this run must take.

        ``max(sequence) + 1``, or ``1`` for a run with no events yet — which is where "gap-free,
        starting at 1" comes from. Two writers computing this concurrently get the same number and
        one of them loses to ``uq_run_events_run_id_sequence``; the caller retries rather than
        writing a duplicate.
        """
        highest = session.scalar(
            select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
        )
        return int(highest or 0) + 1

    def append(
        self,
        session: Session,
        *,
        run_id: str,
        sequence: int,
        event_type: str,
        message: str | None,
        progress_completed: int | None,
        progress_total: int | None,
        data_json: Any,
        now: datetime,
    ) -> RunEvent:
        """Insert one event at ``sequence``.

        Raises:
            sqlalchemy.exc.IntegrityError: ``sequence`` is already taken for this run. Not caught
                here: the retry belongs to the publisher, which is the component that knows how to
                recompute a sequence and try again.
        """
        event = RunEvent(
            run_id=run_id,
            sequence=sequence,
            timestamp=now,
            event_type=event_type,
            message=message,
            progress_completed=progress_completed,
            progress_total=progress_total,
            data_json=data_json,
        )
        session.add(event)
        session.flush()
        return event

    def list_since(
        self, session: Session, run_id: str, *, after_sequence: int = 0, limit: int = 200
    ) -> list[RunEvent]:
        """Return this run's events after ``after_sequence``, oldest first.

        The one read SSE replay and the live stream both use — a client that reconnects with
        ``Last-Event-ID: 42`` and a client that has just caught up ask exactly the same question,
        which is why there is one method and no separate "replay" path to drift out of step. Uses
        ``ix_run_events_run_id_sequence`` (data model §5).

        Args:
            session: The caller's active session.
            run_id: The run to read.
            after_sequence: Return events strictly after this sequence. ``0`` returns from the
                beginning, which is what a fresh subscriber wants.
            limit: Maximum events per call. Bounds one batch, so a client that has fallen a long
                way behind catches up over several reads instead of materializing an entire run's
                history in one.

        Returns:
            The events, ascending by sequence.
        """
        return list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.sequence > after_sequence)
                .order_by(RunEvent.sequence.asc())
                .limit(limit)
            ).all()
        )

    def count_for_run(self, session: Session, run_id: str) -> int:
        """Return how many events this run has emitted."""
        return int(
            session.scalar(
                select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)
            )
            or 0
        )
