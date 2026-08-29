"""Publish the OpenAPI snapshot: ``docs/openapi.json``, the committed contract of ``/api/v1``.

API standards §11: the OpenAPI document is committed as a snapshot artifact and CI fails when it
changes without a corresponding review — which is how an accidental breaking change to a route
is caught. Phase 11 publishes it (development plan, "publish the OpenAPI snapshot"), and the I3
milestone test compares the served surface against this file.

Built from :func:`freeweight.web.app.create_app` under a throwaway XDG root, so generating the
snapshot never touches the developer's real configuration or data.

Usage:
    python scripts/generate_openapi_snapshot.py            # write docs/openapi.json
    python scripts/generate_openapi_snapshot.py --check    # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "docs" / "openapi.json"

sys.path.insert(0, str(REPO_ROOT / "src"))


def render() -> str:
    """Render the served OpenAPI document, key-sorted and indented for review diffs."""
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["XDG_DATA_HOME"] = str(Path(scratch) / "data")
        os.environ["XDG_CONFIG_HOME"] = str(Path(scratch) / "config")
        os.environ["XDG_STATE_HOME"] = str(Path(scratch) / "state")
        os.environ["FREEWEIGHT_PROVIDER__KIND"] = "fake"
        from freeweight.config import load_settings
        from freeweight.web.app import create_app

        document = create_app(load_settings().settings).openapi()
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    """Write or verify the snapshot.

    Returns:
        ``0`` when written, or current under ``--check``; ``1`` when ``--check`` finds it stale.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify rather than write.")
    args = parser.parse_args()
    intended = render()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else None
    if args.check:
        if current != intended:
            print(f"stale: {TARGET.relative_to(REPO_ROOT)}")
            print("Run: python scripts/generate_openapi_snapshot.py — and record the change.")
            return 1
        print(f"current: {TARGET.relative_to(REPO_ROOT)}")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(intended, encoding="utf-8")
    state = "changed" if current != intended else "unchanged"
    print(f"Wrote {TARGET.relative_to(REPO_ROOT)} ({state}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
