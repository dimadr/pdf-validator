import uuid,email,hashlib
from email.message import EmailMessage
from datetime import timedelta
from sqlalchemy import select,func
from common.models import Object,ProcessingRevision,RecipientDelivery,OutboxEvent,EmailSource,IncomingMessage,Attachment,MailboxCheckpoint
from common.pipeline import validate_revision,discard_content
from common.delivery import deliver_recipient,now
from common.ingest import BlobStore,ingest_uid,poll,recover_incomplete,reconcile_files

class SMTP:
    def __init__(self):self.calls=[]
    def send(self,work):self.calls.append(work['recipient'])

def analysis(serial,state='validated',identity='unique'):
    return {'state':state,'identity_state':identity,'calculator_number':serial,'adapter_version':'synthetic','checked_rows':30}

def test_duplicate_and_old_finalizer_never_reopen(session_factory,seed):
    record=seed(state='received');calls=[]
    def parse(*a,**kw):calls.append(1);return analysis(record['serial'])
    assert validate_revision(session_factory,record['revision'],parse)['state']=='validated'
    assert validate_revision(session_factory,record['revision'],parse)['state']=='skipped'
    with session_factory() as db:
        delivery=db.scalar(select(RecipientDelivery.id).where(RecipientDelivery.role=='client'))
    smtp=SMTP();deliver_recipient(session_factory,delivery,smtp)
    assert validate_revision(session_factory,record['revision'],parse)['state']=='skipped' and calls==[1]

def test_ambiguous_identity_has_zero_client_effect(session_factory,seed):
    record=seed(state='received')
    assert validate_revision(session_factory,record['revision'],lambda *a,**k:analysis(None,identity='ambiguous'))['state']=='review_required'
    with session_factory() as db:
        deliveries=list(db.scalars(select(RecipientDelivery)))
    assert deliveries and all(d.role=='admin' for d in deliveries)
    smtp=SMTP()
    for delivery in deliveries:deliver_recipient(session_factory,delivery.id,smtp)
    assert set(smtp.calls)=={'admin@example.invalid','notify@example.invalid'}

def test_prefix_never_matches_another_device(session_factory,seed):
    record=seed(state='received')
    with session_factory() as db:
        with db.begin():db.get(Object,record['object']).calculator_number='1234-01'
    result=validate_revision(session_factory,record['revision'],lambda *a,**k:analysis('1234-99'))
    assert result['state']=='review_required'
    with session_factory() as db:assert db.scalar(select(func.count()).select_from(RecipientDelivery).where(RecipientDelivery.role=='client'))==0

def test_inactive_discard_is_terminal_even_after_activation(session_factory,seed):
    record=seed(state='received',active=False)
    assert validate_revision(session_factory,record['revision'],lambda *a,**k:analysis(record['serial']))['state']=='discarded_inactive'
    assert discard_content(session_factory,record['revision'])['state']=='discarded_inactive' and not record['path'].exists()
    with session_factory() as db:
        with db.begin():db.get(Object,record['object']).is_active=True
    assert validate_revision(session_factory,record['revision'],lambda *a,**k:analysis(record['serial']))['state']=='skipped'
    with session_factory() as db:assert db.scalar(select(func.count()).select_from(RecipientDelivery))==0

def test_validation_technical_failures_bounded(session_factory,seed):
    record=seed(state='received')
    def broken(*a,**k):raise OSError('Synthetic extraction failure')
    for _ in range(4):assert validate_revision(session_factory,record['revision'],broken)['state']=='technical_error'
    with session_factory() as db:
        revision=db.get(ProcessingRevision,record['revision'])
        assert revision.validation_result['attempts']==3
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==2
        assert db.scalar(select(func.count()).select_from(RecipientDelivery))==0

def raw_pdf(subject='Synthetic',contents=(b'%PDF-one',b'%PDF-two'),message_id=None):
    message=EmailMessage();message['From']='noreply@eldis24.ru';message['To']='processor@example.invalid';message['Subject']=subject
    message['Date']='Mon, 01 Jun 2020 00:00:00 +0000' # Backfill must not depend on a one-day Date window.
    if message_id:message['Message-ID']=message_id
    message.set_content('Synthetic report')
    for index,content in enumerate(contents):message.add_attachment(content,maintype='application',subtype='pdf',filename=str(index)+'.pdf')
    return message.as_bytes()

