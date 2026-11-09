import hashlib,pathlib,email,os,types,uuid
import pytest
from sqlalchemy import select
from common.mailbox_archive import queue_move,reconcile_moves,archive_mailbox,PROCESSED,NOTIFICATIONS,folder_name
from common.models import MailboxAction,Attachment,IncomingMessage,OutboxEvent,RecipientDelivery,ProcessingRevision
from common.ingest import BlobStore
from test_pipeline_ingest import raw_pdf
from test_delivery_core import planned,deliver_recipient,SMTP

class Wire:
    def __init__(self,source):self.source=source;self.box='INBOX';self.calls=[];self.moves=0;self.fail_after_move=False
    def select(self,box,readonly=True):
        self.calls.append(('SELECT',box,readonly));self.box=box
        return ('OK',[b'']) if box in self.source.boxes else ('NO',[b'missing'])
    def response(self,name):return name,[str(self.source.validity).encode()]
    def capability(self):return 'OK',[b'IMAP4rev1 UIDPLUS MOVE']
    def list(self,reference,target):return 'OK',[target.encode() if target in self.source.boxes else None]
    def create(self,target):self.source.boxes[target]={};return 'OK',[]
    def uid(self,command,*args):
        self.calls.append((command,*args));messages=self.source.boxes.get(self.box,{})
        if command=='SEARCH':
            ids=list(messages)
            if 'UID' in args:ids=[int(args[-1])] if int(args[-1]) in messages else []
            elif 'HEADER' in args:
                if args[-2]=='Content-Type':ids=[i for i,b in messages.items() if b'disposition-notification' in b]
                else:ids=[i for i,b in messages.items() if email.message_from_bytes(b).get('Message-ID')==args[-1].strip('"')]
            return 'OK',[b' '.join(str(i).encode() for i in ids)]
        if command=='FETCH':
            if 'HEADER.FIELDS' in args[-1]:
                headers=[]
                for uid in map(int,args[0].split(',')):
                    message=email.message_from_bytes(messages[uid])
                    content_type=message.get('Content-Type','text/plain')
                    raw=('Content-Type: '+content_type+'\r\n\r\n').encode()
                    headers.append((f'1 (UID {uid} BODY[HEADER.FIELDS (CONTENT-TYPE)] {{{len(raw)}}}'.encode(),raw))
                return 'OK',headers
            return 'OK',[(b'BODY',messages[int(args[0])])]
        if command=='MOVE':
            uid=int(args[0]);target=args[1];self.source.boxes[target][max(self.source.boxes[target],default=0)+1]=messages.pop(uid);self.moves+=1
            if self.fail_after_move:self.fail_after_move=False;raise TimeoutError('synthetic lost MOVE ACK')
            return 'OK',[]
        raise AssertionError(command)

class Source:
    def __init__(self,raw):self.boxes={'INBOX':{10:raw}};self.validity=77;self.connection=Wire(self);self.marked=[]
    def select(self,box):assert self.connection.select(box,True)[0]=='OK';return self.validity
    def fetch(self,uid):return self.boxes[self.connection.box][uid]
    def uids_after(self,cursor):return []
    def unread_uids(self):return [i for i in self.boxes['INBOX'] if i not in self.marked]
    def mark_seen(self,uid,mailbox,validity):assert validity==self.validity;self.marked.append(uid);return True
    def close(self):pass

def queued(factory,tmp_path,destination=PROCESSED):
    raw=raw_pdf(contents=(b'%PDF archive-only',),message_id='<archive@example.invalid>');path=tmp_path/'raw.eml';path.write_bytes(raw)
    with factory() as db:
        with db.begin():queue_move(db,'synthetic','INBOX',77,10,destination,str(path),'<archive@example.invalid>')
    return Source(raw)

def test_completed_move_keeps_bytes_and_replay_creates_no_second_copy(session_factory,tmp_path):
    source=queued(session_factory,tmp_path);raw=source.fetch(10)
    assert reconcile_moves(session_factory,source,'synthetic')['moved_or_confirmed']==1
    assert source.boxes['INBOX']=={} and list(source.boxes[folder_name(PROCESSED)].values())==[raw]
    assert reconcile_moves(session_factory,source,'synthetic')['moved_or_confirmed']==0 and source.connection.moves==1
    assert not any(c[0] in ('EXPUNGE','STORE','DELETE') for c in source.connection.calls)

def test_lost_move_ack_is_reconciled_without_blind_move_or_smtp(session_factory,tmp_path):
    source=queued(session_factory,tmp_path);source.connection.fail_after_move=True
    assert reconcile_moves(session_factory,source,'synthetic')['uncertain']==1
    assert reconcile_moves(session_factory,source,'synthetic')['moved_or_confirmed']==1
    assert source.connection.moves==1

