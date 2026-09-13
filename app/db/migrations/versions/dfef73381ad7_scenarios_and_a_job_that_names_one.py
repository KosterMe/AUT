"""scenarios, and a job that names one

Revision ID: dfef73381ad7
Revises: 4920581af4ef
Create Date: 2026-09-13 17:13:49.245079
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'dfef73381ad7'
down_revision: Union[str, None] = '4920581af4ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Which signal each of the four profiles cut on. The mapping is the whole of
# the data migration: a profile said two things at once, and this is the half
# that was about cutting.
CUTTERS = {"talking": "speech", "plain": "speech", "split": "speech", "film": "scenes"}


def upgrade() -> None:
    op.create_table(
        'scenarios',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=True),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(length=500),
                  nullable=False, server_default=''),
        sa.Column('builtin', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('data_json', sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scenarios_builtin'), ['builtin'], unique=False)
        batch_op.create_index(batch_op.f('ix_scenarios_name'), ['name'], unique=True)
        batch_op.create_index(batch_op.f('ix_scenarios_owner_id'), ['owner_id'], unique=False)

    with op.batch_alter_table('clip_jobs', schema=None) as batch_op:
        # server_default for the reason the last two columns needed one: the
        # table rebuild SQLite does for an ALTER has to put something in the
        # new column for rows that already exist.
        batch_op.add_column(sa.Column('cutter', sqlmodel.sql.sqltypes.AutoString(length=16),
                                      nullable=False, server_default='speech'))
        batch_op.add_column(sa.Column('scenario_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_clip_jobs_cutter'), ['cutter'], unique=False)
        batch_op.create_index(batch_op.f('ix_clip_jobs_scenario_id'), ['scenario_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_clip_jobs_scenario_id_scenarios', 'scenarios', ['scenario_id'], ['id'],
        )

    # The half of the profile that was about cutting, written down where it
    # belongs. `speech` is already the default, so only the films move — but
    # they are named rather than assumed, because a fifth profile arriving
    # without a line here should be visible as a missing line.
    jobs = sa.table('clip_jobs', sa.column('profile', sa.String), sa.column('cutter', sa.String))
    for profile, cutter in CUTTERS.items():
        op.execute(
            jobs.update().where(jobs.c.profile == op.inline_literal(profile))
            .values(cutter=op.inline_literal(cutter))
        )

    # `scenario_id` is deliberately left null. A job that never chose a
    # scenario renders with the built-in its profile names, which is what it
    # rendered with yesterday — pointing every old job at a row would make the
    # migration responsible for a decision it has no way to get right.


def downgrade() -> None:
    with op.batch_alter_table('clip_jobs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_clip_jobs_scenario_id_scenarios', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_clip_jobs_scenario_id'))
        batch_op.drop_index(batch_op.f('ix_clip_jobs_cutter'))
        batch_op.drop_column('scenario_id')
        batch_op.drop_column('cutter')

    with op.batch_alter_table('scenarios', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_scenarios_owner_id'))
        batch_op.drop_index(batch_op.f('ix_scenarios_name'))
        batch_op.drop_index(batch_op.f('ix_scenarios_builtin'))

    op.drop_table('scenarios')
