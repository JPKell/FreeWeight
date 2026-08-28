"""freeweight.services.goals — loading, validating, storing and moving a user's goal packs.

A goal pack is a directory of JSON the user owns
([Subjective Goals §2](../../../../docs/apps/freeweight/subjective-goals.md)). This module is
everything that touches it: reading it from disk, projecting it into the database, writing a new
one, exporting it as a portable bundle and importing somebody else's.

**Packs load at startup, exactly as prompt packs do.** :func:`load_goals` reads every pack under
the configured root and refuses the *set* if any one of them carries an ``error`` finding. A
malformed pack is a startup failure and not a mid-run surprise — the same rule, and the same
reason, as a stale prompt manifest (prompt standards §5).

**A task prompt is a prompt record.** Tasks are loaded through
:func:`freeweight.services.prompts.load_record`, the same loader and the same ``StrictUndefined``
Jinja2 environment the shipped prompts use, with no filesystem loader configured. That is what
makes "goal templates render under the same sandbox as shipped prompts" (spec §14) true by
construction: there is one environment, and user content renders in it.

**Import validates everything before it writes anything.** Size, path containment, schema, slug
collision and hash are checked against the bundle in memory; only then is a directory created, and
it is created fresh — an import never overwrites an existing goal in place (spec §14).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import (
    ConflictError,
    NotFoundError,
    ValidationError,
    canonical_json,
    sha256_of,
    utc_now,
)

from freeweight.domain.goals.hashing import compute_goal_hash, hashable_document
from freeweight.domain.goals.lint import Finding, Severity, has_errors, lint_pack, suggest_rules
from freeweight.domain.goals.pack import (
    SLUG_PATTERN,
    GoalPack,
    GoalPackInvalid,
    GoalTask,
    parse_pack,
)
from freeweight.infrastructure.db.repositories.goals import GoalRepository
from freeweight.services.prompts import PromptPackInvalid, PromptRecord, load_record

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from baseaicore import Clock

    from freeweight.services.database import Database

__all__ = [
    "BUNDLE_VERSION",
    "CALIBRATION_DIRECTORY",
    "GOAL_FILE",
    "MEMBER_PATTERN",
    "PROMPTS_DIRECTORY",
    "TASKS_DIRECTORY",
    "GoalHashChange",
    "GoalNotFound",
    "GoalPackTooLarge",
    "GoalPathUnsafe",
    "GoalSummary",
    "LoadedGoal",
    "delete_goal",
    "export_bundle",
    "goal_hash_change",
    "import_bundle",
    "list_goals",
    "replace_pack",
    "load_goal",
    "load_goals",
    "suggest_rules_for_pack",
    "sync_goals",
    "write_pack",
]

GOAL_FILE = "goal.json"
TASKS_DIRECTORY = "tasks"
PROMPTS_DIRECTORY = "prompts"
CALIBRATION_DIRECTORY = "calibration"
PACK_FILE = "pack.json"

BUNDLE_VERSION = "1.0"
"""The portable-bundle format version.

The bundle is FreeWeight's own artifact — every file of a pack, in one hash-pinned JSON document
(ADR-0031 §6). It is *not* the ``benchmark.goal_pack`` SetSpec envelope, which is the
cross-application contract and deliberately carries the goal's definition rather than its files:
an envelope names a task's prompt by id, version and hash, which is everything a consumer needs
and nothing an importer could rebuild a runnable pack from."""

MEMBER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
"""What one path component of a pack member may be (security standards §4).

