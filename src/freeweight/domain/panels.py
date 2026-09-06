"""The A-2 panel: what gets measured on an adapter subject, and what does not.

[ADR-0059](../../../docs/adr/0059-adapter-evidence-is-measured-never-inherited.md) settles the
policy — a new adapter subject has **no evidence**, and its panel is declared capabilities plus a
fixed regression panel plus performance — and
[benchmark catalogue §8](../../../docs/apps/freeweight/benchmark-catalog.md) settles the content.
This module is that policy as code.

**Nothing here reads a database and nothing here scores.** It takes a manifest's claims and what is
already known about a base, and returns the suites to run. That is what makes the composition
testable without a GPU, and it is why the panel can be printed before a single run is started.

The one rule that matters more than the rest: **evidence is never inherited**. The base's
measurements decide only *which suite fills the regression panel's third row* — they never become
the adapter subject's evidence, at any weight, discounted or otherwise. A subject that has not been
measured reads `—`, not a number
([ADR-0016](../../../docs/adr/0016-unavailable-is-not-zero.md)).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from freeweight.domain.capability_mapping import CapabilityMapping

__all__ = [
    "FIXED_REGRESSION_MAX_OUTPUT_TOKENS",
    "FIXED_REGRESSION_SUITES",
    "GOAL_ROOT",
    "GOAL_SUITE_PREFIX",
    "PERFORMANCE_SUITE",
    "REGRESSION_PANEL_VERSION",
    "Panel",
    "PanelPart",
    "compose_panel",
    "strongest_capability",
]

FIXED_REGRESSION_SUITES: Final[tuple[str, ...]] = (
    "native.instruction_following",
    "native.structured_output",
)
"""Rows 1 and 2 of the regression panel, the same in every installation.

`native.instruction_following` catches the most common LoRA regression there is — an adapter that
learned a voice and stopped taking direction. `native.structured_output` catches the one that
breaks a tool-calling pipeline outright, and is cheap and deterministic.

**Not configurable, deliberately** (catalogue §8.2). A per-deployment regression panel is not a
regression panel: two adapters' regression numbers stop being comparable and "the regression panel"
stops meaning one thing. Changing this tuple is a change to the catalogue, versioned with it and
visible in review — which is the point of it being here rather than in `[adapters]`."""

FIXED_REGRESSION_MAX_OUTPUT_TOKENS: Final = 512
"""The output cap the two fixed regression rows run under
([ADR-0089](../../../docs/adr/0089-the-fixed-regression-rows-bound-their-own-output.md)).

Part of the panel's definition and versioned with the catalogue, for the same reason the suite
tuple above is: a subject measured at 512 and a subject measured at 4096 have not been measured the
same way, so a per-deployment cap would reintroduce the incomparability the fixed panel exists to
prevent.

**512 is chosen to be unreachable by a model that is behaving.** On the reference machine the
longest answer any healthy subject produced on either row was 61 tokens — from the adapter
explicitly trained to answer at length — against a median of 17. A *damaged* adapter has usually
lost the instruction to stop along with every other instruction, and generates until the served
context runs out; capping it turns a forty-minute panel into a one-minute one.

A sample that ends at the cap records ``finish_reason = "length"`` and is scored as what it is: an
answer that did not comply. On a suite whose subject is whether the model does what it was told,
failing to stop *is* a failure to follow instructions (ADR-0089 rule 2).

Applies to :data:`FIXED_REGRESSION_SUITES` and to nothing else. The declared part, the performance
part and row 3 run ordinary capability suites whose output needs are set by their own definitions,
and a cap chosen for a three-word-answer suite would truncate a long-context benchmark and record
the truncation as a capability loss."""

GOAL_ROOT: Final = "user"
"""The reserved capability root a goal suite emits under (ADR-0032 §1).

