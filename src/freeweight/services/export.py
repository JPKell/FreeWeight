"""freeweight.services.export — streaming exports of stored results.

``GET /api/v1/results/export`` and ``freeweight results export`` are the same function with two
front ends ([API §5](../../../docs/apps/freeweight/api.md), spec §7.3). This module is that
function.

Four decisions shape everything below.

**It streams, and it is not allowed to stop streaming.** Spec §15 budgets a 10 000-sample run, and
a 10 000-sample run serialized whole is tens of megabytes resident before the first byte reaches
the client. :func:`iter_export` therefore yields text chunks and never materializes the document;
the JSON writer emits the envelope's head, then one run at a time, then its tail. That the
assembled result is byte-identical to :func:`baseaicore.canonical_json` over the same structure is
not asserted by inspection — ``tests/e2e/test_export.py`` builds both and compares them, because a
hand-assembled canonical document is exactly the kind of thing that drifts.

**An unavailable measurement is the string ``"unsupported"``.** Spec §11 contract 6, ADR-0016 §4.
It is never ``null``, never ``0``, and never an omitted key: a consumer must be able to tell "this
machine could not measure it" from "this export did not include it", and only an explicit value
does that. The CSV writer follows the same rule, which is why its cells say ``unsupported`` rather
than being empty.

**The SetSpec contract is emitted through its own model, not by hand.** Every run carries a
``benchmark.run_summary`` payload built by :class:`~setspec.benchmark.v1.BenchmarkRunSummaryOut`,
so the runtime-profile hash check, the timing-order check and the measurement serializers all run
over what FreeWeight exports. A hand-built dictionary would pass today and diverge at the next
SetSpec minor.

**The document type is ``freeweight.export``, an application-owned schema**
([ADR-0035](../../../docs/adr/0035-application-owned-document-schemas.md)). No SetSpec schema can
carry what this endpoint documents: ``benchmark.run_summary`` has no slot at all for the raw
samples ``include_samples=true`` asks for, and the export's own shape follows this endpoint's query
model — ``scope``, the two include flags, the ``complete`` marker — which is FreeWeight's, not the
contract's. So the container is FreeWeight's, in FreeWeight's namespace, and it *embeds* the SetSpec
run summary verbatim under ``summary`` with the keyed metric rows and the optional samples beside
it where they are addressable.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from baseaicore import (
    UNSUPPORTED,
    ProviderKind,
    SuiteError,
    ValidationError,
    canonical_json,
    sha256_of,
    to_rfc3339,
    utc_now,
)
from setspec.envelope import GeneratorInfo, SchemaVersion
from weightsdb import DatabaseUnavailable

from freeweight.__about__ import __version__

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

    from freeweight.services.database import Database

__all__ = [
    "CALIBRATION_REPORT_SCHEMA_VERSION",
    "EMITTED_SCHEMAS",
    "EXPORT_SCHEMA",
    "GOAL_PACK_SCHEMA_VERSION",
    "EXPORT_SCHEMA_VERSION",
    "ExportFormat",
    "ExportRefused",
    "ExportScope",
    "ExportSelection",
    "ExportedMetric",
    "ExportedRun",
    "content_type_for",
    "goal_pack_payload",
    "iter_export",
    "iter_goal_export",
    "model_identity_payload",
    "read_export",
    "resolve_run_ids",
]

GOAL_PACK_SCHEMA_VERSION = SchemaVersion(1, 0)
"""The ``benchmark.goal_pack`` version this build writes (spec §7.3)."""

CALIBRATION_REPORT_SCHEMA_VERSION = SchemaVersion(1, 0)
"""The ``benchmark.calibration_report`` version this build writes (spec §7.3)."""

EMITTED_SCHEMAS: Mapping[str, str] = {
    "benchmark.run_summary": "1.0",
    "capability.evidence": "1.0",
    "benchmark.evidence_bundle": "1.0",
    "benchmark.goal_pack": "1.0",
    "benchmark.calibration_report": "1.0",
    "freeweight.export": "1.0",
}
"""Every document schema this build writes, with the version it writes it at.

What ``freeweight version`` and ``GET /api/v1/version`` report under ``schemas`` (CLI standards
§2), so a consumer can check the schema versions before it fetches a bundle (API §10 step 1).
Declared here, beside the constants that write them, rather than in the two front ends."""


@contextmanager
def _translated() -> Iterator[None]:
    """Translate raw driver failures into the suite's error hierarchy.

    The same guard :mod:`freeweight.services.runs` uses: an unmigrated database must reach a route
    as a ``DatabaseUnavailable`` it has an error state for, not as a driver exception that 500s.
    """
    try:
        yield
    except SuiteError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(f"Could not read the database: {exc}") from exc


EXPORT_SCHEMA = "freeweight.export"
"""The schema name of an exported results document.

In FreeWeight's **own** namespace, not SetSpec's: an application may mint a document schema only
under its own name, and ``benchmark.*`` belongs to the shared contract package (ADR-0035 §1). This
document was briefly ``benchmark.export`` and no compatibility path is kept for that name — the
suite is pre-1.0 and results produced during development are not being retained, so a reader that
accepted both would be carrying a branch for documents nobody has."""

EXPORT_SCHEMA_VERSION = SchemaVersion(1, 0)
"""The version this build writes. Additive changes bump the minor; a consumer rejects an
unsupported major and names both versions (API standards §7 rule 3)."""

_GENERATOR = GeneratorInfo(name="freeweight", version=__version__)

_UNSUPPORTED_JSON = "unsupported"

MAX_EXPORT_RUNS = 500
"""Ceiling on how many runs one export may span.

