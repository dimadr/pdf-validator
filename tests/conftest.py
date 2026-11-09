import os,uuid,hashlib
from pathlib import Path
from datetime import datetime,timezone
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from common.models import Base,EmailSource,IncomingMessage,Attachment,Object,ProcessingRevision

@pytest.fixture(scope='session')
def core_engine():
    raw=os.getenv('TEST_DATABASE_URL')
    if not raw:pytest.skip('TEST_DATABASE_URL required: real isolated PostgreSQL acceptance')
    url=make_url(raw)
    assert url.host in ('127.0.0.1','localhost','postgres') and url.database.startswith(('audit_','test_','project1_test'))
    engine=create_engine(url,pool_pre_ping=True,pool_size=8,max_overflow=2)
    with engine.connect() as db:
        assert db.execute(text('SELECT current_database()')).scalar()==url.database
        assert db.execute(text('SHOW server_version_num')).scalar().startswith('15')
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()

@pytest.fixture
def session_factory(core_engine,monkeypatch):
    monkeypatch.setenv('PIPELINE_V2_ENABLED','true')
    monkeypatch.setenv('ADMIN_EMAIL','admin@example.invalid');monkeypatch.setenv('NOTIFY_EMAIL','notify@example.invalid')
    with core_engine.begin() as db:
        db.execute(text('TRUNCATE '+','.join('"'+t.name+'"' for t in Base.metadata.sorted_tables)+' CASCADE'))
    return sessionmaker(bind=core_engine,expire_on_commit=False)

@pytest.fixture
def seed(session_factory,tmp_path):
    def create(state='validated',recipients='one@example.invalid,two@example.invalid',active=True):
        with session_factory() as db:
            with db.begin():
                from sqlalchemy import select
                source=db.scalar(select(EmailSource).where(EmailSource.email=='noreply@eldis24.ru'))
                if source is None:source=EmailSource(id=uuid.uuid4(),email='noreply@eldis24.ru',name='Synthetic source',is_active=True)
                obj=Object(id=uuid.uuid4(),name='Synthetic object',name_norm='synthetic '+str(uuid.uuid4()),calculator_number=str(uuid.uuid4())[:8],email=recipients,is_active=active)
                db.add_all([source,obj]);db.flush()
                message=IncomingMessage(id=uuid.uuid4(),provider_message_id='<'+str(uuid.uuid4())+'@example.invalid>',source_id=source.id,
                    from_email=source.email,subject='Synthetic report',received_at=datetime.now(timezone.utc),status='new')
                db.add(message);db.flush()
                content=b'%PDF-synthetic-'+uuid.uuid4().bytes;path=tmp_path/(str(uuid.uuid4())+'.pdf');path.write_bytes(content)
                attachment=Attachment(id=uuid.uuid4(),message_id=message.id,object_id=obj.id,calculator_number=obj.calculator_number,
                    filename='synthetic.pdf',file_path=str(path),file_sha256=hashlib.sha256(content).hexdigest(),file_size=len(content),status='sent',sent_to_email='historical@example.invalid')
                db.add(attachment);db.flush()
                revision=ProcessingRevision(id=uuid.uuid4(),attachment_id=attachment.id,request_key=str(uuid.uuid4()),request_hash='a'*64,
                    state=state,file_path=str(path),content_sha256=attachment.file_sha256,object_id=obj.id,calculator_number=obj.calculator_number,validation_result={})
                db.add(revision);db.flush();attachment.current_revision_id=revision.id
                return {'attachment':attachment.id,'revision':revision.id,'object':obj.id,'serial':obj.calculator_number,'path':path,'source':source.id}
    return create