A ``user.<slug>`` term is **not** in the capability mapping and cannot be: goals map themselves,
and the mapping file refuses to declare sources for one. So the panel resolves it directly to the
``goal.<slug>`` suite that produces it, which is why a house-voice LoRA declaring
``user.house_voice`` gets its own goal suite in the declared part with no special case anywhere
else (catalogue §8.4)."""

GOAL_SUITE_PREFIX: Final = "goal."
"""How a goal slug becomes a suite key."""

PERFORMANCE_SUITE: Final = "native.performance"
"""The performance part. Decode throughput and TTFT **with this adapter active** are the subject's
own numbers: a LoRA is extra matrix multiplies per token, so the base's figure is not the
subject's."""

REGRESSION_PANEL_VERSION: Final = "1"
"""Bumped whenever :data:`FIXED_REGRESSION_SUITES` or the third row's rule changes.

Recorded on every composed panel, so two subjects' regression numbers can be compared only when
they were taken under the same panel — and so a panel composed six months apart is visibly a
different panel rather than silently one."""


@dataclass(frozen=True, slots=True)
class PanelPart:
    """One part of a panel, and why its suites are in it.

    Attributes:
        name: ``declared``, ``regression`` or ``performance``.
        suites: The suite keys to run, in order, without duplicates.
        reason: One line a person can read in the CLI or the UI.
    """

    name: str
    suites: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class Panel:
    """The suites to run against one subject, in three parts and no more.

    Attributes:
        parts: ``declared``, ``regression``, ``performance``, always in that order and always all
            three — a part may be empty, which is information rather than an omission.
        panel_version: :data:`REGRESSION_PANEL_VERSION` at composition.
        unmapped_capabilities: Terms the manifest declared that no suite in this build measures.
            Reported rather than dropped: a manifest claiming something unmeasurable is a fact
            about the claim, and silently ignoring it would make the panel look complete.
        regression_third_row: The suite chosen for the base's strongest measured capability, or
            ``None`` when the base has no evidence to choose from.
    """

    parts: tuple[PanelPart, ...]
    panel_version: str = REGRESSION_PANEL_VERSION
    unmapped_capabilities: tuple[str, ...] = ()
    regression_third_row: str | None = None

    @property
    def suites(self) -> tuple[str, ...]:
        """Every suite in the panel, in part order, each appearing once."""
        seen: dict[str, None] = {}
        for part in self.parts:
            for suite in part.suites:
                seen.setdefault(suite, None)
        return tuple(seen)

    def part(self, name: str) -> PanelPart:
        """Return the named part.

        Raises:
            KeyError: No part of that name — the three are fixed.
        """
        for part in self.parts:
            if part.name == name:
                return part
        raise KeyError(name)

    def as_json(self) -> dict[str, Any]:
        """Render the panel for ``--json`` and for the UI."""
        return {
            "panel_version": self.panel_version,
            "suites": list(self.suites),
            "parts": [
                {"name": part.name, "suites": list(part.suites), "reason": part.reason}
                for part in self.parts
            ],
            "regression_third_row": self.regression_third_row,
            "unmapped_capabilities": list(self.unmapped_capabilities),
        }


def strongest_capability(base_scores: Mapping[str, float]) -> str | None:
    """Return the base's highest-scoring measured capability, or ``None``.

    Args:
        base_scores: ``{capability_id: score}`` for the **bare base**, from its own evidence.
            Empty when the base has not been measured.

    Returns:
        The capability with the highest score; ties break on the capability id, so the choice is
        deterministic and two compositions of the same data agree. ``None`` for an unmeasured base
        — the rule cannot invent a strongest capability, and ADR-0016 is why it does not try.
    """
    if not base_scores:
        return None
    return min(base_scores, key=lambda capability: (-base_scores[capability], capability))


def _goal_suite(capability: str) -> str | None:
    """Return the ``goal.<slug>`` suite a ``user.<slug>`` capability comes from, or ``None``."""
    prefix = f"{GOAL_ROOT}."
    if not capability.startswith(prefix) or len(capability) <= len(prefix):
        return None
    return f"{GOAL_SUITE_PREFIX}{capability[len(prefix) :]}"


