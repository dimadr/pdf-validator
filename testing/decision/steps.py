"""Targeted operations on an isolated restored copy. No production credentials."""
import os,json,pathlib,sys,time,uuid,hashlib,urllib.request,datetime
import psycopg2
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from celery import Celery
assert os.getenv('PROJECT1_TEST_SCOPE')=='production-decision-copy'
assert '@postgres:5432/project1_test_decision' in os.environ['DATABASE_URL']
ROOT=pathlib.Path('/data');FIX=pathlib.Path('/fixtures/pdf')
SERIALS={'legacy-before':'990000001','legacy-after':'990000002','canary':'990000-CANARY-20261030-1',
         'during':'990000-RESUME-1','after':'990000-AFTER-1','fault':'990000-FAULT-1'}
TABLES=['objects','email_sources','incoming_messages','attachments','reports','users','trusted_emails','settings']
def sql(query,params=None):
    with psycopg2.connect(os.environ['DATABASE_URL'].replace('postgresql+psycopg2:','postgresql:')) as c:
        with c.cursor() as q:q.execute(query,params);return q.fetchall() if q.description else []
def api(path='state',payload=None):
    request=urllib.request.Request('http://127.0.0.1:8025/'+path,data=json.dumps(payload).encode() if payload else None,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)
def inject(name):return api('add',{'fixture':name+'.pdf'})['uid']
def app(role):
    value=Celery('decision-'+role,broker=os.environ['REDIS_URL'],backend=os.environ['REDIS_URL'])
    if role=='v2':value.conf.update(task_default_queue='project1_v2',broker_transport_options={'global_keyprefix':'decision_v2:'},result_backend_transport_options={'global_keyprefix':'decision_v2:'})
    return value
def effects(name):
    digest=hashlib.sha256((FIX/(name+'.pdf')).read_bytes()).hexdigest()
    return [r for r in api()['smtp'] if digest in r['pdf_sha256']]
def wait(predicate,seconds=45):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.2)
    raise RuntimeError('Timed out awaiting isolated transition')
def state():
    return {'smtp':api()['smtp'],'imap_uids':api()['uids'],
        'v2_revisions':sql('SELECT count(*) FROM processing_revisions')[0][0],
        'v2_states':dict(sql('SELECT state,count(*) FROM recipient_deliveries GROUP BY state')),
        'checkpoint':sql('SELECT uidvalidity,last_uid FROM mailbox_checkpoints'),
        'ai_requests':sql('SELECT count(*) FROM ai_requests')[0][0]}
def drained(role):
    worker='legacy@decision' if role=='legacy' else 'v2@decision'
    inspector=app(role).control.inspect(destination=[worker],timeout=3)
    result={k:getattr(inspector,k)() for k in ('active','reserved','scheduled')}
    assert all(v is not None and all(not tasks for tasks in v.values()) for v in result.values()),'Worker is not drained'
    return {k:0 for k in result}
def fetch(role):
    result=app(role).send_task('email_client.fetch_emails_task').get(timeout=30)
    if role=='v2':assert result['state']=='complete',result
    else:assert result.get('status')=='success',result
    return result
def pump():
    app('v2').send_task('pipeline.dispatch_outbox').get(timeout=15)
    return (sql("SELECT count(*) FROM recipient_deliveries WHERE state IN ('pending','in_flight')")[0][0]==0
        and sql("SELECT count(*) FROM processing_revisions WHERE state IN ('received','extracting','technical_error')")[0][0]==0
        and sql('SELECT count(*) FROM outbox_events WHERE published_at IS NULL')[0][0]==0)
def revision(name):
    digest=hashlib.sha256((FIX/(name+'.pdf')).read_bytes()).hexdigest()
    return sql('SELECT current_revision_id FROM attachments WHERE file_sha256=%s',(digest,))[0][0]
def check_originals():
    original=json.loads((ROOT/'original.json').read_text());actual={}
    for table,data in original.items():
        columns=','.join('"'+c+'"' for c in data['columns'])
        result=sql("SELECT count(*),md5(coalesce(string_agg(row_to_json(t)::text,'' ORDER BY id),'')) FROM (SELECT "+columns+' FROM '+table+' WHERE id=ANY(%s::uuid[])) t',(data['ids'],))[0]
        assert list(result)==data['checksum'],'Original history changed: '+table
        actual[table]=list(result)
    return actual

