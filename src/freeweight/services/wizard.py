"""freeweight.services.wizard — the seven-step goal authoring wizard.

Subjective Goals §7, as a state machine. ``freeweight goals init`` runs the same interview in a
terminal and this module is what the web wizard runs; both produce the same goal pack, and neither
is required — a pack written by hand in an editor is equally first-class (ADR-0031 §6).

**Step 2 is the part that earns the feature**, and it is the reason this module exists at all
rather than the routes assembling a pack directly. For each criterion the wizard asks two
questions:

    Could two people who both read your description grade the same essay the same way?
    Is this one quality, or two stuck together?

and then it has to *make the split visible* rather than performing it. ``not LinkedIn`` is a
vocabulary problem and a register problem, and splitting it is what makes both measurable.
:meth:`WizardDraft.split` therefore records ``split_from`` on both halves and keeps the original's
words, so the user can see what their sentence turned into and undo it. A wizard that quietly
rewrote the rubric would be measuring its own edit (ADR-0031 §3).

**The rule proposer never applies anything.** :func:`propose_rules` returns proposals with their
parameters pre-filled and ``accepted`` false; only :func:`accept_rule` moves a criterion's rung.
``tests/integration/test_starter_packs.py`` asserts that on the *persisted pack*, because a
proposal that quietly became a rule would change what a goal measures without the author having
agreed to it.

**Where a draft lives, and why.** Steps 1–4 are pre-pack state and live in ``wizard_drafts``, one
row per draft, with ``created_at``, ``updated_at`` and an ``expires_at`` that makes an abandoned
draft disappear rather than accumulate. Steps 5–6 are not stored here at all: the grading step
writes real ``calibration_samples`` and ``calibration_grades`` rows through
:mod:`freeweight.services.calibration`, which is what makes grading survive a refresh, a restart
and an out-of-order submission. Only the part that is not yet a goal is a draft.

Drafts previously lived as JSON values in the ``settings`` table, which could express none of that
lifecycle and made ``db status`` count a half-written goal as a setting.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from baseaicore import NotFoundError, ValidationError, new_id, to_rfc3339, utc_now

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from freeweight.services.database import Database
    from freeweight.services.goals import LoadedGoal

__all__ = [
    "DRAFT_TTL_DAYS",
    "SPLIT_QUESTIONS",
    "DraftCriterion",
    "DraftNotFound",
    "DraftTask",
    "RuleProposal",
    "WeightShift",
    "WizardDraft",
    "WizardStep",
    "accept_rule",
    "delete_draft",
    "draft_from_goal",
    "set_scale",
    "load_draft",
    "pack_body",
    "propose_rules",
    "save_draft",
    "purge_expired_drafts",
    "save_pack",
    "start_draft",
    "weight_shift",
]

DRAFT_TTL_DAYS = 30
"""How long an untouched draft survives.

Long enough that authoring a rubric over a fortnight is normal, short enough that a draft abandoned
after one screen does not sit in the database forever. Every save extends it: the clock measures
neglect, not age."""

SPLIT_QUESTIONS: tuple[str, str] = (
    "Could two people who both read your description grade the same text the same way?",
    "Is this one quality, or two stuck together?",
)
"""The two questions step 2 asks of every criterion.

Held as data rather than as template copy so the CLI interview and the web wizard ask exactly the
same thing — the questions *are* the feature, and two surfaces asking them differently would be
two different features."""

_SCALE_POINTS: frozenset[int] = frozenset({3, 5, 7})
"""The ordinal scale sizes a judged criterion may declare (Subjective Goals §3.4)."""

_MINIMUM_DESCRIBED_POINTS = 3
"""Top, middle and bottom. Fewer is an unanchored scale, which the pack lint refuses."""

GRADING_COST_MINUTES_PER_SAMPLE = 2
"""Rule of thumb the wizard states **before** the user invests in grading.

Twelve samples across five criteria is a real sitting. Saying so up front is the difference
between a user who chose to spend half an hour and one who feels tricked into it."""


class WizardStep(StrEnum):
    """The seven steps, in order.

    Attributes:
        INTENT: What are you trying to get?
        CRITERIA: Break it into criteria, and answer the two questions of each.
        RULES: The application proposes rules; the user accepts, edits or skips each.
        TASKS: The user's own prompts.
        GRADE: Generate candidate outputs and grade them, blinded and shuffled.
        AGREEMENT: See the agreement, in words, with the samples that disagreed.
        SAVE: Write the pack, and show the path and the ``goal_hash``.
    """

    INTENT = "intent"
    CRITERIA = "criteria"
    RULES = "rules"
    TASKS = "tasks"
    GRADE = "grade"
    AGREEMENT = "agreement"
    SAVE = "save"

    @property
    def ordinal(self) -> int:
        """This step's position, from 1."""
        return list(WizardStep).index(self) + 1

    @property
    def heading(self) -> str:
        """The heading the wizard shows for this step.

        Not ``title``: ``StrEnum`` inherits ``str.title``, and shadowing a string method with a
        property makes the enum stop behaving like the string it is.
        """
        return _STEP_TITLES[self]


