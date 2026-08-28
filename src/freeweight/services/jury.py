"""freeweight.services.jury — reaching the jurors, and turning what they say into grades.

The provider-facing half of rung 5. Everything about *who may judge* is
:mod:`freeweight.domain.jury`'s; everything about *how a jury is polled* is here: resolving each
juror through the provider, rendering the judge prompt with the author's anchors, presenting the
answer blind, randomizing criterion order, repeating, and parsing what comes back.

**A juror never judges its own output.** The refusal is decided in the domain and *recorded* here,
on the run, as part of the jury's own identity — never silently discounted (ADR-0031 §4).

**A provider failure is a skipped criterion, not a failed sample.** Spec §13: rule criteria still
score when the jury cannot be reached, and the partial result says so. So every call here is
wrapped, and a juror that could not answer contributes a
:class:`~freeweight.domain.scorers.judged.JurorVerdict` carrying its refusal reason rather than an
absence.

**The anchors reach the model through a prompt record.** ``goals.judge.rubric`` renders the
author's graded examples as few-shot exemplars with their grades and notes — a prompt record with
no exception and no f-strings ([ADR-0012](../../../../docs/adr/0012-prompt-storage-format.md)),
which is what makes the exact text a juror was shown reconstructible from a stored hash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from baseaicore import (
    Measurement,
    RuntimeProfile,
    elapsed_ms,
    is_supported,
    monotonic_ns,
    sha256_of,
)
from modelrack import GenerationRequest, Message, Role, SamplingParameters
from modelrack.errors import ProviderError

from freeweight.domain.goals.criteria import CriterionOutcome
from freeweight.domain.judging import JudgeChoice, parse_choice, present
from freeweight.domain.jury import JuryAssembly, assemble_jury, order_criteria, presentation_seed
from freeweight.domain.scorers.judged import (
    PAIRWISE,
    JudgedCriterionResult,
    JurorVerdict,
    combine_verdicts,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from baseaicore import ModelIdentity
    from modelrack import Provider

    from freeweight.config import JudgeSettings
    from freeweight.domain.benchmark import BenchmarkCase
    from freeweight.domain.goals.pack import Criterion, GoalPack
    from freeweight.services.prompts import PromptLibrary

__all__ = [
    "PAIRWISE_PROMPT_ID",
    "RUBRIC_PROMPT_ID",
    "AnchorExemplar",
    "JuryService",
    "JurorModel",
    "build_jury",
    "render_anchors",
    "render_descriptors",
]

logger = logging.getLogger(__name__)

RUBRIC_PROMPT_ID = "goals.judge.rubric"
"""The absolute-mode judge prompt record."""

PAIRWISE_PROMPT_ID = "goals.judge.pairwise"
"""The pairwise-mode judge prompt record."""

REFUSED_PROTOCOL = "protocol_error"
"""The juror answered in a form no grade could be read from."""

REFUSED_PROVIDER = "provider_error"
"""The juror could not be reached at all."""

_RATIONALE_CHARACTERS = 200
"""How much of a juror's reason is stored (spec §14's bounded scorer-evidence exception)."""


@dataclass(frozen=True, slots=True)
class JurorModel:
    """One model that may serve on a jury.

    Attributes:
        canonical_id: Its canonical model identity string.
        identity: The identity the provider generates against.
        remote: Whether it runs off this machine.
    """

    canonical_id: str
    identity: ModelIdentity
    remote: bool = False


@dataclass(frozen=True, slots=True)
class AnchorExemplar:
    """One of the author's graded examples, as it is shown to a juror.

    Attributes:
        content: The answer the author graded.
        grade: The grade they gave it.
        note: Their own note, or ``""``. The note is the part that teaches — it is why the
            author graded it that way, in their words.
    """

    content: str
    grade: int
    note: str = ""


def render_descriptors(scale_points: int, descriptors: Mapping[str, str]) -> str:
    """Render a criterion's scale for the judge prompt, highest point first.

    Args:
        scale_points: The scale's size.
        descriptors: Grade point to sentence.

    Returns:
        One line per described point, in descending order — the order a rubric is read in.
    """
    lines = [
        f"{point}: {descriptors[str(point)]}"
        for point in range(scale_points, 0, -1)
        if str(point) in descriptors
    ]
    return "\n".join(lines)


def render_anchors(anchors: Sequence[AnchorExemplar]) -> str:
    """Render the author's graded examples as few-shot exemplars.

    Args:
        anchors: The examples, in the order they should be shown.

    Returns:
        A block ending in a blank line, or ``""`` when there are none. Empty is a real state — an
        uncalibrated first pass has no anchors — which is why the prompt's ``anchors`` variable
        has a default rather than being required.
    """
    if not anchors:
        return ""
    blocks = []
    for index, anchor in enumerate(anchors, start=1):
        note = f"\nWhy: {anchor.note}" if anchor.note else ""
        blocks.append(
            f"EXAMPLE {index} — the author graded this {anchor.grade}{note}\n{anchor.content}"
        )
    return "\nTHE AUTHOR'S OWN GRADED EXAMPLES\n" + "\n\n".join(blocks) + "\n"


@dataclass(frozen=True, slots=True)
class JuryService:
    """Polls a jury for one sample's judged criteria.

    Satisfies :class:`~freeweight.benchmarks.goal.runner.JudgeCollaborator`, which is the seam
    that keeps :mod:`freeweight.domain.goals` free of providers.

    Args:
        provider: The provider every juror is generated through.
        library: The pack the judge prompts render from.
        pack: The goal whose criteria are being scored.
        assembly: The jury, already assembled and its refusals recorded.
        jurors: The jury's models, in polling order.
        anchors: The author's graded examples, by criterion key. Empty for an uncalibrated goal.
        repetitions: How many times each juror grades each criterion.
        temperature: The sampling temperature every juror is polled at.
        seed: The run's own seed, so criterion order is reproducible from the run record.
        runtime_profile: How each juror is loaded and served. A juror is a *measuring instrument*,
            so the context it is served at is part of its identity as one — and left unset, a
            provider serves it at the model's advertised maximum, which on a modern local model is
            128K-262K and enough KV cache to take a machine down (ADR-0023 §4). The candidate's own
            profile is deliberately reused: judging at a different context than the answers were
            generated at would be a second, unrecorded variable.
        timeout_seconds: Per-call timeout.
    """

    provider: Provider
    library: PromptLibrary
    pack: GoalPack
    assembly: JuryAssembly
    jurors: tuple[JurorModel, ...] = ()
    anchors: Mapping[str, tuple[AnchorExemplar, ...]] = field(default_factory=dict)
    repetitions: int = 3
    temperature: float = 0.0
    seed: int = 0
    runtime_profile: RuntimeProfile = field(default_factory=RuntimeProfile)
    timeout_seconds: float = 300.0

    def score_judged(
        self,
        *,
        criteria: Sequence[Criterion],
        response_text: str,
        case: BenchmarkCase,
    ) -> Sequence[CriterionOutcome]:
        """Grade every judged criterion for one sample. Never raises.

        Args:
            criteria: The judged criteria, in the goal's declaration order.
            response_text: The candidate's answer, shown to jurors with no attribution.
            case: The case, whose metadata carries the task text.

        Returns:
            One outcome per criterion, in the order given, each carrying its jury's verdicts under
            ``detail["judge_verdicts"]`` so the run engine can write them to ``judge_verdicts`` in
            the same transaction as the criterion score. A criterion no juror could grade comes
            back *skipped* with ``judge_unavailable`` — never scored zero.
        """
        import dataclasses

        outcomes: list[CriterionOutcome] = []
        for result in self.grade_all(criteria, response_text, case):
            outcomes.append(
                dataclasses.replace(
                    result.outcome,
                    detail={
                        **dict(result.outcome.detail),
                        "judge_verdicts": [
                            _verdict_json(verdict, self.judge_prompt_reference())
                            for verdict in result.verdicts
                        ],
                    },
                )
            )
        return outcomes

    def grade_all(
        self,
        criteria: Sequence[Criterion],
        response_text: str,
        case: BenchmarkCase,
    ) -> list[JudgedCriterionResult]:
        """Grade every judged criterion and keep every verdict, for ``judge_verdicts``.

        Criterion order is randomized per sample and seeded from the run's own seed: a juror asked
        about five criteria in the same order every time anchors on the first one, and an order
        nobody can reconstruct is a run nobody can repeat (benchmark catalog §7.3).
        """
        by_key = {criterion.key: criterion for criterion in criteria}
        order = order_criteria(
            [criterion.key for criterion in criteria],
            seed_material=presentation_seed(
                run_seed=self.seed, sample_key=case.case_id, criterion_key="*"
            ),
        )
        graded = {key: self._grade_one(by_key[key], response_text, case) for key in order}
        return [graded[criterion.key] for criterion in criteria]

    def _grade_one(
        self, criterion: Criterion, response_text: str, case: BenchmarkCase
    ) -> JudgedCriterionResult:
        """Poll every juror for one criterion, every repetition."""
        if not self.jurors:
            return combine_verdicts(criterion, ())
        verdicts: list[JurorVerdict] = []
        for ordinal, juror in enumerate(self.jurors):
            for repetition in range(1, self.repetitions + 1):
                verdicts.append(
                    self._poll(criterion, response_text, case, juror, ordinal, repetition)
                )
        return combine_verdicts(criterion, verdicts)

    def _poll(  # noqa: PLR0913 — one verdict needs its whole context
        self,
        criterion: Criterion,
        response_text: str,
        case: BenchmarkCase,
        juror: JurorModel,
        ordinal: int,
        repetition: int,
    ) -> JurorVerdict:
        """Ask one juror once, and turn its answer — or its silence — into a verdict."""
        task_text = str(case.metadata.get("task_text", case.prompt))
        if criterion.mode == PAIRWISE:
            return self._poll_pairwise(
                criterion, response_text, task_text, juror, ordinal, repetition
            )
        rendered = self.library.render(
            RUBRIC_PROMPT_ID,
            {
                "task": task_text,
                "criterion_name": criterion.name,
                "criterion_intent": criterion.intent,
                "scale_points": criterion.scale.points if criterion.scale else 5,
                "descriptors": render_descriptors(
                    criterion.scale.points if criterion.scale else 5,
                    criterion.scale.descriptors if criterion.scale else {},
                ),
                "anchors": render_anchors(self.anchors.get(criterion.key, ())),
                "answer": response_text,
            },
        )
        started = monotonic_ns()
        try:
            result = self.provider.generate(self._request(juror, rendered.system, rendered.user))
        except ProviderError as exc:
            logger.warning(
                "goal.juror_unreachable",
                extra={"juror": juror.canonical_id, "criterion": criterion.key, "code": exc.code},
            )
            return JurorVerdict(
                juror_canonical_id=juror.canonical_id,
                juror_ordinal=ordinal,
                repetition=repetition,
                refused_reason=REFUSED_PROVIDER,
                latency_ms=elapsed_ms(started),
            )
        latency = elapsed_ms(started)
        grade, reason = _parse_grade(
            result.text, points=criterion.scale.points if criterion.scale else 5
        )
        return JurorVerdict(
            juror_canonical_id=juror.canonical_id,
            juror_ordinal=ordinal,
            repetition=repetition,
            grade=grade,
            rationale=None if reason is None else reason[:_RATIONALE_CHARACTERS],
            refused_reason=None if grade is not None else REFUSED_PROTOCOL,
            latency_ms=latency,
            remote=juror.remote,
            input_tokens=_reported(result.usage.tokens.input_tokens),
            output_tokens=_reported(result.usage.tokens.output_tokens),
        )

    def _poll_pairwise(  # noqa: PLR0913 — one verdict needs its whole context
        self,
        criterion: Criterion,
        response_text: str,
        task_text: str,
        juror: JurorModel,
        ordinal: int,
        repetition: int,
    ) -> JurorVerdict:
        """Ask one juror to compare the candidate against a reference, in a seeded order."""
        references = [
            anchor.content for anchor in self.anchors.get(criterion.key, ()) if anchor.content
        ]
        if not references:
            return JurorVerdict(
                juror_canonical_id=juror.canonical_id,
                juror_ordinal=ordinal,
                repetition=repetition,
                refused_reason=REFUSED_PROTOCOL,
            )
        reference = references[repetition % len(references)]
        # Alternate the order across repetitions rather than randomizing it: a pairwise criterion
        # is measured with the same bias control native.judge uses, and asking both ways is the
        # control.
        candidate_first = repetition % 2 == 1
        order = (0, 1) if candidate_first else (1, 0)
        presentation = present(("candidate", "reference"), (response_text, reference), order)
        rendered = self.library.render(
            PAIRWISE_PROMPT_ID,
            {
                "task": task_text,
                "criterion_name": criterion.name,
                "criterion_intent": criterion.intent,
                "answers": presentation.rendered(),
            },
        )
        started = monotonic_ns()
        try:
            result = self.provider.generate(self._request(juror, rendered.system, rendered.user))
        except ProviderError:
            return JurorVerdict(
                juror_canonical_id=juror.canonical_id,
                juror_ordinal=ordinal,
                repetition=repetition,
                presentation_order="candidate_first" if candidate_first else "reference_first",
                refused_reason=REFUSED_PROVIDER,
                latency_ms=elapsed_ms(started),
            )
        choice = parse_choice(result.text, labels=presentation.labels)
        chosen = {
            JudgeChoice.FIRST: presentation.subjects[0],
            JudgeChoice.SECOND: presentation.subjects[1],
            JudgeChoice.TIE: "tie",
        }.get(choice)
        return JurorVerdict(
            juror_canonical_id=juror.canonical_id,
            juror_ordinal=ordinal,
            repetition=repetition,
            pairwise_choice=chosen,
            presentation_order="candidate_first" if candidate_first else "reference_first",
            rationale=result.text[:_RATIONALE_CHARACTERS],
            refused_reason=None if chosen is not None else REFUSED_PROTOCOL,
            latency_ms=elapsed_ms(started),
            remote=juror.remote,
            input_tokens=_reported(result.usage.tokens.input_tokens),
            output_tokens=_reported(result.usage.tokens.output_tokens),
        )

    def _request(self, juror: JurorModel, system: str | None, user: str) -> GenerationRequest:
        """Build one juror's request under the jury's own frozen sampling parameters."""
        messages: list[Message] = []
        if system:
            messages.append(Message(role=Role.SYSTEM, content=system))
        messages.append(Message(role=Role.USER, content=user))
        return GenerationRequest(
            identity=juror.identity,
            messages=tuple(messages),
            runtime_profile=self.runtime_profile,
            sampling=SamplingParameters(temperature=self.temperature, seed=self.seed),
            timeout_seconds=self.timeout_seconds,
        )

    def refusal_detail(self) -> dict[str, Any]:
        """Return the jury's own identity and every refusal, for the run record.

        Self-judging is *recorded*, not silently discounted: a reader has to be able to see that
        the candidate was excluded from its own jury and that the jury was therefore smaller than
        the goal asked for (ADR-0031 §4).
        """
        return {
            **self.assembly.as_json(),
            **self.judge_prompt_reference(),
            "self_judging_refused": list(self.assembly.self_judging_refused),
        }

    def with_anchors(self, anchors: Mapping[str, tuple[AnchorExemplar, ...]]) -> JuryService:
        """Return a copy of this jury bound to a different set of exemplars.

        The calibration service rebinds the jury to the anchors *its own* partition produced,
        rather than trusting whatever the caller assembled the jury with. The two have to come
        from one computation: if they can drift apart, the way they drift is by showing the jury a
        sample it is about to be measured on.
        """
        import dataclasses

        return dataclasses.replace(self, anchors=dict(anchors))

    def judge_prompt_reference(self) -> dict[str, str]:
        """Return the judge prompt's identity, for the ``judge_set`` a result carries."""
        record = self.library.get(RUBRIC_PROMPT_ID)
        return {
            "prompt_id": record.prompt_id,
            "prompt_version": record.version,
            "prompt_sha256": record.sha256,
        }


