"""freeweight.services.prompts — the prompt library: records, packs, rendering and hashing.

Prompts are versioned data, not source code
([ADR-0012](../../../../docs/adr/0012-prompt-storage-format.md),
[Prompt Management Standards](../../../../docs/standards/prompt-management-standards.md)). This
module is the loader, validator, renderer and hasher for FreeWeight's own pack, and it is written
**as if it were a package**: it holds no prompt, knows nothing about benchmarks, and takes its
pack root as an argument. That is deliberate — ADR-0028 §2 extracts exactly this machinery into
``setspec.prompts`` at LoadCoach P4, and FreeWeight adopts the extracted version at its own P12
under the acceptance criterion "the existing test suite passes unchanged". Code shaped around
FreeWeight's directory layout could not meet that criterion.

**Two hashes, and only one of them is a fingerprint input.**

* ``sha256`` over a record's canonical JSON identifies one prompt version.
* ``prompt_subset_hash`` over the sorted ``(prompt_id, version, sha256)`` of *the prompts one
  benchmark declares* is what enters the reproducibility fingerprint and the evidence-separation
  rules. Editing a prompt no benchmark uses changes no fingerprint (ADR-0028 §1).
* ``pack_sha256`` is the same computation over the whole pack. It is recorded on a run as
  **provenance only** — "which pack was installed" — and is never hashed into a fingerprint.

Both cross an application boundary inside ``capability.evidence``, so their determinism is a
contract and is golden-tested (ADR-0028 §3), not merely exercised.

Rendering is Jinja2 with ``StrictUndefined`` and no filesystem or network access in the
environment: a referenced-but-unsupplied variable is an error, never an empty string. Unknown
variables supplied by a caller are an error too — that is how a renamed variable is caught rather
than silently ignored.

Loading and validation happen **once at startup** (:func:`load_pack` from
:func:`freeweight.bootstrap.bootstrap`); a malformed prompt is a startup failure, never a surprise
in the middle of a run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import NotFoundError, ValidationError, canonical_json, sha256_of
from jinja2 import StrictUndefined, TemplateError, meta
from jinja2.sandbox import SandboxedEnvironment

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "PACK_ROOT",
    "PROMPT_RECORD_SCHEMA_VERSION",
    "ManifestDrift",
    "PromptLibrary",
    "PromptNotFound",
    "PromptPackInvalid",
    "PromptRecord",
    "PromptReference",
    "PromptRenderError",
    "PromptVariableError",
    "RenderedPrompt",
    "VariableSpec",
    "build_manifest",
    "load_pack",
    "load_record",
    "pack_hash",
    "prompt_record_hash",
    "prompt_subset_hash",
    "write_manifest",
]

PROMPT_RECORD_SCHEMA_VERSION = "1.0"
"""The record format this build understands (prompt standards §2.1, ``schema_version``)."""

PACK_ROOT = Path(__file__).resolve().parent.parent / "prompts"
"""FreeWeight's own pack, shipped inside the package. The one place a default path appears."""

_VARIABLE_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


class PromptPackInvalid(ValidationError):
    """A record or a manifest is malformed, inconsistent, or not the schema version we speak.

    Raised only during loading, which is why it is a *startup* failure: prompt standards §5 says
    the whole pack is validated once, and a malformed prompt discovered mid-run would have already
    produced measurements nobody can reproduce.
    """

    code: ClassVar[str] = "PROMPT_INVALID"


class PromptNotFound(NotFoundError):
    """No prompt with this id, or no such version of it.

    Its own code rather than the generic ``NOT_FOUND``: a benchmark that asks for a prompt version
    the installed pack does not have needs to be told which of the two things is missing, and spec
    §13's ``PROMPT_INVALID`` means "malformed", which this is not.
    """

    code: ClassVar[str] = "PROMPT_NOT_FOUND"


class PromptVariableError(ValidationError):
    """A required variable is missing, an unknown one was supplied, or a value is out of range."""

    code: ClassVar[str] = "PROMPT_INVALID"


class PromptRenderError(ValidationError):
    """The template itself failed: a syntax error, or ``StrictUndefined`` firing."""

    code: ClassVar[str] = "PROMPT_INVALID"


