"""`freeweight doctor` diagnoses every failure mode the troubleshooting guide lists (P14 AC3).

The guide's headings *are* the health components, and this test holds the two in lockstep: every
component the doctor reports has a section in ``docs/troubleshooting.md``, and every component
section in the guide names a real component. The guide comes before the doctor's last test
(dev-plan P14), so a component added without a heading, or a heading for a component that no longer
exists, fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

from freeweight.services.health import get_health_report

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "troubleshooting.md"


def _guide_component_headings() -> set[str]:
    """The ``##`` headings in the guide that name a health component (lower_snake_case)."""
    headings = re.findall(r"^## (\w+)$", GUIDE.read_text(encoding="utf-8"), re.MULTILINE)
    # "Common non-component problems" is a prose heading, not a component; component headings are
    # exactly the lower_snake_case ones.
    return {heading for heading in headings if heading == heading.lower()}


def test_every_reported_component_has_a_troubleshooting_section() -> None:
    from freeweight.config import load_settings

    report = get_health_report(settings=load_settings().settings)
    reported = {component.name for component in report.components}
    documented = _guide_component_headings()

    missing = reported - documented
    assert missing == set(), (
        f"components the doctor reports but the guide does not document: {missing}"
    )


def test_every_documented_component_is_a_real_component() -> None:
    from freeweight.config import load_settings

    report = get_health_report(settings=load_settings().settings)
    reported = {component.name for component in report.components}
    documented = _guide_component_headings()

    stale = documented - reported
    assert stale == set(), f"the guide documents components that no longer exist: {stale}"
