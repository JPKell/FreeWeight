"""freeweight.benchmarks.fixtures.tools — the mock toolbox the quality suites call.

Spec §14: *native tool benchmarks expose only mock tools over fixture data — no shell, no
unrestricted filesystem, no network, no real database.* This module is that sentence made
executable, and the containment it provides is structural rather than advisory:

* **There is no shell.** Nothing here imports :mod:`subprocess`, :mod:`os.system` or
  :mod:`socket`, and ``calculator`` parses arithmetic with its own recursive-descent parser rather
  than reaching for :func:`eval` — security standards §6's "never executed" applies to a model's
  tool arguments exactly as it applies to its prose.
* **Every path is proved contained.** :func:`contained_path` resolves symlinks and refuses a
  result outside its root, so ``../``, an absolute path and a symlink planted inside the fixture
  tree all fail the same way. It is the only way a path is built here.
* **Reads and writes have different roots.** The fixture repository is read-only and the sandbox
  is write-only; a tool cannot write where it reads or read where it writes, so a model cannot
  edit the fixtures out from under the next case.
* **Bounded.** File size, result size, expression length and argument length are all capped, and a
  tool that would exceed one refuses with a code instead of returning a megabyte into a prompt.

**Errors are values, not exceptions.** Every tool returns a :class:`ToolOutcome`, because a failed
tool call is *input to the model* — the whole of ``native.tool_recovery`` depends on the model
seeing what went wrong and trying something else — and an exception would end the case instead.

**Failures can be injected.** :class:`MockToolbox` takes a schedule of deliberate failures, which
is how ``native.tool_recovery`` produces benchmark catalog §3.7's file-not-found, invalid-argument,
empty-search, permission-denied, timeout and ambiguous-result scenarios from the same tools the
success cases use.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from modelrack.types import ToolDefinition

from freeweight.domain.scorers.schema import SchemaUnsupported, validate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

__all__ = [
    "DATA_ROOT",
    "ERROR_AMBIGUOUS",
    "ERROR_CONTAINMENT_REFUSED",
    "ERROR_EMPTY_RESULT",
    "ERROR_INVALID_ARGUMENT",
    "ERROR_NOT_FOUND",
    "ERROR_PERMISSION_DENIED",
    "ERROR_TIMEOUT",
    "ERROR_UNKNOWN_TOOL",
    "MockToolbox",
    "PathEscape",
    "REPO_ROOT",
    "ToolOutcome",
    "contained_path",
    "tool_definitions",
]

DATA_ROOT: Final[Path] = Path(__file__).resolve().parent / "data"
"""The fixture data shipped inside the package."""

REPO_ROOT: Final[Path] = DATA_ROOT / "repo"
"""The read-only fixture repository the filesystem tools see. Nothing above it exists to them."""

ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_INVALID_ARGUMENT = "INVALID_ARGUMENT"
ERROR_EMPTY_RESULT = "EMPTY_RESULT"
ERROR_PERMISSION_DENIED = "PERMISSION_DENIED"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_AMBIGUOUS = "AMBIGUOUS"
ERROR_CONTAINMENT_REFUSED = "CONTAINMENT_REFUSED"
ERROR_UNKNOWN_TOOL = "UNKNOWN_TOOL"

_MAXIMUM_ARGUMENT_CHARACTERS: Final[int] = 512
_MAXIMUM_RESULT_CHARACTERS: Final[int] = 4_000
_MAXIMUM_FILE_BYTES: Final[int] = 64 * 1024
_MAXIMUM_EXPRESSION_CHARACTERS: Final[int] = 120
_SANDBOX_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
"""Security standards §4's identifier allowlist, applied before any filesystem call."""

_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class PathEscape(ValueError):
    """A path argument resolved outside the root it was required to stay inside.

    Its own type rather than a bare ``ValueError`` so the containment tests assert on the
    refusal itself and not on a message, and so a future caller cannot catch it by accident while
    meaning to catch a missing file.
    """