Not a pagination limit — an export is a document, not a page — but a bound on the work a single
unauthenticated loopback request can ask for (API standards §10). ``scope=all`` on a database with
more runs than this is refused by name rather than silently truncated, because a truncated export
that does not say so is a lie about what was measured."""

MAX_SAMPLES_PER_RUN = 50_000
"""Ceiling on raw samples included per run when ``include_samples`` is set."""


class ExportScope(StrEnum):
    """What an export covers.

    Attributes:
        RUN: One run, named by ULID or unambiguous prefix.
        MODEL: Every run of one model, named by canonical ID, ULID or prefix.
        SUITE: Every run of one benchmark suite, named by suite key.
        COMPARISON: An explicit list of runs, comma-separated — the same subject syntax
            ``GET /results/compare`` takes, so a comparison a user is looking at exports as itself.
        ALL: Every run in the database.
    """

    RUN = "run"
    MODEL = "model"
    SUITE = "suite"
    COMPARISON = "comparison"
    ALL = "all"


class ExportFormat(StrEnum):
    """How an export is serialized.

    Attributes:
        JSON: One SetSpec envelope holding every run. Streamed, canonical.
        JSONL: One SetSpec envelope per line, each holding exactly one run, so a consumer can
            process a large export without holding it — and so a truncated download is a prefix of
            valid documents rather than one broken one.
        CSV: Flattened metric rows for spreadsheet use (spec §7.3). Not SetSpec-wrapped: a CSV
            cannot carry an envelope, and pretending otherwise by adding comment lines would break
            every reader that matters.
    """

    JSON = "json"
    JSONL = "jsonl"
    CSV = "csv"


class ExportRefused(ValidationError):
    """The export cannot be produced as asked.

    A separate type from :class:`~freeweight.services.comparison.SubjectNotFound` because the
    causes are different in kind: a refusal here means the *request* is unanswerable — an unknown
    scope, a selector the scope needs and did not get, or a selection wider than
    :data:`MAX_EXPORT_RUNS` — and the caller fixes it by asking for something else.
    """


@dataclass(frozen=True, slots=True)
class ExportSelection:
    """A validated export request.

    Attributes:
        scope: What the export covers.
        selector: The scope's argument: a run reference, a model reference, a suite key, or a
            comma-separated run list. Ignored — and required to be absent — for ``ALL``.
        export_format: How to serialize it.
        include_samples: Whether to include the raw samples behind each metric. Off by default
            because a 10 000-sample run is two orders of magnitude larger with them, and most
            exports are read for their aggregates.
        include_prompts: Whether to include each sample's prompt identity and rendered-prompt hash.
            Separate from ``include_samples`` because prompt standards §4 makes the prompt part of
            the provenance rather than part of the response, and a consumer checking comparability
            wants it without wanting a megabyte of generated text.
        include_prompt_text: Whether to add a **prompt appendix** — each distinct rendered prompt
            once, keyed by its ``rendered_prompt_hash``. Off by default and deliberately separate
            from ``include_prompts``: identity is enough to re-render *on this machine*, and a
            measurement database should not become a second copy of the prompt pack. It is not
            enough anywhere else, because a reader elsewhere does not have the pack — which is the
            difference between an export that is auditable and one that is merely referential.
            Cheap when asked for: prompts repeat across thousands of samples, so the appendix holds
            one copy of each rather than one per sample.
        since: Include only runs created at or after this instant. ``None`` is unbounded.
        until: Include only runs created **strictly before** this instant. ``None`` is unbounded.
            Half-open so that two adjacent windows tile without overlapping or dropping a run
            between them, which is what makes a windowed export of a large history complete.
    """

    scope: ExportScope
    selector: str | None = None
    export_format: ExportFormat = ExportFormat.JSON
    include_samples: bool = False
    include_prompts: bool = False
    include_prompt_text: bool = False
    since: datetime | None = None
    until: datetime | None = None

    def __post_init__(self) -> None:
        """Refuse a scope/selector pairing that cannot mean anything.

        Raises:
            ExportRefused: A scope that needs a selector did not get one, ``ALL`` got one — an
                ignored argument is how a user comes to believe they exported a subset when they
                exported everything — or the ``[since, until)`` window is empty.
        """
        self._check_window()
        if self.scope is ExportScope.ALL:
            if self.selector:
                raise ExportRefused(
                    "scope=all takes no selector; drop it, or name the scope you meant "
                    "(run, model, suite or comparison).",
                    details={"scope": self.scope.value, "selector": self.selector},
                )
            return
        if not self.selector or not self.selector.strip():
            raise ExportRefused(
                f"scope={self.scope.value} needs a selector: {_SELECTOR_HELP[self.scope]}.",
                details={"scope": self.scope.value, "field": "selector"},
            )

    def _check_window(self) -> None:
        """Refuse a window that can never match, and bounds that disagree about time zones.

        Raises:
            ExportRefused: ``until`` is at or before ``since``, or one bound is timezone-aware and
                the other is not — comparing the two raises at query time, and an export that
                fails halfway through streaming has already sent its headers.
        """
        if self.since is None or self.until is None:
            return
        if (self.since.tzinfo is None) != (self.until.tzinfo is None):
            raise ExportRefused(
                "Export window bounds must both be timezone-aware, or both naive.",
                details={"since": str(self.since), "until": str(self.until)},
            )
        if self.until <= self.since:
            raise ExportRefused(
                f"Export window is empty: until={self.until.isoformat()} is not after "
                f"since={self.since.isoformat()}. The window is half-open [since, until).",
                details={"since": self.since.isoformat(), "until": self.until.isoformat()},
            )


_SELECTOR_HELP: Mapping[ExportScope, str] = {
    ExportScope.RUN: "a run ULID or an unambiguous prefix",
    ExportScope.MODEL: "a model canonical ID, ULID or prefix",
    ExportScope.SUITE: "a benchmark suite key such as native.performance",
    ExportScope.COMPARISON: "two or more comma-separated run references",
    ExportScope.ALL: "nothing",
}


@dataclass(frozen=True, slots=True)
class ExportedMetric:
    """One metric row as a reader gets it back from an exported document.

    ``value`` is ``None`` only when the export said ``"unsupported"``; the reason travels with it
    so a viewer can render the em dash *and* say why, which is the whole point of ADR-0016.
    """

    metric_key: str
    value: float | None
    unavailable_reason: str | None
    unit: str
    aggregation: str
    higher_is_better: bool
    sample_count: int
    excluded_count: int
    run_test_id: str | None = None
    gpu_index: int | None = None
    stddev: float | None = None
    coefficient_of_variation: float | None = None


@dataclass(frozen=True, slots=True)
class ExportedRun:
    """One run as a reader gets it back: identity, status and its metrics by key."""

    run_id: str
    suite_key: str
    suite_version: str
    model_canonical_id: str
    machine_fingerprint: str
    runtime_profile_hash: str
    status: str
    reproducibility_fingerprint: str
    metrics: tuple[ExportedMetric, ...] = ()
    sample_count: int = 0

    def metric(self, key: str) -> ExportedMetric | None:
        """Return the metric with this key, or ``None`` if the run did not produce it."""
        return next((row for row in self.metrics if row.metric_key == key), None)


@dataclass(frozen=True, slots=True)
class _RunBundle:
    """Everything one run contributes to an export, read in a single pass."""

    run_id: str
    document: dict[str, Any]
    metrics: tuple[dict[str, Any], ...]
    csv_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def content_type_for(export_format: ExportFormat) -> str:
    """The media type an HTTP response should carry for this format."""
    if export_format is ExportFormat.CSV:
        return "text/csv; charset=utf-8"
    if export_format is ExportFormat.JSONL:
        return "application/x-ndjson; charset=utf-8"
    return "application/json; charset=utf-8"


def _measurement(value: float | None) -> Any:  # noqa: ANN401 — a float or the UNSUPPORTED sentinel
    """Wire form of a numeric measurement: the number, or the ``UNSUPPORTED`` sentinel."""
    return UNSUPPORTED if value is None else float(value)


def _json_measurement(value: float | None) -> Any:  # noqa: ANN401 — a float or the marker string
    """JSON form of a numeric measurement: the number, or ``"unsupported"``."""
    return _UNSUPPORTED_JSON if value is None else float(value)


def _from_json_measurement(value: Any) -> float | None:  # noqa: ANN401 — untrusted JSON
    """Inverse of :func:`_json_measurement`, for the reader."""
    if value is None or value == _UNSUPPORTED_JSON:
        return None
    return float(value)


def resolve_run_ids(database: Database, selection: ExportSelection) -> tuple[str, ...]:
    """Resolve a selection to the run IDs it covers, newest first.

    Args:
        database: The application's database handle.
        selection: The validated request.

    Returns:
        The run IDs, in the order the export will write them: newest first for the scopes that
        are a query, and **in the order given** for ``COMPARISON``, because that is the column
        order the user is looking at.

    Raises:
        ExportRefused: The selection resolves to nothing, or to more than
            :data:`MAX_EXPORT_RUNS` runs.
        ValidationError: A run reference is an ambiguous prefix.
    """
    from sqlalchemy import select

    from freeweight.infrastructure.db.models_runs import BenchmarkSuite, Run

    def windowed(statement: Any) -> Any:  # noqa: ANN401 — a SQLAlchemy Select
        """Apply the half-open ``[since, until)`` window to a run query."""
        if selection.since is not None:
            statement = statement.where(Run.created_at >= selection.since)
        if selection.until is not None:
            statement = statement.where(Run.created_at < selection.until)
        return statement

    with _translated(), database.read() as session:
        if selection.scope is ExportScope.ALL:
            ids = list(session.scalars(windowed(select(Run.id)).order_by(Run.created_at.desc())))
        elif selection.scope is ExportScope.RUN:
            ids = [_resolve_run_id(session, str(selection.selector))]
        elif selection.scope is ExportScope.COMPARISON:
            references = [
                part.strip() for part in str(selection.selector).split(",") if part.strip()
            ]
            if not references:
                raise ExportRefused(
                    "scope=comparison needs at least one run reference.",
                    details={"field": "selector"},
                )
            ids = [_resolve_run_id(session, reference) for reference in references]
        elif selection.scope is ExportScope.SUITE:
            ids = list(
                session.scalars(
                    windowed(
                        select(Run.id)
                        .join(BenchmarkSuite, BenchmarkSuite.id == Run.suite_id)
                        .where(BenchmarkSuite.key == str(selection.selector))
                    ).order_by(Run.created_at.desc())
                )
            )
        else:
            model = _resolve_model(session, str(selection.selector))
            ids = list(
                session.scalars(
                    windowed(select(Run.id).where(Run.model_id == model.id)).order_by(
                        Run.created_at.desc()
                    )
                )
            )

    if not ids:
        window = ""
        if selection.since is not None or selection.until is not None:
            window = (
                f" in [{selection.since.isoformat() if selection.since else '—'}, "
                f"{selection.until.isoformat() if selection.until else '—'})"
            )
        raise ExportRefused(
            f"Nothing to export: scope={selection.scope.value} "
            f"selector={selection.selector or '—'} matched no runs{window}.",
            details={
                "scope": selection.scope.value,
                "selector": selection.selector,
                "since": selection.since.isoformat() if selection.since else None,
                "until": selection.until.isoformat() if selection.until else None,
            },
        )
    if len(ids) > MAX_EXPORT_RUNS:
        raise ExportRefused(
            f"That selection covers {len(ids)} runs; the limit is {MAX_EXPORT_RUNS}. "
            "Narrow it with since/until, or by suite, model or run — a truncated export would "
            "not say it was truncated. The window is half-open, so consecutive windows tile "
            "without dropping a run between them.",
            details={"matched": len(ids), "limit": MAX_EXPORT_RUNS},
        )
    return tuple(ids)


def _resolve_run_id(session: Session, reference: str) -> str:
    """Resolve one run reference to a ULID, or refuse by name."""
    from freeweight.infrastructure.db.repositories.runs import RunRepository

    repository = RunRepository()
    exact = repository.get_by_id(session, reference)
    if exact is not None:
        return str(exact.id)
    matches = repository.get_by_id_prefix(session, reference)
    if not matches:
        raise ExportRefused(f"No run matches {reference!r}.", details={"run": reference})
    if len(matches) > 1:
        raise ValidationError(
            f"{reference!r} matches {len(matches)} runs; use more characters.",
            details={"run": reference, "candidates": [row.id for row in matches]},
        )
    return str(matches[0].id)


def _resolve_model(session: Session, reference: str) -> Any:  # noqa: ANN401 — a models row
    """Resolve one model reference to a row, or refuse by name.

    Accepts what every other surface accepts — canonical ID, ULID, unambiguous ULID prefix, or the
    provider's own model name — because a user who started a run with ``--model qwen3.5:9b`` will
    export it with the same string, and an export that refused the reference the run was created
    with would be a trap rather than a strictness.
    """
    from freeweight.infrastructure.db.repositories.models import ModelRepository

    repository = ModelRepository()
    matches = repository.get_by_id_prefix(session, reference)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValidationError(
            f"{reference!r} matches {len(matches)} models; use more characters.",
            details={"model": reference, "candidates": [row.id for row in matches]},
        )
    row = (
        repository.get_by_canonical_id(session, reference)
        or repository.get_by_id(session, reference)
        or repository.get_by_provider_model_name(session, reference)
    )
    if row is not None:
        return row
    raise ExportRefused(f"No model matches {reference!r}.", details={"model": reference})


def _suite_payload(suite: Any) -> dict[str, Any]:  # noqa: ANN401 — a benchmark_suites row
    """The ``BenchmarkSuiteProvenanceFields`` half of a run summary."""
    dataset_hashes = (
        {str(k): str(v) for k, v in dict(suite.dataset_hashes_json).items()}
        if isinstance(suite.dataset_hashes_json, dict)
        else {}
    )
    return {
        "suite_key": suite.key,
        "suite_version": suite.version,
        "category": suite.category,
        "runner": suite.runner,
        "manifest_hash": suite.manifest_hash,
        "dataset_hashes": dataset_hashes,
        "prompt_subset_hash": suite.prompt_subset_hash or "sha256:unrecorded",
    }


def _model_payload(model: Any, descriptor: Any) -> dict[str, Any]:  # noqa: ANN401 — ORM rows
    """The ``ModelIdentityFields`` half of a run summary.

    Every descriptor number goes through :func:`_measurement`: a model whose parameter count the
    provider never reported exports ``"unsupported"``, not ``0`` — the same rule the UI renders as
    an em dash (ADR-0016 §4).
    """
    payload: dict[str, Any] = {
        "provider_kind": ProviderKind(model.provider_kind),
        "provider_model_name": model.provider_model_name,
        "artifact_digest": model.artifact_digest,
        "identity_confidence": model.identity_confidence,
        "canonical_id": model.canonical_id,
        "observed_at": model.last_seen_at,
    }
    if descriptor is None:
        return payload
    payload["observed_at"] = descriptor.observed_at
    payload.update(
        {
            "family": descriptor.family,
            "architecture": descriptor.architecture,
            "parameter_count": _measurement(descriptor.parameter_count),
            "active_parameter_count": _measurement(descriptor.active_parameter_count),
            "expert_count": _measurement(descriptor.expert_count),
            "quantization": descriptor.quantization,
            "weight_format": descriptor.weight_format,
            "size_bytes": _measurement(descriptor.size_bytes),
            "max_context": _measurement(descriptor.max_context),
            "embedding_dim": _measurement(descriptor.embedding_dim),
            "layers": _measurement(descriptor.layers),
            "attention_heads": _measurement(descriptor.attention_heads),
            "kv_heads": _measurement(descriptor.kv_heads),
            "head_dim": _measurement(descriptor.head_dim),
            "vocab_size": _measurement(descriptor.vocab_size),
            "sliding_window": _measurement(descriptor.sliding_window),
        }
    )
    return payload


def model_identity_payload(model: Any, descriptor: Any) -> dict[str, Any]:  # noqa: ANN401 — ORM rows
    """Build the ``ModelIdentityFields`` payload for one model row and its descriptor snapshot.

    The public name of :func:`_model_payload`, for the evidence service: an evidence record
    carries the same ``model`` object a run summary does (ADR-0022 §1), and two builders of it
    would be two places for the descriptor's measurement rule to drift.

    Args:
        model: A ``models`` row.
        descriptor: The ``model_descriptors`` row the measurement was taken against, or ``None``.

    Returns:
        The payload, ready for ``ModelIdentityFields`` validation.
    """
    return _model_payload(model, descriptor)


def _profile_payload(profile: Any) -> dict[str, Any]:  # noqa: ANN401 — a runtime_profiles row
    """The ``RuntimeProfileFields`` half of a run summary."""
    options = (
        dict(profile.provider_options_json)
        if isinstance(profile.provider_options_json, dict)
        else {}
    )
    return {
        "context_size": profile.context_size,
        "kv_cache_precision": profile.kv_cache_precision,
        "gpu_layers": profile.gpu_layers,
        "flash_attention": profile.flash_attention,
        "threads": profile.threads,
        "batch_size": profile.batch_size,
        "keep_alive": profile.keep_alive,
        "provider_options": options,
    }


def _environment_payload(run: Any, model: Any) -> dict[str, Any]:  # noqa: ANN401 — ORM rows
    """The ``EnvironmentFields`` half of a run summary, taken from the stored fingerprint.

    Read back out of ``fingerprint_document`` rather than re-derived from the current machine:
    an export describes the environment the run *happened in*, and re-reading today's driver
    version would quietly relabel a six-month-old measurement.
    """
    document = (
        dict(run.fingerprint_document_json)
        if isinstance(run.fingerprint_document_json, dict)
        else {}
    )
    environment = document.get("environment")
    environment = environment if isinstance(environment, dict) else {}
    return {
        "provider_kind": ProviderKind(run.provider_kind or model.provider_kind),
        "provider_version": run.provider_version or "unrecorded",
        "gpu_driver_version": environment.get("gpu_driver_version"),
        "cuda_version": environment.get("cuda_version"),
        "os_version": environment.get("os_version"),
    }


def _run_summary_payload(  # noqa: PLR0913 — a run summary is exactly these rows
    *,
    run: Any,  # noqa: ANN401 — a runs row
    suite: Any,  # noqa: ANN401 — a benchmark_suites row
    model: Any,  # noqa: ANN401 — a models row
    descriptor: Any,  # noqa: ANN401 — a model_descriptors row
    profile: Any,  # noqa: ANN401 — a runtime_profiles row
    machine: Any,  # noqa: ANN401 — a machines row
    metrics: Sequence[Any],
) -> dict[str, Any]:
    """Build and validate one run's ``benchmark.run_summary`` payload.

    Validated through the SetSpec *outbound* model, so the profile-hash agreement check and the
    started/completed ordering check run over every exported run rather than over the ones a test
    happened to cover.

    Raises:
        ValidationError: The stored rows do not satisfy the contract — a runtime-profile hash
            that no longer matches its fields, say. Raised rather than repaired: an export that
            silently corrected a stored inconsistency would hide a database problem behind a
            document that looks fine.
    """
    from pydantic import ValidationError as PydanticValidationError
    from setspec.benchmark.v1 import BenchmarkRunSummaryOut

    document = (
        dict(run.fingerprint_document_json)
        if isinstance(run.fingerprint_document_json, dict)
        else {}
    )
    payload = {
        "model": _model_payload(model, descriptor),
        "runtime_profile": _profile_payload(profile),
        "runtime_profile_hash": profile.profile_hash,
        "machine_fingerprint": machine.machine_fingerprint,
        "suite": _suite_payload(suite),
        "environment": _environment_payload(run, model),
        "application": {
            "name": "freeweight",
            "version": run.application_version or __version__,
            "git_commit": run.git_commit or "unrecorded",
        },
        "reproducibility": {
            "reproducibility_fingerprint": run.reproducibility_fingerprint,
            "fingerprint_document": document,
        },
        "status": run.status,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "aggregate_metrics": [
            {
                "metric_key": row.metric_key,
                "value": _measurement(row.numeric_value),
                "unit": row.unit,
                "aggregation": row.aggregation,
                "higher_is_better": bool(row.higher_is_better),
                # ADR-0016 §6: an unsupported metric has no supported samples, so its count is
                # zero regardless of how many attempts stood behind it. SetSpec enforces this,
                # and a stored row that disagrees would otherwise fail validation here.
                "sample_count": 0 if row.numeric_value is None else (row.sample_count or 1),
                "dispersion": (
                    UNSUPPORTED
                    if row.numeric_value is None
                    or row.stddev is None
                    or (row.sample_count or 0) < 2  # noqa: PLR2004 — SetSpec's own minimum
                    else float(row.stddev)
                ),
            }
            for row in metrics
        ],
        "error_code": run.error_code,
        "error_text": run.error_text,
    }
    try:
        return dict(BenchmarkRunSummaryOut.model_validate(payload).model_dump())
    except PydanticValidationError as exc:
        raise ValidationError(
            f"Run {run.id!r} cannot be exported as benchmark.run_summary: {exc.errors()[0]['msg']}",
            details={"run": run.id},
        ) from exc


def _metric_rows(metrics: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """The keyed metric rows FreeWeight's own schema carries beside the SetSpec summary."""
    return tuple(
        {
            "metric_key": row.metric_key,
            "run_test_id": row.run_test_id,
            "value": _json_measurement(row.numeric_value),
            "unavailable_reason": row.unavailable_reason,
            "unit": row.unit,
            "aggregation": row.aggregation,
            "higher_is_better": bool(row.higher_is_better),
            "sample_count": row.sample_count or 0,
            "excluded_count": row.excluded_count or 0,
            "gpu_index": row.gpu_index,
            "stddev": _json_measurement(row.stddev),
            "coefficient_of_variation": _json_measurement(row.coefficient_of_variation),
        }
        for row in metrics
    )


