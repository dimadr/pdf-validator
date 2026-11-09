"""Seen follows durable completion; no historical reprocessing or SMTP is invoked."""
import importlib
import pytest
from pathlib import Path
from sqlalchemy import select
from common.models import Attachment,IncomingMessage,ProcessingRevision,RecipientDelivery,MailboxCheckpoint
from common.delivery import deliver_recipient
from test_delivery_core import planned,SMTP

def ready(factory,seed):
    record=seed()
    with factory() as db:
        with db.begin():
            a=db.get(Attachment,record['attachment']);m=db.get(IncomingMessage,a.message_id)
            m.imap_account='synthetic';m.imap_mailbox='INBOX';m.imap_uidvalidity=77;m.imap_uid=10;m.ingest_state='complete'
            m.part_manifest=[{'attachment_id':str(a.id),'state':'saved'}]
            db.add(MailboxCheckpoint(account='synthetic',mailbox='INBOX',uidvalidity=77,last_uid=10))
    ids=planned(factory,record,['one@example.invalid','two@example.invalid'])
    for id in ids:deliver_recipient(factory,id,SMTP())
    return record,ids

class Source:
    def __init__(self):self.seen=[];self.unread=[10];self.closed=False
    def select(self,box):return 77
    def uids_after(self,cursor):return []
    def unread_uids(self):return self.unread
    def mark_seen(self,uid,mailbox,uidvalidity):
        assert mailbox=='INBOX' and uidvalidity==77
        self.seen.append(uid);self.unread.remove(uid);return True
    def close(self):self.closed=True

def test_real_poll_entry_marks_completed_unread_mail_without_reprocessing(session_factory,seed,tmp_path,monkeypatch):
    record,ids=ready(session_factory,seed);source=Source()
    import dotenv
    monkeypatch.setattr(dotenv,'load_dotenv',lambda *a,**k:False)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'worker'))
    monkeypatch.setenv('DATABASE_URL',session_factory.kw['bind'].url.render_as_string(hide_password=False))
    monkeypatch.setenv('REDIS_URL','memory://');monkeypatch.setenv('IMAP_USER','synthetic')
    mail=importlib.import_module('email_client');monkeypatch.setattr(mail,'SessionLocal',session_factory);monkeypatch.setattr(mail,'UPLOAD_DIR',str(tmp_path/'uploads'))
    monkeypatch.setattr('common.ingest.IMAPSource',lambda:source)
    monkeypatch.setattr('common.mailbox_archive.archive_mailbox',lambda *a,**k:{'moved_or_confirmed':0,'uncertain':0})
    result=mail.fetch_emails_task.run()
    assert source.seen==[10],'Completed message stays unread'
    assert result['processed']==0 and source.closed
    with session_factory() as db:assert all(db.get(RecipientDelivery,id).attempts==1 for id in ids)

@pytest.mark.parametrize('state',['pending','failed','uncertain','review_required','archived'])
def test_unfinished_failed_review_and_archived_mail_stays_unread(session_factory,seed,state):
    from common.ingest import mark_completed_seen
    record,ids=ready(session_factory,seed);source=Source()
    with session_factory() as db:
        with db.begin():
            if state=='review_required':db.get(ProcessingRevision,record['revision']).state=state
            elif state=='archived':db.get(Attachment,record['attachment']).status=state
            else:db.get(RecipientDelivery,ids[-1]).state=state
    assert mark_completed_seen(session_factory,source,'synthetic')['marked']==0 and not source.seen