Applied to every component of every member name in an imported bundle, *before* any path is built
from it. The resolved-path containment check that follows is the second guard, not the first: a
name that never becomes a path cannot escape one."""

_ALLOWED_PREFIXES = (TASKS_DIRECTORY, PROMPTS_DIRECTORY, CALIBRATION_DIRECTORY)
_ALLOWED_ROOT_FILES = (GOAL_FILE, PACK_FILE)
_POLICY_VERSION = "1.0"


class GoalNotFound(NotFoundError):
    """No goal matches the given slug.

    Attributes:
        code: ``"GOAL_NOT_FOUND"``, the stable code spec §13 names.
    """

    code: ClassVar[str] = "GOAL_NOT_FOUND"


class GoalPackTooLarge(ValidationError):
    """An imported bundle exceeds ``goals.max_pack_bytes``.

    Its own code rather than a generic validation failure: the remedy is different from every
    other import refusal, and a caller that wants to raise the cap has to be able to tell.

    Attributes:
        code: ``"PAYLOAD_TOO_LARGE"``, the shared code API standards §10 names for a body that is
            refused on size.
    """

    code: ClassVar[str] = "PAYLOAD_TOO_LARGE"


class GoalPathUnsafe(ValidationError):
    """An imported bundle names a file that would be written outside the pack directory.

    Attributes:
        code: ``"GOAL_PATH_UNSAFE"``.
    """

    code: ClassVar[str] = "GOAL_PATH_UNSAFE"


class GoalHashMismatch(ValidationError):
    """An imported bundle's declared ``goal_hash`` does not describe its own content.

    Attributes:
        code: ``"GOAL_HASH_MISMATCH"``, the goal-pack analogue of ``DATASET_HASH_MISMATCH``.
    """

    code: ClassVar[str] = "GOAL_HASH_MISMATCH"


class GoalSlugCollision(ConflictError):
    """A goal with this slug already exists, and an import never overwrites one in place.

    Attributes:
        code: ``"CONFLICT"``.
    """


@dataclass(frozen=True, slots=True)
class LoadedGoal:
    """One goal pack, read from disk and ready to run.

    Attributes:
        pack: The parsed goal.
        goal_hash: Its measurement-defining hash.
        findings: Everything the lint said, in severity-mixed declaration order.
        pack_path: The directory it was read from.
        pack_sha256: A hash over every file in that directory, for provenance.
        judge_prompt: The judge rubric this pack carries, or ``None`` when it uses the shipped one.
    """

    pack: GoalPack
    goal_hash: str
    findings: tuple[Finding, ...]
    pack_path: Path
    pack_sha256: str
    judge_prompt: PromptRecord | None = None

    @property
    def slug(self) -> str:
        """The goal's slug."""
        return self.pack.slug


@dataclass(frozen=True, slots=True)
class GoalSummary:
    """One row of ``GET /api/v1/goals``."""

    slug: str
    name: str
    goal_hash: str
    goal_pack_version: str
    capability_id: str
    contributes_to: str | None
    score_method_mix: Mapping[str, float]
    unforked: bool
    criteria_count: int
    task_count: int
    judged_criteria_count: int