def _sample_rows(
    session: Session, run_id: str, *, include_prompts: bool
) -> tuple[dict[str, Any], ...]:
    """Every stored sample of one run, ordered so two exports of it are byte-identical."""
    from sqlalchemy import select

    from freeweight.infrastructure.db.models_runs import RunTest, Sample

    statement = (
        select(Sample, RunTest.id)
        .join(RunTest, RunTest.id == Sample.run_test_id)
        .where(RunTest.run_id == run_id)
        .order_by(Sample.run_test_id, Sample.ordinal, Sample.repetition)
        .limit(MAX_SAMPLES_PER_RUN)
    )
    rows: list[dict[str, Any]] = []
    for sample, run_test_id in session.execute(statement):
        row: dict[str, Any] = {
            "sample_id": sample.id,
            "run_test_id": run_test_id,
            "case_id": sample.case_id,
            "ordinal": sample.ordinal,
            "repetition": sample.repetition,
            "status": sample.status,
            "score": _json_measurement(sample.score),
            "score_method": sample.score_method,
            "input_tokens": _json_measurement(sample.input_tokens),
            "output_tokens": _json_measurement(sample.output_tokens),
            "client_wall_ms": _json_measurement(sample.client_wall_ms),
            "client_ttft_ms": _json_measurement(sample.client_ttft_ms),
            "finish_reason": sample.finish_reason,
            "response_hash": sample.response_hash,
            "error_code": sample.error_code,
            "error_text": sample.error_text,
        }
        if include_prompts:
            row["prompt"] = {
                "prompt_id": sample.prompt_id,
                "prompt_version": sample.prompt_version,
                "prompt_hash": sample.prompt_hash,
                "rendered_prompt_hash": sample.rendered_prompt_hash,
            }
        rows.append(row)
    return tuple(rows)


