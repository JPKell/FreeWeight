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
from typer.testing import CliRunner

from freeweight.cli.main import app
from freeweight.services.prompts import (
    PACK_ROOT,
    PromptNotFound,
    PromptPackInvalid,
    PromptRenderError,
    PromptVariableError,
    build_manifest,
    load_pack,
    pack_hash,
    prompt_record_hash,
    prompt_subset_hash,
    write_manifest,
)

runner = CliRunner()

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

    def test_the_pack_hash_is_the_golden_one_recorded_across_the_setspec_adoption(self) -> None:
        """The P12 acceptance criterion for the `setspec.prompts` adoption (ADR-0028 §2).

        This literal was computed by the pre-adoption in-application implementation over the
        shipped pack, and the adoption's claim is byte-identity: the same records, hashed by
        `setspec.prompts`, produce the same value. A benchmark's `prompt_subset_hash` reaches the
        reproducibility fingerprint and separates evidence across applications, so a hasher that
        drifted here would silently separate results that share every prompt they used. Editing a
        shipped prompt legitimately changes this — update the literal in the same commit, with the
        prompt-standards version bump that edit already requires.
        """
        library = load_pack()
        assert (
            library.pack_hash()
            == "sha256:b1b0ffd0a5941fee5e0013d2a826732ea02a285b229bdc006ebd6dd25ff4ceb4"
        )

    def test_every_record_renders_with_its_documented_variables(self) -> None:
        library = load_pack()
        for record in library.all_records():
            variables = {name: _example_value(spec) for name, spec in record.variables.items()}
            rendered = record.render(variables)
            assert rendered.user
            assert rendered.rendered_sha256.startswith("sha256:")

    def test_rendering_is_byte_identical_twice(self) -> None:
        library = load_pack()
        record = library.get(library.ids()[0])
        variables = {name: _example_value(spec) for name, spec in record.variables.items()}
        assert record.render(variables) == record.render(variables)

    def test_no_prompt_lives_in_python_source(self) -> None:
        # Prompt standards §1 and §7's "No inline prompts". The benchmark modules render from the
        # pack; a multi-line instruction string appearing beside them would be a prompt nobody can
        # version, diff or attach to a result.
        for module in (PACK_ROOT.parent / "benchmarks").rglob("*.py"):
            text = module.read_text(encoding="utf-8")
            assert "You are a" not in text, f"{module} looks like it embeds a system prompt"


def _example_value(spec: Any) -> Any:
    """One value that satisfies a variable's declaration, bounds included.

    Bound-aware because a record is entitled to declare one: ``goals.judge.rubric`` restricts its
    ``scale_points`` to 3..7, and a fixture that ignored the declaration would fail a record for
    being *more* precisely specified than the others.
    """
    base = {"string": "example", "integer": 1, "number": 1.0, "boolean": True}[spec.type_name]
    if spec.type_name in {"integer", "number"}:
        if spec.minimum is not None:
            base = max(base, spec.minimum)
        if spec.maximum is not None:
            base = min(base, spec.maximum)
    return base


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


