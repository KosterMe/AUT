"""job owns its montage options

Revision ID: 4920581af4ef
Revises: b36143d838f7
Create Date: 2026-09-13 10:13:46.651658
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = '4920581af4ef'
down_revision: Union[str, None] = 'b36143d838f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('clip_jobs', schema=None) as batch_op:
        # server_default for the same reason the profile column needed one: the
        # rebuild SQLite does for an ALTER has to put *something* in the new
        # column for rows that already exist, and a NOT NULL column with no
        # default gives it nothing to put.
        #
        # An empty object is also the right value for a job planned before this
        # existed: its options are still in its render task's payload, and
        # `clips.render_options_of` reads that when the job carries none. So a
        # queue that was full during the upgrade drains with the options it was
        # queued with, and nothing needs backfilling.
        batch_op.add_column(sa.Column('render_options_json',
                                      sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default='{}'))
        batch_op.add_column(sa.Column('transcript_settings_json',
                                      sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('clip_jobs', schema=None) as batch_op:
        batch_op.drop_column('transcript_settings_json')
        batch_op.drop_column('render_options_json')
