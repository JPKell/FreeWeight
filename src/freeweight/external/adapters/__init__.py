"""freeweight.external.adapters — the registry of external benchmark adapters.

Nine adapters ship for 1.0. Each is a value object with a manifest and a pure ``parse`` function;
:data:`ADAPTERS` is the lookup ``freeweight external list|install|verify`` and the run engine use.
The keys are namespaced under ``external.`` so they never collide with a native suite.
"""

from __future__ import annotations

from freeweight.external.adapters.base import Adapter, AdapterOutcome, AdapterSample
from freeweight.external.adapters.bfcl import BfclAdapter
from freeweight.external.adapters.criticbench import CriticBenchAdapter
from freeweight.external.adapters.cruxeval import CruxEvalAdapter
from freeweight.external.adapters.evalplus import EvalPlusAdapter
from freeweight.external.adapters.ifeval import IFEvalAdapter
from freeweight.external.adapters.judgebench import JudgeBenchAdapter
from freeweight.external.adapters.llmbar import LlmBarAdapter
from freeweight.external.adapters.lm_eval_harness import LmEvalHarnessAdapter
from freeweight.external.adapters.ruler import RulerAdapter

__all__ = [
    "ADAPTERS",
    "Adapter",
    "AdapterOutcome",
    "AdapterSample",
    "get_adapter",
]

_ADAPTER_TYPES: tuple[type, ...] = (
    LmEvalHarnessAdapter,
    IFEvalAdapter,
    EvalPlusAdapter,
    CruxEvalAdapter,
    BfclAdapter,
    RulerAdapter,
    JudgeBenchAdapter,
    LlmBarAdapter,
    CriticBenchAdapter,
)

ADAPTERS: dict[str, Adapter] = {}
for _adapter_type in _ADAPTER_TYPES:
    _instance = _adapter_type()
    ADAPTERS[_instance.manifest.key] = _instance


def get_adapter(key: str) -> Adapter | None:
    """Return the adapter for ``key`` (``external.ifeval``), or ``None`` if none is registered."""
    return ADAPTERS.get(key)
