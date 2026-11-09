"""Initial migration

Revision ID: 0001
Revises: 
Create Date: 2026-02-12 07:22:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create objects table
    op.create_table('objects',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('name_norm', sa.String(length=255), nullable=False),
        sa.Column('address', sa.String(length=255), nullable=True),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name_norm')
    )
    op.create_index('idx_objects_name_norm', 'objects', ['name_norm'], unique=False)
    op.create_index('idx_objects_active', 'objects', ['is_active'], unique=False)

    # Create email_sources table
    op.create_table('email_sources',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email')
    )
    op.create_index('idx_email_sources_active', 'email_sources', ['is_active'], unique=False)

    # Create incoming_messages table
    op.create_table('incoming_messages',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('provider_message_id', sa.String(length=255), nullable=False),
        sa.Column('source_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('from_email', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.Text(), nullable=True),
        sa.Column('parsed_object', sa.String(length=255), nullable=True),
        sa.Column('parsed_address', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=True),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_id'], ['email_sources.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider_message_id')
    )
    op.create_index('idx_messages_provider_id', 'incoming_messages', ['provider_message_id'], unique=False)
    op.create_index('idx_messages_status', 'incoming_messages', ['status'], unique=False)
    op.create_index('idx_messages_source', 'incoming_messages', ['source_id'], unique=False)
    op.create_index('idx_messages_received', 'incoming_messages', ['received_at'], unique=False)

    # Create attachments table
    op.create_table('attachments',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('message_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('object_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('file_path', sa.String(length=500), nullable=True),
        sa.Column('file_sha256', sa.String(length=64), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('reject_reason', sa.String(length=100), nullable=True),
        sa.Column('validation_result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('gpt_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('sent_to_email', sa.String(length=255), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['message_id'], ['incoming_messages.id'], ),
        sa.ForeignKeyConstraint(['object_id'], ['objects.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('file_sha256')
    )
    op.create_index('idx_attachments_sha256', 'attachments', ['file_sha256'], unique=False)
    op.create_index('idx_attachments_status', 'attachments', ['status'], unique=False)
    op.create_index('idx_attachments_message', 'attachments', ['message_id'], unique=False)
    op.create_index('idx_attachments_object', 'attachments', ['object_id'], unique=False)
    op.create_index('idx_attachments_created', 'attachments', ['created_at'], unique=False)

    # Create reports table
    op.create_table('reports',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('attachment_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('report_type', sa.String(length=50), nullable=True),
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['attachment_id'], ['attachments.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_reports_type', 'reports', ['report_type'], unique=False)
    op.create_index('idx_reports_attachment', 'reports', ['attachment_id'], unique=False)
    op.create_index('idx_reports_created', 'reports', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_reports_created', table_name='reports')
    op.drop_index('idx_reports_attachment', table_name='reports')
    op.drop_index('idx_reports_type', table_name='reports')
    op.drop_table('reports')
    op.drop_index('idx_attachments_created', table_name='attachments')
    op.drop_index('idx_attachments_object', table_name='attachments')
    op.drop_index('idx_attachments_message', table_name='attachments')
    op.drop_index('idx_attachments_status', table_name='attachments')
    op.drop_index('idx_attachments_sha256', table_name='attachments')
    op.drop_table('attachments')
    op.drop_index('idx_messages_received', table_name='incoming_messages')
    op.drop_index('idx_messages_source', table_name='incoming_messages')
    op.drop_index('idx_messages_status', table_name='incoming_messages')
    op.drop_index('idx_messages_provider_id', table_name='incoming_messages')
    op.drop_table('incoming_messages')
    op.drop_index('idx_email_sources_active', table_name='email_sources')
    op.drop_table('email_sources')
    op.drop_index('idx_objects_active', table_name='objects')
    op.drop_index('idx_objects_name_norm', table_name='objects')
    op.drop_table('objects')