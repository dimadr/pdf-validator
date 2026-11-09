"""Revision-aware extraction, exact object routing and atomic validation/delivery planning."""
import os,uuid,hashlib,pathlib
from datetime import timedelta
from sqlalchemy import select
from common.models import Attachment,Object,ProcessingRevision,DeliveryIntent
from common.config import require_schema
from common.delivery import enqueue,plan_intent,now,normalized,ServiceConflict

def validate_revision(session_factory,revision_id,analyzer=None,owner=None,lease_seconds=1800,after_claim=None):
    if analyzer is None:
        from common.pdf_validation import analyze_pdf
        analyzer=analyze_pdf
    owner=str(owner or uuid.uuid4())
    with session_factory() as db:
        with db.begin():
            require_schema(db)
            parent=db.scalar(select(ProcessingRevision.attachment_id).where(ProcessingRevision.id==revision_id))
            if parent is None:return {'state':'missing'}
            attachment=db.scalar(select(Attachment).where(Attachment.id==parent).with_for_update())
            if attachment.status=='archived':return {'state':'archived'}
            revision=db.scalar(select(ProcessingRevision).where(ProcessingRevision.id==revision_id).with_for_update())
            if attachment.current_revision_id!=revision.id:return {'state':'stale'}
            if revision.state not in ('received','technical_error'):return {'state':'skipped'}
            if revision.validation_attempts>=3:return {'state':'technical_error','reason':'validation_retry_exhausted'}
            previous=dict(revision.validation_result or {})
            revision.validation_attempts+=1;revision.state='extracting';revision.owner=owner;revision.lease_until=now()+timedelta(seconds=lease_seconds)
            attempts=revision.validation_attempts
            revision.validation_result={**previous,'attempts':attempts}
            message=attachment.message
            context={'message_subject':message.subject if message else '', 'received_at':message.received_at if message else None}
            work={'path':revision.file_path,'sha256':revision.content_sha256,'parent':parent,'attempts':attempts,
                'operation':previous.get('operation','automatic'),'context':context}
    # Claim/attempt is durable before invoking a possibly crashing PDF parser.
    if after_claim:after_claim(work)
    path=pathlib.Path(work['path'] or '')
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=work['sha256']:
        result={'state':'technical_error','reason':'content_unavailable','identity_state':'missing','calculator_number':None,'terminal_error':True}
    else:
        try:result=dict(analyzer(str(path),context=work['context']))
        except Exception:result={'state':'technical_error','reason':'extraction_failed','identity_state':'missing','calculator_number':None}
    from common.ai_fallback import run_fallback
    result=run_fallback(session_factory,revision_id,result,work['context'])
    result['attempts']=attempts;result['operation']=work['operation']
    with session_factory() as db:
        try:
            with db.begin():
                require_schema(db)
                attachment=db.scalar(select(Attachment).where(Attachment.id==work['parent']).with_for_update())
                if attachment.status=='archived':return {'state':'archived'}
                revision=db.scalar(select(ProcessingRevision).where(ProcessingRevision.id==revision_id).with_for_update())
                if attachment.current_revision_id!=revision.id or revision.state!='extracting' or revision.owner!=owner:
                    return {'state':'stale'}
                revision.owner=None;revision.lease_until=None
                revision.validation_result=result;revision.parser_version=result.get('adapter_version','unknown')
                revision.calculator_number=result.get('calculator_number');revision.object_id=None
                state=result.get('state','technical_error');reason=result.get('reason')
                if result.get('ai',{}).get('state')=='used':
                    from common.ai_settings import permitted
                    if not permitted(db,generation=result['ai']['generation'],lock=True):
                        original=result['deterministic_result']
                        result={**original,'ai':{**result['ai'],'state':'failed','reason':'disabled_before_planning'}}
                        revision.calculator_number=result.get('calculator_number')
                        state=result['state'];reason=result.get('reason')
                if state not in ('validated','rejected','review_required','technical_error'):
                    state,reason='technical_error','invalid_analyzer_contract'
                obj=None
                if result.get('identity_state')!='unique' or not revision.calculator_number:
                    if state not in ('technical_error','rejected'):state,reason='review_required','identity_'+result.get('identity_state','missing')
                else:
                    obj=db.scalar(select(Object).where(Object.calculator_number==revision.calculator_number))
                    if obj is not None and not obj.is_active:
                        # The retained/discarded disposition is terminal even after later object activation.
                        revision.state='discarded_inactive';revision.reason='object_inactive';revision.object_id=obj.id;revision.finished_at=now()
                        result['disposition']='discarded_inactive'
                        # File cleanup is a separate idempotent outbox action after this commit.
                        enqueue(db,'pipeline.discard_content',{'revision_id':str(revision.id)})
                        return {'state':revision.state}
                    if obj is None and state=='validated':state,reason='review_required','exact_object_not_found'
                    elif obj is not None:revision.object_id=obj.id
                client_recipients=[]
                if state=='validated':
                    try:
                        client_recipients=[(normalized(r),'client') for r in (obj.email or '').split(',') if r.strip()]
                        if not client_recipients:raise ServiceConflict('No client recipients')
                    except ServiceConflict:state,reason='review_required','invalid_client_recipients'
                result['state']=state;result['reason']=reason;revision.validation_result=dict(result)
                revision.state=state;revision.reason=reason;revision.finished_at=now() if state!='technical_error' else None
                if state=='technical_error':
                    if result.get('terminal_error'):revision.validation_attempts=3
                    elif attempts<3:enqueue(db,'pipeline.validate_revision',{'revision_id':str(revision.id)},now()+timedelta(seconds=60*2**(attempts-1)))
                    return {'state':state}
                if state=='validated':
                    recipients=client_recipients
                    kind='manual_resend' if result['operation']=='manual_resend' else 'automatic'
                else:
                    recipients=[(r,'admin') for r in [os.getenv('ADMIN_EMAIL'),os.getenv('NOTIFY_EMAIL')] if r]
                    kind='admin_notification'
                if recipients:
                    from common.mail_content import client_payload,review_payload
                    payload=client_payload(attachment,obj) if state=='validated' else review_payload(attachment,obj,state,reason,result)
                    plan_intent(db,revision,recipients,'revision:'+str(revision.id)+':'+kind,kind,
                        payload)
                return {'state':state,'role':'client' if state=='validated' else 'admin'}
        except Exception:
            db.rollback()
            # The transaction (including state, intents and outbox) rolls back as a unit.
            raise

def discard_content(session_factory,revision_id):
    with session_factory() as db:
        with db.begin():
            require_schema(db)
            revision=db.scalar(select(ProcessingRevision).where(ProcessingRevision.id==revision_id).with_for_update())
            if revision is None or revision.state!='discarded_inactive':return {'state':'skipped'}
            path=pathlib.Path(revision.file_path or '')
            # Shared content may still be retained for another revision/document.
            retained=db.scalar(select(ProcessingRevision.id).join(Attachment,Attachment.current_revision_id==ProcessingRevision.id)
                .where(ProcessingRevision.file_path==revision.file_path,ProcessingRevision.state!='discarded_inactive',
                    ProcessingRevision.attachment_id!=revision.attachment_id).limit(1))
            if retained:return {'state':'retained_shared'}
            if path.is_file():
                if hashlib.sha256(path.read_bytes()).hexdigest()!=revision.content_sha256:return {'state':'integrity_conflict'}
                path.unlink()
            result=dict(revision.validation_result or {});result['content_deleted']=True;revision.validation_result=result
            return {'state':'discarded_inactive'}