def _read_json(path: Path) -> Any:  # noqa: ANN401 — a JSON value has no narrower type
    """Read and parse one JSON file, refusing with the file that failed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoalPackInvalid(
            f"Goal pack file {path} could not be read: {exc}", details={"file": str(path)}
        ) from exc


def _pack_sha256(directory: Path) -> str:
    """Hash every file in a pack directory, by relative path.

    Over a canonical map of ``{relative path: file hash}`` rather than over a concatenation, so
    the result does not depend on directory iteration order — which differs between filesystems
    and would make the same pack hash differently on two machines.
    """
    digests = {
        str(path.relative_to(directory).as_posix()): sha256_of(
            path.read_bytes().decode("utf-8", "replace")
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }
    return f"sha256:{sha256_of(canonical_json(digests))}"


def _load_tasks(directory: Path) -> tuple[GoalTask, ...]:
    """Load and render every task prompt in a pack's ``tasks/`` directory.

    A task file is a prompt record with one extra block under ``metadata.goal_task``: the task's
    key and name, the variables it renders with, its annotated source, and whether it is unedited
    starter content. Putting them in ``metadata`` keeps the file a valid prompt record — so it
    loads through the same validator, renders in the same sandbox, and hashes the same way — and
    makes the annotated source a ``goal_hash`` input, which it must be: a rung-3 criterion scored
    against a different source is a different measurement.

    Raises:
        GoalPackInvalid: A task file is not a valid prompt record, renders with a variable it did
            not declare, or two tasks share a key.
    """
    task_root = directory / TASKS_DIRECTORY
    if not task_root.is_dir():
        return ()
    tasks: list[GoalTask] = []
    for path in sorted(task_root.glob("*.json")):
        try:
            record = load_record(path, source="goal_pack")
        except PromptPackInvalid as exc:
            raise GoalPackInvalid(
                f"Goal task {path.name} is not a valid prompt record: {exc.message}",
                details={"file": str(path)},
            ) from exc
        block = dict(record.body.get("metadata", {}).get("goal_task", {}))
        variables = dict(block.get("variables", {}))
        try:
            rendered = record.render(variables)
        except ValidationError as exc:
            raise GoalPackInvalid(
                f"Goal task {path.name} does not render: {exc.message}",
                details={"file": str(path)},
            ) from exc
        source = block.get("source")
        tasks.append(
            GoalTask(
                key=str(block.get("key", path.stem)),
                name=str(block.get("name", path.stem)),
                prompt_id=record.prompt_id,
                prompt_version=record.version,
                prompt_sha256=record.sha256,
                rendered_prompt_hash=rendered.rendered_sha256,
                prompt_text=rendered.user,
                system_prompt=rendered.system,
                source=dict(source) if isinstance(source, dict) else None,
                is_starter=bool(block.get("is_starter", False)),
            )
        )
    keys = [task.key for task in tasks]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise GoalPackInvalid(
            f"Goal pack {directory.name!r} declares tasks {duplicates} more than once.",
            details={"slug": directory.name, "duplicates": duplicates},
        )
    return tuple(tasks)


def _load_judge_prompt(directory: Path) -> PromptRecord | None:
    """Load a pack's own judge rubric, if it carries one.

    A pack may override the shipped ``goals.judge.rubric`` with its own record. It is loaded
    through the same loader as everything else; a pack that carries none uses the shipped record,
    which Phase 8B resolves.

    Raises:
        GoalPackInvalid: The pack carries more than one judge record, or one that is not a valid
            prompt record.
    """
    prompt_root = directory / PROMPTS_DIRECTORY
    if not prompt_root.is_dir():
        return None
    candidates = [
        path for path in sorted(prompt_root.glob("*.json")) if path.name != "manifest.json"
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        raise GoalPackInvalid(
            f"Goal pack {directory.name!r} carries {len(candidates)} prompt records; a pack "
            "overrides one judge rubric or none.",
            details={"slug": directory.name, "files": [path.name for path in candidates]},
        )
    try:
        return load_record(candidates[0], source="goal_pack")
    except PromptPackInvalid as exc:
        raise GoalPackInvalid(
            f"Goal judge prompt {candidates[0].name} is not a valid prompt record: {exc.message}",
            details={"file": str(candidates[0])},
        ) from exc


def load_goal(directory: Path) -> LoadedGoal:
    """Load one goal pack from a directory.

    Args:
        directory: The pack directory, named for the goal's slug.

    Returns:
        The loaded goal, with every lint finding attached. Findings are *returned*, not raised:
        ``goals validate`` must name every problem, and the caller decides whether an ``error``
        finding refuses the pack.

    Raises:
        GoalPackInvalid: ``goal.json`` is missing or unreadable, the pack's shape is one this
            build cannot parse, or a task or judge record is not a valid prompt record.
    """
    goal_file = directory / GOAL_FILE
    if not goal_file.is_file():
        raise GoalPackInvalid(
            f"Goal pack {directory} has no {GOAL_FILE}.", details={"directory": str(directory)}
        )
    body = _read_json(goal_file)
    if not isinstance(body, dict):
        raise GoalPackInvalid(
            f"Goal pack {directory}'s {GOAL_FILE} is not a JSON object.",
            details={"directory": str(directory)},
        )
    tasks = _load_tasks(directory)
    judge_prompt = _load_judge_prompt(directory)
    pack = parse_pack(body, tasks=tasks)
    if pack.slug != directory.name:
        raise GoalPackInvalid(
            f"Goal pack in {directory.name!r} declares slug {pack.slug!r}. The directory name is "
            "the slug: a pack whose two disagreed would be reachable under one name and stored "
            "under the other.",
            details={"directory": directory.name, "slug": pack.slug},
        )
    return LoadedGoal(
        pack=pack,
        goal_hash=compute_goal_hash(
            pack, judge_prompt_sha256=None if judge_prompt is None else judge_prompt.sha256
        ),
        findings=lint_pack(pack),
        pack_path=directory,
        pack_sha256=_pack_sha256(directory),
        judge_prompt=judge_prompt,
    )


def load_goals(root: Path) -> tuple[LoadedGoal, ...]:
    """Load every goal pack under ``root``, refusing the set if any one is invalid.

    Called at startup, exactly as the prompt pack is. A pack that cannot be parsed, or that
    carries an ``error`` finding, fails the *application launch* rather than the run that first
    reaches it: a goal is a benchmark, and a benchmark whose definition is wrong must not be
    runnable at all.

    Args:
        root: ``goals.root`` — one directory per goal.

    Returns:
        The loaded goals, by slug. Empty when the root does not exist, which is the normal state
        of a fresh install.

    Raises:
        GoalPackInvalid: One pack could not be parsed, or its lint reported an error. The message
            names the slug and every error finding, because a startup failure that named one
            problem out of five would be fixed five times.
    """
    if not root.is_dir():
        return ()
    loaded: list[LoadedGoal] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        goal = load_goal(directory)
        errors = [finding for finding in goal.findings if finding.severity is Severity.ERROR]
        if errors:
            raise GoalPackInvalid(
                f"Goal pack {goal.slug!r} is not valid: "
                + "; ".join(f"{finding.code}: {finding.message}" for finding in errors),
                details={
                    "slug": goal.slug,
                    "findings": [finding.as_json() for finding in errors],
                },
            )
        loaded.append(goal)
    return tuple(loaded)


def _criterion_rows(goal: LoadedGoal) -> list[dict[str, Any]]:
    """Build the ``goal_criteria`` rows for one loaded goal."""
    by_criterion: dict[str, list[Finding]] = {}
    for finding in goal.findings:
        if finding.criterion_key is not None:
            by_criterion.setdefault(finding.criterion_key, []).append(finding)
    return [
        {
            "key": criterion.key,
            "name": criterion.name,
            "rung": criterion.rung.value,
            "weight": criterion.weight,
            "is_gate": criterion.is_gate,
            "rule_json": dict(criterion.rule) if criterion.rule is not None else None,
            "scale_points": None if criterion.scale is None else criterion.scale.points,
            "scale_descriptors_json": (
                None if criterion.scale is None else dict(criterion.scale.descriptors)
            ),
            "mode": criterion.mode,
            "lint_json": [finding.as_json() for finding in by_criterion.get(criterion.key, ())]
            or None,
        }
        for criterion in goal.pack.criteria
    ]


def _task_rows(goal: LoadedGoal) -> list[dict[str, Any]]:
    """Build the ``goal_tasks`` rows for one loaded goal."""
    return [
        {
            "key": task.key,
            "name": task.name,
            "prompt_id": task.prompt_id,
            "prompt_version": task.prompt_version,
            "prompt_sha256": task.prompt_sha256,
            "rendered_prompt_hash": task.rendered_prompt_hash,
            "source_json": dict(task.source) if task.source else None,
            "is_starter": task.is_starter,
        }
        for task in goal.pack.tasks
    ]


def sync_goals(database: Database, goals: Sequence[LoadedGoal], *, clock: Clock = utc_now) -> None:
    """Project loaded packs into the database, replacing what each declares.

    Args:
        database: The database handle.
        goals: The packs, already loaded and linted.
        clock: Injected, so a test can assert on ``updated_at``.
    """
    now = clock()
    with database.write() as session:
        repository = GoalRepository()
        for goal in goals:
            pack = goal.pack
            repository.sync(
                session,
                slug=pack.slug,
                values={
                    "name": pack.name,
                    "intent": pack.intent or None,
                    "goal_pack_version": pack.goal_pack_version,
                    "goal_hash": goal.goal_hash,
                    "contributes_to": pack.contributes_to,
                    "capability_id": pack.capability_id,
                    "judge_config_json": None if pack.judge is None else pack.judge.as_json(),
                    "calibration_config_json": pack.calibration.as_json(),
                    "pack_path": str(goal.pack_path),
                    "pack_sha256": goal.pack_sha256,
                    "forked_from": pack.forked_from,
                    "unforked": pack.unforked,
                    "created_by": pack.created_by,
                    "lint_json": [finding.as_json() for finding in goal.findings] or None,
                },
                criteria=_criterion_rows(goal),
                tasks=_task_rows(goal),
                now=now,
            )


def _declared_mix(goal: LoadedGoal) -> dict[str, float]:
    """Return the share of *declared* weight at each rung.

    Declared rather than applied: this is a property of the rubric, shown next to the goal in a
    list where no run has happened yet. The applied mix — what actually contributed to a sample —
    is on the result.
    """
    total = goal.pack.total_weight or 1.0
    mix = {"rule": 0.0, "reference": 0.0, "human": 0.0, "judge": 0.0}
    for criterion in goal.pack.criteria:
        mix[criterion.rung.value] += criterion.weight / total
    return mix


def summarize(goal: LoadedGoal) -> GoalSummary:
    """Return the list-view summary of one loaded goal."""
    return GoalSummary(
        slug=goal.pack.slug,
        name=goal.pack.name,
        goal_hash=goal.goal_hash,
        goal_pack_version=goal.pack.goal_pack_version,
        capability_id=goal.pack.capability_id,
        contributes_to=goal.pack.contributes_to,
        score_method_mix=_declared_mix(goal),
        unforked=goal.pack.unforked,
        criteria_count=len(goal.pack.criteria),
        task_count=len(goal.pack.tasks),
        judged_criteria_count=len(goal.pack.judged_criteria),
    )


def list_goals(root: Path) -> tuple[LoadedGoal, ...]:
    """Load every pack under ``root``, tolerating an invalid one.

    Unlike :func:`load_goals`, this does **not** refuse the set: ``goals list`` and
    ``GET /goals`` must still show the nine packs that are fine when the tenth is broken, and
    ``goals validate`` is where the broken one is explained. A pack that cannot be *parsed* at all
    is omitted, because there is nothing to list about it.

    Args:
        root: ``goals.root``.

    Returns:
        The packs that loaded, by slug.
    """
    if not root.is_dir():
        return ()
    loaded: list[LoadedGoal] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            loaded.append(load_goal(directory))
        except GoalPackInvalid:  # noqa: PERF203 — one bad pack must not hide the others
            continue
    return tuple(loaded)


def get_goal(root: Path, slug: str) -> LoadedGoal:
    """Load one goal by slug.

    Raises:
        GoalNotFound: No directory named ``slug`` exists under ``root``.
        GoalPackInvalid: The pack exists and cannot be parsed.
    """
    directory = root / slug
    if not directory.is_dir():
        raise GoalNotFound(f"No goal named {slug!r}.", details={"slug": slug})
    return load_goal(directory)


def suggest_rules_for_pack(goal: LoadedGoal) -> dict[str, list[str]]:
    """Return, per criterion, the rule types that might carry part of it.

    Proposals only — never applied. The application never rewrites the user's criterion
    (ADR-0031 §3); it names what a rule could check and leaves the decision where it belongs.

    Args:
        goal: The loaded goal.

    Returns:
        ``{criterion_key: [rule_type, …]}``, criteria with no suggestion omitted.
    """
    proposals = {criterion.key: list(suggest_rules(criterion)) for criterion in goal.pack.criteria}
    return {key: value for key, value in proposals.items() if value}


@dataclass(frozen=True, slots=True)
class GoalHashChange:
    """What replacing a goal would do to its history.

    Attributes:
        previous: The stored ``goal_hash``.
        current: The hash the new pack would have.
        separated_runs: How many existing runs the change would separate from future ones.
        changed_fields: Which parts of the measurement-defining document differ.
    """

    previous: str
    current: str
    separated_runs: int
    changed_fields: tuple[str, ...] = ()

    @property
    def separates(self) -> bool:
        """Whether the change moves the hash at all."""
        return self.previous != self.current


def goal_hash_change(
    database: Database, *, slug: str, existing: LoadedGoal, replacement: LoadedGoal
) -> GoalHashChange:
    """Report what replacing ``existing`` with ``replacement`` would separate, before it is applied.

    Acceptance criterion 4: the UI states how many existing runs a change would separate **before**
    it is applied. This is that statement, and it is computed by diffing the two hashable
    documents rather than by guessing from the edit.

    Args:
        database: The database handle, for the run count.
        slug: The goal's slug.
        existing: The pack as stored.
        replacement: The pack as it would be.

    Returns:
        The change.
    """
    before = hashable_document(
        existing.pack,
        judge_prompt_sha256=None if existing.judge_prompt is None else existing.judge_prompt.sha256,
    )
    after = hashable_document(
        replacement.pack,
        judge_prompt_sha256=(
            None if replacement.judge_prompt is None else replacement.judge_prompt.sha256
        ),
    )
    changed = tuple(
        sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
    )
    return GoalHashChange(
        previous=existing.goal_hash,
        current=replacement.goal_hash,
        separated_runs=_runs_for_goal(database, slug=slug, goal_hash=existing.goal_hash),
        changed_fields=changed,
    )


def _runs_for_goal(database: Database, *, slug: str, goal_hash: str) -> int:
    """Count the runs recorded against one goal hash."""
    from sqlalchemy import func, select

    from freeweight.infrastructure.db.models_runs import BenchmarkSuite, Run

    with database.read() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(Run)
                .join(BenchmarkSuite, BenchmarkSuite.id == Run.suite_id)
                .where(BenchmarkSuite.key == f"goal.{slug}")
                .where(BenchmarkSuite.goal_hash == goal_hash)
            )
            or 0
        )


def _member_relative_path(name: str) -> Path:
    """Validate one bundle member name and return it as a relative path.

    The name is checked *as a string* before any path is built from it: every component must match
    :data:`MEMBER_PATTERN`, the first component must be an allowed directory or an allowed root
    file, and nothing may be absolute or contain ``..``. A name that never becomes a path cannot
    escape one, which is the cheaper and stronger of the two guards; the resolved-path check in
    :func:`_write_member` is the second.

    Raises:
        GoalPathUnsafe: The name is absolute, traverses upwards, has a component the allowlist
            refuses, or names a location outside the pack's own layout.
    """
    import re

    if not name or name.startswith("/") or "\\" in name or ".." in name.split("/"):
        raise GoalPathUnsafe(
            f"Goal pack member {name!r} is not a relative path inside the pack.",
            details={"member": name},
        )
    parts = name.split("/")
    if any(not re.match(MEMBER_PATTERN, part) for part in parts):
        raise GoalPathUnsafe(
            f"Goal pack member {name!r} has a path component the allowlist refuses "
            f"({MEMBER_PATTERN}).",
            details={"member": name},
        )
    if len(parts) == 1:
        if parts[0] not in _ALLOWED_ROOT_FILES:
            raise GoalPathUnsafe(
                f"Goal pack member {name!r} is not one of {list(_ALLOWED_ROOT_FILES)}.",
                details={"member": name},
            )
    elif parts[0] not in _ALLOWED_PREFIXES:
        raise GoalPathUnsafe(
            f"Goal pack member {name!r} is not under {list(_ALLOWED_PREFIXES)}.",
            details={"member": name},
        )
    return Path(*parts)


def _write_member(root: Path, name: str, content: str) -> Path:
    """Write one validated member under ``root``, proving containment after resolution.

    Raises:
        GoalPathUnsafe: The resolved destination lies outside ``root`` — which the name check
            should already have prevented, and which is checked anyway because a symlink planted
            in the destination tree is not something a name can describe.
    """
    relative = _member_relative_path(name)
    base = root.resolve()
    destination = (base / relative).resolve()
    if destination != base and base not in destination.parents:
        raise GoalPathUnsafe(
            f"Goal pack member {name!r} resolves outside the pack directory.",
            details={"member": name},
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    destination.chmod(0o600)
    return destination


def bundle_hash(files: Mapping[str, str]) -> str:
    """Return the hash a bundle pins itself with.

    Over ``{member: sha256(content)}`` in canonical JSON, so a bundle that was re-serialized with
    different whitespace between its members still verifies while one whose content changed does
    not.
    """
    digests = {name: sha256_of(text) for name, text in files.items()}
    return f"sha256:{sha256_of(canonical_json(digests))}"


def export_bundle(goal: LoadedGoal) -> dict[str, Any]:
    """Return one goal as a portable, hash-pinned bundle.

    Every file of the pack, in one JSON document — the artifact ADR-0031 §6 describes. The
    ``benchmark.goal_pack`` SetSpec envelope is a different thing and is produced by
    :mod:`freeweight.web.routes.goals`: an envelope is what another *application* reads, and it
    carries the goal's definition rather than its files.

    Args:
        goal: The loaded goal.

    Returns:
        The bundle.
    """
    files = {
        str(path.relative_to(goal.pack_path).as_posix()): path.read_text(encoding="utf-8")
        for path in sorted(goal.pack_path.rglob("*"))
        if path.is_file()
    }
    return {
        "bundle_version": BUNDLE_VERSION,
        "slug": goal.pack.slug,
        "goal_hash": goal.goal_hash,
        "goal_pack_version": goal.pack.goal_pack_version,
        "created_by": goal.pack.created_by,
        "files": files,
        "bundle_sha256": bundle_hash(files),
    }


def import_bundle(
    body: Mapping[str, Any], *, root: Path, max_bytes: int, slug: str | None = None
) -> LoadedGoal:
    """Import a bundle, validating everything before writing anything.

    In order: the document's shape, its total size, every member's name, the bundle hash, and the
    slug's availability. Only then is a temporary directory populated, parsed and linted; only
    then is it moved into place. An import **never overwrites an existing goal in place**
    (spec §14) — a colliding slug is refused with the existing hash named, and the caller renames.

    Args:
        body: The parsed bundle.
        root: ``goals.root``.
        max_bytes: ``goals.max_pack_bytes``.
        slug: A slug to import under, overriding the bundle's own.

    Returns:
        The imported goal, loaded from where it now lives.

    Raises:
        GoalPackInvalid: The bundle is malformed, or the pack inside it does not parse or fails
            its lint.
        GoalPackTooLarge: The members total more than ``max_bytes``.
        GoalPathUnsafe: A member would be written outside the pack directory.
        GoalHashMismatch: The declared ``bundle_sha256`` does not describe the members.
        GoalSlugCollision: A goal with this slug already exists.
    """
    files = body.get("files")
    if not isinstance(files, dict) or not files:
        raise GoalPackInvalid(
            "A goal bundle must carry a non-empty 'files' object.", details={"field": "files"}
        )
    members = {str(name): str(content) for name, content in files.items()}
    total = sum(len(content.encode("utf-8")) for content in members.values())
    if total > max_bytes:
        raise GoalPackTooLarge(
            f"Goal bundle is {total} bytes; the cap is {max_bytes} (goals.max_pack_bytes). "
            "Refused before any file was written.",
            details={"bytes": total, "max_bytes": max_bytes},
        )
    for name in members:
        _member_relative_path(name)
    declared = str(body.get("bundle_sha256", ""))
    actual = bundle_hash(members)
    if declared != actual:
        raise GoalHashMismatch(
            f"Goal bundle declares bundle_sha256 {declared!r}; its files hash to {actual!r}. "
            "Refused before any file was written.",
            details={"declared": declared, "actual": actual},
        )
    target_slug = slug or str(body.get("slug", ""))
    # Before *any* path is built from it. A slug reaches the filesystem as a directory name, and
    # the staging directory is created before the pack is parsed, so the pattern check cannot wait
    # for :func:`~freeweight.domain.goals.pack.parse_pack` to run (security standards §4).
    if not SLUG_PATTERN.match(target_slug):
        raise GoalPathUnsafe(
            f"Goal slug {target_slug!r} must match {SLUG_PATTERN.pattern}; it becomes a directory "
            "name, so it is checked before anything is built from it.",
            details={"slug": target_slug},
        )
    destination = root / target_slug
    if destination.exists():
        existing_hash = "unknown"
        try:
            existing_hash = load_goal(destination).goal_hash
        except GoalPackInvalid:  # pragma: no cover — a broken pack still blocks the slug
            pass
        raise GoalSlugCollision(
            f"A goal named {target_slug!r} already exists with goal_hash {existing_hash}. An "
            "import never overwrites a goal in place; import it under a different slug.",
            details={"slug": target_slug, "existing_goal_hash": existing_hash},
        )

    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".import.", dir=root))
    try:
        staging.chmod(0o700)
        for name, content in sorted(members.items()):
            _write_member(staging, name, content)
        if target_slug != str(body.get("slug", "")):
            _rewrite_slug(staging, target_slug)
        staged = load_goal_as(staging, target_slug)
        if has_errors(staged.findings):
            raise GoalPackInvalid(
                f"Goal bundle {target_slug!r} fails validation: "
                + "; ".join(
                    f"{finding.code}: {finding.message}"
                    for finding in staged.findings
                    if finding.severity is Severity.ERROR
                ),
                details={
                    "slug": target_slug,
                    "findings": [finding.as_json() for finding in staged.findings],
                },
            )
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return load_goal(destination)


def _rewrite_slug(directory: Path, slug: str) -> None:
    """Rewrite a staged pack's ``goal.json`` to carry the slug it is being imported under."""
    goal_file = directory / GOAL_FILE
    body = _read_json(goal_file)
    if not isinstance(body, dict):
        raise GoalPackInvalid(
            f"Goal bundle's {GOAL_FILE} is not a JSON object.", details={"file": GOAL_FILE}
        )
    body["slug"] = slug
    goal_file.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_goal_as(directory: Path, slug: str) -> LoadedGoal:
    """Load a staged pack whose directory is not yet named for its slug.

    :func:`load_goal` refuses a pack whose directory name and slug disagree, which is right for a
    pack on disk and wrong for one in a staging directory that has not been moved yet.

    Raises:
        GoalPackInvalid: As :func:`load_goal`, or the pack's slug is not ``slug``.
    """
    body = _read_json(directory / GOAL_FILE)
    declared = body.get("slug") if isinstance(body, dict) else None
    if not isinstance(body, dict) or str(declared or "") != slug:
        raise GoalPackInvalid(
            f"Staged goal pack declares slug {declared!r}, not {slug!r}.",
            details={"slug": slug},
        )
    pack = parse_pack(body, tasks=_load_tasks(directory))
    judge_prompt = _load_judge_prompt(directory)
    return LoadedGoal(
        pack=pack,
        goal_hash=compute_goal_hash(
            pack, judge_prompt_sha256=None if judge_prompt is None else judge_prompt.sha256
        ),
        findings=lint_pack(pack),
        pack_path=directory,
        pack_sha256=_pack_sha256(directory),
        judge_prompt=judge_prompt,
    )


