"""a scenario knows which save it is

Revision ID: d72ff0321e01
Revises: dfef73381ad7
Create Date: 2026-09-13 21:48:06.509770
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd72ff0321e01'
down_revision: Union[str, None] = 'dfef73381ad7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        # `server_default` because SQLite rebuilds the table for an ALTER and
        # has to put something in the new column for the rows already there.
        # Everything that exists is version one, which is true: it has been
        # saved once, by whoever wrote it.
        batch_op.add_column(
            sa.Column('version', sa.Integer(), nullable=False, server_default='1')
        )


def downgrade() -> None:
    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        batch_op.drop_column('version')
