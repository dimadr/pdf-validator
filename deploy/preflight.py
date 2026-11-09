"""Read-only production admission checks for immutable Project1 RC d0881c6."""
import argparse,json,subprocess,sys,datetime,pathlib

IMAGES={
 'backend':'sha256:71059776a735b1a23d5194f7c1bb51b75f90ada8de0d0bf6e49a80de2159c348',
 'worker':'sha256:2bdd033a88de555c4334f5926e95737a0641dca019184a4f97fbf0226db188ec',
 'old_worker':'sha256:a714cfd978aefbf85cc50d93dbf3161aad59fba1d20a0cb0672be7c13a14c96f',
 'old_backend':'sha256:f73d008a4d9a47d6c7ca81e0393b633b12660257d48ed0e7f09ae826ddebe17b',
 'old_beat':'sha256:d73771074ec65503e76a8094cd037e561d9fcfa026a578114d02b19b5276c80a'}
PG_VOLUME='project1_postgres_data'
REDIS_VOLUME='8c7064f2e729326886c6ec169fea292c46e3fc464862e92d1757da783a922078'

def run(args,input=None,timeout=30):
    result=subprocess.run(args,input=input,text=True,capture_output=True,timeout=timeout)
    if result.returncode:raise RuntimeError('Read-only command failed: '+args[0]+' '+args[1])
    return result.stdout

