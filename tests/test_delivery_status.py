from sqlalchemy import select
from common.delivery import plan_intent,deliver_recipient
from common.models import ProcessingRevision,RecipientDelivery
from common.delivery_status import client_delivery
from test_delivery_core import planned,SMTP
from test_api_rc import api,api_databases,seed as api_seed
from sqlalchemy.orm import Session
import uuid

def test_client_summary_uses_latest_operation_and_not_admin_or_old_success(session_factory,seed):
    record=seed();old=planned(session_factory,record)[0];deliver_recipient(session_factory,old,SMTP())
    with session_factory() as db:
        assert client_delivery(db,record['revision'])['all_accepted']
        with db.begin_nested():
            revision=db.get(ProcessingRevision,record['revision'])
            new=plan_intent(db,revision,[('one@example.invalid','client')],'later-resend','manual_resend')
            plan_intent(db,revision,[('admin@example.invalid','admin')],'later-admin','admin_notification')
        db.commit()
        result=client_delivery(db,record['revision'])
        assert result['intent_id']==str(new.id) and result['counts']=={'pending':1} and not result['all_accepted']

def test_summary_is_exposed_by_real_attachment_api(api,tmp_path):
    item=api_seed(api.engine,tmp_path,validated=True)
    with Session(api.engine) as db:
        revision=db.get(ProcessingRevision,uuid.UUID(item['revision']))
        intent=plan_intent(db,revision,[('one@example.invalid','client')],'typed-summary')
        db.flush();db.scalar(select(RecipientDelivery).where(RecipientDelivery.intent_id==intent.id)).state='accepted';db.commit()
    result=api.request('GET',f"/api/v1/attachments/{item['attachment']}")
    assert result.status_code==200,result.text
    assert result.json()['client_delivery']['all_accepted'] and result.json()['client_delivery']['counts']=={'accepted':1}


def test_sender_provider_comes_from_attempt_not_current_configuration(session_factory,seed,monkeypatch):
    record=seed();delivery=planned(session_factory,record)[0]
    with session_factory() as db:
        row=db.get(RecipientDelivery,delivery);row.smtp_provider='brevo';row.attempts=1;row.state='accepted';db.commit()
        monkeypatch.setenv('SMTP_PROVIDER','timeweb')
        assert client_delivery(db,record['revision'])['providers']==['brevo']
