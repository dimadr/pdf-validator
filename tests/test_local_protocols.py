"""Real loopback SMTP/IMAP protocols; no external recipient or authentication traffic."""
import socketserver,threading,imaplib,contextlib,email
from sqlalchemy import select,func
from common.ingest import IMAPSource,BlobStore,poll
from common.models import RecipientDelivery,IncomingMessage,Attachment,OutboxEvent
from common.smtp_transport import SMTPTransport
from common.delivery import deliver_recipient
from test_delivery_core import planned
from test_pipeline_ingest import raw_pdf,trusted

class ThreadedServer(socketserver.ThreadingMixIn,socketserver.TCPServer):
    allow_reuse_address=True;daemon_threads=True

@contextlib.contextmanager
def running(handler):
    server=ThreadedServer(('127.0.0.1',0),handler);server.received=[];server.connections=0;server.commands=[]
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join(timeout=5)

class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.server.connections+=1;self.wfile.write(b'220 localhost synthetic SMTP sink\r\n')
        while True:
            line=self.rfile.readline()
            if not line:return
            command=line.split(b' ',1)[0].strip().upper()
            if command in (b'EHLO',b'HELO'):self.wfile.write(b'250-localhost\r\n250 SIZE 2000000\r\n')
            elif command in (b'MAIL',b'RCPT',b'RSET',b'NOOP'):self.wfile.write(b'250 OK\r\n')
            elif command==b'DATA':
                self.wfile.write(b'354 End with dot\r\n');content=[]
                while True:
                    row=self.rfile.readline()
                    if row==b'.\r\n':break
                    if not row:return
                    content.append(row)
                self.server.received.append(b''.join(content));self.wfile.write(b'250 Accepted by local sink\r\n')
            elif command==b'QUIT':self.wfile.write(b'221 Bye\r\n');return
            else:raise AssertionError('Unexpected SMTP command')

def test_actual_smtp_sink_per_recipient_and_mime(session_factory,seed,monkeypatch):
    ids=planned(session_factory,seed(),['one@example.invalid','two@example.invalid'])
    with running(SMTPHandler) as server:
        monkeypatch.setenv('SMTP_SERVER','127.0.0.1');monkeypatch.setenv('SMTP_PORT',str(server.server_address[1]))
        monkeypatch.setenv('SMTP_TEST_MODE','true');monkeypatch.setenv('SMTP_FROM','processor@example.invalid');monkeypatch.delenv('SMTP_USER',raising=False)
        transport=SMTPTransport()
        for delivery in ids:assert deliver_recipient(session_factory,delivery,transport)['state']=='accepted'
        assert server.connections==2 and len(server.received)==2
        for raw in server.received:
            message=email.message_from_bytes(raw)
            assert message['Date'] and message['Message-ID'] and message['From']=='processor@example.invalid'
            assert any(part.get_content_type()=='application/pdf' for part in message.walk())

def test_strict_aiosmtpd_accepts_real_binary_pdf_with_crlf(session_factory,seed,monkeypatch,tmp_path):
    import socket,hashlib
    from aiosmtpd.controller import Controller
    from testing.smtp.sink import SinkHandler,DiskStore
    from common.models import ProcessingRevision
    from testing.synthetic_pdf import report
    record=seed()
    content=report()
    record['path'].write_bytes(content)
    with session_factory() as db:
        with db.begin():db.get(ProcessingRevision,record['revision']).content_sha256=hashlib.sha256(content).hexdigest()
    delivery=planned(session_factory,record)[0]
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    store=DiskStore(tmp_path/'strict-sink')
    controller=Controller(SinkHandler(store),hostname='127.0.0.1',port=port,decode_data=False)
    controller.start()
    try:
        monkeypatch.setenv('SMTP_SERVER','127.0.0.1');monkeypatch.setenv('SMTP_PORT',str(port))
        monkeypatch.setenv('SMTP_TEST_MODE','true');monkeypatch.setenv('SMTP_FROM','processor@example.invalid')
        monkeypatch.delenv('SMTP_USER',raising=False)
        assert deliver_recipient(session_factory,delivery,SMTPTransport())['state']=='accepted'
        metadata=store.list_messages();assert len(metadata)==1
        raw=store.get(metadata[0]['id'],raw=True)
        assert b'\n' not in raw.replace(b'\r\n',b'')
    finally:controller.stop()


def test_actual_smtp_ack_then_real_pg_commit_failure(session_factory,seed,monkeypatch):
    import uuid
    delivery=planned(session_factory,seed())[0]
    with running(SMTPHandler) as server:
        monkeypatch.setenv('SMTP_SERVER','127.0.0.1');monkeypatch.setenv('SMTP_PORT',str(server.server_address[1]))
        monkeypatch.setenv('SMTP_TEST_MODE','true');monkeypatch.setenv('SMTP_FROM','processor@example.invalid');monkeypatch.delenv('SMTP_USER',raising=False)
        def fail_commit(db):db.add(OutboxEvent(id=uuid.uuid4(),topic=None,payload={}))
        assert deliver_recipient(session_factory,delivery,SMTPTransport(),before_result_commit=fail_commit)['state']=='uncertain'
        assert deliver_recipient(session_factory,delivery,SMTPTransport())['state']=='skipped'
        assert len(server.received)==1
    with session_factory() as db:assert db.get(RecipientDelivery,delivery).state=='uncertain'

class IMAPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'* OK Local IMAP fixture\r\n')
        while True:
            row=self.rfile.readline()
            if not row:return
            self.server.commands.append(row.decode('ascii').strip())
            tag,command,*args=row.split();command=command.upper()
            if command==b'CAPABILITY':self.wfile.write(b'* CAPABILITY IMAP4rev1\r\n'+tag+b' OK CAPABILITY\r\n')
            elif command==b'LOGIN':self.wfile.write(tag+b' OK LOGIN\r\n')
            elif command==b'EXAMINE':self.wfile.write(b'* 1 EXISTS\r\n* OK [UIDVALIDITY 77] valid\r\n'+tag+b' OK [READ-ONLY] EXAMINE\r\n')
            elif command==b'UID' and args[0]==b'SEARCH':self.wfile.write(b'* SEARCH 1\r\n'+tag+b' OK SEARCH\r\n')
            elif command==b'UID' and args[0]==b'FETCH':
                assert b'BODY.PEEK[]' in row
                raw=raw_pdf(contents=(b'%PDF-local-wire',))
                self.wfile.write(b'* 1 FETCH (UID 1 BODY[] {'+str(len(raw)).encode()+b'}\r\n'+raw+b')\r\n'+tag+b' OK FETCH\r\n')
            elif command==b'LOGOUT':self.wfile.write(b'* BYE\r\n'+tag+b' OK LOGOUT\r\n');return
            else:raise AssertionError('Unexpected IMAP command '+command.decode())

def test_actual_imap_uid_readonly_protocol(session_factory,tmp_path,monkeypatch):
    trusted(session_factory);monkeypatch.setenv('IMAP_USER','synthetic');monkeypatch.setenv('IMAP_PASSWORD','synthetic')
    with running(IMAPHandler) as server:
        source=IMAPSource(imaplib.IMAP4('127.0.0.1',server.server_address[1],timeout=5))
        try:assert poll(session_factory,source,BlobStore(tmp_path/'content'),'synthetic')['processed']==1
        finally:source.close()
        assert any('EXAMINE' in command for command in server.commands)
        assert not any('STORE ' in command or 'EXPUNGE' in command or 'SELECT ' in command for command in server.commands)
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(IncomingMessage))==1
        assert db.scalar(select(func.count()).select_from(Attachment))==1
