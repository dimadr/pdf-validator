"""Add Brevo observations and durable polling cursors."""
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = depends_on = None


def upgrade():
    op.execute('''CREATE TABLE brevo_delivery_events (
      event_key VARCHAR(64) PRIMARY KEY, smtp_attempt_id UUID NOT NULL REFERENCES smtp_attempts(id),
      provider_message_id VARCHAR(255) NOT NULL, event_type VARCHAR(64) NOT NULL,
      occurred_at TIMESTAMPTZ, reason TEXT, processing_state VARCHAR(32) NOT NULL,
      payload JSONB NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
    op.create_index('ix_brevo_delivery_events_smtp_attempt_id', 'brevo_delivery_events', ['smtp_attempt_id'])
    op.execute('''CREATE TABLE brevo_poll_checkpoints (
      smtp_attempt_id UUID PRIMARY KEY REFERENCES smtp_attempts(id), next_poll_at TIMESTAMPTZ NOT NULL,
      page_offset INTEGER NOT NULL DEFAULT 0, window_end TIMESTAMPTZ,
      last_polled_at TIMESTAMPTZ, last_error VARCHAR(64))''')
    op.create_index('ix_brevo_poll_checkpoints_next_poll_at', 'brevo_poll_checkpoints', ['next_poll_at'])


def downgrade():
    raise RuntimeError('Brevo delivery history must be preserved')