@dataclass(frozen=True, slots=True)
class VariableSpec:
    """One declared template variable (prompt standards §2.1, ``variables``).

    Attributes:
        name: The variable's name as it appears in the template.
        type_name: ``"string"``, ``"integer"``, ``"number"`` or ``"boolean"``.
        required: Whether a caller must supply it. A non-required variable must declare a
            ``default``, because ``StrictUndefined`` gives a missing optional variable nothing to
            render.
        description: What it is for. Mandatory: an undocumented variable is one nobody can supply
            correctly from the record alone.
        default: The value used when the caller supplies none.
        minimum: Inclusive lower bound for numeric types, or ``None``.
        maximum: Inclusive upper bound for numeric types, or ``None``.
    """

    name: str
    type_name: str
    required: bool
    description: str
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None

    def coerce(self, value: Any, *, prompt_id: str) -> Any:  # noqa: ANN401 — a JSON value
        """Validate one supplied value against this declaration and return it unchanged.

        Args:
            value: What the caller supplied.
            prompt_id: The owning record, for the error message.

        Returns:
            ``value``, unchanged. Nothing is coerced — a string where an integer was declared is a
            caller's bug, and quietly parsing it would hide the day the caller starts passing
            ``"8"`` for one model and ``8`` for another.

        Raises:
            PromptVariableError: The value is of the wrong type, or outside a declared bound.
        """
        expected = _VARIABLE_TYPES[self.type_name]
        # bool is a subclass of int in Python, so an integer variable would silently accept True.
        if isinstance(value, bool) is not (self.type_name == "boolean") or not isinstance(
            value, expected
        ):
            raise PromptVariableError(
                f"Prompt {prompt_id!r} variable {self.name!r} is declared {self.type_name!r}; "
                f"got {type(value).__name__}.",
                details={"prompt_id": prompt_id, "variable": self.name, "type": self.type_name},
            )
        if isinstance(value, int | float) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise PromptVariableError(
                    f"Prompt {prompt_id!r} variable {self.name!r} must be at least "
                    f"{self.minimum}; got {value}.",
                    details={"prompt_id": prompt_id, "variable": self.name, "min": self.minimum},
                )
            if self.maximum is not None and value > self.maximum:
                raise PromptVariableError(
                    f"Prompt {prompt_id!r} variable {self.name!r} must be at most "
                    f"{self.maximum}; got {value}.",
                    details={"prompt_id": prompt_id, "variable": self.name, "max": self.maximum},
                )
        return value