_STEP_TITLES: Mapping[WizardStep, str] = {
    WizardStep.INTENT: "What are you trying to get?",
    WizardStep.CRITERIA: "Break it into criteria",
    WizardStep.RULES: "Rules that could carry part of it",
    WizardStep.TASKS: "Your tasks",
    WizardStep.GRADE: "Generate and grade",
    WizardStep.AGREEMENT: "See the agreement",
    WizardStep.SAVE: "Save",
}


class DraftNotFound(NotFoundError):
    """No wizard draft has that id."""


@dataclass(frozen=True, slots=True)
class DraftCriterion:
    """One criterion as the wizard holds it before a pack exists.

    Attributes:
        key: The criterion key, derived from the name and stable once set.
        name: The user's own words.
        intent: Their description of what it means.
        rung: ``rule``, ``reference``, ``human`` or ``judge``. Starts as ``judge`` and moves left
            only when the user accepts a rule.
        weight: Its share of the composite.
        scale_points: The ordinal scale, for a judged criterion.
        descriptors: Scale descriptors by point, as strings.
        rule: The accepted rule block, or ``None``.
        graded_alike: The user's answer to the first of :data:`SPLIT_QUESTIONS`.
        one_quality: Their answer to the second.
        split_from: The key of the criterion this was split out of, or ``None``.
    """

    key: str
    name: str
    intent: str = ""
    rung: str = "judge"
    weight: float = 0.0
    scale_points: int = 5
    descriptors: Mapping[str, str] = field(default_factory=dict)
    rule: Mapping[str, Any] | None = None
    graded_alike: bool | None = None
    one_quality: bool | None = None
    split_from: str | None = None

    @property
    def is_deterministic(self) -> bool:
        """Whether this criterion is scored without a judge."""
        return self.rung in {"rule", "reference"}

    @property
    def needs_descriptors(self) -> bool:
        """Whether this criterion is judged and its scale is still unanchored.

        The pack lint refuses a judged criterion that describes fewer than three of its points, so
        this is what the wizard shows *before* the user reaches a save that would fail.
        """
        return self.rung == "judge" and len(self.descriptors) < _MINIMUM_DESCRIBED_POINTS

    @property
    def needs_attention(self) -> bool:
        """Whether step 2's questions still have an answer the user should act on.

        ``True`` when the user said two people would *not* grade it alike, or that it is two
        qualities — either answer is the wizard's cue to offer a split, and leaving it unanswered
        is not the same as answering "no".
        """
        return self.graded_alike is False or self.one_quality is False

    def as_json(self) -> dict[str, Any]:
        """The stored form."""
        return {
            "key": self.key,
            "name": self.name,
            "intent": self.intent,
            "rung": self.rung,
            "weight": self.weight,
            "scale_points": self.scale_points,
            "descriptors": dict(self.descriptors),
            "rule": dict(self.rule) if self.rule else None,
            "graded_alike": self.graded_alike,
            "one_quality": self.one_quality,
            "split_from": self.split_from,
        }


@dataclass(frozen=True, slots=True)
class DraftTask:
    """One task the user supplied, or a starter they have not replaced yet."""

    key: str
    name: str
    prompt_text: str
    is_starter: bool = False

    def as_json(self) -> dict[str, Any]:
        """The stored form."""
        return {
            "key": self.key,
            "name": self.name,
            "prompt_text": self.prompt_text,
            "is_starter": self.is_starter,
        }


@dataclass(frozen=True, slots=True)
class WizardDraft:
    """Everything the wizard holds before the pack is written.

    Attributes:
        draft_id: Opaque identifier, carried in the URL so a refresh resumes.
        step: Where the user is.
        slug: The goal's slug.
        name: Its display name.
        intent: Step 1's free text. Nothing is inferred from it; it is stored so the goal is
            legible in six months.
        criteria: Step 2 and 3's criteria.
        tasks: Step 4's tasks.
        forked_from: The starter this began as, or ``None``.
        saved_slug: Set once step 7 has written the pack, so a refresh of the final step shows
            what happened rather than writing it twice.
        created_at: When the draft began.
        updated_at: When it last changed.
    """

    draft_id: str
    step: WizardStep = WizardStep.INTENT
    slug: str = ""
    name: str = ""
    intent: str = ""
    criteria: tuple[DraftCriterion, ...] = ()
    tasks: tuple[DraftTask, ...] = ()
    forked_from: str | None = None
    saved_slug: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def judged(self) -> tuple[DraftCriterion, ...]:
        """The criteria a jury would have to score."""
        return tuple(item for item in self.criteria if item.rung == "judge")

    @property
    def total_weight(self) -> float:
        """The sum of the declared weights."""
        return sum(item.weight for item in self.criteria)

    def criterion(self, key: str) -> DraftCriterion | None:
        """One criterion by key, or ``None``."""
        return next((item for item in self.criteria if item.key == key), None)

    def as_json(self) -> dict[str, Any]:
        """The stored form."""
        return {
            "draft_id": self.draft_id,
            "step": self.step.value,
            "slug": self.slug,
            "name": self.name,
            "intent": self.intent,
            "criteria": [item.as_json() for item in self.criteria],
            "tasks": [item.as_json() for item in self.tasks],
            "forked_from": self.forked_from,
            "saved_slug": self.saved_slug,
            "created_at": to_rfc3339(self.created_at) if self.created_at else None,
            "updated_at": to_rfc3339(self.updated_at) if self.updated_at else None,
        }


