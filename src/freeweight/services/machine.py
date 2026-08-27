"""freeweight.services.machine — identify this host once per process and persist it.

Called once, at server startup (:func:`freeweight.bootstrap.bootstrap`), never per-request: a
machine's identity does not change within a process's lifetime, and re-profiling on every request
would mean every page load pays SweatMeter's static-collection cost — reading ``/proc/cpuinfo``,
enumerating GPUs — for nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from baseaicore import canonical_json, is_supported, utc_now

from freeweight.infrastructure.db.repositories.machines import MachineRepository

if TYPE_CHECKING:
    from baseaicore import GpuProfile, MachineProfile, Measurement, StorageDevice
    from baseaicore.timeutil import Clock
    from sweatmeter import TelemetryCollector

    from freeweight.services.database import Database

__all__ = ["profile_machine"]


def _optional_int(value: Measurement) -> int | None:
    """Collapse a ``Measurement`` to a plain ``int``, treating ``UNSUPPORTED`` as absent.

    The ``machines`` table keeps plain nullable columns rather than a measurement/reason pair
    (data model §2): unlike a GPU's VRAM, a host reporting no core count is not a case this schema
    needs to distinguish from "not yet collected".
    """
    return int(value) if is_supported(value) else None


def _canonical_dict(value: object) -> dict[str, Any]:
    """Round-trip a structure through canonical JSON into a plain, JSON-safe ``dict``.

    ``json.loads`` is typed to return ``Any``; the cast is safe because :func:`canonical_json`
    only ever produces a JSON object at the top level for the mapping inputs this module passes it.
    """
    return cast("dict[str, Any]", json.loads(canonical_json(value)))


def _gpu_json(gpu: GpuProfile) -> dict[str, Any]:
    """Render one GPU's static identity as JSON, ``UNSUPPORTED`` fields included honestly."""
    return _canonical_dict(
        {
            "index": gpu.index,
            "name": gpu.name,
            "uuid": gpu.uuid,
            "vram_total_bytes": gpu.vram_total_bytes,
            "driver_version": gpu.driver_version,
            "cuda_version": gpu.cuda_version,
            "compute_capability": gpu.compute_capability,
            "vendor": gpu.vendor,
        }
    )


def _storage_json(device: StorageDevice) -> dict[str, Any]:
    """Render one storage device as JSON, ``UNSUPPORTED`` fields included honestly."""
    return _canonical_dict(
        {
            "name": device.name,
            "size_bytes": device.size_bytes,
            "model": device.model,
            "rotational": device.rotational,
        }
    )


def profile_machine(
    database: Database, collector: TelemetryCollector, *, clock: Clock = utc_now
) -> MachineProfile:
    """Profile this host and upsert its identity, refreshing ``last_seen_at``.

    The three stages the phase names — profile, fingerprint, upsert — are two calls: SweatMeter's
    :meth:`~sweatmeter.TelemetryCollector.machine_profile` collects the static facts *and* computes
    their fingerprint in one non-raising call, so this function only has to persist what it
    returns.

    Args:
        database: The application's database handle.
        collector: The SweatMeter collector to profile from. ``machine_profile()`` never raises
            (SweatMeter spec §11.1): a collector that cannot read this platform returns an
            all-``UNSUPPORTED`` profile rather than failing startup.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The profile that was persisted — SweatMeter's own :class:`~baseaicore.MachineProfile`,
        unchanged.
    """
    profile = collector.machine_profile()
    now = clock()
    with database.write() as session:
        MachineRepository().upsert(
            session,
            machine_fingerprint=profile.machine_fingerprint,
            hostname=profile.hostname,
            os_name=profile.os_name,
            os_version=profile.os_version,
            kernel=profile.kernel,
            architecture=profile.architecture,
            cpu_model=profile.cpu_model,
            physical_cores=_optional_int(profile.physical_cores),
            logical_cores=_optional_int(profile.logical_cores),
            ram_bytes=_optional_int(profile.ram_bytes),
            gpus_json=[_gpu_json(gpu) for gpu in profile.gpus],
            storage_json=[_storage_json(device) for device in profile.storage],
            python_version=profile.python_version,
            now=now,
        )
    return profile
