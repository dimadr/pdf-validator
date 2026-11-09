"""Isolated acceptance application. Never use as a production entrypoint."""
import base64,json,os,pathlib,secrets,subprocess,sys,threading,time,uuid,imaplib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from email.message import EmailMessage
from socketserver import StreamRequestHandler,ThreadingTCPServer
from sqlalchemy import select

assert os.getenv('PROJECT1_TEST_SCOPE')=='isolated-compose' and os.getenv('SMTP_SERVER')=='smtp'
os.environ.update(AI_PROVIDER_MODE='stub',ROUTERAI_API_KEY='synthetic-stub-only',IMAP_USER='synthetic',IMAP_PASSWORD='synthetic',
    CELERY_QUEUE='staging_v2',CELERY_REDIS_KEY_PREFIX='staging_v2:',CELERY_VISIBILITY_TIMEOUT='30')
from backend.main import app
from backend.database import SessionLocal
from backend.routers.auth import get_password_hash,require_admin
from common.models import User,Object,EmailSource,AIRequest,RecipientDelivery,ProcessingRevision
from common.ingest import IMAPSource,BlobStore,poll
from common.delivery import dispatch_outbox
from fastapi import Depends,UploadFile,File,HTTPException
from fastapi.responses import HTMLResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from testing.synthetic_pdf import report,extraction

mail=[];mail_lock=threading.Lock();calls=[];injections={}
class IMAPHandler(StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'* OK Synthetic staging IMAP\r\n')
        while True:
            raw=self.rfile.readline()
            if not raw:return
            tag,command,*args=raw.split();command=command.upper()
            if command==b'CAPABILITY':self.wfile.write(b'* CAPABILITY IMAP4rev1\r\n'+tag+b' OK CAPABILITY completed\r\n')
            elif command==b'LOGIN':self.wfile.write(tag+b' OK LOGIN completed\r\n')
            elif command==b'EXAMINE':self.wfile.write(b'* '+str(len(mail)).encode()+b' EXISTS\r\n* OK [UIDVALIDITY 77] valid\r\n'+tag+b' OK [READ-ONLY]\r\n')
            elif command==b'UID' and args[0]==b'SEARCH':
                start=int(args[-1].split(b':')[0]);uids=b' '.join(str(i).encode() for i in range(start,len(mail)+1))
                self.wfile.write(b'* SEARCH '+uids+b'\r\n'+tag+b' OK SEARCH completed\r\n')
            elif command==b'UID' and args[0]==b'FETCH':
                value=mail[int(args[1])-1]
                self.wfile.write(b'* 1 FETCH (UID '+args[1]+b' BODY[] {'+str(len(value)).encode()+b'}\r\n'+value+b')\r\n'+tag+b' OK FETCH completed\r\n')
            elif command==b'LOGOUT':self.wfile.write(b'* BYE\r\n'+tag+b' OK LOGOUT completed\r\n');return
            else:self.wfile.write(tag+b' BAD Unsupported synthetic command\r\n')
class IMAPServer(ThreadingTCPServer):allow_reuse_address=True;daemon_threads=True

class AIHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        import io,pdfplumber,re
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with pdfplumber.open(io.BytesIO(base64.b64decode(data['file_data']))) as pdf:
            content='\n'.join(p.extract_text() or '' for p in pdf.pages)
        serial=re.search(r'Заводской номер: ([\w-]+)',content).group(1)
        calls.append({'model':data['model'],'serial':serial})
        output=json.dumps({'extraction':extraction(serial),'usage':{'total_tokens':40,'cost':'unknown'}},ensure_ascii=False).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(output)

def add_mail(content,filename):
    if len(content)>10*1024*1024 or not content.startswith(b'%PDF'):raise HTTPException(422,'Synthetic PDF required')
    message=EmailMessage();message['From']='noreply@eldis24.ru';message['To']='processor@example.invalid'
    message['Subject']='Synthetic acceptance report';message['Message-ID']='<'+uuid.uuid4().hex+'@example.invalid>'
    message.set_content('Synthetic staging report')
    message.add_attachment(content,maintype='application',subtype='pdf',filename=pathlib.Path(filename).name)
    with mail_lock:mail.append(message.as_bytes());return len(mail)

@app.get('/testing',response_class=HTMLResponse)
def controls():
    return '''<html lang="ru"><meta charset="utf-8"><h1>Изолированный staging</h1>
    <p>Только синтетические PDF и SMTP sink. Вход и AI-тумблер — в <a href="/">основном UI</a>.</p>
    <button onclick="send('normal')">Обычный PDF</button> <button onclick="send('complex')">Сложный PDF</button>
    <button onclick="send('rejected')">Брак PDF</button>
    <input type="file" id="pdf" accept="application/pdf"><button onclick="upload()">Загрузить синтетический PDF в IMAP</button>
    <button onclick="state()">Результаты</button><pre id="result"></pre><script>
    const headers=()=>({Authorization:'Bearer '+localStorage.getItem('token')});
    async function send(kind){const r=await fetch('/testing/inject/'+kind,{method:'POST',headers:headers()});document.getElementById('result').textContent=await r.text();}
    async function upload(){const data=new FormData();data.append('file',document.getElementById('pdf').files[0]);const r=await fetch('/testing/upload',{method:'POST',headers:headers(),body:data});document.getElementById('result').textContent=await r.text();}
    async function state(){const r=await fetch('/testing/state',{headers:headers()});document.getElementById('result').textContent=JSON.stringify(await r.json(),null,2);}
    </script></html>'''