def test_database_commit_failure_after_move_is_reconciled(session_factory,tmp_path):
    source=queued(session_factory,tmp_path)
    def fail(db):db.add(OutboxEvent(id=uuid.uuid4(),topic=None,payload={}))
    with pytest.raises(Exception):reconcile_moves(session_factory,source,'synthetic',before_commit=fail)
    assert reconcile_moves(session_factory,source,'synthetic')['moved_or_confirmed']==1 and source.connection.moves==1

def test_uidvalidity_change_holds_original_without_move(session_factory,tmp_path):
    source=queued(session_factory,tmp_path);source.validity=88
    assert reconcile_moves(session_factory,source,'synthetic')['uncertain']==1
    assert source.connection.moves==0 and 10 in source.boxes['INBOX']

def test_utf7_folder_encoding_matches_protocol_examples():
    assert folder_name('INBOX')=='INBOX' and folder_name('&')=='&-'
    assert folder_name('台北')=='&U,BTFw-'
    assert folder_name(PROCESSED).isascii() and folder_name(NOTIFICATIONS).isascii()


@pytest.mark.parametrize('recipient,expected',[('one@example.invalid',True),('wrong@example.invalid',False)])
def test_old_mdn_moves_to_notifications_and_only_matching_receipt_turns_green(session_factory,seed,tmp_path,recipient,expected,monkeypatch):
    if os.getenv('INGEST_REGRESSION_SOURCE'):
        old=types.ModuleType('mdn_ingest_regression');sourcefile=pathlib.Path(os.environ['INGEST_REGRESSION_SOURCE'])
        exec(compile(sourcefile.read_text(encoding='utf-8'),str(sourcefile),'exec'),old.__dict__)
        monkeypatch.setattr('common.ingest.ingest_uid',old.ingest_uid)
    record=seed();delivery=planned(session_factory,record)[0];deliver_recipient(session_factory,delivery,SMTP())
    with session_factory() as db:mid=db.get(RecipientDelivery,delivery).message_id
    raw=(f'From: {recipient}\r\nMessage-ID: <receipt@example.invalid>\r\nMIME-Version: 1.0\r\n'
         'Content-Type: multipart/report; report-type=disposition-notification; boundary="mdn"\r\n\r\n'
         '--mdn\r\nContent-Type: message/disposition-notification\r\n\r\n'
         f'Final-Recipient: rfc822; {recipient}\r\nOriginal-Message-ID: {mid}\r\n'
         'Disposition: manual-action/MDN-sent-manually; displayed\r\n\r\n--mdn--\r\n').encode()
    source=Source(raw);store=BlobStore(tmp_path/'store')
    assert source.uids_after(999)==[]  # MDN below polling checkpoint must still be handled.
    assert archive_mailbox(session_factory,source,store,'synthetic')['moved_or_confirmed']==1,'Receipt remains in Inbox'
    assert source.boxes['INBOX']=={} and list(source.boxes[folder_name(NOTIFICATIONS)].values())==[raw]
    assert archive_mailbox(session_factory,source,store,'synthetic')['moved_or_confirmed']==0
    with session_factory() as db:
        assert bool(db.get(Attachment,record['attachment']).read_receipt_received)==expected
        assert db.get(RecipientDelivery,delivery).attempts==1
        assert db.scalar(select(MailboxAction)).state=='done'
    from common.ingest import reconcile_files
    assert reconcile_files(session_factory,store)['unreferenced']==[]


@pytest.mark.parametrize('state,delivery_state',[('review_required',None),('validated','pending'),('validated','uncertain')])
def test_unfinished_or_uncertain_pdf_stays_in_inbox(session_factory,seed,tmp_path,state,delivery_state):
    record=seed();raw=raw_pdf();path=tmp_path/'raw.eml';path.write_bytes(raw)
    if delivery_state:
        delivery=planned(session_factory,record)[0]
    with session_factory() as db:
        with db.begin():
            a=db.get(Attachment,record['attachment']);m=a.message
            m.imap_account='synthetic';m.imap_mailbox='INBOX';m.imap_uidvalidity=77;m.imap_uid=10
            m.ingest_state='complete';m.raw_mime_path=str(path);m.part_manifest=[{'attachment_id':str(a.id),'state':'saved'}]
            db.get(ProcessingRevision,record['revision']).state=state
            if delivery_state:db.get(RecipientDelivery,delivery).state=delivery_state
    source=Source(raw)
    assert archive_mailbox(session_factory,source,BlobStore(tmp_path/'store'),'synthetic')['moved_or_confirmed']==0
    assert source.boxes['INBOX']=={10:raw} and source.connection.moves==0

