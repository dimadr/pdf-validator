"""Real PostgreSQL migrations and HTTP API. SMTP/Redis effects are not invoked."""
import asyncio
import hashlib
import os
from pathlib import Path
import secrets
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import uuid

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text, func
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from common.models import (Base,Attachment,EmailSource,IncomingMessage,Object,
    ProcessingRevision,DeliveryIntent,RecipientDelivery,OutboxEvent)

ROOT=Path(__file__).resolve().parents[1]


def migrate(engine,target='head'):
    config=Config(str(ROOT/'backend/alembic.ini'))
    config.set_main_option('script_location',str(ROOT/'backend/alembic'))
    with engine.begin() as connection:
        config.attributes['connection']=connection
        command.upgrade(config,target)


@pytest.fixture(scope='module')
def api_databases():
    raw=os.getenv('TEST_DATABASE_URL')
    if not raw:
        pytest.skip('TEST_DATABASE_URL required: isolated PostgreSQL 15 acceptance')
    url=make_url(raw)
    assert url.host in ('127.0.0.1','localhost','postgres')
    assert url.database.startswith(('audit_','test_','project1_test'))
    admin=create_engine(url,isolation_level='AUTOCOMMIT')
    suffix=uuid.uuid4().hex[:10]
    names={k:'audit_rc_api_'+suffix+'_'+k for k in ('runtime','clean','captured','broken')}
    engines={}
    with admin.connect() as connection:
        assert connection.execute(text('SELECT current_database()')).scalar()==url.database
        assert connection.execute(text('SHOW server_version_num')).scalar().startswith('15')
        for name in names.values():
            connection.execute(text('CREATE DATABASE "'+name+'"'))
    try:
        for key,name in names.items():
            engines[key]=create_engine(url.set(database=name),pool_size=5,max_overflow=2)
        migrate(engines['runtime'])
        yield engines
    finally:
        for engine in engines.values():
            engine.dispose()
        with admin.connect() as connection:
            for name in names.values():
                connection.execute(text('DROP DATABASE "'+name+'" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def api(api_databases,monkeypatch):
    engine=api_databases['runtime']
    monkeypatch.setenv('DATABASE_URL',engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv('SECRET_KEY',secrets.token_hex(32))
    monkeypatch.setenv('PIPELINE_V2_ENABLED','true')
    monkeypatch.setenv('REDIS_URL','memory://')
    import backend.database as database
    from backend.main import app
    from backend.routers.auth import get_current_user
    database.engine.dispose()
    monkeypatch.setattr(database,'engine',engine)
    monkeypatch.setattr(database,'SessionLocal',sessionmaker(bind=engine,expire_on_commit=False))
    with engine.begin() as connection:
        connection.execute(text('TRUNCATE '+','.join('"'+t.name+'"' for t in Base.metadata.tables.values())+' CASCADE'))
    user=SimpleNamespace(id=uuid.uuid4(),role='admin',is_active=True,username='synthetic-admin')
    app.dependency_overrides[get_current_user]=lambda:user
    def request(method,path,**kwargs):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://audit.invalid') as client:
                return await client.request(method,path,**kwargs)
        return asyncio.run(run())
    yield SimpleNamespace(request=request,engine=engine,user=user,app=app,auth=get_current_user)
    app.dependency_overrides.clear()


def seed(engine,tmp_path,validated=False,active=True):
    content=b'%PDF synthetic '+uuid.uuid4().bytes
    path=tmp_path/(uuid.uuid4().hex+'.pdf');path.write_bytes(content)
    with Session(engine) as db:
        source=EmailSource(email=uuid.uuid4().hex+'@example.com',is_active=True)
        obj=Object(name='Synthetic object',name_norm='synthetic '+uuid.uuid4().hex,
                   calculator_number=uuid.uuid4().hex[:12],email='receiver@example.com',is_active=active)
        db.add_all([source,obj]);db.flush()
        message=IncomingMessage(source_id=source.id,provider_message_id=uuid.uuid4().hex,
                                from_email=source.email,status='new')
        db.add(message);db.flush()
        attachment=Attachment(message_id=message.id,object_id=obj.id,filename='fixture.pdf',
            file_path=str(path),file_sha256=hashlib.sha256(content).hexdigest(),status='sent',
            calculator_number=obj.calculator_number,sent_to_email='historical@example.com')
        db.add(attachment);db.flush()
        revision=None
        if validated:
            revision=ProcessingRevision(attachment_id=attachment.id,request_key=uuid.uuid4().hex,
                request_hash='a'*64,state='validated',content_sha256=attachment.file_sha256,
                file_path=str(path),object_id=obj.id,calculator_number=obj.calculator_number)
            db.add(revision);db.flush();attachment.current_revision_id=revision.id
        ids={'attachment':str(attachment.id),'object':str(obj.id),'source':str(source.id),
             'revision':str(revision.id) if revision else None,'path':path}
        db.commit();return ids


def test_clean_and_captured_upgrade_schema_parity(api_databases):
    clean,captured=api_databases['clean'],api_databases['captured']
    migrate(clean)
    ddl=(ROOT/'tests/fixtures/legacy-production-schema.sql').read_text(encoding='utf-8')
    with captured.begin() as connection:
        connection.exec_driver_sql(ddl)
        connection.execute(text('SET search_path TO public'))
        connection.execute(text("INSERT INTO alembic_version(version_num) VALUES ('0004')"))
        connection.execute(text("INSERT INTO objects(id,name,name_norm,email,calculator_number) VALUES (gen_random_uuid(),'Legacy','shared','legacy@example.com','legacy-1')"))
    migrate(captured)
    for engine in (clean,captured):
        with engine.begin() as connection:
            assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar()=='0010'
            for i in range(2):
                connection.execute(text("INSERT INTO objects(id,name,name_norm,calculator_number) VALUES (gen_random_uuid(),'Branch','same-name',:serial)"),{'serial':str(uuid.uuid4())})
            diffs=compare_metadata(MigrationContext.configure(connection,opts={'compare_type':True}),Base.metadata)
            assert diffs==[],repr(diffs)
    with captured.connect() as connection:
        assert connection.execute(text("SELECT email FROM objects WHERE calculator_number='legacy-1'")).scalar()=='legacy@example.com'


def test_empty_downgrade_and_guarded_history_rollback(api_databases):
    engine=api_databases['broken'];migrate(engine)
    cfg=Config(str(ROOT/'backend/alembic.ini'));cfg.set_main_option('script_location',str(ROOT/'backend/alembic'))
    # Schema 0008 deliberately retains SMTP provenance even in an empty database.
    with pytest.raises(RuntimeError,match='history'):
        with engine.begin() as connection:
            cfg.attributes['connection']=connection;command.downgrade(cfg,'0005')
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO mailbox_checkpoints(id,account,mailbox,uidvalidity,last_uid) VALUES(gen_random_uuid(),'fixture','INBOX',1,0)"))
    with pytest.raises(RuntimeError,match='history'):
        with engine.begin() as connection:
            cfg.attributes['connection']=connection;command.downgrade(cfg,'0005')
    with engine.connect() as connection:
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar()=='0010'
        assert connection.execute(text('SELECT count(*) FROM mailbox_checkpoints')).scalar()==1


def test_readiness_and_disabled_manual_gate(api,monkeypatch,tmp_path):
    assert api.request('GET','/health/ready').status_code==200
    item=seed(api.engine,tmp_path)
    monkeypatch.setenv('PIPELINE_V2_ENABLED','false')
    response=api.request('POST',f"/api/v1/attachments/{item['attachment']}/resend",headers={'Idempotency-Key':uuid.uuid4().hex})
    assert response.status_code==503
    with Session(api.engine) as db:assert db.scalar(select(func.count()).select_from(OutboxEvent))==0


def test_ai_http_switch_strict_admin_and_persistent_without_restart(api,monkeypatch):
    from common.models import AISettingAudit,AIControl
    monkeypatch.setenv('ROUTERAI_API_KEY','synthetic-secret-never-return')
    path='/api/v1/settings/ai-fallback'
    assert api.request('GET',path).json()['requested'] is False
    assert api.request('PUT',path,json={'enabled':True}).json()['effective_ai'] is True
    assert api.request('GET',path).json()['requested'] is True
    assert api.request('PUT',path,json={'enabled':'true'}).status_code==422
    api.user.role='operator'
    assert api.request('PUT',path,json={'enabled':False}).status_code==403
    api.user.role='admin'
    assert api.request('PUT',path,json={'enabled':False}).json()['effective_ai'] is False
    assert 'synthetic-secret' not in api.request('GET',path).text
    with Session(api.engine) as db:
        assert db.scalar(select(func.count()).select_from(AISettingAudit))==2
        assert not db.get(AIControl,1).enabled


def test_schema_missing_returns_503_even_with_flag_off(api,monkeypatch):
    monkeypatch.setenv('PIPELINE_V2_ENABLED','false')
    with api.engine.begin() as connection:connection.execute(text('ALTER TABLE attachments RENAME COLUMN current_revision_id TO hidden_revision_id'))
    try:
        assert api.request('GET','/health/ready').status_code==503
    finally:
        with api.engine.begin() as connection:connection.execute(text('ALTER TABLE attachments RENAME COLUMN hidden_revision_id TO current_revision_id'))


def test_reprocess_idempotency_stale_revision_and_history(api,tmp_path):
    item=seed(api.engine,tmp_path);key=uuid.uuid4().hex
    path=f"/api/v1/attachments/{item['attachment']}/reprocess"
    first=api.request('POST',path,headers={'Idempotency-Key':key})
    again=api.request('POST',path,headers={'Idempotency-Key':key})
    assert first.status_code==again.status_code==202,(first.text,again.text)
    assert first.json()['revision_id']==again.json()['revision_id']
    with Session(api.engine) as db:
        attachment=db.get(Attachment,uuid.UUID(item['attachment']))
        revision=db.get(ProcessingRevision,attachment.current_revision_id)
        assert attachment.sent_to_email=='historical@example.com' and attachment.object_id is not None
        assert revision.object_id is None and revision.calculator_number is None
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==1
    stale=api.request('POST',path,headers={'Idempotency-Key':uuid.uuid4().hex},json={'expected_revision_id':str(uuid.uuid4())})
    assert stale.status_code==409
    missing=api.request('POST',path)
    assert missing.status_code==422


def test_resend_preserves_accepted_history_and_per_recipient_view(api,tmp_path):
    from common.delivery import plan_intent
    item=seed(api.engine,tmp_path,validated=True)
    with Session(api.engine) as db:
        revision=db.get(ProcessingRevision,uuid.UUID(item['revision']))
        old=plan_intent(db,revision,[('receiver@example.com','client')],uuid.uuid4().hex)
        db.flush()
        recipient=db.scalar(select(RecipientDelivery).where(RecipientDelivery.intent_id==old.id))
        recipient.state='accepted';old_id=recipient.id;old_message_id=recipient.message_id;db.commit()
    key=uuid.uuid4().hex;path=f"/api/v1/attachments/{item['attachment']}/resend"
    result=api.request('POST',path,headers={'Idempotency-Key':key},json={'expected_revision_id':item['revision']})
    repeat=api.request('POST',path,headers={'Idempotency-Key':key},json={'expected_revision_id':item['revision']})
    assert result.status_code==repeat.status_code==202,(result.text,repeat.text)
    assert result.json()['intent_id']==repeat.json()['intent_id']
    with Session(api.engine) as db:
        old=db.get(RecipientDelivery,old_id)
        assert old.state=='accepted' and old.message_id==old_message_id
        assert db.get(Attachment,uuid.UUID(item['attachment'])).sent_to_email=='historical@example.com'
        assert db.scalar(select(func.count()).select_from(DeliveryIntent))==2
    page=api.request('GET',f"/api/v1/attachments/{item['attachment']}/deliveries")
    assert page.status_code==200 and page.json()['total']==2
    assert {row['state'] for row in page.json()['items']}=={'accepted','pending'}
    details=api.request('GET',f"/api/v1/attachments/{item['attachment']}/details")
    assert details.status_code==200
    assert details.json()['current_revision']['object_name']=='Synthetic object'
    assert details.json()['sent_to_email']=='historical@example.com'
    assert details.json()['recipient_state_counts']['client']=={'accepted':1,'pending':1}


def test_inactive_and_missing_content_cannot_enqueue_client_resend(api,tmp_path):
    inactive=seed(api.engine,tmp_path,validated=True,active=False)
    missing=seed(api.engine,tmp_path,validated=True);missing['path'].unlink()
    for item in (inactive,missing):
        response=api.request('POST',f"/api/v1/attachments/{item['attachment']}/resend",headers={'Idempotency-Key':uuid.uuid4().hex})
        assert response.status_code==409
    with Session(api.engine) as db:assert db.scalar(select(func.count()).select_from(RecipientDelivery))==0


def test_batch_deduplicates_ids_and_keeps_per_item_results(api,tmp_path):
    item=seed(api.engine,tmp_path)
    response=api.request('POST','/api/v1/attachments/batch-reprocess',
        headers={'Idempotency-Key':uuid.uuid4().hex},json=[item['attachment'],item['attachment'],str(uuid.uuid4())])
    assert response.status_code==202,response.text
    assert response.json()['processed']==1 and response.json()['errors']==1
    assert response.json()['duplicate_ids']==1


def test_input_source_integrity_and_env_mail_config(api,tmp_path):
    assert api.request('POST','/api/v1/objects/',json={'name':'','email':'not-an-address'}).status_code==422
    valid=api.request('POST','/api/v1/objects/',json={'name':'Object','calculator_number':'442E','email':'one@example.com, two@example.org'})
    assert valid.status_code==200,valid.text
    item=seed(api.engine,tmp_path)
    assert api.request('PUT',f"/api/v1/objects/{item['object']}",json={'is_active':None}).status_code==422
    for invalid in ({'email':None},{'is_active':None}):
        assert api.request('PUT',f"/api/v1/email-sources/{item['source']}",json=invalid).status_code==422
    response=api.request('DELETE',f"/api/v1/email-sources/{item['source']}")
    assert response.status_code==409
    assert api.request('PUT','/api/v1/settings/email-config',json={'smtp_server':'unused.example.com'}).status_code==409
    config=api.request('GET','/api/v1/settings/email-config').json()
    assert config['editable'] is False and config['managed_by']=='deployment_environment'


def test_source_list_reads_inactive_canary_history_without_relaxing_input_validation(api):
    addresses=['noreply@eldis24.ru','eldis@example.com','dv@example.com','source-4@example.com']
    canaries=['canary-history@manual-canary.invalid','brevo-canary-history@manual-canary.invalid']
    with Session(api.engine) as db:
        db.add_all([EmailSource(email=value,name='Existing source',is_active=True) for value in addresses])
        db.add_all([EmailSource(email=value,name='Inactive canary history',is_active=False) for value in canaries]);db.commit()
        before=[(str(s.id),s.email,s.is_active) for s in db.scalars(select(EmailSource).order_by(EmailSource.email))]
    response=api.request('GET','/api/v1/email-sources/')
    assert response.status_code==200,response.text
    rows=response.json();assert len(rows)==6
    assert {r['email'] for r in rows}==set(addresses+canaries)
    assert all(not r['is_active'] for r in rows if r['email'] in canaries)
    canary=next(r for r in rows if r['email']==canaries[0])
    detail=api.request('GET',f"/api/v1/email-sources/{canary['id']}")
    assert detail.status_code==200 and detail.json()['email']==canaries[0]
    for value in ('not-an-address','new-canary@manual-canary.invalid'):
        assert api.request('POST','/api/v1/email-sources/',json={'email':value}).status_code==422
        assert api.request('PUT',f"/api/v1/email-sources/{canary['id']}",json={'email':value}).status_code==422
    with Session(api.engine) as db:
        after=[(str(s.id),s.email,s.is_active) for s in db.scalars(select(EmailSource).order_by(EmailSource.email))]
    assert before==after


def test_document_list_includes_processed_read_receipt(api,tmp_path):
    from datetime import datetime

    item=seed(api.engine,tmp_path,validated=True)
    receipt_at=datetime(2026,11,9,8,23,55)
    with Session(api.engine) as db:
        attachment=db.get(Attachment,uuid.UUID(item['attachment']))
        attachment.read_receipt_received=True
        attachment.read_receipt_at=receipt_at
        db.commit()

    response=api.request('GET','/api/v1/attachments',params={'search':'fixture.pdf'})
    assert response.status_code==200,response.text
    row=next(row for row in response.json() if row['id']==item['attachment'])
    assert row['read_receipt_received'] is True
    assert row['read_receipt_at']==receipt_at.isoformat()


def test_deactivation_rejects_inflight_and_does_not_rewrite_recipient(api,tmp_path):
    from common.delivery import plan_intent
    item=seed(api.engine,tmp_path,validated=True)
    with Session(api.engine) as db:
        revision=db.get(ProcessingRevision,uuid.UUID(item['revision']))
        intent=plan_intent(db,revision,[('receiver@example.com','client')],uuid.uuid4().hex);db.flush()
        delivery=db.scalar(select(RecipientDelivery).where(RecipientDelivery.intent_id==intent.id))
        delivery.state='in_flight';delivery_id=delivery.id;db.commit()
    assert api.request('PUT',f"/api/v1/objects/{item['object']}",json={'is_active':False}).status_code==409
    assert api.request('PUT',f"/api/v1/objects/{item['object']}",json={'email':'future@example.com'}).status_code==200
    with Session(api.engine) as db:
        assert db.get(Object,uuid.UUID(item['object'])).is_active is True
        assert db.get(RecipientDelivery,delivery_id).recipient=='receiver@example.com'


def test_pagination_and_admin_boundary(api):
    with Session(api.engine) as db:
        db.add_all([Object(name='Branch '+str(i),name_norm='branch '+str(i),calculator_number='page-'+str(i)) for i in range(105)])
        db.commit()
    response=api.request('GET','/api/v1/objects/?skip=100&limit=100')
    assert response.status_code==200 and len(response.json())==5
    assert response.headers['X-Total-Count']=='105'
    api.user.role='operator'
    assert api.request('POST','/api/v1/objects/',json={'name':'Forbidden'}).status_code==403
    api.app.dependency_overrides.clear()
    assert api.request('GET','/api/v1/attachments').status_code==401


def test_concurrent_http_request_key_has_one_revision_and_outbox(api,tmp_path):
    item=seed(api.engine,tmp_path);key=uuid.uuid4().hex
    def send(_):
        return api.request('POST',f"/api/v1/attachments/{item['attachment']}/reprocess",headers={'Idempotency-Key':key})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first,second=list(pool.map(send,range(2)))
    assert first.status_code==second.status_code==202
    assert first.json()['revision_id']==second.json()['revision_id']
    with Session(api.engine) as db:
        assert db.scalar(select(func.count()).select_from(ProcessingRevision))==1
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==1


def test_command_commit_failure_rolls_back_and_same_key_can_retry(api,tmp_path,monkeypatch):
    from sqlalchemy.exc import OperationalError
    import backend.database as database
    item=seed(api.engine,tmp_path);key=uuid.uuid4().hex
    class FailedCommit(Session):
        def commit(self):
            raise OperationalError('synthetic commit failure',None,RuntimeError('inert database failure'))
    monkeypatch.setattr(database,'SessionLocal',sessionmaker(bind=api.engine,class_=FailedCommit))
    path=f"/api/v1/attachments/{item['attachment']}/reprocess"
    failed=api.request('POST',path,headers={'Idempotency-Key':key})
    assert failed.status_code==503
    with Session(api.engine) as db:
        assert db.scalar(select(func.count()).select_from(ProcessingRevision))==0
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==0
        assert db.get(Attachment,uuid.UUID(item['attachment'])).sent_to_email=='historical@example.com'
    monkeypatch.setattr(database,'SessionLocal',sessionmaker(bind=api.engine))
    assert api.request('POST',path,headers={'Idempotency-Key':key}).status_code==202


def test_first_admin_bootstrap_and_password_http_boundaries(api):
    from backend.create_admin import create_initial_admin
    from common.models import User
    password=secrets.token_hex(12)
    with Session(api.engine) as db:
        user=create_initial_admin(db,'first-admin',password)
        db.commit()
        assert user.role=='admin'
        with pytest.raises(ValueError,match='already exists'):
            create_initial_admin(db,'second-admin',password)
        db.rollback()
    api.app.dependency_overrides.clear()
    assert api.request('POST','/api/v1/auth/login',json={'username':'first-admin','password':password}).status_code==200
    assert api.request('POST','/api/v1/auth/login',json={'username':'first-admin','password':'x'*73}).status_code==401
    api.app.dependency_overrides[api.auth]=lambda:api.user
    assert api.request('POST','/api/v1/auth/register',json={'username':'long-password','password':'я'*37}).status_code==422
    with Session(api.engine) as db:
        assert db.scalar(select(func.count()).select_from(User))==1


def test_v2_current_outcomes_reach_reports_filters_and_csv_without_mutating_legacy(api,tmp_path):
    valid=seed(api.engine,tmp_path,validated=True)
    rejected=seed(api.engine,tmp_path,validated=True)
    review=seed(api.engine,tmp_path,validated=True)
    received=seed(api.engine,tmp_path)
    legacy_sent=seed(api.engine,tmp_path)
    legacy_rejected=seed(api.engine,tmp_path)
    with Session(api.engine) as db:
        for item in (valid,rejected,review,received):
            db.get(Attachment,uuid.UUID(item['attachment'])).status='new'
        db.get(Attachment,uuid.UUID(valid['attachment'])).reject_reason='stale_legacy_error'
        denied=db.get(ProcessingRevision,uuid.UUID(rejected['revision']))
        denied.state='rejected';denied.reason='empty_cells';denied.validation_result={'checked_rows':3}
        # The report must resolve the current revision's object, not stale legacy identity.
        db.get(Attachment,uuid.UUID(rejected['attachment'])).object_id=None
        ambiguous=db.get(ProcessingRevision,uuid.UUID(review['revision']))
        ambiguous.state='review_required';ambiguous.reason='ambiguous_identity'
        old=db.get(Attachment,uuid.UUID(legacy_rejected['attachment']))
        old.status='rejected';old.reject_reason='legacy_error'
        db.commit()
    result=api.request('POST',f"/api/v1/attachments/{received['attachment']}/reprocess",
        headers={'Idempotency-Key':uuid.uuid4().hex})
    assert result.status_code==202
    summary=api.request('GET','/api/v1/reports/summary')
    assert summary.status_code==200,summary.text
    assert summary.json()=={'total_attachments':6,'processed':5,'rejected':2,'sent':1,
        'smtp_accepted':0,'legacy_sent':1,'rejected_by_reason':{'empty_cells':1,'legacy_error':1}}
    reports=api.request('GET','/api/v1/reports/rejections')
    assert reports.status_code==200,reports.text
    assert reports.headers['X-Total-Count']=='2'
    by_id={row['id']:row for row in reports.json()}
    assert set(by_id)=={rejected['attachment'],legacy_rejected['attachment']}
    assert by_id[rejected['attachment']]['reject_reason']=='empty_cells'
    assert by_id[rejected['attachment']]['reject_details']=={'checked_rows':3}
    assert by_id[rejected['attachment']]['object_name']=='Synthetic object'
    filtered=api.request('GET','/api/v1/reports/rejections?reject_reason=empty_cells')
    assert [row['id'] for row in filtered.json()]==[rejected['attachment']]
    csv=api.request('GET','/api/v1/reports/rejections.csv')
    assert csv.status_code==200 and rejected['attachment'] in csv.text and 'empty_cells' in csv.text
    assert valid['attachment'] not in csv.text
    expected={'validated':{valid['attachment']},'rejected':{rejected['attachment'],legacy_rejected['attachment']},
              'review_required':{review['attachment']},'new':{received['attachment']}}
    for state,ids in expected.items():
        response=api.request('GET','/api/v1/attachments?status='+state)
        assert response.status_code==200,response.text
        assert {row['id'] for row in response.json()}==ids
    current=api.request('GET',f"/api/v1/attachments/{valid['attachment']}").json()
    assert current['status']=='new' and current['effective_status']=='validated'
    assert current['reject_reason']=='stale_legacy_error'
    days=api.request('GET','/api/v1/reports/processing-stats')
    assert days.status_code==200,days.text
    for key,expected_total in [('total',6),('processed',5),('rejected',2),('smtp_accepted',0),('legacy_sent',1)]:
        assert sum(day[key] for day in days.json()['daily_stats'].values())==expected_total
    with Session(api.engine) as db:
        assert db.get(Attachment,uuid.UUID(valid['attachment'])).status=='new'
        assert db.get(Attachment,uuid.UUID(valid['attachment'])).reject_reason=='stale_legacy_error'


def test_smtp_summary_requires_all_clients_in_latest_current_intent_and_ignores_admin(api,tmp_path):
    from common.delivery import plan_intent
    client=seed(api.engine,tmp_path,validated=True)
    admin=seed(api.engine,tmp_path,validated=True)
    with Session(api.engine) as db:
        for item in (client,admin):db.get(Attachment,uuid.UUID(item['attachment'])).status='new'
        revision=db.get(ProcessingRevision,uuid.UUID(client['revision']))
        partial=plan_intent(db,revision,[('one@example.com','client'),('two@example.com','client')],uuid.uuid4().hex)
        db.flush()
        deliveries=list(db.scalars(select(RecipientDelivery).where(RecipientDelivery.intent_id==partial.id).order_by(RecipientDelivery.recipient)))
        deliveries[0].state='accepted';deliveries[1].state='failed';old_message_id=deliveries[0].message_id
        administrative=db.get(ProcessingRevision,uuid.UUID(admin['revision']))
        administrative.state='rejected';administrative.reason='test_rejected'
        notice=plan_intent(db,administrative,[('notify@example.com','admin')],uuid.uuid4().hex,kind='admin_notification')
        db.flush();db.scalar(select(RecipientDelivery).where(RecipientDelivery.intent_id==notice.id)).state='accepted'
        db.commit()
    assert api.request('GET','/api/v1/reports/summary').json()['smtp_accepted']==0
    assert api.request('GET','/api/v1/attachments?status=accepted').json()==[]
    with Session(api.engine) as db:
        revision=db.get(ProcessingRevision,uuid.UUID(client['revision']))
        complete=plan_intent(db,revision,[('one@example.com','client'),('two@example.com','client')],uuid.uuid4().hex,kind='manual_resend')
        db.flush()
        for row in db.scalars(select(RecipientDelivery).where(RecipientDelivery.intent_id==complete.id)):row.state='accepted'
        db.commit()
    summary=api.request('GET','/api/v1/reports/summary').json()
    assert summary['smtp_accepted']==1 and summary['sent']==1 and summary['legacy_sent']==0
    accepted=api.request('GET','/api/v1/attachments?status=accepted')
    assert accepted.status_code==200,accepted.text
    assert [row['id'] for row in accepted.json()]==[client['attachment']]
    daily=api.request('GET','/api/v1/reports/processing-stats').json()
    assert sum(row['smtp_accepted'] for row in daily['daily_stats'].values())==1
    # A later pending client intent changes the current aggregate, without rewriting old success.
    with Session(api.engine) as db:
        revision=db.get(ProcessingRevision,uuid.UUID(client['revision']))
        plan_intent(db,revision,[('one@example.com','client')],uuid.uuid4().hex,kind='manual_resend')
        db.commit()
        assert db.scalar(select(RecipientDelivery).where(RecipientDelivery.message_id==old_message_id)).state=='accepted'
    assert api.request('GET','/api/v1/reports/summary').json()['smtp_accepted']==0
    assert api.request('GET','/api/v1/attachments?status=accepted').json()==[]
