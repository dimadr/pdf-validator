"""Read models for operator review. Never enqueue, claim, alter state or open SMTP."""
from collections import Counter
from datetime import datetime,time,timedelta,timezone
import hashlib,json,pathlib
from sqlalchemy import select,func
from common.models import Attachment,IncomingMessage,ProcessingRevision,Object,RecipientDelivery,DeliveryIntent
from common.delivery import normalized,ServiceConflict
from common.smtp_settings import selection,quota_used,quota_limit

MSK=timezone(timedelta(hours=3))

def preview(db,date_from,date_to,external_brevo_used=None):
    if date_to<date_from or (date_to-date_from).days>31:raise ValueError('Select an inclusive period of at most 32 days')
    if external_brevo_used is not None and external_brevo_used<0:raise ValueError('External Brevo usage cannot be negative')
    start=datetime.combine(date_from,time.min,MSK).astimezone(timezone.utc).replace(tzinfo=None)
    end=datetime.combine(date_to+timedelta(days=1),time.min,MSK).astimezone(timezone.utc).replace(tzinfo=None)
    provider=selection(db)
    records=db.execute(select(Attachment,IncomingMessage,ProcessingRevision,Object)
        .join(IncomingMessage,IncomingMessage.id==Attachment.message_id)
        .outerjoin(ProcessingRevision,ProcessingRevision.id==Attachment.current_revision_id)
        .outerjoin(Object,Object.id==ProcessingRevision.object_id)
        .where(IncomingMessage.received_at>=start,IncomingMessage.received_at<end)
        .order_by(IncomingMessage.received_at,Attachment.id)).all()
    if len(records)>10000:raise ValueError('Preview exceeds 10000 documents; select a shorter interval')
    revision_ids=[r.id for _,_,r,_ in records if r]
    deliveries={}
    if revision_ids:
        for delivery,rid in db.execute(select(RecipientDelivery,DeliveryIntent.revision_id).join(DeliveryIntent)
            .where(DeliveryIntent.revision_id.in_(revision_ids))):deliveries.setdefault(rid,[]).append(delivery)
    counts=Counter();reasons=Counter();targets=set();documents=[];operations=Counter()
    for attachment,message,revision,obj in records:
        state=revision.state if revision else attachment.status;rows=deliveries.get(revision.id,[]) if revision else []
        eligible=[d for d in rows if d.state=='pending' or d.state=='failed' and d.retry_at is not None]
        reason=revision.reason if revision else attachment.reject_reason
        if attachment.status=='archived':category='archived';eligible=[]
        elif not revision:category='legacy_history_only';eligible=[]
        elif state=='discarded_inactive' or obj and not obj.is_active:category='inactive';eligible=[]
        elif any(d.state in ('in_flight','uncertain') for d in rows):category='held';eligible=[]
        elif state=='rejected':category='rejected';reasons[reason or state]+=1
        elif state!='validated':category='review_required'
        elif not obj or obj.calculator_number!=revision.calculator_number:category='review_required';reason='object_binding_changed';eligible=[]
        elif not pathlib.Path(revision.file_path or '').is_file():category='review_required';reason='content_unavailable';eligible=[]
        else:
            clients=[d for d in rows if d.role=='client']
            try:expected={normalized(a) for a in (obj.email or '').split(',') if a.strip()}
            except ServiceConflict:expected=set()
            if not expected or expected!={d.recipient for d in clients}:
                category='review_required';reason='recipient_config_changed_or_not_planned';eligible=[]
            elif clients and all(d.state=='accepted' for d in clients):category='already_accepted'
            elif any(d.role=='client' for d in eligible):category='ready'
            else:category='held'
        counts[category]+=1
        for delivery in eligible:
            targets.add(delivery.recipient);operations[delivery.role]+=1
        documents.append({'attachment_id':str(attachment.id),'received_at_msk':message.received_at.replace(tzinfo=timezone.utc).astimezone(MSK).isoformat(),
            'serial':revision.calculator_number if revision else attachment.calculator_number,'object':obj.name if obj else None,
            'state':state,'category':category,'reason':reason,
            'planned_provider':provider if eligible else None,'recipients':[{'address':d.recipient,'role':d.role,'state':d.state} for d in eligible]})
    messages=db.scalars(select(IncomingMessage).where(IncomingMessage.received_at>=start,IncomingMessage.received_at<end)).all()
    duplicates=sum(p.get('state') in ('deduplicated','discarded_inactive','historical_content_unavailable') for m in messages for p in (m.part_manifest or []))
    used=quota_used(db);limit=quota_limit();available=None if external_brevo_used is None else max(0,limit-used-external_brevo_used)
    uncertain=db.scalar(select(func.count()).select_from(RecipientDelivery).where(RecipientDelivery.state=='uncertain'))
    blockers=[]
    if not records:blockers.append('no_documents_in_period')
    if uncertain:blockers.append('unresolved_smtp_outcomes')
    if counts['held'] or counts['review_required']:blockers.append('documents_require_review')
    if provider=='brevo':
        if available is None:blockers.append('external_brevo_usage_not_confirmed')
        elif sum(operations.values())>available:blockers.append('brevo_capacity_requires_waiting_or_operator_plan')
    plan={'period_msk':[str(date_from),str(date_to)],'documents_total':len(records),'counts':dict(counts),
        'rejections_by_reason':dict(reasons),'duplicate_parts':duplicates,'unique_recipients':len(targets),
        'smtp_operations':dict(operations),'provider':provider,
        'brevo':{'limit_rolling_24h':limit,'project1_reserved_24h':used,'other_sends_24h':external_brevo_used,'available_24h':available},
        'documents':documents,'blockers':blockers,'status':'NO_GO' if blockers else 'READY_FOR_OPERATOR_REVIEW',
        'effects':'read_only; no approval, sending, scheduling or state changes'}
    plan['plan_sha256']=hashlib.sha256(json.dumps(plan,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return plan