def _from_json(body: Mapping[str, Any]) -> WizardDraft:
    """Rebuild a draft from its stored form."""
    from baseaicore import from_rfc3339

    return WizardDraft(
        draft_id=str(body["draft_id"]),
        step=WizardStep(str(body.get("step", "intent"))),
        slug=str(body.get("slug", "")),
        name=str(body.get("name", "")),
        intent=str(body.get("intent", "")),
        criteria=tuple(
            DraftCriterion(
                key=str(entry["key"]),
                name=str(entry.get("name", "")),
                intent=str(entry.get("intent", "")),
                rung=str(entry.get("rung", "judge")),
                weight=float(entry.get("weight", 0.0)),
                scale_points=int(entry.get("scale_points", 5)),
                descriptors={str(k): str(v) for k, v in (entry.get("descriptors") or {}).items()},
                rule=entry.get("rule"),
                graded_alike=entry.get("graded_alike"),
                one_quality=entry.get("one_quality"),
                split_from=entry.get("split_from"),
            )
            for entry in body.get("criteria", ())
        ),
        tasks=tuple(
            DraftTask(
                key=str(entry["key"]),
                name=str(entry.get("name", "")),
                prompt_text=str(entry.get("prompt_text", "")),
                is_starter=bool(entry.get("is_starter", False)),
            )
            for entry in body.get("tasks", ())
        ),
        forked_from=body.get("forked_from"),
        saved_slug=body.get("saved_slug"),
        created_at=from_rfc3339(body["created_at"]) if body.get("created_at") else None,
        updated_at=from_rfc3339(body["updated_at"]) if body.get("updated_at") else None,
    )


def _slugify(value: str) -> str:
    """Derive a legal slug or criterion key from free text."""
    cleaned = "".join(character if character.isalnum() else "_" for character in value.lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:48] or "goal"


def start_draft(database: Database, *, intent: str, name: str = "") -> WizardDraft:
    """Begin a draft at step 1.

    Args:
        database: The application's database handle.
        intent: The user's own description of what they are trying to get.
        name: A display name; derived from the intent's first words when omitted.

    Returns:
        The stored draft.

    Raises:
        ValidationError: ``intent`` is empty. Step 1 is the one field the wizard cannot supply a
            default for — it is the sentence the whole goal is legible by.
    """
    if not intent.strip():
        raise ValidationError(
            "Say what you are trying to get, in your own words. Nothing is inferred from it; it "
            "is stored so the goal still makes sense in six months.",
            details={"field": "intent"},
        )
    label = name.strip() or " ".join(intent.split()[:6])
    now = utc_now()
    draft = WizardDraft(
        draft_id=new_id(),
        step=WizardStep.CRITERIA,
        slug=_slugify(label),
        name=label,
        intent=intent.strip(),
        created_at=now,
        updated_at=now,
    )
    save_draft(database, draft)
    return draft


def load_draft(database: Database, draft_id: str) -> WizardDraft:
    """Load one draft.

    An expired draft reads as absent rather than as a row: ``expires_at`` is the fact, and a
    collector running or not running does not change whether the draft is still there.

    Raises:
        DraftNotFound: No draft has that id, or it has expired.
    """
    from freeweight.infrastructure.db.models_goals import WizardDraft as DraftRow

    with database.read() as session:
        row = session.get(DraftRow, draft_id)
        body = row.body_json if row is not None else None
        expired = row is not None and row.expires_at <= utc_now()
    if body is None or expired:
        raise DraftNotFound(
            f"No wizard draft {draft_id!r}. Start again from the goals page — nothing was saved "
            "under that link.",
            details={"draft": draft_id},
        )
    parsed = json.loads(body) if isinstance(body, str) else body
    if not isinstance(parsed, Mapping):  # pragma: no cover — only a hand-edited row reaches this
        raise DraftNotFound(
            f"Wizard draft {draft_id!r} is not a draft document.", details={"draft": draft_id}
        )
    return _from_json(parsed)


def save_draft(database: Database, draft: WizardDraft) -> WizardDraft:
    """Store a draft, stamping ``updated_at`` and pushing its expiry out.

    Every step writes through here, so a refresh at any point resumes where the user was rather
    than at the beginning — and every step also renews the draft, so a rubric written slowly is
    never collected out from under its author.
    """
    from freeweight.infrastructure.db.models_goals import WizardDraft as DraftRow

    now = utc_now()
    stamped = replace(draft, updated_at=now, created_at=draft.created_at or now)
    expires = now + timedelta(days=DRAFT_TTL_DAYS)
    with database.write() as session:
        row = session.get(DraftRow, draft.draft_id)
        if row is None:
            session.add(
                DraftRow(
                    id=draft.draft_id,
                    slug=stamped.slug or None,
                    body_json=stamped.as_json(),
                    created_at=stamped.created_at or now,
                    updated_at=now,
                    expires_at=expires,
                )
            )
        else:
            row.slug = stamped.slug or None
            row.body_json = stamped.as_json()
            row.updated_at = now
            row.expires_at = expires
    return stamped


