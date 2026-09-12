"""style presets, per-clip compositions

Revision ID: b36143d838f7
Revises: 1446f0c589c8
Create Date: 2026-09-08 02:25:04.918381
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'b36143d838f7'
down_revision: Union[str, None] = '1446f0c589c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK_STYLE = "fk_clip_jobs_style_id"


def upgrade() -> None:
    op.create_table(
        'styles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=True),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(length=500),
                  nullable=False, server_default=''),
        sa.Column('profile', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=True),
        sa.Column('data_json', sqlmodel.sql.sqltypes.AutoString(),
                  nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('styles', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_styles_name'), ['name'], unique=True)
        batch_op.create_index(batch_op.f('ix_styles_owner_id'), ['owner_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_styles_profile'), ['profile'], unique=False)

    with op.batch_alter_table('clip_jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('style_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_clip_jobs_style_id'), ['style_id'], unique=False)
        # Named, because SQLite implements an ALTER as a table rebuild and an
        # anonymous constraint cannot be dropped again on the way back down.
        batch_op.create_foreign_key(FK_STYLE, 'styles', ['style_id'], ['id'])

    with op.batch_alter_table('clips', schema=None) as batch_op:
        # server_default, so clips rendered before compositions were stored
        # keep working: without it the rebuild has no value to put in the new
        # column for existing rows.
        batch_op.add_column(sa.Column('composition_json', sqlmodel.sql.sqltypes.AutoString(),
                                      nullable=False, server_default='{}'))


def downgrade() -> None:
    with op.batch_alter_table('clips', schema=None) as batch_op:
        batch_op.drop_column('composition_json')

    with op.batch_alter_table('clip_jobs', schema=None) as batch_op:
        batch_op.drop_constraint(FK_STYLE, type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_clip_jobs_style_id'))
        batch_op.drop_column('style_id')

    with op.batch_alter_table('styles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_styles_profile'))
        batch_op.drop_index(batch_op.f('ix_styles_owner_id'))
        batch_op.drop_index(batch_op.f('ix_styles_name'))

    op.drop_table('styles')
