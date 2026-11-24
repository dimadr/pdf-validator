"""Interpret confirmed Brevo events and queue text-only dispatcher alerts."""
import os
import re
from collections import defaultdict
from datetime import timezone

from sqlalchemy import select

from common.delivery import ServiceConflict, normalized, plan_intent
from common.models import BrevoDeliveryEvent, DeliveryIntent, Object, ProcessingRevision, RecipientDelivery, SMTPAttempt

FINAL_EVENTS = {
    'delivered': 'delivered', 'hardbounce': 'bounced', 'hardbounces': 'bounced',
    'blocked': 'blocked', 'invalid': 'failed', 'error': 'failed', 'failed': 'failed',
}
TEMPORARY_FAILURES = {'softbounce', 'softbounces'}
PROBLEM_EVENTS = (set(FINAL_EVENTS) - {'delivered'}) | TEMPORARY_FAILURES
DELIVERY_EVENTS = PROBLEM_EVENTS | {'delivered', 'deferred'}
SMTP_REPLY = re.compile(r'(?<![\d.])([45]\d{2})(?:[ -]+([45]\.\d{1,3}\.\d{1,3}))?(?![\d.])')


def reply_codes(reason):
    match = SMTP_REPLY.search(reason or '')
    return (int(match[1]), match[2]) if match else (None, None)


def outcome(events):
    valid = [event for event in events if event.processing_state == 'delivery'
             and event.occurred_at is not None and event.event_type in DELIVERY_EVENTS]
    order = lambda event: (event.occurred_at, event.event_type == 'delivered', event.event_key)
    terminals = [event for event in valid if event.event_type in FINAL_EVENTS]
    current = max(terminals or valid, key=order, default=None)
    if current is None:
        return 'unknown', None, None
    status = FINAL_EVENTS.get(current.event_type, 'deferred')
    if status == 'delivered':
        previous = max((event for event in valid if event.event_type in PROBLEM_EVENTS | {'deferred'}
                        and event.occurred_at < current.occurred_at), key=order, default=None)
        return status, current, previous
    return status, current, current


def problem_details(recipient, revision, obj, kind, reason, occurred_at, message_id):
    code, enhanced = reply_codes(reason)
    return {'smtp_code': code, 'smtp_enhanced_code': enhanced, 'reason': reason,
            'recipient': recipient, 'calculator_number': revision.calculator_number,
            'object': obj.name if obj else None, 'at': occurred_at.astimezone(timezone.utc).isoformat(),
            'provider_message_id': message_id, 'event': kind}


def problem_context():
    return (select(RecipientDelivery, ProcessingRevision, Object).select_from(RecipientDelivery)
            .join(DeliveryIntent, DeliveryIntent.id == RecipientDelivery.intent_id)
            .join(ProcessingRevision, ProcessingRevision.id == DeliveryIntent.revision_id)
            .outerjoin(Object, Object.id == ProcessingRevision.object_id))


def snapshot_problem(db, attempt_id, kind, reason, occurred_at, message_id):
    context = db.execute(problem_context().join(SMTPAttempt, SMTPAttempt.delivery_id == RecipientDelivery.id)
                         .where(SMTPAttempt.id == attempt_id, RecipientDelivery.role == 'client')).one_or_none()
    if context is None:
        return None
    delivery, revision, obj = context
    return problem_details(delivery.recipient, revision, obj, kind, reason, occurred_at, message_id)


def alert_payload(problem, phase):
    category = 'Временный отказ' if phase == 'temporary' else 'Окончательная недоставка'
    meter = ' '.join((problem['calculator_number'] or 'не определён').split())
    labels = (('Прибор', 'calculator_number'), ('Объект', 'object'), ('Получатель', 'recipient'),
              ('Время события', 'at'), ('Brevo Message-ID', 'provider_message_id'), ('Ответ SMTP', 'reason'))
    lines = [f"{label}: {' '.join(str(problem[key] or '—').split())}" for label, key in labels]
    return {'subject': f'Brevo — {category.lower()} — прибор {meter}', 'delivery_problem': problem,
            'body': category + '\n\n' + '\n'.join(lines)}


def queue_dispatcher_alerts(db, attempt_ids, since):
    address = os.getenv('NOTIFY_EMAIL', '').strip()
    try:
        dispatcher = normalized(address)
    except ServiceConflict:
        return 0
    candidates = select(BrevoDeliveryEvent.smtp_attempt_id).where(
        BrevoDeliveryEvent.smtp_attempt_id.in_(attempt_ids), BrevoDeliveryEvent.event_type.in_(PROBLEM_EVENTS),
        BrevoDeliveryEvent.processing_state == 'delivery', BrevoDeliveryEvent.occurred_at >= since)
    query = (problem_context().add_columns(BrevoDeliveryEvent)
             .join(SMTPAttempt, SMTPAttempt.delivery_id == RecipientDelivery.id)
             .join(BrevoDeliveryEvent, BrevoDeliveryEvent.smtp_attempt_id == SMTPAttempt.id)
             .where(SMTPAttempt.id.in_(candidates), RecipientDelivery.role == 'client',
                    BrevoDeliveryEvent.processing_state == 'delivery'))
    histories = defaultdict(list)
    contexts = {}
    for delivery, revision, obj, event in db.execute(query):
        contexts[delivery.id] = delivery, revision, obj
        histories[delivery.id].append(event)

    queued = 0
    for delivery_id, events in histories.items():
        delivery, revision, obj = contexts[delivery_id]
        status, current, _ = outcome(events)
        if (current is None or current.event_type not in PROBLEM_EVENTS or current.occurred_at < since
                or dispatcher.lower() == delivery.recipient.lower()):
            continue
        phase = 'temporary' if status == 'deferred' else 'final'
        request_key = f'brevo-delivery-alert:{delivery_id}:{phase}'
        if db.scalar(select(DeliveryIntent.id).where(DeliveryIntent.request_key == request_key)):
            continue
        problem = problem_details(
            delivery.recipient, revision, obj, current.event_type, current.reason,
            current.occurred_at, current.provider_message_id)
        plan_intent(db, revision, [(dispatcher, 'admin')], request_key,
                    kind='admin_notification', payload=alert_payload(problem, phase))
        queued += 1
    return queued