@dataclass(frozen=True, slots=True)
class PromptRecord:
    """One versioned prompt, exactly as its JSON file declares it.

    Attributes:
        prompt_id: Dotted, stable, unique within the pack. Never renamed — a rename is a new
            prompt.
        version: The prompt's own semantic version, independent of ``schema_version``.
        system: The system turn, or ``None`` when the prompt has none.
        template: The user turn's Jinja2 template.
        variables: Every variable the templates use, by name.
        purpose: One sentence saying what this prompt is for.
        source: ``"pack"`` for a shipped record, ``"user_override"`` for one loaded from the user's
            config directory (prompt standards §6). Recorded on every sample that used it, because
            an overridden prompt invalidates comparison with results from the shipped one.
        body: The parsed file, retained so ``sha256`` hashes what was actually installed rather
            than a reconstruction of it.
    """

    prompt_id: str
    version: str
    system: str | None
    template: str
    variables: Mapping[str, VariableSpec]
    purpose: str
    source: str
    body: Mapping[str, Any]

    @property
    def sha256(self) -> str:
        """``sha256:``-prefixed hash of this record's canonical JSON."""
        return prompt_record_hash(self.body)

    @property
    def reference(self) -> PromptReference:
        """This record as the ``(prompt_id, version, sha256)`` triple a manifest declares."""
        return PromptReference(prompt_id=self.prompt_id, version=self.version, sha256=self.sha256)

    def render(self, variables: Mapping[str, Any]) -> RenderedPrompt:
        """Validate ``variables`` and render this record's system and user text.

        Args:
            variables: The caller's values, by variable name.

        Returns:
            The rendering, with both hashes attached.

        Raises:
            PromptVariableError: A required variable is missing, an unknown variable was supplied,
                or a value is of the wrong type or outside its declared bounds. Unknown variables
                are refused rather than ignored, which is how a renamed variable is caught.
            PromptRenderError: The template failed to render — a syntax error, or
                ``StrictUndefined`` firing on something ``variables`` validation could not see.
        """
        unknown = sorted(set(variables) - set(self.variables))
        if unknown:
            raise PromptVariableError(
                f"Prompt {self.prompt_id!r} does not declare {unknown}; it declares "
                f"{sorted(self.variables)}.",
                details={"prompt_id": self.prompt_id, "unknown": unknown},
            )
        resolved: dict[str, Any] = {}
        for name, spec in self.variables.items():
            if name in variables:
                resolved[name] = spec.coerce(variables[name], prompt_id=self.prompt_id)
            elif spec.required:
                raise PromptVariableError(
                    f"Prompt {self.prompt_id!r} requires variable {name!r}: {spec.description}",
                    details={"prompt_id": self.prompt_id, "variable": name},
                )
            else:
                resolved[name] = spec.default
        environment = _environment()
        try:
            system = (
                None
                if self.system is None
                else environment.from_string(self.system).render(**resolved)
            )
            user = environment.from_string(self.template).render(**resolved)
        except (TemplateError, TypeError) as exc:
            # ``TypeError`` is caught alongside Jinja2's own errors because a template that tries
            # to ``include`` a file raises one from the loader-less environment rather than a
            # ``TemplateError``. Every exception from inside ``from_string(...).render(...)`` is a
            # problem with the template, and a caller that receives a bare ``TypeError`` from a
            # user's goal pack cannot tell what refused it.
            raise PromptRenderError(
                f"Prompt {self.prompt_id!r} v{self.version} failed to render: {exc}",
                details={"prompt_id": self.prompt_id, "version": self.version},
            ) from exc
        return RenderedPrompt(
            prompt_id=self.prompt_id,
            version=self.version,
            sha256=self.sha256,
            system=system,
            user=user,
        )


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """One rendering of one record, carrying everything a sample has to record.

    ``rendered_sha256`` is over the system and user text together, so two renderings that differ
    only in the system turn hash differently. It is a cross-application determinism contract
    (ADR-0028 §3): the same record and the same variables produce the same bytes in every process.
    """

    prompt_id: str
    version: str
    sha256: str
    system: str | None
    user: str

    @property
    def rendered_sha256(self) -> str:
        """``sha256:``-prefixed hash of the rendered system and user text."""
        return f"sha256:{sha256_of(canonical_json({'system': self.system, 'user': self.user}))}"


@dataclass(frozen=True, slots=True)
class PromptReference:
    """The ``(prompt_id, version, sha256)`` triple a manifest and a subset hash are built from."""

    prompt_id: str
    version: str
    sha256: str

    def as_json(self) -> dict[str, str]:
        """Render as the object a benchmark manifest's ``prompt_ids`` entry uses."""
        return {"prompt_id": self.prompt_id, "version": self.version, "sha256": self.sha256}


def _environment() -> SandboxedEnvironment:
    """Build the one Jinja2 environment prompts render in.

    Three properties, and every one of them is load-bearing once *user-authored* content renders
    here — which it does from Phase 8A onward, and which is why spec §14 calls goal content
    "untrusted input to FreeWeight's own renderer":

    * **Sandboxed.** :class:`~jinja2.sandbox.SandboxedEnvironment` refuses attribute access to
      dunder attributes, so ``{{ ''.__class__.__mro__ }}`` — the first step of every Jinja2
      escape to ``open`` and to the network — raises instead of resolving. A goal pack imported
      from another machine is somebody else's file.
    * **No loader.** A template cannot ``include`` or ``extend``, so it has no filesystem reach of
      its own either.
    * **``StrictUndefined``** (prompt standards §2.1): a referenced-but-unsupplied variable is an
      error, never an empty string.

    ``autoescape=False`` because a prompt is plain text sent to a model, and HTML-escaping it
    would silently change the instruction.
    """
    return SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)  # noqa: S701 — plain text, see docstring


def prompt_record_hash(body: Mapping[str, Any]) -> str:
    """Return the ``sha256:``-prefixed hash of one prompt record.

    Over :func:`~baseaicore.canonical_json`, never over the file's bytes: re-indenting a record
    must not separate results that used identical prompt text.

    Args:
        body: The record exactly as parsed from its file.

    Returns:
        ``"sha256:"`` followed by 64 lowercase hex characters.
    """
    return f"sha256:{sha256_of(canonical_json(body))}"


def _triples(references: Iterable[PromptReference]) -> list[list[str]]:
    """Return the sorted ``(prompt_id, version, sha256)`` triples the two pack hashes hash."""
    return sorted(
        [reference.prompt_id, reference.version, reference.sha256] for reference in references
    )


