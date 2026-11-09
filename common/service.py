"""Application commands. Caller commits command state and outbox atomically."""
import uuid,hashlib,pathlib
from sqlalchemy import select,func,update,or_
from common.models import Attachment,Object,ProcessingRevision,DeliveryIntent,RecipientDelivery
from common.config import require_schema
from common.delivery import ServiceConflict,fingerprint,enqueue,plan_intent

def _request_lock(db,key):
    if not key or len(key)>200:raise ServiceConflict('Idempotency-Key is required (maximum 200 characters)')
    db.execute(select(func.pg_advisory_xact_lock(int.from_bytes(hashlib.sha256(key.encode()).digest()[:8],'big',signed=True))))

def _attachment(db,attachment_id,expected_revision_id):
    attachment=db.scalar(select(Attachment).where(Attachment.id==attachment_id).with_for_update())
    if attachment is None:raise ServiceConflict('Attachment not found')
    if attachment.status=='archived':raise ServiceConflict('Attachment is in the reversible trash; restore it before processing')
    if expected_revision_id is not None and str(attachment.current_revision_id)!=str(expected_revision_id):
        raise ServiceConflict('Processing revision changed')
    busy=db.scalar(select(RecipientDelivery.id).join(DeliveryIntent,RecipientDelivery.intent_id==DeliveryIntent.id)
        .join(ProcessingRevision,DeliveryIntent.revision_id==ProcessingRevision.id)
        .where(ProcessingRevision.attachment_id==attachment.id,or_(RecipientDelivery.state.in_(['in_flight','uncertain']),
            RecipientDelivery.error_code=='legacy_smtp_history_incomplete')).limit(1))
    if busy:raise ServiceConflict('Delivery outcome must be reconciled before a new operation')
    return attachment

def _content(attachment):
    path=pathlib.Path(attachment.file_path or '')
    if not path.is_file():raise ServiceConflict('Content is unavailable; recover verified bytes first')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=attachment.file_sha256:
        raise ServiceConflict('Content hash does not match immutable attachment identity')

def _create_revision(db,attachment,key,request_hash,operation):
    _content(attachment)
    current=db.get(ProcessingRevision,attachment.current_revision_id) if attachment.current_revision_id else None
    if current and current.state=='discarded_inactive':raise ServiceConflict('Inactive content was terminally discarded')
    revision=ProcessingRevision(id=uuid.uuid4(),attachment_id=attachment.id,request_key=key,request_hash=request_hash,
        state='received',file_path=attachment.file_path,content_sha256=attachment.file_sha256,
        validation_result={'operation':operation},calculator_number=None,object_id=None)
    db.add(revision);db.flush()
    old_intents=select(DeliveryIntent.id).join(ProcessingRevision,DeliveryIntent.revision_id==ProcessingRevision.id).where(ProcessingRevision.attachment_id==attachment.id)
    db.execute(update(RecipientDelivery).where(RecipientDelivery.intent_id.in_(old_intents),RecipientDelivery.state.in_(['pending','failed']))
        .values(state='cancelled',error_code='superseded_revision',retry_at=None))
    attachment.current_revision_id=revision.id
    enqueue(db,'pipeline.validate_revision',{'revision_id':str(revision.id)})
    return revision

def request_reprocess(db,attachment_id,request_key,expected_revision_id=None):
    require_schema(db);_request_lock(db,request_key)
    digest=fingerprint({'operation':'reprocess','attachment':str(attachment_id),'expected':str(expected_revision_id)})
    previous=db.scalar(select(ProcessingRevision).where(ProcessingRevision.request_key==request_key))
    if previous:
        if previous.request_hash!=digest:raise ServiceConflict('Idempotency key payload conflict')
        return previous
    if db.scalar(select(DeliveryIntent.id).where(DeliveryIntent.request_key==request_key)):
        raise ServiceConflict('Idempotency key operation conflict')
    attachment=_attachment(db,attachment_id,expected_revision_id)
    accepted=db.scalar(select(RecipientDelivery.id).join(DeliveryIntent,RecipientDelivery.intent_id==DeliveryIntent.id)
        .join(ProcessingRevision,DeliveryIntent.revision_id==ProcessingRevision.id).where(
            ProcessingRevision.attachment_id==attachment.id,RecipientDelivery.role=='client',RecipientDelivery.state=='accepted').limit(1))
    if accepted:raise ServiceConflict('Client PDF already accepted; use the explicit resend command, not reprocessing')
    return _create_revision(db,attachment,request_key,digest,'reprocess')

def request_resend(db,attachment_id,request_key,expected_revision_id=None):
    require_schema(db);_request_lock(db,request_key)
    command={'operation':'manual_resend','attachment':str(attachment_id),'expected':str(expected_revision_id)}
    previous=db.scalar(select(DeliveryIntent).where(DeliveryIntent.request_key==request_key))
    if previous:
        if previous.payload.get('command')!=command:raise ServiceConflict('Idempotency key payload conflict')
        return previous
    digest=fingerprint(command)
    previous_revision=db.scalar(select(ProcessingRevision).where(ProcessingRevision.request_key==request_key))
    if previous_revision:
        if previous_revision.request_hash!=digest:raise ServiceConflict('Idempotency key payload conflict')
        return previous_revision
    attachment=_attachment(db,attachment_id,expected_revision_id);_content(attachment)
    if attachment.current_revision_id is None:
        # Legacy sent might be an administrative fallback. Revalidate rather than invent recipient history.
        return _create_revision(db,attachment,request_key,digest,'manual_resend')
    revision=db.get(ProcessingRevision,attachment.current_revision_id)
    if revision.state!='validated':raise ServiceConflict('Client resend requires validated current revision')
    obj=db.get(Object,revision.object_id) if revision.object_id else None
    if obj is None or not obj.is_active or obj.calculator_number!=revision.calculator_number or not obj.email:
        raise ServiceConflict('Active exact object and recipients required')
    recipients=[(x.strip(),'client') for x in obj.email.split(',') if x.strip()]
    from common.mail_content import client_payload
    return plan_intent(db,revision,recipients,request_key,'manual_resend',
        {**client_payload(attachment,obj),'command':command})
