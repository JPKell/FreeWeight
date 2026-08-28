"""freeweight.domain.agreement — how closely two graders agree, and how closely a jury agrees.

The intellectual core of the calibrated-judge instrument, and the module whose failure mode is the
most dangerous in this application: a subtly wrong ``kappa_w`` would be invisible for months,
because it would go on producing plausible numbers. So every function here is a published formula,
written out in full, with hand-computable behaviour asserted from both ends — a perfect grader must
give 1.0 and a random one must give ≈ 0, and *both* have to hold or neither number means anything.

**Four statistics, because one cannot answer the question.**

| | |
|---|---|
| ``kappa_w`` | Quadratic-weighted Cohen's kappa. Ordinal-aware and chance-corrected: a 4-vs-5
  disagreement counts far less than a 1-vs-5, and agreement chance would have produced is
  subtracted out. |
| ``rho`` | Spearman rank correlation. Does the jury *rank* as the author ranks? |
| ``mae`` | Mean absolute error, in scale points — the units the author thinks in. |
| ``bias`` | Mean signed error. Negative: the jury is harsher than the author. Positive: more
  generous. |

They must be able to disagree with each other. A jury that is consistently one point generous has a
high ``rho``, a high ``kappa_w`` and a non-zero ``bias``; if all four move together, only one of
them is real.

**``n_holdout`` is inseparable from every coefficient.** Not enforced here — this module returns
numbers — but every caller carries the count beside the value, and
:class:`AgreementResult` makes that structural by holding both. ``kappa_w`` without its ``n`` is a
number pretending to be a fact.

**A set with no variance has no agreement to measure.** Chance-corrected statistics divide by the
disagreement chance would produce; where every grade is identical, that is zero, and the honest
answer is ``None`` rather than a coefficient produced by a division nobody should have done.

Pure domain: stdlib only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "AgreementResult",
    "DEFAULT_CONCENTRATION_THRESHOLD",
    "AgreementBand",
    "band_for",
    "concentrated_grades",
    "agreement",
    "cohens_kappa_weighted",
    "has_variance",
    "krippendorff_alpha",
    "mean_absolute_error",
    "signed_bias",
    "spearman_rho",
    "weighted_mean",
]

DEFAULT_CONCENTRATION_THRESHOLD = 0.8
"""Share of grades in the top or bottom two scale points that makes a set unreliable.

