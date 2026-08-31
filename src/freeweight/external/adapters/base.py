"""freeweight.external.adapters.base — the adapter protocol and the normalized result it yields.

Every external benchmark parses differently and reports the same shape. An adapter owns exactly
two things: its :class:`~freeweight.external.manifest.ExternalManifest` and a pure function that
turns the tool's **raw output bytes** into :class:`AdapterOutcome` — a per-case list of
:class:`AdapterSample` plus benchmark-level metrics. The parser is the untrusted-input boundary
(Security Standards §14, ADR-0018): it is handed bytes a subprocess produced and must treat every
one of them as hostile — malformed JSON, a truncated stream, an unexpected schema, a score out of
range and a missing field are all *outcomes it reports*, never exceptions that escape or partial
parses it rescues.

A parser therefore never raises for bad content: it returns an :class:`AdapterOutcome` whose
``error_code``/``error_text`` say what was wrong, and whose samples cover exactly the cases it
could read. A benchmark that produced no readable case is a failed result with a reason, which is
what ``EXTERNAL_BENCHMARK_FAILED`` names — the run continues, the failure is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.external.manifest import ExternalManifest

__all__ = ["Adapter", "AdapterOutcome", "AdapterSample"]


@dataclass(frozen=True, slots=True)
class AdapterSample:
    """One case's normalized result, on the same shape a native sample carries.

    Attributes:
        case_id: The benchmark's own identifier for this case. Stable across runs so two runs of
            the same benchmark compare case-by-case.
        score: ``0.0..1.0``, or ``None`` when this case could not be scored — never a fabricated
            zero (ADR-0016). A parser that read a case but found no verdict for it reports
            ``None`` with an ``error_code``, exactly as a native scorer does.
        detail: The evidence for the number — the predicted and expected answer, the failing
            check, an excerpt — stored in ``samples.result_json`` so a headline metric drills to
            something readable. Capped by the caller before storage.
        error_code: A stable code when ``score`` is ``None``; ``None`` otherwise.
        error_text: A human-readable reason when ``score`` is ``None``.
    """

    case_id: str
    score: float | None
    detail: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_text: str | None = None

    def __post_init__(self) -> None:
        """Refuse an internally dishonest sample, exactly as ``ScoreResult`` does."""
        if self.score is not None:
            if not 0.0 <= self.score <= 1.0:
                message = f"score must be within 0.0..1.0; got {self.score!r}."
                raise ValueError(message)
            if self.error_code is not None:
                message = f"a scored sample must not carry an error code; got {self.error_code!r}."
                raise ValueError(message)
        elif self.error_code is None:
            message = "an unscored sample must carry an error_code saying why (ADR-0016)."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class AdapterOutcome:
    """What one adapter made of one benchmark run's output.

    Attributes:
        samples: The per-case normalized samples the parser could read. Empty when nothing parsed.
        metrics: Benchmark-level metric values keyed by metric name, e.g.
            ``{"exact_match": 0.62}``. These are what the tool reported at the summary level;
            per-case scores in ``samples`` are what a drill-down reads.
        error_code: ``None`` on a clean parse; a stable code (``EXTERNAL_BENCHMARK_FAILED``) when
            the output could not be parsed at all or the subprocess failed.
        error_text: The reason, when ``error_code`` is set.
        partial: Whether the parse was partial — some cases read, some unreadable. A partial
            outcome still returns what it could, with the count visible.
    """

    samples: tuple[AdapterSample, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    error_code: str | None = None
    error_text: str | None = None
    partial: bool = False

    @property
    def ok(self) -> bool:
        """Whether the parse produced at least one sample and carried no fatal error."""
        return self.error_code is None and bool(self.samples)

    def scored_count(self) -> int:
        """How many samples carry a real score, for the run's visible sample count."""
        return sum(1 for sample in self.samples if sample.score is not None)


@runtime_checkable
class Adapter(Protocol):
    """One external benchmark, as FreeWeight drives and reads it."""

    @property
    def manifest(self) -> ExternalManifest:
        """This adapter's manifest — its provenance, datasets and sandbox requirement."""
        ...

    def command(self, *, datasets_dir: Any, model_ref: str) -> Sequence[str]:  # noqa: ANN401
        """The argument list that runs this benchmark against ``model_ref``.

        Returns the argv for :func:`~freeweight.external.invocation.run_invocation` (or, for a
        code-execution benchmark, for :func:`~freeweight.external.sandbox.run_sandboxed`). An
        adapter that only parses recorded output for tests may return an empty list.
        """
        ...

    def parse(self, raw_output: bytes) -> AdapterOutcome:
        """Turn the tool's raw output into a normalized outcome.

        **Never raises for bad content.** Malformed JSON, a truncated stream, an unexpected
        schema, an out-of-range score and a missing field are all reported through
        :class:`AdapterOutcome`'s error fields, never propagated — the parser's whole job is to be
        the boundary that makes untrusted output safe.
        """
        ...
