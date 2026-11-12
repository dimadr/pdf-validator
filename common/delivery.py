"""Transactional delivery intents, outbox, atomic claims and ambiguous SMTP outcomes."""
import hashlib,json,uuid,os
from datetime import datetime,timezone,timedelta
from email.utils import format_datetime
from sqlalchemy import select,update,func
from common.models import Attachment,Object,ProcessingRevision,DeliveryIntent,RecipientDelivery,OutboxEvent,SMTPAttempt

def now():
    return datetime.now(timezone.utc)

class DefinitiveFailure(Exception):
    def __init__(self, message, retryable=True, code='smtp_rejected', response_code=None, response=None):
        super().__init__(message);self.retryable=retryable;self.code=code
        self.response_code=response_code;self.response=response

class AmbiguousFailure(Exception):
    pass

class ServiceConflict(ValueError):
    pass

def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def normalized(address):
    address=address.strip()
    if any(c in address for c in '\r\n<>();:"\\[]') or address.count('@')!=1:
        raise ServiceConflict('Invalid recipient address')
    local,domain=address.rsplit('@',1)
    if not local or not domain or any(c.isspace() for c in address):
        raise ServiceConflict('Invalid recipient address')
    # Do not case-fold a possibly case-sensitive local part.
    return local+'@'+domain.lower()

def enqueue(db,topic,payload,available_at=None):
    event=OutboxEvent(id=uuid.uuid4(),topic=topic,payload=payload,available_at=available_at or now(),attempts=0)
    db.add(event)
    return event

def plan_intent(db,revision,recipients,request_key,kind='automatic',payload=None):
    """Caller owns the surrounding transaction; never publishes or sends here."""
    if not request_key or len(request_key)>200:
        raise ServiceConflict('Idempotency key required (maximum 200 characters)')
    recipients=sorted(set((normalized(address),role) for address,role in recipients))
    if not recipients or any(role not in ('client','admin') for _,role in recipients):
        raise ServiceConflict('No valid resolved recipients')
    if len({a for a,_ in recipients})!=len(recipients):
        raise ServiceConflict('Recipient has conflicting roles')
    if any(role=='client' for _,role in recipients) and revision.state!='validated':
        raise ServiceConflict('Client delivery requires validated content')
    if payload is None:
        from common.mail_content import client_payload,review_payload
        attachment=db.get(Attachment,revision.attachment_id);obj=db.get(Object,revision.object_id) if revision.object_id else None
        payload=client_payload(attachment,obj) if any(role=='client' for _,role in recipients) else review_payload(attachment,obj,revision.state,revision.reason,revision.validation_result)
    body=payload
    digest=fingerprint({'revision':str(revision.id),'content':revision.content_sha256,'kind':kind,'recipients':recipients,'payload':body})
    # Serialize the request key even when two different documents race for it.
    db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(hashlib.sha256(request_key.encode()).digest()[:8],'big',signed=True))))
    existing=db.scalar(select(DeliveryIntent).where(DeliveryIntent.request_key==request_key))
    if existing:
        if existing.payload_hash!=digest:
            raise ServiceConflict('Idempotency key reused with different payload')
        return existing
    intent=DeliveryIntent(id=uuid.uuid4(),revision_id=revision.id,request_key=request_key,payload_hash=digest,kind=kind,payload=body)
    db.add(intent);db.flush()
    for address,role in recipients:
        delivery_id=uuid.uuid4()
        delivery=RecipientDelivery(id=delivery_id,intent_id=intent.id,recipient=address,original_address=address,role=role,
            state='pending',attempts=0,message_id='<'+str(delivery_id)+'@'+os.getenv('MESSAGE_ID_DOMAIN','processor.invalid')+'>',
            mime_date=format_datetime(now()))
        db.add(delivery)
        enqueue(db,'pipeline.deliver_recipient',{'delivery_id':str(delivery_id)})
    return intent

def dispatch_outbox(session_factory,publish,limit=100,after_publish=None):
    count=0
    for _ in range(limit):
        with session_factory() as db:
            event_id=None
            try:
                with db.begin():
                    event=db.scalar(select(OutboxEvent).where(OutboxEvent.published_at.is_(None),OutboxEvent.available_at<=now())
                        .order_by(OutboxEvent.created_at,OutboxEvent.id).with_for_update(skip_locked=True).limit(1))
                    if event is None:return count
                    event_id=event.id
                    publish(event.topic,dict(event.payload))
                    if after_publish:after_publish(event)
                    event.published_at=now();event.attempts+=1;event.last_error=None
                count+=1
            except Exception:
                db.rollback()
                if event_id is not None:
                    try:
                        with db.begin():
                            db.execute(update(OutboxEvent).where(OutboxEvent.id==event_id,OutboxEvent.published_at.is_(None))
                                .values(attempts=OutboxEvent.attempts+1,last_error='Broker publish/result commit failed; retained for retry'))
                    except Exception:db.rollback()
                # The durable unpublished row survives. Avoid immediate hot-loop on a failed broker.
                return count
    return count