def delete_draft(database: Database, draft_id: str) -> None:
    """Remove a draft once its pack is written, or when the user abandons it."""
    from freeweight.infrastructure.db.models_goals import WizardDraft as DraftRow

    with database.write() as session:
        row = session.get(DraftRow, draft_id)
        if row is not None:
            session.delete(row)


def purge_expired_drafts(database: Database) -> int:
    """Delete every draft past its expiry.

    Returns:
        How many rows were removed.

    Not scheduled and not automatic — it is called where a draft list is built, so the clean-up
    happens on the path that would otherwise show stale entries, and never behind a user's back.
    """
    from sqlalchemy import select

    from freeweight.infrastructure.db.models_goals import WizardDraft as DraftRow

    with database.write() as session:
        expired = list(session.scalars(select(DraftRow).where(DraftRow.expires_at <= utc_now())))
        for row in expired:
            session.delete(row)
    return len(expired)


def add_criterion(
    draft: WizardDraft, *, name: str, intent: str = "", weight: float | None = None
) -> WizardDraft:
    """Append a criterion, keyed from its name.

    New criteria start judged. That is the honest default: the user has described a quality in
    words, and until a rule has been proposed *and accepted* nothing deterministic is measuring it.

    Raises:
        ValidationError: The name is empty, or its key collides with an existing criterion.
    """
    if not name.strip():
        raise ValidationError("A criterion needs a name.", details={"field": "name"})
    key = _slugify(name)
    if draft.criterion(key) is not None:
        raise ValidationError(
            f"This goal already has a criterion called {name!r}.", details={"field": "name"}
        )
    # A new criterion arrives at the current mean share rather than at zero, so that
    # normalization gives every criterion an equal share when nobody has expressed a preference —
    # and preserves the proportions when somebody has. Appending at zero would leave the *newest*
    # criterion weightless, which is a weight the pack lint refuses and a surprise nobody wants.
    default = (draft.total_weight / len(draft.criteria)) if draft.criteria else 1.0
    criteria = (
        *draft.criteria,
        DraftCriterion(
            key=key,
            name=name.strip(),
            intent=intent.strip(),
            weight=default if weight is None else float(weight),
        ),
    )
    return _rebalance(replace(draft, criteria=criteria))


def answer_questions(
    draft: WizardDraft, key: str, *, graded_alike: bool | None, one_quality: bool | None
) -> WizardDraft:
    """Record the user's answers to step 2's two questions for one criterion."""
    criteria = tuple(
        replace(item, graded_alike=graded_alike, one_quality=one_quality)
        if item.key == key
        else item
        for item in draft.criteria
    )
    return replace(draft, criteria=criteria)


def set_scale(
    draft: WizardDraft,
    key: str,
    *,
    points: int = 5,
    descriptors: Mapping[str, str] | None = None,
) -> WizardDraft:
    """Set a judged criterion's ordinal scale and its descriptors.

    A judged criterion with no descriptors **fails validation** (Subjective Goals §3.4), and it
    should: "rate the tone 1-5" gives a jury nothing to anchor on and reliably produces a
    ``kappa_w`` near zero. So the wizard asks for the top, the middle and the bottom of the scale
    in the author's own words, and refuses to write a pack that would be rejected at load time —
    which is far better than writing one and failing at the next server start.

    Args:
        draft: The draft.
        key: Which criterion.
        points: The scale size, 3, 5 or 7.
        descriptors: ``{point: description}``. Blank entries are dropped rather than stored, so a
            half-filled form does not produce a scale that claims to be anchored and is not.

    Returns:
        The draft with that criterion's scale set.

    Raises:
        ValidationError: The criterion does not exist, the scale size is not one of the three, or
            fewer than three points were described.
    """
    if draft.criterion(key) is None:
        raise ValidationError(f"No criterion {key!r}.", details={"criterion": key})
    if points not in _SCALE_POINTS:
        raise ValidationError(
            f"A scale has {', '.join(str(value) for value in sorted(_SCALE_POINTS))} points; "
            f"got {points}.",
            details={"field": "points", "value": points},
        )
    filled = {
        str(point): str(text).strip()
        for point, text in (descriptors or {}).items()
        if str(text).strip()
    }
    if len(filled) < _MINIMUM_DESCRIBED_POINTS:
        raise ValidationError(
            "Describe at least the top, the middle and the bottom of the scale. A judged "
            "criterion with no descriptors gives the jury nothing to anchor on, and its agreement "
            "with you will be near zero however carefully you grade.",
            details={"field": "descriptors", "criterion": key},
        )
    criteria = tuple(
        replace(item, scale_points=points, descriptors=filled) if item.key == key else item
        for item in draft.criteria
    )
    return replace(draft, criteria=criteria)


