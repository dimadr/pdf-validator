"""Add email config settings

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-17 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Email config settings will be stored in settings table, no new columns needed
    pass


def downgrade() -> None:
    pass
