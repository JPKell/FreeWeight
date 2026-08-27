"""The prompt library: loading, validation, rendering and — above all — hashing.

Prompt Management Standards §7's table, and the Phase 6 assertion that motivates the whole design:
"Editing a prompt the suite does not use leaves its fingerprint unchanged; editing one it does use
changes it" (:class:`TestSubsetHashing`). That is
[ADR-0028 §1](../../../docs/adr/0028-prompt-pack-granularity.md) made testable — a fingerprint
that changes for unrelated reasons is almost as damaging as one that fails to change.

The hashes here cross an application boundary inside ``capability.evidence``, so their determinism
is a contract rather than an implementation detail (ADR-0028 §3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from freeweight.services.prompts import (
    PACK_ROOT,
    PromptNotFound,
    PromptPackInvalid,
    PromptRenderError,
    PromptVariableError,
    load_pack,
    pack_hash,
    prompt_record_hash,
    prompt_subset_hash,
)

_RECORD: dict[str, Any] = {
    "prompt_id": "benchmarks.example.probe",
    "version": "1.0.0",
    "schema_version": "1.0",
    "purpose": "Exercise the loader.",
    "task": "benchmark.example",
    "capability": "speed",
    "system": None,
    "template": "{{ subject }}",
    "variables": {
        "subject": {"type": "string", "required": True, "description": "What to talk about."}
    },
    "response": {"format": "text", "json_schema_ref": None, "expectations": []},
    "model_requirements": {
        "min_context_tokens": 512,
        "requires_capabilities": [],
        "recommended_temperature": 0.0,
    },
    "metadata": {
        "author": "test",
        "created_at": "2026-08-27T00:00:00Z",
        "changed_at": "2026-08-27T00:00:00Z",
        "change_reason": "First version.",
        "supersedes": None,
        "tags": [],
    },
}


def _write_pack(root: Path, records: list[dict[str, Any]]) -> Path:
    """Write a pack with a *correct* manifest and return its root."""
    root.mkdir(parents=True, exist_ok=True)
    references = []
    for index, record in enumerate(records):
        path = root / f"record{index}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        references.append(
            {
                "prompt_id": record["prompt_id"],
                "version": record["version"],
                "sha256": prompt_record_hash(record),
            }
        )
    manifest = {
        "pack_id": "test.pack",
        "pack_version": "1.0.0",
        "schema_version": "1.0",
        "generated_at": "2026-08-27T00:00:00Z",
        "prompts": references,
        "pack_sha256": pack_hash(
            type(
                "R",
                (),
                {"prompt_id": r["prompt_id"], "version": r["version"], "sha256": r["sha256"]},
            )()
            for r in references
        ),
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(json.dumps(_RECORD))
    record.update(overrides)
    return record


class TestTheShippedPack:
    """This build's own pack, which every benchmark and every fingerprint depends on."""

    def test_it_parses_and_its_manifest_is_current(self) -> None:
        # Prompt standards §7: "Manifest is current — recomputed hashes match the committed
        # manifest." A record edited without regenerating the manifest fails here, which is what
        # stops a stale hash reaching a benchmark's provenance.
        library = load_pack()
        assert library.pack_id == "freeweight.core"
        assert library.ids()

    def test_every_record_renders_with_its_documented_variables(self) -> None:
        library = load_pack()
        for record in library.all_records():
            variables = {
                name: _example_value(spec.type_name) for name, spec in record.variables.items()
            }
            rendered = record.render(variables)
            assert rendered.user
            assert rendered.rendered_sha256.startswith("sha256:")

    def test_rendering_is_byte_identical_twice(self) -> None:
        library = load_pack()
        record = library.get(library.ids()[0])
        variables = {
            name: _example_value(spec.type_name) for name, spec in record.variables.items()
        }
        assert record.render(variables) == record.render(variables)

    def test_no_prompt_lives_in_python_source(self) -> None:
        # Prompt standards §1 and §7's "No inline prompts". The benchmark modules render from the
        # pack; a multi-line instruction string appearing beside them would be a prompt nobody can
        # version, diff or attach to a result.
        for module in (PACK_ROOT.parent / "benchmarks").rglob("*.py"):
            text = module.read_text(encoding="utf-8")
            assert "You are a" not in text, f"{module} looks like it embeds a system prompt"


def _example_value(type_name: str) -> Any:
    return {"string": "example", "integer": 1, "number": 1.0, "boolean": True}[type_name]