class TestTheManifestBuilder:
    """``freeweight prompts build`` regenerates the manifest; ``--check`` proves it was current.

    Prompt standards §3: "the manifest is regenerated by ``<app> prompts build`` and validated in
    CI; a record edited without a regenerated manifest fails the build." The builder and the
    startup validator share one arithmetic, which is asserted here by building a manifest and then
    loading the pack it describes — a builder that hashed differently would produce a pack that
    refuses to load the moment it is built.
    """

    def test_the_shipped_manifest_is_current(self) -> None:
        _, drift = build_manifest(PACK_ROOT)
        assert drift.is_current, (
            f"the committed manifest is stale: added={drift.added} removed={drift.removed} "
            f"changed={drift.changed}. Run `freeweight prompts build`."
        )

    def test_the_check_form_of_the_command_agrees(self) -> None:
        result = runner.invoke(app, ["prompts", "build", "--check", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["current"] is True
        assert body["written"] is False

    def test_a_rebuilt_manifest_loads(self, tmp_path: Path) -> None:
        root = tmp_path / "pack"
        _write_pack(root, [_record()])
        (root / "manifest.json").unlink()
        manifest, _ = build_manifest(root, generated_at="2026-08-27T00:00:00Z")
        write_manifest(manifest, root)
        assert load_pack(root).ids() == ("benchmarks.example.probe",)

    def test_drift_names_what_moved(self, tmp_path: Path) -> None:
        root = tmp_path / "pack"
        _write_pack(root, [_record()])
        (root / "record1.json").write_text(
            json.dumps(_record(prompt_id="benchmarks.example.other")), encoding="utf-8"
        )
        _, drift = build_manifest(root)
        assert drift.added == (("benchmarks.example.other", "1.0.0"),)
        assert not drift.is_current
        assert drift.pack_sha256_changed

    def test_an_edited_record_shows_as_changed_not_added(self, tmp_path: Path) -> None:
        root = tmp_path / "pack"
        _write_pack(root, [_record()])
        (root / "record0.json").write_text(
            json.dumps(_record(purpose="Exercise the loader, differently.")), encoding="utf-8"
        )
        _, drift = build_manifest(root)
        assert drift.changed == (("benchmarks.example.probe", "1.0.0"),)
        assert drift.added == drift.removed == ()

    def test_a_rebuild_keeps_the_pack_identity_it_found(self, tmp_path: Path) -> None:
        root = tmp_path / "pack"
        _write_pack(root, [_record()])
        manifest, _ = build_manifest(root)
        assert manifest["pack_id"] == "test.pack"
        assert manifest["generated_at"] == "2026-08-27T00:00:00Z", (
            "a rebuild never reaches for the clock; --check could not compare two manifests"
        )

    def test_the_builder_refuses_a_malformed_record(self, tmp_path: Path) -> None:
        root = tmp_path / "pack"
        _write_pack(root, [_record()])
        (root / "broken.json").write_text("{", encoding="utf-8")
        with pytest.raises(PromptPackInvalid):
            build_manifest(root)


class TestTheReadCommands:
    """``prompts list`` and ``prompts show`` read the pack a run would actually render."""

    def test_list_names_every_installed_record(self) -> None:
        result = runner.invoke(app, ["prompts", "list", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["pack_id"] == "freeweight.core"
        installed = {entry["prompt_id"] for entry in body["prompts"]}
        assert installed == set(load_pack().ids())

    def test_show_prints_the_record_and_its_hash(self) -> None:
        result = runner.invoke(app, ["prompts", "show", "benchmarks.tool_use.task", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["sha256"].startswith("sha256:")
        assert body["template"]
        assert set(body["variables"]) == {"task"}

    def test_show_exits_two_for_a_prompt_that_does_not_exist(self) -> None:
        result = runner.invoke(app, ["prompts", "show", "benchmarks.nope", "--json"])
        assert result.exit_code == 2
        assert "PROMPT_NOT_FOUND" in result.output or "NOT_FOUND" in result.output


class TestPhase7SubsetHashes:
    """Each quality suite's declared subset hash describes its own prompts and nothing else."""

    @pytest.mark.parametrize(
        "suite",
        [
            "native.instruction_following",
            "native.structured_output",
            "native.tool_use",
            "native.tool_recovery",
            "native.agent",
        ],
    )
    def test_the_manifest_agrees_with_the_installed_pack(self, suite: str) -> None:
        # ``build_registry`` verifies every suite's subset hash and refuses to build otherwise,
        # so reaching this assertion at all is the check; the assertion names the suite so a
        # failure says which manifest to rebuild.
        from freeweight.services.runs import build_registry

        benchmark = build_registry().get(suite)
        library = load_pack()
        references = library.references(
            (entry["prompt_id"], entry.get("version")) for entry in benchmark.manifest.prompt_ids
        )
        assert benchmark.manifest.prompt_subset_hash == prompt_subset_hash(references)

    def test_a_suite_is_unaffected_by_a_prompt_it_does_not_use(self) -> None:
        library = load_pack()
        used = library.references([("benchmarks.tool_use.task", None)])
        before = prompt_subset_hash(used)
        # ADR-0028 §1, restated over the Phase 7 prompts: adding the agent prompt to the *pack*
        # changes the pack hash and no suite's subset hash.
        assert before == prompt_subset_hash(used)
        assert before != library.pack_hash()


class TestTheCommandsHumanOutput:
    """The default output is for a person; ``--json`` is for a pipe (CLI standards §3)."""

    def test_list_prints_the_pack_identity_and_every_record(self) -> None:
        result = runner.invoke(app, ["prompts", "list"])
        assert result.exit_code == 0, result.output
        assert "freeweight.core" in result.output
        for prompt_id in load_pack().ids():
            assert prompt_id in result.output

    def test_show_prints_the_template_and_its_variables(self) -> None:
        result = runner.invoke(app, ["prompts", "show", "benchmarks.agent.goal"])
        assert result.exit_code == 0, result.output
        assert "sha256:" in result.output
        assert "goal (string, required)" in result.output
        assert "GOAL" in result.output

    def test_show_accepts_an_explicit_version(self) -> None:
        result = runner.invoke(
            app, ["prompts", "show", "benchmarks.agent.goal", "--version", "1.0.0"]
        )
        assert result.exit_code == 0, result.output

    def test_show_exits_two_for_a_version_that_does_not_exist(self) -> None:
        result = runner.invoke(
            app, ["prompts", "show", "benchmarks.agent.goal", "--version", "9.9.9"]
        )
        assert result.exit_code == 2

    def test_build_reports_a_current_manifest_and_writes_nothing_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from freeweight.services import prompts as prompts_module

        root = _write_pack(tmp_path / "pack", [_record()])
        monkeypatch.setattr(prompts_module, "PACK_ROOT", root)
        before = (root / "manifest.json").read_text(encoding="utf-8")
        result = runner.invoke(app, ["prompts", "build"])
        assert result.exit_code == 0, result.output
        assert "manifest is current" in result.output
        assert json.loads((root / "manifest.json").read_text(encoding="utf-8")) == json.loads(
            before
        )

    def test_build_rewrites_a_stale_manifest_and_names_what_moved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from freeweight.services import prompts as prompts_module

        root = _write_pack(tmp_path / "pack", [_record()])
        monkeypatch.setattr(prompts_module, "PACK_ROOT", root)
        (root / "extra.json").write_text(
            json.dumps(_record(prompt_id="benchmarks.example.extra")), encoding="utf-8"
        )
        result = runner.invoke(app, ["prompts", "build"])
        assert result.exit_code == 0, result.output
        assert "added: benchmarks.example.extra" in result.output
        assert "manifest rebuilt" in result.output
        assert load_pack(root).ids() == ("benchmarks.example.extra", "benchmarks.example.probe")

    def test_check_exits_five_on_a_stale_manifest_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from freeweight.services import prompts as prompts_module

        root = _write_pack(tmp_path / "pack", [_record()])
        monkeypatch.setattr(prompts_module, "PACK_ROOT", root)
        before = (root / "manifest.json").read_text(encoding="utf-8")
        (root / "extra.json").write_text(
            json.dumps(_record(prompt_id="benchmarks.example.extra")), encoding="utf-8"
        )
        result = runner.invoke(app, ["prompts", "build", "--check"])
        # Exit 5, "the operation executed and did not succeed": the check ran, and the answer is
        # no. It is not a usage error (2) and the pack is not unusable (3).
        assert result.exit_code == 5
        assert "manifest is stale" in result.output
        assert (root / "manifest.json").read_text(encoding="utf-8") == before

    def test_dry_run_changes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from freeweight.services import prompts as prompts_module

        root = _write_pack(tmp_path / "pack", [_record()])
        monkeypatch.setattr(prompts_module, "PACK_ROOT", root)
        before = (root / "manifest.json").read_text(encoding="utf-8")
        (root / "extra.json").write_text(
            json.dumps(_record(prompt_id="benchmarks.example.extra")), encoding="utf-8"
        )
        result = runner.invoke(app, ["prompts", "build", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert (root / "manifest.json").read_text(encoding="utf-8") == before

    def test_an_unreadable_manifest_exits_three_from_the_read_commands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from freeweight.services import prompts as prompts_module

        root = tmp_path / "pack"
        root.mkdir()
        (root / "manifest.json").write_text("{", encoding="utf-8")
        monkeypatch.setattr(prompts_module, "PACK_ROOT", root)
        for arguments in (["prompts", "list"], ["prompts", "show", "x"]):
            result = runner.invoke(app, arguments)
            # Exit 3 is CLI standards §4's "configuration error — … missing prompt pack", and is
            # the same code startup uses for the same condition.
            assert result.exit_code == 3, f"{arguments}: {result.output}"

    def test_build_repairs_an_unreadable_manifest_rather_than_refusing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``build`` is the command whose job is to produce a correct manifest; refusing to run
        # because the existing one is broken would leave the only repair tool unusable exactly
        # when it is needed.
        from freeweight.services import prompts as prompts_module

        root = _write_pack(tmp_path / "pack", [_record()])
        (root / "manifest.json").write_text("{", encoding="utf-8")
        monkeypatch.setattr(prompts_module, "PACK_ROOT", root)
        result = runner.invoke(app, ["prompts", "build"])
        assert result.exit_code == 0, result.output
        assert load_pack(root).ids() == ("benchmarks.example.probe",)

    def test_build_exits_three_when_a_record_is_malformed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from freeweight.services import prompts as prompts_module

        root = _write_pack(tmp_path / "pack", [_record()])
        (root / "broken.json").write_text("{", encoding="utf-8")
        monkeypatch.setattr(prompts_module, "PACK_ROOT", root)
        result = runner.invoke(app, ["prompts", "build"])
        assert result.exit_code == 3
        assert "PROMPT_INVALID" in result.output
