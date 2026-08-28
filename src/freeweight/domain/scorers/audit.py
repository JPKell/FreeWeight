"""freeweight.domain.scorers.audit — rung-2 scoring for ``native.audit``.

Benchmark catalog §3.9. A mutation corpus over known-correct, unit-tested code gives every case
exact ground truth: which line was changed, which defect class it belongs to, and which function
holds it. Scoring is therefore a *matching* problem, not a judgement — a finding is compared with
an injected defect by line number, and nothing here reads what the model wrote about it.

**Clean samples are half the measurement.** The catalog's rule is blunt: "a model that reports many
possible problems must not score well". A corpus of nothing but mutated code cannot express that
rule, because on such a corpus flagging every line is perfect recall. So clean samples are scored
by the opposite standard — the correct answer is *silence* — and the
``clean_code_false_positive_rate`` they produce is reported next to recall everywhere.

**A finding is matched to a defect by line, with a tolerance, and localization is scored
separately.** The two questions "did it find the bug" and "did it point at the right line" have
different answers and deserve different numbers: a model that names the right defect one line above
it has found the bug, and a suite that scored that as a miss would be measuring line arithmetic.
:data:`MATCH_TOLERANCE_LINES` decides the first; ``line_localization_accuracy`` reports the second.

**An answer this module cannot read is not a failure to find bugs.** A response carrying no
findings block returns ``score=None`` with :data:`ERROR_UNPARSEABLE` rather than being scored as a
model that found nothing. Reading a defect report out of prose would need a judge, and rung 5 is
not available to a suite whose whole point is that the ground truth is exact
([ADR-0016](../../../../../docs/adr/0016-unavailable-is-not-zero.md)).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from freeweight.domain.scorers.schema import extract_json
from freeweight.domain.scoring import ScoreMethod, ScoreResult

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from freeweight.domain.benchmark import BenchmarkCase

__all__ = [
    "EXPECTATION_KEY",
    "MATCH_TOLERANCE_LINES",
    "AuditExpectation",
    "AuditScorer",
    "Defect",
    "Finding",
    "audit_metrics",
    "match_findings",
    "parse_findings",
]

EXPECTATION_KEY = "audit"
"""The key under which a case declares its injected defects."""

ERROR_UNPARSEABLE = "AUDIT_UNPARSEABLE"
"""The answer carried no findings block this module can read."""

ERROR_NO_EXPECTATION = "NO_EXPECTATION"
"""The case declared no audit expectation, so there is nothing to match findings against."""

MATCH_TOLERANCE_LINES = 2
"""How far from the injected line a finding may sit and still count as having found it.

Two lines, and the number is a judgement call this docstring owes a reason for: a mutated
comparison is frequently reported against the ``if`` above it or the ``return`` below it, and both
readings name the same defect. Wider than this and a finding starts matching a *different* defect
in a dense function, which would inflate recall by accident."""

_EVIDENCE_FINDINGS = 12
"""How many findings are kept in the sample's evidence.

