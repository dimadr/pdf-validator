import uuid,threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from common.models import Attachment,ProcessingRevision,RecipientDelivery,DeliveryIntent,OutboxEvent
from common.delivery import plan_intent,deliver_recipient,dispatch_outbox,reconcile,DefinitiveFailure,ServiceConflict,now,_claim
from common.service import request_resend,request_reprocess

def planned(factory,record,addresses=None,key=None,role='client'):
    with factory() as db:
        with db.begin():
            revision=db.get(ProcessingRevision,record['revision'])
            intent=plan_intent(db,revision,[(a,role) for a in (addresses or ['one@example.invalid'])],key or str(uuid.uuid4()))
            db.flush();return list(db.scalars(select(RecipientDelivery.id).where(RecipientDelivery.intent_id==intent.id)))

class SMTP:
    def __init__(self,fail=None):self.calls=[];self.fail=fail;self.lock=threading.Lock()
    def send(self,work):
        with self.lock:self.calls.append(work['recipient'])
        if self.fail==work['recipient']:raise DefinitiveFailure('Synthetic explicit temporary rejection')

def test_two_workers_claim_one_effect(session_factory,seed):
    delivery=planned(session_factory,seed())[0];smtp=SMTP();barrier=threading.Barrier(2)
    def run(_):barrier.wait();return deliver_recipient(session_factory,delivery,smtp)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
    assert len(smtp.calls)==1 and sorted(r['state'] for r in results)==['accepted','skipped']

def test_replayed_task_and_outbox_publish_do_not_resend(session_factory,seed):
    delivery=planned(session_factory,seed())[0];events=[]
    def crash(event):raise RuntimeError('Crash after publish before commit')
    assert dispatch_outbox(session_factory,lambda t,p:events.append(p),after_publish=crash)==0
    assert dispatch_outbox(session_factory,lambda t,p:events.append(p))==1 and len(events)==2
    smtp=SMTP()
    for _ in events:deliver_recipient(session_factory,delivery,smtp)
    assert smtp.calls==['one@example.invalid']

def test_partial_failure_only_second_retries(session_factory,seed):
    ids=planned(session_factory,seed(),['one@example.invalid','two@example.invalid']);smtp=SMTP('two@example.invalid')
    for delivery in ids:deliver_recipient(session_factory,delivery,smtp)
    with session_factory() as db:
        with db.begin():
            failed=db.scalar(select(RecipientDelivery).where(RecipientDelivery.state=='failed'));failed.retry_at=now()-timedelta(seconds=1)
    smtp.fail=None
    results=[deliver_recipient(session_factory,delivery,smtp) for delivery in ids]
    with session_factory() as db:
        details=[(d.recipient,d.state,d.attempts,str(d.retry_at),d.error_code) for d in db.scalars(select(RecipientDelivery))]
    assert smtp.calls.count('one@example.invalid')==1 and smtp.calls.count('two@example.invalid')==2,(results,details)

def test_broker_failure_retains_atomic_outbox(session_factory,seed):
    record=seed(state='received')
    with session_factory() as db:
        with db.begin():revision=request_reprocess(db,record['attachment'],'new-process',record['revision'])
    def unavailable(*a):raise ConnectionError('Synthetic broker unavailable')
    assert dispatch_outbox(session_factory,unavailable)==0
    with session_factory() as db:
        assert db.get(ProcessingRevision,revision.id).state=='received'
        assert db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None)))==1
    assert dispatch_outbox(session_factory,lambda *a:None)==1

def test_ack_commit_failure_is_uncertain_no_resend(session_factory,seed):
    delivery=planned(session_factory,seed())[0];smtp=SMTP()
    def crash(db):db.add(OutboxEvent(id=uuid.uuid4(),topic=None,payload={})) # Actual PostgreSQL NOT NULL failure at commit.
    assert deliver_recipient(session_factory,delivery,smtp,before_result_commit=crash)['state']=='uncertain'
    assert deliver_recipient(session_factory,delivery,smtp)['state']=='skipped'
    with session_factory() as db:assert db.get(RecipientDelivery,delivery).state=='uncertain'
    assert len(smtp.calls)==1

def test_manual_resend_retains_accepted_history(session_factory,seed):
    record=seed();old=planned(session_factory,record)[0];smtp=SMTP();deliver_recipient(session_factory,old,smtp)
    with session_factory() as db:
        original=db.get(RecipientDelivery,old).message_id
        with db.begin_nested():intent=request_resend(db,record['attachment'],'manual-resend',record['revision'])
        db.commit()
    with session_factory() as db:
        assert db.get(RecipientDelivery,old).state=='accepted' and db.get(RecipientDelivery,old).message_id==original
        assert db.scalar(select(func.count()).select_from(RecipientDelivery).where(RecipientDelivery.intent_id==intent.id))==2
        assert db.get(Attachment,record['attachment']).sent_to_email=='historical@example.invalid'