def trusted(factory):
    with factory() as db:
        with db.begin():db.add(EmailSource(id=uuid.uuid4(),email='noreply@eldis24.ru',name='Synthetic source',is_active=True))

def test_uid_identity_without_message_id_and_backfill(session_factory,tmp_path):
    trusted(session_factory);store=BlobStore(tmp_path/'content')
    class Source:
        def select(self,box):return 7
        def uids_after(self,uid):return [v for v in [1,2] if v>uid]
        def fetch(self,uid):return raw_pdf(contents=(b'%PDF-'+str(uid).encode(),))
    assert poll(session_factory,Source(),store,'account')['processed']==2
    assert poll(session_factory,Source(),store,'account')['processed']==0
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(IncomingMessage))==2
        assert db.scalar(select(func.count()).select_from(Attachment))==2
        assert db.scalar(select(MailboxCheckpoint.last_uid))==2

def test_partial_write_recovery_does_not_lose_later_mail(session_factory,tmp_path):
    trusted(session_factory)
    class Store(BlobStore):
        fail=True
        def put(self,content,suffix):
            if content==b'%PDF-two' and self.fail:raise OSError('Synthetic disk failure')
            return super().put(content,suffix)
    store=Store(tmp_path/'content')
    class Source:
        def select(self,box):return 8
        def uids_after(self,uid):return [v for v in [1,2] if v>uid]
        def fetch(self,uid):return raw_pdf(contents=(b'%PDF-one',b'%PDF-two')) if uid==1 else raw_pdf(contents=(b'%PDF-three',))
    assert poll(session_factory,Source(),store,'account')['processed']==2
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Attachment))==2
        assert db.scalar(select(func.count()).select_from(IncomingMessage).where(IncomingMessage.ingest_state=='incomplete'))==1
    store.fail=False;assert recover_incomplete(session_factory,store)==1
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Attachment))==3
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==3

def test_uidvalidity_reset_keeps_both_envelopes_no_duplicate_content_send(session_factory,tmp_path):
    trusted(session_factory);store=BlobStore(tmp_path/'content');raw=raw_pdf(contents=(b'%PDF-same',),message_id='<same@example.invalid>')
    ingest_uid(session_factory,store,'account','INBOX',10,1,raw)
    ingest_uid(session_factory,store,'account','INBOX',11,1,raw)
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(IncomingMessage))==2
        assert db.scalar(select(func.count()).select_from(Attachment))==1
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==1

def test_unknown_charset_and_read_subject_keep_pdf(session_factory,tmp_path):
    trusted(session_factory);store=BlobStore(tmp_path/'content')
    raw=raw_pdf(subject='Read: ordinary PDF reply',contents=(b'%PDF-ordinary',))
    raw=raw.replace(b'Subject: Read: ordinary PDF reply',b'Subject: =?unknown-audit-charset?b?UmVhZDogUERG?=')
    result=ingest_uid(session_factory,store,'account','INBOX',1,1,raw)
    assert result['state']=='complete'
    with session_factory() as db:assert db.scalar(select(func.count()).select_from(Attachment))==1
    assert not reconcile_files(session_factory,store)['missing']

def test_backfill_does_not_resurrect_discarded_or_missing_legacy_content(session_factory,seed,tmp_path):
    record=seed(state='discarded_inactive');content=record['path'].read_bytes();record['path'].unlink()
    store=BlobStore(tmp_path/'content')
    result=ingest_uid(session_factory,store,'account','INBOX',1,1,raw_pdf(contents=(content,)))
    assert result['state']=='complete' and not list(store.root.glob('*.pdf'))
    with session_factory() as db:
        received=db.scalar(select(IncomingMessage).where(IncomingMessage.imap_account=='account'))
        assert received.part_manifest[0]['state']=='discarded_inactive'
        assert db.scalar(select(func.count()).select_from(OutboxEvent))==0
        assert db.scalar(select(func.count()).select_from(RecipientDelivery))==0

