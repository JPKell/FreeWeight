"""freeweight.domain.goals — a benchmark the user writes, as a domain object.

[ADR-0031](../../../../../docs/adr/0031-user-defined-goal-benchmarks.md) makes a user-authored,
criterion-scored goal a third runner kind alongside ``native`` and ``external``. This package holds
what a goal *is*, with no filesystem, no database and no provider in it:

* :mod:`~freeweight.domain.goals.pack` — the pack's shape, parsed and structurally validated.
* :mod:`~freeweight.domain.goals.hashing` — ``goal_hash``, over the measurement-defining subset
  only.
* :mod:`~freeweight.domain.goals.criteria` — scoring one criterion, under a timeout.
* :mod:`~freeweight.domain.goals.lint` — every problem a pack has, with a severity.
* :mod:`~freeweight.domain.goals.composite` — criteria become one score, gates and all.

Loading a pack from disk is :mod:`freeweight.services.goals`' job, because a task prompt is a
prompt record and prompt records are loaded by a service.
"""

from __future__ import annotations
