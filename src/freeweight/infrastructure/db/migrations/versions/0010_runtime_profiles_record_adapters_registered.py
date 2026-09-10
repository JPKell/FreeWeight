"""runtime profiles record adapters_registered

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-10 00:00:00.000000

Row WA1 (ADR-0135). ``baseaicore.RuntimeProfile.profile_hash`` hashes ``adapters_registered``
whenever it is stated (ADR-0074), and FreeWeight has stated it — ``true`` or ``false`` — for every
run on an adapter-capable provider since 1.1.0. This table had no column for it, so the export sent
a profile without the field and SetSpec refused the run's summary, and a queued, resumed or
repeated run rebuilt a profile that was not the one it had been hashed under.

One nullable column. ``NULL`` means not stated, exactly as on the domain type.

**The backfill recovers the value; it never guesses.** The hash is a pure function of the profile's
fields, so each existing row's three candidates — ``NULL``, ``false``, ``true`` — are hashed with
the row's other columns, and the one that reproduces the stored ``profile_hash`` is written. A row
that matches none stays ``NULL`` and is logged at WARNING by id: its hash came from something this
table cannot reconstruct, and inventing a value would describe a profile that never ran. The counts
by outcome are logged at INFO.

``baseaicore.RuntimeProfile`` is imported rather than the hash reimplemented here. Its hash is
pinned by BaseAiCore's own goldens, and a second implementation is the one thing that could
disagree with the stored values this recovery reads.

SQLite adds a nullable column in place, with no table rebuild; batch mode for the reason the
earlier revisions record. ``downgrade`` drops the column and the recovered values with it — an
``upgrade`` recovers them again from the unchanged hashes.
"""

from __future__ import annotations

import logging
from collections import Counter

import sqlalchemy as sa
from alembic import op
from baseaicore import RuntimeProfile
from weightsdb import PortableJSON

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_log = logging.getLogger(__name__)

_STATES: tuple[bool | None, ...] = (None, False, True)

_runtime_profiles = sa.table(
    "runtime_profiles",
    sa.column("id", sa.String()),
    sa.column("profile_hash", sa.String()),
    sa.column("context_size", sa.Integer()),
    sa.column("kv_cache_precision", sa.String()),
    sa.column("gpu_layers", sa.Integer()),
    sa.column("flash_attention", sa.Boolean()),
    sa.column("threads", sa.Integer()),
    sa.column("batch_size", sa.Integer()),
    sa.column("keep_alive", sa.String()),
    sa.column("adapters_registered", sa.Boolean()),
    sa.column("provider_options_json", PortableJSON()),
)


def upgrade() -> None:
    with op.batch_alter_table("runtime_profiles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("adapters_registered", sa.Boolean(), nullable=True))

    bind = op.get_bind()
    counts: Counter[str] = Counter()
    unmatched: list[str] = []
    for row in bind.execute(sa.select(_runtime_profiles)).mappings().all():
        options = row["provider_options_json"]
        matching = [
            state
            for state in _STATES
            if RuntimeProfile(
                context_size=row["context_size"],
                kv_cache_precision=row["kv_cache_precision"],
                gpu_layers=row["gpu_layers"],
                flash_attention=row["flash_attention"],
                threads=row["threads"],
                batch_size=row["batch_size"],
                keep_alive=row["keep_alive"],
                adapters_registered=state,
                provider_options=dict(options) if isinstance(options, dict) else {},
            ).profile_hash
            == row["profile_hash"]
        ]
        if not matching:
            counts["unmatched"] += 1
            unmatched.append(str(row["id"]))
            continue
        # The three candidates hash to three different values, so at most one matches.
        state = matching[0]
        counts["null" if state is None else str(state).lower()] += 1
        if state is not None:
            bind.execute(
                sa.update(_runtime_profiles)
                .where(_runtime_profiles.c.id == row["id"])
                .values(adapters_registered=state)
            )

    _log.info(
        "adapters_registered backfill: %d null, %d false, %d true, %d unmatched",
        counts["null"],
        counts["false"],
        counts["true"],
        counts["unmatched"],
    )
    if unmatched:
        _log.warning(
            "%d runtime_profiles row(s) match no adapters_registered state and stay NULL: %s",
            len(unmatched),
            ", ".join(unmatched),
        )


def downgrade() -> None:
    with op.batch_alter_table("runtime_profiles", schema=None) as batch_op:
        batch_op.drop_column("adapters_registered")
