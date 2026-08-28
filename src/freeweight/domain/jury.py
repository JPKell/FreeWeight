"""freeweight.domain.jury — who judges, in what order, and what to do when too few can.

The rules of assembly, with no provider in sight. Everything about *reaching* a juror lives in
:mod:`freeweight.services.jury`; everything about *who is allowed to be one* lives here, so that
``freeweight judges validate``, ``POST /judges/validate`` and an actual run cannot disagree about
the answer.

**A juror never judges its own output.** Refused and recorded, never discounted
([ADR-0031 §4](../../../../docs/adr/0031-user-defined-goal-benchmarks.md)). ``native.judge``
already measures self-preference as a defect; admitting it here would be measuring with an
instrument the catalogue elsewhere calls broken.

**A jury that cannot be assembled degrades, and says so.** Fewer eligible models than
``jury_size`` gives the largest jury available, with :attr:`JuryAssembly.reduced` set and the
reason recorded — a single-juror goal loses inter-juror agreement, and the result says so rather
than quietly reporting one model's opinion as a jury's. Zero eligible jurors is
``JUDGE_UNAVAILABLE``: judged criteria are skipped, rule criteria still score, and the partial
result says which.

**A remote juror needs two opt-ins.** ``providers.allow_remote`` *and* the goal's own
``judge.allow_remote``. Neither can be satisfied by accident, and a remote jury separates results
from a locally-judged one rather than merging with it (ADR-0032 §4).

Pure domain: stdlib and this package's own :mod:`~freeweight.domain.judging`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from freeweight.domain.judging import (
    REASON_SELF_JUDGING,
    JurorEligibility,
    eligible_jurors,
    judge_benchmark_reference,
    randomized_order,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "ERROR_JUDGE_UNAVAILABLE",
    "REASON_JURY_REDUCED",
    "JuryAssembly",
    "assemble_jury",
    "judge_set_identity",
    "order_criteria",
    "presentation_seed",
]

ERROR_JUDGE_UNAVAILABLE = "JUDGE_UNAVAILABLE"
"""Not one model may judge this candidate. Judged criteria skip; rule criteria still score."""

REASON_JURY_REDUCED = "jury_reduced"
"""The jury is smaller than the goal asked for, and the result says so."""


@dataclass(frozen=True, slots=True)
class JuryAssembly:
    """The jury a run will actually poll, and every reason it is not the one that was configured.

    Attributes:
        jurors: The eligible models, in the order they will be polled.
        requested_size: The ``jury_size`` the goal asked for.
        reduced: Whether the jury is smaller than that.
        refusals: One entry per model that was considered and refused, with its reasons. Recorded
            rather than summarized: "the jury is two, not three" is much less useful than "qwen3
            is the candidate and cannot judge itself".
        remote: Whether any juror runs off this machine.
    """

    jurors: tuple[str, ...]
    requested_size: int
    reduced: bool = False
    refusals: tuple[JurorEligibility, ...] = ()
    remote: bool = False

    @property
    def available(self) -> bool:
        """Whether there is anyone to judge at all."""
        return bool(self.jurors)

    @property
    def self_judging_refused(self) -> tuple[str, ...]:
        """Every model refused specifically because it produced the answer under judgement."""
        return tuple(
            refusal.model_canonical_id
            for refusal in self.refusals
            if REASON_SELF_JUDGING in refusal.reasons
        )

    def as_json(self) -> dict[str, Any]:
        """Return the assembly as the run record stores it."""
        return {
            "jurors": list(self.jurors),
            "requested_size": self.requested_size,
            "jury_reduced": self.reduced,
            "reduction_reason": REASON_JURY_REDUCED if self.reduced else None,
            "remote": self.remote,
            "refused": [
                {"model": refusal.model_canonical_id, "reasons": list(refusal.reasons)}
                for refusal in self.refusals
            ],
        }


def assemble_jury(
    available: Sequence[str],
    *,
    candidate: str | None,
    requested: Sequence[str] = (),
    jury_size: int = 3,
    remote: Mapping[str, bool] | None = None,
    allow_remote: bool = False,
) -> JuryAssembly:
    """Choose the jury, and record everyone who was refused and why.

    Args:
        available: Canonical IDs of the models this machine can serve.
        candidate: The model whose output is being judged. Never eligible to judge itself.
        requested: The jury the goal's own configuration names, in order. Empty means
            auto-selection from what is installed, which takes the first ``jury_size`` eligible
            models in the order they were listed — deterministic, because the caller sorts.
        jury_size: How many jurors the goal asked for.
        remote: Which models are remote, by canonical ID.
        allow_remote: Whether **both** remote opt-ins are satisfied. The caller performs the
            ``and``; this function does not read configuration.

    Returns:
        The assembly. Never raises for an empty jury: zero jurors is a *state*, reported through
        :attr:`JuryAssembly.available`, because a goal's rule criteria must still score when no
        model can judge (spec §13).

    Raises:
        ValueError: ``jury_size`` is not positive. A jury of zero is a configuration error rather
            than a degradation, and treating it as one would silently disable judging.
    """
    if jury_size < 1:
        raise ValueError(f"judge.jury_size must be at least 1; got {jury_size}.")
    verdicts = eligible_jurors(
        available,
        candidate=candidate,
        requested=requested,
        remote=remote,
        allow_remote=allow_remote,
    )
    chosen = [verdict.model_canonical_id for verdict in verdicts if verdict.eligible][:jury_size]
    refusals = tuple(verdict for verdict in verdicts if not verdict.eligible)
    remote_by_id = dict(remote or {})
    return JuryAssembly(
        jurors=tuple(chosen),
        requested_size=jury_size,
        reduced=len(chosen) < jury_size,
        refusals=refusals,
        remote=any(remote_by_id.get(model, False) for model in chosen),
    )


def presentation_seed(*, run_seed: int, sample_key: str, criterion_key: str) -> str:
    """Return the seed material one presentation's order randomization uses.

    Composed from the run's own recorded seed and the two keys that identify the question, so the
    order a jury saw is reproducible from the run record alone, and two criteria on the same
    sample do not receive the same order by construction.
    """
    return f"{run_seed}:{sample_key}:{criterion_key}"


def order_criteria(criteria: Sequence[str], *, seed_material: str) -> tuple[str, ...]:
    """Return the criteria in the order one juror will be asked about them.

    Randomized because a juror asked about five criteria in the same order every time anchors on
    the first one; seeded because a run whose order nobody can reconstruct is a run nobody can
    repeat (benchmark catalog §7.3's "case and criterion order randomized").
    """
    return tuple(randomized_order(criteria, seed_material=seed_material))


def judge_set_identity(
    assembly: JuryAssembly,
    *,
    prompt_id: str,
    prompt_version: str,
    prompt_sha256: str,
) -> dict[str, Any]:
    """Return the ``judge_set`` a judged result carries — a hard-separation input (ADR-0032 §4).

    A different jury is a different instrument, and a different instrument is a different
    measurement. This is what makes that structural on the wire: the jurors, the prompt that was
    put to them, and whether any of them ran off this machine.

    Args:
        assembly: The jury that was polled.
        prompt_id: The judge prompt record's ID.
        prompt_version: That record's version.
        prompt_sha256: That record's canonical hash.

    Returns:
        The identity, JSON-safe, with the link to each juror's own ``native.judge`` results
        attached so "how trustworthy is this instrument" is one interaction away.
    """
    return judge_benchmark_reference(
        assembly.jurors,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        remote=assembly.remote,
    )
