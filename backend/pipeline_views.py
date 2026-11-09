"""Read models keep legacy metadata separate from recipient-level SMTP facts."""
import email
import hashlib
import os
from datetime import timezone
from pathlib import Path

from sqlalchemy import select, func
from common.brevo_delivery import FINAL_EVENTS
from common.models import Attachment, BrevoDeliveryEvent, MailboxAction, ProcessingRevision, DeliveryIntent, RecipientDelivery, Object, SMTPAttempt
from common.delivery_status import client_delivery


def revision_dict(row):
    if row is None:
        return None
    return {key:getattr(row,key) for key in (
        'id','attachment_id','state','calculator_number','object_id','content_sha256',
        'reason','parser_version','validation_result','created_at','finished_at')}


def pipeline_view(db, attachment):
    revision=db.get(ProcessingRevision,attachment.current_revision_id) if attachment.current_revision_id else None
    counts={}
    if revision is not None:
        rows=db.execute(select(RecipientDelivery.role,RecipientDelivery.state,func.count())
            .join(DeliveryIntent,RecipientDelivery.intent_id==DeliveryIntent.id)
            .where(DeliveryIntent.revision_id==revision.id)
            .group_by(RecipientDelivery.role,RecipientDelivery.state))
        for role,state,count in rows:
            counts.setdefault(role,{})[state]=count
    view=revision_dict(revision)
    if revision is not None and revision.object_id:
        obj=db.get(Object,revision.object_id)
        view['object_name']=obj.name if obj else None
    return {'current_revision_id':attachment.current_revision_id,
            'current_revision':view,'recipient_state_counts':counts,
            'client_delivery':client_delivery(db,revision.id) if revision else None,
            'effective_status':revision.state if revision else attachment.status,
            'legacy_history_only':revision is None}


def _read_confirmations(db, attachment_id, deliveries):
    attachment = db.get(Attachment, attachment_id)
    known = [row for row in deliveries if row.state == 'accepted' and row.accepted_at]
    if not attachment.read_receipt_received or not known:
        return {}
    # Historical MDN flags are aggregate. Attribute one only when the ledger has
    # exactly one possible recipient or the retained MIME identifies that recipient.
    accepted = db.execute(select(RecipientDelivery.id, RecipientDelivery.accepted_at)
        .join(DeliveryIntent).join(ProcessingRevision)
        .where(ProcessingRevision.attachment_id == attachment_id, RecipientDelivery.state == 'accepted')
        .limit(2)).all()
    stamp = attachment.read_receipt_at
    if len(accepted) == 1 and stamp and accepted[0].accepted_at:
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        if stamp >= accepted[0].accepted_at:
            return {accepted[0].id: stamp}

    targets = {(row.message_id, row.recipient): row for row in known}
    root = Path(os.getenv('UPLOAD_DIR', '/app/uploads')).resolve()
    actions = db.scalars(select(MailboxAction).where(MailboxAction.destination == 'Уведомления',
        MailboxAction.created_at >= min(row.accepted_at for row in known))
        .order_by(MailboxAction.created_at.desc()).limit(200))
    result = {}
    for action in actions:
        try:
            path = Path(action.raw_path).resolve()
            if not path.is_relative_to(root) or path.stat().st_size > 512 * 1024:
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != action.raw_sha256:
                continue
        except OSError:
            continue
        message = email.message_from_bytes(raw)
        if message.get_content_type() != 'multipart/report' or message.get_param('report-type') != 'disposition-notification':
            continue
        for part in message.walk():
            if part.get_content_type() != 'message/disposition-notification':
                continue
            reports = part.get_payload()
            for report in reports if isinstance(reports, list) else []:
                key = (str(report.get('Original-Message-ID', '')).strip(),
                       str(report.get('Final-Recipient', '')).split(';')[-1].strip())
                disposition = str(report.get('Disposition', '')).split(';')[-1].strip().split('/')[0].lower()
                delivery = targets.get(key)
                if disposition == 'displayed' and delivery and action.created_at >= delivery.accepted_at:
                    result[delivery.id] = action.created_at
    return result