PROBE=r'''
import os,json,datetime,email,imaplib,ssl,re
import psycopg2,redis
from celery import Celery
from email.utils import parseaddr
out={'environment':{k:bool(os.getenv(k)) for k in ['DATABASE_URL','REDIS_URL','SECRET_KEY','SMTP_SERVER','SMTP_USER','SMTP_PASSWORD','IMAP_SERVER','IMAP_USER','IMAP_PASSWORD']}}
out['environment']['smtp_from_effective_present']=bool(os.getenv('SMTP_FROM') or os.getenv('SMTP_USER'))
out['environment']['smtp_port_valid']=os.getenv('SMTP_PORT','587').isdigit() and int(os.getenv('SMTP_PORT','587')) in (465,587)
url=os.environ['DATABASE_URL'].replace('postgresql+psycopg2:','postgresql:')
c=psycopg2.connect(url,connect_timeout=5);c.set_session(readonly=True)
with c.cursor() as q:
 q.execute("SET LOCAL statement_timeout='5s'")
 q.execute('SELECT version_num FROM alembic_version');out['schema']=q.fetchone()[0]
 q.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'");tables={r[0] for r in q.fetchall()}
 q.execute("SELECT count(*) FROM attachments WHERE status NOT IN ('sent','rejected','archived')"+(' AND current_revision_id IS NULL' if 'processing_revisions' in tables else ''))
 out['legacy_unfinished']=q.fetchone()[0]
 out['v2']={'revisions':0,'imap_revisions':0,'smtp_attempts':0,'in_flight':0,'uncertain':0,'pending':0,'unpublished':0,'ai_enabled':False,'checkpoint':None}
 if out['schema'] in ('0007','0008','0009','0010'):
  required={'processing_revisions','delivery_intents','recipient_deliveries','outbox_events','mailbox_checkpoints','ai_control','ai_requests','ai_setting_audit'}
  if out['schema'] in ('0008','0009','0010'):required.update(('smtp_attempts','smtp_provider_audit'))
  if out['schema'] in ('0009','0010'):required.add('mailbox_actions')
  if out['schema']=='0010':required.update(('brevo_delivery_events','brevo_poll_checkpoints'))
  out['v2_tables_complete']=required.issubset(tables)
  if out['v2_tables_complete']:
   for key,sql in [('revisions','SELECT count(*) FROM processing_revisions'),
    ('imap_revisions','SELECT count(*) FROM processing_revisions r JOIN attachments a ON a.id=r.attachment_id JOIN incoming_messages m ON m.id=a.message_id WHERE m.imap_uid IS NOT NULL'),
    ('smtp_attempts','SELECT coalesce(sum(attempts),0) FROM recipient_deliveries'),
    ('in_flight',"SELECT count(*) FROM recipient_deliveries WHERE state='in_flight'"),('uncertain',"SELECT count(*) FROM recipient_deliveries WHERE state='uncertain'"),
    ('pending',"SELECT count(*) FROM recipient_deliveries WHERE state='pending' OR state='failed' AND retry_at IS NOT NULL"),
    ('unpublished','SELECT count(*) FROM outbox_events WHERE published_at IS NULL'),('ai_enabled','SELECT coalesce(bool_or(enabled),false) FROM ai_control')]:
    q.execute(sql);out['v2'][key]=q.fetchone()[0]
   q.execute('SELECT uidvalidity,last_uid FROM mailbox_checkpoints WHERE account=%s AND mailbox=%s',(os.environ['IMAP_USER'],os.getenv('IMAP_MAILBOX','INBOX')))
   checkpoint=q.fetchone()
   if checkpoint:out['v2']['checkpoint']={'uidvalidity':checkpoint[0],'last_uid':checkpoint[1]}
 q.execute("SELECT m.provider_message_id,count(a.id)>0 AND bool_and(a.status IN ('sent','rejected')) FROM incoming_messages m LEFT JOIN attachments a ON a.message_id=m.id WHERE m.provider_message_id IS NOT NULL GROUP BY m.provider_message_id")
 covered=dict(q.fetchall())
 q.execute('SELECT email FROM email_sources WHERE is_active=true');trusted={parseaddr(v[0])[1].lower() for v in q.fetchall()}
c.rollback();c.close()
r=redis.Redis.from_url(os.environ['REDIS_URL'],socket_connect_timeout=5,socket_timeout=5)
out['redis_ping']=r.ping();out['redis']={'nonempty_lists':{},'unacked':0}
for key in r.scan_iter():
 kind=r.type(key)
 if kind==b'list':
  count=r.llen(key)
  if count:out['redis']['nonempty_lists'][key.decode(errors='replace')]=count
 if kind==b'hash' and key.endswith(b'unacked'):out['redis']['unacked']+=r.hlen(key)
connection=imaplib.IMAP4_SSL(os.environ['IMAP_SERVER'],int(os.getenv('IMAP_PORT','993')),ssl_context=ssl.create_default_context(),timeout=15)
connection.login(os.environ['IMAP_USER'],os.environ['IMAP_PASSWORD'])
status,_=connection.select(os.getenv('IMAP_MAILBOX','INBOX'),readonly=True)
if status!='OK':raise RuntimeError('IMAP EXAMINE failed')
validity=int(connection.response('UIDVALIDITY')[1][0]);uidnext=int(connection.response('UIDNEXT')[1][0])
status,values=connection.uid('SEARCH',None,'ALL')
if status!='OK':raise RuntimeError('IMAP UID SEARCH failed')
uids=sorted(int(v) for v in values[0].split())[-100:];matched=[];unmatched=[];ignored=[]
if uids:
 status,parts=connection.uid('FETCH',','.join(map(str,uids)),'(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)])')
 if status!='OK':raise RuntimeError('IMAP read-only header proof failed')
 for part in parts:
  if not isinstance(part,tuple):continue
  uid=int(re.search(rb'UID (\d+)',part[0]).group(1));headers=email.message_from_bytes(part[1])
  sender=parseaddr(headers.get('From',''))[1].lower();message_id=headers.get('Message-ID','').strip()
  if sender not in trusted:ignored.append(uid)
  elif covered.get(message_id,False):matched.append(uid)
  else:unmatched.append(uid)
connection.logout()
candidate=min(unmatched)-1 if unmatched else max(uids,default=uidnext-1)
out['imap']={'uidvalidity':validity,'uidnext':uidnext,'last_uid':max(uids,default=0),
 'tail_count':len(uids),'matched_trusted_uids':sorted(matched),'unprocessed_or_unproved_trusted_uids':sorted(unmatched),
 'ignored_untrusted_uids':sorted(ignored),'safe_bootstrap_candidate':candidate,
 'verified_legacy_anchor':any(uid<=candidate for uid in matched),
 'unclassified_uids':sorted(set(uids)-set(matched)-set(unmatched)-set(ignored)),
 'scope':'Last 100 UID headers only; historical delivery is not re-proven'}
print(json.dumps(out))
'''