Subjective Goals §5.1's warning, as a number: *"You graded eleven of twelve samples 4 or 5.
Agreement measured on this set will be unreliable. Add some weaker examples."* Eleven of twelve is
0.92; the threshold is set below it so the case the specification describes actually fires."""

_MINIMUM_PAIRS = 2


@dataclass(frozen=True, slots=True)
class AgreementResult:
    """One criterion's measured agreement, with the count that makes it interpretable.

    Attributes:
        kappa_w: Quadratic-weighted Cohen's kappa, or ``None`` when the set has no variance to
            correct against.
        rho: Spearman rank correlation, or ``None`` when either series is constant.
        mae: Mean absolute error in scale points.
        bias: Mean signed error: jury minus author.
        n: How many held-out samples all four were computed over. Never separable from them — a
            coefficient without its sample count is a number pretending to be a fact.
        scale_points: The ordinal scale's size, recorded because ``kappa_w``'s weights depend on
            it and two coefficients computed on different scales are not comparable.
    """

    kappa_w: float | None
    rho: float | None
    mae: float
    bias: float
    n: int
    scale_points: int

    def as_json(self) -> dict[str, Any]:
        """Return the figures as the API and the report render them, ``n`` included."""
        return {
            "kappa_w": self.kappa_w,
            "rho": self.rho,
            "mae": self.mae,
            "bias": self.bias,
            "n_holdout": self.n,
            "scale_points": self.scale_points,
        }


class AgreementBand:
    """Subjective Goals §5.5's interpretation bands, and what each one means for the user.

    A band rather than a bare coefficient, because "0.62" tells a person nothing and "Good.
    Usable; expect the occasional sample you would score differently" tells them what to do.
    """

    STRONG = "strong"
    GOOD = "good"
    FAIR = "fair"
    NOT_MEASURABLE = "not_measurable"

    DESCRIPTIONS: Mapping[str, str] = {
        STRONG: "Strong. The judge tracks your grading closely.",
        GOOD: "Good. Usable; expect the occasional sample you would score differently.",
        FAIR: "Fair. Evidence is emitted, but confidence is reduced substantially.",
        NOT_MEASURABLE: (
            "Not measurable yet. Results run and are inspectable; no evidence is emitted."
        ),
    }


def band_for(kappa_w: float | None) -> str:
    """Return the interpretation band one coefficient falls in (Subjective Goals §5.5).

    Args:
        kappa_w: The coefficient, or ``None``.

    Returns:
        One of :class:`AgreementBand`'s members. ``None`` maps to
        :attr:`AgreementBand.NOT_MEASURABLE`, which is what an unmeasurable set *is*.
    """
    if kappa_w is None:
        return AgreementBand.NOT_MEASURABLE
    if kappa_w >= 0.75:  # noqa: PLR2004 — §5.5's own band boundaries
        return AgreementBand.STRONG
    if kappa_w >= 0.60:  # noqa: PLR2004 — §5.5's own band boundaries
        return AgreementBand.GOOD
    if kappa_w >= 0.40:  # noqa: PLR2004 — §5.5's own band boundaries
        return AgreementBand.FAIR
    return AgreementBand.NOT_MEASURABLE


def has_variance(values: Sequence[float]) -> bool:
    """Whether a set of grades varies at all.

    Args:
        values: The grades.

    Returns:
        ``False`` when every value is identical, or when there are fewer than two. A
        chance-corrected statistic divides by the disagreement chance would produce, and on a set
        with no variance that is zero: there is nothing to agree *about*.
    """
    return len(values) >= _MINIMUM_PAIRS and len(set(values)) > 1


def concentrated_grades(
    grades: Sequence[int],
    *,
    scale_points: int,
    threshold: float = DEFAULT_CONCENTRATION_THRESHOLD,
) -> bool:
    """Whether a grade set is too bunched at one end to measure agreement on.

    Subjective Goals §5.1: a set that is all excellent or all terrible cannot produce a meaningful
    agreement figure, and the wizard says so *before* computing anything. This is that check.

    Args:
        grades: The author's grades.
        scale_points: The ordinal scale's size.
        threshold: The share at one end that counts as concentrated.

    Returns:
        ``True`` when at least ``threshold`` of the grades sit in the top two scale points, or at
        least ``threshold`` sit in the bottom two. ``False`` for an empty set, which is a different
        problem and gets a different message.

    Raises:
        ValueError: ``scale_points`` is below 3. There is no "top two and bottom two" on a
            two-point scale, and this build refuses even-numbered scales anyway.
    """
    if scale_points < 3:  # noqa: PLR2004 — the smallest ordinal scale this build accepts
        raise ValueError(f"An ordinal scale here has at least 3 points; got {scale_points}.")
    if not grades:
        return False
    top = sum(1 for grade in grades if grade >= scale_points - 1)
    bottom = sum(1 for grade in grades if grade <= 2)  # noqa: PLR2004 — the bottom two points
    return max(top, bottom) / len(grades) >= threshold


def cohens_kappa_weighted(
    author: Sequence[int], jury: Sequence[int], *, scale_points: int
) -> float | None:
    """Quadratic-weighted Cohen's kappa between two graders on one ordinal scale.

    ``kappa_w = 1 − Σ(w_ij × O_ij) / Σ(w_ij × E_ij)``, with ``w_ij = (i − j)² / (k − 1)²``
    (Subjective Goals §5.4). Written out rather than reduced, because the reduced forms are where
    the sign errors live.

    Args:
        author: The author's grades, ``1..scale_points``.
        jury: The jury's medians, in the same order and the same units.
        scale_points: ``k``.

    Returns:
        The coefficient, legitimately negative when the two disagree worse than chance would.
        ``None`` when there is nothing to correct against: fewer than two pairs, or *both*
        graders were constant — in which case expected disagreement is zero and the ratio is a
        division nobody should perform.

    Raises:
        ValueError: The two series are different lengths, ``scale_points`` is below 3, or a grade
            is outside ``1..scale_points``. Each would silently produce a plausible number.
    """
    _check_pair(author, jury, scale_points)
    if len(author) < _MINIMUM_PAIRS:
        return None
    size = scale_points
    weights = [
        [((row - column) ** 2) / ((size - 1) ** 2) for column in range(size)] for row in range(size)
    ]
    total = len(author)
    observed = [[0.0] * size for _ in range(size)]
    for left, right in zip(author, jury, strict=True):
        observed[left - 1][right - 1] += 1.0
    row_totals = [sum(row) for row in observed]
    column_totals = [sum(observed[row][column] for row in range(size)) for column in range(size)]

    numerator = sum(
        weights[row][column] * observed[row][column] / total
        for row in range(size)
        for column in range(size)
    )
    denominator = sum(
        weights[row][column] * (row_totals[row] / total) * (column_totals[column] / total)
        for row in range(size)
        for column in range(size)
    )
    if denominator == 0:
        return None
    return 1.0 - numerator / denominator


def spearman_rho(author: Sequence[float], jury: Sequence[float]) -> float | None:
    """Spearman rank correlation, with ties resolved by average ranks.

    Args:
        author: The author's grades.
        jury: The jury's medians, in the same order.

    Returns:
        The correlation in ``-1..1``, or ``None`` when either series is constant — a constant
        series has no ranks to correlate, and reporting ``0.0`` would say "no relationship" where
        the truth is "no measurement".

    Raises:
        ValueError: The two series are different lengths.
    """
    if len(author) != len(jury):
        raise ValueError(
            f"Spearman's rho needs paired series; got {len(author)} and {len(jury)} values."
        )
    if len(author) < _MINIMUM_PAIRS or not has_variance(author) or not has_variance(jury):
        return None
    left = _average_ranks(author)
    right = _average_ranks(jury)
    return _pearson(left, right)


def _average_ranks(values: Sequence[float]) -> list[float]:
    """Return each value's rank, tied values sharing the average of the ranks they span."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    """Pearson correlation between two equal-length series, or ``None`` when either is constant."""
    count = len(left)
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True))
    spread_left = math.sqrt(sum((a - mean_left) ** 2 for a in left))
    spread_right = math.sqrt(sum((b - mean_right) ** 2 for b in right))
    if spread_left == 0 or spread_right == 0:
        return None
    return covariance / (spread_left * spread_right)


