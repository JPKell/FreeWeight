"""Drafting a manifest for an unmanifested artifact (row WX7, ADR-0145).

A draft is a proposal. Every test here is about what stays true after one is written: nothing is
registered, nothing is overwritten, and the fields FreeWeight cannot prove are marked unproven.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from baseaicore import DataClassification, IdentityConfidence, SuiteError

from freeweight.config import AdapterSettings
from freeweight.infrastructure.adapters import read_directory
from freeweight.services.adapters import AdaptersDisabled, DraftRefused, draft_manifest

if TYPE_CHECKING:
    from pathlib import Path


def _settings(directory: Path) -> AdapterSettings:
    return AdapterSettings(directory=str(directory))


def test_a_draft_records_the_artifacts_own_digest(tmp_path: Path) -> None:
    """Identity is the content hash (ADR-0061 rule 5), and it is the one thing FreeWeight knows."""
    (tmp_path / "terse.gguf").write_bytes(b"lora-weights")

    path, payload = draft_manifest(_settings(tmp_path), "terse", base_model_name="qwen3:8b")

    assert path.endswith("terse.manifest.draft.json")
    assert payload["artifact_sha256"] == "sha256:" + hashlib.sha256(b"lora-weights").hexdigest()
    assert payload["artifact_file"] == "terse.gguf"
    assert json.loads((tmp_path / "terse.manifest.draft.json").read_text())["payload"] == payload


def test_the_base_is_named_never_proven(tmp_path: Path) -> None:
    """A digest nobody verified is the misattribution ADR-0061 exists to prevent."""
    (tmp_path / "terse.gguf").write_bytes(b"lora-weights")

    _, payload = draft_manifest(_settings(tmp_path), "terse", base_model_name="qwen3:8b")

    assert payload["base"]["identity_confidence"] == IdentityConfidence.NAME_ONLY.value
    assert "artifact_digest" not in payload["base"]
    assert payload["data_classification"] == DataClassification.CONFIDENTIAL.value


def test_a_draft_registers_nothing(tmp_path: Path) -> None:
    """The suffix keeps it out of the reading's entries — that is the whole enforcement."""
    (tmp_path / "terse.gguf").write_bytes(b"lora-weights")

    draft_manifest(_settings(tmp_path), "terse", base_model_name="qwen3:8b")

    reading = read_directory(tmp_path)
    assert reading.entries == ()
    assert [path.name for path in reading.drafts] == ["terse.manifest.draft.json"]
    assert reading.unmanifested == ()


def test_an_existing_draft_is_never_overwritten(tmp_path: Path) -> None:
    """What a second draft would destroy is a person's review of the first."""
    (tmp_path / "terse.gguf").write_bytes(b"lora-weights")
    draft_manifest(_settings(tmp_path), "terse", base_model_name="qwen3:8b")

    with pytest.raises(DraftRefused):
        draft_manifest(_settings(tmp_path), "terse", base_model_name="qwen3:8b")


def test_an_artifact_that_is_not_there_is_refused(tmp_path: Path) -> None:
    """A draft for nothing would be a manifest for a file nobody can serve."""
    with pytest.raises(DraftRefused):
        draft_manifest(_settings(tmp_path), "absent", base_model_name="qwen3:8b")


@pytest.mark.parametrize("name", ["../escape", "sub/terse", ".", ""])
def test_a_name_that_is_not_one_segment_is_refused(tmp_path: Path, name: str) -> None:
    """The name is a file stem inside the directory, and is never resolved as a path."""
    (tmp_path / "terse.gguf").write_bytes(b"lora-weights")

    with pytest.raises(DraftRefused):
        draft_manifest(_settings(tmp_path), name, base_model_name="qwen3:8b")


def test_adapters_off_is_refused_by_name(tmp_path: Path) -> None:
    """Empty means off (ADR-0061 rule 2): there is nowhere to write, and the key is named."""
    with pytest.raises(AdaptersDisabled) as raised:
        draft_manifest(AdapterSettings(directory=""), "terse", base_model_name="qwen3:8b")

    assert isinstance(raised.value, SuiteError)
    assert "adapters" in raised.value.message.lower()
