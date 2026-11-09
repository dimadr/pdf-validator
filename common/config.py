import os
from sqlalchemy import inspect

class PipelineUnavailable(RuntimeError):
    pass

def enabled():
    return os.getenv('PIPELINE_V2_ENABLED', 'false').lower() in ('1', 'true', 'yes')

def require_schema(db):
    if not enabled():
        raise PipelineUnavailable('Reliable pipeline is disabled')
    required = {'processing_revisions', 'delivery_intents', 'recipient_deliveries', 'outbox_events', 'mailbox_checkpoints'}
    if not required.issubset(set(inspect(db.connection()).get_table_names())):
        raise PipelineUnavailable('Reliable pipeline migration is required')

def legacy_blocked(db, attachment):
    # Flag rollback must never reactivate legacy sending for a v2-managed document.
    return enabled() or attachment.current_revision_id is not None
