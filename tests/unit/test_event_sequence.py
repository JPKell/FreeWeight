"""Unit tests for the run event store's sequence and wire format (development plan, Phase 5).

The phase asks for "event sequence gap-free from 1" and, in the acceptance criteria, for a replay
that has "no gap and no duplicate". This module covers the store's half of that: sequences, the
uniqueness constraint that defends them under concurrency, the ``after_sequence`` read that replay
is built on, and the frame shape API standards §8 fixes. The *stream* half — a live subscriber
reconnecting mid-run — is ``tests/integration/test_sse_replay.py``.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.conftest import RunEnvironment

from freeweight.services.database import Database
from freeweight.services.events import (
    RUN_EVENT_TYPES,
    RunEventPublisher,
    event_to_json,
    format_event_frame,
    format_heartbeat,
    read_events,
)
from freeweight.services.runs import ExecutionConfig, create_run


def _queued_run(environment: RunEnvironment) -> str:
    """Create one queued run and return its id — the parent every event needs."""
    from freeweight.config import ExecutionSettings

    summary = create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=ExecutionConfig.resolve(ExecutionSettings()),
    )
    return summary.id


@pytest.fixture
def environment(run_environment: Callable[..., RunEnvironment]) -> RunEnvironment:
    return run_environment()


class TestSequenceAllocation:
    def test_first_event_is_sequence_one(self, environment: RunEnvironment) -> None:
        run_id = _queued_run(environment)
        stored = environment.publisher.publish(run_id, "run.started", message="go")
        assert stored.sequence == 1

    def test_sequences_are_gap_free_and_ascending(self, environment: RunEnvironment) -> None:
        run_id = _queued_run(environment)
        for index in range(20):
            environment.publisher.publish(
                run_id, "run.progress", message=f"step {index}", progress=(index, 20)
            )
        events = read_events(environment.database, run_id)
        assert [event.sequence for event in events] == list(range(1, 21))

    def test_two_runs_have_independent_sequences(self, environment: RunEnvironment) -> None:
        first = _queued_run(environment)
        second = _queued_run(environment)
        environment.publisher.publish(first, "run.started")
        environment.publisher.publish(second, "run.started")
        environment.publisher.publish(first, "run.progress")
        assert [event.sequence for event in read_events(environment.database, first)] == [1, 2]
        assert [event.sequence for event in read_events(environment.database, second)] == [1]

    def test_concurrent_publishers_never_duplicate_a_sequence(
        self, environment: RunEnvironment
    ) -> None:
        """Eight threads appending at once still produce 1..N with no gap and no repeat.

        This is the constraint doing the work, not the retry: without
        ``uq_run_events_run_id_sequence`` two threads computing ``max(sequence) + 1`` would both
        write the same id and a client could not reassemble the stream.
        """
        run_id = _queued_run(environment)
        publisher = RunEventPublisher(environment.database)
        errors: list[BaseException] = []

        def append() -> None:
            try:
                for _ in range(5):
                    publisher.publish(run_id, "sample.completed")
            except BaseException as exc:  # noqa: BLE001 — re-raised in the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=append) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        sequences = [
            event.sequence for event in read_events(environment.database, run_id, limit=1000)
        ]
        assert sequences == list(range(1, 41))


class TestReadSince:
    def test_after_sequence_returns_only_what_follows(self, environment: RunEnvironment) -> None:
        run_id = _queued_run(environment)
        for _ in range(10):
            environment.publisher.publish(run_id, "sample.completed")
        tail = read_events(environment.database, run_id, after_sequence=7)
        assert [event.sequence for event in tail] == [8, 9, 10]

    def test_reading_past_the_end_returns_nothing(self, environment: RunEnvironment) -> None:
        run_id = _queued_run(environment)
        environment.publisher.publish(run_id, "run.started")
        assert read_events(environment.database, run_id, after_sequence=99) == ()

    def test_limit_bounds_a_batch_without_losing_anything(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queued_run(environment)
        for _ in range(10):
            environment.publisher.publish(run_id, "sample.completed")
        first = read_events(environment.database, run_id, limit=4)
        second = read_events(
            environment.database, run_id, after_sequence=first[-1].sequence, limit=4
        )
        third = read_events(
            environment.database, run_id, after_sequence=second[-1].sequence, limit=4
        )
        assert [event.sequence for event in (*first, *second, *third)] == list(range(1, 11))


class TestVocabularyAndPayload:
    def test_an_undeclared_event_type_is_refused(self, environment: RunEnvironment) -> None:
        run_id = _queued_run(environment)
        with pytest.raises(ValueError, match="not a declared run event type"):
            environment.publisher.publish(run_id, "run.exploded")

    @pytest.mark.parametrize("event_type", sorted(RUN_EVENT_TYPES))
    def test_every_declared_type_is_publishable(
        self, environment: RunEnvironment, event_type: str
    ) -> None:
        run_id = _queued_run(environment)
        stored = environment.publisher.publish(run_id, event_type)
        assert stored.event_type == event_type

    def test_progress_is_absent_rather_than_zero_when_an_event_carries_none(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queued_run(environment)
        stored = environment.publisher.publish(run_id, "run.started")
        assert stored.progress_completed is None
        assert stored.progress_total is None
        assert "progress" not in _payload(format_event_frame(stored))

    def test_terminal_events_are_marked_terminal(self, environment: RunEnvironment) -> None:
        run_id = _queued_run(environment)
        for event_type in ("run.completed", "run.failed", "run.cancelled", "run.interrupted"):
            stored = environment.publisher.publish(run_id, event_type)
            assert stored.is_terminal
        assert not environment.publisher.publish(run_id, "run.progress").is_terminal


def _payload(frame: str) -> dict[str, Any]:
    """Extract the ``payload`` object from one rendered SSE frame."""
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    envelope = json.loads(data_line.removeprefix("data: "))
    return dict(envelope["payload"])


class TestFrameFormat:
    def test_frame_has_id_event_and_data_lines_and_a_blank_terminator(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queued_run(environment)
        stored = environment.publisher.publish(
            run_id, "sample.completed", message="one", progress=(1, 4), data={"score": 1.0}
        )
        frame = format_event_frame(stored)
        lines = frame.split("\n")
        assert lines[0] == f"id: {stored.sequence}"
        assert lines[1] == "event: sample.completed"
        assert lines[2].startswith("data: ")
        assert frame.endswith("\n\n")

    def test_envelope_fields_are_siblings_of_payload_not_mixed_into_it(
        self, environment: RunEnvironment
    ) -> None:
        """ADR-0025 §3: a flat frame in which the two mix is a defect, not a style choice."""
        run_id = _queued_run(environment)
        stored = environment.publisher.publish(run_id, "run.started", message="go")
        data_line = next(
            line for line in format_event_frame(stored).splitlines() if line.startswith("data: ")
        )
        envelope = json.loads(data_line.removeprefix("data: "))
        assert envelope["schema"] == "event.envelope"
        assert envelope["schema_version"] == "1.0"
        assert envelope["generator"]["name"] == "freeweight"
        assert set(envelope["payload"]) & {"schema", "schema_version", "generator"} == set()

    def test_payload_carries_the_entity_progress_and_data(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queued_run(environment)
        stored = environment.publisher.publish(
            run_id, "sample.completed", message="one", progress=(3, 12), data={"score": 0.5}
        )
        payload = _payload(format_event_frame(stored))
        assert payload["entity"] == {"kind": "run", "id": run_id}
        assert payload["sequence"] == stored.sequence
        assert payload["type"] == "sample.completed"
        assert payload["progress"] == {"completed": 3, "total": 12}
        assert payload["data"] == {"score": 0.5}

    def test_the_frame_is_byte_identical_for_a_fixed_clock(
        self, environment: RunEnvironment
    ) -> None:
        run_id = _queued_run(environment)
        stored = environment.publisher.publish(run_id, "run.started", message="go")
        fixed = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)
        assert format_event_frame(stored, clock=lambda: fixed) == format_event_frame(
            stored, clock=lambda: fixed
        )

    def test_heartbeat_is_an_sse_comment(self) -> None:
        fixed = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)
        assert format_heartbeat(clock=lambda: fixed) == ": heartbeat 2026-08-27T09:00:00.000Z\n\n"

    def test_cli_json_is_the_same_fields_unwrapped(self, environment: RunEnvironment) -> None:
        run_id = _queued_run(environment)
        stored = environment.publisher.publish(
            run_id, "sample.failed", message="boom", progress=(1, 2)
        )
        body = event_to_json(stored)
        assert body["type"] == "sample.failed"
        assert body["run_id"] == run_id
        assert body["progress_completed"] == 1
        assert "schema" not in body


class TestRestartContinuity:
    def test_sequence_continues_after_the_handle_is_reopened(
        self, environment: RunEnvironment
    ) -> None:
        """A restart does not restart the sequence — the store, not memory, holds the counter."""
        run_id = _queued_run(environment)
        for _ in range(3):
            environment.publisher.publish(run_id, "sample.completed")
        environment.database.close()

        with Database.from_url(environment.database_url) as reopened:
            publisher = RunEventPublisher(reopened)
            stored = publisher.publish(run_id, "run.interrupted", message="restarted")
            assert stored.sequence == 4
            assert [event.sequence for event in read_events(reopened, run_id)] == [1, 2, 3, 4]


class TestLoggingSideEffect:
    """Publishing writes an INFO log record, and that must not be able to fail.

    Observability standards §4: "every domain event that matters to a user is also logged at INFO
    with the same event name". The log call is therefore on the hot path of every run, and it runs
    only when the logger is *enabled* for INFO — which the default test configuration is not, and
    a configured server is. A structured field colliding with one of ``LogRecord``'s own attributes
    raises there and nowhere else, so this test raises the level deliberately.
    """

    def test_publishing_with_info_logging_enabled_does_not_raise(
        self, environment: RunEnvironment, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_id = _queued_run(environment)
        with caplog.at_level(logging.INFO, logger="freeweight.services.events"):
            stored = environment.publisher.publish(
                run_id, "sample.completed", message="one done", progress=(1, 4)
            )
        assert stored.sequence == 1
        record = next(r for r in caplog.records if r.name == "freeweight.services.events")
        assert record.getMessage() == "sample.completed"
        # `extra=` fields land in the record's __dict__ rather than on its declared type.
        assert record.__dict__["run_id"] == run_id
        assert record.__dict__["event_message"] == "one done"

    @pytest.mark.parametrize("event_type", sorted(RUN_EVENT_TYPES))
    def test_every_event_type_logs_cleanly(
        self, environment: RunEnvironment, caplog: pytest.LogCaptureFixture, event_type: str
    ) -> None:
        run_id = _queued_run(environment)
        with caplog.at_level(logging.INFO, logger="freeweight.services.events"):
            environment.publisher.publish(run_id, event_type, message=f"{event_type} happened")