def prompt_subset_hash(references: Iterable[PromptReference]) -> str:
    """Return the ``sha256:``-prefixed hash over an arbitrary subset of a pack.

    **This is the fingerprint input**, not :func:`pack_hash`. A benchmark manifest declares the
    prompts it uses and hashes exactly those, so editing a prompt no benchmark uses separates
    nothing and editing one a benchmark uses separates that benchmark's results (ADR-0028 §1).

    Args:
        references: The prompts in the subset, in any order — the triples are sorted, so the
            result does not depend on declaration order.

    Returns:
        ``"sha256:"`` followed by 64 lowercase hex characters. An empty subset hashes the empty
        list, which is a real, stable value: "this benchmark uses no prompt" is a fact, not a
        missing one.
    """
    return f"sha256:{sha256_of(canonical_json(_triples(references)))}"


def pack_hash(references: Iterable[PromptReference]) -> str:
    """Return the ``sha256:``-prefixed hash over a whole pack.

    Identical arithmetic to :func:`prompt_subset_hash` over every record. Recorded on a run as
    provenance and deliberately **not** a fingerprint input (ADR-0028 §1).
    """
    return prompt_subset_hash(references)


def _require(body: Mapping[str, Any], field: str, path: Path) -> Any:  # noqa: ANN401 — JSON
    """Return ``body[field]``, or refuse with the file that is missing it."""
    if field not in body or body[field] in (None, "") and field != "system":
        raise PromptPackInvalid(
            f"Prompt record {path.name} is missing required field {field!r}.",
            details={"file": str(path), "field": field},
        )
    return body[field]


def _parse_variables(body: Mapping[str, Any], path: Path) -> dict[str, VariableSpec]:
    """Parse and validate the ``variables`` block of one record."""
    raw = body.get("variables", {})
    if not isinstance(raw, dict):
        raise PromptPackInvalid(
            f"Prompt record {path.name} declares a non-object 'variables' block.",
            details={"file": str(path)},
        )
    specs: dict[str, VariableSpec] = {}
    for name, declaration in raw.items():
        if not isinstance(declaration, dict):
            raise PromptPackInvalid(
                f"Prompt record {path.name} declares variable {name!r} as a non-object.",
                details={"file": str(path), "variable": name},
            )
        type_name = str(declaration.get("type", ""))
        if type_name not in _VARIABLE_TYPES:
            raise PromptPackInvalid(
                f"Prompt record {path.name} declares variable {name!r} with unknown type "
                f"{type_name!r}; known types are {sorted(_VARIABLE_TYPES)}.",
                details={"file": str(path), "variable": name, "type": type_name},
            )
        description = str(declaration.get("description", ""))
        if not description:
            raise PromptPackInvalid(
                f"Prompt record {path.name} variable {name!r} has no description; a variable "
                "nobody can interpret from the record is a variable nobody can supply correctly.",
                details={"file": str(path), "variable": name},
            )
        required = bool(declaration.get("required", False))
        if not required and "default" not in declaration:
            raise PromptPackInvalid(
                f"Prompt record {path.name} variable {name!r} is optional but declares no "
                "default; StrictUndefined would fail on it the first time it is omitted.",
                details={"file": str(path), "variable": name},
            )
        specs[name] = VariableSpec(
            name=name,
            type_name=type_name,
            required=required,
            description=description,
            default=declaration.get("default"),
            minimum=declaration.get("min"),
            maximum=declaration.get("max"),
        )
    return specs


def _declared_and_used(record: PromptRecord, path: Path) -> None:
    """Refuse a record whose declarations and templates disagree.

    Both directions are checked (prompt standards §7): a template variable that is not declared
    cannot be documented or type-checked, and a declared variable that no template uses is dead
    weight that a caller will eventually supply and wonder why nothing changed.
    """
    environment = _environment()
    used: set[str] = set()
    for source in (record.system, record.template):
        if source is None:
            continue
        try:
            used |= meta.find_undeclared_variables(environment.parse(source))
        except TemplateError as exc:
            raise PromptPackInvalid(
                f"Prompt record {path.name} has a template that does not parse: {exc}",
                details={"file": str(path)},
            ) from exc
    undeclared = sorted(used - set(record.variables))
    unused = sorted(set(record.variables) - used)
    if undeclared or unused:
        raise PromptPackInvalid(
            f"Prompt record {path.name} declares {sorted(record.variables)} but uses "
            f"{sorted(used)}; undeclared={undeclared}, unused={unused}.",
            details={"file": str(path), "undeclared": undeclared, "unused": unused},
        )


