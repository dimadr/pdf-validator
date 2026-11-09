from datetime import date,datetime
from sqlalchemy import select,func,text
from common.dispatch_preview import preview
from common.models import Attachment,Object,RecipientDelivery,OutboxEvent,Setting
from common.delivery import deliver_recipient
from test_delivery_core import planned,SMTP

def today_record(session_factory,seed):
    record=seed()
    with session_factory() as db:
        with db.begin():
            attachment=db.get(Attachment,record['attachment']);attachment.message.received_at=datetime(2026,11,23,21,0)
    return record

def test_preview_is_read_only_reproducible_and_uses_moscow_source_dates(session_factory,seed):
    record=today_record(session_factory,seed);ids=planned(session_factory,record,['one@example.invalid','two@example.invalid'])
    with session_factory() as db:
        with db.begin():
            db.execute(text('SET TRANSACTION READ ONLY'))
            before=db.scalar(select(func.count()).select_from(OutboxEvent))
            first=preview(db,date(2026,11,24),date(2026,11,24));second=preview(db,date(2026,11,24),date(2026,11,24))
            assert first==second and first['counts']=={'ready':1} and first['unique_recipients']==2
            assert first['documents'][0]['received_at_msk'].startswith('2026-11-24T00:00:00')
            assert db.scalar(select(func.count()).select_from(OutboxEvent))==before
            assert all(d.attempts==0 for d in db.scalars(select(RecipientDelivery)))
            assert preview(db,date(2026,11,23),date(2026,11,23))['documents_total']==0

def test_brevo_external_usage_unknown_blocks_plan_and_counts_admin_too(session_factory,seed):
    record=today_record(session_factory,seed);planned(session_factory,record,['one@example.invalid','two@example.invalid'])
    with session_factory() as db:
        with db.begin():db.add(Setting(key='smtp_provider',value='brevo'))
        p=preview(db,date(2026,11,24),date(2026,11,25))
        assert p['status']=='NO_GO' and p['brevo']['available_24h'] is None
        p=preview(db,date(2026,11,24),date(2026,11,25),299)
        assert p['brevo']['available_24h']==1 and 'brevo_capacity_requires_waiting_or_operator_plan' in p['blockers']

def test_accepted_inactive_and_archived_are_not_planned_again(session_factory,seed):
    record=today_record(session_factory,seed);ids=planned(session_factory,record,['one@example.invalid'])
    with session_factory() as db:
        with db.begin():db.get(Object,record['object']).email='one@example.invalid'
    deliver_recipient(session_factory,ids[0],SMTP())
    with session_factory() as db:
        p=preview(db,date(2026,11,24),date(2026,11,24),0);assert p['counts']=={'already_accepted':1} and p['unique_recipients']==0
    with session_factory() as db:
        with db.begin():db.get(Object,record['object']).is_active=False
    with session_factory() as db:assert preview(db,date(2026,11,24),date(2026,11,24),0)['counts']=={'inactive':1}
    with session_factory() as db:
        with db.begin():db.get(Attachment,record['attachment']).status='archived'
    with session_factory() as db:assert preview(db,date(2026,11,24),date(2026,11,24),0)['counts']=={'archived':1}
