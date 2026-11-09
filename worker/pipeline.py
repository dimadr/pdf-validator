"""Reliable Celery entrypoints. Production enablement requires the explicit migration gate."""
import uuid
from celery_app import celery_app
from database import SessionLocal
from common.config import enabled
from common.delivery import dispatch_outbox,deliver_recipient,reconcile
from common.pipeline import validate_revision as run_validation,discard_content as run_discard
from common.smtp_transport import SMTPTransport

@celery_app.task(name='pipeline.dispatch_outbox')
def dispatch():
    if not enabled():return {'state':'disabled'}
    return {'published':dispatch_outbox(SessionLocal,lambda topic,payload:celery_app.send_task(topic,kwargs=payload))}

@celery_app.task(name='pipeline.validate_revision',acks_late=True,reject_on_worker_lost=True)
def validate_revision(revision_id):
    if not enabled():return {'state':'disabled'}
    return run_validation(SessionLocal,uuid.UUID(revision_id))

@celery_app.task(name='pipeline.deliver_recipient',acks_late=True,reject_on_worker_lost=True)
def send_recipient(delivery_id):
    if not enabled():return {'state':'disabled'}
    return deliver_recipient(SessionLocal,uuid.UUID(delivery_id),SMTPTransport())

@celery_app.task(name='pipeline.reconcile')
def recovery():
    if not enabled():return {'state':'disabled'}
    from common.ingest import recover_incomplete,BlobStore
    import os
    result=reconcile(SessionLocal)
    result['ingest_recovered']=recover_incomplete(SessionLocal,BlobStore(os.getenv('UPLOAD_DIR','./uploads')))
    return result


@celery_app.task(name='pipeline.poll_brevo_events', soft_time_limit=14, time_limit=18)
def poll_brevo_events():
    if not enabled():
        return {'state': 'disabled'}
    from common.brevo_delivery import BrevoEventsClient, load_api_key, poll_brevo

    key = load_api_key()
    if not key:
        return {'state': 'unconfigured'}
    client = BrevoEventsClient(key)
    try:
        return poll_brevo(SessionLocal, client)
    finally:
        client.close()

@celery_app.task(name='pipeline.discard_content',acks_late=True,reject_on_worker_lost=True)
def discard_content(revision_id):
    if not enabled():return {'state':'disabled'}
    return run_discard(SessionLocal,uuid.UUID(revision_id))