def load_record(path: Path, *, source: str = "pack") -> PromptRecord:
    """Parse and validate one prompt record file.

    The public entry point to the same loader :func:`load_pack` uses record by record. A goal
    pack's tasks and its judge rubric are prompt records
    ([ADR-0031 §6](../../../../docs/adr/0031-user-defined-goal-benchmarks.md)) but they do not
    live in a pack with a manifest, so they are loaded one at a time — through *this* function
    rather than through a second parser, which is what makes "user-authored goal content renders
    under the same ``StrictUndefined`` sandbox as shipped prompts" (spec §14) true by construction
    rather than by intention.

    Args:
        path: The record file.
        source: What to mark the record as — ``"pack"``, ``"user_override"`` or ``"goal_pack"``.

    Returns:
        The parsed record.

    Raises:
        PromptPackInvalid: The file is not valid JSON, is missing a required field, declares an
            unsupported ``schema_version``, or its declarations and templates disagree.
    """
    return _load_record(path, source=source)


def _load_record(path: Path, *, source: str) -> PromptRecord:
    """Parse and validate one record file.

    Raises:
        PromptPackInvalid: The file is not valid JSON, is missing a required field, declares an
            unsupported ``schema_version``, or its declarations and templates disagree.
    """
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptPackInvalid(
            f"Prompt record {path.name} could not be read: {exc}", details={"file": str(path)}
        ) from exc
    if not isinstance(body, dict):
        raise PromptPackInvalid(
            f"Prompt record {path.name} is not a JSON object.", details={"file": str(path)}
        )
    schema_version = str(_require(body, "schema_version", path))
    if schema_version != PROMPT_RECORD_SCHEMA_VERSION:
        raise PromptPackInvalid(
            f"Prompt record {path.name} declares record schema {schema_version!r}; this build "
            f"speaks {PROMPT_RECORD_SCHEMA_VERSION!r}.",
            details={"file": str(path), "schema_version": schema_version},
        )
    metadata = body.get("metadata")
    if not isinstance(metadata, dict) or not str(metadata.get("change_reason", "")):
        raise PromptPackInvalid(
            f"Prompt record {path.name} has no metadata.change_reason; every version bump states "
            "why it happened (prompt standards §2.1).",
            details={"file": str(path)},
        )
    system = body.get("system")
    record = PromptRecord(
        prompt_id=str(_require(body, "prompt_id", path)),
        version=str(_require(body, "version", path)),
        system=None if system is None else str(system),
        template=str(_require(body, "template", path)),
        variables=_parse_variables(body, path),
        purpose=str(_require(body, "purpose", path)),
        source=source,
        body=body,
    )
    _declared_and_used(record, path)
    return record