def test_parser_crashes_consume_durable_attempts(session_factory,seed):
    import pytest
    from common.delivery import reconcile
    record=seed(state='received')
    class Crash(BaseException):pass
    def crash(work):raise Crash()
    for attempt in range(1,4):
        with pytest.raises(Crash):validate_revision(session_factory,record['revision'],after_claim=crash)
        with session_factory() as db:
            with db.begin():
                revision=db.get(ProcessingRevision,record['revision'])
                assert revision.validation_attempts==attempt and revision.state=='extracting'
                revision.lease_until=now()-timedelta(seconds=1)
        reconcile(session_factory)
    assert validate_revision(session_factory,record['revision'])['reason']=='validation_retry_exhausted'

def test_malformed_recipient_is_durable_review(session_factory,seed):
    record=seed(state='received',recipients=' , ')
    result=validate_revision(session_factory,record['revision'],lambda *a,**k:analysis(record['serial']))
    assert result['state']=='review_required'
    with session_factory() as db:
        assert db.get(ProcessingRevision,record['revision']).reason=='invalid_client_recipients'
        assert db.scalar(select(func.count()).select_from(RecipientDelivery).where(RecipientDelivery.role=='client'))==0

def test_legacy_cutover_requires_explicit_uid_no_historical_send(session_factory,seed,tmp_path,monkeypatch):
    record=seed()
    with session_factory() as db:
        with db.begin():db.get(Attachment,record['attachment']).current_revision_id=None
    class Source:
        def select(self,box):return 1
        def uids_after(self,cursor):return []
    monkeypatch.delenv('IMAP_BOOTSTRAP_UID',raising=False)
    assert poll(session_factory,Source(),BlobStore(tmp_path/'content'),'account')['state']=='bootstrap_required'
    monkeypatch.setenv('IMAP_BOOTSTRAP_UID','900')
    assert poll(session_factory,Source(),BlobStore(tmp_path/'content'),'account')['state']=='complete'
    with session_factory() as db:assert db.scalar(select(MailboxCheckpoint.last_uid))==900


def test_uid_barrier_before_during_after_switch_is_exactly_once(session_factory,seed,tmp_path,monkeypatch):
    record=seed();legacy=record['path'].read_bytes()
    with session_factory() as db:
        with db.begin():db.get(Attachment,record['attachment']).current_revision_id=None
    class Source:
        mails={1:raw_pdf(contents=(legacy,),message_id='<before@example.invalid>')}
        def select(self,box):return 77
        def uids_after(self,cursor):return [uid for uid in self.mails if uid>cursor]
        def fetch(self,uid):return self.mails[uid]
    source=Source();store=BlobStore(tmp_path/'cutover-content')
    # Captured before pausing the old producer. No historical backfill.
    monkeypatch.setenv('IMAP_BOOTSTRAP_UID','1')
    source.mails[2]=raw_pdf(contents=(b'%PDF-during-pause',),message_id='<during@example.invalid>')
    assert poll(session_factory,source,store,'cutover')['processed']==1
    source.mails[3]=raw_pdf(contents=(b'%PDF-after-switch',),message_id='<after@example.invalid>')
    assert poll(session_factory,source,store,'cutover')['processed']==1
    assert poll(session_factory,source,store,'cutover')['processed']==0
    with session_factory() as db:
        revisions=list(db.scalars(select(ProcessingRevision).join(Attachment,Attachment.current_revision_id==ProcessingRevision.id)))
        assert len(revisions)==2
        assert db.scalar(select(MailboxCheckpoint.last_uid))==3
        assert db.get(Attachment,record['attachment']).status=='sent'
    for revision in revisions:assert validate_revision(session_factory,revision.id,lambda *a,**k:analysis(record['serial']))['state']=='validated'
    with session_factory() as db:deliveries=list(db.scalars(select(RecipientDelivery)))
    smtp=SMTP()
    for delivery in deliveries:
        assert deliver_recipient(session_factory,delivery.id,smtp)['state']=='accepted'
        assert deliver_recipient(session_factory,delivery.id,smtp)['state']=='skipped'
    assert len(deliveries)==4 and len(smtp.calls)==4
