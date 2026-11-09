"""Reliable revisions, recipient ledger, outbox and scoped IMAP identity.
Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '0006'
down_revision = '0005'
branch_labels = depends_on = None

IMAP_COLUMNS = ['imap_account','imap_mailbox','imap_uidvalidity','imap_uid','ingest_state','part_manifest','raw_mime_path']
NEW_TABLES = ['processing_revisions','delivery_intents','recipient_deliveries','outbox_events','mailbox_checkpoints']


def upgrade():
    for column in [
        sa.Column('imap_account',sa.String(255)), sa.Column('imap_mailbox',sa.String(255)),
        sa.Column('imap_uidvalidity',sa.BigInteger()), sa.Column('imap_uid',sa.BigInteger()),
        sa.Column('ingest_state',sa.String(32)), sa.Column('part_manifest',JSONB()),
        sa.Column('raw_mime_path',sa.Text())]:
        op.add_column('incoming_messages',column)
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints('incoming_messages'):
        if constraint['column_names']==['provider_message_id']:
            op.drop_constraint(constraint['name'],'incoming_messages',type_='unique')
    op.alter_column('incoming_messages','provider_message_id',nullable=True,existing_type=sa.String(255))
    op.create_index('uq_legacy_message_provider_id','incoming_messages',['provider_message_id'],unique=True,
                    postgresql_where=sa.text('imap_account IS NULL'))
    op.create_unique_constraint('uq_message_imap_identity','incoming_messages',
                                ['imap_account','imap_mailbox','imap_uidvalidity','imap_uid'])
    op.execute("CREATE TABLE processing_revisions (\n\tid UUID NOT NULL, \n\tattachment_id UUID NOT NULL, \n\trequest_key VARCHAR(255) NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tstate VARCHAR(32) NOT NULL, \n\tcontent_sha256 VARCHAR(64) NOT NULL, \n\tfile_path TEXT NOT NULL, \n\tcalculator_number VARCHAR(50), \n\tobject_id UUID, \n\tvalidation_result JSONB, \n\treason TEXT, \n\tparser_version VARCHAR(100), \n\tvalidation_attempts INTEGER DEFAULT 0 NOT NULL, \n\towner VARCHAR(255), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_revision_state CHECK (state IN ('received','extracting','validated','rejected','review_required','technical_error','discarded_inactive')), \n\tFOREIGN KEY(attachment_id) REFERENCES attachments (id), \n\tUNIQUE (request_key), \n\tFOREIGN KEY(object_id) REFERENCES objects (id)\n)")
    op.execute('CREATE INDEX ix_processing_revisions_attachment_id ON processing_revisions (attachment_id)')
    op.execute("CREATE TABLE delivery_intents (\n\tid UUID NOT NULL, \n\trevision_id UUID NOT NULL, \n\trequest_key VARCHAR(255) NOT NULL, \n\tpayload_hash VARCHAR(64) NOT NULL, \n\tkind VARCHAR(32) NOT NULL, \n\tpayload JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_intent_kind CHECK (kind IN ('automatic','manual_resend','admin_notification')), \n\tFOREIGN KEY(revision_id) REFERENCES processing_revisions (id), \n\tUNIQUE (request_key)\n)")
    op.execute('CREATE INDEX ix_delivery_intents_revision_id ON delivery_intents (revision_id)')
    op.execute("CREATE TABLE recipient_deliveries (\n\tid UUID NOT NULL, \n\tintent_id UUID NOT NULL, \n\trecipient VARCHAR(320) NOT NULL, \n\toriginal_address TEXT NOT NULL, \n\trole VARCHAR(16) NOT NULL, \n\tstate VARCHAR(16) DEFAULT 'pending' NOT NULL, \n\tattempts INTEGER DEFAULT 0 NOT NULL, \n\terror TEXT, \n\terror_code VARCHAR(100), \n\tmessage_id VARCHAR(255) NOT NULL, \n\taccepted_at TIMESTAMP WITH TIME ZONE, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\towner VARCHAR(255), \n\tretry_at TIMESTAMP WITH TIME ZONE, \n\tmime_date VARCHAR(100) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_delivery_intent_recipient UNIQUE (intent_id, recipient), \n\tCONSTRAINT ck_delivery_role CHECK (role IN ('client','admin')), \n\tCONSTRAINT ck_delivery_state CHECK (state IN ('pending','in_flight','accepted','failed','uncertain','cancelled')), \n\tCONSTRAINT ck_delivery_attempts CHECK (attempts >= 0), \n\tFOREIGN KEY(intent_id) REFERENCES delivery_intents (id)\n)")
    op.execute('CREATE INDEX idx_delivery_lease ON recipient_deliveries (state, lease_until)')
    op.execute('CREATE INDEX idx_delivery_ready ON recipient_deliveries (state, retry_at)')
    op.execute('CREATE TABLE outbox_events (\n\tid UUID NOT NULL, \n\ttopic VARCHAR(100) NOT NULL, \n\tpayload JSONB NOT NULL, \n\tavailable_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tpublished_at TIMESTAMP WITH TIME ZONE, \n\tattempts INTEGER DEFAULT 0 NOT NULL, \n\tlast_error TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id)\n)')
    op.execute('CREATE INDEX idx_outbox_pending ON outbox_events (published_at, available_at)')
    op.execute('CREATE TABLE mailbox_checkpoints (\n\tid UUID NOT NULL, \n\taccount VARCHAR(255) NOT NULL, \n\tmailbox VARCHAR(255) NOT NULL, \n\tuidvalidity BIGINT NOT NULL, \n\tlast_uid BIGINT DEFAULT 0 NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_mailbox_account_folder UNIQUE (account, mailbox)\n)')

    op.add_column('attachments',sa.Column('current_revision_id',UUID(as_uuid=True),nullable=True))
    op.create_foreign_key('fk_attachments_current_revision','attachments','processing_revisions',
                          ['current_revision_id'],['id'])


def downgrade():
    bind=op.get_bind()
    for name in NEW_TABLES:
        if bind.execute(sa.text('SELECT EXISTS (SELECT 1 FROM '+name+')')).scalar():
            raise RuntimeError('Cannot remove reliable delivery history; disable feature and retain schema')
    if bind.execute(sa.text('SELECT EXISTS (SELECT 1 FROM attachments WHERE current_revision_id IS NOT NULL)')).scalar():
        raise RuntimeError('Current revisions require reconciliation before downgrade')
    if bind.execute(sa.text('SELECT EXISTS (SELECT 1 FROM incoming_messages WHERE imap_account IS NOT NULL)')).scalar():
        raise RuntimeError('Scoped IMAP history cannot be downgraded automatically')
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM incoming_messages WHERE provider_message_id IS NULL) OR EXISTS (SELECT 1 FROM incoming_messages GROUP BY provider_message_id HAVING count(*)>1)")).scalar():
        raise RuntimeError('RFC Message-ID metadata cannot satisfy legacy global uniqueness')
    op.drop_constraint('fk_attachments_current_revision','attachments',type_='foreignkey')
    op.drop_column('attachments','current_revision_id')
    op.drop_constraint('uq_message_imap_identity','incoming_messages',type_='unique')
    op.drop_index('uq_legacy_message_provider_id',table_name='incoming_messages')
    for name in reversed(IMAP_COLUMNS):
        op.drop_column('incoming_messages',name)
    for name in ['recipient_deliveries','delivery_intents','processing_revisions','outbox_events','mailbox_checkpoints']:
        op.drop_table(name)
    op.alter_column('incoming_messages','provider_message_id',nullable=False,existing_type=sa.String(255))
    op.create_unique_constraint('incoming_messages_provider_message_id_key','incoming_messages',['provider_message_id'])
