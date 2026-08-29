"""The mock toolbox cannot reach outside its fixtures, under adversarial arguments.

Spec §14: *native tool benchmarks expose only mock tools over fixture data — no shell, no
unrestricted filesystem, no network, no real database.* Security standards §14 requires the path
traversal, absolute path and symlink cases to be asserted, and §6 requires that model-generated
content is never executed.

These are the tests that stand behind the phase's acceptance criterion 4, "adversarial tool
arguments cannot escape the fixture directory", and behind its named failure mode, "tool fixtures
leaking real paths" — which is checked here as a property of *every* tool result, including the
error messages, because an error message goes into the next prompt exactly like a result does.
"""

from __future__ import annotations

import ast
import compileall
from pathlib import Path

import pytest

from freeweight.benchmarks.fixtures import tools as toolbox_module
from freeweight.benchmarks.fixtures.tools import (
    DATA_ROOT,
    REPO_ROOT,
    WRITING_TOOL,
    MockToolbox,
    PathEscape,
    contained_path,
    tool_definitions,
)

_ESCAPES = [
    "../tools.json",
    "../../../etc/passwd",
    "/etc/passwd",
    "pkg/../../records.json",
    "./../../inventory.json",
    "pkg/../../../../../../../../etc/hostname",
]


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Where the one writing tool is allowed to write, for the tests that exercise it."""
    return tmp_path / "sandbox"


@pytest.fixture
def toolbox(sandbox: Path) -> MockToolbox:
    """A toolbox offering everything, writing into a throwaway sandbox."""
    return MockToolbox(sandbox_root=sandbox)


class TestReadContainment:
    """No argument reads a byte outside the fixture repository."""

    @pytest.mark.parametrize("path", _ESCAPES)
    def test_read_file_refuses_every_escape(self, toolbox: MockToolbox, path: str) -> None:
        outcome = toolbox.invoke("read_file", {"path": path})
        assert not outcome.ok
        assert outcome.error_code == "CONTAINMENT_REFUSED"

    @pytest.mark.parametrize("path", _ESCAPES)
    def test_list_directory_refuses_every_escape(self, toolbox: MockToolbox, path: str) -> None:
        outcome = toolbox.invoke("list_directory", {"path": path})
        assert not outcome.ok
        assert outcome.error_code == "CONTAINMENT_REFUSED"

    def test_a_symlink_out_of_the_fixture_tree_is_refused(
        self, toolbox: MockToolbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A symlink planted *inside* the fixture tree is the case a string-level check misses:
        # the path contains no "..", is not absolute, and still lands outside. Containment is
        # decided after resolution, which is why this fails like the rest.
        secret = tmp_path / "secret.txt"
        secret.write_text("not for the model", encoding="utf-8")
        planted = tmp_path / "repo"
        planted.mkdir()
        (planted / "pkg").mkdir()
        (planted / "pkg" / "real.py").write_text("def visible(): ...\n", encoding="utf-8")
        (planted / "escape.py").symlink_to(secret)
        monkeypatch.setattr(toolbox_module, "REPO_ROOT", planted)

        assert toolbox.invoke("read_file", {"path": "pkg/real.py"}).ok
        outcome = toolbox.invoke("read_file", {"path": "escape.py"})
        assert not outcome.ok
        assert outcome.error_code == "CONTAINMENT_REFUSED"
        assert "not for the model" not in outcome.content

    def test_contained_path_accepts_the_root_itself(self) -> None:
        assert contained_path(REPO_ROOT, ".") == REPO_ROOT.resolve()

    @pytest.mark.parametrize("path", _ESCAPES)
    def test_contained_path_raises_its_own_type(self, path: str) -> None:
        with pytest.raises(PathEscape):
            contained_path(REPO_ROOT, path)

    def test_a_null_byte_is_refused_rather_than_reaching_the_filesystem(self) -> None:
        with pytest.raises(PathEscape):
            contained_path(REPO_ROOT, "pkg/pricing.py\x00.txt")


class TestWriteContainment:
    """The one writing tool writes inside the sandbox or not at all."""

    @pytest.mark.parametrize(
        "name",
        [
            "../escape.txt",
            "/tmp/escape.txt",  # noqa: S108 — an adversarial argument, not a path this code uses
            "pkg/nested.txt",
            "..",
            "",
            "a" * 200,
        ],
    )
    def test_write_sandbox_file_refuses_anything_but_a_plain_name(
        self, toolbox: MockToolbox, name: str
    ) -> None:
        outcome = toolbox.invoke("write_sandbox_file", {"name": name, "content": "x"})
        assert not outcome.ok
        assert outcome.error_code == "INVALID_ARGUMENT"

    def test_a_permitted_write_lands_in_the_sandbox_with_restrictive_modes(
        self, toolbox: MockToolbox, sandbox: Path
    ) -> None:
        assert toolbox.invoke("write_sandbox_file", {"name": "note.txt", "content": "hi"}).ok
        written = sandbox / "note.txt"
        assert written.read_text(encoding="utf-8") == "hi"
        # Security standards §5: new files 0600, new directories 0700.
        assert written.stat().st_mode & 0o777 == 0o600
        assert sandbox.stat().st_mode & 0o777 == 0o700

    def test_nothing_is_written_before_the_name_is_validated(
        self, toolbox: MockToolbox, sandbox: Path
    ) -> None:
        toolbox.invoke("write_sandbox_file", {"name": "../escape.txt", "content": "x"})
        assert not sandbox.exists()

    def test_a_toolbox_with_no_sandbox_cannot_offer_the_writing_tool(self) -> None:
        # Refused by construction, not by a runtime check: the only tool here that writes cannot be
        # reached without somebody having chosen where it writes (security standards §5).
        with pytest.raises(ValueError, match="sandbox_root"):
            MockToolbox(offered=("calculator", WRITING_TOOL))

    def test_the_default_toolbox_offers_everything_except_the_writing_tool(self) -> None:
        offered = set(MockToolbox().offered)
        assert WRITING_TOOL not in offered
        assert "read_file" in offered
        assert WRITING_TOOL in set(MockToolbox(sandbox_root=Path("/nonexistent")).offered)

    def test_the_repository_is_never_written_to(self, toolbox: MockToolbox) -> None:
        before = sorted(path.name for path in REPO_ROOT.rglob("*"))
        toolbox.invoke("write_sandbox_file", {"name": "note.txt", "content": "x"})
        assert sorted(path.name for path in REPO_ROOT.rglob("*")) == before


class TestNoHostPathsLeak:
    """A tool result — including an error — is read by the model. It carries no host path."""

    def test_no_result_contains_an_absolute_path_from_this_machine(
        self, toolbox: MockToolbox
    ) -> None:
        roots = {str(REPO_ROOT.resolve()), str(DATA_ROOT.resolve()), str(Path.home())}
        calls: list[tuple[str, dict[str, object]]] = [
            ("read_file", {"path": "pkg/pricing.py"}),
            ("read_file", {"path": "../../../etc/passwd"}),
            ("read_file", {"path": "nope.py"}),
            ("list_directory", {"path": "."}),
            ("list_directory", {"path": "/etc"}),
            ("search_text", {"query": "restock"}),
            ("search_text", {"query": "nothing-matches-this"}),
            ("search_symbol", {"symbol": "total_units"}),
            ("search_symbol", {"symbol": "nosuchsymbol"}),
            ("run_mock_test", {"target": "tests/test_pricing.py"}),
            ("write_sandbox_file", {"name": "../x", "content": "x"}),
        ]
        for name, arguments in calls:
            content = toolbox.invoke(name, arguments).content
            for root in roots:
                assert root not in content, f"{name} leaked {root}"

    def test_search_results_are_repository_relative(self, toolbox: MockToolbox) -> None:
        outcome = toolbox.invoke("search_symbol", {"symbol": "restock_cost"})
        assert outcome.content.startswith("pkg/pricing.py:")


class TestTheRepositoryIsWhatWasAuthored:
    """An installed copy of the fixtures presents the same repository a checkout does.

    ``pip`` byte-compiles every ``.py`` in the wheel, and the fixture repository under
    ``data/repo`` is ``.py`` files that are content rather than code — so an installed FreeWeight
    grows ``__pycache__`` directories that no source checkout has, and the tools were reading them
    as UTF-8. Every search raised :exc:`UnicodeDecodeError` on the first ``.pyc``, which ends a
    benchmark case rather than answering it; the caches were also visible in a listing, so the
    repository a model explored depended on how FreeWeight had been installed. These tests plant
    the caches the way an installer makes them, because a checkout never has any.
    """

    @pytest.fixture
    def installed_copy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A fixture repository byte-compiled in place, as ``pip install`` leaves one."""
        planted = tmp_path / "repo"
        (planted / "pkg").mkdir(parents=True)
        (planted / "pkg" / "pricing.py").write_text(
            "def restock_cost(units):\n    return units * 2\n", encoding="utf-8"
        )
        (planted / "README.md").write_text("# fixture\n", encoding="utf-8")
        compileall.compile_dir(str(planted), quiet=2)
        assert list(planted.rglob("*.pyc")), "precondition: the caches were created"
        monkeypatch.setattr(toolbox_module, "REPO_ROOT", planted)
        return planted

    def test_searching_reads_no_byte_compiled_cache(
        self, toolbox: MockToolbox, installed_copy: Path
    ) -> None:
        text = toolbox.invoke("search_text", {"query": "restock_cost"})
        symbol = toolbox.invoke("search_symbol", {"symbol": "restock_cost"})

        assert text.ok and symbol.ok
        assert text.content == "pkg/pricing.py:1: def restock_cost(units):"
        assert symbol.content == "pkg/pricing.py:1: def restock_cost(units):"

    def test_a_cache_is_in_no_listing_and_cannot_be_read(
        self, toolbox: MockToolbox, installed_copy: Path
    ) -> None:
        cache = next(installed_copy.rglob("*.pyc"))
        relative = cache.relative_to(installed_copy).as_posix()

        listing = toolbox.invoke("list_directory", {"path": "pkg"})
        read = toolbox.invoke("read_file", {"path": relative})

        assert listing.content == "pricing.py"
        assert (read.ok, read.error_code) == (False, "NOT_FOUND")

    def test_a_file_that_is_not_text_is_refused_rather_than_ending_the_case(
        self, toolbox: MockToolbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same guarantee for a binary fixture that is not a cache: a value, not a traceback."""
        planted = tmp_path / "repo"
        planted.mkdir()
        (planted / "pricing.py").write_text("def restock_cost(): ...\n", encoding="utf-8")
        (planted / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        monkeypatch.setattr(toolbox_module, "REPO_ROOT", planted)

        read = toolbox.invoke("read_file", {"path": "logo.png"})
        search = toolbox.invoke("search_symbol", {"symbol": "restock_cost"})

        assert (read.ok, read.error_code) == (False, "INVALID_ARGUMENT")
        assert search.ok
        assert search.content == "pricing.py:1: def restock_cost(): ..."


class TestNoExecution:
    """Security standards §6: model-generated content is never executed, however it is framed."""

    def test_the_calculator_evaluates_without_an_interpreter(self, toolbox: MockToolbox) -> None:
        assert toolbox.invoke("calculator", {"expression": "(3 + 4) * 2"}).content == "14"

    @pytest.mark.parametrize(
        "expression",
        [
            "__import__('os').system('true')",
            "open('/etc/passwd').read()",
            "9**9**9",
            "1 if True else 2",
            "[x for x in range(10)]",
            "3 / 0",
            "(1 + 2",
            "1 + 2)",
            "",
        ],
    )
    def test_anything_outside_the_grammar_is_refused(
        self, toolbox: MockToolbox, expression: str
    ) -> None:
        outcome = toolbox.invoke("calculator", {"expression": expression})
        assert not outcome.ok
        assert outcome.error_code == "INVALID_ARGUMENT"

    def test_the_module_reaches_for_no_execution_primitive(self) -> None:
        # A structural assertion rather than a behavioural one: the guarantee is that these names
        # are *absent*, and a test that only tried a handful of inputs could not say that.
        source = Path(toolbox_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not imported & {
            "subprocess",
            "os",
            "socket",
            "shutil",
            "urllib",
            "httpx",
            "requests",
        }
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not called & {"eval", "exec", "compile", "__import__"}

    def test_run_mock_test_returns_a_recording_and_runs_nothing(self, toolbox: MockToolbox) -> None:
        outcome = toolbox.invoke("run_mock_test", {"target": "tests/test_pricing.py"})
        assert outcome.ok
        assert '"failed": 1' in outcome.content


class TestTheDataTools:
    """The four fixture-data tools: they read shipped JSON and reach nothing else.

    Included in the security module rather than beside the suites because the point being asserted
    is spec §14's "no real database" — ``database_query`` is a dictionary lookup and an equality
    test, and its refusals are the ones the recovery suite's scenarios are built from.
    """

    def test_lookup_record_finds_and_refuses(self, toolbox: MockToolbox) -> None:
        found = toolbox.invoke("lookup_record", {"record_id": "CUST-1001"})
        missing = toolbox.invoke("lookup_record", {"record_id": "CUST-9999"})
        assert found.ok and "Northwind Supply" in found.content
        assert missing.error_code == "NOT_FOUND"

    def test_database_query_filters_by_one_exact_value(self, toolbox: MockToolbox) -> None:
        filtered = toolbox.invoke(
            "database_query", {"table": "orders", "field": "sku", "value": "B2"}
        )
        assert filtered.ok
        assert filtered.digest == "2 row(s)"
        assert toolbox.invoke("database_query", {"table": "orders"}).ok

    @pytest.mark.parametrize(
        ("arguments", "code"),
        [
            ({"table": "secrets"}, "NOT_FOUND"),
            ({"table": "orders", "field": "sku"}, "INVALID_ARGUMENT"),
            ({"table": "orders", "field": "sku", "value": "ZZ"}, "EMPTY_RESULT"),
        ],
    )
    def test_database_query_refuses_what_it_cannot_answer(
        self, toolbox: MockToolbox, arguments: dict[str, object], code: str
    ) -> None:
        assert toolbox.invoke("database_query", arguments).error_code == code

    def test_get_inventory_finds_and_refuses(self, toolbox: MockToolbox) -> None:
        assert toolbox.invoke("get_inventory", {"sku": "A1"}).digest == "A1: 12 units"
        assert toolbox.invoke("get_inventory", {"sku": "ZZ"}).error_code == "NOT_FOUND"

    def test_run_mock_test_refuses_an_unrecorded_target(self, toolbox: MockToolbox) -> None:
        assert toolbox.invoke("run_mock_test", {"target": "tests/nope.py"}).error_code == (
            "NOT_FOUND"
        )

    def test_the_filesystem_tools_answer_and_refuse(self, toolbox: MockToolbox) -> None:
        assert (
            "def total_units" in toolbox.invoke("read_file", {"path": "pkg/inventory.py"}).content
        )
        assert toolbox.invoke("read_file", {"path": "pkg/nope.py"}).error_code == "NOT_FOUND"
        assert toolbox.invoke("list_directory", {"path": "."}).content == "README.md\npkg/\ntests/"
        assert toolbox.invoke("list_directory", {"path": "pkg/nope"}).error_code == "NOT_FOUND"

    def test_search_refuses_an_empty_query_and_a_non_identifier_symbol(
        self, toolbox: MockToolbox
    ) -> None:
        assert toolbox.invoke("search_text", {"query": "   "}).error_code == "INVALID_ARGUMENT"
        assert toolbox.invoke("search_symbol", {"symbol": "not a name"}).error_code == (
            "INVALID_ARGUMENT"
        )
        assert toolbox.invoke("search_symbol", {"symbol": "nosuchthing"}).error_code == "NOT_FOUND"

    def test_an_over_long_expression_is_refused_before_it_is_parsed(
        self, toolbox: MockToolbox
    ) -> None:
        assert toolbox.invoke("calculator", {"expression": "1+" * 100 + "1"}).error_code == (
            "INVALID_ARGUMENT"
        )

    def test_an_over_long_write_is_refused(self, toolbox: MockToolbox) -> None:
        assert (
            toolbox.invoke(
                "write_sandbox_file", {"name": "big.txt", "content": "x" * 5000}
            ).error_code
            == "INVALID_ARGUMENT"
        )

    def test_an_over_long_result_is_truncated_and_says_so(self) -> None:
        from freeweight.benchmarks.fixtures.tools import ToolOutcome

        outcome = ToolOutcome.success("x" * 10_000)
        assert outcome.ok
        assert outcome.content.endswith("characters.")
        assert len(outcome.content) < 10_000


class TestTheAllowlist:
    """A tool the case did not offer is never run, whatever the model calls it."""

    def test_an_unoffered_tool_is_refused(self, tmp_path: Path) -> None:
        limited = MockToolbox(sandbox_root=tmp_path / "sandbox", offered=("calculator",))
        outcome = limited.invoke("read_file", {"path": "pkg/pricing.py"})
        assert not outcome.ok
        assert outcome.error_code == "UNKNOWN_TOOL"

    def test_a_tool_that_does_not_exist_at_all_is_refused(self, toolbox: MockToolbox) -> None:
        assert toolbox.invoke("shell", {"command": "ls"}).error_code == "UNKNOWN_TOOL"

    def test_arguments_are_validated_against_the_tools_own_schema(
        self, toolbox: MockToolbox
    ) -> None:
        assert toolbox.invoke("read_file", {}).error_code == "INVALID_ARGUMENT"
        assert toolbox.invoke("read_file", {"path": 7}).error_code == "INVALID_ARGUMENT"
        assert toolbox.invoke("read_file", {"path": "a", "mode": "w"}).error_code == (
            "INVALID_ARGUMENT"
        )

    def test_an_oversize_argument_is_refused_before_the_tool_runs(
        self, toolbox: MockToolbox
    ) -> None:
        assert toolbox.invoke("search_text", {"query": "x" * 1000}).error_code == (
            "INVALID_ARGUMENT"
        )

    def test_asking_for_a_tool_the_fixture_file_does_not_define_is_a_key_error(self) -> None:
        with pytest.raises(KeyError, match="shell"):
            tool_definitions(["read_file", "shell"])

    def test_the_shipped_definitions_declare_only_bounded_schemas(self) -> None:
        # Every parameter schema is checked by this application's own bounded validator, so a
        # fixture that reached for a keyword the validator refuses would fail here rather than at
        # the first tool call of a run.
        from freeweight.domain.scorers.schema import SUPPORTED_KEYWORDS

        for definition in tool_definitions():
            assert set(definition.parameters) <= SUPPORTED_KEYWORDS
            for subschema in definition.parameters.get("properties", {}).values():
                assert set(subschema) <= SUPPORTED_KEYWORDS


class TestInjectedFailures:
    """The recovery suite's failures are scheduled, and they run out."""

    def test_the_first_call_fails_and_the_second_works(self, tmp_path: Path) -> None:
        box = MockToolbox(
            sandbox_root=tmp_path / "sandbox",
            offered=("get_inventory",),
            injected_failures={"get_inventory": ["TIMEOUT"]},
        )
        first = box.invoke("get_inventory", {"sku": "A1"})
        second = box.invoke("get_inventory", {"sku": "A1"})
        assert (first.ok, first.error_code) == (False, "TIMEOUT")
        assert second.ok

    def test_scheduling_a_failure_on_an_unoffered_tool_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError):
            MockToolbox(
                sandbox_root=tmp_path / "sandbox",
                offered=("calculator",),
                injected_failures={"read_file": ["NOT_FOUND"]},
            )
