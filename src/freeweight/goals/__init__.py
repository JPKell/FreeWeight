"""freeweight.goals — the goal content that ships with the application.

Only the four starter packs live here. A *user's* goals live under ``goals.root`` in their config
directory, hand-editable and git-trackable (ADR-0031 §6); nothing in this package is ever written
to, and nothing here is loaded as a runnable goal until a user forks it.
"""

from __future__ import annotations

__all__: list[str] = []
