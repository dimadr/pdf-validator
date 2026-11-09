"""Explicit test-worker --include only. Never listed in production Celery includes."""
import os,time,uuid
import redis
from celery_app import celery_app
from database import SessionLocal
from common.delivery import deliver_recipient
from common.smtp_transport import SMTPTransport

@celery_app.task(name='test.delivery_fault',acks_late=True,reject_on_worker_lost=True)
def fault_delivery(delivery_id,key,phase):
    if os.getenv('SMTP_TEST_MODE')!='true' or not key.startswith('rc_test_'):
        raise RuntimeError('Fault tasks require an isolated test runtime')
    client=redis.Redis.from_url(os.environ['TEST_REDIS_URL'])
    def stop_once(*a):
        if client.set(key,b'blocked',nx=True):
            while not client.get(key+':release'):time.sleep(.1)
    if phase=='before_claim':stop_once()
    return deliver_recipient(SessionLocal,uuid.UUID(delivery_id),SMTPTransport(),before_smtp=stop_once if phase=='after_claim' else None)
@celery_app.task(name='testing.ai_status')
def ai_status():
    import os,time
    from common.ai_settings import status
    from database import SessionLocal
    time.sleep(1)
    with SessionLocal() as db:return {'pid':os.getpid(),**status(db)}
