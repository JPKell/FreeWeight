"""Every path [spec §7.1](../../docs/apps/freeweight/spec.md) declares is routable.

Three endpoints were declared and unbuilt for eight phases, and the way that was found was a live
end-to-end journey getting a 404 — at Phase 10A, from a document written at Phase 1. Nothing had
ever asserted that the specification's own list of paths was served, so the gap could only surface
when somebody used one.

This is that assertion. It reads §7.1 out of the specification, not out of a list maintained beside
it: a copy would drift the same way, and the failure mode this exists to prevent *is* drift between
the document and the build.

A path the specification declares but this phase does not build is listed in :data:`SCHEDULED`,
with the phase that owns it — so "not built yet" stays a decision with an owner rather than
becoming an absence nobody notices.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "docs" / "apps" / "freeweight" / "spec.md"

#: Paths §7.1 declares that a later phase owns. Keyed by path, valued by why.
SCHEDULED: dict[str, str] = {
    "/api/v1/evidence": "Phase 11 — capability evidence and the LoadCoach contract",
    "/api/v1/evidence/export": "Phase 11 — capability evidence and the LoadCoach contract",
}

_PATH = re.compile(r"(?:GET|POST|PUT|DELETE|PATCH)\s+(/api/v1/\S+)")


def _declared_paths() -> set[str]:
    """Read §7.1's code block and return every path it declares."""
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("### 7.1 HTTP")
    block = text[start : text.index("```", text.index("```", start) + 3)]
    return {match.group(1).rstrip("†") for match in _PATH.finditer(block)}


def _served_paths(app: Any) -> set[str]:
    """Every ``/api/v1`` path the running application actually routes.

    Two sources, unioned, because neither alone is the whole truth: the OpenAPI document is the
    published contract but omits any route excluded from the schema — both SSE endpoints are — and
    the route tree carries those but wraps each included router in a node whose own ``path`` is
    ``None``. "Routable" is the property this module is about, so it asks both.
    """
    served: set[str] = set(app.openapi()["paths"])
    for node in app.routes:
        router = getattr(node, "original_router", None)
        prefix = getattr(router, "prefix", "") or ""
        for route in getattr(router, "routes", ()) or ():
            path = getattr(route, "path", None)
            if isinstance(path, str) and path:
                served.add(prefix + path)
    return {path for path in served if path.startswith("/api/v1")}


def _normalize(path: str) -> str:
    """Reduce a path to its shape, so ``{id}`` and ``{run_id}`` compare equal."""
    return re.sub(r"\{[^}]+\}", "{}", path).rstrip("/")


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The application as it is actually served, built under a throwaway XDG root."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from freeweight.config import load_settings
    from freeweight.web.app import create_app

    return create_app(load_settings().settings)


class TestTheDeclaredSurfaceIsServed:
    def test_the_specification_still_declares_a_path_list(self) -> None:
        """If §7.1 is restructured, this whole module silently passes. So check it first."""
        declared = _declared_paths()
        assert len(declared) > 20, f"§7.1 parsed to only {len(declared)} paths; has it moved?"

    def test_every_declared_path_is_routable(self, app: Any) -> None:
        """The assertion that would have caught the models API at Phase 3."""
        served = {_normalize(path) for path in _served_paths(app)}
        missing = sorted(
            path
            for path in _declared_paths()
            if _normalize(path) not in served and path not in SCHEDULED
        )
        assert not missing, (
            "spec §7.1 declares paths this build does not route. Either build them, or record the "
            f"phase that owns them in SCHEDULED: {missing}"
        )

    def test_every_scheduled_path_is_still_declared(self) -> None:
        """A scheduled path removed from the spec must not linger here as a permanent excuse."""
        declared = _declared_paths()
        stale = sorted(path for path in SCHEDULED if path not in declared)
        assert not stale, f"SCHEDULED names paths §7.1 no longer declares: {stale}"

    def test_every_scheduled_path_is_genuinely_absent(self, app: Any) -> None:
        """And one that *has* been built must be removed from the list, or the list starts lying."""
        served = {_normalize(path) for path in _served_paths(app)}
        built = sorted(path for path in SCHEDULED if _normalize(path) in served)
        assert not built, f"SCHEDULED still lists paths that are now served: {built}"