def split_criterion(draft: WizardDraft, key: str, *, first: str, second: str) -> WizardDraft:
    """Split one criterion into two, recording where both halves came from.

    This is the move that makes "not LinkedIn" measurable: a vocabulary criterion and a register
    criterion, each of which one rule can carry part of. The wizard makes the move *visible* —
    both halves record ``split_from``, and the original's weight is divided between them — rather
    than performing it silently on the user's words.

    Raises:
        ValidationError: The criterion does not exist, or a half has no name.
    """
    original = draft.criterion(key)
    if original is None:
        raise ValidationError(f"No criterion {key!r} to split.", details={"criterion": key})
    if not first.strip() or not second.strip():
        raise ValidationError(
            "Both halves need a name. If only one of them is a real quality, rename the criterion "
            "instead of splitting it.",
            details={"field": "first" if not first.strip() else "second"},
        )
    halves = tuple(
        DraftCriterion(
            key=_slugify(label),
            name=label.strip(),
            intent=original.intent,
            rung="judge",
            weight=original.weight / 2,
            scale_points=original.scale_points,
            descriptors=dict(original.descriptors),
            split_from=original.key,
        )
        for label in (first, second)
    )
    criteria: list[DraftCriterion] = []
    for item in draft.criteria:
        if item.key == key:
            criteria.extend(halves)
        else:
            criteria.append(item)
    return _rebalance(replace(draft, criteria=tuple(criteria)))


def _rebalance(draft: WizardDraft) -> WizardDraft:
    """Normalize weights to sum to 1.0, preserving relative shares.

    A pack whose weights do not sum to one is a lint *error*, so the wizard cannot hand the user a
    pack it knows is invalid. Equal shares when nothing has been set yet; proportional otherwise,
    so a user who has expressed a preference keeps it.
    """
    if not draft.criteria:
        return draft
    total = sum(item.weight for item in draft.criteria)
    if total <= 0:
        share = round(1.0 / len(draft.criteria), 4)
        weights = [share] * len(draft.criteria)
    else:
        weights = [round(item.weight / total, 4) for item in draft.criteria]
    # Rounding leaves a remainder; it goes on the first criterion so the sum is exact.
    weights[0] = round(weights[0] + (1.0 - sum(weights)), 4)
    return replace(
        draft,
        criteria=tuple(
            replace(item, weight=weight)
            for item, weight in zip(draft.criteria, weights, strict=True)
        ),
    )


@dataclass(frozen=True, slots=True)
class RuleProposal:
    """One rule the application offers for one criterion.

    Attributes:
        criterion_key: Which criterion it would carry part of.
        rule_type: The ``rule.type`` it would set.
        parameters: Pre-filled, editable parameters.
        explanation: Why it was proposed, in the user's own words where possible.
        accepted: Whether the user has accepted it. **Always false when proposed** — the proposer
            never applies a rule (Subjective Goals §7 step 3).
    """

    criterion_key: str
    rule_type: str
    parameters: Mapping[str, Any]
    explanation: str
    accepted: bool = False

    def as_json(self) -> dict[str, Any]:
        """The wire form."""
        return {
            "criterion": self.criterion_key,
            "rule_type": self.rule_type,
            "parameters": dict(self.parameters),
            "explanation": self.explanation,
            "accepted": self.accepted,
        }


_DEFAULT_PARAMETERS: Mapping[str, Mapping[str, Any]] = {
    "forbidden_phrases": {
        "case_sensitive": False,
        "phrases": [
            "delve",
            "leverage",
            "in today's landscape",
            "it's worth noting",
            "navigate the complexities",
            "tapestry",
            "unlock",
            "seamless",
            "robust",
            "game changer",
            "at the end of the day",
            "circle back",
            "synergy",
            "best-in-class",
        ],
        "max_hits": 4,
    },
    "required_phrases": {"case_sensitive": False, "min_occurrences": 1, "phrases": []},
    "word_count": {"min": 100, "max": 800, "tolerance": 200},
    "sentence_length_distribution": {"mean_words": {"min": 10, "max": 24}, "cv": {"min": 0.4}},
    "paragraph_shape": {"paragraphs": {"min": 2, "max": 12}},
    "readability": {"metric": "flesch_kincaid_grade", "min": 8.0, "max": 14.0, "tolerance": 4.0},
    "pov_tense": {"person": "first", "tense": "past", "tolerance": 0.15},
    "vocabulary_profile": {
        "banned_register": ["amazing", "incredible", "effortlessly", "simply", "obviously"],
        "type_token_ratio": {"min": 0.3},
    },
    "punctuation_profile": {"exclamation_per_1000_words": {"max": 1.0}},
    "structure": {"headings": {"min": 1, "max": 6}, "max_heading_depth": 3},
    "json_schema": {},
    "regex_match": {"pattern": ""},
    "repetition": {"n": 5, "max_rate": 0.1},
    "entity_recall": {"case_sensitive": False},
    "claim_coverage": {"min_overlap": 0.5},
    "no_unsupported_claims": {"check": ["numbers", "entities"]},
    "reference_similarity": {},
}
"""Starting parameters for each proposable rule type.

Pre-filled and editable, never final. A proposal with an empty parameter block would be a
suggestion the user has to research; one with plausible values is a suggestion they can read,
disagree with, and change in ten seconds."""


