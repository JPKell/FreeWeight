"""Importing somebody else's goal pack: five refusals, each with its own code, each before a write.

Spec §14: "Imported goal packs are size-capped, path-containment-checked, schema-validated and
hash-verified before a single file is written; an import never overwrites an existing goal in
place." The phase's own test list names the five cases — oversize, path traversal, colliding slug,
bad hash, malformed JSON — and this file asserts each of them *and* that the goals directory is
untouched afterwards, because "refused" and "refused before writing" are different guarantees.

It also holds the second half of the phase's security list: **goal templates cannot reach the
filesystem or network through the Jinja2 environment**, which is asserted against the environment
user content actually renders in rather than against a policy statement about it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from baseaicore import ConflictError

from freeweight.domain.goals.pack import GoalPackInvalid
from freeweight.services.goals import (
    GoalHashMismatch,
    GoalPackTooLarge,
    GoalPathUnsafe,
    bundle_hash,
    export_bundle,
    import_bundle,
    write_pack,
)

_MAX_BYTES = 5_242_880


def _goal_body(slug: str = "imported_voice") -> dict[str, Any]:
    return {
        "slug": slug,
        "name": "Imported voice",
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "created_by": "somebody else",
        "criteria": [
            {
                "key": "no_llm_tells",
                "name": "No LLM tells",
                "rung": "rule",
                "weight": 1.0,
                "rule": {"type": "forbidden_phrases", "phrases": ["delve"]},
            }
        ],
    }


def _task_record() -> dict[str, Any]:
    return {
        "prompt_id": "goals.imported_voice.task_001",
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": "One task from the exporting author's own work.",
        "task": "goal.imported_voice",
        "capability": "creative_writing",
        "system": None,
        "template": "Write a paragraph about a warehouse.",
        "variables": {},
        "response": {"format": "text", "json_schema_ref": None, "expectations": []},
        "model_requirements": {
            "min_context_tokens": 2048,
            "requires_capabilities": [],
            "recommended_temperature": 0.7,
        },
        "metadata": {
            "author": "somebody else",
            "created_at": "2026-08-27T00:00:00Z",
            "changed_at": "2026-08-27T00:00:00Z",
            "change_reason": "First version.",
            "supersedes": None,
            "tags": ["goal"],
            "goal_task": {"key": "task_001", "name": "Task 1"},
        },
    }


def _bundle(files: dict[str, str] | None = None, **changes: Any) -> dict[str, Any]:
    members = (
        files
        if files is not None
        else {
            "goal.json": json.dumps(_goal_body()),
            "tasks/001.json": json.dumps(_task_record()),
        }
    )
    body: dict[str, Any] = {
        "bundle_version": "1.0",
        "slug": "imported_voice",
        "goal_hash": "sha256:" + "00" * 32,
        "files": members,
        "bundle_sha256": bundle_hash(members),
    }
    body.update(changes)
    return body


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A goals directory that starts empty."""
    destination = tmp_path / "goals"
    destination.mkdir()
    return destination


def _contents(root: Path) -> set[str]:
    """Every path under the goals root, so "nothing was written" is checkable."""
    return {str(path.relative_to(root)) for path in root.rglob("*")}


