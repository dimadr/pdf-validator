"""Add read receipt tracking fields

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-16 11:30:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('attachments', sa.Column('read_receipt_received', sa.Boolean(), nullable=True, server_default='false'))
    op.add_column('attachments', sa.Column('read_receipt_at', sa.DateTime(), nullable=True))
    op.add_column('attachments', sa.Column('original_message_id', sa.String(length=255), nullable=True))
    op.add_column('attachments', sa.Column('sent_filename', sa.String(length=255), nullable=True))
    op.add_column('attachments', sa.Column('calculator_number', sa.String(length=50), nullable=True))
    op.create_index('idx_attachments_calculator', 'attachments', ['calculator_number'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_attachments_calculator', table_name='attachments')
    op.drop_column('attachments', 'calculator_number')
    op.drop_column('attachments', 'sent_filename')
    op.drop_column('attachments', 'original_message_id')
    op.drop_column('attachments', 'read_receipt_at')
    op.drop_column('attachments', 'read_receipt_received')