def test_all_pdf_parts_must_be_complete_and_store_failure_retries_without_smtp(session_factory,seed):
    from common.ingest import mark_completed_seen
    record,ids=ready(session_factory,seed);other=seed(state='review_required');source=Source()
    with session_factory() as db:
        with db.begin():
            m=db.get(Attachment,record['attachment']).message
            m.part_manifest=[*m.part_manifest,{'attachment_id':str(other['attachment']),'state':'saved'}]
    assert mark_completed_seen(session_factory,source,'synthetic')['marked']==0
    with session_factory() as db:
        with db.begin():db.get(Attachment,record['attachment']).message.part_manifest=[{'attachment_id':str(record['attachment']),'state':'saved'}]
    source.mark_seen=lambda *a:False
    assert mark_completed_seen(session_factory,source,'synthetic')=={'marked':0,'errors':1}
    source=Source();assert mark_completed_seen(session_factory,source,'synthetic')['marked']==1
    assert mark_completed_seen(session_factory,source,'synthetic')['marked']==0
    with session_factory() as db:assert all(db.get(RecipientDelivery,id).attempts==1 for id in ids)

def test_seen_protocol_checks_uidvalidity_and_preserves_other_flags(monkeypatch):
    from common.ingest import IMAPSource
    class Connection:
        def __init__(self):self.calls=[];self.validity=77
        def login(self,*a):pass
        def select(self,box,readonly):self.calls.append(('SELECT',box,readonly));return 'OK',[]
        def response(self,name):return name,[str(self.validity).encode()]
        def uid(self,*args):self.calls.append(args);return 'OK',[]
    monkeypatch.setenv('IMAP_USER','synthetic');monkeypatch.setenv('IMAP_PASSWORD','synthetic')
    connection=Connection();source=IMAPSource(connection)
    assert source.mark_seen(10,'INBOX',77)
    assert ('STORE','10','+FLAGS.SILENT',r'(\Seen)') in connection.calls
    assert connection.calls[-1]==('SELECT','INBOX',True)
    connection.calls=[];connection.validity=88
    with pytest.raises(RuntimeError,match='UIDVALIDITY'):source.mark_seen(10,'INBOX',77)
    assert not any(c[0]=='STORE' for c in connection.calls)

def test_real_imap_store_sets_seen_and_keeps_peek_readonly(monkeypatch):
    import socketserver,imaplib
    from common.ingest import IMAPSource
    from test_local_protocols import running
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b'* OK synthetic IMAP\r\n')
            while True:
                row=self.rfile.readline()
                if not row:return
                self.server.commands.append(row.decode().strip());tag,command,*args=row.split();command=command.upper()
                if command==b'CAPABILITY':self.wfile.write(b'* CAPABILITY IMAP4rev1\r\n'+tag+b' OK capability\r\n')
                elif command==b'LOGIN':self.wfile.write(tag+b' OK login\r\n')
                elif command in (b'SELECT',b'EXAMINE'):
                    self.wfile.write(b'* 1 EXISTS\r\n* OK [UIDVALIDITY 77] valid\r\n'+tag+b' OK selected\r\n')
                elif command==b'UID' and args[0]==b'SEARCH':self.wfile.write(b'* SEARCH'+(b'' if getattr(self.server,'seen',False) else b' 10')+b'\r\n'+tag+b' OK search\r\n')
                elif command==b'UID' and args[0]==b'STORE':
                    assert args[1:]==[b'10',b'+FLAGS.SILENT',b'(\\Seen)']
                    self.server.seen=True;self.wfile.write(tag+b' OK store\r\n')
                elif command==b'LOGOUT':self.wfile.write(b'* BYE\r\n'+tag+b' OK logout\r\n');return
                else:raise AssertionError('Unexpected IMAP command')
    monkeypatch.setenv('IMAP_USER','synthetic');monkeypatch.setenv('IMAP_PASSWORD','synthetic')
    with running(Handler) as server:
        source=IMAPSource(imaplib.IMAP4('127.0.0.1',server.server_address[1],timeout=5))
        try:
            assert source.select('INBOX')==77 and source.unread_uids()==[10]
            assert source.mark_seen(10,'INBOX',77) and source.unread_uids()==[]
        finally:source.close()
        assert sum('UID STORE' in c for c in server.commands)==1
        assert not any('EXPUNGE' in c or 'DELETE' in c or 'MOVE' in c for c in server.commands)
