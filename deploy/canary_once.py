"""Prepare one NEW controlled canary through the existing V2 outbox. No resend."""
import base64,datetime,hashlib,json,os,re,sys,uuid,time
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

def scope_guard():
    database=os.environ['DATABASE_URL']
    test=(os.getenv('PROJECT1_TEST_SCOPE')=='production-decision-copy' and
        urlsplit(database).path.startswith('/project1_test_'))
    if not test:
        if datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date()<datetime.date(2026,11,2):
            raise RuntimeError('STOP: no production changes before 02.11.2026')
        if os.getenv('PROJECT1_PRODUCTION_APPROVAL')!='GO: production deployment':
            raise RuntimeError('STOP: separate owner production GO is required')
    return database,test

def prepare(payload):
    database,test=scope_guard()
    from sqlalchemy import create_engine,select,func
    from sqlalchemy.orm import sessionmaker
    from common.models import Object,EmailSource,IncomingMessage,Attachment,ProcessingRevision,AIControl,OutboxEvent
    from common.ingest import BlobStore
    from common.service import request_reprocess
    from common.config import require_schema
    recipient=payload['recipient'];serial=payload['serial'];key=payload['request_key']
    if test:
        if not recipient.endswith('@example.invalid'):raise RuntimeError('Test canary requires a sink recipient')
    elif recipient not in ('dv@example.com','np@example.com'):raise RuntimeError('STOP: select an explicitly approved owner test mailbox')
    if not serial.startswith('990000-CANARY-') or not re.fullmatch(r'[0-9A-Za-z-]{4,50}',serial):raise RuntimeError('Canary requires a reserved full synthetic serial')
    if not key.startswith('controlled-canary:') or len(key)>200:raise RuntimeError('Explicit canary idempotency key required')
    content=base64.b64decode(payload['pdf_base64'],validate=True);digest=hashlib.sha256(content).hexdigest()
    if digest!=payload['sha256'] or not content.startswith(b'%PDF'):raise RuntimeError('Canary bytes/hash do not match')
    engine=create_engine(database,hide_parameters=True);factory=sessionmaker(bind=engine,expire_on_commit=False)
    try:
        with factory() as db:
            with db.begin():
                require_schema(db)
                control=db.get(AIControl,1)
                if control is None or control.enabled:raise RuntimeError('STOP: AI must be OFF')
                db.execute(select(func.pg_advisory_xact_lock(713031103)))
                previous=db.scalar(select(ProcessingRevision).where(ProcessingRevision.request_key==key))
                if previous:
                    obj=db.get(Object,previous.object_id) if previous.object_id else db.scalar(select(Object).where(Object.calculator_number==serial))
                    if previous.content_sha256!=digest or obj is None or obj.email!=recipient or obj.calculator_number!=serial:
                        raise RuntimeError('Canary key conflicts with existing content/route')
                    return {'state':'existing','revision_id':str(previous.id),'new_send_planned':False,'sha256':digest}
                if db.scalar(select(func.count()).select_from(ProcessingRevision)) or db.scalar(select(func.count()).select_from(OutboxEvent)):
                    raise RuntimeError('STOP: first canary requires a fresh V2 ledger and paused producers')
                if db.scalar(select(Attachment.id).where(Attachment.file_sha256==digest)):
                    raise RuntimeError('STOP: content already exists; historical resend is forbidden')
                if db.scalar(select(Object.id).where(Object.calculator_number==serial)):
                    raise RuntimeError('STOP: serial already belongs to an object; do not modify it')
                obj=Object(id=uuid.uuid4(),name='Project1 controlled canary',name_norm='project1 controlled canary '+key,
                    calculator_number=serial,email=recipient,is_active=True)
                source=EmailSource(id=uuid.uuid4(),email='canary-'+uuid.uuid4().hex+'@example.invalid',name='Manual owner canary',is_active=False)
                db.add_all([obj,source]);db.flush()
                message=IncomingMessage(id=uuid.uuid4(),provider_message_id='<'+uuid.uuid4().hex+'@manual-canary.invalid>',
                    source_id=source.id,from_email=source.email,subject='Owner-authorized synthetic canary',status='new')
                db.add(message);db.flush()
                path,_=BlobStore(os.getenv('UPLOAD_DIR','/app/uploads')).put(content,'.pdf')
                attachment=Attachment(id=uuid.uuid4(),message_id=message.id,filename='controlled-canary.pdf',
                    file_path=path,file_sha256=digest,file_size=len(content),status='new')
                db.add(attachment);db.flush()
                revision=request_reprocess(db,attachment.id,key)
                return {'state':'prepared','revision_id':str(revision.id),'object_id':str(obj.id),'attachment_id':str(attachment.id),
                    'sha256':digest,'serial':serial,'recipient':recipient,'origin':'manual canary; no fabricated IMAP UID'}
    finally:engine.dispose()