def _inactive_blocks_delivery(revision,obj,role):
    return (obj is not None and not obj.is_active
            and (role!='admin' or not str(revision.file_path or '').lower().endswith('.xls')))

def _route_error(attachment,revision,obj,role):
    if attachment.status=='archived':return 'attachment_archived'
    if attachment.current_revision_id!=revision.id or revision.state in ('received','extracting','technical_error','discarded_inactive'):
        return 'stale_or_unavailable_revision'
    if _inactive_blocks_delivery(revision,obj,role):return 'object_inactive'
    if role=='client' and (revision.state!='validated' or obj is None):return 'client_route_not_validated'
    if role=='client' and obj.calculator_number!=revision.calculator_number:return 'object_binding_changed'

def _claim(session_factory,delivery_id,owner,lease_seconds,max_attempts):
    with session_factory() as db:
        with db.begin():
            parent=db.execute(select(ProcessingRevision.attachment_id).join(DeliveryIntent,DeliveryIntent.revision_id==ProcessingRevision.id)
                .join(RecipientDelivery,RecipientDelivery.intent_id==DeliveryIntent.id).where(RecipientDelivery.id==delivery_id)).scalar_one_or_none()
            if parent is None:return None
            attachment=db.scalar(select(Attachment).where(Attachment.id==parent).with_for_update())
            delivery=db.scalar(select(RecipientDelivery).where(RecipientDelivery.id==delivery_id).with_for_update())
            intent=db.get(DeliveryIntent,delivery.intent_id);revision=db.get(ProcessingRevision,intent.revision_id)
            if attachment.status=='archived':
                if delivery.state in ('pending','failed'):
                    delivery.state='cancelled';delivery.error_code='attachment_archived';delivery.retry_at=None
                return None
            if delivery.state not in ('pending','failed') or (delivery.attempts>=max_attempts and delivery.error_code!='smtp_quota'):return None
            if delivery.state=='failed' and delivery.retry_at is None:return None
            due=delivery.retry_at
            if due is not None and (due if due.tzinfo else due.replace(tzinfo=timezone.utc)) > now():return None
            if attachment.current_revision_id!=revision.id or revision.state in ('received','extracting','technical_error','discarded_inactive'):
                delivery.state='cancelled';delivery.error_code='stale_or_unavailable_revision';return None
            obj=db.scalar(select(Object).where(Object.id==revision.object_id).with_for_update()) if revision.object_id else None
            if _inactive_blocks_delivery(revision,obj,delivery.role):
                delivery.state='cancelled';delivery.error_code='object_inactive';return None
            if delivery.role=='client' and (revision.state!='validated' or obj is None):
                delivery.state='cancelled';delivery.error_code='client_route_not_validated';return None
            if delivery.role=='client' and obj.calculator_number!=revision.calculator_number:
                delivery.state='cancelled';delivery.error_code='object_binding_changed';return None
            ai=(revision.validation_result or {}).get('ai',{})
            if delivery.role=='client' and ai.get('state')=='used':
                from common.ai_settings import permitted
                if not permitted(db,generation=ai.get('generation'),lock=True):
                    delivery.state='cancelled';delivery.error_code='ai_disabled_before_delivery';return None
            from common.smtp_settings import selection,quota_used,quota_limit
            provider=selection(db,lock=True)
            if provider=='brevo' and quota_used(db)>=quota_limit():
                delivery.retry_at=now()+timedelta(minutes=15)
                delivery.error_code='smtp_quota';delivery.error='Brevo local rolling 24h limit reached; retained for retry'
                enqueue(db,'pipeline.deliver_recipient',{'delivery_id':str(delivery.id)},delivery.retry_at)
                return None
            delivery.state='in_flight';delivery.owner=owner;delivery.lease_until=now()+timedelta(seconds=lease_seconds)
            delivery.attempts+=1;delivery.error=None;delivery.error_code=None
            delivery.smtp_provider=provider;delivery.smtp_response_code=None;delivery.smtp_response=None
            attempt_id=uuid.uuid4()
            db.add(SMTPAttempt(id=attempt_id,delivery_id=delivery.id,attempt=delivery.attempts,provider=provider,state='in_flight'))
            return {'id':delivery.id,'owner':owner,'recipient':delivery.recipient,'message_id':delivery.message_id,
                'date':delivery.mime_date,'path':revision.file_path,'sha256':revision.content_sha256,'payload':dict(intent.payload),'attempt':delivery.attempts,
                'provider':provider,'attempt_id':attempt_id,
                'ai_generation':ai.get('generation') if delivery.role=='client' and ai.get('state')=='used' else None}