class TestRecordValidation:
    """A malformed record is a startup failure, never a surprise mid-run (prompt standards §5)."""

    def test_a_missing_required_field_is_refused(self, tmp_path: Path) -> None:
        record = _record()
        del record["template"]
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        (tmp_path / "r.json").write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(PromptPackInvalid, match="missing required field 'template'"):
            load_pack(tmp_path)

    def test_an_unknown_record_schema_version_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        (tmp_path / "r.json").write_text(
            json.dumps(_record(schema_version="2.0")), encoding="utf-8"
        )
        with pytest.raises(PromptPackInvalid, match="record schema '2.0'"):
            load_pack(tmp_path)

    def test_a_missing_change_reason_is_refused(self, tmp_path: Path) -> None:
        record = _record()
        record["metadata"] = {**record["metadata"], "change_reason": ""}
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        (tmp_path / "r.json").write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(PromptPackInvalid, match="change_reason"):
            load_pack(tmp_path)

    def test_an_undeclared_template_variable_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        (tmp_path / "r.json").write_text(
            json.dumps(_record(template="{{ subject }} {{ missing }}")), encoding="utf-8"
        )
        with pytest.raises(PromptPackInvalid, match="undeclared=\\['missing'\\]"):
            load_pack(tmp_path)

    def test_a_declared_but_unused_variable_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        (tmp_path / "r.json").write_text(json.dumps(_record(template="nothing")), encoding="utf-8")
        with pytest.raises(PromptPackInvalid, match="unused=\\['subject'\\]"):
            load_pack(tmp_path)

    def test_an_optional_variable_must_declare_a_default(self, tmp_path: Path) -> None:
        record = _record()
        record["variables"]["subject"]["required"] = False
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        (tmp_path / "r.json").write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(PromptPackInvalid, match="declares no default"):
            load_pack(tmp_path)

    def test_a_stale_manifest_is_refused(self, tmp_path: Path) -> None:
        root = _write_pack(tmp_path / "pack", [_record()])
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest["prompts"][0]["sha256"] = "sha256:" + "0" * 64
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(PromptPackInvalid, match="is stale"):
            load_pack(root)


class TestRendering:
    """``StrictUndefined``, declared types, and unknown variables as errors rather than noise."""

    @staticmethod
    def _library(tmp_path: Path, record: dict[str, Any] | None = None) -> Any:
        return load_pack(_write_pack(tmp_path / "pack", [record or _record()]))

    def test_it_renders(self, tmp_path: Path) -> None:
        rendered = self._library(tmp_path).render(
            "benchmarks.example.probe", {"subject": "caching"}
        )
        assert rendered.user == "caching"
        assert rendered.prompt_id == "benchmarks.example.probe"
        assert rendered.version == "1.0.0"

    def test_a_missing_required_variable_is_an_error_not_an_empty_string(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PromptVariableError, match="requires variable 'subject'"):
            self._library(tmp_path).render("benchmarks.example.probe", {})

    def test_an_unknown_variable_is_refused_rather_than_ignored(self, tmp_path: Path) -> None:
        # This is how a renamed variable is caught: silently ignoring it would render the old
        # template with a default and report a measurement of the wrong prompt.
        with pytest.raises(PromptVariableError, match="does not declare"):
            self._library(tmp_path).render(
                "benchmarks.example.probe", {"subject": "x", "subjekt": "y"}
            )

    def test_a_declared_type_is_enforced(self, tmp_path: Path) -> None:
        with pytest.raises(PromptVariableError, match="declared 'string'"):
            self._library(tmp_path).render("benchmarks.example.probe", {"subject": 7})

    def test_declared_bounds_are_enforced(self, tmp_path: Path) -> None:
        record = _record(
            template="{{ words }}",
            variables={
                "words": {
                    "type": "integer",
                    "required": True,
                    "description": "How many.",
                    "min": 100,
                    "max": 500,
                }
            },
        )
        library = self._library(tmp_path, record)
        with pytest.raises(PromptVariableError, match="at least 100"):
            library.render("benchmarks.example.probe", {"words": 10})
        with pytest.raises(PromptVariableError, match="at most 500"):
            library.render("benchmarks.example.probe", {"words": 900})
        assert library.render("benchmarks.example.probe", {"words": 100}).user == "100"

    def test_strict_undefined_fires_on_a_filter_over_nothing(self, tmp_path: Path) -> None:
        record = _record(template="{{ subject.missing_attribute.deeper }}")
        with pytest.raises(PromptRenderError):
            self._library(tmp_path, record).render("benchmarks.example.probe", {"subject": "text"})

    def test_an_unknown_prompt_names_what_is_installed(self, tmp_path: Path) -> None:
        with pytest.raises(PromptNotFound, match="benchmarks.example.probe"):
            self._library(tmp_path).get("benchmarks.nope")

    def test_an_unknown_version_is_distinguished_from_an_unknown_prompt(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PromptNotFound, match="has no version '9.9.9'"):
            self._library(tmp_path).get("benchmarks.example.probe", version="9.9.9")


