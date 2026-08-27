"""freeweight.benchmarks.interaction — how a quality suite drives a conversation.

Five of Phase 7's suites need more than one provider call to produce one sample. A tool case is a
loop — ask, run the tools it asked for, hand the results back, ask again — and a structured-output
case is a call plus, at most, one corrective retry (benchmark catalog §3.5). Neither fits the run
engine's default "one call, score the text".

**The seam is deliberately narrow.** A benchmark never touches the provider, the database or the
clock; it is handed a :class:`TurnCaller` — "here is a conversation, give me the next assistant
turn" — and it returns an :class:`InteractionOutcome`. Everything about *when* a call happens,
what it costs and how it is stored stays in :mod:`freeweight.services.runs`; everything about
*what to say next* stays in the benchmark, where it is benchmark logic and can be unit-tested with
a two-line fake caller.

**Nothing here decides control flow from model output.** The loop advances on the provider's
declared ``finish_reason`` and on the tool schema, never on what the model wrote — testing
standards §3's rule that Python decides control flow, applied to the one place in this application
where a model is closest to being allowed to.

**Everything is bounded.** Every interaction has a step budget, and running out of it is a
recorded outcome (:data:`~freeweight.domain.scorers.tools.STOPPED_STEP_LIMIT`) rather than a hang
or an exception: a model that loops forever is a measurement, and one this suite is meant to take.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from baseaicore import elapsed_ms, monotonic_ns, sha256_of
from modelrack.types import Message, ResponseFormat, ResponseFormatKind, Role

from freeweight.domain.scorers.schema import SchemaUnsupported, extract_json, validate
from freeweight.domain.scorers.tools import (
    STOPPED_ANSWERED,
    STOPPED_PROVIDER_ERROR,
    STOPPED_STEP_LIMIT,
    ToolInvocation,
    ToolTranscript,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from modelrack.types import GenerationResult, ToolDefinition

    from freeweight.domain.benchmark import BenchmarkCase
    from freeweight.services.prompts import PromptLibrary

__all__ = [
    "DEFAULT_MAX_STEPS",
    "Interaction",
    "InteractionOutcome",
    "StructuredOutputSession",
    "ToolSession",
    "TurnCaller",
]

_INVALID_ARGUMENT = "INVALID_ARGUMENT"
_UNKNOWN_TOOL = "UNKNOWN_TOOL"
"""The two toolbox codes that mean "the harness did not run the tool".

Restated here rather than imported from :mod:`freeweight.benchmarks.fixtures.tools` so that this
module — which is about *driving a conversation* — does not depend on the fixture toolbox, and a
future toolbox with the same two outcomes can be dropped in without touching the driver.
"""

DEFAULT_MAX_STEPS = 8
"""Assistant turns one tool case may take before the harness stops asking.

Eight, because the longest declared sequence in the agent suite is four calls and a model is
entitled to a wrong turn or two before it gets there; anything beyond that is a loop, and the
catalog's ``steps_to_completion`` and ``hit_step_limit`` are how a loop is reported.
"""


@runtime_checkable
class TurnCaller(Protocol):
    """Produces the next assistant turn for a conversation. Supplied by the run engine."""

    def __call__(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDefinition] = (),
        response_format: ResponseFormat | None = None,
    ) -> GenerationResult:
        """Return the model's next turn.

        Args:
            messages: The conversation so far, system turn first.
            tools: Tools the model may call on this turn.
            response_format: The shape the answer should take, or ``None`` for free text.

        Returns:
            The result.

        Raises:
            ProviderError: The call failed. An interaction catches this and ends with
                :data:`~freeweight.domain.scorers.tools.STOPPED_PROVIDER_ERROR`, so a provider
                failure mid-trajectory is a recorded sample rather than a failed test.
        """
        ...


@dataclass(frozen=True, slots=True)
class InteractionOutcome:
    """What one multi-call interaction produced, for the run engine to store.

    Attributes:
        results: Every provider result the interaction obtained, in order. The engine sums their
            token counts onto the sample and reads the last one's finish reason, so a sample's
            ``output_tokens`` covers the whole interaction rather than only its final turn.
        text: The text the scorer scores — the model's final answer.
        transcript: The tool trajectory, or ``None`` for an interaction that used no tools.
        detail: Extra evidence merged into the sample's ``result_json``.
        error_code: The provider's stable code when the interaction ended in failure.
        error_text: The matching message.
    """

    results: tuple[GenerationResult, ...] = ()
    text: str = ""
    transcript: ToolTranscript | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_text: str | None = None


@runtime_checkable
class Interaction(Protocol):
    """Drives one case's conversation. A test declaring one is executed through it."""

    def run(self, caller: TurnCaller, case: BenchmarkCase) -> InteractionOutcome:
        """Produce this case's answer, making as many calls as the case needs."""
        ...