def _uncertain(session_factory,work,reason,response_code=None,response=None):
    try:
        with session_factory() as db:
            with db.begin():
                db.execute(update(RecipientDelivery).where(RecipientDelivery.id==work['id'],RecipientDelivery.owner==work['owner'],RecipientDelivery.state=='in_flight')
                    .values(state='uncertain',error=reason,error_code='smtp_result_commit_unknown',lease_until=None,
                        smtp_response_code=response_code,smtp_response=response))
                db.execute(update(SMTPAttempt).where(SMTPAttempt.id==work['attempt_id'],SMTPAttempt.state=='in_flight')
                    .values(state='uncertain',error_code='smtp_result_commit_unknown',finished_at=now(),response_code=response_code,response=response))
    except Exception:
        # A failed database cannot record certainty. Lease reconciliation is the durable fallback.
        pass

def deliver_recipient(session_factory,delivery_id,smtp_transport,owner=None,lease_seconds=120,max_attempts=3,before_result_commit=None,before_smtp=None):
    owner=str(owner or uuid.uuid4())
    work=_claim(session_factory,delivery_id,owner,lease_seconds,max_attempts)
    if work is None:return {'state':'skipped'}
    state,error,code,retryable='accepted',None,None,False
    response_code,response=None,None
    try:
        if before_smtp:before_smtp(work)
        # A suspended worker must not resume external work after its claim became uncertain.
        with session_factory() as fence:
            with fence.begin():
                parent=fence.execute(select(ProcessingRevision.attachment_id).join(DeliveryIntent,DeliveryIntent.revision_id==ProcessingRevision.id)
                    .join(RecipientDelivery,RecipientDelivery.intent_id==DeliveryIntent.id).where(RecipientDelivery.id==work['id'])).scalar_one()
                attachment=fence.scalar(select(Attachment).where(Attachment.id==parent).with_for_update())
                current=fence.scalar(select(RecipientDelivery).where(RecipientDelivery.id==work['id']).with_for_update())
                revision=fence.get(ProcessingRevision,fence.get(DeliveryIntent,current.intent_id).revision_id)
                obj=fence.scalar(select(Object).where(Object.id==revision.object_id).with_for_update()) if revision.object_id else None
                if current.state!='in_flight' or current.owner!=work['owner']:return {'state':'skipped','reason':'claim_expired_before_smtp'}
                route_error=_route_error(attachment,revision,obj,current.role)
                if route_error:
                    current.state='cancelled';current.error_code=route_error;current.lease_until=None;current.retry_at=None
                    fence.execute(update(SMTPAttempt).where(SMTPAttempt.id==work['attempt_id']).values(state='failed',error_code=route_error,finished_at=now()))
                    return {'state':'cancelled','reason':route_error}
                if work.get('ai_generation') is not None:
                    from common.ai_settings import permitted
                    if not permitted(fence,generation=work['ai_generation'],lock=True):
                        fence.execute(update(RecipientDelivery).where(RecipientDelivery.id==work['id'],RecipientDelivery.owner==work['owner'],
                            RecipientDelivery.state=='in_flight').values(state='cancelled',error_code='ai_disabled_before_smtp',lease_until=None))
                        fence.execute(update(SMTPAttempt).where(SMTPAttempt.id==work['attempt_id'])
                            .values(state='failed',error_code='ai_disabled_before_smtp',finished_at=now()))
                        return {'state':'cancelled','reason':'ai_disabled_before_smtp'}
                valid=fence.execute(update(RecipientDelivery).where(RecipientDelivery.id==work['id'],RecipientDelivery.owner==work['owner'],
                    RecipientDelivery.state=='in_flight',RecipientDelivery.lease_until>now())
                    .values(lease_until=now()+timedelta(seconds=lease_seconds))).rowcount
                if valid!=1:return {'state':'skipped','reason':'claim_expired_before_smtp'}
        # Verify the immutable bytes used for this processing revision before every external effect.
        import pathlib
        path=pathlib.Path(work['path'] or '')
        content=path.read_bytes() if path.is_file() else None
        if content is None or hashlib.sha256(content).hexdigest()!=work['sha256']:
            raise DefinitiveFailure('Content missing or integrity check failed',False,'content_integrity')
        work['content']=content
        result=smtp_transport.send(work) or {}
        response_code,response=result.get('code'),result.get('response')
    except DefinitiveFailure as exc:
        state,error,code,retryable='failed',str(exc),exc.code,exc.retryable
        response_code,response=exc.response_code,exc.response
    except AmbiguousFailure:
        state,error,code='uncertain','SMTP acceptance outcome unknown','smtp_ambiguous'
    except Exception:
        state,error,code='uncertain','Worker failed after claim; outcome unknown','worker_ambiguous'
    try:
        with session_factory() as db:
            with db.begin():
                delivery=db.scalar(select(RecipientDelivery).where(RecipientDelivery.id==work['id']).with_for_update())
                if delivery.state!='in_flight' or delivery.owner!=work['owner']:
                    raise ServiceConflict('Stale claim cannot commit an outcome')
                delivery.state=state;delivery.error=error;delivery.error_code=code;delivery.lease_until=None;delivery.retry_at=None
                delivery.smtp_response_code=response_code;delivery.smtp_response=response
                attempt=db.get(SMTPAttempt,work['attempt_id'])
                attempt.state=state;attempt.response_code=response_code;attempt.response=response;attempt.error_code=code;attempt.finished_at=now()
                if state=='accepted':delivery.accepted_at=now()
                if state=='failed' and retryable and (delivery.attempts<max_attempts or code=='smtp_quota'):
                    delivery.retry_at=now()+timedelta(seconds=900 if code=='smtp_quota' else 60*2**min(delivery.attempts-1,6))
                    enqueue(db,'pipeline.deliver_recipient',{'delivery_id':str(delivery.id)},delivery.retry_at)
                if before_result_commit:before_result_commit(db)
    except Exception:
        _uncertain(session_factory,work,'SMTP attempt finished but PostgreSQL result commit failed',response_code,response)
        return {'state':'uncertain'}
    return {'state':state}

