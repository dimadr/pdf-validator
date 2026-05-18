"""Add object_date to objects table

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-18 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('objects', sa.Column('object_date', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('objects', 'object_date')