def test_admin_acceptance_is_not_client_delivery(session_factory,seed):
    record=seed(state='review_required');delivery=planned(session_factory,record,['admin@example.invalid'],role='admin')[0]
    assert deliver_recipient(session_factory,delivery,SMTP())['state']=='accepted'
    with session_factory() as db:
        assert db.get(ProcessingRevision,record['revision']).state=='review_required'
        assert db.get(RecipientDelivery,delivery).role=='admin'

def test_expired_claim_is_uncertain_and_not_retried(session_factory,seed):
    delivery=planned(session_factory,seed())[0]
    assert _claim(session_factory,delivery,str(uuid.uuid4()),-1,3)
    assert reconcile(session_factory)['uncertain']==1
    smtp=SMTP();assert deliver_recipient(session_factory,delivery,smtp)['state']=='skipped' and not smtp.calls

def test_crash_before_claim_remains_pending(session_factory,seed):
    delivery=planned(session_factory,seed())[0];smtp=SMTP()
    with session_factory() as db:assert db.get(RecipientDelivery,delivery).state=='pending'
    assert deliver_recipient(session_factory,delivery,smtp)['state']=='accepted'

def test_reprocess_cancels_old_tasks_and_resets_identity(session_factory,seed):
    record=seed();old=planned(session_factory,record)[0]
    with session_factory() as db:
        with db.begin():revision=request_reprocess(db,record['attachment'],'new-revision',record['revision'])
    smtp=SMTP();assert deliver_recipient(session_factory,old,smtp)['state']=='skipped' and not smtp.calls
    with session_factory() as db:
        current=db.get(ProcessingRevision,revision.id)
        assert current.object_id is None and current.calculator_number is None and current.state=='received'

def test_reprocess_refuses_live_or_uncertain_claim(session_factory,seed):
    record=seed();delivery=planned(session_factory,record)[0];_claim(session_factory,delivery,str(uuid.uuid4()),120,3)
    with session_factory() as db:
        with pytest.raises(ServiceConflict):request_reprocess(db,record['attachment'],'unsafe-reprocess',record['revision'])

def test_idempotency_race_and_payload_conflict(session_factory,seed):
    record=seed();barrier=threading.Barrier(2)
    def create(_):
        barrier.wait()
        with session_factory() as db:
            with db.begin():return plan_intent(db,db.get(ProcessingRevision,record['revision']),[('one@example.invalid','client')],'same-key').id
    with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(create,range(2)))
    assert ids[0]==ids[1]
    with session_factory() as db:
        with pytest.raises(ServiceConflict):plan_intent(db,db.get(ProcessingRevision,record['revision']),[('other@example.invalid','client')],'same-key')

def test_atomic_command_rollback_has_no_revision_or_outbox(session_factory,seed):
    record=seed()
    with pytest.raises(RuntimeError):
        with session_factory() as db:
            with db.begin():request_reprocess(db,record['attachment'],'rollback-key');raise RuntimeError('Injected transaction rollback')
    with session_factory() as db:
        assert db.scalar(select(ProcessingRevision.id).where(ProcessingRevision.request_key=='rollback-key')) is None
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==0

def test_permanent_failure_clears_previous_retry_schedule(session_factory,seed):
    delivery=planned(session_factory,seed())[0]
    class Reject:
        calls=0
        def send(self,work):
            self.calls+=1;raise DefinitiveFailure('Synthetic reject',retryable=self.calls==1)
    smtp=Reject();deliver_recipient(session_factory,delivery,smtp)
    with session_factory() as db:
        with db.begin():db.get(RecipientDelivery,delivery).retry_at=now()-timedelta(seconds=1)
    deliver_recipient(session_factory,delivery,smtp)
    assert deliver_recipient(session_factory,delivery,smtp)['state']=='skipped' and smtp.calls==2
    with session_factory() as db:assert db.get(RecipientDelivery,delivery).retry_at is None

def test_changed_object_serial_cancels_old_binding(session_factory,seed):
    from common.models import Object
    record=seed();delivery=planned(session_factory,record)[0]
    with session_factory() as db:
        with db.begin():db.get(Object,record['object']).calculator_number='another-full-serial'
    smtp=SMTP();assert deliver_recipient(session_factory,delivery,smtp)['state']=='skipped' and not smtp.calls
    with session_factory() as db:
        with pytest.raises(ServiceConflict):request_resend(db,record['attachment'],'changed-binding',record['revision'])

def test_display_name_cannot_create_duplicate_mailbox_keys(session_factory,seed):
    record=seed()
    with session_factory() as db:
        with pytest.raises(ServiceConflict):plan_intent(db,db.get(ProcessingRevision,record['revision']),
            [('Name<one@example.invalid>','client'),('one@example.invalid','client')],'display-name')
