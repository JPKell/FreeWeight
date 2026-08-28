"""freeweight.web.routes.compare — the comparison API and the comparison page.

Two routers, exactly as :mod:`freeweight.web.routes.runs` has: :data:`api_router` serves
``GET /api/v1/results/compare`` for clients, and :data:`router` serves the HTML page, which is an
ordinary server-rendered table with links and no JavaScript at all (ADR-0020, and UI standards
§13's "the page works with JavaScript disabled for all read-only content").

Both handlers are plain ``def``: every one of them touches the database, and ADR-0003 puts a
database-touching handler in Starlette's worker threadpool rather than on the event loop.

A route handler contains no business logic. Everything about what may be compared with what lives
in :mod:`freeweight.services.comparison` and :mod:`freeweight.domain.comparison`; these two
functions parse a query string, call one service function and render.

**The JSON document is the service's, not this module's.** ``--json`` on the CLI must print
the same field names the HTTP API returns (CLI standards §3), so
:func:`~freeweight.services.comparison.comparison_json` lives in the service both surfaces call
rather than here, where only one of them could reach it.

**A subject may be a run or a model.** api.md documents ``?subjects=a,b,c&suite=…`` without
saying which; Phase 9 shipped the run reading with ``suite`` as a guard, and Phase 10 adds the
other without removing it. A reference that resolves to a run is that run; one that does not, but
names a model, resolves to that model's *latest completed run of ``suite``* — and naming a model
with no suite is refused, because "compare these two models" has no answer until somebody says at
what. The resolver lives in :mod:`freeweight.services.results`, beside the dashboard's, so the two
cannot come to disagree about what "latest" means (``PHASE9_ISSUES.md`` §9).

**The page's job is to make a separation impossible to miss.** Where two runs cannot be merged,
their columns carry different group numbers, the row says so, and the fingerprint diff that
separates them is printed underneath in a ``<details>`` a reader can open — UI standards §5's
"comparisons across an incomparable boundary are visually separated and labelled, never silently
averaged", with the field-level diff Machine Identity §4 rule 3 requires beside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from baseaicore import ValidationError
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from freeweight.__about__ import __version__
from freeweight.infrastructure.db.errors import DatabaseError
from freeweight.services.comparison import (
    Comparison,
    ComparisonRefused,
    SubjectNotFound,
    compare_runs,
    comparison_json,
    enforce_suite,
)
from freeweight.services.results import resolve_subject_runs
from freeweight.web.rendering import render

if TYPE_CHECKING:
    from collections.abc import Sequence

    from freeweight.services.database import Database

__all__ = ["api_router", "parse_subjects", "router"]

api_router = APIRouter(tags=["results"])
router = APIRouter(include_in_schema=False)

_SubjectsQuery = Annotated[
    str | None,
    Query(
        alias="subjects",
        description=(
            "Comma-separated run ULIDs or prefixes, or model references. A model reference "
            "resolves to that model's latest completed run of `suite`, so it needs one. At "
            "least two subjects, and no run twice."
        ),
    ),
]
_SuiteQuery = Annotated[
    str | None,
    Query(
        alias="suite",
        description=(
            "Both a selector and a guard. It selects which suite a model subject is compared "
            "at, and it refuses by name any run subject that belongs to a different suite — "
            "rather than comparing against a suite that measured something else."
        ),
    ),
]


def parse_subjects(raw: str | None) -> tuple[str, ...]:
    """Split the ``subjects`` query parameter into run references.

    Args:
        raw: The parameter as given, or ``None`` when it was omitted.

    Returns:
        The references, in order, with surrounding whitespace and empty entries removed. Order is
        preserved because it is the column order of the table the caller asked for.
    """
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@api_router.get("/results/compare")
def compare(
    request: Request, subjects: _SubjectsQuery = None, suite: _SuiteQuery = None
) -> dict[str, Any]:
    """Compare two or more runs, aligned by metric key, with comparability verdicts.

    Never averages across a boundary marked ``separate``: the response carries the groups and the
    field-level fingerprint diff that separates them, and the caller decides what to do with that
    (API §5).

    Args:
        request: The incoming request; the database handle lives on its application state.
        subjects: Comma-separated run or model references. At least two.
        suite: The suite a model subject is compared at, and the guard every run subject
            must satisfy.

    Returns:
        The comparison document.

    Raises:
        ComparisonRefused: Fewer than two subjects, a repeated run, or a subject outside ``suite``.
        SubjectNotFound: A reference matches no run.
        ValidationError: A reference is an ambiguous prefix.
    """
    database: Database = request.app.state.database
    comparison = compare_runs(
        database, resolve_subject_runs(database, parse_subjects(subjects), suite=suite)
    )
    enforce_suite(comparison, suite)
    return comparison_json(comparison)


def _page(
    *,
    comparison: Comparison | None,
    subjects: Sequence[str],
    suite: str | None,
    error: str | None,
    status_code: int,
) -> HTMLResponse:
    """Render the comparison page in whichever of its four states applies."""
    return HTMLResponse(
        render(
            "compare/index.html",
            app_version=__version__,
            page="compare",
            comparison=comparison,
            subjects=list(subjects),
            subjects_value=", ".join(subjects),
            suite=suite or "",
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/compare", response_class=HTMLResponse)
def compare_page(
    request: Request, subjects: _SubjectsQuery = None, suite: _SuiteQuery = None
) -> HTMLResponse:
    """Render the comparison table, or the state that explains why there is not one.

    Four states, as UI standards §6 requires of every view: **empty** when no subjects were named,
    with the form that names them; **error** when the request cannot be satisfied, showing the
    code and what to do; **populated** otherwise. There is no loading state because the page is
    rendered server-side in one pass — there is nothing to wait for.
    """
    database: Database = request.app.state.database
    references = parse_subjects(subjects)
    if not references:
        return _page(comparison=None, subjects=(), suite=suite, error=None, status_code=200)
    try:
        comparison = compare_runs(database, resolve_subject_runs(database, references, suite=suite))
        enforce_suite(comparison, suite)
    except SubjectNotFound as exc:
        return _page(
            comparison=None,
            subjects=references,
            suite=suite,
            error=f"{exc.message} ({exc.code})",
            status_code=404,
        )
    except (ComparisonRefused, ValidationError) as exc:
        return _page(
            comparison=None,
            subjects=references,
            suite=suite,
            error=f"{exc.message} ({exc.code})",
            status_code=400,
        )
    except DatabaseError as exc:
        return _page(
            comparison=None,
            subjects=references,
            suite=suite,
            error=f"{exc.message} ({exc.code})",
            status_code=503,
        )
    return _page(
        comparison=comparison, subjects=references, suite=suite, error=None, status_code=200
    )
