import uuid
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from common.models import Attachment,ProcessingRevision,DeliveryIntent,RecipientDelivery,OutboxEvent
from common.service import request_reprocess,request_resend
from common.delivery import ServiceConflict,plan_intent,deliver_recipient,reconcile,enqueue,now
from common.pipeline import validate_revision
from test_api_rc import api,api_databases,seed as api_seed

def test_trash_hidden_from_active_lists_reports_and_reversible_without_new_work(api,tmp_path):
    active=api_seed(api.engine,tmp_path);trash=api_seed(api.engine,tmp_path)
    with Session(api.engine) as db:
        a=db.get(Attachment,uuid.UUID(active['attachment']));a.status='rejected';a.reject_reason='send_error'
        a=db.get(Attachment,uuid.UUID(trash['attachment']));a.status='archived';a.reject_reason='send_error';a.validation_result={'original_error':'preserved'}
        db.commit()
    response=api.request('GET','/api/v1/attachments');assert response.status_code==200
    assert response.headers['X-Total-Count']=='1' and response.json()[0]['id']==active['attachment']
    response=api.request('GET','/api/v1/attachments?status=archived');assert response.headers['X-Total-Count']=='1'
    assert response.json()[0]['id']==trash['attachment'] and response.json()[0]['reject_reason']=='send_error'
    assert api.request('GET','/api/v1/reports/summary').json()['total_attachments']==1
    assert trash['attachment'] not in api.request('GET','/api/v1/reports/rejections.csv').text
    for operation in ('reprocess','resend'):
        response=api.request('POST',f"/api/v1/attachments/{trash['attachment']}/{operation}",headers={'Idempotency-Key':str(uuid.uuid4())},json={})
        assert response.status_code==409 and 'trash' in response.json()['detail']
    with Session(api.engine) as db:
        assert db.scalar(select(func.count()).select_from(ProcessingRevision))==0
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==0
        a=db.get(Attachment,uuid.UUID(trash['attachment']));assert a.validation_result=={'original_error':'preserved'}
        a.status='rejected';db.commit()
    assert api.request('GET','/api/v1/attachments').headers['X-Total-Count']=='2'

def test_trash_rejects_commands_and_skips_pdf_parser(session_factory,seed):
    record=seed(state='received')
    with session_factory() as db:
        with db.begin():db.get(Attachment,record['attachment']).status='archived'
    for command in (request_reprocess,request_resend):
        with session_factory() as db:
            with pytest.raises(ServiceConflict,match='trash'):command(db,record['attachment'],str(uuid.uuid4()))
    def forbidden(*args,**kwargs):raise AssertionError('Archived PDF parser called')
    assert validate_revision(session_factory,record['revision'],analyzer=forbidden)=={'state':'archived'}

def test_trash_never_sends_and_recovery_does_not_republish(session_factory,seed):
    record=seed()
    with session_factory() as db:
        with db.begin():
            revision=db.get(ProcessingRevision,record['revision'])
            intent=plan_intent(db,revision,[('sink@example.invalid','client')],str(uuid.uuid4()))
            db.flush();delivery=db.scalar(select(RecipientDelivery).where(RecipientDelivery.intent_id==intent.id));did=delivery.id
            for event in db.scalars(select(OutboxEvent)):event.published_at=now()-timedelta(minutes=6)
            db.get(Attachment,record['attachment']).status='archived'
    assert reconcile(session_factory)['republish']==0
    class ForbiddenSMTP:
        def send(self,work):raise AssertionError('Archived SMTP was called')
    assert deliver_recipient(session_factory,did,ForbiddenSMTP())=={'state':'skipped'}
    with session_factory() as db:
        row=db.get(RecipientDelivery,did);assert row.state=='cancelled' and row.attempts==0

@pytest.mark.parametrize('decision,expected',[('validated','review_required'),('rejected','rejected'),('review_required','review_required')])
def test_legacy_review_preserves_real_pdf_decision_and_fences_smtp_atomically(session_factory,seed,decision,expected):
    from deploy.legacy_review import review_factory
    record=seed(state='received')
    def analyzer(path,context):
        return {'state':decision,'reason':'pdf_review_reason' if decision!='validated' else None,'identity_state':'unique','calculator_number':record['serial']}
    worker_factory=lambda:session_factory(autoflush=False)
    validate_revision(review_factory(worker_factory,record['attachment']),record['revision'],analyzer=analyzer)
    with session_factory() as db:
        revision=db.get(ProcessingRevision,record['revision'])
        assert revision.state==expected
        assert revision.reason==('legacy_smtp_history_incomplete' if decision=='validated' else 'pdf_review_reason')
        assert revision.validation_result['operator_review']['pdf_decision']==decision
        rows=list(db.scalars(select(RecipientDelivery)));assert rows
        assert all(r.state=='cancelled' and r.attempts==0 for r in rows)
        did=rows[0].id
    class ForbiddenSMTP:
        def send(self,work):raise AssertionError('Legacy unknown SMTP was repeated')
    assert deliver_recipient(session_factory,did,ForbiddenSMTP())=={'state':'skipped'}