def mean_absolute_error(author: Sequence[float], jury: Sequence[float]) -> float:
    """Mean absolute difference in scale points.

    Args:
        author: The author's grades.
        jury: The jury's medians, in the same order.

    Returns:
        The mean absolute error, ``0.0`` for an empty pair of series.

    Raises:
        ValueError: The two series are different lengths.
    """
    if len(author) != len(jury):
        raise ValueError(
            f"Mean absolute error needs paired series; got {len(author)} and {len(jury)} values."
        )
    if not author:
        return 0.0
    return sum(abs(a - b) for a, b in zip(author, jury, strict=True)) / len(author)


def signed_bias(author: Sequence[float], jury: Sequence[float]) -> float:
    """Mean signed error: the jury minus the author.

    Negative means the jury grades **harsher** than the author; positive, more generously. The
    direction is fixed here and stated everywhere it is rendered, because a bias whose sign the
    reader has to infer is a bias they will infer backwards.

    Args:
        author: The author's grades.
        jury: The jury's medians, in the same order.

    Returns:
        The mean signed error, ``0.0`` for an empty pair of series.

    Raises:
        ValueError: The two series are different lengths.
    """
    if len(author) != len(jury):
        raise ValueError(
            f"Signed bias needs paired series; got {len(author)} and {len(jury)} values."
        )
    if not author:
        return 0.0
    return sum(b - a for a, b in zip(author, jury, strict=True)) / len(author)


def agreement(author: Sequence[int], jury: Sequence[int], *, scale_points: int) -> AgreementResult:
    """Compute all four statistics at once, with the count that makes them interpretable.

    Args:
        author: The author's grades.
        jury: The jury's medians, in the same order.
        scale_points: The ordinal scale's size.

    Returns:
        The result, ``n`` included.

    Raises:
        ValueError: As :func:`cohens_kappa_weighted`.
    """
    return AgreementResult(
        kappa_w=cohens_kappa_weighted(author, jury, scale_points=scale_points),
        rho=spearman_rho(author, jury),
        mae=mean_absolute_error(author, jury),
        bias=signed_bias(author, jury),
        n=len(author),
        scale_points=scale_points,
    )


