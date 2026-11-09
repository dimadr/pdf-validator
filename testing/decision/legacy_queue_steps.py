"""One synthetic missing PDF on an empty 0004 DB; actual old task + Redis."""
import os,pathlib,json,time,uuid,datetime,collections,urllib.request,sys,hashlib
import psycopg2,redis
from celery import Celery
assert os.getenv('PROJECT1_TEST_SCOPE')=='production-decision-copy'
assert '@postgres:5432/project1_test_decision' in os.environ['DATABASE_URL']
ROOT=pathlib.Path('/data');app=Celery('legacy-queue-test',broker=os.environ['REDIS_URL'],backend=os.environ['REDIS_URL'])
def sql(query,args=None):
    with psycopg2.connect(os.environ['DATABASE_URL'].replace('postgresql+psycopg2:','postgresql:')) as c:
        with c.cursor() as q:q.execute(query,args);return q.fetchall() if q.description else None
def smtp():
    with urllib.request.urlopen('http://127.0.0.1:8025/state') as response:return json.load(response)['smtp']
def wait(predicate,seconds=45):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=predicate()
        if value:return value
        time.sleep(.2)
    raise RuntimeError('Isolated legacy queue transition timed out')
def calls():
    p=ROOT/'retry-calls.jsonl';return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []
def drained():
    view=app.control.inspect(destination=['legacy@decision'],timeout=2)
    for name in ('active','reserved','scheduled'):
        value=getattr(view,name)()
        if value is None or any(value.values()):return False
    r=redis.Redis.from_url(os.environ['REDIS_URL'])
    return r.llen('celery')==0 and r.hlen('unacked')==0
def main(mode):
    if mode=='seed':
        a,s,m=[str(uuid.uuid4()) for _ in range(3)]
        sql('INSERT INTO email_sources(id,email,is_active) VALUES(%s,%s,false)',(s,'legacy-fixture@example.invalid'))
        sql('INSERT INTO incoming_messages(id,provider_message_id,source_id,from_email,status) VALUES(%s,%s,%s,%s,%s)',(m,'<legacy-fixture@example.invalid>',s,'legacy-fixture@example.invalid','done'))
        sql("INSERT INTO attachments(id,message_id,filename,file_path,file_sha256,file_size,status,reject_reason,created_at,updated_at) VALUES(%s,%s,'missing.pdf','/app/uploads/never-created.pdf',%s,100,'rejected','send_error',now()-interval '2 days',now()-interval '1 day')",(a,m,'f'*64))
        updated=sql('SELECT updated_at FROM attachments WHERE id=%s',(a,))[0][0].isoformat()
        (ROOT/'legacy-seed.json').write_text(json.dumps({'attachment_id':a,'updated_at':updated}))
        return {'attachment_id':a,'updated_at':updated,'production_data_used':False}
    seed=json.loads((ROOT/'legacy-seed.json').read_text());a=seed['attachment_id']
    if mode in ('first-cycle','second-cycle'):
        previous={r['task_id'] for r in calls()};root=app.send_task('recovery.recover_stuck_attachments');result=root.get(timeout=10)
        assert result['recovered']['rejected']==1
        wait(lambda:len({r['task_id'] for r in calls()}-previous)==1)
        task=next(iter({r['task_id'] for r in calls()}-previous))
        wait(lambda:sum(r['task_id']==task for r in calls())==14)
        if mode=='first-cycle':wait(drained)
        else:
            wait(lambda:sum(len(v) for v in (app.control.inspect(destination=['legacy@decision'],timeout=2).scheduled() or {}).values())==8)
        assert sql('SELECT updated_at FROM attachments WHERE id=%s',(a,))[0][0].isoformat()==seed['updated_at']
        assert not smtp()
        return {'recovery_task_id':root.id,'send_task_id':task,'actual_retry_publications':14,
            'countdowns':dict(collections.Counter(str(r['original_countdown']) for r in calls() if r['task_id']==task)),
            'aged_updated_at_unchanged':True,'smtp_effects':0,'new_task_id':task not in previous,'scheduled_peak_expected':8}
    if mode=='await-drain':
        wait(drained);assert not smtp()
        return {'natural_drain':'PASS','smtp_effects':0,'documents_retained':sql('SELECT count(*) FROM attachments')[0][0]}
    if mode=='completed-guard':
        ident=str(uuid.uuid4());message=sql('SELECT message_id FROM attachments WHERE id=%s',(a,))[0][0]
        sql("INSERT INTO attachments(id,message_id,filename,file_path,file_sha256,status,sent_to_email,sent_at,original_message_id) VALUES(%s,%s,'completed.pdf','/app/uploads/completed.pdf',%s,'sent','owner@example.invalid',now(),'<accepted-test@example.invalid>')",(ident,message,'e'*64))
        result=app.send_task('email_sender.send_pdf_attachment',args=[ident]).get(timeout=10)
        wait(drained);assert result['status']=='error' and 'status is sent' in result['message'] and not smtp()
        return {'completed_attachment':ident,'smtp_effects':0,'old_terminal_guard':'PASS'}
    if mode=='pending-fixtures':
        message=sql('SELECT message_id FROM attachments WHERE id=%s',(a,))[0][0];result={}
        for kind,name in [('VALID_PENDING','operator'),('SMTP_UNCERTAIN','after')]:
            ident=str(uuid.uuid4());content=(pathlib.Path('/fixtures/pdf')/(name+'.pdf')).read_bytes();path=pathlib.Path('/app/uploads')/(ident+'.pdf');path.write_bytes(content)
            sql("INSERT INTO attachments(id,message_id,filename,file_path,file_sha256,status) VALUES(%s,%s,'pending.pdf',%s,%s,'validated')",(ident,message,str(path),hashlib.sha256(content).hexdigest()))
            task=app.send_task('email_sender.send_pdf_attachment',args=[ident]);result[kind]={'attachment_id':ident,'task_id':task.id}
        assert not smtp();return result
    if mode=='state':return {'smtp_effects':len(smtp()),'documents_retained':sql('SELECT count(*) FROM attachments')[0][0]}
    raise RuntimeError('Unknown isolated mode')
if __name__=='__main__':print(json.dumps(main(sys.argv[1])))