def write_pack(
    root: Path, *, goal: Mapping[str, Any], tasks: Iterable[Mapping[str, Any]]
) -> LoadedGoal:
    """Write a new goal pack to disk and load it back.

    The one place a pack is created — by ``goals init``, by ``POST /goals`` and by forking a
    starter — so that all three produce the same directory and the same file permissions.

    Args:
        root: ``goals.root``.
        goal: The ``goal.json`` body. Its ``slug`` names the directory.
        tasks: The task prompt records, in order.

    Returns:
        The loaded goal, including its lint findings. Findings never block creation: a lint
        finding is returned, not raised (api.md §Goals).

    Raises:
        GoalPackInvalid: The body has no usable slug, or the written pack does not parse.
        GoalSlugCollision: A goal with that slug already exists.
    """
    slug = str(goal.get("slug", ""))
    if not SLUG_PATTERN.match(slug):
        raise GoalPackInvalid(
            f"Goal slug {slug!r} must match {SLUG_PATTERN.pattern}.", details={"slug": slug}
        )
    destination = root / slug
    if destination.exists():
        raise GoalSlugCollision(f"A goal named {slug!r} already exists.", details={"slug": slug})
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".write.", dir=root))
    try:
        staging.chmod(0o700)
        _write_member(staging, GOAL_FILE, json.dumps(goal, indent=2, ensure_ascii=False) + "\n")
        for ordinal, task in enumerate(tasks, start=1):
            _write_member(
                staging,
                f"{TASKS_DIRECTORY}/{ordinal:03d}.json",
                json.dumps(task, indent=2, ensure_ascii=False) + "\n",
            )
        load_goal_as(staging, slug)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return load_goal(destination)