def _conversation(case: BenchmarkCase) -> list[Message]:
    """Open a conversation from a case's rendered prompt."""
    messages: list[Message] = []
    if case.system_prompt:
        messages.append(Message(role=Role.SYSTEM, content=case.system_prompt))
    messages.append(Message(role=Role.USER, content=case.prompt))
    return messages


@dataclass(frozen=True, slots=True)
class ToolSession:
    """Runs one tool case: ask, execute what was asked for, hand the results back, repeat.

    Args:
        toolbox: A factory for the case's toolbox. A *factory*, because the toolbox holds
            per-case state — the injected-failure counters — and one instance shared between
            cases would let one case's scripted timeout land in the next one's first call.
        max_steps: The assistant-turn budget.
    """

    toolbox: Any
    max_steps: int = DEFAULT_MAX_STEPS

    def run(self, caller: TurnCaller, case: BenchmarkCase) -> InteractionOutcome:
        """Drive the loop until the model answers, the budget runs out, or the provider fails.

        The loop's condition is the provider's ``finish_reason`` together with the presence of
        tool calls on the result — never a phrase in the model's text. A model that writes "I am
        done" while still requesting a tool is still requesting a tool.

        Args:
            caller: The engine's next-turn function.
            case: The case, whose ``metadata`` names the tools it offers and the failures to
                inject.

        Returns:
            The outcome, always carrying a transcript: even a trajectory that failed on the first
            call is a trajectory, and scoring it needs the record of what happened.
        """
        from modelrack.errors import ProviderError

        toolbox = self.toolbox(case)
        definitions = toolbox.definitions()
        offered = tuple(definition.name for definition in definitions)
        messages = _conversation(case)
        calls: list[ToolInvocation] = []
        results: list[GenerationResult] = []
        text = ""
        stopped = STOPPED_STEP_LIMIT
        error_code: str | None = None
        error_text: str | None = None

        for step in range(1, self.max_steps + 1):
            try:
                result = caller(messages, tools=definitions)
            except ProviderError as exc:
                stopped, error_code, error_text = STOPPED_PROVIDER_ERROR, exc.code, exc.message
                break
            results.append(result)
            if not result.tool_calls:
                text = result.text
                stopped = STOPPED_ANSWERED
                break
            messages.append(
                Message(
                    role=Role.ASSISTANT,
                    content=result.text or " ",
                    tool_calls=result.tool_calls,
                )
            )
            for requested in result.tool_calls:
                # "The provider sent argument text and it did not parse" — distinct from "the
                # model called a tool with no arguments", which is a legitimate call. ModelRack
                # keeps the raw text precisely so the two stay distinguishable
                # (:class:`~modelrack.types.ToolCall`), and a harness that conflated them would
                # score a malformed call as a well-formed one against a tool that needs none.
                parsed = not (requested.raw_arguments is not None and not requested.arguments)
                started_ns = monotonic_ns()
                outcome = toolbox.invoke(requested.name, requested.arguments)
                duration_ms = elapsed_ms(started_ns)
                calls.append(
                    ToolInvocation(
                        step=step,
                        name=requested.name,
                        arguments=dict(requested.arguments),
                        call_id=requested.id,
                        known_tool=requested.name in offered,
                        arguments_parsed=parsed,
                        arguments_valid=parsed and outcome.error_code != _INVALID_ARGUMENT,
                        executed=outcome.error_code not in {_UNKNOWN_TOOL, _INVALID_ARGUMENT},
                        ok=outcome.ok,
                        error_code=outcome.error_code,
                        result_digest=outcome.digest,
                        # The harness's answer is hashed, never stored: it is content, and it goes
                        # straight into the next prompt. Two runs whose tools answered identically
                        # are comparable without either keeping a byte of it (spec §14).
                        result_hash=f"sha256:{sha256_of(outcome.content)}",
                        duration_ms=duration_ms,
                    )
                )
                messages.append(
                    Message(
                        role=Role.TOOL,
                        content=outcome.content,
                        tool_call_id=requested.id,
                        name=requested.name,
                    )
                )

        transcript = ToolTranscript(
            calls=tuple(calls),
            final_text=text,
            steps=len(results),
            stopped=stopped,
            offered_tools=offered,
            error_code=error_code,
        )
        return InteractionOutcome(
            results=tuple(results),
            text=text,
            transcript=transcript,
            detail={"tool_steps": len(results), "tool_calls": len(calls)},
            error_code=error_code,
            error_text=error_text,
        )


