"""Run only the requested A/B/canary rehearsals on separate Docker resources."""
import pathlib,sys,json,subprocess,datetime,tarfile,hashlib,argparse
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ssh-key',required=True)
parser.add_argument('--artifacts',type=pathlib.Path)
parser.add_argument('--operator-only',action='store_true')
parser.add_argument('--legacy-queue-only',action='store_true')
args=parser.parse_args()
root=pathlib.Path(__file__).resolve().parents[2];sys.path.insert(0,str(root))
from testing.synthetic_pdf import report
out=args.artifacts or root/'.testing-artifacts/production-decision';out.mkdir(exist_ok=True,parents=True)
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dt%H%M%Sz');project='project1-decision-'+stamp
source=out/'fixtures';(source/'pdf').mkdir(exist_ok=True,parents=True)
serials={'legacy-before':'990000001','legacy-after':'990000002','canary':'990000-CANARY-20261030-1','during':'990000-RESUME-1','after':'990000-AFTER-1','fault':'990000-FAULT-1'}
serials['operator']='990000-CANARY-APPROVAL-1'
for name,serial in serials.items():(source/'pdf'/(name+'.pdf')).write_bytes(report(serial))
package=out/'controlled-fixtures.tar.gz'
with tarfile.open(package,'w:gz') as tar:
 for path in (source/'pdf').glob('*.pdf'):tar.add(path,arcname='pdf/'+path.name)
 for name in ['fixtures.py','steps.py']:tar.add(root/'testing/decision'/name,arcname=name)
 tar.add(root/'deploy/canary_once.py',arcname='canary_once.py')
 tar.add(root/'deploy/preflight.py',arcname='preflight.py')
 for path in (root/'deploy/legacy_queue').glob('*.py'):tar.add(path,arcname=path.name)
 for name in ['legacy_queue_worker.py','legacy_queue_steps.py','legacy_queue_host.py']:tar.add(root/'testing/decision'/name,arcname=name)