def main(mode):
    if mode=='preflight-probe':
        import contextlib,io
        from preflight import PROBE
        namespace={}
        with contextlib.redirect_stdout(io.StringIO()):exec(PROBE,namespace)
        observed=namespace['out']
        assert observed['schema']=='0007' and observed['v2_tables_complete'] and observed['redis_ping']
        assert observed['v2']['revisions']==1 and observed['v2']['imap_revisions']==0
        assert not observed['v2']['ai_enabled'] and observed['v2']['checkpoint'] is None
        return {'postgres_v2_queries':'PASS','redis_ping':True,'imap_examine':'PASS','manual_canary_checkpoint_absent_is_valid':True}
    if mode=='seed':
        original={}
        for table in TABLES:
            columns=[r[0] for r in sql("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",(table,))]
            ids=[str(r[0]) for r in sql('SELECT id FROM '+table)]
            checks=list(sql("SELECT count(*),md5(coalesce(string_agg(row_to_json(t)::text,'' ORDER BY id),'')) FROM "+table+' t')[0])
            original[table]={'columns':columns,'ids':ids,'checksum':checks}
        (ROOT/'original.json').write_text(json.dumps(original))
        objects={}
        for name,serial in SERIALS.items():
            assert not sql('SELECT 1 FROM objects WHERE calculator_number=%s',(serial,))
            ident=str(uuid.uuid4());objects[name]=ident
            sql('INSERT INTO objects(id,name,name_norm,calculator_number,email,is_active) VALUES(%s,%s,%s,%s,%s,true)',
                (ident,'Controlled '+name,'controlled '+name,serial,name+'@example.invalid'))
            assert not sql('SELECT 1 FROM attachments WHERE file_sha256=%s',(hashlib.sha256((FIX/(name+'.pdf')).read_bytes()).hexdigest(),))
        (ROOT/'objects.json').write_text(json.dumps(objects));return {'seeded_objects':len(objects),'production_rows_modified':False}
    if mode=='legacy-before' or mode=='legacy-after':
        uid=inject(mode);fetch('legacy');wait(lambda:len(effects(mode))==1)
        result=effects(mode);assert result[0]['recipient']==mode+'@example.invalid'
        attachment=sql('SELECT status,sent_to_email FROM attachments WHERE file_sha256=%s',(hashlib.sha256((FIX/(mode+'.pdf')).read_bytes()).hexdigest(),))[0]
        assert attachment[0]=='sent' and mode+'@example.invalid' in attachment[1]
        wait(lambda:not sql("SELECT 1 FROM attachments WHERE status NOT IN ('sent','rejected')"));drain=drained('legacy')
        return {'uid':uid,'legacy_processing_and_client_smtp':'PASS','effects':1,'drained':drain}
    if mode=='A-barrier':
        assert sql('SELECT count(*) FROM processing_revisions')[0][0]==0
        assert sql('SELECT count(*) FROM recipient_deliveries')[0][0]==0
        return {'v2_operations':0,'v2_smtp_attempts':0,'legacy_can_return':True}
    if mode=='operator-canary':
        import base64
        from canary_once import prepare,wait_for_acceptance
        content=(FIX/'operator.pdf').read_bytes();digest=hashlib.sha256(content).hexdigest()
        payload={'serial':'990000-CANARY-APPROVAL-1','recipient':'operator-canary@example.invalid',
            'request_key':'controlled-canary:operator:'+digest,'sha256':digest,'pdf_base64':base64.b64encode(content).decode()}
        result=prepare(payload);assert result['state']=='prepared';proof=wait_for_acceptance(result)
        assert proof['state']=='accepted' and proof['serial']==payload['serial'] and proof['recipient']==payload['recipient']
        wait(lambda:len(effects('operator'))==1)
        assert effects('operator')[0]['recipient']==payload['recipient']
        previous=prepare(payload);assert previous['state']=='existing';assert wait_for_acceptance(previous)['state']=='accepted'
        assert len(effects('operator'))==1
        delivery=sql('SELECT d.id,d.state FROM recipient_deliveries d JOIN delivery_intents i ON i.id=d.intent_id WHERE i.revision_id=%s',(result['revision_id'],))[0]
        assert delivery[1]=='accepted'
        duplicate=app('v2').send_task('pipeline.deliver_recipient',kwargs={'delivery_id':str(delivery[0])}).get(timeout=15)
        assert duplicate['state']=='skipped' and len(effects('operator'))==1
        assert sql('SELECT count(*) FROM mailbox_checkpoints')[0][0]==0
        assert sql('SELECT count(*) FROM ai_requests')[0][0]==0
        return {**result,'smtp_ack_and_saved_record':'PASS','repeated_command_effects':0,'duplicate_task_effects':0,
            'original_checksums':check_originals(),'ai_requests':0,'fabricated_imap_uids':0,'drained':drained('v2')}
    if mode=='canary':
        uid=inject('canary');fetch('v2');wait(pump);wait(lambda:len(effects('canary'))==1)
        rid=revision('canary');obj=json.loads((ROOT/'objects.json').read_text())['canary']
        row=sql('SELECT state,calculator_number,object_id FROM processing_revisions WHERE id=%s',(rid,))[0]
        assert row[0]=='validated' and row[1]==SERIALS['canary'] and str(row[2])==obj
        delivery=sql('SELECT d.id,d.state,d.recipient,d.message_id FROM recipient_deliveries d JOIN delivery_intents i ON i.id=d.intent_id WHERE i.revision_id=%s',(rid,))[0]
        assert delivery[1]=='accepted' and delivery[2]=='canary@example.invalid'
        assert effects('canary')[0]['message_id']==delivery[3]
        duplicate=app('v2').send_task('pipeline.deliver_recipient',kwargs={'delivery_id':str(delivery[0])}).get(timeout=15)
        assert duplicate['state']=='skipped' and len(effects('canary'))==1
        (ROOT/'canary-delivery.json').write_text(json.dumps({'delivery_id':str(delivery[0]),'revision_id':str(rid),'message_id':delivery[3]}))
        return {'uid':uid,'serial':row[1],'object_id':obj,'recipient':delivery[2],'smtp_ack_and_db':'PASS','duplicate_effects':0,'drained':drained('v2')}
    if mode=='during':
        uid=inject('during');return {'queued_in_imap_while_worker_stopped':uid,'smtp_effects':len(effects('during'))}
    if mode=='resume':
        old=json.loads((ROOT/'canary-delivery.json').read_text())
        replay=app('v2').send_task('pipeline.deliver_recipient',kwargs={'delivery_id':old['delivery_id']}).get(timeout=15)
        assert replay['state']=='skipped';fetch('v2');wait(pump);wait(lambda:len(effects('during'))==1)
        uid=inject('after');fetch('v2');wait(pump);wait(lambda:len(effects('after'))==1)
        checkpoint=sql('SELECT uidvalidity,last_uid FROM mailbox_checkpoints')[0]
        assert checkpoint[1]==uid and len(effects('canary'))==1
        assert effects('during')[0]['recipient']=='during@example.invalid' and effects('after')[0]['recipient']=='after@example.invalid'
        assert sql('SELECT count(*) FROM ai_requests')[0][0]==0
        assert not sql('SELECT 1 FROM ai_control WHERE enabled=true')
        return {'checkpoint':list(checkpoint),'accepted_canary_not_resent':True,'during_after_delivered':True,
            'v2_new_uids':sql('SELECT imap_uid FROM incoming_messages WHERE imap_account=%s ORDER BY imap_uid',(os.environ['IMAP_USER'],)),
            'ai_requests':0,'original_checksums':check_originals(),'drained':drained('v2')}
    if mode=='uncertain':
        uid=inject('fault');fetch('v2')
        from common.pipeline import validate_revision
        from common.delivery import deliver_recipient,reconcile
        from common.smtp_transport import SMTPTransport
        from common.models import OutboxEvent
        factory=sessionmaker(bind=create_engine(os.environ['DATABASE_URL']),expire_on_commit=False)
        rid=uuid.UUID(str(revision('fault')));assert validate_revision(factory,rid)['state']=='validated'
        delivery=sql('SELECT d.id FROM recipient_deliveries d JOIN delivery_intents i ON i.id=d.intent_id WHERE i.revision_id=%s',(str(rid),))[0][0]
        def fail_commit(db):db.add(OutboxEvent(id=uuid.uuid4(),topic=None,payload={}))
        assert deliver_recipient(factory,delivery,SMTPTransport(),before_result_commit=fail_commit)['state']=='uncertain'
        assert len(effects('fault'))==1
        assert deliver_recipient(factory,delivery,SMTPTransport())['state']=='skipped'
        reconcile(factory);wait(pump)
        assert len(effects('fault'))==1
        return {'uid':uid,'smtp_effects':1,'persisted_state':'uncertain','autoresend':False,
                'continuous_automatic_delivery':'NO-GO: ambiguous SMTP outcome requires reconciliation'}
    if mode=='state':return state()
    raise ValueError('Unknown controlled mode')

if __name__=='__main__':
    result=main(sys.argv[1]);print(json.dumps(result,default=str))