def replace_pack(
    root: Path,
    *,
    slug: str,
    goal: Mapping[str, Any],
    tasks: Iterable[Mapping[str, Any]],
    dry_run: bool = False,
) -> tuple[LoadedGoal, LoadedGoal]:
    """Replace one goal's pack, returning what it was and what it would be.

    Staged first, exactly as an import is: the replacement is written to a temporary directory,
    parsed and linted there, and only swapped in once it is known to load. A pack that failed to
    parse would otherwise leave the user with neither the old goal nor the new one.

    That staging is also what makes the *preview* honest. With ``dry_run``, the replacement is
    built and loaded and then discarded, so a caller can report what the change would separate
    **before** anything is applied — which is what acceptance criterion 4 asks for, and which a
    report computed after the swap could not truthfully claim.

    Args:
        root: ``goals.root``.
        slug: The goal to replace. The body's own slug must match it — a rename is a new goal.
        goal: The new ``goal.json`` body.
        tasks: The new task prompt records, in order.
        dry_run: Build and load the replacement, then leave the stored pack untouched.

    Returns:
        ``(previous, current)``. Under ``dry_run`` the second is loaded from the staging directory
        that has since been removed, so its ``pack_path`` is not a location that still exists — its
        ``goal_hash`` and its criteria, which is what a preview is about, are exact.

    Raises:
        GoalNotFound: No goal with that slug exists.
        GoalPackInvalid: The body renames the goal, or the replacement does not parse.
    """
    previous = get_goal(root, slug)
    if str(goal.get("slug", slug)) != slug:
        raise GoalPackInvalid(
            f"A goal cannot be renamed: this pack declares slug {goal.get('slug')!r} but is "
            f"replacing {slug!r}. A rename is a new goal, because the slug is the capability its "
            "evidence is emitted under.",
            details={"slug": slug, "declared": goal.get("slug")},
        )
    staging = Path(tempfile.mkdtemp(prefix=".write.", dir=root))
    try:
        staging.chmod(0o700)
        _write_member(staging, GOAL_FILE, json.dumps(goal, indent=2, ensure_ascii=False) + "\n")
        for ordinal, task in enumerate(tasks, start=1):
            _write_member(
                staging,
                f"{TASKS_DIRECTORY}/{ordinal:03d}.json",
                json.dumps(task, indent=2, ensure_ascii=False) + "\n",
            )
        staged = load_goal_as(staging, slug)
        if dry_run:
            shutil.rmtree(staging, ignore_errors=True)
            return previous, staged
        shutil.rmtree(previous.pack_path)
        staging.rename(previous.pack_path)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return previous, load_goal(root / slug)