def _rendered_prompts(database: Database, run_ids: Sequence[str]) -> dict[str, str]:
    """Build the prompt appendix: each distinct rendered prompt once, keyed by its hash.

    Prompt text is not stored — spec §14 keeps prompts as hashes, and a measurement database that
    quietly accumulated a second copy of the prompt pack would be a different product. The text is
    therefore **re-rendered** here, from the installed suites' own cases, which is the claim prompt
    standards §4 makes ("the identity is sufficient to re-render") turned into something that
    actually runs.

    Because it re-renders, it also *verifies*: a case is only offered under the hash its current
    text produces. A prompt edited since the run simply does not match, so it is absent from the
    appendix rather than present and wrong — a reader gets no text instead of the wrong text, and
    the sample's own hash still says what was asked.

    Args:
        database: The application's database handle.
        run_ids: The runs whose samples the appendix must cover.

    Returns:
        ``{rendered_prompt_hash: prompt text}``, holding only hashes these runs actually used, and
        only those the installed suites can still produce.
    """
    from sqlalchemy import distinct, select

    from freeweight.infrastructure.db.models_runs import RunTest, Sample

    with _translated(), database.read() as session:
        used = {
            value
            for value in session.scalars(
                select(distinct(Sample.rendered_prompt_hash))
                .join(RunTest, RunTest.id == Sample.run_test_id)
                .where(RunTest.run_id.in_(run_ids))
            )
            if value
        }
    if not used:
        return {}

    from freeweight.services.runs import build_registry

    appendix: dict[str, str] = {}
    for benchmark in build_registry().all():
        for test in benchmark.tests:
            for case in test.cases():
                digest = f"sha256:{sha256_of(case.prompt)}"
                if digest in used and digest not in appendix:
                    appendix[digest] = case.prompt
    return appendix


