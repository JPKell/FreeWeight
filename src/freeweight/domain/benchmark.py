"""freeweight.domain.benchmark — what a benchmark *is*, independent of how it is run or stored.

Four value objects and two protocols:

* :class:`MetricDefinition` — one number a benchmark produces, with its unit, its direction and
  how repetitions combine. Units live in the name (coding standards); direction is declared, never
  guessed from the key.
* :class:`BenchmarkCase` — one prompt-and-expectation pair. The smallest unit that produces a
  sample.
* :class:`BenchmarkTest` — a named group of cases sharing one scorer.
* :class:`BenchmarkManifest` — the declarative record of benchmark catalog §5, plus its hash. The
  hash is what separates results between suite versions, so it is computed from canonical JSON of
  the manifest body and never from a file's bytes (a reformatted file is the same benchmark).
* :class:`Benchmark` — the runnable protocol: a manifest plus its tests.
* :class:`BenchmarkRegistry` — the lookup the run engine uses, populated by the composition root.

Prompt *records* are still not here — they are data, loaded and hashed by
:mod:`freeweight.services.prompts` — but a manifest now carries the ``prompt_ids`` it declares and
the ``prompt_subset_hash`` over exactly those
([ADR-0028](../../../../docs/adr/0028-prompt-pack-granularity.md)). The subset hash is a
*fingerprint input*; the pack hash is not. A suite that declares no prompts (``native.echo``,
whose cases carry literal text) has no subset hash and separates nothing, which is what keeps a
self-test self-contained.

Dataset installation is still absent; it arrives with the external adapters.

Pure domain: stdlib and :mod:`baseaicore` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from baseaicore import NotFoundError, canonical_json, sha256_of

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from freeweight.domain.scoring import Scorer

__all__ = [
    "Benchmark",
    "BenchmarkCase",
    "BenchmarkManifest",
    "BenchmarkNotFound",
    "BenchmarkRegistry",
    "BenchmarkTest",
    "MetricDefinition",
    "compute_manifest_hash",
]


class BenchmarkNotFound(NotFoundError):
    """No benchmark suite (or test within one) matches the given key.

    Its own stable code per spec §13 rather than the generic ``NOT_FOUND``: a caller that asked
    for ``native.eco`` needs to distinguish "no such suite" from "no such run".
    """

    code: ClassVar[str] = "BENCHMARK_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """One number a benchmark produces, fully described.

    Attributes:
        key: Stable metric name, identical across every run of this benchmark. The unit belongs
            in the name wherever the number has one (``decode_tokens_per_second``, not ``speed``).
        unit: The unit as it is shown to a person: ``"ratio"``, ``"ms"``, ``"tokens/s"``,
            ``"bytes"``, ``"count"``.
        higher_is_better: Declared, never inferred. A comparison that guesses direction from a key
            gets latency backwards the first time someone adds a metric whose name does not
            contain the word it was matching on.
        aggregation: How samples combine into the run-level value: one of ``mean``, ``median``,
            ``p50``, ``p95``, ``p99``, ``min``, ``max``, ``sum``, ``count``, ``ratio``, ``raw``
            (data model §2, ``metric_values``).
        description: One sentence a tooltip can show, so every metric can reveal its definition
            (UI standards §5).
    """

    key: str
    unit: str
    higher_is_better: bool
    aggregation: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One prompt and what a correct answer to it looks like.

    Attributes:
        case_id: Stable within its test, and stable across runs — it is half of ``samples``'
            uniqueness constraint and the thing a user compares between two runs.
        ordinal: The case's position in its test's declared order, from 0. Recorded on the sample
            so a run whose case order was randomized can still be read back in declaration order.
        prompt: The rendered user-turn text sent to the model. Rendered, not templated: by the
            time a case exists the prompt library has already produced its final text, so the
            hash on the sample is over exactly what was sent.
        expectation: What the scorer compares against. Its shape is the scorer's business — this
            module deliberately does not interpret it.
        metadata: Anything the benchmark wants recorded with the sample that is neither prompt nor
            expectation.
        system_prompt: The system-turn text, or ``None`` for a case that has none.
        prompt_id: The prompt record this case was rendered from, or ``None`` for a suite whose
            cases carry literal text (``native.echo``). Stored on the sample so a result can name
            the exact prompt that produced it (prompt standards §4).
        prompt_version: That record's version, or ``None``.
        required_context_tokens: The served context this case needs. A case that needs more than
            the run's served context is **skipped with a recorded reason** rather than sent and
            failed — benchmark catalog §3.1's "only those the model supports", made a property of
            the case rather than of the suite, since the same suite runs against models with
            different contexts.
    """

    case_id: str
    ordinal: int
    prompt: str
    expectation: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    system_prompt: str | None = None
    prompt_id: str | None = None
    prompt_version: str | None = None
    required_context_tokens: int | None = None


