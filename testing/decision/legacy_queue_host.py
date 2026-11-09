"""Executed only inside the existing isolated runner's host orchestration scope."""
import sys,copy,socket,base64
assert base.parent==pathlib.Path('/root/project1-rc-tests') and project.startswith('project1-decision-')
sys.path.insert(0,str(fixture))
from inspect_legacy_queue import collect
from queue_policy import classify,prepare,verify
from preflight import IMAGES
result['limits']={'cpu':2.0,'memory_mib':2304}
result['initial_copy_restore_count']=0
start_worker('legacy')
producer=project+'-producer'
create(producer,IMAGES['old_beat'],['--env-file',str(envfiles['legacy']),'-e','PYTHONUNBUFFERED=1','--cpus','0.25','--memory','384m','--memory-swap','384m',
 '--read-only','--tmpfs','/tmp:size=32m','--cap-drop','ALL'],['celery','-A','celery_app','beat','--loglevel=INFO','--schedule=/tmp/beat'])
for attempt in range(45):
    log=subprocess.run(['docker','logs',producer],capture_output=True,text=True)
    if 'beat: Starting' in log.stdout+log.stderr:break
    time.sleep(1)
else:raise RuntimeError('Own producer did not finish startup; do not test shutdown before its signal handlers exist')
result['scenarios']['actual_old_first_recovery']=step('first-cycle')
result['scenarios']['actual_old_repeat_recovery']=step('second-cycle')
ident=result['seed']['attachment_id']
def snapshot():return collect(worker,project+'-coordinator',[ident],True,producer,'legacy@decision',network)
before=snapshot();before.pop('escrow');classification=classify(before);plan=prepare(before,classification)
permit=verify(before,plan,'drain')
assert permit['verdict']=='DRAIN_ALLOWED',permit
assert classification['summary']['MISSING_PDF']['messages']==1 and classification['summary']['RETRY_DUPLICATE']['messages']==7
stopped=copy.deepcopy(before);stopped['worker_after']['active']=[{'task_id':'unexplained'}]
unchanged_before=run(['docker','exec',project+'-redis','redis-cli','HLEN','unacked'])
assert verify(stopped,plan,'drain')['verdict']=='STOP'
assert unchanged_before==run(['docker','exec',project+'-redis','redis-cli','HLEN','unacked'])
result['test_producer_before_stop']=json.loads(run(['docker','inspect','--format','{{json .State}}',producer]))
run(['docker','stop','--time','30',producer]);result['test_producer_after_stop']=json.loads(run(['docker','inspect','--format','{{json .State}}',producer]))
assert result['test_producer_after_stop']['ExitCode']==0,result['test_producer_after_stop']
post_stop=snapshot();post_stop.pop('escrow');assert verify(post_stop,plan,'drain')['verdict']=='DRAIN_ALLOWED'
result['scenarios']['producer_stop_then_natural_drain']=step('await-drain')
result['scenarios']['completed_document_guard']=step('completed-guard')
drained=snapshot();drained.pop('escrow');drained_proof=verify(drained,plan,'drained')
assert drained_proof['verdict']=='DRAINED',drained_proof
run(['docker','stop','--time','30',worker]);assert run(['docker','inspect','--format','{{.State.ExitCode}}',worker])=='0'
final=snapshot();keys=final.pop('escrow')
escrow=(json.dumps({'format':'redis-DUMP/v1','keys':keys,'handover_plan':plan},sort_keys=True)+'\n').encode()
final['escrow_sha256']=hashlib.sha256(escrow).hexdigest()
peer={'sha256':final['escrow_sha256'],'host':'isolated-fixture-peer'}
ready=verify(final,plan,'fenced',escrow,peer,socket.gethostname());assert ready['verdict']=='CUTOVER_READY',ready
assert verify(final,plan,'fenced',escrow,dict(peer,sha256='wrong'),socket.gethostname())['verdict']=='STOP'
for name,value in [('inventory-before.json',before),('classification.json',classification),('plan.json',plan),('drained-proof.json',drained_proof),('fenced-inventory.json',final),('ready.json',ready)]:
 (base/name).write_text(json.dumps(value,indent=2))
(base/'escrow.json').write_bytes(escrow)
result['scenarios']['queue_policy_and_fencing']={'observed_messages':8,'logical_documents':1,'duplicates':7,
 'drain_permitted':permit['verdict'],'pre_stop_barrier':drained_proof['verdict'],'final_gate':ready['verdict'],
 'active_STOP_preserved_transport':True,'wrong_peer_copy_STOP':True,'held_operations':ready['held_operations'],
 'automatic_replay':0,'escrow_peer_receipt':'simulated different host in isolated test; real cutover requires actual copied bytes receipt'}
cases=step('pending-fixtures');blocked=snapshot();blocked.pop('escrow')
for doc in blocked['documents']:
    if doc['attachment_id']==cases['VALID_PENDING']['attachment_id']:doc['operator_no_smtp_proof_sha256']='a'*64
    if doc['attachment_id']==cases['SMTP_UNCERTAIN']['attachment_id']:doc['smtp_uncertain']=True
blocked_plan=prepare(blocked,classify(blocked))
assert len(blocked_plan['operations'])==3  # Missing PDF hold survives an empty old queue and later new operations.
failed=verify(blocked,blocked_plan,'fenced',escrow,peer,socket.gethostname())
assert failed['verdict']=='STOP' and any('VALID_PENDING' in r for r in failed['reasons']) and any('SMTP_UNCERTAIN' in r for r in failed['reasons'])
after=snapshot();after.pop('escrow');assert after['transport_key_sha256']==blocked['transport_key_sha256']
for name,value in [('blocked-inventory.json',blocked),('blocked-plan.json',blocked_plan),('blocked-proof.json',failed)]:
    (base/name).write_text(json.dumps(value,indent=2))
result['scenarios']['required_and_uncertain_preserved']={'native_redis_messages_retained':len(after['deliveries']),
 'held_operations':len(blocked_plan['operations']),'verdict':'STOP','transport_unchanged':True,'smtp_effects':0}
result['final_state']=step('state');worker=None