def _verdict_json(verdict: JurorVerdict, prompt: Mapping[str, str]) -> dict[str, Any]:
    """Render one juror's verdict as the ``judge_verdicts`` row the run engine writes.

    Every field the data model names, including the presentation order — order bias is measured
    rather than assumed, so the order a verdict was given in is part of the verdict.
    """
    return {
        "juror_canonical_id": verdict.juror_canonical_id,
        "juror_ordinal": verdict.juror_ordinal,
        "repetition": verdict.repetition,
        "grade": verdict.grade,
        "pairwise_choice": verdict.pairwise_choice,
        "presentation_order": verdict.presentation_order,
        "rationale": verdict.rationale,
        "rationale_sha256": (
            None if verdict.rationale is None else f"sha256:{sha256_of(verdict.rationale)}"
        ),
        "prompt_id": prompt["prompt_id"],
        "prompt_version": prompt["prompt_version"],
        "judge_prompt_sha256": prompt["prompt_sha256"],
        "refused_reason": verdict.refused_reason,
        "latency_ms": verdict.latency_ms,
        "input_tokens": verdict.input_tokens,
        "output_tokens": verdict.output_tokens,
        "remote": verdict.remote,
    }


def _reported(value: Measurement) -> float | None:
    """Return a provider-reported count, or ``None`` where the provider reported none.

    ``None`` rather than zero: a provider that reports no token counts has not told us the call
    was free (ADR-0016).
    """
    return float(value) if is_supported(value) else None


