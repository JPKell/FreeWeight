"""``adapter_catalog``: what ``GET /api/v1/adapters`` answers once something is measured (row WP3).

The join this asserts is the one ADR-0059 forbids getting wrong: the scores measured on an adapter
subject and the bare base's are reported side by side, and neither is ever the other's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.integration.test_adapter_subjects import (
    _ADAPTER_DIGEST,
    _adapter_capable,
    _entry,
    _run_to_completion,
    _start,
    evidence_settings,
)

from freeweight.config import AdapterSettings, EvidenceSettings
from freeweight.services.adapters import adapter_catalog

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import RunEnvironment

__all__ = ["evidence_settings"]  # the fixture, re-used from the adapter-subject tests


def test_an_adapter_measured_and_gone_from_the_directory_is_still_listed_with_its_runs(
    run_environment: Callable[..., RunEnvironment], evidence_settings: EvidenceSettings
) -> None:
    env = run_environment(script=_adapter_capable())
    entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
    _run_to_completion(env, adapter=None, entries=entries, settings=evidence_settings)
    _run_to_completion(env, adapter="terse", entries=entries, settings=evidence_settings)

    body = adapter_catalog(env.database, AdapterSettings())

    assert body["enabled"] is False and body["note"]
    [adapter] = body["adapters"]
    assert adapter["name"] == "terse" and adapter["artifact_sha256"] == _ADAPTER_DIGEST
    assert adapter["in_directory"] is False and adapter["available"] is False
    assert adapter["measured"] is True and adapter["run_count"] == 1
    assert adapter["last_run_at"]
    [subject] = adapter["subjects"]
    assert subject["base"] == env.model_ref
    assert subject["subject"] and subject["subject"] != env.model_ref
    assert subject["measured"], "the adapter subject was measured"
    assert subject["base_measured"], "the bare base was measured"


def test_a_measured_base_gives_an_unmeasured_adapter_subject_nothing(
    run_environment: Callable[..., RunEnvironment], evidence_settings: EvidenceSettings
) -> None:
    """The base's evidence is beside the subject's, never copied into it (ADR-0059)."""
    env = run_environment(script=_adapter_capable())
    entries = (_entry(env, "terse", artifact_digest=_ADAPTER_DIGEST),)
    _run_to_completion(env, adapter=None, entries=entries, settings=evidence_settings)
    _start(env, adapter="terse", entries=entries)  # created, never executed

    [adapter] = adapter_catalog(env.database, AdapterSettings())["adapters"]

    assert adapter["run_count"] == 1
    [subject] = adapter["subjects"]
    assert subject["measured"] == {} and subject["subject"] is None
    assert subject["base_measured"]
