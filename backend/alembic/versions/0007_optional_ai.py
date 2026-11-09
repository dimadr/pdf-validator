"""Optional AI settings and durable reservations; immutable DDL, initial OFF."""
from alembic import op
import sqlalchemy as sa
revision = '0007'
down_revision = '0006'
branch_labels = depends_on = None

def upgrade():
    op.execute('\nCREATE TABLE ai_control (\n\tid SERIAL NOT NULL, \n\tenabled BOOLEAN DEFAULT false NOT NULL, \n\tgeneration INTEGER DEFAULT 0 NOT NULL, \n\tenabled_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_ai_singleton CHECK (id = 1)\n)\n\n')
    op.execute('\nCREATE TABLE ai_setting_audit (\n\tid UUID NOT NULL, \n\tactor VARCHAR(255) NOT NULL, \n\tprevious BOOLEAN NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n')
    op.execute("\nCREATE TABLE ai_requests (\n\tid UUID NOT NULL, \n\trevision_id UUID NOT NULL, \n\tcache_key VARCHAR(64) NOT NULL, \n\tcontent_sha256 VARCHAR(64) NOT NULL, \n\tmodel VARCHAR(150) NOT NULL, \n\tparser_engine VARCHAR(32) NOT NULL, \n\tprompt_version VARCHAR(100) NOT NULL, \n\tstate VARCHAR(32) NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\treason TEXT NOT NULL, \n\tresult JSONB, \n\tusage JSONB, \n\terror_code VARCHAR(100), \n\telapsed_ms INTEGER, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_ai_request_state CHECK (state IN ('reserved','complete','failed','unknown')), \n\tFOREIGN KEY(revision_id) REFERENCES processing_revisions (id), \n\tUNIQUE (cache_key)\n)\n\n")
    op.execute('CREATE INDEX ix_ai_requests_created_at ON ai_requests (created_at)')
    op.execute('CREATE INDEX ix_ai_requests_revision_id ON ai_requests (revision_id)')
    op.execute('INSERT INTO ai_control (id,enabled,generation) VALUES (1,false,0)')

def downgrade():
    bind=op.get_bind()
    if bind.execute(sa.text('SELECT EXISTS(SELECT 1 FROM ai_requests) OR EXISTS(SELECT 1 FROM ai_setting_audit)')).scalar():
        raise RuntimeError('AI provenance must be retained; use image rollback without schema downgrade')
    for name in ('ai_requests','ai_setting_audit','ai_control'):op.drop_table(name)