def _suites_for(mapping: CapabilityMapping, capabilities: Iterable[str]) -> tuple[str, ...]:
    """Return the suites that feed these capabilities, without duplicates.

    Mapping order for shipped capabilities; a ``user.<slug>`` term resolves directly to its goal
    suite, because goals map themselves and the mapping file refuses to declare sources for one.
    """
    wanted = list(dict.fromkeys(capabilities))
    ordered: dict[str, None] = {}
    for capability, sources in mapping.sources.items():
        if capability not in wanted:
            continue
        for source in sources:
            ordered.setdefault(source.suite_key, None)
    for capability in wanted:
        goal = _goal_suite(capability)
        if goal is not None:
            ordered.setdefault(goal, None)
    return tuple(ordered)


def compose_panel(
    mapping: CapabilityMapping,
    *,
    declared_capabilities: Sequence[str] = (),
    base_scores: Mapping[str, float] | None = None,
    available_suites: Sequence[str] | None = None,
) -> Panel:
    """Compose the panel for one adapter subject (ADR-0059 §2).

    Three parts and no more. A full base panel per adapter is hours of GPU time for information
    that is mostly a re-measurement of the base, and that cost is what makes people stop measuring
    adapters at all.

    Args:
        mapping: The capability mapping, which is what turns a declared capability into suites.
        declared_capabilities: The manifest's `declared_capabilities` — **the claim under test**.
            A `user.<slug>` term maps to that goal suite like any other; there is no special case,
            and a house-voice LoRA scored by a calibrated house-voice goal is the intended pairing.
        base_scores: The **bare base's** measured capability scores, used only to choose the
            regression panel's third row. They never become the subject's own evidence, at any
            weight (ADR-0059 rule 1) — this argument decides which suite to *run*, not what to
            record.
        available_suites: The suites this build can actually run, or ``None`` to skip the filter.
            A panel naming a suite the registry does not have would fail at run time instead of at
            composition, where a person can see it.

    Returns:
        The panel. Every part is present even when empty: a subject whose manifest declares nothing
        has an empty `declared` part, which is a fact about the manifest and not a gap in the
        panel.
    """
    runnable = None if available_suites is None else set(available_suites)

    def keep(suites: Iterable[str]) -> tuple[str, ...]:
        return tuple(suite for suite in suites if runnable is None or suite in runnable)

    declared = tuple(dict.fromkeys(declared_capabilities))
    known = set(mapping.capabilities)

    def is_measurable(term: str) -> bool:
        return term in known or _goal_suite(term) is not None

    unmapped = tuple(term for term in declared if not is_measurable(term))
    declared_suites = keep(_suites_for(mapping, (term for term in declared if is_measurable(term))))

    third_row_capability = strongest_capability(base_scores or {})
    third_row_suites = (
        keep(_suites_for(mapping, [third_row_capability]))
        if third_row_capability is not None
        else ()
    )
    third_row = third_row_suites[0] if third_row_suites else None

    regression = keep(FIXED_REGRESSION_SUITES)
    if third_row is not None and third_row not in regression:
        regression = (*regression, third_row)

    if third_row_capability is None:
        regression_reason = (
            f"the fixed regression panel v{REGRESSION_PANEL_VERSION}, catching a LoRA that stopped "
            "taking direction or stopped emitting valid structure. The third row — the base's "
            "strongest measured capability — is absent because this base has no evidence yet; "
            "measure the base first."
        )
    else:
        regression_reason = (
            f"the fixed regression panel v{REGRESSION_PANEL_VERSION}, catching a LoRA that stopped "
            "taking direction or stopped emitting valid structure, plus this base's strongest "
            f"measured capability ({third_row_capability}) — what these weights were for."
        )

    return Panel(
        parts=(
            PanelPart(
                name="declared",
                suites=declared_suites,
                reason=(
                    "the claim under test: every suite feeding a capability this adapter's "
                    "manifest declares."
                ),
            ),
            PanelPart(name="regression", suites=regression, reason=regression_reason),
            PanelPart(
                name="performance",
                suites=keep((PERFORMANCE_SUITE,)),
                reason=(
                    "throughput and TTFT with this adapter active — a LoRA is extra work per "
                    "token, so the base's number is not this subject's."
                ),
            ),
        ),
        panel_version=REGRESSION_PANEL_VERSION,
        unmapped_capabilities=unmapped,
        regression_third_row=third_row,
    )
