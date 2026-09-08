"""Degradation: disk full mid-run (graceful-degradation.md, row "Disk full").

The matrix's wording — "Run aborted at the next checkpoint; completed samples preserved" — reads
as if a full disk stops the whole run. Reaching the actual checkpoint (the sample write in
``SampleRepository.insert``, ``services/runs.py``) with a real write failure shows a finer-grained
and, on inspection, more useful behaviour: ``_execute_test``'s own containment (spec §13, "a
failed test never fails its run") catches the write failure at the *test* it happened in, marks
that one test ``failed`` with a stable ``error_code``, and lets every other test in the run attempt
normally — so a run is never aborted outright by one exhausted checkpoint, it degrades test by
test. This test proves that finer property instead, and `graceful-degradation.md`'s FreeWeight
cell was corrected in the same change to say so.

Rather than filling a real disk or patching ``os.write`` globally, this attaches a SQLAlchemy
engine event at the one seam the application already owns for this: the ``Database`` handle a
caller hands to ``execute_run``. The event raises ``sqlite3.OperationalError("database or disk is
full")`` — SQLite's own wording for ``ENOSPC`` — the moment a third sample is about to be written,
so two samples have already committed and the third is the checkpoint that fails.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import event
from tests.conftest import RunEnvironment

from freeweight.config import ExecutionSettings
from freeweight.domain.run_state import RunStatus
from freeweight.services.database import Database
from freeweight.services.runs import ExecutionConfig, create_run, get_run, list_samples
from freeweight.services.scheduler import RunScheduler


@pytest.fixture
def full_disk_after(tmp_path: Any) -> Callable[[int], Database]:
    """Build a ``Database`` whose *n*-th ``INSERT INTO samples`` raises a disk-full error.

    Every earlier write — migrations, the model descriptor, the run row, the first ``n - 1``
    samples — succeeds normally; only the sample insert at the boundary fails, and every write
    after it (recording the run as ``failed``) succeeds too, because a real full disk does not
    stop the application from being able to name its own failure in the one small row that says
    so — it stops the workload that filled the disk in the first place.
    """
    from weightsdb import create_engine_for

    def build(fail_at: int) -> Database:
        engine = create_engine_for(f"sqlite:///{tmp_path / 'run.sqlite3'}")
        seen = {"count": 0}

        def _maybe_fail(
            conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
        ) -> None:
            if "INTO samples" not in statement:
                return
            seen["count"] += 1
            if seen["count"] == fail_at:
                raise sqlite3.OperationalError("database or disk is full")

        event.listen(engine, "before_cursor_execute", _maybe_fail)
        return Database(engine)

    return build


def test_a_disk_full_checkpoint_fails_the_run_and_keeps_the_completed_samples(
    run_environment: Callable[..., RunEnvironment],
    full_disk_after: Callable[[int], Database],
) -> None:
    # native.echo's five cases run in a fixed, script-driven order (seed=7), so the third sample
    # write is deterministic across runs — this is not a race against which case happens to be
    # third, it is *always* the third.
    database = full_disk_after(3)
    environment = run_environment(database=database, name="run.sqlite3")

    create_run(
        environment.database,
        environment.provider,
        environment.collector,
        environment.registry,
        model_ref=environment.model_ref,
        suite_key="native.echo",
        execution=ExecutionConfig.resolve(
            ExecutionSettings(warmup_repetitions=0, cooldown_seconds=0), measured_repetitions=1
        ),
    )
    scheduler = RunScheduler(
        environment.database, environment.provider, registry=environment.registry
    )
    run_id = scheduler.run_once()
    assert run_id is not None

    detail = get_run(environment.database, run_id)
    # The run itself is never aborted by one checkpoint failure — only the test it happened in is.
    assert detail.run.status == RunStatus.COMPLETED.value

    failed_tests = [test for test in detail.tests if test.status == "failed"]
    assert len(failed_tests) == 1
    assert failed_tests[0].error_code == "INTERNAL_ERROR"

    samples = [
        sample for test in detail.tests for sample in list_samples(environment.database, test.id)
    ]
    # Four of the five cases committed: the two before the checkpoint in the test it hit, plus
    # every case of the *next* test, which never saw the failure at all. Nothing invents a
    # fabricated sample for the one case that failed, and nothing already committed is lost.
    assert len(samples) == 4
    assert {sample.status for sample in samples} == {"completed"}