def reconcile(session_factory):
    """Never automatically resend an expired claim; restore only demonstrably unclaimed work."""
    with session_factory() as db:
        with db.begin():
            expired_ids=list(db.scalars(update(RecipientDelivery).where(RecipientDelivery.state=='in_flight',RecipientDelivery.lease_until<now())
                .values(state='uncertain',error='Claim expired; SMTP outcome unknown',error_code='lease_expired',lease_until=None).returning(RecipientDelivery.id)))
            expired=len(expired_ids)
            if expired_ids:
                db.execute(update(SMTPAttempt).where(SMTPAttempt.delivery_id.in_(expired_ids),SMTPAttempt.state=='in_flight')
                    .values(state='uncertain',error_code='lease_expired',finished_at=now()))
            abandoned=list(db.execute(select(ProcessingRevision.id,ProcessingRevision.attachment_id).where(
                ProcessingRevision.state=='extracting',ProcessingRevision.lease_until<now())).all())
            for revision_id,attachment_id in abandoned:
                attachment=db.scalar(select(Attachment).where(Attachment.id==attachment_id).with_for_update())
                if attachment.status=='archived':continue
                revision=db.scalar(select(ProcessingRevision).where(ProcessingRevision.id==revision_id).with_for_update(skip_locked=True))
                if revision is None or revision.state!='extracting':continue
                revision.state='technical_error';revision.reason='validation_worker_lost';revision.lease_until=None;revision.owner=None
                if attachment.current_revision_id==revision.id and revision.validation_attempts<3:
                    enqueue(db,'pipeline.validate_revision',{'revision_id':str(revision.id)},now()+timedelta(seconds=60*2**max(0,revision.validation_attempts-1)))
            # Broker early loss/redelivery: outbox republishes only recipient work that never became claimed.
            stale=db.scalars(select(OutboxEvent).where(OutboxEvent.published_at<now()-timedelta(minutes=5)).with_for_update(skip_locked=True)).all()
            reset=0
            for event in stale:
                if event.topic=='pipeline.deliver_recipient':
                    item=db.get(RecipientDelivery,uuid.UUID(event.payload['delivery_id']))
                    eligible=item is not None and (item.state=='pending' or item.state=='failed' and item.retry_at is not None) and (item.attempts<3 or item.error_code=='smtp_quota')
                elif event.topic=='pipeline.validate_revision':
                    item=db.get(ProcessingRevision,uuid.UUID(event.payload['revision_id']))
                    eligible=item is not None and item.state in ('received','technical_error') and item.validation_attempts<3
                elif event.topic=='pipeline.discard_content':
                    item=db.get(ProcessingRevision,uuid.UUID(event.payload['revision_id']))
                    eligible=item is not None and item.state=='discarded_inactive' and not (item.validation_result or {}).get('content_deleted')
                else:eligible=False
                if eligible:
                    revision=item if event.topic!='pipeline.deliver_recipient' else db.get(ProcessingRevision,db.get(DeliveryIntent,item.intent_id).revision_id)
                    attachment=db.get(Attachment,revision.attachment_id)
                    if attachment.status=='archived':continue
                    event.published_at=None;reset+=1
            return {'uncertain':expired,'republish':reset}
