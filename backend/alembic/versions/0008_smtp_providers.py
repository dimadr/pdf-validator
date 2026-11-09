"""Additive SMTP selection and per-attempt provenance; keep Timeweb initially."""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = depends_on = None

def upgrade():
    op.add_column('recipient_deliveries', sa.Column('smtp_provider', sa.String(16)))
    op.add_column('recipient_deliveries', sa.Column('smtp_response_code', sa.Integer()))
    op.add_column('recipient_deliveries', sa.Column('smtp_response', sa.Text()))
    op.execute('''CREATE TABLE smtp_provider_audit (
        id UUID PRIMARY KEY, actor VARCHAR(255) NOT NULL,
        previous VARCHAR(16) NOT NULL, provider VARCHAR(16) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE smtp_attempts (
        id UUID PRIMARY KEY, delivery_id UUID NOT NULL REFERENCES recipient_deliveries(id),
        attempt INTEGER NOT NULL, provider VARCHAR(16) NOT NULL, state VARCHAR(16) NOT NULL,
        response_code INTEGER, response TEXT, error_code VARCHAR(100),
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ,
        CONSTRAINT uq_smtp_attempt UNIQUE(delivery_id,attempt),
        CONSTRAINT ck_smtp_attempt_provider CHECK (provider IN ('timeweb','brevo')),
        CONSTRAINT ck_smtp_attempt_state CHECK (state IN ('in_flight','accepted','failed','uncertain')))''')
    op.create_index('idx_smtp_quota', 'smtp_attempts', ['provider', 'started_at'])
    # Existing ACKs predate provider selection; their transport is the unchanged Timeweb.
    op.execute("UPDATE recipient_deliveries SET smtp_provider='timeweb' WHERE attempts>0")

def downgrade():
    raise RuntimeError('SMTP history must be retained; switch to Timeweb without schema downgrade')