class TestAValidBundleImports:
    """The happy path, so the refusals below are refusals of something that would have worked."""

    def test_it_lands_and_loads(self, root: Path) -> None:
        goal = import_bundle(_bundle(), root=root, max_bytes=_MAX_BYTES)
        assert goal.pack.slug == "imported_voice"
        assert (root / "imported_voice" / "goal.json").is_file()

    def test_a_round_trip_through_export_preserves_the_hash(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        original = write_pack(source, goal=_goal_body(), tasks=[_task_record()])
        destination = tmp_path / "destination"
        destination.mkdir()
        imported = import_bundle(export_bundle(original), root=destination, max_bytes=_MAX_BYTES)
        assert imported.goal_hash == original.goal_hash

    def test_it_can_be_imported_under_a_different_slug(self, root: Path) -> None:
        goal = import_bundle(_bundle(), root=root, max_bytes=_MAX_BYTES, slug="their_voice")
        assert goal.pack.slug == "their_voice"
        assert (root / "their_voice").is_dir()

    def test_written_files_are_owner_only(self, root: Path) -> None:
        # Security standards §5: new files are 0600.
        import_bundle(_bundle(), root=root, max_bytes=_MAX_BYTES)
        mode = (root / "imported_voice" / "goal.json").stat().st_mode & 0o777
        assert mode == 0o600


class TestOversize:
    def test_it_is_refused_with_its_own_code(self, root: Path) -> None:
        big = {"goal.json": json.dumps(_goal_body()), "tasks/001.json": "x" * 4096}
        with pytest.raises(GoalPackTooLarge) as caught:
            import_bundle(_bundle(big), root=root, max_bytes=1024)
        assert caught.value.code == "PAYLOAD_TOO_LARGE"

    def test_and_nothing_is_written(self, root: Path) -> None:
        big = {"goal.json": "x" * 4096}
        with pytest.raises(GoalPackTooLarge):
            import_bundle(_bundle(big), root=root, max_bytes=1024)
        assert _contents(root) == set()


class TestPathTraversal:
    @pytest.mark.parametrize(
        "member",
        [
            "../escaped.json",
            "tasks/../../escaped.json",
            "/etc/passwd",
            "tasks/../../../etc/passwd",
            "..",
            "tasks\\..\\escaped.json",
        ],
    )
    def test_every_shape_of_escape_is_refused(self, root: Path, member: str) -> None:
        files = {"goal.json": json.dumps(_goal_body()), member: "{}"}
        with pytest.raises(GoalPathUnsafe) as caught:
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)
        assert caught.value.code == "GOAL_PATH_UNSAFE"

    def test_a_member_outside_the_pack_layout_is_refused(self, root: Path) -> None:
        files = {"goal.json": json.dumps(_goal_body()), "somewhere/else.json": "{}"}
        with pytest.raises(GoalPathUnsafe, match="not under"):
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)

    def test_an_unexpected_root_file_is_refused(self, root: Path) -> None:
        files = {"goal.json": json.dumps(_goal_body()), "setup.py": "print(1)"}
        with pytest.raises(GoalPathUnsafe, match="is not one of"):
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)

    def test_and_nothing_is_written(self, root: Path) -> None:
        files = {"goal.json": json.dumps(_goal_body()), "../escaped.json": "{}"}
        with pytest.raises(GoalPathUnsafe):
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)
        assert _contents(root) == set()
        assert not (root.parent / "escaped.json").exists()


class TestCollidingSlug:
    def test_an_existing_slug_is_refused_and_names_the_existing_hash(self, root: Path) -> None:
        existing = import_bundle(_bundle(), root=root, max_bytes=_MAX_BYTES)
        with pytest.raises(ConflictError) as caught:
            import_bundle(_bundle(), root=root, max_bytes=_MAX_BYTES)
        assert caught.value.code == "CONFLICT"
        assert caught.value.details["existing_goal_hash"] == existing.goal_hash

    def test_the_existing_pack_is_untouched(self, root: Path) -> None:
        # An import never overwrites a goal in place.
        import_bundle(_bundle(), root=root, max_bytes=_MAX_BYTES)
        before = (root / "imported_voice" / "goal.json").read_text(encoding="utf-8")
        other = _bundle()
        other["files"]["goal.json"] = json.dumps(_goal_body() | {"name": "Different"})
        other["bundle_sha256"] = bundle_hash(other["files"])
        with pytest.raises(ConflictError):
            import_bundle(other, root=root, max_bytes=_MAX_BYTES)
        assert (root / "imported_voice" / "goal.json").read_text(encoding="utf-8") == before


class TestBadHash:
    def test_a_bundle_that_does_not_describe_its_own_files_is_refused(self, root: Path) -> None:
        with pytest.raises(GoalHashMismatch) as caught:
            import_bundle(
                _bundle(bundle_sha256="sha256:" + "ff" * 32), root=root, max_bytes=_MAX_BYTES
            )
        assert caught.value.code == "GOAL_HASH_MISMATCH"

    def test_a_tampered_member_is_caught(self, root: Path) -> None:
        body = _bundle()
        body["files"]["goal.json"] = json.dumps(_goal_body() | {"name": "Tampered"})
        with pytest.raises(GoalHashMismatch):
            import_bundle(body, root=root, max_bytes=_MAX_BYTES)

    def test_and_nothing_is_written(self, root: Path) -> None:
        with pytest.raises(GoalHashMismatch):
            import_bundle(
                _bundle(bundle_sha256="sha256:" + "ff" * 32), root=root, max_bytes=_MAX_BYTES
            )
        assert _contents(root) == set()


