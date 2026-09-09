"""an operator can disable a discovered model

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-09 00:00:00.000000

ADR-0118, FreeWeight's half: a disabled model is not offered as a benchmark subject and a run
that names one is refused. The flag is the operator's; discovery writes every other column on a
rescan and leaves this one alone, so a model disabled here stays disabled.

Default true, no backfill: every existing model was measurable before this revision and stays
measurable after it. The server default keeps `NOT NULL` satisfiable for a row inserted by older
code against a migrated database; batch mode for the SQLite reason the earlier revisions record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.drop_column("enabled")