class TestSubsetHashing:
    """ADR-0028 §1, and the Phase 6 assertion that motivates it.

    A benchmark declares the prompts it uses. Editing one of *those* changes its subset hash and
    separates its results; editing anything else in the pack changes nothing about it.
    """

    @staticmethod
    def _pack(tmp_path: Path, used_template: str, other_template: str) -> Any:
        used = _record(prompt_id="benchmarks.used.probe", template=used_template)
        other = _record(prompt_id="benchmarks.other.probe", template=other_template)
        return load_pack(_write_pack(tmp_path, [used, other]))

    def test_editing_an_unused_prompt_leaves_the_subset_hash_unchanged(
        self, tmp_path: Path
    ) -> None:
        before = self._pack(tmp_path / "a", "{{ subject }}", "{{ subject }}")
        after = self._pack(tmp_path / "b", "{{ subject }}", "{{ subject }}!")
        wanted = (("benchmarks.used.probe", "1.0.0"),)
        assert prompt_subset_hash(before.references(wanted)) == prompt_subset_hash(
            after.references(wanted)
        )

    def test_editing_a_used_prompt_changes_the_subset_hash(self, tmp_path: Path) -> None:
        before = self._pack(tmp_path / "a", "{{ subject }}", "{{ subject }}")
        after = self._pack(tmp_path / "b", "{{ subject }}!", "{{ subject }}")
        wanted = (("benchmarks.used.probe", "1.0.0"),)
        assert prompt_subset_hash(before.references(wanted)) != prompt_subset_hash(
            after.references(wanted)
        )

    def test_the_pack_hash_changes_for_either_edit(self, tmp_path: Path) -> None:
        # Which is precisely why the pack hash is provenance and not a fingerprint input: it is
        # sensitive to changes that separate nothing.
        before = self._pack(tmp_path / "a", "{{ subject }}", "{{ subject }}")
        after = self._pack(tmp_path / "b", "{{ subject }}", "{{ subject }}!")
        assert before.pack_hash() != after.pack_hash()

    def test_subset_order_does_not_matter(self, tmp_path: Path) -> None:
        pack = self._pack(tmp_path, "{{ subject }}", "{{ subject }}")
        forward = pack.references(
            (("benchmarks.used.probe", "1.0.0"), ("benchmarks.other.probe", "1.0.0"))
        )
        reverse = pack.references(
            (("benchmarks.other.probe", "1.0.0"), ("benchmarks.used.probe", "1.0.0"))
        )
        assert prompt_subset_hash(forward) == prompt_subset_hash(reverse)

    def test_an_empty_subset_is_a_real_value(self, tmp_path: Path) -> None:
        assert prompt_subset_hash(()).startswith("sha256:")


class TestOverrides:
    """A user override replaces a shipped record and is marked on everything that used it."""

    def test_an_override_replaces_the_shipped_record_and_is_marked(self, tmp_path: Path) -> None:
        root = _write_pack(tmp_path / "pack", [_record()])
        overrides = tmp_path / "overrides"
        overrides.mkdir()
        (overrides / "benchmarks.example.probe.json").write_text(
            json.dumps(_record(template="{{ subject }} (overridden)")), encoding="utf-8"
        )
        library = load_pack(root, override_root=overrides)
        record = library.get("benchmarks.example.probe")
        assert record.source == "user_override"
        assert record.render({"subject": "x"}).user == "x (overridden)"

    def test_an_override_does_not_make_the_manifest_stale(self, tmp_path: Path) -> None:
        # The manifest describes the shipped pack. An override deliberately differs from it and is
        # marked on every result that used it instead (prompt standards §6).
        root = _write_pack(tmp_path / "pack", [_record()])
        overrides = tmp_path / "overrides"
        overrides.mkdir()
        (overrides / "x.json").write_text(
            json.dumps(_record(template="{{ subject }}!")), encoding="utf-8"
        )
        assert load_pack(root, override_root=overrides) is not None