remote='/root/project1-rc-tests/decision-'+stamp
opts=['-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes','-i',str(pathlib.Path(args.ssh_key).resolve())]
subprocess.run(['ssh','-T',*opts,'root@192.0.2.10','mkdir','-m','700',remote],check=True)
subprocess.run(['scp',*opts,str(package),'root@192.0.2.10:'+remote+'/fixtures.tar.gz'],check=True)
state={'project':project,'remote':remote,'stamp':stamp};(out/'run.json').write_text(json.dumps(state,indent=2),newline='\n')
script=r'''
import subprocess,pathlib,os,json,secrets,time,threading,shutil,tarfile,hashlib
project=__PROJECT__;base=pathlib.Path(__BASE__)
assert str(base).startswith('/root/project1-rc-tests/decision-')
def run(args,timeout=90,input=None):
 p=subprocess.run(args,input=input,capture_output=True,text=True,timeout=timeout)
 if p.returncode:raise RuntimeError('Isolated command failed: '+args[0]+' '+args[1]+'; '+p.stderr[-1800:])
 return p.stdout.strip()
assert hashlib.sha256((base/'fixtures.tar.gz').read_bytes()).hexdigest()==__HASH__
fixture=base/'fixtures';fixture.mkdir()
with tarfile.open(base/'fixtures.tar.gz') as tar:tar.extractall(fixture,filter='data')
tls=fixture/'tls';tls.mkdir(mode=0o700)
run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=smtp','-addext','subjectAltName=DNS:smtp,DNS:imap,DNS:localhost','-addext','basicConstraints=critical,CA:FALSE',
 '-keyout',str(tls/'key.pem'),'-out',str(tls/'cert.pem')])
ids=run(['docker','ps','-aq']).split();protected=json.loads(run(['docker','inspect',*ids]))
def fingerprint(items):return {c['Id']:[c['State']['StartedAt'],c['State']['Status'],c['RestartCount'],c['State']['OOMKilled']] for c in items}
baseline=fingerprint(protected)
available=lambda:int(next(s.split()[1] for s in pathlib.Path('/proc/meminfo').read_text().splitlines() if s.startswith('MemAvailable:')))
assert available()>3*1024*1024 and os.getloadavg()[0]<os.cpu_count() and shutil.disk_usage(base).free>10*1024**3
result={'resource_preflight':{'ram_available_kib':available(),'load':os.getloadavg(),'free_disk':shutil.disk_usage(base).free},
 'limits':{'cpu':2.0,'memory_mib':2176},'base_rc':'d0881c6','scenarios':{}}
names=[];volumes=[];network=project+'-isolated';done=threading.Event();aborted=threading.Event();worker=None
def guard():
 strikes=0
 while not done.wait(5):
  try:
   import urllib.request
   assert fingerprint(json.loads(run(['docker','inspect',*baseline],timeout=15)))==baseline
   assert available()>2*1024*1024
   strikes=strikes+1 if os.getloadavg()[0]>os.cpu_count() else 0
   assert strikes<3
   start=time.monotonic()
   with urllib.request.urlopen('http://127.0.0.1:8001/health',timeout=3) as response:assert response.status==200
   assert time.monotonic()-start<3
  except Exception:
   aborted.set()
   for name in list(names):subprocess.run(['docker','stop','--time','2',name],capture_output=True)
   return
thread=threading.Thread(target=guard,daemon=True);thread.start()
def create(name,image,options,command):
 assert not aborted.is_set()
 names.append(name)
 return run(['docker','run','-d','--name',name,'--network',network,'--label','io.project1.decision='+project,'--pids-limit','192',
  '--security-opt','no-new-privileges:true',*options,image,*command])
password=secrets.token_hex(24);secret=secrets.token_hex(32);test_password=secrets.token_hex(12)
common={'DATABASE_URL':'postgresql+psycopg2://project1_test:'+password+'@postgres:5432/project1_test_decision','REDIS_URL':'redis://redis:6379/0',
 'SECRET_KEY':secret,'PROJECT1_TEST_SCOPE':'production-decision-copy','IMAP_SERVER':'imap','IMAP_PORT':'1993','IMAP_USER':'fixture-user@example.invalid','IMAP_PASSWORD':test_password,
 'SMTP_SERVER':'smtp','SMTP_PORT':'1025','SMTP_PASSWORD':test_password,'ADMIN_EMAIL':'admin@example.invalid','NOTIFY_EMAIL':'notify@example.invalid',
 'UPLOAD_DIR':'/app/uploads','SSL_CERT_FILE':'/fixtures/tls/cert.pem','ROUTERAI_API_KEY':'','AI_FORCE_DISABLED':'true'}
envfiles={}
for role in ['legacy','v2','coordinator']:
 env=dict(common,SMTP_USER='processor@example.invalid' if role=='legacy' else '',SMTP_FROM='processor@example.invalid',PIPELINE_V2_ENABLED='false' if role=='legacy' else 'true',
  SMTP_TEST_MODE='false' if role=='legacy' else 'true',CELERY_QUEUE='celery' if role=='legacy' else 'project1_v2',
  CELERY_REDIS_KEY_PREFIX='' if role=='legacy' else 'decision_v2:',IMAP_BOOTSTRAP_UID='2',CELERY_VISIBILITY_TIMEOUT='10')
 path=base/(role+'.env');path.write_text('\n'.join(k+'='+v for k,v in env.items()));path.chmod(0o600);envfiles[role]=path
backend='sha256:71059776a735b1a23d5194f7c1bb51b75f90ada8de0d0bf6e49a80de2159c348'
new='sha256:2bdd033a88de555c4334f5926e95737a0641dca019184a4f97fbf0226db188ec'
old='sha256:a714cfd978aefbf85cc50d93dbf3161aad59fba1d20a0cb0672be7c13a14c96f'
def step(mode):
 value=run(['docker','exec',project+'-coordinator','python','-B','/fixtures/'+('legacy_queue_steps.py' if __LEGACY_QUEUE__ else 'steps.py'),mode],timeout=100)
 return json.loads(value.splitlines()[-1])
def start_worker(role):
 global worker
 assert worker is None,'Two senders must never overlap'
 name=project+'-'+role;image=old if role=='legacy' else new
 create(name,image,['--env-file',str(envfiles[role]),'--cpus','0.7','--memory','512m','--memory-swap','512m','--cap-drop','ALL','--read-only','--tmpfs','/tmp:size=64m',
  '-v',project+'-uploads:/app/uploads','-v',project+'-data:/data','-v',str(fixture)+':/fixtures:ro'],
  (['python','-B','/fixtures/legacy_queue_worker.py'] if __LEGACY_QUEUE__ else ['celery','-A','celery_app','worker','--concurrency=1','--prefetch-multiplier=1','--hostname='+('legacy@decision' if role=='legacy' else 'v2@decision'),
   '--loglevel=WARNING','--without-mingle','--without-gossip']))
 worker=name
 code="import sys;sys.path.insert(0,'/fixtures');from steps import app;print(bool(app('"+role+"').control.ping(destination=['"+('legacy@decision' if role=='legacy' else 'v2@decision')+"'],timeout=1)))"
 for i in range(30):
  if run(['docker','exec',project+'-coordinator','python','-B','-c',code])=='True':return
  time.sleep(.2)
 raise RuntimeError('Own worker did not start')
def stop_worker():
 global worker
 assert worker
 run(['docker','stop','--time','30',worker]);assert run(['docker','inspect','--format','{{.State.ExitCode}}',worker])=='0'
 logs=run(['docker','logs',worker]);(base/(worker+'.log')).write_text(logs)
 run(['docker','rm',worker]);worker=None
try:
 run(['docker','network','create','--internal','--label','io.project1.decision='+project,network])
 for kind in ['pg','redis','uploads','data']:
  name=project+'-'+kind;volumes.append(name);run(['docker','volume','create','--label','io.project1.decision='+project,name])
 create(project+'-pg','postgres:15',['--network-alias','postgres','--cpus','0.4','--memory','512m','--memory-swap','512m',
  '-e','POSTGRES_DB=project1_test_decision','-e','POSTGRES_USER=project1_test','-e','POSTGRES_PASSWORD='+password,'-v',project+'-pg:/var/lib/postgresql/data'],[])
 create(project+'-redis','redis:7-alpine',['--network-alias','redis','--cpus','0.15','--memory','128m','--memory-swap','128m','-v',project+'-redis:/data'],['redis-server','--appendonly','yes','--appendfsync','always'])
 for attempt in range(40):
  check=subprocess.run(['docker','exec',project+'-pg','pg_isready','-h','127.0.0.1','-U','project1_test','-d','project1_test_decision'],capture_output=True)
  if check.returncode==0:break
  time.sleep(.3)
 assert check.returncode==0
 if not __LEGACY_QUEUE__:
  dump=pathlib.Path('/root/project1-backups/20261029T130619Z/payload/database.dump')
  assert hashlib.sha256(dump.read_bytes()).hexdigest()=='5b500a9868e5a70019a51ac397514ea12df1619b1e5ce723751bc098b13caf63'
  with dump.open('rb') as inp:
   restore=subprocess.run(['docker','exec','-i',project+'-pg','pg_restore','-U','project1_test','-d','project1_test_decision','--no-owner','--no-privileges','--exit-on-error'],stdin=inp,capture_output=True)
   (base/'initial-restore.stderr').write_bytes(restore.stderr)
   assert restore.returncode==0,'Initial restore of isolated copy failed; see private initial-restore.stderr'
  result['initial_copy_restore_count']=1
 create(project+'-coordinator',backend,['--env-file',str(envfiles['coordinator']),'--network-alias','smtp','--network-alias','imap',
  '--cpus','0.50' if __LEGACY_QUEUE__ else '0.75','--memory','768m' if __LEGACY_QUEUE__ else '1024m','--memory-swap','768m' if __LEGACY_QUEUE__ else '1024m','--cap-drop','ALL','--read-only','--tmpfs','/tmp:size=128m',
  '-v',project+'-data:/data','-v',project+'-uploads:/app/uploads','-v',str(fixture)+':/fixtures:ro'],['python','-B','/fixtures/fixtures.py'])
 time.sleep(.5)
 if __LEGACY_QUEUE__:
  run(['docker','exec','-w','/app/backend',project+'-coordinator','python','-m','alembic','upgrade','0004'])
 result['seed']=step('seed')
 if __LEGACY_QUEUE__:
  exec((fixture/'legacy_queue_host.py').read_text())
 elif __OPERATOR__:
  run(['docker','exec','-w','/app/backend',project+'-coordinator','python','-m','alembic','upgrade','head'])
  start_worker('v2');result['scenarios']['operator_canary']=step('operator-canary')
  result['scenarios']['native_v2_readonly_probe']=step('preflight-probe');result['final_state']=step('state');stop_worker()
 else:
  start_worker('legacy');result['scenarios']['legacy_before']=step('legacy-before');stop_worker()
  run(['docker','exec','-w','/app/backend',project+'-coordinator','python','-m','alembic','upgrade','head'])
  start_worker('v2');result['scenarios']['A_barrier']=step('A-barrier');stop_worker()
  start_worker('legacy');result['scenarios']['A_legacy_resumed']=step('legacy-after');stop_worker()
  start_worker('v2');result['scenarios']['canary']=step('canary');result['before_pause']=step('state');stop_worker()
  result['scenarios']['B_during_pause']=step('during')
  start_worker('v2');result['scenarios']['B_v2_resumed']=step('resume')
  result['scenarios']['B_unknown_outcome']=step('uncertain');result['final_state']=step('state');stop_worker()
 result['post_failure_restore_count']=0;result['schema_downgrade_count']=0;result['parallel_senders_maximum']=1
 assert not aborted.is_set();result['targeted_rehearsal']='PASS'
except Exception:
 try:result['failure_state']=step('state')
 except Exception:pass
 raise
finally:
 done.set();thread.join(timeout=6)
 for name in names:
  p=subprocess.run(['docker','logs',name],capture_output=True,text=True)
  if p.returncode==0:(base/(name+'.log')).write_text(p.stdout+p.stderr)
  subprocess.run(['docker','rm','-f',name],capture_output=True)
 for name in volumes:subprocess.run(['docker','volume','rm',name],capture_output=True)
 subprocess.run(['docker','network','rm',network],capture_output=True)
 for path in envfiles.values():path.unlink(missing_ok=True)
 result['production_unchanged']=fingerprint(json.loads(run(['docker','inspect',*baseline])))==baseline
 result['cleanup_remaining']={kind:run(command+['--filter','label=io.project1.decision='+project]) for kind,command in [
  ('containers',['docker','ps','-aq']),('volumes',['docker','volume','ls','-q']),('networks',['docker','network','ls','-q'])]}
 (base/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps({'result_file':str(base/'result.json'),'scenarios':result.get('scenarios'),
 'production_unchanged':result['production_unchanged'],'cleanup_remaining':result['cleanup_remaining']}))
'''.replace('__PROJECT__',repr(project)).replace('__BASE__',repr(remote)).replace('__HASH__',repr(hashlib.sha256(package.read_bytes()).hexdigest())).replace('__OPERATOR__',repr(args.operator_only)).replace('__LEGACY_QUEUE__',repr(args.legacy_queue_only))
p=subprocess.run(['ssh','-T',*opts,'root@192.0.2.10','python3','-B','-'],input=script,text=True,encoding='utf8',capture_output=True)
(out/'rehearsal-console.log').write_text(p.stdout+p.stderr,encoding='utf8',newline='\n')
print(p.stdout[-5000:]);print(p.stderr[-2200:] if p.returncode else '')
raise SystemExit(p.returncode)