def wait_for_acceptance(prepared,seconds=60):
    """Bounded use of the existing dispatcher while the producer is paused."""
    database,_=scope_guard()
    if prepared.get('state') not in ('prepared','existing'):raise RuntimeError('No prepared canary')
    from sqlalchemy import create_engine,select,func
    from sqlalchemy.orm import sessionmaker
    from common.models import ProcessingRevision,RecipientDelivery,DeliveryIntent,AIControl
    from common.delivery import dispatch_outbox
    from celery import Celery
    celery_app=Celery('controlled-canary',broker=os.environ['REDIS_URL'])
    celery_app.conf.update(task_default_queue=os.getenv('CELERY_QUEUE','celery'),
        broker_transport_options={'global_keyprefix':os.getenv('CELERY_REDIS_KEY_PREFIX','')})
    engine=create_engine(database,hide_parameters=True);factory=sessionmaker(bind=engine,expire_on_commit=False)
    deadline=time.monotonic()+seconds
    try:
        while time.monotonic()<deadline:
            with factory() as db:
                revision=db.get(ProcessingRevision,uuid.UUID(prepared['revision_id']))
                if revision is None or not revision.request_key.startswith('controlled-canary:'):raise RuntimeError('Unknown canary revision')
                if db.scalar(select(func.count()).select_from(ProcessingRevision))!=1 or db.get(AIControl,1).enabled:
                    raise RuntimeError('STOP: canary isolation/AI OFF condition changed')
                rows=list(db.scalars(select(RecipientDelivery).join(DeliveryIntent,DeliveryIntent.id==RecipientDelivery.intent_id).where(DeliveryIntent.revision_id==revision.id)))
                if any(r.state=='uncertain' for r in rows):raise RuntimeError('STOP: uncertain SMTP, do not resend')
                if any(r.state in ('failed','cancelled') for r in rows):raise RuntimeError('STOP: canary delivery failed; no automatic resend')
                if revision.state in ('rejected','review_required','technical_error','discarded_inactive'):raise RuntimeError('STOP: canary was not validated')
                if len(rows)==1 and rows[0].role=='client' and rows[0].state=='accepted':
                    row=rows[0]
                    return {'state':'accepted','revision_id':str(revision.id),'serial':revision.calculator_number,
                        'object_id':str(revision.object_id),'recipient':row.recipient,'delivery_id':str(row.id),'message_id':row.message_id,
                        'accepted_at':row.accepted_at.isoformat(),'attempts':row.attempts}
            dispatch_outbox(factory,lambda topic,payload:celery_app.send_task(topic,kwargs=payload))
            time.sleep(.5)
        raise RuntimeError('STOP: canary timed out; retain current data and check outcomes before any retry')
    finally:engine.dispose()

if __name__=='__main__':
    try:
        data=json.load(sys.stdin)
        print(json.dumps(wait_for_acceptance(data) if '--wait' in sys.argv else prepare(data)))
    except Exception as error:print(json.dumps({'state':'STOP','reason':str(error)}));sys.exit(2)