def delete_goal(
    database: Database, root: Path, slug: str, *, dry_run: bool = True
) -> dict[str, Any]:
    """Preview or perform the deletion of one goal.

    Every destructive operation previews first (database standards §8). The preview states what
    would be lost in the terms that matter: how many runs it orphans, and how many of the user's
    own grades it destroys — the second is the one that is expensive to reproduce.

    Args:
        database: The database handle.
        root: ``goals.root``.
        slug: The goal to delete.
        dry_run: ``True`` previews; ``False`` deletes the rows and the pack directory.

    Returns:
        What was, or would be, removed.

    Raises:
        GoalNotFound: No such goal.
    """
    from sqlalchemy import func, select

    from freeweight.infrastructure.db.models_goals import CalibrationGrade, Goal, GoalCriterion

    with database.read() as session:
        row = session.scalars(select(Goal).where(Goal.slug == slug)).one_or_none()
        if row is None and not (root / slug).is_dir():
            raise GoalNotFound(f"No goal named {slug!r}.", details={"slug": slug})
        grades = 0
        if row is not None:
            grades = int(
                session.scalar(
                    select(func.count())
                    .select_from(CalibrationGrade)
                    .join(
                        GoalCriterion,
                        GoalCriterion.id == CalibrationGrade.goal_criterion_id,
                    )
                    .where(GoalCriterion.goal_id == row.id)
                )
                or 0
            )
        goal_hash = row.goal_hash if row is not None else ""
    runs = _runs_for_goal(database, slug=slug, goal_hash=goal_hash) if goal_hash else 0
    preview = {
        "slug": slug,
        "dry_run": dry_run,
        "orphaned_runs": runs,
        "destroyed_grades": grades,
        "pack_path": str(root / slug),
    }
    if dry_run:
        return preview
    if row is not None:
        with database.write() as session:
            GoalRepository().delete(session, row.id)
    shutil.rmtree(root / slug, ignore_errors=True)
    return preview


POLICY_VERSION = _POLICY_VERSION
"""The calibration-policy version recorded on every report this build writes."""
