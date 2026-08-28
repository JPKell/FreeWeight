"""freeweight.benchmarks.corpora — the shipped corpora the Phase 8 suites are built from.

Four JSON files, one per suite, holding the material that is *data rather than code*: the mutated
and clean code snippets ``native.audit`` shows a model, the question/answer/known-correctness
triples ``native.critique`` reviews, the answer pairs and triples ``native.judge`` compares, and
the needles and filler ``native.long_context`` buries them in.

They live here rather than beside each suite because they are the thing whose *hash* is pinned in
a manifest's ``dataset_hashes``: a corpus that moved would move a suite's provenance with it, and
one directory with one loader makes "which bytes did this run measure" a single question with a
single answer.

**Nothing here is generated at import time.** :func:`load` reads a file and parses it; the suites
render their cases from what it returns. The one exception is ``native.long_context``, whose filler
text is expanded from a sentence pool by its own module — a hundred thousand tokens of lorem ipsum
checked into git would be a corpus nobody can diff.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from baseaicore import canonical_json, sha256_of

__all__ = ["CORPUS_ROOT", "corpus_hash", "load"]

CORPUS_ROOT = Path(__file__).parent
"""The directory the corpora are read from."""


@lru_cache(maxsize=8)
def _read(name: str) -> str:
    """Read one corpus file's text, once per process."""
    return (CORPUS_ROOT / f"{name}.json").read_text(encoding="utf-8")


def load(name: str) -> dict[str, Any]:
    """Load one corpus by name.

    Args:
        name: The file's stem, e.g. ``"audit_code"``.

    Returns:
        The parsed object.

    Raises:
        FileNotFoundError: The corpus is not installed — a packaging defect, raised at suite-build
            time, which is startup.
        ValueError: The file is not a JSON object. A corpus that parsed to a list would be read
            differently by every suite that touched it.
    """
    body = json.loads(_read(name))
    if not isinstance(body, dict):
        raise ValueError(f"Corpus {name!r} is not a JSON object.")
    return body


def corpus_hash(name: str) -> str:
    """Return the ``sha256:``-prefixed hash of one corpus's canonical content.

    Over canonical JSON rather than the file's bytes, exactly as a manifest hash is: re-indenting
    a corpus must not separate a suite's results from the ones it produced yesterday.

    Args:
        name: The file's stem.

    Returns:
        ``"sha256:"`` followed by 64 lowercase hex characters.
    """
    return f"sha256:{sha256_of(canonical_json(load(name)))}"
