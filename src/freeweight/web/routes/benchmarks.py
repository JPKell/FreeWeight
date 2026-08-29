"""freeweight.web.routes.benchmarks — the installed suites, over the API.

Spec §7.1 has declared ``GET /api/v1/benchmarks`` and ``GET /api/v1/benchmarks/{key}`` since Phase
1, and nothing built them: no phase of the development plan needed the API form, so no test ever
asked for one. ``tests/contract/test_declared_surface.py`` now asserts that every path §7.1
declares is routable, which is what turned "not built" from an absence into a failure.

A route handler contains no business logic (coding standards): both handlers here read the
registry the application already holds — the same one the run engine executes from, so a suite this
lists is a suite that can be run — and render it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baseaicore import NotFoundError
from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from freeweight.domain.benchmark import BenchmarkManifest

__all__ = ["api_router"]

api_router = APIRouter(tags=["benchmarks"])


def _manifest_json(manifest: BenchmarkManifest) -> dict[str, Any]:
    """One suite's manifest, as its API body."""
    return {
        "key": manifest.key,
        "name": manifest.name,
        "version": manifest.version,
        "category": manifest.category,
        "runner": manifest.runner,
        "capabilities": list(manifest.capabilities),
        "requires": dict(manifest.requires),
        "dataset_hashes": dict(manifest.dataset_hashes),
        "prompt_subset_hash": manifest.prompt_subset_hash,
        "manifest_hash": manifest.manifest_hash,
        "headline_metric": manifest.headline_metric,
        "license": manifest.license,
    }


@api_router.get("/benchmarks", summary="Installed benchmark suites")
def list_benchmarks_endpoint(request: Request) -> dict[str, Any]:
    """Return every suite this build can run, in key order.

    Read from the registry the run engine executes from, deliberately: a suite listed here is a
    suite that can be run, and a listing assembled separately would eventually disagree with what
    ``POST /runs`` accepts. Goal suites appear alongside the shipped ones, because to a client
    they are ordinary suites — what makes them different is who authored them
    ([ADR-0031](../../../../docs/adr/)).

    Returns:
        ``{"items": [...]}`` with each suite's manifest identity and its test count.
    """
    registry = request.app.state.registry
    return {
        "items": [
            {
                **_manifest_json(benchmark.manifest),
                "test_count": len(benchmark.tests),
            }
            for benchmark in registry.all()
        ]
    }


@api_router.get("/benchmarks/{key}", summary="One suite's manifest, tests and metrics")
def get_benchmark_endpoint(request: Request, key: str) -> dict[str, Any]:
    """Return one suite with its tests, their metric definitions and its prompt references.

    Args:
        request: The incoming request.
        key: The suite key, e.g. ``native.performance`` or ``goal.house_voice``.

    Returns:
        The manifest, plus one entry per test naming the metrics it declares.

    Raises:
        NotFoundError: No suite is registered under ``key``, answered as ``404`` naming what is.
    """
    registry = request.app.state.registry
    if key not in registry.keys():
        raise NotFoundError(
            f"No benchmark suite {key!r} is installed.",
            details={"benchmark": key, "installed": list(registry.keys())},
        )
    benchmark = registry.get(key)
    return {
        **_manifest_json(benchmark.manifest),
        "prompt_ids": [dict(entry) for entry in benchmark.manifest.prompt_ids],
        "tests": [
            {
                "key": test.key,
                "name": test.name,
                "measurement_class": test.measurement_class,
                "case_count": len(list(test.cases())),
                "metrics": [
                    {
                        "metric_key": metric.metric_key,
                        "unit": metric.unit,
                        "higher_is_better": metric.higher_is_better,
                        "aggregation": metric.aggregation,
                    }
                    for metric in test.metrics
                ],
            }
            for test in benchmark.tests
        ],
    }
