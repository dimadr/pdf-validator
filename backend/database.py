from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import inspect, text
from fastapi import HTTPException
from common.models import Base
import os

DATABASE_URL = os.environ['DATABASE_URL']

engine = create_engine(DATABASE_URL, pool_pre_ping=True, hide_parameters=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def require_backend_schema(db):
    if db.info.get('backend_schema_checked'):
        return
    inspector=inspect(db.connection())
    required={'processing_revisions','delivery_intents','recipient_deliveries','outbox_events','mailbox_checkpoints','ai_control','ai_setting_audit','ai_requests','smtp_attempts','smtp_provider_audit','mailbox_actions','brevo_delivery_events','brevo_poll_checkpoints'}
    if not required.issubset(set(inspector.get_table_names())):
        raise HTTPException(status_code=503,detail='Database migration 0010 is required')
    if db.execute(text('SELECT version_num FROM alembic_version')).scalar()!='0010':
        raise HTTPException(status_code=503,detail='Database migration 0010 is required')
    attachment_columns={c['name'] for c in inspector.get_columns('attachments')}
    incoming_columns={c['name'] for c in inspector.get_columns('incoming_messages')}
    if 'current_revision_id' not in attachment_columns or not {'imap_account','imap_uid','part_manifest'}.issubset(incoming_columns):
        raise HTTPException(status_code=503,detail='Database schema is incomplete')
    db.info['backend_schema_checked']=True


def get_db():
    db = SessionLocal()
    try:
        require_backend_schema(db)
        yield db
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        from sqlalchemy.exc import SQLAlchemyError
        if isinstance(exc,SQLAlchemyError):
            raise HTTPException(status_code=503,detail='Database is unavailable') from exc
        raise
    finally:
        db.close()