def _parse_grade(text: str, *, points: int) -> tuple[int | None, str | None]:
    """Read one juror's grade, or refuse to.

    The accepted shape is a JSON object carrying an integer ``grade`` within the scale. A grade
    outside the scale is refused rather than clamped: a juror that answered ``7`` on a five-point
    scale did not understand the rubric, and clamping would hide that behind a plausible 5.
    """
    from freeweight.domain.scorers.schema import extract_json

    document, error = extract_json(text)
    if error is not None or not isinstance(document, dict):
        return None, None
    raw = document.get("grade")
    reason = document.get("reason")
    reason_text = str(reason) if isinstance(reason, str) else None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None, reason_text
    grade = int(raw)
    if grade != raw or not 1 <= grade <= points:
        return None, reason_text
    return grade, reason_text


def build_jury(  # noqa: PLR0913 — a jury is assembled from exactly these facts
    provider: Provider,
    *,
    pack: GoalPack,
    library: PromptLibrary,
    settings: JudgeSettings,
    candidate_canonical_id: str,
    available: Sequence[str],
    allow_remote_provider: bool,
    anchors: Mapping[str, tuple[AnchorExemplar, ...]] | None = None,
    seed: int = 0,
    remote: Mapping[str, bool] | None = None,
    runtime_profile: RuntimeProfile | None = None,
) -> JuryService:
    """Assemble the jury for one goal run and resolve every juror through the provider.

    Args:
        provider: The provider every juror is generated through.
        pack: The goal being run.
        library: The prompt pack the judge prompts render from.
        settings: The ``[judge]`` defaults.
        candidate_canonical_id: The model under measurement, which never judges itself.
        available: Canonical IDs of the models this machine can serve.
        allow_remote_provider: ``providers.allow_remote``. Combined here with the goal's own
            ``judge.allow_remote`` — **both** are required (ADR-0031 §4), and the ``and`` happens
            in one place so neither can be satisfied alone.
        anchors: The author's graded examples, by criterion key.
        seed: The run's seed, for criterion-order randomization.
        remote: Which models are remote, by canonical ID.

    Returns:
        The service, which may hold an empty jury: zero eligible jurors is a *state* (judged
        criteria skip, rule criteria still score), not an error.
    """
    judge = pack.judge
    jury_size = judge.jury_size if judge is not None else settings.jury_size
    requested = list(judge.models) if judge is not None and judge.models else list(settings.models)
    goal_allows_remote = judge.allow_remote if judge is not None else settings.allow_remote
    assembly = assemble_jury(
        sorted(available),
        candidate=candidate_canonical_id if settings.refuse_self_judging else None,
        requested=requested,
        jury_size=jury_size,
        remote=remote,
        allow_remote=allow_remote_provider and goal_allows_remote,
    )
    resolved: list[JurorModel] = []
    remote_by_id = dict(remote or {})
    # From the catalogue the caller already listed, not by re-resolving the canonical ID: a
    # canonical ID carries a digest, and an adapter is entitled to accept only its own provider
    # model *names* as references (ADR-0024). Falling back to ``resolve`` covers a juror the
    # catalogue did not name, which is how a configured-but-unlisted model surfaces as a refusal
    # rather than as a silent absence.
    catalogue: dict[str, Any] = {}
    try:
        catalogue = {
            descriptor.identity.canonical_id: descriptor.identity
            for descriptor in provider.list_models()
        }
    except ProviderError:  # pragma: no cover — the caller listed them a moment ago
        catalogue = {}
    for canonical_id in assembly.jurors:
        identity = catalogue.get(canonical_id)
        if identity is None:
            try:
                identity = provider.resolve(canonical_id)
            except ProviderError:
                logger.warning("goal.juror_unresolvable", extra={"juror": canonical_id})
                continue
        resolved.append(
            JurorModel(
                canonical_id=canonical_id,
                identity=identity,
                remote=remote_by_id.get(canonical_id, False),
            )
        )
    return JuryService(
        provider=provider,
        library=library,
        pack=pack,
        assembly=assembly,
        jurors=tuple(resolved),
        anchors=dict(anchors or {}),
        repetitions=judge.repetitions if judge is not None else settings.repetitions,
        temperature=settings.temperature,
        seed=seed,
        runtime_profile=runtime_profile if runtime_profile is not None else RuntimeProfile(),
    )
