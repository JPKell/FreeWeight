"""a machine can be given a nickname

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-12 00:00:00.000000

Row WX7. A machine is identified by its fingerprint, which is correct and unreadable: the console
lists three of them and an operator cannot tell which is the laptop. The nickname is the
operator's own label for one — written by ``PATCH /api/v1/machines/{id}``, shown wherever a
machine is named, and never used to identify anything. Identity stays the fingerprint.

One nullable column, no backfill and no default: a machine nobody has named has no nickname, which
is not the same as an empty one. Profiling a host on a later run refreshes every other column and
leaves this one alone, exactly as ADR-0118's ``models.enabled`` is left alone by discovery — an
operator's decision is not re-derived from the hardware.

Batch mode for the SQLite reason the earlier revisions record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("machines", schema=None) as batch_op:
        batch_op.add_column(sa.Column("nickname", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("machines", schema=None) as batch_op:
        batch_op.drop_column("nickname")