def propose_rules(draft: WizardDraft) -> tuple[RuleProposal, ...]:
    """Propose the rules that could carry part of each judged criterion.

    The proposals come from :func:`freeweight.domain.goals.lint.suggest_rules`, which is a keyword
    table and nothing cleverer — deliberately, because putting a model in the authoring loop would
    make the wizard's advice unauditable and slow. It over-fires by design: a proposal the user
    disagrees with costs them one click.

    Args:
        draft: The draft, at step 3.

    Returns:
        Every proposal, in criterion order. A criterion whose rule the user has already accepted
        contributes a proposal marked ``accepted`` so the UI can show what it did.
    """
    from freeweight.domain.goals.criteria import REFERENCE_RULE_TYPES
    from freeweight.domain.goals.lint import suggest_rules
    from freeweight.domain.goals.pack import Criterion, Rung, ScaleSpec

    proposals: list[RuleProposal] = []
    for item in draft.criteria:
        if item.rule is not None:
            proposals.append(
                RuleProposal(
                    criterion_key=item.key,
                    rule_type=str(item.rule.get("type", "")),
                    parameters={k: v for k, v in item.rule.items() if k != "type"},
                    explanation="You accepted this rule.",
                    accepted=True,
                )
            )
            continue
        probe = Criterion(
            key=item.key,
            name=item.name,
            rung=Rung.JUDGE,
            weight=item.weight,
            intent=item.intent,
            scale=ScaleSpec(
                points=item.scale_points,
                descriptors={str(k): str(v) for k, v in item.descriptors.items()},
            ),
        )
        for rule_type in suggest_rules(probe):
            proposals.append(
                RuleProposal(
                    criterion_key=item.key,
                    rule_type=rule_type,
                    parameters=dict(_DEFAULT_PARAMETERS.get(rule_type, {})),
                    explanation=(
                        f"Your description of {item.name!r} reads like something a "
                        f"{rule_type.replace('_', ' ')} rule can check. Rules are free, exact, "
                        "and never disagree with you."
                        + (
                            " This one needs ground truth on the task — an annotated source."
                            if rule_type in REFERENCE_RULE_TYPES
                            else ""
                        )
                    ),
                )
            )
    return tuple(proposals)


def accept_rule(
    draft: WizardDraft,
    criterion_key: str,
    *,
    rule_type: str,
    parameters: Mapping[str, Any] | None = None,
) -> WizardDraft:
    """Accept one proposed rule, moving that criterion down the ladder.

    Args:
        draft: The draft.
        criterion_key: Which criterion the rule carries.
        rule_type: The accepted ``rule.type``.
        parameters: The user's edited parameters, or ``None`` for the pre-filled ones.

    Returns:
        The draft with that criterion's rung and rule set.

    Raises:
        ValidationError: The criterion does not exist, or the rule type is not one this build runs.
    """
    from freeweight.domain.goals.criteria import RULE_RUNGS, RULE_TYPES

    if draft.criterion(criterion_key) is None:
        raise ValidationError(
            f"No criterion {criterion_key!r}.", details={"criterion": criterion_key}
        )
    if rule_type not in RULE_TYPES:
        raise ValidationError(
            f"{rule_type!r} is not a rule this build runs. It runs {sorted(RULE_TYPES)}.",
            details={"field": "rule_type", "value": rule_type},
        )
    block = {"type": rule_type, **dict(parameters or _DEFAULT_PARAMETERS.get(rule_type, {}))}
    criteria = tuple(
        replace(item, rung=RULE_RUNGS[rule_type].value, rule=block, descriptors={})
        if item.key == criterion_key
        else item
        for item in draft.criteria
    )
    return replace(draft, criteria=criteria)


@dataclass(frozen=True, slots=True)
class WeightShift:
    """How much weight the accepted rules have moved off the judge.

    Attributes:
        deterministic: The share now scored without a judge.
        judged: The remainder.
        moved: How much has moved since every criterion was judged — which is where a draft
            starts, so this is exactly ``deterministic``.
    """

    deterministic: float
    judged: float
    moved: float

    def sentence(self) -> str:
        """The running statement the rule step shows after every accept."""
        if self.deterministic <= 0:
            return (
                "Nothing is scored deterministically yet. Every criterion here needs a judge, "
                "which means this goal's confidence will depend on how well the jury agrees "
                "with you."
            )
        return (
            f"{self.deterministic:.0%} of this goal's weight is now scored by rules — free, "
            f"exact, and they never disagree with you. The remaining {self.judged:.0%} is what "
            "the jury has to grade, and what calibration has to measure."
        )


