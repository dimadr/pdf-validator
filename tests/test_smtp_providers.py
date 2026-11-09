"""Targeted PostgreSQL, concurrency, protocol and ACL checks; external SMTP is stubbed."""
import uuid, ssl, smtplib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select, func
from common.models import RecipientDelivery, SMTPAttempt, SMTPProviderAudit, OutboxEvent
from common.delivery import deliver_recipient, DefinitiveFailure, AmbiguousFailure, now
from common.smtp_settings import set_provider, selection, status
from common.smtp_transport import SMTPTransport
from test_delivery_core import planned
from test_api_rc import api, api_databases

@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv('BREVO_SMTP_KEY','synthetic-only-smtp-fixture')
    monkeypatch.setenv('BREVO_SMTP_USER','smtp-user@example.com')
    monkeypatch.setenv('BREVO_SMTP_FROM','eldis@example.com')
    monkeypatch.delenv('BREVO_SMTP_KEY_FILE',raising=False)
    monkeypatch.setenv('SMTP_SERVER','127.0.0.1');monkeypatch.setenv('SMTP_FROM','test@example.invalid')
    monkeypatch.setenv('BREVO_DAILY_LIMIT','300')

def switch(factory, provider):
    with factory() as db:
        with db.begin():return set_provider(db,provider,'synthetic-admin',selection(db))

class Sink:
    def __init__(self,fail=None):self.calls=[];self.fail=fail
    def send(self,work):
        self.calls.append(dict(work))
        if self.fail:raise self.fail
        return {'code':250,'response':'2.0.0 queued synthetic-id'}

def due(factory,delivery):
    with factory() as db:
        with db.begin():db.get(RecipientDelivery,delivery).retry_at=now()-timedelta(seconds=1)

def test_switch_is_persisted_audited_and_does_not_enqueue(session_factory):
    assert switch(session_factory,'brevo')['provider']=='brevo'
    with session_factory() as db:
        assert selection(db)=='brevo'
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==0
        assert [(r.previous,r.provider,r.actor) for r in db.scalars(select(SMTPProviderAudit))]==[('timeweb','brevo','synthetic-admin')]
    switch(session_factory,'timeweb')
    with session_factory() as db:assert selection(db)=='timeweb'

def test_claim_freezes_provider_and_accepted_never_resends_after_switch(session_factory,seed):
    delivery=planned(session_factory,seed())[0];sink=Sink()
    assert deliver_recipient(session_factory,delivery,sink,before_smtp=lambda _:switch(session_factory,'brevo'))['state']=='accepted'
    assert sink.calls[0]['provider']=='timeweb'
    assert deliver_recipient(session_factory,delivery,sink)['state']=='skipped'
    with session_factory() as db:
        row=db.get(RecipientDelivery,delivery)
        assert (row.smtp_provider,row.smtp_response_code,row.attempts)==('timeweb',250,1)
        assert db.scalar(select(SMTPAttempt)).response=='2.0.0 queued synthetic-id'
    assert len(sink.calls)==1

def test_partial_ack_and_definitive_retry_can_change_provider(session_factory,seed):
    ids=planned(session_factory,seed(),['one@example.invalid','two@example.invalid']);sink=Sink()
    deliver_recipient(session_factory,ids[0],sink)
    sink.fail=DefinitiveFailure('explicit rejection',True,response_code=451,response='try later')
    deliver_recipient(session_factory,ids[1],sink)
    switch(session_factory,'brevo');due(session_factory,ids[1]);sink.fail=None
    assert deliver_recipient(session_factory,ids[0],sink)['state']=='skipped'
    assert deliver_recipient(session_factory,ids[1],sink)['state']=='accepted'
    with session_factory() as db:
        history=list(db.scalars(select(SMTPAttempt).where(SMTPAttempt.delivery_id==ids[1]).order_by(SMTPAttempt.attempt)))
        assert [(a.provider,a.state,a.response_code) for a in history]==[('timeweb','failed',451),('brevo','accepted',250)]
    assert len(sink.calls)==3

