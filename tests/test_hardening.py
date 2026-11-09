"""Reproductions against the deployed b299da5 baseline; effects go only to loopback."""
import uuid
from sqlalchemy import select,func
from common.models import Object,Attachment,DeliveryIntent,RecipientDelivery,SMTPAttempt,OutboxEvent
from common.delivery import deliver_recipient,ServiceConflict
from common.service import request_reprocess
from common.pipeline import validate_revision
from test_delivery_core import planned,SMTP
import pytest

def test_reprocess_cannot_repeat_an_already_accepted_client_pdf(session_factory,seed):
    record=seed();delivery=planned(session_factory,record)[0]
    assert deliver_recipient(session_factory,delivery,SMTP())['state']=='accepted'
    with session_factory() as db:
        with pytest.raises(ServiceConflict,match='already accepted'):
            request_reprocess(db,record['attachment'],'new-analysis-after-client-ack',record['revision'])

@pytest.mark.parametrize('change,reason',[('inactive','object_inactive'),('archive','attachment_archived'),('revision','stale_or_unavailable_revision')])
def test_route_is_rechecked_after_claim_before_smtp(session_factory,seed,change,reason):
    record=seed();delivery=planned(session_factory,record)[0];smtp=SMTP()
    def changed(work):
        with session_factory() as db:
            with db.begin():
                if change=='inactive':db.get(Object,record['object']).is_active=False
                elif change=='archive':db.get(Attachment,record['attachment']).status='archived'
                else:db.get(Attachment,record['attachment']).current_revision_id=None
    result=deliver_recipient(session_factory,delivery,smtp,before_smtp=changed)
    assert not smtp.calls,'SMTP sent after the document/object was made unavailable'
    assert result['state']=='cancelled'
    with session_factory() as db:
        row=db.get(RecipientDelivery,delivery);assert row.error_code==reason and row.lease_until is None
        assert db.scalar(select(SMTPAttempt)).state=='failed'

def test_rejected_admin_notification_is_russian_and_keeps_filename(session_factory,seed):
    record=seed(state='received')
    with session_factory() as db:
        with db.begin():db.get(Attachment,record['attachment']).sent_filename='Объект - Адрес.pdf'
    validate_revision(session_factory,record['revision'],lambda *a,**k:{'state':'rejected',
        'identity_state':'unique','calculator_number':record['serial'],'reason':'missing_measurements',
        'issues':[{'code':'missing_required_measurements','count':2}]})
    with session_factory() as db:
        intent=db.scalar(select(DeliveryIntent));assert intent.kind=='admin_notification'
        assert intent.payload['subject'].startswith('ОШИБКА:')
        assert 'Здравствуйте!' in intent.payload['body'] and 'missing_measurements' in intent.payload['body']
        assert 'Processing decision' not in intent.payload['body'] and intent.payload['filename']=='Объект - Адрес.pdf'
        assert set(db.scalars(select(RecipientDelivery.role)))=={'admin'}

def test_default_client_intent_never_invents_english_placeholder(session_factory,seed):
    record=seed();planned(session_factory,record)
    with session_factory() as db:
        payload=db.scalar(select(DeliveryIntent)).payload
        assert payload['subject'].startswith('Отчет ') and 'Здравствуйте!' in payload['body']
        assert payload['filename']!='report.pdf'

def test_legacy_smtp_review_hold_cannot_be_bypassed_by_reprocess(session_factory,seed):
    record=seed();delivery=planned(session_factory,record)[0]
    with session_factory() as db:
        with db.begin():
            row=db.get(RecipientDelivery,delivery);row.state='cancelled';row.error_code='legacy_smtp_history_incomplete'
    with session_factory() as db:
        with pytest.raises(ServiceConflict,match='reconciled'):
            request_reprocess(db,record['attachment'],'bypass-history-hold',record['revision'])