def weight_shift(draft: WizardDraft) -> WeightShift:
    """The running statement of how much weight has moved off the judge."""
    total = draft.total_weight or 1.0
    deterministic = sum(item.weight for item in draft.criteria if item.is_deterministic) / total
    return WeightShift(
        deterministic=round(deterministic, 4),
        judged=round(1.0 - deterministic, 4),
        moved=round(deterministic, 4),
    )


def add_task(
    draft: WizardDraft, *, name: str, prompt_text: str, is_starter: bool = False
) -> WizardDraft:
    """Append a task.

    Raises:
        ValidationError: The name or the prompt is empty.
    """
    if not name.strip() or not prompt_text.strip():
        raise ValidationError(
            "A task needs a name and a prompt. Use a prompt from your real work: a voice "
            "measured on someone else's prompts is not your voice.",
            details={"field": "prompt_text" if name.strip() else "name"},
        )
    key = _slugify(name)
    if any(task.key == key for task in draft.tasks):
        raise ValidationError(
            f"This goal already has a task called {name!r}.", details={"field": "name"}
        )
    return replace(
        draft,
        tasks=(
            *draft.tasks,
            DraftTask(
                key=key,
                name=name.strip(),
                prompt_text=prompt_text.strip(),
                is_starter=is_starter,
            ),
        ),
    )


def _task_record(draft: WizardDraft, task: DraftTask) -> dict[str, Any]:
    """Render one draft task as a prompt record.

    A task is a prompt record with one extra block, so it loads through the same validator,
    renders in the same sandbox and hashes the same way as every other prompt in the suite
    (prompt standards §2.1).
    """
    stamp = to_rfc3339(draft.created_at or utc_now())
    return {
        "prompt_id": f"goals.{draft.slug}.{task.key}",
        "version": "1.0.0",
        "schema_version": "1.0",
        "purpose": f"A task for the goal {draft.name!r}, supplied by its author.",
        "task": f"goal.{draft.slug}",
        "capability": "creative_writing",
        "system": None,
        "template": task.prompt_text,
        "variables": {},
        "response": {"format": "text", "json_schema_ref": None, "expectations": []},
        "model_requirements": {
            "min_context_tokens": 4096,
            "requires_capabilities": [],
            "recommended_temperature": 0.7,
        },
        "metadata": {
            "author": "wizard",
            "created_at": stamp,
            "changed_at": stamp,
            "change_reason": "Written by the goal authoring wizard.",
            "supersedes": None,
            "tags": ["goal"],
            "goal_task": {"key": task.key, "name": task.name, "is_starter": task.is_starter},
        },
    }


def pack_body(draft: WizardDraft) -> dict[str, Any]:
    """Render the draft as a ``goal.json`` body.

    The output is an ordinary, hand-editable JSON document — the artifact the user ends up owning
    (spec §20 criterion 13). Nothing about it records that a wizard produced it beyond
    ``created_by``, because a pack written here and one written in an editor have to be the same
    kind of thing.
    """
    judged = [item for item in draft.criteria if item.rung == "judge"]
    return {
        "slug": draft.slug,
        "name": draft.name,
        "goal_pack_version": "1.0.0",
        "schema_version": "1.0",
        "intent": draft.intent,
        "created_by": "wizard",
        **({"forked_from": draft.forked_from} if draft.forked_from else {}),
        "criteria": [
            {
                "key": item.key,
                "name": item.name,
                **({"intent": item.intent} if item.intent else {}),
                "rung": item.rung,
                "weight": item.weight,
                **({"rule": dict(item.rule)} if item.rule else {}),
                **(
                    {
                        "scale": {
                            "points": item.scale_points,
                            "descriptors": dict(item.descriptors),
                        }
                    }
                    if item.rung == "judge"
                    else {}
                ),
            }
            for item in draft.criteria
        ],
        **(
            {
                "judge": {
                    "jury_size": 3,
                    "models": [],
                    "repetitions": 3,
                    "randomize_order": True,
                    "allow_remote": False,
                    "temperature": 0.0,
                }
            }
            if judged
            else {}
        ),
        "calibration": {
            "target_samples": 12,
            "min_samples": 8,
            "holdout_fraction": 0.4,
            "partition_seed": 0,
            "min_agreement": 0.4,
        },
    }