def krippendorff_alpha(
    ratings: Sequence[Sequence[float | None]], *, interval: bool = True
) -> float | None:
    """Krippendorff's alpha across any number of jurors, with missing ratings allowed.

    Computed from the coincidence matrix, which is what makes missing values natural rather than a
    special case: a unit rated by two of three jurors contributes its two ratings and no
    imputation. The difference function is the interval metric ``δ² = (c − k)²``, appropriate to an
    ordinal grading scale used as a numeric one — which is exactly how the jury median is used
    downstream.

    Args:
        ratings: One sequence per *unit* (per sample), holding one entry per juror; ``None`` where
            that juror did not rate that unit.
        interval: Kept as an explicit argument so the metric is visible at every call site. Only
            the interval metric is implemented; ``False`` raises.

    Returns:
        ``1.0`` when every juror agreed everywhere, falling towards ``0.0`` at chance and below it
        when jurors disagree worse than chance. ``None`` when fewer than two ratings pair up at
        all, or when expected disagreement is zero — every usable rating identical, which is
        perfect agreement about nothing and is reported as such by the caller.

    Raises:
        ValueError: ``interval`` is ``False``. A nominal or ordinal metric would give a different
            number, and silently substituting one for the other is the failure this argument
            exists to prevent.
    """
    if not interval:
        raise ValueError(
            "Only the interval difference metric is implemented. A nominal or ordinal metric "
            "gives a different number, and returning one under the other's name would be the "
            "quietest possible error."
        )
    # The coincidence matrix counts *ordered* pairs of ratings within a unit, each weighted by
    # 1/(m_u - 1). That weighting is what lets units rated by different numbers of jurors
    # contribute on equal terms, and it is why missing ratings need no imputation.
    coincidences: dict[tuple[float, float], float] = {}
    for unit in ratings:
        present = [float(value) for value in unit if value is not None]
        if len(present) < _MINIMUM_PAIRS:
            continue
        weight = len(present) - 1
        for index, left in enumerate(present):
            for other, right in enumerate(present):
                if index == other:
                    continue
                key = (left, right)
                coincidences[key] = coincidences.get(key, 0.0) + 1.0 / weight
    if not coincidences:
        return None
    totals: dict[float, float] = {}
    for (left, _right), count in coincidences.items():
        totals[left] = totals.get(left, 0.0) + count
    grand = sum(totals.values())
    if grand <= 1:
        return None
    observed = (
        sum(count * (left - right) ** 2 for (left, right), count in coincidences.items()) / grand
    )
    expected = sum(
        totals[left] * totals[right] * (left - right) ** 2 for left in totals for right in totals
    ) / (grand * (grand - 1))
    if expected == 0:
        return None
    return 1.0 - observed / expected


def weighted_mean(values: Mapping[str, float | None], weights: Mapping[str, float]) -> float | None:
    """Weight a set of per-criterion figures by their criteria's weights.

    Args:
        values: The figures, by criterion key. A ``None`` entry is *excluded*, not treated as
            zero — a criterion whose agreement could not be measured must not drag the weighted
            figure towards zero as though it had been measured badly.
        weights: The criteria's weights, by key.

    Returns:
        The weighted mean over the criteria that had a value, or ``None`` when none did.
    """
    usable = {
        key: value for key, value in values.items() if value is not None and weights.get(key, 0) > 0
    }
    if not usable:
        return None
    total = sum(weights[key] for key in usable)
    return sum(weights[key] * value for key, value in usable.items()) / total


def _check_pair(author: Sequence[int], jury: Sequence[int], scale_points: int) -> None:
    """Refuse a pair of grade series this module cannot honestly compare."""
    if len(author) != len(jury):
        raise ValueError(
            f"Agreement needs paired series; got {len(author)} and {len(jury)} grades."
        )
    if scale_points < 3:  # noqa: PLR2004 — the smallest ordinal scale this build accepts
        raise ValueError(f"An ordinal scale here has at least 3 points; got {scale_points}.")
    for label, series in (("author", author), ("jury", jury)):
        for grade in series:
            if not 1 <= grade <= scale_points:
                raise ValueError(
                    f"A {label} grade of {grade} is outside 1..{scale_points}. A grade off its "
                    "own scale would silently reweight every kappa cell."
                )