def test_processed_worker_entry_moves_existing_completed_uid_without_smtp(session_factory,seed,tmp_path,monkeypatch):
    record=seed();raw=raw_pdf(contents=(b'%PDF archive-only',),message_id='<archive@example.invalid>');path=tmp_path/'raw.eml';path.write_bytes(raw)
    with session_factory() as db:
        with db.begin():
            a=db.get(Attachment,record['attachment']);m=a.message
            m.imap_account='synthetic';m.imap_mailbox='INBOX';m.imap_uidvalidity=77;m.imap_uid=10;m.ingest_state='complete';m.raw_mime_path=str(path)
            m.part_manifest=[{'attachment_id':str(a.id),'state':'saved'}]
    ids=planned(session_factory,record)
    for id in ids:deliver_recipient(session_factory,id,SMTP())
    source=Source(raw)
    import dotenv
    monkeypatch.setattr(dotenv,'load_dotenv',lambda *a,**k:False)
    monkeypatch.syspath_prepend(str(pathlib.Path(__file__).resolve().parents[1]/'worker'))
    monkeypatch.setenv('DATABASE_URL',session_factory.kw['bind'].url.render_as_string(hide_password=False));monkeypatch.setenv('REDIS_URL','memory://');monkeypatch.setenv('IMAP_USER','synthetic')
    module=types.ModuleType('archive_entry_regression')
    sourcefile=pathlib.Path(os.getenv('WORKER_REGRESSION_SOURCE',str(pathlib.Path(__file__).resolve().parents[1]/'worker/email_client.py')))
    module.__file__=str(sourcefile);exec(compile(sourcefile.read_text(encoding='utf-8'),str(sourcefile),'exec'),module.__dict__)
    monkeypatch.setattr(module,'SessionLocal',session_factory);monkeypatch.setattr(module,'UPLOAD_DIR',str(tmp_path/'uploads'));monkeypatch.setattr('common.ingest.IMAPSource',lambda:source)
    module.fetch_emails_task.run()
    assert source.boxes['INBOX']=={},'Completed mail is still in Inbox'
    with session_factory() as db:assert all(db.get(RecipientDelivery,id).attempts==1 for id in ids)

def test_real_imap_uid_move_with_cyrillic_folder_and_exact_message_bytes(session_factory,tmp_path,monkeypatch):
    import socketserver,imaplib
    from test_local_protocols import running
    from common.ingest import IMAPSource
    fake=queued(session_factory,tmp_path);raw=fake.fetch(10)
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.box='INBOX';self.wfile.write(b'* OK synthetic IMAP\r\n')
            while True:
                row=self.rfile.readline()
                if not row:return
                self.server.commands.append(row.decode().strip());tag,cmd,*args=row.split();cmd=cmd.upper()
                if cmd==b'CAPABILITY':self.wfile.write(b'* CAPABILITY IMAP4rev1 MOVE UIDPLUS\r\n'+tag+b' OK cap\r\n')
                elif cmd==b'LOGIN':self.wfile.write(tag+b' OK login\r\n')
                elif cmd in (b'SELECT',b'EXAMINE'):
                    self.box=args[0].decode().strip('"')
                    if self.box not in self.server.boxes:self.wfile.write(tag+b' NO missing\r\n');continue
                    self.wfile.write(b'* 1 EXISTS\r\n* OK [UIDVALIDITY 77] valid\r\n'+tag+b' OK select\r\n')
                elif cmd==b'LIST':
                    if args[-1].decode() in self.server.boxes:self.wfile.write(b'* LIST () "/" '+args[-1]+b'\r\n')
                    self.wfile.write(tag+b' OK list\r\n')
                elif cmd==b'CREATE':self.server.boxes[args[0].decode()]={};self.wfile.write(tag+b' OK create\r\n')
                elif cmd==b'UID':
                    command=args.pop(0);messages=self.server.boxes[self.box]
                    if command==b'SEARCH':
                        ids=list(messages)
                        if args[0]==b'UID':ids=[int(args[1])] if int(args[1]) in messages else []
                        self.wfile.write(b'* SEARCH'+(b' '+b' '.join(str(i).encode() for i in ids) if ids else b'')+b'\r\n'+tag+b' OK search\r\n')
                    elif command==b'FETCH':
                        body=messages[int(args[0])];self.wfile.write(b'* 1 FETCH (UID '+args[0]+b' BODY[] {'+str(len(body)).encode()+b'}\r\n'+body+b')\r\n'+tag+b' OK fetch\r\n')
                    elif command==b'MOVE':self.server.boxes[args[1].decode()][22]=messages.pop(int(args[0]));self.wfile.write(tag+b' OK move\r\n')
                    else:raise AssertionError(command)
                elif cmd==b'LOGOUT':self.wfile.write(b'* BYE\r\n'+tag+b' OK logout\r\n');return
                else:raise AssertionError(cmd)
    monkeypatch.setenv('IMAP_USER','synthetic');monkeypatch.setenv('IMAP_PASSWORD','synthetic')
    with running(Handler) as server:
        server.boxes={'INBOX':{10:raw}}
        source=IMAPSource(imaplib.IMAP4('127.0.0.1',server.server_address[1],timeout=5))
        try:assert reconcile_moves(session_factory,source,'synthetic')['moved_or_confirmed']==1
        finally:source.close()
        assert server.boxes['INBOX']=={} and list(server.boxes[folder_name(PROCESSED)].values())==[raw]
        assert sum('UID MOVE ' in c for c in server.commands)==1 and not any('EXPUNGE' in c for c in server.commands)
