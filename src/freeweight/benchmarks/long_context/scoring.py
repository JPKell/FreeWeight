"""freeweight.benchmarks.long_context.scoring — exact match, plus the facts a sweep is read by.

A long-context answer is scored exactly as any other short answer is: the buried fact either came
back or it did not, decided by :class:`~freeweight.domain.scorers.exact.ExactMatchScorer` with the
case's own declared normalization. Nothing about retrieval needs a different comparison.

What *is* different is that a long-context sample is meaningless without the shape of the document
that produced it. ``effective_context_tokens`` is computed across a whole sweep — the largest
tested context still clearing a share of the short-context baseline — so every sample has to carry
the context length it ran at and the threshold its suite was configured with. That is what this
wrapper adds, and it is why it lives beside the suite rather than in
:mod:`freeweight.domain.scorers`: it records *what the case was configured as*, which is case
metadata, not a scoring rule.

The threshold travels on the sample rather than being read from configuration when the number is
computed. A result taken last month was measured against the threshold in force last month, and
re-reading today's setting would silently redraw the line under it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from freeweight.domain.metrics import DEFAULT_EFFECTIVE_CONTEXT_THRESHOLD
from freeweight.domain.scorers.exact import ExactMatchScorer
from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from freeweight.domain.benchmark import BenchmarkCase

__all__ = ["CONTEXT_EXPECTATION_KEY", "LongContextScorer"]

CONTEXT_EXPECTATION_KEY = "long_context"
"""The key under which a case records the document shape its sample must carry."""


@dataclass(frozen=True, slots=True)
class LongContextScorer:
    """Exact-matches the buried fact and records the sweep coordinates alongside the verdict.

    Args:
        threshold_fraction: The share of the short-context baseline that counts as usable, written
            onto every sample so the figure computed from these samples can be read back with the
            line it was drawn at.
    """

    key: str = "long_context_retrieval"
    method: ScoreMethod = ScoreMethod.RULE
    threshold_fraction: float = DEFAULT_EFFECTIVE_CONTEXT_THRESHOLD
    _exact: ExactMatchScorer = field(default_factory=ExactMatchScorer)

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Score the answer and attach the case's context length, position and distractor volume.

        Args:
            case: The case, carrying ``expectation["exact"]`` and
                ``expectation["long_context"]``.
            response_text: Exactly what the model returned.

        Returns:
            The exact-match verdict, with ``context_tokens``, ``position_percent``,
            ``distractor_count`` and ``effective_context_threshold`` added to ``detail``. An
            unscoreable case stays unscoreable — the coordinates are added to the evidence either
            way, because "which depth failed to be measured" is part of the record.
        """
        verdict = self._exact.score(case, response_text)
        declared = case.expectation.get(CONTEXT_EXPECTATION_KEY)
        shape = declared if isinstance(declared, dict) else {}
        detail = {
            **dict(verdict.detail),
            "context_tokens": int(shape.get("context_tokens", 0)),
            "position_percent": int(shape.get("position_percent", 0)),
            "distractor_count": int(shape.get("distractor_count", 0)),
            "effective_context_threshold": self.threshold_fraction,
            # The same number under a second key, so the *ceiling of the sweep* is visible
            # beside the effective context it produced. A model that did not fail anywhere
            # the sweep looked has an effective context equal to the longest length tested,
            # and without this a reader would take that for observed degradation.
            "longest_tested_context_tokens": int(shape.get("context_tokens", 0)),
        }
        return ScoreResult(
            score=verdict.score,
            method=self.method,
            detail=detail,
            error_code=verdict.error_code,
            error_text=verdict.error_text,
        )