class TestMalformedContent:
    def test_a_bundle_with_no_files_is_refused(self, root: Path) -> None:
        with pytest.raises(GoalPackInvalid) as caught:
            import_bundle({"files": {}}, root=root, max_bytes=_MAX_BYTES)
        assert caught.value.code == "GOAL_INVALID"

    def test_a_goal_json_that_is_not_json_is_refused(self, root: Path) -> None:
        files = {"goal.json": "{not json at all", "tasks/001.json": json.dumps(_task_record())}
        with pytest.raises(GoalPackInvalid):
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)
        assert _contents(root) == set()

    def test_a_pack_that_fails_its_lint_is_refused(self, root: Path) -> None:
        body = _goal_body()
        body["criteria"][0]["weight"] = 0.5  # weights no longer sum to one
        files = {"goal.json": json.dumps(body), "tasks/001.json": json.dumps(_task_record())}
        with pytest.raises(GoalPackInvalid, match="WEIGHTS_DO_NOT_SUM"):
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)
        assert _contents(root) == set()

    def test_a_task_that_is_not_a_prompt_record_is_refused(self, root: Path) -> None:
        files = {
            "goal.json": json.dumps(_goal_body()),
            "tasks/001.json": json.dumps({"prompt_id": "x"}),
        }
        with pytest.raises(GoalPackInvalid, match="not a valid prompt record"):
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)
        assert _contents(root) == set()

    def test_a_slug_that_is_not_an_identifier_is_refused(self, root: Path) -> None:
        files = {
            "goal.json": json.dumps(_goal_body(slug="../../etc")),
            "tasks/001.json": json.dumps(_task_record()),
        }
        with pytest.raises((GoalPackInvalid, GoalPathUnsafe)):
            import_bundle(_bundle(files, slug="../../etc"), root=root, max_bytes=_MAX_BYTES)
        assert _contents(root) == set()

    def test_a_slug_colliding_with_a_shipped_capability_root_is_refused(self, root: Path) -> None:
        # ADR-0032 §1 reserves ``user`` so goal capabilities never compete with the vocabulary's
        # own terms; a slug naming a shipped root would do exactly that.
        files = {
            "goal.json": json.dumps(_goal_body(slug="reasoning")),
            "tasks/001.json": json.dumps(_task_record()),
        }
        with pytest.raises(GoalPackInvalid, match="shipped capability root"):
            import_bundle(_bundle(files, slug="reasoning"), root=root, max_bytes=_MAX_BYTES)
        assert _contents(root) == set()


class TestGoalTemplatesCannotReachTheFilesystemOrNetwork:
    """User-authored goal content renders in the same sandbox shipped prompts do (spec §14)."""

    def test_the_environment_has_no_loader_so_include_and_extends_fail(self) -> None:
        # freeweight.services.prompts is now a thin wrapper around setspec.prompts (ADR-0028);
        # the sandboxed environment itself, including this private constructor, moved there too.
        from setspec.prompts import _environment

        environment = _environment()
        assert environment.loader is None

    @pytest.mark.parametrize(
        "template",
        [
            "{% include 'goal.json' %}",
            "{% extends 'base.html' %}",
            "{% import 'other.json' as other %}",
        ],
    )
    def test_a_template_that_tries_to_read_a_file_fails_to_render(self, template: str) -> None:
        # A loader-less environment raises ``TypeError`` here rather than a Jinja2 error, which is
        # why ``PromptRecord.render`` catches both and reports one refusal.
        from jinja2 import TemplateError
        from setspec.prompts import _environment

        with pytest.raises((TemplateError, TypeError)):
            _environment().from_string(template).render()

    def test_a_task_template_that_reads_a_file_is_refused_at_import(self, root: Path) -> None:
        record = _task_record()
        record["template"] = "{% include '/etc/passwd' %}"
        files = {
            "goal.json": json.dumps(_goal_body()),
            "tasks/001.json": json.dumps(record),
        }
        with pytest.raises(GoalPackInvalid):
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)
        assert _contents(root) == set()

    def test_an_undeclared_variable_is_an_error_not_an_empty_string(self, root: Path) -> None:
        # StrictUndefined, exactly as prompt standards §2.1 requires of every shipped prompt.
        record = _task_record()
        record["template"] = "Write about {{ topic }}."
        files = {
            "goal.json": json.dumps(_goal_body()),
            "tasks/001.json": json.dumps(record),
        }
        with pytest.raises(GoalPackInvalid):
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)

    @pytest.mark.parametrize(
        "template",
        [
            "{{ ''.__class__.__mro__ }}",
            "{{ ''.__class__.__base__.__subclasses__() }}",
            "{{ cycler.__init__.__globals__ }}",
            "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        ],
    )
    def test_the_first_step_of_every_escape_is_refused(self, template: str) -> None:
        # ``__class__`` is the doorway to ``open`` and to the socket module, and a goal pack
        # imported from another machine is somebody else's file. The sandbox closes it.
        from jinja2 import TemplateError
        from setspec.prompts import _environment

        with pytest.raises((TemplateError, TypeError)):
            _environment().from_string(template).render()

    def test_a_task_template_that_reaches_for_a_dunder_is_refused_at_import(
        self, root: Path
    ) -> None:
        record = _task_record()
        record["template"] = "Write about {{ ''.__class__ }}."
        files = {"goal.json": json.dumps(_goal_body()), "tasks/001.json": json.dumps(record)}
        with pytest.raises(GoalPackInvalid):
            import_bundle(_bundle(files), root=root, max_bytes=_MAX_BYTES)
        assert _contents(root) == set()
