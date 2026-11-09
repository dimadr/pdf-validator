"""Persistent synthetic IMAP + STARTTLS/AUTH SMTP sink; internal test network only."""
import socketserver,ssl,threading,json,pathlib,os,email,re,hashlib,subprocess
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from email.message import EmailMessage
from email.utils import formatdate,parseaddr
assert os.getenv('PROJECT1_TEST_SCOPE')=='production-decision-copy'
ROOT=pathlib.Path('/data');ROOT.mkdir(exist_ok=True)
LOCK=threading.RLock();COMMANDS=[]
TLS=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);TLS.load_cert_chain('/fixtures/tls/cert.pem','/fixtures/tls/key.pem')

def messages():return sorted((ROOT/'mail').glob('*.eml'),key=lambda p:int(p.stem)) if (ROOT/'mail').exists() else []
def snapshot():
    with LOCK:
        path=ROOT/'smtp.jsonl'
        return {'uids':[int(p.stem) for p in messages()],
                'smtp':[json.loads(s) for s in path.read_text().splitlines()] if path.exists() else [],
                'commands':list(COMMANDS)}
def append_pdf(name):
    path=pathlib.Path('/fixtures/pdf')/pathlib.Path(name).name
    if path.parent!=pathlib.Path('/fixtures/pdf') or not path.is_file():raise ValueError('Unknown synthetic fixture')
    content=path.read_bytes()
    with LOCK:
        uid=max([int(p.stem) for p in messages()],default=0)+1
        mail=EmailMessage();mail['From']='noreply@eldis24.ru';mail['To']='processor@example.invalid'
        mail['Date']=formatdate(localtime=False);mail['Message-ID']=f'<decision-{uid}@example.invalid>'
        mail['Subject']='Synthetic controlled decision test'
        mail.set_content('Synthetic fixture only.');mail.add_attachment(content,maintype='application',subtype='pdf',filename=name)
        (ROOT/'mail').mkdir(exist_ok=True)
        target=ROOT/'mail'/f'{uid}.eml'
        with target.open('xb') as stream:stream.write(mail.as_bytes());stream.flush();os.fsync(stream.fileno())
        return uid

class Server(socketserver.ThreadingTCPServer):allow_reuse_address=True;daemon_threads=True
class SecureIMAP(Server):
    def get_request(self):
        connection,address=super().get_request();return TLS.wrap_socket(connection,server_side=True),address
class IMAP(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'* OK Synthetic IMAP ready\r\n')
        while True:
            raw=self.rfile.readline()
            if not raw:return
            tag,verb,*args=raw.split();verb=verb.upper()
            with LOCK:COMMANDS.append('IMAP '+verb.decode()+(' '+args[0].decode() if verb==b'UID' else ''))
            if verb==b'CAPABILITY':self.wfile.write(b'* CAPABILITY IMAP4rev1\r\n'+tag+b' OK CAPABILITY completed\r\n')
            elif verb==b'LOGIN':self.wfile.write(tag+b' OK LOGIN completed\r\n')
            elif verb in (b'SELECT',b'EXAMINE'):
                count=len(messages());self.wfile.write(f'* {count} EXISTS\r\n* OK [UIDVALIDITY 771030] valid\r\n* OK [UIDNEXT {count+1}] next\r\n'.encode()+tag+b' OK SELECT completed\r\n')
            elif verb==b'UID' and args[0]==b'SEARCH' or verb==b'SEARCH':
                ids=[int(p.stem) for p in messages()]
                if verb==b'UID' and args[-1]!=b'ALL':ids=[u for u in ids if u>=int(args[-1].split(b':')[0])]
                self.wfile.write(b'* SEARCH '+b' '.join(str(u).encode() for u in ids)+b'\r\n'+tag+b' OK SEARCH completed\r\n')
            elif verb==b'FETCH' or verb==b'UID' and args[0]==b'FETCH':
                token=args[0] if verb==b'FETCH' else args[1]
                ids=list(map(int,token.split(b','))) if b':' not in token else [int(p.stem) for p in messages()]
                for uid in ids:
                    source=ROOT/'mail'/f'{uid}.eml'
                    if not source.exists():continue
                    body=source.read_bytes()
                    if b'HEADER.FIELDS' in raw:
                        header=email.message_from_bytes(body)
                        body=('Message-ID: '+header['Message-ID']+'\r\nFrom: '+header['From']+'\r\n\r\n').encode()
                    label=b'RFC822' if verb==b'FETCH' else b'BODY[]'
                    self.wfile.write(b'* '+str(uid).encode()+b' FETCH (UID '+str(uid).encode()+b' '+label+b' {'+str(len(body)).encode()+b'}\r\n'+body+b')\r\n')
                self.wfile.write(tag+b' OK FETCH completed\r\n')
            elif verb in (b'CLOSE',b'NOOP'):self.wfile.write(tag+b' OK completed\r\n')
            elif verb==b'LOGOUT':self.wfile.write(b'* BYE\r\n'+tag+b' OK LOGOUT completed\r\n');return
            else:self.wfile.write(tag+b' BAD unsupported synthetic command\r\n')

