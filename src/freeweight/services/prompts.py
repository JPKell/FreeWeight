"""freeweight.services.prompts — FreeWeight's own prompt pack location, on ``setspec.prompts``.

The loader, validator, renderer and hasher moved to ``setspec.prompts`` at SetSpec Phase 5 /
LoadCoach Phase 4 ([ADR-0011](../../../../docs/adr/0011-shared-package-boundaries.md),
[ADR-0028](../../../../docs/adr/0028-prompt-pack-granularity.md)) — this module is now only
FreeWeight's own pack location (:data:`PACK_ROOT`) and the default-path wrapper around
:func:`~setspec.prompts.load_pack` that every existing call site in this application already
depends on. Every name this module exported before the move is still importable from here,
unchanged (Phase 12's own acceptance criterion: "the existing test suite passes unchanged").
"""

from __future__ import annotations

from pathlib import Path

from setspec.prompts import (
    PROMPT_RECORD_SCHEMA_VERSION,
    ManifestDrift,
    PromptLibrary,
    PromptNotFound,
    PromptPackInvalid,
    PromptRecord,
    PromptReference,
    PromptRenderError,
    PromptVariableError,
    RenderedPrompt,
    VariableSpec,
    build_manifest,
    load_record,
    pack_hash,
    prompt_record_hash,
    prompt_subset_hash,
    write_manifest,
)
from setspec.prompts import load_pack as _setspec_load_pack

__all__ = [
    "PACK_ROOT",
    "PROMPT_RECORD_SCHEMA_VERSION",
    "ManifestDrift",
    "PromptLibrary",
    "PromptNotFound",
    "PromptPackInvalid",
    "PromptRecord",
    "PromptReference",
    "PromptRenderError",
    "PromptVariableError",
    "RenderedPrompt",
    "VariableSpec",
    "build_manifest",
    "load_pack",
    "load_record",
    "pack_hash",
    "prompt_record_hash",
    "prompt_subset_hash",
    "write_manifest",
]

PACK_ROOT = Path(__file__).resolve().parent.parent / "prompts"
"""FreeWeight's own pack, shipped inside the package. The one place a default path appears."""


def load_pack(root: Path = PACK_ROOT, *, override_root: Path | None = None) -> PromptLibrary:
    """Load, validate and index FreeWeight's own prompt pack (or ``root``, if given).

    Thin wrapper over :func:`setspec.prompts.load_pack` that restores the default-to-``PACK_ROOT``
    convenience every existing call site in this application relies on — the generic function
    itself takes no default, since it has no opinion about any one application's directory layout.
    See :func:`setspec.prompts.load_pack` for the full contract (arguments, return value, and what
    it raises).
    """
    return _setspec_load_pack(root, override_root=override_root)
