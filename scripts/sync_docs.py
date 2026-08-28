"""Mirror the canonical FreeWeight documents into this repository's ``docs/``.

The suite's documentation lives in one place — ``AiSuite/docs/`` in the workspace — and each
component repository carries a copy of the documents that concern it so it can be worked on
standalone. A copy maintained by hand drifts, and did: the mirror once held four of the seven
documents, with the links to the missing three quietly stripped out of the four that were there.

This script is the whole convention, executable:

* every ``apps/freeweight/*.md`` is copied verbatim, so a link *between* mirrored documents keeps
  working;
* every link that points **outside** the mirrored set — an ADR, a standard, an architecture note —
  is **de-linked to its own text**, because a link to a file that is not in this repository is worse
  than plain prose: it looks navigable and is not.

Run it with ``--check`` in CI to fail when the mirror and the canonical copy have diverged.

Usage:
    python scripts/sync_docs.py            # write the mirror
    python scripts/sync_docs.py --check    # exit 1 if the mirror is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL = REPO_ROOT.parent / "docs"
MIRROR = REPO_ROOT / "docs"
SUBDIR = Path("apps/freeweight")

#: ``[text](target)`` where the target leaves ``apps/freeweight/`` — ``../../adr/…`` and friends.
_OUTBOUND = re.compile(r"\[([^\]]+)\]\(\.\./\.\./[^)]+\)")


def delink(text: str) -> str:
    """Return ``text`` with every link out of the mirrored set reduced to its label.

    Args:
        text: One document's markdown.

    Returns:
        The same markdown with outbound links flattened. Links between mirrored documents,
        anchors and absolute URLs are left alone.
    """
    return _OUTBOUND.sub(r"\1", text)


def render() -> dict[Path, str]:
    """Return ``{mirror path: content}`` for every document the mirror should hold.

    Returns:
        The complete intended mirror. Every ``.md`` under the canonical ``apps/freeweight/``
        directory is included; nothing else is.

    Raises:
        FileNotFoundError: The canonical documentation directory is not where it should be,
            which means this repository has been checked out away from the workspace.
    """
    source = CANONICAL / SUBDIR
    if not source.is_dir():
        raise FileNotFoundError(f"Canonical documentation not found at {source}.")
    return {
        MIRROR / SUBDIR / path.name: delink(path.read_text(encoding="utf-8"))
        for path in sorted(source.glob("*.md"))
    }


def main() -> int:
    """Write or verify the mirror.

    Returns:
        ``0`` when the mirror is written, or is already current under ``--check``; ``1`` when
        ``--check`` finds it stale, naming every file that differs.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify rather than write.")
    args = parser.parse_args()

    intended = render()
    stale = [
        path
        for path, content in intended.items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]
    if args.check:
        for path in stale:
            print(f"stale: {path.relative_to(REPO_ROOT)}")
        if stale:
            print(f"\n{len(stale)} document(s) differ. Run: python scripts/sync_docs.py")
        return 1 if stale else 0

    for path, content in intended.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"Mirrored {len(intended)} document(s); {len(stale)} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