@pytest.mark.parametrize('commit_failure',[False,True])
def test_uncertain_stays_blocked_across_provider_switch(session_factory,seed,commit_failure):
    delivery=planned(session_factory,seed())[0];sink=Sink(None if commit_failure else AmbiguousFailure())
    def crash(db):db.add(OutboxEvent(id=uuid.uuid4(),topic=None,payload={}))
    assert deliver_recipient(session_factory,delivery,sink,before_result_commit=crash if commit_failure else None)['state']=='uncertain'
    switch(session_factory,'brevo')
    assert deliver_recipient(session_factory,delivery,sink)['state']=='skipped'
    with session_factory() as db:
        attempt=db.scalar(select(SMTPAttempt));assert attempt.state=='uncertain'
        if commit_failure:assert attempt.response_code==250
    assert len(sink.calls)==1

def test_quota_is_reserved_atomically_and_pending_is_retained(session_factory,seed,monkeypatch):
    monkeypatch.setenv('BREVO_DAILY_LIMIT','1');switch(session_factory,'brevo')
    ids=[planned(session_factory,seed())[0] for _ in range(2)];sink=Sink()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda delivery:deliver_recipient(session_factory,delivery,sink),ids))
    assert len(sink.calls)==1
    with session_factory() as db:
        waiting=db.scalar(select(RecipientDelivery).where(RecipientDelivery.state=='pending'))
        assert waiting.attempts==0 and waiting.error_code=='smtp_quota' and waiting.retry_at
        waiting_id=waiting.id
    switch(session_factory,'timeweb');due(session_factory,waiting_id)
    assert deliver_recipient(session_factory,waiting_id,sink)['state']=='accepted'

def test_quota_response_does_not_exhaust_delivery(session_factory,seed):
    switch(session_factory,'brevo');delivery=planned(session_factory,seed())[0]
    sink=Sink(DefinitiveFailure('SMTP explicit rejection 451',True,'smtp_quota',451,'daily quota exceeded'))
    for _ in range(4):
        assert deliver_recipient(session_factory,delivery,sink)['state']=='failed';due(session_factory,delivery)
    sink.fail=None
    assert deliver_recipient(session_factory,delivery,sink)['state']=='accepted'
    with session_factory() as db:assert db.get(RecipientDelivery,delivery).attempts==5

def test_brevo_tls_verified_sender_and_ack_without_external_network(monkeypatch):
    calls=[]
    class SMTP:
        submission_reply=(250,b'2.0.0 queued synthetic-id')
        def __init__(self,host,port,timeout):calls.append((host,port))
        def starttls(self,context):
            assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
        def login(self,user,key):assert user=='smtp-user@example.com' and key=='synthetic-only-smtp-fixture'
        def sendmail(self,sender,recipients,raw):
            assert sender=='eldis@example.com' and recipients==['dv@example.com'];assert b'Message-ID: <test@example.invalid>' in raw
            return {}
        def quit(self):pass
    monkeypatch.setattr('common.smtp_transport.RecordedSMTP',SMTP)
    result=SMTPTransport().send(dict(provider='brevo',recipient='dv@example.com',date='Mon, 02 Nov 2026 09:00:00 +0000',
        message_id='<test@example.invalid>',payload={},content=b'%PDF synthetic'))
    assert result['code']==250 and calls==[('smtp-relay.brevo.com',587)]

def test_provider_api_admin_confirmation_stale_and_no_secret_bypass(api):
    path='/api/v1/settings/smtp-provider'
    assert api.request('GET',path).json()['provider']=='timeweb'
    body={'provider':'brevo','expected_provider':'timeweb','confirmed':False}
    assert api.request('PUT',path,json=body).status_code==422
    body['confirmed']=True
    result=api.request('PUT',path,json=body)
    assert result.status_code==200 and result.json()['provider']=='brevo'
    assert 'synthetic-only-smtp-fixture' not in result.text
    assert api.request('PUT',path,json=body).status_code==409
    assert api.request('PUT','/api/v1/settings/settings/smtp_provider',json={'value':'timeweb'}).status_code==422
    api.user.role='operator'
    assert api.request('GET',path).status_code==403
    assert api.request('PUT',path,json=body).status_code==403
