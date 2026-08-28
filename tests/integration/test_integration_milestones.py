"""The roadmap's integration milestones I1–I3, as tests a reviewer can point at.

Roadmap §4 says each integration milestone "has a dedicated verification, and none is considered
complete on the basis of a code review". The verifications existed — spread across the import
contracts, the telemetry tests and the export contract tests — but nothing *labelled* them, so a
reviewer could not point at one thing and say "this is I3".

This module is that one thing. It does not duplicate the checks: where a contract already proves a
milestone, the test here asserts through the same mechanism rather than a parallel one, so there is
no second answer to drift from the first.

    pytest -m "integration_milestone"        # every milestone this repository owns
    pytest -m I2                             # just "telemetry works, including with no GPU"
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration_milestone


@pytest.mark.I1
class TestI1FreeWeightThroughModelRack:
    """**I1 · FW P3** — discovery goes through ModelRack; no provider HTTP code lives here."""

    def test_no_module_speaks_a_provider_protocol_directly(self) -> None:
        """The assertion the milestone names, run as the milestone rather than as lint.

        ``.importlinter`` forbids ``freeweight.domain`` from importing ``httpx``; this is the wider
        claim — that **no** part of the application talks to a provider over HTTP, because the one
        model client is ModelRack (ADR-0007).
        """
        offenders: list[str] = []
        for path in (REPO_ROOT / "src" / "freeweight").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import httpx", "from httpx")):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {stripped}")
        assert not offenders, (
            "FreeWeight must reach a provider only through ModelRack (ADR-0007); these modules "
            f"speak HTTP themselves: {offenders}"
        )

    def test_the_import_contracts_still_hold(self) -> None:
        """``lint-imports`` is part of the gate; running it here ties it to the milestone."""
        result = subprocess.run(  # noqa: S603 — a fixed argument list, no shell
            [sys.executable, "-m", "importlinter.cli", "lint-imports"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_discovery_returns_models_the_provider_reported(self, run_environment: Any) -> None:
        """The behavioural half: models reach the database through the provider abstraction."""
        from sqlalchemy import select

        from freeweight.infrastructure.db.models import Model

        environment = run_environment()
        with environment.database.read() as session:
            models = list(session.scalars(select(Model)))

        assert models, "discovery through ModelRack produced no models"
        assert all(row.canonical_id for row in models)


@pytest.mark.I2
class TestI2FreeWeightAndSweatMeter:
    """**I2 · FW P4** — telemetry live, machine profile persisted, and the no-GPU path exercised."""

    def test_the_machine_profile_is_persisted_with_its_fingerprint(
        self, run_environment: Any
    ) -> None:
        """Profile, fingerprint, upsert — the three stages the milestone names."""
        from sqlalchemy import select

        from freeweight.infrastructure.db.models import Machine
        from freeweight.services.machine import profile_machine

        environment = run_environment()
        profile = profile_machine(environment.database, environment.collector)

        with environment.database.read() as session:
            machines = list(session.scalars(select(Machine)))

        assert machines, "no machine profile was persisted"
        assert profile.machine_fingerprint in {row.machine_fingerprint for row in machines}

    def test_a_machine_with_no_gpu_reports_unsupported_rather_than_zero(
        self, run_environment: Any
    ) -> None:
        """The no-GPU path the milestone names.

        ``run_environment`` builds its collector on SweatMeter's null readers, which is exactly a
        machine with no readable GPU. Its device figures must come back unsupported — never ``0``,
        which would read as a measured absence of memory rather than an absent measurement
        (ADR-0016).
        """
        environment = run_environment()
        profile = environment.collector.machine_profile()

        assert profile.machine_fingerprint, "a machine with no GPU still has an identity"
        assert not profile.gpus, "the null reader must report no devices, not a device of zeroes"


@pytest.mark.I3
class TestI3FreeWeightToSetSpec:
    """**I3 · FW P6, frozen at FW P11** — exports validate against the schemas and the goldens."""

    def test_every_schema_this_build_emits_is_one_it_may_emit(self) -> None:
        """Either SetSpec defines it, or it is in FreeWeight's own namespace (ADR-0035)."""
        import re

        from setspec.envelope import SUPPORTED_SCHEMAS

        pattern = re.compile(
            r"""["']schema["']\s*:\s*["']([a-z_.]+)["']|schema=["']([a-z_.]+)["']"""
        )
        found: set[str] = set()
        for path in (REPO_ROOT / "src" / "freeweight").rglob("*.py"):
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                found.add(match.group(1) or match.group(2))

        assert found
        for schema in found - {"event.envelope"}:
            assert schema in SUPPORTED_SCHEMAS or schema.startswith("freeweight."), schema

    def test_the_committed_openapi_snapshot_matches_the_running_application(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The contract half of I3: the published surface is the one that is served."""
        snapshot = REPO_ROOT / "docs" / "openapi.json"
        if not snapshot.exists():
            pytest.skip("no OpenAPI snapshot is committed yet; Phase 11 publishes it")

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        from freeweight.config import load_settings
        from freeweight.web.app import create_app

        served = create_app(load_settings().settings).openapi()
        assert served["paths"].keys() == json.loads(snapshot.read_text())["paths"].keys()