def _delivery_summary(events):
    observations = [event for event in events if event.processing_state == 'delivery']
    finals = [event for event in observations if event.event_type in FINAL_EVENTS]
    # A delayed transient notification cannot undo an observed final outcome.
    latest = max(finals or observations, key=lambda e: (e.occurred_at, e.event_type != 'delivered', e.event_key), default=None)
    opens = [event.occurred_at for event in events if event.processing_state == 'open_signal']
    return {'status': FINAL_EVENTS.get(latest.event_type, 'deferred') if latest else 'unknown',
            'at': latest.occurred_at if latest else None, 'reason': latest.reason if latest else None,
            'opened_at': max(opens, default=None), 'event_count': len(events)}


def delivery_page(db, attachment_id, skip=0, limit=100):
    query=select(RecipientDelivery,DeliveryIntent,ProcessingRevision.id.label('revision_id'))\
        .join(DeliveryIntent,RecipientDelivery.intent_id==DeliveryIntent.id)\
        .join(ProcessingRevision,DeliveryIntent.revision_id==ProcessingRevision.id)\
        .where(ProcessingRevision.attachment_id==attachment_id)
    total=db.scalar(select(func.count()).select_from(query.subquery()))
    rows=db.execute(query.order_by(RecipientDelivery.created_at.desc(),RecipientDelivery.id)
                    .offset(skip).limit(limit)).all()
    reads = _read_confirmations(db, attachment_id, [delivery for delivery, _, _ in rows])
    brevo_ids = [delivery.id for delivery, _, _ in rows if delivery.smtp_provider == 'brevo']
    events = {}
    if brevo_ids:
        for delivery_id, event in db.execute(select(SMTPAttempt.delivery_id, BrevoDeliveryEvent)
                .join(BrevoDeliveryEvent, SMTPAttempt.id == BrevoDeliveryEvent.smtp_attempt_id)
                .where(SMTPAttempt.delivery_id.in_(brevo_ids), SMTPAttempt.provider == 'brevo')):
            events.setdefault(delivery_id, []).append(event)
    items=[]
    for delivery,intent,revision_id in rows:
        values={key:getattr(delivery,key) for key in ('id','intent_id','recipient','original_address','role',
            'state','attempts','error','error_code','message_id','accepted_at','lease_until',
            'retry_at','created_at','updated_at','smtp_provider','smtp_response_code','smtp_response')}
        values.update(revision_id=revision_id,kind=intent.kind)
        values['delivery'] = _delivery_summary(events.get(delivery.id, []))
        values['read_confirmed_at'] = reads.get(delivery.id)
        values['smtp_history']=[{key:getattr(attempt,key) for key in (
            'attempt','provider','state','response_code','response','error_code','started_at','finished_at')}
            for attempt in db.scalars(select(SMTPAttempt).where(SMTPAttempt.delivery_id==delivery.id).order_by(SMTPAttempt.attempt))]
        items.append(values)
    return {'items':items,'total':total,'skip':skip,'limit':limit,
            'accepted_means':'SMTP submission accepted; inbox delivery is not proven'}


def delivery_event_page(db, attachment_id, delivery_id, skip, limit):
    query = (select(BrevoDeliveryEvent, SMTPAttempt.attempt, RecipientDelivery.recipient, RecipientDelivery.message_id)
             .join(SMTPAttempt, SMTPAttempt.id == BrevoDeliveryEvent.smtp_attempt_id)
             .join(RecipientDelivery, RecipientDelivery.id == SMTPAttempt.delivery_id)
             .join(DeliveryIntent, DeliveryIntent.id == RecipientDelivery.intent_id)
             .join(ProcessingRevision, ProcessingRevision.id == DeliveryIntent.revision_id)
             .where(RecipientDelivery.id == delivery_id, ProcessingRevision.attachment_id == attachment_id))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(BrevoDeliveryEvent.occurred_at.desc().nulls_last(), BrevoDeliveryEvent.event_key)
                      .offset(skip).limit(limit))
    items = [{'at': event.occurred_at, 'provider': 'brevo', 'recipient': recipient, 'message_id': message_id,
              'provider_message_id': event.provider_message_id, 'smtp_attempt': attempt, 'event': event.event_type,
              'processing_state': event.processing_state, 'reason': event.reason}
             for event, attempt, recipient, message_id in rows]
    return {'items': items, 'total': total, 'skip': skip, 'limit': limit}