def _bundle(session: Session, run_id: str, selection: ExportSelection) -> _RunBundle:
    """Read one run and build its export document."""
    from freeweight.infrastructure.db.models import Machine, ModelDescriptor, RuntimeProfile
    from freeweight.infrastructure.db.models_runs import BenchmarkSuite, Run
    from freeweight.infrastructure.db.repositories.models import ModelRepository
    from freeweight.infrastructure.db.repositories.runs import MetricValueRepository

    run = session.get(Run, run_id)
    if run is None:  # pragma: no cover — the IDs came from this database moments ago
        raise ExportRefused(f"No run matches {run_id!r}.", details={"run": run_id})
    suite = session.get(BenchmarkSuite, run.suite_id)
    model = ModelRepository().get_by_id(session, run.model_id)
    descriptor = session.get(ModelDescriptor, run.model_descriptor_id)
    profile = session.get(RuntimeProfile, run.runtime_profile_id)
    machine = session.get(Machine, run.machine_id)
    if suite is None or model is None or profile is None or machine is None:
        raise ExportRefused(
            f"Run {run.id!r} is missing the identity rows an export needs.",
            details={"run": run.id},
        )

    all_metrics = MetricValueRepository().list_for_run(session, run.id)
    run_level = [row for row in all_metrics if row.run_test_id is None and row.sample_id is None]
    keyed = [row for row in all_metrics if row.sample_id is None]

    document: dict[str, Any] = {
        "run_id": run.id,
        "label": run.label,
        "notes": run.notes,
        "summary": _run_summary_payload(
            run=run,
            suite=suite,
            model=model,
            descriptor=descriptor,
            profile=profile,
            machine=machine,
            metrics=run_level,
        ),
        "metrics": list(_metric_rows(keyed)),
        "degradations": (
            list(run.degradations_json) if isinstance(run.degradations_json, list) else []
        ),
    }
    if selection.include_samples:
        samples = _sample_rows(session, run.id, include_prompts=selection.include_prompts)
        document["samples"] = list(samples)
    csv_rows = tuple(
        {
            "run_id": run.id,
            "label": run.label or "",
            "model_canonical_id": model.canonical_id,
            "suite_key": suite.key,
            "suite_version": suite.version,
            "machine_fingerprint": machine.machine_fingerprint,
            "runtime_profile_hash": profile.profile_hash,
            "status": run.status,
            "started_at": to_rfc3339(run.started_at) if run.started_at else "",
            "completed_at": to_rfc3339(run.completed_at) if run.completed_at else "",
            **row,
        }
        for row in _metric_rows(keyed)
    )
    return _RunBundle(
        run_id=run.id,
        document=document,
        metrics=_metric_rows(keyed),
        csv_rows=csv_rows,
    )