class PromptLibrary:
    """One loaded, validated pack: every record, indexed by id and version.

    Built once at startup by :func:`load_pack` and shared read-only thereafter. Immutable in
    practice: nothing here mutates the index after construction, so the instance is safe to hand
    to the scheduler thread and to every request at the same time.

    Args:
        pack_id: The pack's identity, from its manifest.
        pack_version: The pack's version, from its manifest.
        records: Every record in the pack, overrides applied.
        shipped: The records as installed, before any override. Kept so a benchmark manifest can
            be verified against what shipped rather than against what the user replaced: an
            override *deliberately* differs from the manifest (prompt standards §6), and a
            verification that compared against the effective record would make dropping one file
            into the override directory stop the whole application from starting.

    Raises:
        PromptPackInvalid: Two records declare the same ``(prompt_id, version)``.
    """

    __slots__ = ("_by_id", "_shipped", "pack_id", "pack_version")

    def __init__(
        self,
        *,
        pack_id: str,
        pack_version: str,
        records: Sequence[PromptRecord],
        shipped: Sequence[PromptRecord] = (),
    ) -> None:
        """Index ``records`` by id and version."""
        by_id: dict[str, dict[str, PromptRecord]] = {}
        for record in records:
            versions = by_id.setdefault(record.prompt_id, {})
            if record.version in versions:
                raise PromptPackInvalid(
                    f"Prompt {record.prompt_id!r} version {record.version!r} is declared twice.",
                    details={"prompt_id": record.prompt_id, "version": record.version},
                )
            versions[record.version] = record
        self.pack_id = pack_id
        self.pack_version = pack_version
        self._by_id = by_id
        self._shipped = {record.prompt_id: record for record in (shipped or records)}

    @property
    def overridden_ids(self) -> tuple[str, ...]:
        """Every prompt id a user override replaced, sorted.

        Recorded on every run that rendered one and refused outright unless the run asked for it
        (prompt standards §6): an overridden prompt invalidates comparison with results produced
        by the shipped one, so a run has to say so before it is allowed to happen.
        """
        return tuple(
            sorted(
                prompt_id
                for prompt_id, versions in self._by_id.items()
                if any(record.source == "user_override" for record in versions.values())
            )
        )

    def shipped_references(
        self, wanted: Iterable[tuple[str, str | None]]
    ) -> tuple[PromptReference, ...]:
        """Return the triples for the records as *installed*, ignoring any override.

        Args:
            wanted: The ``(prompt_id, version)`` pairs to resolve.

        Returns:
            One reference per pair, taken from the shipped record where one exists and from the
            effective record otherwise (a goal pack's own prompts ship with no installed twin).

        Raises:
            PromptNotFound: One of the pairs names no record at all.
        """
        resolved: list[PromptReference] = []
        for prompt_id, version in wanted:
            shipped = self._shipped.get(prompt_id)
            if shipped is not None and (version is None or shipped.version == version):
                resolved.append(shipped.reference)
            else:
                resolved.append(self.get(prompt_id, version=version).reference)
        return tuple(resolved)

    def ids(self) -> tuple[str, ...]:
        """Every prompt id in the pack, sorted."""
        return tuple(sorted(self._by_id))

    def all_records(self) -> tuple[PromptRecord, ...]:
        """Every record, ordered by ``(prompt_id, version)``."""
        return tuple(
            self._by_id[prompt_id][version]
            for prompt_id in sorted(self._by_id)
            for version in sorted(self._by_id[prompt_id])
        )

    def get(self, prompt_id: str, *, version: str | None = None) -> PromptRecord:
        """Return one record; the highest version when ``version`` is ``None``.

        Args:
            prompt_id: The dotted prompt id.
            version: The exact version, or ``None`` for the latest installed.

        Returns:
            The record.

        Raises:
            PromptNotFound: No such prompt, or no such version of it. The message names what is
                installed, because the overwhelmingly likely cause is a typo or a pack that was
                not rebuilt.
        """
        versions = self._by_id.get(prompt_id)
        if versions is None:
            raise PromptNotFound(
                f"No prompt {prompt_id!r} in pack {self.pack_id!r}; it holds {list(self.ids())}.",
                details={"prompt_id": prompt_id, "available": list(self.ids())},
            )
        if version is None:
            return versions[max(versions, key=_version_key)]
        record = versions.get(version)
        if record is None:
            raise PromptNotFound(
                f"Prompt {prompt_id!r} has no version {version!r}; installed versions are "
                f"{sorted(versions)}.",
                details={"prompt_id": prompt_id, "version": version, "available": sorted(versions)},
            )
        return record

    def render(
        self, prompt_id: str, variables: Mapping[str, Any], *, version: str | None = None
    ) -> RenderedPrompt:
        """Render one prompt. See :meth:`PromptRecord.render` for what it refuses."""
        return self.get(prompt_id, version=version).render(variables)

    def references(self, wanted: Iterable[tuple[str, str | None]]) -> tuple[PromptReference, ...]:
        """Return the triples for ``(prompt_id, version)`` pairs, in declaration order.

        Args:
            wanted: The prompts to resolve; ``None`` as a version means the latest installed.

        Returns:
            One reference per pair.

        Raises:
            PromptNotFound: One of the pairs names no installed record.
        """
        return tuple(
            self.get(prompt_id, version=version).reference for prompt_id, version in wanted
        )

    def pack_hash(self) -> str:
        """The whole pack's hash — provenance on a run, never a fingerprint input."""
        return pack_hash(record.reference for record in self.all_records())


def _version_key(version: str) -> tuple[int, ...]:
    """Sort key for a semantic version, falling back to zeros for a non-numeric component."""
    parts: list[int] = []
    for component in version.split("."):
        parts.append(int(component) if component.isdigit() else 0)
    return tuple(parts)


