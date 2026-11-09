"""Durable IMAP move intents; never remove receipt or delivery history."""
from alembic import op
revision='0009'
down_revision='0008'
branch_labels=depends_on=None

def upgrade():
    op.execute('''CREATE TABLE mailbox_actions (
      id UUID PRIMARY KEY, account VARCHAR(255) NOT NULL, mailbox VARCHAR(255) NOT NULL,
      uidvalidity BIGINT NOT NULL, uid BIGINT NOT NULL, destination VARCHAR(255) NOT NULL,
      raw_path TEXT NOT NULL, raw_sha256 VARCHAR(64) NOT NULL, message_id VARCHAR(255),
      state VARCHAR(16) NOT NULL DEFAULT 'pending', error TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ,
      CONSTRAINT uq_mailbox_action UNIQUE(account,mailbox,uidvalidity,uid,destination),
      CONSTRAINT ck_mailbox_action_state CHECK(state IN ('pending','done','uncertain')))''')

def downgrade():
    raise RuntimeError('Mailbox move history must be retained; use an image rollback without a schema downgrade')