class SMTP(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 synthetic sink\r\n');sender=None;recipient=None
        while True:
            line=self.rfile.readline(16384)
            if not line:return
            verb=line.split(b' ',1)[0].strip().upper()
            with LOCK:COMMANDS.append('SMTP '+verb.decode(errors='replace'))
            if verb in (b'EHLO',b'HELO'):self.wfile.write(b'250-synthetic\r\n250-STARTTLS\r\n250 AUTH PLAIN LOGIN\r\n')
            elif verb==b'STARTTLS':
                self.wfile.write(b'220 TLS ready\r\n');self.wfile.flush()
                self.request=TLS.wrap_socket(self.request,server_side=True);self.rfile=self.request.makefile('rb');self.wfile=self.request.makefile('wb',buffering=0)
            elif verb==b'AUTH':
                if line.upper().startswith(b'AUTH LOGIN'):
                    self.wfile.write(b'334 VXNlcm5hbWU6\r\n');self.rfile.readline();self.wfile.write(b'334 UGFzc3dvcmQ6\r\n');self.rfile.readline()
                self.wfile.write(b'235 synthetic authentication accepted\r\n')
            elif verb==b'MAIL':
                sender=parseaddr(line.decode().split(':',1)[1].strip().split(' SIZE=',1)[0])[1]
                self.wfile.write(b'250 OK\r\n' if sender.endswith('@example.invalid') else b'550 synthetic addresses only\r\n')
            elif verb==b'RCPT':
                recipient=parseaddr(line.decode().split(':',1)[1].strip())[1]
                self.wfile.write(b'250 OK\r\n' if recipient.endswith('@example.invalid') else b'550 synthetic addresses only\r\n')
            elif verb==b'DATA':
                if not sender or not recipient or not recipient.endswith('@example.invalid'):self.wfile.write(b'550 no synthetic envelope\r\n');continue
                self.wfile.write(b'354 data\r\n');parts=[]
                while True:
                    part=self.rfile.readline()
                    if not part:return
                    if part==b'.\r\n':break
                    parts.append(part[1:] if part.startswith(b'..') else part)
                content=b''.join(parts);mail=email.message_from_bytes(content)
                record={'recipient':recipient,'message_id':mail.get('Message-ID'),
                    'pdf_sha256':[hashlib.sha256(p.get_payload(decode=True)).hexdigest() for p in mail.walk() if p.get_content_type()=='application/pdf']}
                with LOCK:
                    with (ROOT/'smtp.jsonl').open('a') as stream:stream.write(json.dumps(record)+'\n');stream.flush();os.fsync(stream.fileno())
                self.wfile.write(b'250 stored and accepted\r\n')
            elif verb==b'QUIT':self.wfile.write(b'221 bye\r\n');return
            elif verb in (b'RSET',b'NOOP'):self.wfile.write(b'250 OK\r\n')
            else:self.wfile.write(b'500 unsupported\r\n')

class HTTP(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        value=snapshot();self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
    def do_POST(self):
        value=json.loads(self.rfile.read(int(self.headers['Content-Length'])));uid=append_pdf(value['fixture'])
        self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'uid':uid}).encode())

if __name__=='__main__':
    for address,handler,kind in [(('0.0.0.0',1993),IMAP,SecureIMAP),(('0.0.0.0',1025),SMTP,Server)]:
        server=kind(address,handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0',8025),HTTP).serve_forever()