def save_pack(database: Database, root: Path, draft: WizardDraft) -> tuple[WizardDraft, LoadedGoal]:
    """Write the draft to disk as a goal pack and record that it was written.

    Idempotent from the user's point of view: once ``saved_slug`` is set, a refresh of the final
    step shows the pack that exists rather than trying to write a second one and colliding with
    itself.

    Args:
        database: The application's database handle.
        root: ``goals.root``.
        draft: The completed draft.

    Returns:
        ``(draft, goal)`` — the draft with ``saved_slug`` set, and the loaded pack.

    Raises:
        ValidationError: The draft has no criterion or no task; either would produce a pack whose
            lint refuses it, and refusing here says why in the wizard rather than at startup.
        GoalSlugCollision: A goal with that slug already exists.
    """
    from freeweight.services.goals import get_goal, write_pack

    if draft.saved_slug:
        return draft, get_goal(root, draft.saved_slug)
    if not draft.criteria:
        raise ValidationError(
            "A goal needs at least one criterion. Go back to step 2.",
            details={"field": "criteria"},
        )
    if not draft.tasks:
        raise ValidationError(
            "A goal needs at least one task — something for the models to actually answer. Go "
            "back to step 4.",
            details={"field": "tasks"},
        )
    unanchored = [item.key for item in draft.criteria if item.needs_descriptors]
    if unanchored:
        raise ValidationError(
            f"These judged criteria have no scale descriptors: {', '.join(unanchored)}. Describe "
            "the top, the middle and the bottom of each scale in step 2. A jury given "
            "'rate the tone 1 to 5' and nothing else agrees with nobody, and the pack would be "
            "refused at load time anyway.",
            details={"field": "descriptors", "criteria": unanchored},
        )
    goal = write_pack(
        root,
        goal=pack_body(draft),
        tasks=[_task_record(draft, task) for task in draft.tasks],
    )
    saved = save_draft(database, replace(draft, saved_slug=goal.slug, step=WizardStep.SAVE))
    return saved, goal


def advance(database: Database, draft: WizardDraft, step: WizardStep) -> WizardDraft:
    """Move a draft to ``step`` and store it."""
    return save_draft(database, replace(draft, step=step))


def draft_from_goal(
    database: Database, goal: LoadedGoal, *, forked_from: str | None = None
) -> WizardDraft:
    """Open an existing goal pack in the wizard, losing nothing it declares.

    This is the second half of "the artifact is yours": a pack written by hand in an editor opens
    here, and :func:`pack_body` renders it back to an equivalent document. The round trip is
    asserted at the byte level in ``tests/e2e/test_goal_wizard_journey.py``, because "without
    loss" only means something as an exact statement.

    Args:
        database: The application's database handle, for storing the draft.
        goal: The loaded pack.
        forked_from: The starter key this pack came from, when it came from one.

    Returns:
        The stored draft, at step 2.
    """
    now = utc_now()
    draft = WizardDraft(
        draft_id=new_id(),
        step=WizardStep.CRITERIA,
        slug=goal.pack.slug,
        name=goal.pack.name,
        intent=goal.pack.intent,
        forked_from=forked_from or goal.pack.forked_from,
        criteria=tuple(
            DraftCriterion(
                key=criterion.key,
                name=criterion.name,
                intent=criterion.intent,
                rung=criterion.rung.value,
                weight=criterion.weight,
                scale_points=criterion.scale.points if criterion.scale else 5,
                descriptors=dict(criterion.scale.descriptors) if criterion.scale else {},
                rule=dict(criterion.rule) if criterion.rule else None,
            )
            for criterion in goal.pack.criteria
        ),
        tasks=tuple(
            DraftTask(
                key=task.key,
                name=task.name,
                prompt_text=task.prompt_text or "",
                is_starter=task.is_starter,
            )
            for task in goal.pack.tasks
        ),
        created_at=now,
        updated_at=now,
    )
    return save_draft(database, draft)


def starter_draft(database: Database, key: str) -> WizardDraft:
    """Begin a draft pre-filled from one shipped starter.

    The starter's criteria and tasks arrive as *drafts the user is expected to edit*, and each
    task keeps ``is_starter`` until they change it — which is what the ``unforked`` badge is
    computed from.

    Raises:
        StarterNotFound: No starter has that key.
    """
    from freeweight.goals.starters import starter_directory
    from freeweight.services.goals import load_goal

    goal = load_goal(starter_directory(key))
    draft = draft_from_goal(database, goal, forked_from=key)
    return save_draft(
        database,
        replace(draft, tasks=tuple(replace(task, is_starter=True) for task in draft.tasks)),
    )


def grading_cost_sentence(draft: WizardDraft) -> str:
    """What step 5 will cost, said **before** the user starts it.

    Subjective Goals §5.2 targets twelve samples; §7 step 5 is explicit that grading twelve
    samples across five criteria is a real sitting. Stating the cost first is what makes a user's
    decision to spend it a decision.
    """
    judged = len(draft.judged)
    if judged == 0:
        return (
            "Every criterion here is scored by a rule, so there is nothing to grade and nothing "
            "to calibrate. This goal will run with judge_validity_factor = 1.0."
        )
    samples = 12
    minutes = samples * GRADING_COST_MINUTES_PER_SAMPLE
    return (
        f"Next you will grade {samples} samples on {judged} judged "
        f"{'criterion' if judged == 1 else 'criteria'}. Budget about {minutes} minutes, in one "
        "sitting if you can — your grades are the ground truth for everything that follows, and "
        "the wizard saves them as you go, so you can stop and come back."
    )


def sequence() -> Sequence[WizardStep]:
    """The steps, so a template can render the progress indicator without knowing the enum."""
    return list(WizardStep)