def contained_path(root: Path, *parts: str) -> Path:
    """Join ``parts`` onto ``root`` and prove the result stays inside it.

    Security standards §5's sanctioned path builder, and the only way a path is built in this
    module. Resolution happens **before** the containment check and follows symlinks, which is
    what makes a symlink planted inside the fixture tree fail as surely as a literal ``../``: the
    check is on where the path actually lands, never on how it was spelled.

    ``root`` itself is resolved too, so a root reached through a symlink — a developer whose
    checkout lives under one, a temporary directory on macOS — does not make every contained path
    look like an escape.

    Args:
        root: The directory the result must stay inside.
        *parts: Path components, from a model's tool arguments or from any other untrusted source.

    Returns:
        The resolved path, guaranteed to be ``root`` itself or a descendant of it.

    Raises:
        PathEscape: The resolved path lies outside ``root`` — an absolute path, a ``..``
            traversal, a symlink pointing out, or a NUL byte the filesystem would reject.
    """
    base = root.resolve()
    try:
        candidate = base.joinpath(*parts).resolve()
    except (OSError, ValueError) as exc:
        raise PathEscape(f"{parts!r} is not a usable path under {base}: {exc}") from exc
    if candidate != base and base not in candidate.parents:
        raise PathEscape(
            f"{parts!r} resolves to {candidate}, which is outside {base}. Refused before any "
            "filesystem call that would have used it."
        )
    return candidate


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What one mock tool returned.

    Attributes:
        ok: Whether the tool produced a result.
        content: The text handed back to the model, as a ``tool`` turn. Always a string, because
            that is what a tool result is on the wire; structured answers are canonical JSON.
        error_code: A stable code when ``ok`` is ``False``; ``None`` otherwise.
        digest: A short summary for the transcript, so a stored trajectory is readable without
            replaying every tool result.
    """

    ok: bool
    content: str
    error_code: str | None = None
    digest: str = ""

    @classmethod
    def failure(cls, code: str, message: str) -> ToolOutcome:
        """Build a failed outcome whose message is what the model will read."""
        return cls(ok=False, content=f"error: {code}: {message}", error_code=code, digest=code)

    @classmethod
    def success(cls, content: str, digest: str = "") -> ToolOutcome:
        """Build a successful outcome, truncating an over-long result rather than sending it.

        Truncation is announced in the content. A result silently cut in half would look to the
        model like a file that simply ends there, and the case would then be measuring the
        harness's ceiling rather than the model.
        """
        if len(content) > _MAXIMUM_RESULT_CHARACTERS:
            content = (
                content[:_MAXIMUM_RESULT_CHARACTERS]
                + f"\n… truncated at {_MAXIMUM_RESULT_CHARACTERS} characters."
            )
        return cls(ok=True, content=content, digest=digest or content[:80])


def tool_definitions(names: Iterable[str] | None = None) -> tuple[ToolDefinition, ...]:
    """Load the tool definitions from ``data/tools.json``.

    The descriptions the model reads live in JSON beside the fixtures, not in Python: a tool
    description *is* prompt content (ModelRack's own :class:`~modelrack.types.ToolDefinition` says
    so), and prompt standards §1 keeps prompt content out of source where it cannot be versioned
    or diffed as behaviour.

    Args:
        names: The tools to offer, or ``None`` for all of them. A case that offers a subset is how
            benchmark catalog §3.6's "tool unavailable" scenario is built — the tool genuinely is
            not there, rather than being there and refusing.

    Returns:
        The definitions, in the order ``tools.json`` declares them.

    Raises:
        KeyError: ``names`` includes a tool the fixture file does not define — a defect in a
            benchmark, caught when the suite is built rather than mid-run.
    """
    body = json.loads((DATA_ROOT / "tools.json").read_text(encoding="utf-8"))
    declared = {entry["name"]: entry for entry in body["tools"]}
    wanted = list(declared) if names is None else list(names)
    missing = [name for name in wanted if name not in declared]
    if missing:
        raise KeyError(f"No fixture tool named {missing}; the toolbox holds {sorted(declared)}.")
    return tuple(
        ToolDefinition(
            name=name,
            description=str(declared[name]["description"]),
            parameters=dict(declared[name]["parameters"]),
        )
        for name in wanted
    )


@dataclass
class MockToolbox:
    """Executes the mock tools, over fixture data, inside two proven roots.

    One instance per case: the failure schedule and the call counts are per-case state, and a
    toolbox shared between cases would let one case's injected timeout land in the next one.

    Args:
        sandbox_root: The only directory anything here writes to. Created on first write, and
            supplied by the caller so it is a run-scoped temporary directory rather than anywhere
            near the user's data.
        offered: The tool names this case offers. A call to anything else is a *hallucinated*
            tool and is refused with :data:`ERROR_UNKNOWN_TOOL` — the harness never runs a tool
            the case did not put on the model's allowlist (security standards §6).
        injected_failures: ``{tool_name: [error_code, …]}``. The first call to that tool takes the
            first code, the second call the second, and once the list is exhausted the tool works
            normally. That shape is what makes "fail once, then succeed" — the only shape in which
            recovery is measurable — a one-line declaration in a case.
    """

    sandbox_root: Path
    offered: tuple[str, ...] = ()
    injected_failures: Mapping[str, Sequence[str]] = field(default_factory=dict)
    _used: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        """Default the offered set to every tool, and validate the injection schedule.

        Raises:
            KeyError: The failure schedule names a tool that does not exist.
        """
        if not self.offered:
            self.offered = tuple(definition.name for definition in tool_definitions())
        unknown = sorted(set(self.injected_failures) - set(self.offered))
        if unknown:
            raise KeyError(
                f"Failure injection names {unknown}, which this case does not offer; it offers "
                f"{sorted(self.offered)}."
            )

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """The definitions for exactly the tools this case offers."""
        return tool_definitions(self.offered)

    def invoke(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Run one tool call. Never raises.

        The single entry point, so every guard applies to every call: the tool must be offered,
        the arguments must validate against its declared schema, an injected failure takes
        precedence over real work, and only then does the tool run.

        Args:
            name: The tool the model asked for, exactly as it asked.
            arguments: The parsed arguments.

        Returns:
            The outcome. A refusal is an outcome, not an exception: the model has to read it.
        """
        if name not in self.offered:
            return ToolOutcome.failure(
                ERROR_UNKNOWN_TOOL,
                f"there is no tool named {name!r}; the tools available are {sorted(self.offered)}",
            )
        invalid = self._invalid_arguments(name, arguments)
        if invalid is not None:
            return invalid
        injected = self._injected(name)
        if injected is not None:
            return injected
        return _HANDLERS[name](self, arguments)

    def _invalid_arguments(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome | None:
        """Validate the call against the tool's own schema, or return the refusal.

        Security standards §6: a tool runs only when "the arguments validate against the tool's
        schema". The same bounded validator the structured-output suite uses does the checking, so
        there is one notion of conformance in the application rather than two.
        """
        for definition in self.definitions():
            if definition.name != name:
                continue
            oversize = [
                key
                for key, value in arguments.items()
                if isinstance(value, str) and len(value) > _MAXIMUM_ARGUMENT_CHARACTERS
            ]
            if oversize:
                return ToolOutcome.failure(
                    ERROR_INVALID_ARGUMENT,
                    f"argument(s) {sorted(oversize)} exceed "
                    f"{_MAXIMUM_ARGUMENT_CHARACTERS} characters",
                )
            try:
                violations = validate(dict(arguments), definition.parameters)
            except SchemaUnsupported as exc:  # pragma: no cover — the shipped schemas are bounded
                return ToolOutcome.failure(ERROR_INVALID_ARGUMENT, str(exc))
            if violations:
                return ToolOutcome.failure(
                    ERROR_INVALID_ARGUMENT,
                    "; ".join(f"{item.path}: {item.detail}" for item in violations),
                )
        return None

    def _injected(self, name: str) -> ToolOutcome | None:
        """Return this call's scheduled failure, if the case scheduled one."""
        schedule = self.injected_failures.get(name, ())
        index = self._used.get(name, 0)
        self._used[name] = index + 1
        if index >= len(schedule):
            return None
        code = str(schedule[index])
        return ToolOutcome.failure(code, _INJECTED_MESSAGES.get(code, "the tool did not complete"))

    # --- the tools ------------------------------------------------------------------------

    def _calculator(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Evaluate one arithmetic expression with this module's own parser."""
        expression = str(arguments["expression"])
        if len(expression) > _MAXIMUM_EXPRESSION_CHARACTERS:
            return ToolOutcome.failure(
                ERROR_INVALID_ARGUMENT,
                f"the expression is {len(expression)} characters; the limit is "
                f"{_MAXIMUM_EXPRESSION_CHARACTERS}",
            )
        try:
            value = _evaluate(expression)
        except ArithmeticError as exc:
            return ToolOutcome.failure(ERROR_INVALID_ARGUMENT, str(exc))
        rendered = f"{value:g}" if isinstance(value, float) else str(value)
        return ToolOutcome.success(rendered, digest=f"{expression} = {rendered}")

    def _read_file(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Return one fixture file's text."""
        try:
            path = contained_path(REPO_ROOT, str(arguments["path"]))
        except PathEscape:
            return _outside_repository(str(arguments["path"]))
        if not path.is_file():
            return ToolOutcome.failure(
                ERROR_NOT_FOUND, f"no file at {arguments['path']!r} in the repository"
            )
        if path.stat().st_size > _MAXIMUM_FILE_BYTES:  # pragma: no cover — fixtures are small
            return ToolOutcome.failure(ERROR_INVALID_ARGUMENT, "the file is too large to return")
        return ToolOutcome.success(
            path.read_text(encoding="utf-8"), digest=f"read {arguments['path']}"
        )

    def _list_directory(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """List one fixture directory."""
        try:
            path = contained_path(REPO_ROOT, str(arguments["path"]))
        except PathEscape:
            return _outside_repository(str(arguments["path"]))
        if not path.is_dir():
            return ToolOutcome.failure(
                ERROR_NOT_FOUND, f"no directory at {arguments['path']!r} in the repository"
            )
        entries = sorted(
            f"{child.name}/" if child.is_dir() else child.name for child in path.iterdir()
        )
        return ToolOutcome.success("\n".join(entries), digest=f"{len(entries)} entries")

    def _search_text(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Find literal text across the fixture repository.

        Literal, never a pattern: a regex from a model's arguments is a denial-of-service
        primitive (spec §14), and "find this string" is what every scenario in the catalog needs.
        """
        query = str(arguments["query"])
        if not query.strip():
            return ToolOutcome.failure(ERROR_INVALID_ARGUMENT, "the query is empty")
        hits = [
            f"{path.relative_to(REPO_ROOT).as_posix()}:{number}: {line.strip()}"
            for path, number, line in _repository_lines()
            if query in line
        ]
        if not hits:
            return ToolOutcome.failure(ERROR_EMPTY_RESULT, f"nothing matches {query!r}")
        return ToolOutcome.success("\n".join(hits), digest=f"{len(hits)} matches for {query!r}")

    def _search_symbol(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Find where a function or class is defined."""
        symbol = str(arguments["symbol"])
        if _SYMBOL.match(symbol) is None:
            return ToolOutcome.failure(
                ERROR_INVALID_ARGUMENT, f"{symbol!r} is not a Python identifier"
            )
        hits = [
            f"{path.relative_to(REPO_ROOT).as_posix()}:{number}: {line.strip()}"
            for path, number, line in _repository_lines()
            if re.match(rf"\s*(?:def|class)\s+{re.escape(symbol)}\b", line)
        ]
        if not hits:
            return ToolOutcome.failure(ERROR_NOT_FOUND, f"no definition of {symbol!r}")
        return ToolOutcome.success(
            "\n".join(hits), digest=f"{symbol} defined in {len(hits)} place(s)"
        )

    def _lookup_record(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Return one fixture customer record."""
        records = _load("records.json")
        record = records.get(str(arguments["record_id"]))
        if record is None:
            return ToolOutcome.failure(ERROR_NOT_FOUND, f"no record {arguments['record_id']!r}")
        return ToolOutcome.success(json.dumps(record, sort_keys=True), digest=str(record["name"]))

    def _database_query(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Filter one fixture table by one field's exact value.

        A dictionary lookup and an equality test — there is no SQL here and no database to send it
        to (spec §14). ``field`` without ``value`` is refused rather than treated as "no filter":
        a half-specified filter is the ambiguous-argument case, not a request for everything.
        """
        tables = _load("tables.json")
        table = tables.get(str(arguments["table"]))
        if table is None:
            return ToolOutcome.failure(
                ERROR_NOT_FOUND,
                f"no table {arguments['table']!r}; the tables are {sorted(tables)}",
            )
        name = arguments.get("field")
        value = arguments.get("value")
        if (name is None) != (value is None):
            return ToolOutcome.failure(
                ERROR_INVALID_ARGUMENT, "'field' and 'value' are supplied together or not at all"
            )
        rows = (
            table
            if name is None
            else [row for row in table if str(row.get(str(name))) == str(value)]
        )
        if not rows:
            return ToolOutcome.failure(ERROR_EMPTY_RESULT, "no rows match")
        return ToolOutcome.success(json.dumps(rows, sort_keys=True), digest=f"{len(rows)} row(s)")

    def _get_inventory(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Return one SKU's stock level."""
        inventory = _load("inventory.json")
        item = inventory.get(str(arguments["sku"]))
        if item is None:
            return ToolOutcome.failure(ERROR_NOT_FOUND, f"no SKU {arguments['sku']!r}")
        return ToolOutcome.success(
            json.dumps(item, sort_keys=True), digest=f"{item['sku']}: {item['units']} units"
        )

    def _write_sandbox_file(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Write one file into the sandbox — the only write anything here performs.

        The name is matched against security standards §4's identifier allowlist *before* any
        filesystem call, and the resulting path is proved contained anyway. Both, not either: the
        allowlist is what makes the intent explicit, and the containment check is what holds if
        the allowlist is ever loosened.
        """
        name = str(arguments["name"])
        if _SANDBOX_NAME.match(name) is None:
            return ToolOutcome.failure(
                ERROR_INVALID_ARGUMENT,
                f"{name!r} is not a plain file name; use letters, digits, dot, dash, underscore",
            )
        try:
            path = contained_path(self.sandbox_root, name)
        except PathEscape:  # pragma: no cover — the allowlist above already refused every escape
            return ToolOutcome.failure(
                ERROR_CONTAINMENT_REFUSED, f"{name!r} is not inside the scratch directory"
            )
        content = str(arguments["content"])
        if len(content) > _MAXIMUM_RESULT_CHARACTERS:
            return ToolOutcome.failure(ERROR_INVALID_ARGUMENT, "the content is too large")
        self.sandbox_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return ToolOutcome.success(
            f"wrote {name} ({len(content)} characters)", digest=f"wrote {name}"
        )

    def _run_mock_test(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Return the recorded outcome of one fixture test file.

        Recorded, never executed. There is no test runner behind this tool and no code is run:
        spec §14's "model-generated code is never executed on the host" is not a policy this
        module implements, it is a capability this module does not have.
        """
        results = _load("tests.json")
        target = str(arguments["target"])
        result = results.get(target)
        if result is None:
            return ToolOutcome.failure(
                ERROR_NOT_FOUND, f"no recorded result for {target!r}; try {sorted(results)}"
            )
        return ToolOutcome.success(
            json.dumps(result, sort_keys=True),
            digest=f"{result['passed']} passed, {result['failed']} failed",
        )


_HANDLERS: Mapping[str, Callable[[MockToolbox, Mapping[str, Any]], ToolOutcome]] = {
    "calculator": MockToolbox._calculator,
    "read_file": MockToolbox._read_file,
    "list_directory": MockToolbox._list_directory,
    "search_text": MockToolbox._search_text,
    "search_symbol": MockToolbox._search_symbol,
    "lookup_record": MockToolbox._lookup_record,
    "database_query": MockToolbox._database_query,
    "get_inventory": MockToolbox._get_inventory,
    "write_sandbox_file": MockToolbox._write_sandbox_file,
    "run_mock_test": MockToolbox._run_mock_test,
}
"""Tool name to implementation. One table, so an offered tool with no handler is a ``KeyError`` at
the first call rather than a silently missing capability."""

_INJECTED_MESSAGES: Mapping[str, str] = {
    ERROR_NOT_FOUND: "the requested item does not exist",
    ERROR_INVALID_ARGUMENT: "the arguments were not accepted",
    ERROR_EMPTY_RESULT: "the search returned nothing",
    ERROR_PERMISSION_DENIED: "that path is not readable",
    ERROR_TIMEOUT: "the tool did not answer in time",
    ERROR_AMBIGUOUS: "several records match; narrow the request",
}
"""What the model reads for each injected failure — benchmark catalog §3.7's six scenarios."""


def _outside_repository(requested: str) -> ToolOutcome:
    """Refuse a path that escaped the fixture root, **without naming where it landed**.

    The refusal the model reads says only that the path is outside the repository. It deliberately
    does not carry the resolved absolute path, because that string goes straight into the next
    prompt and would tell the model the checkout's real location on this machine — the phase's
    named failure mode "tool fixtures leaking real paths", reached by way of an error message
    rather than a result. The full path is available to a developer through
    :class:`PathEscape`, which is what the containment tests assert on.
    """
    return ToolOutcome.failure(
        ERROR_CONTAINMENT_REFUSED,
        f"{requested!r} is outside the fixture repository; use a repository-relative path",
    )


def _load(name: str) -> Any:  # noqa: ANN401 — parsed JSON fixture data
    """Read one fixture JSON file from the package's own data directory."""
    return json.loads((DATA_ROOT / name).read_text(encoding="utf-8"))


def _repository_lines() -> list[tuple[Path, int, str]]:
    """Yield every ``(path, line number, line)`` in the fixture repository, in path order."""
    lines: list[tuple[Path, int, str]] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or path.stat().st_size > _MAXIMUM_FILE_BYTES:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            lines.append((path, number, line))
    return lines


# --- the calculator's parser --------------------------------------------------------------

_TOKEN = re.compile(r"\s*(?:(?P<number>\d+(?:\.\d+)?)|(?P<operator>[-+*/()]))")


def _evaluate(expression: str) -> float | int:
    """Evaluate ``+ - * / ( )`` over decimal literals, with no interpreter involved.

    A hand-written recursive-descent parser rather than :func:`eval` or :func:`ast.literal_eval`
    with a fixer-upper. The argument comes from a model, security standards §6 forbids executing
    model-generated content "regardless of how the content is framed", and an expression evaluator
    built on the Python grammar is exactly the framing that gets that rule wrong. Exponentiation
    is absent for the same reason a size cap exists: ``9**9**9`` is a denial of service spelled in
    three characters.

    Args:
        expression: The expression text.

    Returns:
        The value: an ``int`` where the arithmetic stayed whole, a ``float`` otherwise.

    Raises:
        ArithmeticError: The expression does not parse, contains a character outside the grammar,
            or divides by zero.
    """
    tokens: list[str] = []
    position = 0
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        if match is None:
            if expression[position].isspace():
                position += 1
                continue
            raise ArithmeticError(
                f"{expression[position]!r} is not part of this calculator's grammar "
                "(+ - * / parentheses and decimal numbers)"
            )
        tokens.append(match.group("number") or match.group("operator"))
        position = match.end()
    if not tokens:
        raise ArithmeticError("the expression is empty")
    value, index = _sum(tokens, 0)
    if index != len(tokens):
        raise ArithmeticError(f"unexpected {tokens[index]!r} after a complete expression")
    return value


def _sum(tokens: Sequence[str], index: int) -> tuple[float | int, int]:
    """Parse a sequence of ``+``/``-`` terms."""
    value, index = _product(tokens, index)
    while index < len(tokens) and tokens[index] in {"+", "-"}:
        operator = tokens[index]
        right, index = _product(tokens, index + 1)
        value = value + right if operator == "+" else value - right
    return value, index


def _product(tokens: Sequence[str], index: int) -> tuple[float | int, int]:
    """Parse a sequence of ``*``/``/`` factors."""
    value, index = _atom(tokens, index)
    while index < len(tokens) and tokens[index] in {"*", "/"}:
        operator = tokens[index]
        right, index = _atom(tokens, index + 1)
        if operator == "/":
            if right == 0:
                raise ArithmeticError("division by zero")
            value = value / right
        else:
            value = value * right
    return value, index


def _atom(tokens: Sequence[str], index: int) -> tuple[float | int, int]:
    """Parse a number, a parenthesised expression, or a unary sign."""
    if index >= len(tokens):
        raise ArithmeticError("the expression ends where a number was expected")
    head = tokens[index]
    if head == "-":
        value, index = _atom(tokens, index + 1)
        return -value, index
    if head == "+":
        return _atom(tokens, index + 1)
    if head == "(":
        value, index = _sum(tokens, index + 1)
        if index >= len(tokens) or tokens[index] != ")":
            raise ArithmeticError("a '(' is never closed")
        return value, index + 1
    if head in {")", "*", "/"}:
        raise ArithmeticError(f"{head!r} appears where a number was expected")
    return (float(head) if "." in head else int(head)), index + 1