@runtime_checkable
class BenchmarkTest(Protocol):
    """A named group of cases sharing one scorer and one set of metrics."""

    @property
    def key(self) -> str:
        """Stable within the suite; half of ``benchmark_tests``' uniqueness constraint."""
        ...

    @property
    def name(self) -> str:
        """Human-readable name, shown in the UI."""
        ...

    @property
    def category(self) -> str:
        """The benchmark catalog §2 category this test contributes to."""
        ...

    @property
    def scorer(self) -> Scorer:
        """The scorer every case in this test is scored by."""
        ...

    @property
    def measurement_class(self) -> str:
        """``cold``, ``warm``, ``cache_reused`` or ``n/a`` (benchmark catalog §3.1).

        Declared by the test, stored on its ``run_tests`` row, and read by
        :mod:`freeweight.domain.aggregation`, which refuses to combine tests of different classes
        into one run-level metric. A test that does not care about model state declares ``n/a``;
        it never declares ``warm`` by default, because a default here would silently make a cold
        measurement comparable to a warm one.
        """
        ...

    @property
    def streaming(self) -> bool:
        """Whether this test's cases are executed through :meth:`~modelrack.Provider.stream`.

        ``True`` is what makes ``client_ttft_ms`` and inter-chunk timings exist at all: a blocking
        call has no first-token moment to observe. It is declared per test rather than inferred,
        because streaming and non-streaming calls are different measurements of the same model and
        a run must record which one it made.
        """
        ...

    @property
    def metrics(self) -> Sequence[MetricDefinition]:
        """The metrics this test produces."""
        ...

    @property
    def requires(self) -> Mapping[str, Any]:
        """Preconditions, as in a manifest's ``requires`` block.

        Checked before the test runs; an unmet requirement skips the test with a recorded reason
        rather than failing the run (spec §13).
        """
        ...

    def cases(self) -> Iterator[BenchmarkCase]:
        """Yield this test's cases in declaration order.

        Returns:
            A fresh iterator each call — the run engine iterates cases more than once (once to
            count, once to execute) and a generator consumed by the count would leave the
            execution with nothing.
        """
        ...


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    """The declarative record benchmark catalog §5 defines, as loaded from ``manifest.json``.

    Attributes:
        key: The suite key, e.g. ``"native.echo"``.
        name: Human-readable suite name.
        version: The suite's own version. A version change **separates** results; it never
            invalidates them (spec §19).
        category: The benchmark catalog §2 category.
        runner: ``"native"``, ``"external"`` or ``"goal"`` (ADR-0031).
        capabilities: The capability IDs this suite contributes evidence to. Empty for a
            self-test suite, which measures the harness rather than the model.
        requires: Preconditions for the whole suite.
        dataset_hashes: Pinned hashes of any installed data. Empty for a self-contained suite.
        prompt_ids: The ``(prompt_id, version, sha256)`` triples this suite declares, exactly as
            the manifest states them. Empty for a suite whose cases carry literal prompt text.
        prompt_subset_hash: The hash over **only** those prompts, or ``None`` when the suite
            declares none. This — never the pack hash — is what enters the reproducibility
            fingerprint and the evidence-separation rules (ADR-0028 §1). It is read from the
            manifest and verified against the installed pack by the suite's own loader, because a
            manifest cannot be trusted to describe prompts it does not contain.
        license: The suite's licence, as written in the manifest.
        body: The manifest exactly as parsed, minus ``manifest_hash`` — the input
            :func:`compute_manifest_hash` hashes, retained so the stored copy is the file's own
            content rather than a lossy reconstruction of it.
    """

    key: str
    name: str
    version: str
    category: str
    runner: str
    capabilities: tuple[str, ...]
    requires: Mapping[str, Any]
    dataset_hashes: Mapping[str, str]
    prompt_ids: tuple[Mapping[str, str], ...]
    prompt_subset_hash: str | None
    license: str
    body: Mapping[str, Any]

    @property
    def manifest_hash(self) -> str:
        """``sha256:``-prefixed hash of the manifest body, as stored on ``benchmark_suites``."""
        return compute_manifest_hash(self.body)

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> BenchmarkManifest:
        """Build a manifest from parsed ``manifest.json`` content.

        Args:
            body: The parsed object. ``manifest_hash``, if present, is dropped: a file cannot
                contain its own hash and still hash to it, so the stored hash is always computed
                here and never trusted from the file.

        Returns:
            The manifest.

        Raises:
            ValueError: A required field (``key``, ``name``, ``version``, ``category``,
                ``runner``) is missing. A manifest missing one of these cannot be stored, because
                every one of them is ``NOT NULL`` on ``benchmark_suites``.
        """
        hashable = {name: value for name, value in body.items() if name != "manifest_hash"}
        missing = [
            name
            for name in ("key", "name", "version", "category", "runner")
            if not hashable.get(name)
        ]
        if missing:
            raise ValueError(f"Benchmark manifest is missing required field(s): {missing}.")
        return cls(
            key=str(hashable["key"]),
            name=str(hashable["name"]),
            version=str(hashable["version"]),
            category=str(hashable["category"]),
            runner=str(hashable["runner"]),
            capabilities=tuple(str(item) for item in hashable.get("capabilities", ())),
            requires=dict(hashable.get("requires", {})),
            dataset_hashes=dict(hashable.get("dataset_hashes", {})),
            prompt_ids=tuple(
                {str(key): str(value) for key, value in dict(entry).items()}
                for entry in hashable.get("prompt_ids", ())
            ),
            prompt_subset_hash=(
                str(hashable["prompt_subset_hash"]) if hashable.get("prompt_subset_hash") else None
            ),
            license=str(hashable.get("license", "project")),
            body=hashable,
        )