def load_pack(root: Path = PACK_ROOT, *, override_root: Path | None = None) -> PromptLibrary:
    """Load, validate and index a prompt pack from disk.

    Called once at startup. Every record under ``root`` is parsed and validated, the manifest is
    checked against what was actually found, and a disagreement is a startup failure — a pack
    whose manifest is stale has hashes that do not describe the prompts it will render, and those
    hashes go into evidence another application reads.

    Args:
        root: The pack directory, containing ``manifest.json`` and record files.
        override_root: The user's override directory (prompt standards §6), or ``None``. An
            override replaces the shipped record of the same ``(prompt_id, version)`` and is
            marked ``source="user_override"`` so every record that used it says so.

    Returns:
        The loaded library.

    Raises:
        PromptPackInvalid: The manifest is missing or malformed; a record is malformed; the
            manifest's recorded hashes do not match the records on disk; or a record on disk is
            absent from the manifest, or vice versa.
    """
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptPackInvalid(
            f"Prompt pack manifest {manifest_path} could not be read: {exc}. Rebuild it before "
            "starting: a pack whose manifest cannot be read cannot be attributed to a result.",
            details={"file": str(manifest_path)},
        ) from exc
    if not isinstance(manifest, dict):
        raise PromptPackInvalid(
            f"Prompt pack manifest {manifest_path} is not a JSON object.",
            details={"file": str(manifest_path)},
        )
    shipped = [
        _load_record(path, source="pack")
        for path in sorted(root.rglob("*.json"))
        if path != manifest_path
    ]
    # Checked against the *shipped* records, before overrides are applied: the manifest describes
    # what was installed, and an override deliberately differs from it (prompt standards §6).
    _check_manifest(manifest, shipped, manifest_path)
    records = list(shipped)
    if override_root is not None and override_root.is_dir():
        overrides = {
            record.prompt_id: record
            for record in (
                _load_record(path, source="user_override")
                for path in sorted(override_root.glob("*.json"))
            )
        }
        records = [overrides.pop(record.prompt_id, record) for record in records]
        records.extend(overrides.values())
    return PromptLibrary(
        pack_id=str(manifest.get("pack_id", "")),
        pack_version=str(manifest.get("pack_version", "")),
        records=records,
        shipped=shipped,
    )


def _check_manifest(
    manifest: Mapping[str, Any], shipped: Sequence[PromptRecord], path: Path
) -> None:
    """Refuse a manifest that does not describe the records installed beside it.

    Only shipped records are compared: a user override deliberately differs from the manifest, and
    is marked on every result that used it instead (prompt standards §6).
    """
    declared = manifest.get("prompts")
    if not isinstance(declared, list):
        raise PromptPackInvalid(
            f"Prompt pack manifest {path} declares no 'prompts' list.", details={"file": str(path)}
        )
    expected = _triples(record.reference for record in shipped)
    found = sorted(
        [str(item.get("prompt_id")), str(item.get("version")), str(item.get("sha256"))]
        for item in declared
        if isinstance(item, dict)
    )
    if expected != found:
        raise PromptPackInvalid(
            f"Prompt pack manifest {path} is stale: it declares {found} but the pack holds "
            f"{expected}. Regenerate the manifest — a hash that does not describe the installed "
            "prompt is a hash that separates the wrong results.",
            details={"file": str(path), "declared": found, "found": expected},
        )
    recorded = str(manifest.get("pack_sha256", ""))
    actual = pack_hash(record.reference for record in shipped)
    if recorded != actual:
        raise PromptPackInvalid(
            f"Prompt pack manifest {path} records pack_sha256 {recorded!r}; the pack hashes to "
            f"{actual!r}.",
            details={"file": str(path), "recorded": recorded, "actual": actual},
        )


@dataclass(frozen=True, slots=True)
class ManifestDrift:
    """The difference between the manifest on disk and the records installed beside it.

    Returned rather than raised, because ``freeweight prompts build`` has two jobs — write the
    correct manifest, and *report* whether the committed one was already correct — and CI needs
    the second without the first (prompt standards §3, "validated in CI").

    Attributes:
        pack_root: The pack this describes.
        added: ``(prompt_id, version)`` pairs on disk that the manifest does not declare.
        removed: Pairs the manifest declares that are no longer on disk.
        changed: Pairs whose record hash differs from the one the manifest records.
        pack_sha256_changed: Whether the pack hash itself moved.
    """

    pack_root: Path
    added: tuple[tuple[str, str], ...]
    removed: tuple[tuple[str, str], ...]
    changed: tuple[tuple[str, str], ...]
    pack_sha256_changed: bool

    @property
    def is_current(self) -> bool:
        """Whether the committed manifest already describes the installed records exactly."""
        return not (self.added or self.removed or self.changed or self.pack_sha256_changed)