CSV_COLUMNS: tuple[str, ...] = (
    "run_id",
    "label",
    "model_canonical_id",
    "suite_key",
    "suite_version",
    "machine_fingerprint",
    "runtime_profile_hash",
    "status",
    "started_at",
    "completed_at",
    "metric_key",
    "run_test_id",
    "value",
    "unavailable_reason",
    "unit",
    "aggregation",
    "higher_is_better",
    "sample_count",
    "excluded_count",
    "gpu_index",
    "stddev",
    "coefficient_of_variation",
)
"""The flattened CSV header, in a fixed order so a saved spreadsheet formula keeps working."""


def _csv_line(values: Mapping[str, Any]) -> str:
    """Render one CSV record with the module's fixed column order."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n"
    )
    writer.writerow({column: values.get(column, "") for column in CSV_COLUMNS})
    return buffer.getvalue()


def _envelope_head(payload_head: str) -> str:
    """The canonical envelope up to the point the payload's ``runs`` array opens.

    Canonical JSON sorts keys, so the envelope's own order is fixed: ``generated_at``,
    ``generator``, ``payload``, ``schema``, ``schema_version``. Inside the payload the same rule
    puts ``complete`` and ``runs`` before ``scope`` and ``selector``, which is what makes the
    streaming split possible at all — the array is not the last key, but everything before it is
    known up front.
    """
    return payload_head


def iter_export(
    database: Database, selection: ExportSelection, *, now: datetime | None = None
) -> Iterator[str]:
    """Stream an export as text chunks.

    One run is read, serialized and released before the next is read, so the resident cost is one
    run rather than the whole document. The caller is expected to iterate to exhaustion or close
    the generator; the database session is opened once and closed with it.

    Args:
        database: The application's database handle.
        selection: The validated request.
        now: The document's ``generated_at``. Injected so a test can produce a byte-identical
            document twice (coding standards §5).

    Yields:
        Text chunks. Concatenating them gives the whole document; for
        :attr:`ExportFormat.JSONL` each chunk is exactly one line.

    Raises:
        ExportRefused: The selection matched nothing, or matched more than
            :data:`MAX_EXPORT_RUNS` runs.
    """
    run_ids = resolve_run_ids(database, selection)
    generated_at = now if now is not None else utc_now()
    if selection.export_format is ExportFormat.CSV:
        yield from _iter_csv(database, selection, run_ids)
        return
    if selection.export_format is ExportFormat.JSONL:
        yield from _iter_jsonl(database, selection, run_ids, generated_at)
        return
    yield from _iter_json(database, selection, run_ids, generated_at)


def _envelope_fields(generated_at: datetime) -> dict[str, Any]:
    """The four envelope fields that sit beside ``payload``."""
    return {
        "generated_at": generated_at,
        "generator": {"name": _GENERATOR.name, "version": _GENERATOR.version},
        "schema": EXPORT_SCHEMA,
        "schema_version": str(EXPORT_SCHEMA_VERSION),
    }


def _iter_json(
    database: Database,
    selection: ExportSelection,
    run_ids: Sequence[str],
    generated_at: datetime,
) -> Iterator[str]:
    """Stream the single-envelope JSON form."""
    fields = _envelope_fields(generated_at)
    appendix = _rendered_prompts(database, run_ids) if selection.include_prompt_text else None
    # Canonical JSON sorts keys, so the payload's own order is fixed and the array is not last:
    # complete, prompt_appendix, runs, scope, selector, since, until. Everything before `runs` has
    # to be known up front, which is why the appendix is a pre-pass rather than accumulated while
    # streaming — it is one DISTINCT query plus a re-render, not a second walk of the samples.
    head = (
        '{"generated_at":' + canonical_json(fields["generated_at"]) + ","
        '"generator":' + canonical_json(fields["generator"]) + ","
        '"payload":{"complete":true,'
        + ('"prompt_appendix":' + canonical_json(appendix) + "," if appendix is not None else "")
        + '"runs":['
    )
    tail = (
        '],"scope":' + canonical_json(selection.scope.value) + ","
        '"selector":' + canonical_json(selection.selector) + ","
        '"since":' + canonical_json(selection.since) + ","
        '"until":' + canonical_json(selection.until) + "},"
        '"schema":' + canonical_json(EXPORT_SCHEMA) + ","
        '"schema_version":' + canonical_json(str(EXPORT_SCHEMA_VERSION)) + "}"
    )
    yield _envelope_head(head)
    with _translated(), database.read() as session:
        for index, run_id in enumerate(run_ids):
            prefix = "" if index == 0 else ","
            yield prefix + canonical_json(_bundle(session, run_id, selection).document)
    yield tail


def _iter_jsonl(
    database: Database,
    selection: ExportSelection,
    run_ids: Sequence[str],
    generated_at: datetime,
) -> Iterator[str]:
    """Stream the line-per-run form: one complete envelope per line.

    Each line is a whole document rather than a fragment, so a consumer that reads the first
    thousand lines of a large export has a thousand valid envelopes and not one truncated one.
    ``complete`` is ``false`` on every line for the same reason: no single line describes the
    whole selection.
    """
    with _translated(), database.read() as session:
        for run_id in run_ids:
            bundle = _bundle(session, run_id, selection)
            document = {
                **_envelope_fields(generated_at),
                "payload": {
                    "complete": False,
                    "runs": [bundle.document],
                    "scope": selection.scope.value,
                    "selector": selection.selector,
                },
            }
            yield canonical_json(document) + "\n"


def _iter_csv(
    database: Database, selection: ExportSelection, run_ids: Sequence[str]
) -> Iterator[str]:
    """Stream the flattened CSV form: one row per metric, one header."""
    yield _csv_line(dict.fromkeys(CSV_COLUMNS, "") | {column: column for column in CSV_COLUMNS})
    with _translated(), database.read() as session:
        for run_id in run_ids:
            for row in _bundle(session, run_id, selection).csv_rows:
                yield _csv_line(row)


def read_export(text: str) -> tuple[ExportedRun, ...]:
    """Read an exported JSON or JSONL document back into run records.

    This is the viewer half of the round trip: it is what proves an export carries what the
    dashboard showed, rather than merely being well-formed. It accepts either format because a
    consumer that saved whichever one it asked for should not have to remember which.

    Args:
        text: The exported document.

    Returns:
        One record per run, in document order.

    Raises:
        ValidationError: The text is not an export of a supported major, or is not JSON at all.
    """
    import json

    stripped = text.strip()
    if not stripped:
        raise ValidationError("Empty document: nothing to read.", details={"field": "body"})
    documents: list[Any] = []
    try:
        if stripped.startswith("{") and "\n" not in stripped.rstrip("\n"):
            documents = [json.loads(stripped)]
        else:
            first = json.loads(stripped) if stripped.startswith("{") else None
            documents = (
                [first]
                if isinstance(first, dict)
                else [json.loads(line) for line in stripped.splitlines() if line.strip()]
            )
    except json.JSONDecodeError:
        documents = [json.loads(line) for line in stripped.splitlines() if line.strip()]

    runs: list[ExportedRun] = []
    for document in documents:
        runs.extend(_read_document(document))
    return tuple(runs)


def _read_document(document: Any) -> list[ExportedRun]:  # noqa: ANN401 — untrusted JSON
    """Read one envelope into run records, rejecting an unsupported major by name."""
    if not isinstance(document, dict):
        raise ValidationError(
            "An export is a JSON object with a SetSpec envelope.", details={"field": "body"}
        )
    schema = document.get("schema")
    if schema != EXPORT_SCHEMA:
        raise ValidationError(
            f"Not a {EXPORT_SCHEMA} document: schema is {schema!r}.",
            details={"field": "schema", "value": schema},
        )
    version = str(document.get("schema_version", ""))
    major = version.split(".", 1)[0]
    if major != str(EXPORT_SCHEMA_VERSION.major):
        raise ValidationError(
            f"Unsupported {EXPORT_SCHEMA} major: document is {version}, this build reads "
            f"{EXPORT_SCHEMA_VERSION}.",
            details={"document_version": version, "supported": str(EXPORT_SCHEMA_VERSION)},
        )
    payload = document.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    entries = payload.get("runs")
    entries = entries if isinstance(entries, list) else []
    return [_read_run(entry) for entry in entries if isinstance(entry, dict)]


def _mapping(value: Any) -> dict[str, Any]:  # noqa: ANN401 — untrusted JSON
    """Narrow an untrusted JSON value to a mapping, or to an empty one."""
    return dict(value) if isinstance(value, dict) else {}


def _read_run(entry: Mapping[str, Any]) -> ExportedRun:
    """Read one run entry into an :class:`ExportedRun`."""
    summary = _mapping(entry.get("summary"))
    suite = _mapping(summary.get("suite"))
    model = _mapping(summary.get("model"))
    reproducibility = _mapping(summary.get("reproducibility"))
    metrics = entry.get("metrics")
    metrics = metrics if isinstance(metrics, list) else []
    samples = entry.get("samples")
    return ExportedRun(
        run_id=str(entry.get("run_id", "")),
        suite_key=str(suite.get("suite_key", "")),
        suite_version=str(suite.get("suite_version", "")),
        model_canonical_id=str(model.get("canonical_id", "")),
        machine_fingerprint=str(summary.get("machine_fingerprint", "")),
        runtime_profile_hash=str(summary.get("runtime_profile_hash", "")),
        status=str(summary.get("status", "")),
        reproducibility_fingerprint=str(reproducibility.get("reproducibility_fingerprint", "")),
        metrics=tuple(
            ExportedMetric(
                metric_key=str(row.get("metric_key", "")),
                value=_from_json_measurement(row.get("value")),
                unavailable_reason=row.get("unavailable_reason"),
                unit=str(row.get("unit", "")),
                aggregation=str(row.get("aggregation", "")),
                higher_is_better=bool(row.get("higher_is_better")),
                sample_count=int(row.get("sample_count") or 0),
                excluded_count=int(row.get("excluded_count") or 0),
                run_test_id=row.get("run_test_id"),
                gpu_index=row.get("gpu_index"),
                stddev=_from_json_measurement(row.get("stddev")),
                coefficient_of_variation=_from_json_measurement(
                    row.get("coefficient_of_variation")
                ),
            )
            for row in metrics
            if isinstance(row, dict)
        ),
        sample_count=len(samples) if isinstance(samples, list) else 0,
    )


# ---------------------------------------------------------------------------
# Goal exports (Phase 10A)
#
# Spec §7.3 lists `benchmark.goal_pack` and `benchmark.calibration_report` among FreeWeight's
# exports. Both are single SetSpec documents rather than a collection: a pack is one document and
# a calibration report is one measurement, so each is returned as the envelope alone with no
# wrapper (API standards §3, ADR-0025 §2).
#
# They stream for the same reason everything else here does, even though neither is large today:
# one code path for "an export leaves this process as chunks" is one place to get the canonical
# framing right.
# ---------------------------------------------------------------------------


def iter_goal_export(
    database: Database,
    goal: Any,  # noqa: ANN401 — a LoadedGoal; importing it here would pull the goal service in
    *,
    document: str = "goal_pack",
    now: datetime | None = None,
) -> Iterator[str]:
    """Stream one goal's ``benchmark.goal_pack`` or ``benchmark.calibration_report``.

    Args:
        database: The application's database handle, for the calibration report.
        goal: The loaded goal.
        document: ``"goal_pack"`` or ``"calibration_report"``.
        now: The document's ``generated_at``, injected for byte-identical reproduction.

    Yields:
        One chunk holding the whole envelope. A goal pack is a few kilobytes and a calibration
        report is smaller; both are produced whole because neither has an unbounded dimension the
        way a run's samples do.

    Raises:
        ExportRefused: ``document`` names neither shape, or a calibration report was asked for on
            a goal that has never been calibrated — which is a different answer from an empty
            report, and saying so is the point.
    """
    from setspec.envelope import dump_envelope

    generated_at = now if now is not None else utc_now()
    if document == "goal_pack":
        from setspec.goal.v1 import GoalPackOut

        payload = GoalPackOut.model_validate(goal_pack_payload(goal))
        yield dump_envelope(
            payload,
            schema="benchmark.goal_pack",
            version=GOAL_PACK_SCHEMA_VERSION,
            generator=_GENERATOR,
            generated_at=generated_at,
        )
        return
    if document != "calibration_report":
        raise ExportRefused(
            f"A goal exports 'goal_pack' or 'calibration_report'; got {document!r}.",
            details={"field": "document", "value": document},
        )
    from freeweight.services.calibration import latest_outcome

    outcome = latest_outcome(database, goal)
    if outcome is None:
        raise ExportRefused(
            f"Goal {goal.slug!r} has no calibration report to export. Grade its samples and run "
            "the calibration first — an empty report and an uncalibrated goal are different "
            "things, and this refuses rather than conflating them.",
            details={"slug": goal.slug},
        )
    yield dump_envelope(
        _calibration_report_payload(goal, outcome),
        schema="benchmark.calibration_report",
        version=CALIBRATION_REPORT_SCHEMA_VERSION,
        generator=_GENERATOR,
        generated_at=generated_at,
    )


def _calibration_report_payload(
    goal: Any,  # noqa: ANN401 — a LoadedGoal
    outcome: Any,  # noqa: ANN401 — a CalibrationOutcome
) -> Any:  # noqa: ANN401 — a CalibrationReportOut
    """Build and validate the ``benchmark.calibration_report`` payload.

    Every coefficient carries its ``n_holdout``, because that is what SetSpec's own model requires
    and because a ``kappa_w`` without its ``n`` is a number pretending to be a fact
    (Subjective Goals §5.4).
    """
    from setspec.goal.v1 import CalibrationReportOut

    return CalibrationReportOut.model_validate(
        {
            "goal_slug": goal.pack.slug,
            "goal_hash": goal.goal_hash,
            "judge_set": _judge_set_payload(outcome.judge_set),
            "criteria": [
                {
                    "criterion_key": agreement.criterion_key,
                    "weight": agreement.weight,
                    "agreement": {
                        # SetSpec requires a number for each of the four; a criterion with no
                        # variance to measure produces ``None`` here, and 0.0 would be a claim
                        # of perfect disagreement rather than of an absent measurement. Such a
                        # criterion is dropped below rather than exported as a fabricated zero.
                        "kappa_w": agreement.result.kappa_w,
                        "rho": agreement.result.rho,
                        "mae": agreement.result.mae,
                        "bias": agreement.result.bias,
                        "n_anchor": outcome.verdict.n_anchor,
                        "n_holdout": agreement.result.n,
                        "graded_by": outcome.graded_by,
                        "measured_at": outcome.measured_at or utc_now(),
                    },
                    "inter_juror_alpha": agreement.inter_juror_alpha,
                    "judge_validity_factor": agreement.validity,
                }
                for agreement in outcome.criteria
                if agreement.result.kappa_w is not None and agreement.result.rho is not None
            ],
            "weighted_kappa_w": outcome.verdict.weighted_kappa_w,
            "min_agreement": outcome.verdict.min_agreement,
            "passed_gate": outcome.verdict.state.value == "calibrated",
            "judge_validity_factor": outcome.verdict.judge_validity_factor,
            "n_anchor": outcome.verdict.n_anchor,
            "n_holdout": outcome.verdict.n_holdout,
            "partition_seed": outcome.partition_seed,
            "graded_by": outcome.graded_by,
            "measured_at": outcome.measured_at or utc_now(),
            "policy_version": outcome.verdict.policy_version,
        }
    )


def _judge_set_payload(judge_set: Mapping[str, Any]) -> dict[str, Any] | None:
    """Narrow the stored judge-set record to the four fields SetSpec's own model declares.

    The stored record carries more — the assembly's refusals, its self-judging exclusions — and
    those belong on the report a person reads rather than in a cross-application contract that
    has not declared them (API standards §7 rule 5: a writer never emits unknown fields).
    """
    if not judge_set:
        return None
    return {
        "jurors": list(judge_set.get("jurors", ())),
        "prompt_id": str(judge_set.get("prompt_id", "")),
        "prompt_version": str(judge_set.get("prompt_version", "")),
        "prompt_sha256": str(judge_set.get("prompt_sha256", "")),
        "remote": bool(judge_set.get("remote", False)),
    }


def goal_pack_payload(goal: Any) -> dict[str, Any]:  # noqa: ANN401 — a LoadedGoal
    """Build the ``benchmark.goal_pack`` payload from a loaded goal.

    Lives here rather than beside the HTTP route that first needed it: a service may not import a
    route (the dependency runs one way), and the export path and the ``GET /goals/{slug}/export``
    endpoint must produce the same bytes for the same pack or the two surfaces are two contracts.

    It carries the goal's *definition* — criteria, weights, rungs, task prompt identities and
    hashes — which is what a consumer needs to decide comparability. The portable *bundle*, which
    carries the files an importer would need, is ``freeweight goals export``.
    """
    pack = goal.pack
    judge_set = None
    if pack.judge is not None:
        judge_set = {
            "jurors": list(pack.judge.models),
            "prompt_id": pack.judge.prompt_id,
            "prompt_version": pack.judge.prompt_version,
            "prompt_sha256": (
                goal.judge_prompt.sha256 if goal.judge_prompt is not None else "sha256:unresolved"
            ),
            "remote": pack.judge.allow_remote,
        }
    return {
        "slug": pack.slug,
        "name": pack.name,
        "intent": pack.intent,
        "goal_pack_version": pack.goal_pack_version,
        "goal_hash": goal.goal_hash,
        "contributes_to": pack.contributes_to,
        "criteria": [
            {
                "key": criterion.key,
                "name": criterion.name,
                "rung": criterion.rung.value,
                "weight": criterion.weight,
                "is_gate": criterion.is_gate,
                "rule_type": criterion.rule_type,
                "scale_points": None if criterion.scale is None else criterion.scale.points,
                "has_scale_descriptors": (criterion.scale is not None and criterion.scale.anchored),
            }
            for criterion in pack.criteria
        ],
        "tasks": [
            {
                "key": task.key,
                "prompt_id": task.prompt_id,
                "prompt_version": task.prompt_version,
                "prompt_sha256": task.prompt_sha256,
                "is_starter": task.is_starter,
            }
            for task in pack.tasks
        ],
        "judge_set": judge_set,
        "unforked": pack.unforked,
        "created_by": pack.created_by,
        "created_at": pack.created_at or utc_now(),
    }