INSPECT=r'''
import os,json,socket
from celery import Celery
app=Celery('readonly-preflight',broker=os.environ['REDIS_URL'])
app.conf.broker_transport_options={'global_keyprefix':os.getenv('CELERY_REDIS_KEY_PREFIX','')}
inspect=app.control.inspect(destination=['celery@'+socket.gethostname()],timeout=3)
out={}
for key in ('active','reserved','scheduled'):
 value=getattr(inspect,key)()
 out[key]=None if value is None else sum(len(v) for v in value.values())
print(json.dumps(out))
'''

def collect():
    ids=run(['docker','ps','-aq']).split()
    containers=json.loads(run(['docker','inspect',*ids]));named={c['Name'].lstrip('/'):c for c in containers}
    facts={'images':{},'containers':{},'senders':[],'producers':[],'inspection':{}}
    for role,image in IMAGES.items():
        p=subprocess.run(['docker','image','inspect','--format','{{.Id}}',image],capture_output=True,text=True)
        facts['images'][role]=p.returncode==0 and p.stdout.strip()==image
    for name in ('project1-postgres','project1-redis','project1-worker','project1-backend','project1-celery-beat'):
        c=named.get(name)
        facts['containers'][name]=None if c is None else {'id':c['Id'],'running':c['State']['Running'],
            'started':c['State']['StartedAt'],'exit_code':c['State']['ExitCode'],'mounts':c['Mounts']}
    facts['uploads_present']=pathlib.Path('/root/project1/uploads').is_dir() and next(pathlib.Path('/root/project1/uploads').iterdir(),None) is not None
    facts['app_mounts']=[{'name':c['Name'].lstrip('/'),'mounts':c['Mounts']} for c in containers if
        c['Name'].lstrip('/') in ('project1-worker','project1-backend','project1-celery-beat','project1-v2-worker','project1-v2-backend','project1-v2-celery-beat')]
    for c in containers:
        if not c['State']['Running'] or 'project1_default' not in c['NetworkSettings']['Networks']:continue
        command=' '.join(c['Config']['Cmd'] or [])
        if c['Image'] not in IMAGES.values() and c['Config'].get('Labels',{}).get('com.docker.compose.project') not in ('project1','project1-v2'):continue
        name=c['Name'].lstrip('/')
        if 'celery' in command and 'worker' in command:
            facts['senders'].append(name)
            facts['inspection'][name]=json.loads(run(['docker','exec','-i',name,'python','-B','-'],input=INSPECT))
        if 'celery' in command and 'beat' in command:facts['producers'].append(name)
    # Use a running application container; never run a new production container.
    observer=next((n for n in ('project1-backend','project1-v2-backend','project1-worker','project1-v2-worker') if n in named and named[n]['State']['Running']),None)
    if observer is None:raise RuntimeError('No running application observer; use a controlled readonly observer separately')
    facts.update(json.loads(run(['docker','exec','-i',observer,'python','-B','-'],input=PROBE,timeout=60)))
    compare="import sys,io,json,os;from dotenv import dotenv_values;v=dotenv_values(stream=io.StringIO(sys.stdin.read()));keys=['DATABASE_URL','REDIS_URL','SECRET_KEY','SMTP_SERVER','SMTP_USER','SMTP_PASSWORD','IMAP_SERVER','IMAP_USER','IMAP_PASSWORD'];print(json.dumps({k:bool(v.get(k)) and v[k]==os.getenv(k) for k in keys}))"
    facts['env_file_matches_running_app']=json.loads(run(['docker','exec','-i',observer,'python','-B','-c',compare],input=pathlib.Path('/root/project1/.env').read_text()))
    facts['observed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    return facts

def evaluate(facts,phase,barrier=None,legacy_drain_proof=None):
    errors=[]
    def require(value,reason):
        if not value:errors.append(reason)
    require(all(facts.get('images',{}).get(k) for k in IMAGES),'Required immutable RC/legacy image is missing')
    containers=facts.get('containers',{})
    for name,volume,destination in [('project1-postgres',PG_VOLUME,'/var/lib/postgresql/data'),('project1-redis',REDIS_VOLUME,'/data')]:
        c=containers.get(name);require(c is not None and c['running'],name+' is missing or stopped')
        mounts=[] if c is None else c['mounts']
        require(any(m.get('Type')=='volume' and m.get('Name')==volume and m.get('Destination')==destination and m.get('RW') is True for m in mounts),name+' has an unexpected/readonly/empty replacement volume')
    require(facts.get('uploads_present'),'Existing uploads directory is absent or empty')
    for app in facts.get('app_mounts',[]):
        require(any(m.get('Type')=='bind' and m.get('Source')=='/root/project1/uploads' and m.get('Destination')=='/app/uploads' and m.get('RW') is True for m in app['mounts']),app['name']+' does not share the original writable uploads')
    require(facts.get('schema') in (('0004','0007','0008','0009','0010') if phase in ('prepare','legacy-return') else ('0007','0008','0009','0010')),'Unexpected PostgreSQL schema for '+phase)
    if facts.get('schema') in ('0007','0008','0009','0010'):require(facts.get('v2_tables_complete'),'V2/AI/SMTP schema is incomplete')
    required=('DATABASE_URL','REDIS_URL','SECRET_KEY','SMTP_SERVER','SMTP_USER','SMTP_PASSWORD','IMAP_SERVER','IMAP_USER','IMAP_PASSWORD','smtp_from_effective_present','smtp_port_valid')
    require(all(facts.get('environment',{}).get(k) for k in required),'Existing .env is incompatible; missing variable or unsupported SMTP port')
    require(all(facts.get('env_file_matches_running_app',{}).get(k) for k in required[:9]),'Host .env differs from running app or lacks a required value')
    require(facts.get('redis_ping'),'Application cannot reach Redis')
    require(len(facts.get('senders',[]))<=1,'Simultaneous or unknown Project1 senders')
    require(len(facts.get('producers',[]))<=1,'Simultaneous Project1 producers')
    drained_producer=False
    if legacy_drain_proof is not None:
        proof=legacy_drain_proof;age=(datetime.datetime.fromisoformat(facts['observed_at'])-datetime.datetime.fromisoformat(proof['observed_at'])).total_seconds()
        drained_producer=(phase=='prepare' and proof.get('format')=='legacy-queue-proof/v1' and proof.get('verdict')=='DRAINED' and 0<=age<=60
            and all(proof.get(role,{}).get(k)==containers.get(name,{}).get(k) for role,name in [('worker_identity','project1-worker'),('producer_identity','project1-celery-beat')] for k in ('id','started'))
            and not containers.get('project1-celery-beat',{}).get('running',True) and containers.get('project1-celery-beat',{}).get('exit_code')==0)
        require(drained_producer,'Invalid/stale legacy drain proof; no queue conditions may be bypassed')
    if phase=='prepare':
        require(facts.get('senders')==['project1-worker'] and (facts.get('producers')==['project1-celery-beat'] or (drained_producer and not facts.get('producers'))),'Pre-stop barrier must observe the original worker and beat running, or a verified cleanly stopped producer')
    for name,states in facts.get('inspection',{}).items():
        for key in ('active','reserved','scheduled'):require(states.get(key)==0,name+' '+key+' is nonempty or unknown')
    require(not facts.get('redis',{}).get('nonempty_lists'),'Redis has queued/unknown list work')
    require(facts.get('redis',{}).get('unacked')==0,'Redis has unacked task(s)')
    require(facts.get('legacy_unfinished')==0,'Unfinished legacy attachments exist')
    imap=facts.get('imap',{})
    require(imap.get('uidvalidity',0)>0 and imap.get('uidnext',0)>0,'IMAP UID/UIDVALIDITY is unknown')
    require(imap.get('verified_legacy_anchor'),'No verified legacy UID anchor in the inspected tail')
    require(not imap.get('unclassified_uids',[]),'IMAP tail contains UID(s) without a classified header; repeat observation, do not skip them')
    v2=facts.get('v2',{})
    require(not v2.get('ai_enabled',True),'AI must be OFF for cutover/canary')
    checkpoint=v2.get('checkpoint')
    if checkpoint:
        require(checkpoint['uidvalidity']==imap.get('uidvalidity'),'Stored IMAP UIDVALIDITY changed; do not automatically reset to UID 0')
        require(checkpoint['last_uid']<=imap.get('last_uid',-1),'Stored checkpoint exceeds current mailbox UID')
    if phase!='prepare':
        require('project1-worker' not in facts.get('senders',[]) and 'project1-celery-beat' not in facts.get('producers',[]),'Legacy sender/producer is still running')
        require(barrier is not None,'Fresh pre-stop UID/drain evidence is required')
        if barrier is not None:
            require(barrier.get('verdict')=='PASS','Pre-stop evidence was not PASS')
            age=(datetime.datetime.fromisoformat(facts['observed_at'])-datetime.datetime.fromisoformat(barrier['observed_at'])).total_seconds()
            require(age>=0 and (phase!='cutover-ready' or age<=600),'UID/drain evidence is invalid or older than 10 minutes for first cutover')
            old=barrier.get('facts',{}).get('imap',{})
            require(imap.get('uidvalidity')==old.get('uidvalidity'),'IMAP UIDVALIDITY changed since the barrier')
            require(old.get('safe_bootstrap_candidate',-1)<=imap.get('last_uid',-2),'IMAP bootstrap exceeds current mailbox UID')
            for name in ('project1-worker','project1-celery-beat'):
                prior=barrier.get('facts',{}).get('containers',{}).get(name);current=containers.get(name)
                require(prior and current and prior['id']==current['id'] and prior['started']==current['started'],'Legacy container identity changed after drain')
                require(current is not None and not current['running'] and current['exit_code']==0,'Legacy process did not stop cleanly: '+name)
    if phase=='legacy-return':
        require(v2.get('smtp_attempts',-1)==0,'V2 SMTP was already attempted; legacy return forbidden')
        require(v2.get('revisions',-1)==0 and v2.get('unpublished',-1)==0,'V2 operations exist; automatic legacy handback is not safe')
        require(not facts.get('senders') and not facts.get('producers'),'Stop/drain V2 before legacy handback')
    if phase=='cutover-ready':
        require(not facts.get('senders') and not facts.get('producers'),'New sender/producer must not run before the cutover gate')
        require(v2.get('revisions',-1)==0 and v2.get('unpublished',-1)==0 and v2.get('smtp_attempts',-1)==0,'First cutover requires a fresh V2 ledger; use recovery procedure for existing work')
    if phase!='prepare':
        require(v2.get('in_flight',-1)==0,'V2 in-flight outcome is not finalized')
        require(v2.get('uncertain',-1)==0,'V2 uncertain SMTP requires reconciliation; automatic resume is NO-GO')
    if phase=='v2-resume':
        require(not facts.get('producers'),'Pause all producers before the V2 recovery gate')
        require(set(facts.get('senders',[])).issubset({'project1-v2-worker'}),'Only the expected V2 worker may be present during recovery')
    if phase=='v2-resume' and v2.get('imap_revisions',0)>0:require(checkpoint is not None,'V2 IMAP history exists without the expected mailbox checkpoint')
    return errors

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--phase',choices=('prepare','cutover-ready','legacy-return','v2-resume'),default='prepare')
    parser.add_argument('--barrier-file',type=pathlib.Path);parser.add_argument('--legacy-drain-proof',type=pathlib.Path);args=parser.parse_args()
    try:
        facts=collect();barrier=json.loads(args.barrier_file.read_text()) if args.barrier_file else None
        drain=json.loads(args.legacy_drain_proof.read_text()) if args.legacy_drain_proof else None
        errors=evaluate(facts,args.phase,barrier,drain)
        result={'verdict':'STOP' if errors else 'PASS','phase':args.phase,'observed_at':facts['observed_at'],'reasons':errors,'facts':facts}
    except Exception as error:
        result={'verdict':'STOP','phase':args.phase,'reasons':[type(error).__name__+': '+str(error)],'facts':{}}
    print(json.dumps(result,ensure_ascii=False,indent=2));return 2 if result['verdict']=='STOP' else 0

if __name__=='__main__':sys.exit(main())