def build_manifest(
    root: Path = PACK_ROOT, *, generated_at: str | None = None
) -> tuple[dict[str, Any], ManifestDrift]:
    """Recompute a pack's manifest from the records on disk, and say what moved.

    The counterpart to :func:`_check_manifest`: that function refuses a stale manifest at startup,
    this one produces the manifest that would satisfy it. One arithmetic, written once — a builder
    that hashed differently from the validator would produce a pack that fails to load the moment
    it is built (prompt standards §3).

    ``pack_id``, ``pack_version`` and ``schema_version`` are carried over from the existing
    manifest where there is one: they are the pack's identity and its owner's decision, and a
    rebuild is not the moment to invent them. ``generated_at`` is taken from the caller so the
    output is deterministic under test; ``None`` keeps whatever the existing manifest recorded, or
    the epoch for a pack that has none — a rebuild never reaches for the clock on its own, because
    a timestamp that changes on every invocation makes ``prompts build --check`` unable to compare
    two manifests at all.

    Args:
        root: The pack directory holding ``manifest.json`` and the record files.
        generated_at: The RFC 3339 instant to stamp, or ``None`` to keep the existing one.

    Returns:
        ``(manifest, drift)`` — the manifest body that describes what is installed, and how it
        differs from the one currently on disk.

    Raises:
        PromptPackInvalid: A record file is malformed. A pack that cannot be read cannot be
            described, and writing a manifest over an unreadable pack would bless the breakage.
    """
    manifest_path = root / "manifest.json"
    existing: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            existing = parsed
    shipped = [
        _load_record(path, source="pack")
        for path in sorted(root.rglob("*.json"))
        if path != manifest_path
    ]
    references = [record.reference for record in shipped]
    manifest: dict[str, Any] = {
        "pack_id": str(existing.get("pack_id", root.name)),
        "pack_version": str(existing.get("pack_version", "1.0.0")),
        "schema_version": str(existing.get("schema_version", PROMPT_RECORD_SCHEMA_VERSION)),
        "generated_at": str(
            generated_at
            if generated_at is not None
            else existing.get("generated_at", "1970-01-01T00:00:00Z")
        ),
        "prompts": [reference.as_json() for reference in sorted(references, key=_reference_key)],
        "pack_sha256": pack_hash(references),
    }
    return manifest, _drift(existing, manifest, root)


def _reference_key(reference: PromptReference) -> tuple[str, str]:
    """Order manifest entries by ``(prompt_id, version)`` so a rebuild is byte-stable."""
    return (reference.prompt_id, reference.version)


def _drift(existing: Mapping[str, Any], rebuilt: Mapping[str, Any], root: Path) -> ManifestDrift:
    """Compare a committed manifest with a freshly computed one."""

    def index(body: Mapping[str, Any]) -> dict[tuple[str, str], str]:
        declared = body.get("prompts")
        entries = declared if isinstance(declared, list) else []
        return {
            (str(entry.get("prompt_id")), str(entry.get("version"))): str(entry.get("sha256"))
            for entry in entries
            if isinstance(entry, dict)
        }

    before, after = index(existing), index(rebuilt)
    return ManifestDrift(
        pack_root=root,
        added=tuple(sorted(set(after) - set(before))),
        removed=tuple(sorted(set(before) - set(after))),
        changed=tuple(sorted(key for key in set(before) & set(after) if before[key] != after[key])),
        pack_sha256_changed=str(existing.get("pack_sha256", "")) != str(rebuilt["pack_sha256"]),
    )


def write_manifest(manifest: Mapping[str, Any], root: Path = PACK_ROOT) -> Path:
    """Write ``manifest.json`` into ``root`` and return the path written.

    Two trailing spaces of indentation and a final newline, matching every other JSON file in the
    pack: the manifest is reviewed in diffs, and a one-line file makes a single changed hash look
    like a rewritten pack.

    Args:
        manifest: The body from :func:`build_manifest`.
        root: The pack directory.

    Returns:
        The path that was written.
    """
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path