Bounded, because ``result_json`` is scorer evidence and not a place to store the response
(spec §14). A model that reported four hundred problems is fully described by its counts plus the
first dozen."""


@dataclass(frozen=True, slots=True)
class Defect:
    """One defect the corpus injected, with its exact ground truth.

    Attributes:
        line: The 1-based line the mutation sits on.
        category: The mutation class, e.g. ``"off_by_one"``. Compared exactly, casefolded.
        severity: The corpus's severity label, or ``""`` when the case declares none.
        function: The enclosing function's name, or ``""``.
    """

    line: int
    category: str = ""
    severity: str = ""
    function: str = ""

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> Defect:
        """Build one defect from a case declaration.

        Args:
            body: The declaration, with at least ``line``.

        Returns:
            The defect.

        Raises:
            ValueError: ``line`` is missing or is not a positive whole number. Raised where the
                case is read, not during scoring: a corpus entry with no line has no ground truth
                and must fail the build rather than produce a case nobody can score.
        """
        line = body.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise ValueError(f"An audit defect needs a positive integer 'line'; got {line!r}.")
        return cls(
            line=line,
            category=str(body.get("category", "")),
            severity=str(body.get("severity", "")),
            function=str(body.get("function", "")),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    """One defect the model reported.

    Attributes:
        line: The line it named, or ``None`` when it named none. A finding with no line cannot be
            matched to a defect and is counted as a false positive — which is the honest reading:
            "something is wrong somewhere" is not a defect report.
        category: The class it named, casefolded and stripped.
        severity: The severity it named.
        function: The function it named.
    """

    line: int | None = None
    category: str = ""
    severity: str = ""
    function: str = ""


@dataclass(frozen=True, slots=True)
class AuditExpectation:
    """What one case declares about the code the model was shown.

    Attributes:
        defects: Every injected defect. Empty for a clean sample, which is not the same as a
            sample with no expectation at all — see :attr:`is_clean`.
        clean: Whether the corpus declares this sample defect-free. Declared rather than derived
            from an empty ``defects`` list, so a corpus entry that forgot its ground truth is
            distinguishable from one that deliberately has none.
    """

    defects: tuple[Defect, ...] = ()
    clean: bool = False

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> AuditExpectation:
        """Build one expectation from a case declaration.

        Args:
            body: The ``expectation["audit"]`` object.

        Returns:
            The expectation.

        Raises:
            ValueError: A declared defect is malformed, or a sample declares both ``clean`` and
                a defect. The second is a contradiction the corpus must not be able to express:
                a clean sample with an injected defect would count its own ground truth as a
                false positive.
        """
        defects = tuple(Defect.from_json(dict(item)) for item in body.get("defects", ()))
        clean = bool(body.get("clean", False))
        if clean and defects:
            raise ValueError(
                f"An audit case declares clean=true and {len(defects)} injected defect(s). A "
                "clean sample's correct answer is silence, so a defect in one would be scored as "
                "a false positive against the corpus's own ground truth."
            )
        return cls(defects=defects, clean=clean)

    @property
    def is_clean(self) -> bool:
        """Whether the correct answer to this case is to report nothing."""
        return self.clean and not self.defects


def parse_findings(text: str) -> tuple[Finding, ...] | None:
    """Read the model's findings block, or refuse to.

    The accepted shape is a JSON document that is either a list of findings or an object with a
    ``findings`` list. Anything else — prose, an empty answer, JSON of another shape — yields
    ``None``, which the scorer turns into ``score=None`` rather than into "found nothing".

    Args:
        text: Exactly what the model returned.

    Returns:
        The findings, or ``None`` when no findings block could be read. An *empty* tuple is a
        real answer: a model that returned ``{"findings": []}`` has said the code is clean, and
        that is a measurement.
    """
    document, error = extract_json(text)
    if error is not None:
        return None
    if isinstance(document, dict):
        document = document.get("findings")
    if not isinstance(document, list):
        return None
    findings: list[Finding] = []
    for item in document:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        findings.append(
            Finding(
                line=int(line) if isinstance(line, int) and not isinstance(line, bool) else None,
                category=str(item.get("category", "")).strip().casefold(),
                severity=str(item.get("severity", "")).strip().casefold(),
                function=str(item.get("function", "")).strip(),
            )
        )
    return tuple(findings)


def match_findings(
    defects: Sequence[Defect], findings: Sequence[Finding]
) -> tuple[tuple[tuple[Defect, Finding], ...], tuple[Finding, ...], tuple[Defect, ...]]:
    """Pair findings with the defects they found, greedily and by line distance.

    Greedy nearest-first, one finding per defect and one defect per finding. A model that reported
    the same line five times gets one true positive and four false positives, which is the
    behaviour the "must not score well for reporting many possible problems" rule needs: repeated
    guesses are guesses.

    Args:
        defects: The injected defects.
        findings: What the model reported.

    Returns:
        ``(matched, false_positives, missed)``. ``matched`` is in defect declaration order.
    """
    candidates: list[tuple[int, int, int]] = []
    for defect_index, defect in enumerate(defects):
        for finding_index, finding in enumerate(findings):
            if finding.line is None:
                continue
            distance = abs(finding.line - defect.line)
            if distance <= MATCH_TOLERANCE_LINES:
                candidates.append((distance, defect_index, finding_index))
    candidates.sort()
    taken_defects: dict[int, int] = {}
    taken_findings: set[int] = set()
    for _distance, defect_index, finding_index in candidates:
        if defect_index in taken_defects or finding_index in taken_findings:
            continue
        taken_defects[defect_index] = finding_index
        taken_findings.add(finding_index)
    matched = tuple(
        (defects[defect_index], findings[taken_defects[defect_index]])
        for defect_index in range(len(defects))
        if defect_index in taken_defects
    )
    false_positives = tuple(
        finding for index, finding in enumerate(findings) if index not in taken_findings
    )
    missed = tuple(defect for index, defect in enumerate(defects) if index not in taken_defects)
    return matched, false_positives, missed


def audit_metrics(
    expectation: AuditExpectation, findings: Sequence[Finding]
) -> tuple[float, dict[str, float], dict[str, Any]]:
    """Compute one case's audit figures.

    Every rate whose denominator is empty for this case is **omitted** from the returned metrics
    rather than reported as zero, so the run-level rate excludes the case and counts the exclusion
    (benchmark catalog §5.1). Recall has no meaning on a clean sample; the clean-code
    false-positive rate has none on a mutated one; localization has none where nothing matched.

    Args:
        expectation: The case's ground truth.
        findings: What the model reported.

    Returns:
        ``(score, metrics, evidence)``. ``score`` is the case's headline: per-case F1 on a mutated
        sample, and ``1.0`` for silence / ``0.0`` for any finding on a clean one.
    """
    matched, false_positives, missed = match_findings(expectation.defects, findings)
    true_positives = len(matched)
    metrics: dict[str, float] = {}

    reported = true_positives + len(false_positives)
    if reported:
        metrics["precision"] = true_positives / reported
    injected = true_positives + len(missed)
    if injected:
        metrics["recall"] = true_positives / injected
    if reported and injected:
        precision, recall = metrics["precision"], metrics["recall"]
        total = precision + recall
        metrics["f1"] = 0.0 if total == 0 else 2 * precision * recall / total
    if expectation.is_clean:
        metrics["clean_code_false_positive_rate"] = 1.0 if findings else 0.0
    if matched:
        metrics["line_localization_accuracy"] = sum(
            1.0 for defect, finding in matched if finding.line == defect.line
        ) / len(matched)
        with_function = [(defect, finding) for defect, finding in matched if defect.function]
        if with_function:
            metrics["function_localization_accuracy"] = sum(
                1.0
                for defect, finding in with_function
                if finding.function.casefold() == defect.function.casefold()
            ) / len(with_function)

    if expectation.is_clean:
        score = 0.0 if findings else 1.0
    else:
        score = metrics.get("f1", 0.0)

    evidence: dict[str, Any] = {
        "injected_defects": injected,
        "reported_findings": len(findings),
        "true_positives": true_positives,
        "false_positives": len(false_positives),
        "missed": [{"line": defect.line, "category": defect.category} for defect in missed][
            :_EVIDENCE_FINDINGS
        ],
        "spurious": [
            {"line": finding.line, "category": finding.category} for finding in false_positives
        ][:_EVIDENCE_FINDINGS],
        "is_clean_sample": expectation.is_clean,
    }
    return score, metrics, evidence


@dataclass(frozen=True, slots=True)
class AuditScorer:
    """Scores one audit response against the corpus's injected defects.

    The headline ``score`` is per-case F1 on a mutated sample and "did it stay silent" on a clean
    one. Precision, recall, the clean-code false-positive rate and the two localization figures
    travel in ``detail``, where the suite's metric definitions pick them up as their own metrics —
    never folded into the headline, because precision and recall trade against each other and one
    number that hid the trade is the number this suite exists to refuse.
    """

    key: str = "audit"
    method: ScoreMethod = ScoreMethod.RULE

    def score(self, case: BenchmarkCase, response_text: str) -> ScoreResult:
        """Match the model's findings against the case's ground truth.

        Args:
            case: The case, carrying ``expectation["audit"]``.
            response_text: Exactly what the model returned.

        Returns:
            The verdict. ``score=None`` with :data:`ERROR_NO_EXPECTATION` when the case declares
            no ground truth, and with :data:`ERROR_UNPARSEABLE` when the answer carries no
            findings block — neither is a model failure to find defects, and scoring either as
            ``0.0`` would blame the model for a defect in the case or for answering in prose.
        """
        declared = case.expectation.get(EXPECTATION_KEY)
        if not isinstance(declared, dict):
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_NO_EXPECTATION,
                error_text=(
                    f"Case {case.case_id!r} declares no expectation[{EXPECTATION_KEY!r}], so "
                    "there is no ground truth to match findings against."
                ),
            )
        try:
            expectation = AuditExpectation.from_json(declared)
        except ValueError as exc:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id},
                error_code=ERROR_NO_EXPECTATION,
                error_text=(
                    f"Case {case.case_id!r} declares an audit expectation it cannot use: {exc}"
                ),
            )
        findings = parse_findings(response_text)
        if findings is None:
            return ScoreResult(
                score=None,
                method=self.method,
                detail={"case": case.case_id, "is_clean_sample": expectation.is_clean},
                error_code=ERROR_UNPARSEABLE,
                error_text=(
                    f"Case {case.case_id!r}: the answer carries no findings block. Reading a "
                    "defect report out of prose would need a judge, and this suite is scored "
                    "against exact ground truth."
                ),
            )
        score, metrics, evidence = audit_metrics(expectation, findings)
        return ScoreResult(
            score=score,
            method=self.method,
            detail={"case": case.case_id, **metrics, **evidence},
        )