@app.post('/testing/inject/{kind}')
def inject(kind:str,user=Depends(require_admin)):
    if kind not in ('normal','complex','rejected'):raise HTTPException(422)
    injections[kind]=injections.get(kind,0)+1
    number={'normal':1,'complex':2,'rejected':3}[kind] if injections[kind]==1 else len(mail)+4
    serial='005545-'+str(number)
    # A new sequence changes bytes without altering the instrument identity.
    content=report(serial,grid=kind!='complex',missing=kind=='rejected')
    return {'uid':add_mail(content,kind+'.pdf'),'kind':kind,'serial':serial}

@app.post('/testing/upload')
async def upload(file:UploadFile=File(...),user=Depends(require_admin)):
    return {'uid':add_mail(await file.read(),file.filename or 'synthetic.pdf')}

@app.get('/testing/state')
def state(user=Depends(require_admin)):
    import httpx
    with SessionLocal() as db:
        return {'imap_messages':len(mail),'ai_calls':calls,'revisions':[{'id':str(r.id),'state':r.state,
            'serial':r.calculator_number,'reason':r.reason,'ai':(r.validation_result or {}).get('ai')} for r in db.scalars(select(ProcessingRevision))],
            'deliveries':[{'recipient':d.recipient,'state':d.state,'role':d.role,'error_code':d.error_code} for d in db.scalars(select(RecipientDelivery))],
            'sink':httpx.get('http://smtp:8025/messages',timeout=3,trust_env=False).json()}

def main():
    password=secrets.token_urlsafe(18)
    with SessionLocal() as db:
        with db.begin():
            if db.scalar(select(User).where(User.username=='staging-admin')) is None:
                db.add(User(username='staging-admin',password_hash=get_password_hash(password),role='admin',is_active=True))
                db.add(EmailSource(email='noreply@eldis24.ru',is_active=True))
                for i in range(1,121):db.add(Object(name=f'Synthetic object {i:03d}',name_norm=f'synthetic {i:03d}',
                    calculator_number=f'005545-{i}',email=f'client-{i}@example.invalid',is_active=True))
            else:password=json.loads(pathlib.Path('/results/staging-login.json').read_text())['password']
    pathlib.Path('/results/staging-login.json').write_text(json.dumps({'username':'staging-admin','password':password}))
    for address,handler,server in [(('127.0.0.1',1143),IMAPHandler,IMAPServer),(('127.0.0.1',8099),AIHandler,ThreadingHTTPServer)]:
        instance=server(address,handler);threading.Thread(target=instance.serve_forever,daemon=True).start()
    environment=dict(os.environ,PYTHONPATH='/app/worker:/app')
    workerlog=open('/results/staging-worker.log','wb')
    worker=subprocess.Popen([sys.executable,'-m','celery','-A','celery_app','worker','--pool=prefork','--concurrency=1',
        '-Q','staging_v2','--loglevel=WARNING','--without-mingle','--without-gossip'],cwd='/app/worker',env=environment,stdout=workerlog,stderr=subprocess.STDOUT)
    from celery import Celery
    producer=Celery('staging',broker=os.environ['REDIS_URL']);producer.conf.update(task_default_queue='staging_v2',
        broker_transport_options={'global_keyprefix':'staging_v2:'})
    def polling():
        while True:
            source=None
            try:
                source=IMAPSource(imaplib.IMAP4('127.0.0.1',1143,timeout=5))
                poll(SessionLocal,source,BlobStore('/results/staging-uploads'),'synthetic')
                dispatch_outbox(SessionLocal,lambda topic,payload:producer.send_task(topic,kwargs=payload))
            except Exception as error:
                pathlib.Path('/results/staging-loop-error.txt').write_text(type(error).__name__)
            finally:
                if source:source.close()
            time.sleep(1)
    threading.Thread(target=polling,daemon=True).start()
    app.router.routes[:]=[r for r in app.router.routes if getattr(r,'path',None)!='/']
    app.mount('/',StaticFiles(directory='/app/ui',html=True),name='ui')
    import uvicorn
    try:uvicorn.run(app,host='0.0.0.0',port=8000,log_level='warning')
    finally:worker.terminate();worker.wait(timeout=15);workerlog.close()

if __name__=='__main__':main()