@dataclass(frozen=True, slots=True)
class StructuredOutputSession:
    """Runs one structured-output case: one call, and at most one corrective retry.

    Benchmark catalog §3.5's recovery rate is the share of cases that failed the first attempt and
    conformed on the second. It is reported as its own figure and **never** substituted for the
    first-attempt rate: a model that needs a retry every time is a different model from one that
    does not, and a suite reporting only the post-retry number would hide that entirely.

    The retry is a prompt record (``benchmarks.structured_output.repair``), not a string here, and
    it states the validator's failures without stating the fix — a repair prompt that supplied the
    answer would measure the prompt.

    Args:
        library: The pack the repair prompt is rendered from.
        repair_prompt_id: Which record to render.
    """

    library: PromptLibrary
    repair_prompt_id: str = "benchmarks.structured_output.repair"

    def run(self, caller: TurnCaller, case: BenchmarkCase) -> InteractionOutcome:
        """Make the call, validate it, and retry once if it did not conform.

        Args:
            caller: The engine's next-turn function.
            case: The case, carrying its schema under ``expectation["schema"]``.

        Returns:
            The outcome. ``text`` is the **final** attempt's text — the one the scorer scores —
            and ``detail`` carries ``first_attempt_conformed`` and ``recovered_after_retry`` so
            the two rates stay separable.
        """
        from modelrack.errors import ProviderError

        schema = case.expectation.get("schema")
        schema = schema if isinstance(schema, dict) else {}
        response_format = (
            ResponseFormat(kind=ResponseFormatKind.JSON_SCHEMA, schema=schema) if schema else None
        )
        messages = _conversation(case)
        results: list[GenerationResult] = []
        try:
            first = caller(messages, response_format=response_format)
        except ProviderError as exc:
            return InteractionOutcome(error_code=exc.code, error_text=exc.message)
        results.append(first)

        failures = _failures(first.text, schema)
        if not failures:
            return InteractionOutcome(
                results=tuple(results),
                text=first.text,
                detail={"first_attempt_conformed": 1.0, "retried": 0.0},
            )

        rendered = self.library.render(
            self.repair_prompt_id,
            {
                "error_summary": "\n".join(failures),
                "schema_json": json.dumps(schema, sort_keys=True),
            },
        )
        messages.append(Message(role=Role.ASSISTANT, content=first.text or " "))
        messages.append(Message(role=Role.USER, content=rendered.user))
        try:
            second = caller(messages, response_format=response_format)
        except ProviderError as exc:
            return InteractionOutcome(
                results=tuple(results),
                text=first.text,
                detail={"first_attempt_conformed": 0.0, "retried": 1.0},
                error_code=exc.code,
                error_text=exc.message,
            )
        results.append(second)
        recovered = not _failures(second.text, schema)
        return InteractionOutcome(
            results=tuple(results),
            text=second.text,
            detail={
                "first_attempt_conformed": 0.0,
                "retried": 1.0,
                "recovered_after_retry": 1.0 if recovered else 0.0,
                "first_attempt_failures": failures,
            },
        )


def _failures(text: str, schema: Mapping[str, Any]) -> list[str]:
    """Return the validator's complaints about ``text``, as lines for the repair prompt.

    Empty when the answer conforms — including when the case declares no schema at all, which is
    not a conformance failure but an absence of anything to conform to.
    """
    if not schema:
        return []
    document, parse_error = extract_json(text)
    if parse_error is not None:
        return [parse_error]
    try:
        violations = validate(document, schema)
    except SchemaUnsupported as exc:  # pragma: no cover — shipped case schemas are bounded
        return [str(exc)]
    return [f"{violation.path}: {violation.detail}" for violation in violations]