def compute_manifest_hash(body: Mapping[str, Any]) -> str:
    """Return the ``sha256:``-prefixed hash of a manifest body.

    Over :func:`~baseaicore.canonical_json`, not over the file's bytes: re-indenting
    ``manifest.json`` or reordering its keys must not separate a suite's results from the ones it
    produced yesterday, and hashing the file directly would do exactly that.

    Args:
        body: The manifest object, with ``manifest_hash`` already removed.

    Returns:
        ``"sha256:"`` followed by 64 lowercase hex characters.
    """
    return f"sha256:{sha256_of(canonical_json(body))}"


@runtime_checkable
class Benchmark(Protocol):
    """A manifest plus the tests it declares — everything the run engine needs to execute it."""

    @property
    def manifest(self) -> BenchmarkManifest:
        """This benchmark's manifest."""
        ...

    @property
    def tests(self) -> Sequence[BenchmarkTest]:
        """The tests, in declaration order."""
        ...


class BenchmarkRegistry:
    """The set of benchmarks this build can run, keyed by suite key.

    Populated by the composition root (:mod:`freeweight.services.runs`'s
    :func:`~freeweight.services.runs.build_registry`), never by import side effects: a registry
    that fills itself as modules are imported has a content that depends on import order, and the
    Phase 7 suites are added by editing one list rather than by hoping a module was imported.

    Args:
        benchmarks: The benchmarks to register.

    Raises:
        ValueError: Two benchmarks declare the same ``(key, version)``.
    """

    __slots__ = ("_by_key",)

    def __init__(self, benchmarks: Sequence[Benchmark] = ()) -> None:
        """Index ``benchmarks`` by suite key."""
        by_key: dict[str, Benchmark] = {}
        for benchmark in benchmarks:
            key = benchmark.manifest.key
            existing = by_key.get(key)
            if existing is not None:
                raise ValueError(
                    f"Two benchmarks are registered under the key {key!r} "
                    f"(versions {existing.manifest.version!r} and "
                    f"{benchmark.manifest.version!r}); a key identifies one suite."
                )
            by_key[key] = benchmark
        self._by_key = by_key

    def keys(self) -> tuple[str, ...]:
        """Every registered suite key, sorted."""
        return tuple(sorted(self._by_key))

    def all(self) -> tuple[Benchmark, ...]:
        """Every registered benchmark, in key order."""
        return tuple(self._by_key[key] for key in sorted(self._by_key))

    def get(self, key: str) -> Benchmark:
        """Return the benchmark registered under ``key``.

        Args:
            key: The suite key, e.g. ``"native.echo"``.

        Returns:
            The benchmark.

        Raises:
            BenchmarkNotFound: No benchmark is registered under ``key``. The message lists what is
                registered, because the overwhelmingly likely cause is a typo in a suite name.
        """
        benchmark = self._by_key.get(key)
        if benchmark is None:
            raise BenchmarkNotFound(
                f"No benchmark suite named {key!r}; available suites are {list(self.keys())}.",
                details={"suite": key, "available": list(self.keys())},
            )
        return benchmark
