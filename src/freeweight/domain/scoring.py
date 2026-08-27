"""freeweight.domain.scoring — the ``Scorer`` protocol and the result one produces.

A scorer turns "what the model said" into a number, or into an honest refusal to produce one.
It is the narrowest interface in the application on purpose: given a case's expectation and the
text that came back, return a :class:`ScoreResult`. It never sees the run, the database, the
provider or the clock, which is what lets Phase 7's thirteen scorers be pure functions with
table-driven tests.

**A scorer never returns ``0`` for "could not score".** ``score=None`` with a ``method`` and an
``error_code`` is the only honest representation of an unscoreable sample
([ADR-0016](../../../../docs/adr/0016-unavailable-is-not-zero.md)), and it is what keeps that
sample out of the aggregate while leaving it visible in the counts (spec §13).

**The rung a scorer occupies is recorded, not implied.** :class:`ScoreMethod` is the scoring
ladder of [Benchmark Catalog §1](../../../../docs/apps/freeweight/benchmark-catalog.md), and
``samples.score_method`` stores which rung produced each number, so a result assembled from mixed
rungs can say so rather than presenting one average.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from freeweight.domain.benchmark import BenchmarkCase

__all__ = ["ScoreMethod", "ScoreResult", "Scorer"]


class ScoreMethod(StrEnum):
    """The rung of the scoring ladder that produced a score (benchmark catalog §1).

    Ordered here highest-rung-first, matching the catalog's preference order: a lower rung is used
    only when every higher one is impossible.
    """

    EXECUTION = "execution"
    RULE = "rule"
    REFERENCE = "reference"
    HUMAN = "human"
    JUDGE = "judge"


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """One scorer's verdict on one sample.

    Attributes:
        score: The score in ``0.0..1.0``, or ``None`` when this sample could not be scored.
            ``None`` is not a low score: the sample is excluded from aggregates and its exclusion
            is visible in the sample count (spec §13). A scorer that means "the model got it
            entirely wrong" returns ``0.0``, which is a measurement; ``None`` means there is no
            measurement.
        method: The ladder rung that produced ``score``. Recorded on the sample even when
            ``score`` is ``None``, because *which instrument failed* is part of the record.
        detail: Scorer-specific evidence for the number — matched phrases, the expected and actual
            values, the rule that fired. Stored in ``samples.result_json`` so a headline metric
            drills to something a person can read, not just to a float.
        error_code: A stable code when ``score`` is ``None``; ``None`` otherwise.
        error_text: A human-readable reason when ``score`` is ``None``; ``None`` otherwise.
    """

    score: float | None
    method: ScoreMethod
    detail: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_text: str | None = None

    def __post_init__(self) -> None:
        """Refuse a result that is internally dishonest.

        Raises:
            ValueError: ``score`` is outside ``0.0..1.0``, or a scored result carries an error
                code, or an unscored result carries none. The last two are the cases that would
                otherwise reach the database as a row nobody can interpret: a score *and* an
                error, or a ``NULL`` score with no recorded reason for it.
        """
        if self.score is not None:
            if not 0.0 <= self.score <= 1.0:
                raise ValueError(f"score must be within 0.0..1.0; got {self.score!r}.")
            if self.error_code is not None:
                raise ValueError(
                    f"A scored result must not carry an error code; got {self.error_code!r} "
                    f"alongside score={self.score!r}."
                )
        elif self.error_code is None:
            raise ValueError(
                "An unscored result (score=None) must carry an error_code saying why; "
                "a NULL score with no reason is exactly the row ADR-0016 exists to prevent."
            )


@runtime_checkable
class Scorer(Protocol):
    """Turns a case's expectation and a model's response into a :class:`ScoreResult`.

    Implementations are pure and stateless: the same case and the same text always produce the
    same result, in any process, in any order. Nothing here is allowed to call a provider, read
    the filesystem or consult a clock — a scorer that needs a model is a rung-5 judge, and rung 5
    has its own machinery and its own calibration precondition (benchmark catalog §1).
    """

    @property
    def key(self) -> str:
        """The scorer's stable name, as written in a benchmark manifest's ``scorer`` field."""
        ...

    @property
    def method(self) -> ScoreMethod:
        """The ladder rung this scorer occupies."""
        ...

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Score one response against one case.

        Args:
            case: The case that produced ``response_text``, carrying its own expectation.
            response_text: Exactly what the model returned, unmodified.

        Returns:
            The verdict. A scorer signals "unscoreable" by returning ``score=None`` with an
            ``error_code``; it does **not** raise. An exception escaping here is a defect in the
            scorer, and the run engine records it as a failed sample rather than failing the test.
        """
        ...
