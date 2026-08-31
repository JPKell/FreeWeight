"""Unit tests for freeweight.services.machine: profile → fingerprint → upsert → last_seen_at.

Phase 4's own test list (development plan): the machine profile is stored once per fingerprint,
and ``last_seen_at`` is updated on a later sighting without moving ``first_seen_at``. Every test
runs against SweatMeter's own scripted readers (``sweatmeter.testing``), never the real platform.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from baseaicore import GpuVendor
from sweatmeter import GpuSample, TelemetryCollector
from sweatmeter.testing import HostReading, NullGpuReader, ScriptedGpuReader, ScriptedHostReader
from sweatmeter.types import HostFacts
from weightsdb import MigrationRunner, create_engine_for

from freeweight.infrastructure.db.repositories.machines import MachineRepository
from freeweight.services.database import MIGRATIONS_LOCATION, Database
from freeweight.services.inventory import list_machines
from freeweight.services.machine import profile_machine

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 27, 13, 0, 0, tzinfo=UTC)

_FULL_FACTS = HostFacts(
    hostname="bench-01",
    os_name="Linux",
    os_version="Ubuntu 26.04 LTS",
    kernel="6.9.0",
    architecture="x86_64",
    cpu_model="AMD Ryzen 9 9950X",
    physical_cores=16,
    logical_cores=32,
    ram_bytes=64 * 1024**3,
    python_version="3.13.0",
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    """A migrated, throwaway SQLite database — real storage, no provider (testing standards §7)."""
    engine = create_engine_for(f"sqlite:///{tmp_path / 'freeweight.sqlite3'}")
    MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    handle = Database(engine)
    try:
        yield handle
    finally:
        handle.close()


def _collector(*readings: HostReading) -> TelemetryCollector:
    return TelemetryCollector(host=ScriptedHostReader(readings), gpu=NullGpuReader())


def test_profile_is_persisted_on_first_call(database: Database) -> None:
    profile = profile_machine(
        database, _collector(HostReading(static_facts=_FULL_FACTS)), clock=lambda: NOW
    )

    (machine,) = list_machines(database)
    assert machine.machine_fingerprint == profile.machine_fingerprint
    assert machine.hostname == "bench-01"
    assert machine.cpu_model == "AMD Ryzen 9 9950X"
    assert machine.logical_cores == 32
    assert machine.ram_bytes == 64 * 1024**3
    assert machine.first_seen_at == NOW
    assert machine.last_seen_at == NOW


def test_second_sighting_updates_last_seen_at_but_not_first_seen_at(database: Database) -> None:
    collector = _collector(
        HostReading(static_facts=_FULL_FACTS), HostReading(static_facts=_FULL_FACTS)
    )

    profile_machine(database, collector, clock=lambda: NOW)
    profile_machine(database, collector, clock=lambda: LATER)

    machines = list_machines(database)
    assert len(machines) == 1
    (machine,) = machines
    assert machine.first_seen_at == NOW
    assert machine.last_seen_at == LATER


def test_unreported_core_and_ram_counts_are_stored_as_null_not_zero(database: Database) -> None:
    profile = profile_machine(
        database, _collector(HostReading(static_facts=HostFacts())), clock=lambda: NOW
    )

    with database.read() as session:
        machine = MachineRepository().get_by_fingerprint(session, profile.machine_fingerprint)
        assert machine is not None
        assert machine.physical_cores is None
        assert machine.logical_cores is None
        assert machine.ram_bytes is None


def test_gpu_and_storage_are_recorded_as_unsupported_strings_not_null(database: Database) -> None:
    gpu_sample = GpuSample(index=0, uuid="GPU-abc123")
    collector = TelemetryCollector(
        host=ScriptedHostReader([HostReading(static_facts=_FULL_FACTS)]),
        gpu=ScriptedGpuReader([(gpu_sample,)]),
    )

    profile_machine(database, collector, clock=lambda: NOW)
    fingerprint = list_machines(database)[0].machine_fingerprint

    with database.read() as session:
        machine = MachineRepository().get_by_fingerprint(session, fingerprint)
        assert machine is not None
        (gpu_json,) = cast("list[dict[str, Any]]", machine.gpus_json)
        assert gpu_json["uuid"] == "GPU-abc123"
        assert gpu_json["vram_total_bytes"] == "unsupported"
        assert gpu_json["vendor"] == GpuVendor.UNKNOWN.value
